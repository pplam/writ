"""The committed plan on disk: a small index plus one file per feature.

The store (`.writ/store.db`) is the source of truth. These files are its plan-phase projection,
written so that an agent (a critic, the adjudicator) can be pointed at them and
read what it needs, instead of having the whole graph pasted into its prompt.

    .writ/plans/<plan-id>/
        plan.json               the index: requirements, milestones, one row per feature
        features/<task-id>.json one dispatchable unit in full
        rounds/r<rev>/          everything done against a revision:
            known-findings.json what writ already found, for the critics
            <critic>/           each critic's report and transcript
            adjudicate-<n>/     each plan-repair attempt

Every path recorded here or shown to an agent is relative to the repository
root; see `rel`.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from . import contracts, plans, state

INDEX_FILENAME = "plan.json"
DRAFT_FILENAME = "draft.json"
FEATURES_DIRNAME = "features"
ROUNDS_DIRNAME = "rounds"

#: the fields a feature file carries, in the order they are written. `id`,
#: `kind`, `milestone` and `status` are context; the rest is what a repair may edit.
FEATURE_FIELDS = (
    "id",
    "title",
    "kind",
    "milestone",
    "status",
    "goal",
    "owns",
    "provides",
    "consumes",
    "notes",
    "design_section",
    "requirement_ids",
    "depends_on",
    "acceptances",
    "allowed",
    "forbidden",
)

#: the fields only a feature (docs/planning-redesign.md §4) carries; a plain
#: task's file leaves them out rather than writing them empty
CONTRACT_FIELDS = ("goal", "owns", "provides", "consumes")

#: what a plan repair may change on a feature
EDITABLE_FIELDS = (
    "title",
    "goal",
    "owns",
    "provides",
    "consumes",
    "notes",
    "design_section",
    "requirement_ids",
    "depends_on",
    "acceptances",
    "allowed",
    "forbidden",
)


def rel(root: str | os.PathLike[str], path: str | os.PathLike[str]) -> str:
    """`path` relative to the repository root, when it is under it."""
    target = Path(path)
    base = Path(root).resolve()
    try:
        return target.resolve().relative_to(base).as_posix()
    except ValueError:
        return str(target)


def new_id(doc: str | os.PathLike[str] | None) -> str:
    stem = Path(doc).stem if doc else "plan"
    stem = re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-").lower() or "plan"
    return f"{stem}-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}"


def current_id(data: dict[str, Any]) -> str | None:
    return plans.plan_status(data).get("id")


def assign_id(data: dict[str, Any], plan_id: str | None = None) -> str:
    """Record which plan directory the committed graph lives in.

    A project planned before plan files existed gets one minted from its first
    design document, the first time anything asks.
    """
    record = plans.plan_status(data)
    if plan_id:
        record["id"] = plan_id
    elif not record.get("id"):
        docs = data.get("design_docs") or [None]
        record["id"] = new_id(docs[0])
    return record["id"]


def directory(root: str | os.PathLike[str], data: dict[str, Any]) -> Path:
    return state.plan_dir(root, assign_id(data))


def index_path(root, data) -> Path:
    return directory(root, data) / INDEX_FILENAME


def features_dir(root, data) -> Path:
    return directory(root, data) / FEATURES_DIRNAME


def rounds_dir(root, data, revision: int | None = None) -> Path:
    rev = plans.revision(data) if revision is None else revision
    return directory(root, data) / ROUNDS_DIRNAME / f"r{rev}"


# --------------------------------------------------------------------------
# building the projection


def feature(task: dict[str, Any]) -> dict[str, Any]:
    """One task as its feature file holds it."""
    payload = {
        "id": task["id"],
        "title": task.get("title", ""),
        "kind": task.get("kind", "task"),
        "milestone": task.get("milestone"),
        "status": task.get("status", "planned"),
        "notes": task.get("notes", "") or "",
        "design_section": task.get("design_section"),
        "requirement_ids": list(task.get("requirement_ids", [])),
        "depends_on": list(task.get("depends_on", [])),
        "acceptances": [item["text"] for item in task.get("acceptances", [])],
        "allowed": list(task.get("allowed", [])),
        "forbidden": list(task.get("forbidden", [])),
    }
    if contracts.is_feature(task):
        payload["goal"] = task.get("goal", "") or ""
        for key in ("owns", "provides", "consumes"):
            payload[key] = list(task.get(key) or [])
    # written in FEATURE_FIELDS order, so a diff of two exports reads cleanly
    return {key: payload[key] for key in FEATURE_FIELDS if key in payload}


def requirement_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The fixed inventory as the index shows it: each capability and its details."""
    return [
        {
            "id": record.get("id", req_id),
            "priority": record.get("priority"),
            "status": record.get("status"),
            "text": record.get("text", ""),
            "details": list(record.get("details") or []),
        }
        for req_id, record in sorted(plans.requirements(data).items())
    ]


def index(root: str | os.PathLike[str], data: dict[str, Any]) -> dict[str, Any]:
    """The plan at a glance: enough to see the whole graph without opening a feature."""
    folder = features_dir(root, data)
    docs = data.get("design_docs") or []
    return {
        "plan_id": assign_id(data),
        "revision": plans.revision(data),
        "status": plans.plan_status(data).get("status"),
        "design_docs": [rel(root, doc) for doc in docs],
        "requirements": requirement_rows(data),
        "milestones": [
            {
                "id": milestone_id,
                "title": milestone.get("title", ""),
                "tasks": list(milestone.get("tasks", [])),
            }
            for milestone_id, milestone in sorted(data.get("milestones", {}).items())
        ],
        "features": [
            {
                "id": task_id,
                "title": task.get("title", ""),
                "kind": task.get("kind", "task"),
                "milestone": task.get("milestone"),
                "status": task.get("status"),
                **(
                    {
                        "owns": list(task.get("owns") or []),
                        "provides": contracts.names(task.get("provides") or []),
                        "consumes": contracts.names(task.get("consumes") or []),
                    }
                    if contracts.is_feature(task)
                    else {}
                ),
                "depends_on": list(task.get("depends_on", [])),
                "requirement_ids": list(task.get("requirement_ids", [])),
                "file": rel(root, folder / f"{task_id}.json"),
            }
            for task_id, task in sorted(data.get("tasks", {}).items())
        ],
    }


def dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _render(payload: Any) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def _put(path: Path, payload: Any) -> bool:
    """Write `payload` unless the file already holds exactly it. True if written."""
    text = _render(payload)
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except (OSError, UnicodeDecodeError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def write_features(folder: Path, tasks: dict[str, dict[str, Any]]) -> list[Path]:
    """Write one file per task into `folder`, removing files for tasks that are gone.

    A file that already says what it should is left alone, so refreshing the
    view after every commit costs a read per feature, not a write. Returns the
    files it wrote or removed.
    """
    folder.mkdir(parents=True, exist_ok=True)
    touched: list[Path] = []
    for stale in folder.glob("*.json"):
        if stale.stem not in tasks:
            stale.unlink()
            touched.append(stale)
    for task_id, task in tasks.items():
        path = folder / f"{task_id}.json"
        if _put(path, feature(task)):
            touched.append(path)
    return touched


def export(root: str | os.PathLike[str], data: dict[str, Any]) -> Path:
    """Write the index and every feature file for the current revision."""
    write_features(features_dir(root, data), data.get("tasks", {}))
    path = index_path(root, data)
    _put(path, index(root, data))
    return path


def refresh(root: str | os.PathLike[str], data: dict[str, Any]) -> list[Path]:
    """Bring an exported plan's files up to date. Returns what had to change.

    Only a plan that has been exported: before that there is nothing to keep
    current, and exporting would mint an id for a plan still being drafted.
    """
    if not current_id(data) or not directory(root, data).is_dir():
        return []
    touched = write_features(features_dir(root, data), data.get("tasks", {}))
    path = index_path(root, data)
    if _put(path, index(root, data)):
        touched.append(path)
    return touched


def drift(root: str | os.PathLike[str], data: dict[str, Any]) -> list[Path]:
    """The plan files that no longer say what the store says: edited by hand, or
    by an agent that wrote outside its output. Nothing is changed."""
    if not current_id(data) or not directory(root, data).is_dir():
        return []
    expected = {
        features_dir(root, data) / f"{task_id}.json": feature(task)
        for task_id, task in data.get("tasks", {}).items()
    }
    expected[index_path(root, data)] = index(root, data)
    found = [
        path
        for path, payload in expected.items()
        if not path.exists() or path.read_text(encoding="utf-8", errors="replace") != _render(payload)
    ]
    found.extend(
        path for path in features_dir(root, data).glob("*.json") if path not in expected
    )
    return sorted(found)


#: fields of a feature file that are progress, not plan: a changeset leaves them out
PROGRESS_FIELDS = ("status",)

_ABSENT = object()


def field_changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Field by field, what differs: `before` and `after`, either missing if the
    field was added or deleted."""
    fields: dict[str, Any] = {}
    for key in sorted(set(before) | set(after)):
        if before.get(key, _ABSENT) == after.get(key, _ABSENT):
            continue
        change: dict[str, Any] = {}
        if key in before:
            change["before"] = before[key]
        if key in after:
            change["after"] = after[key]
        fields[key] = change
    return fields


def changeset(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """What changed between two task maps, as feature files: added, removed, modified."""
    def plan_of(task: dict[str, Any]) -> dict[str, Any]:
        entry = feature(task)
        for key in PROGRESS_FIELDS:
            entry.pop(key, None)
        return entry

    modified = {}
    for task_id in sorted(set(before) & set(after)):
        fields = field_changes(plan_of(before[task_id]), plan_of(after[task_id]))
        if fields:
            modified[task_id] = fields
    return {
        "added": {task_id: plan_of(after[task_id]) for task_id in sorted(set(after) - set(before))},
        "removed": sorted(set(before) - set(after)),
        "modified": modified,
    }


def ensure(root: str | os.PathLike[str], data: dict[str, Any]) -> Path:
    """Bring the files up to date with `data` before anything reads them.

    Always a full re-export: task statuses move without a revision bump, and a
    projection that is cheap to rewrite is not worth a staleness rule that can
    be wrong. `data` may gain a plan id; a caller outside a transaction that
    wants it kept has to save it.
    """
    return export(root, data)
