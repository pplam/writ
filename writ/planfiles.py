"""The committed plan on disk: a small index plus one file per feature.

`state.json` is the source of truth. These files are its plan-phase projection,
written so that an agent (a critic, the adjudicator) can be pointed at them and
read what it needs, instead of having the whole graph pasted into its prompt.

    .writ/plans/<plan-id>/
        plan.json               the index: requirements, milestones, one row per feature
        features/<task-id>.json one dispatchable unit in full
        reviews/r<rev>/         critic reports for a revision
        rounds/r<rev>/          plan-repair attempts against a revision

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
REVIEWS_DIRNAME = "reviews"
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


def reviews_dir(root, data, revision: int | None = None) -> Path:
    rev = plans.revision(data) if revision is None else revision
    return directory(root, data) / REVIEWS_DIRNAME / f"r{rev}"


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


def write_features(folder: Path, tasks: dict[str, dict[str, Any]]) -> None:
    """Write one file per task into `folder`, removing files for tasks that are gone."""
    folder.mkdir(parents=True, exist_ok=True)
    for stale in folder.glob("*.json"):
        if stale.stem not in tasks:
            stale.unlink()
    for task_id, task in tasks.items():
        dump(folder / f"{task_id}.json", feature(task))


def export(root: str | os.PathLike[str], data: dict[str, Any]) -> Path:
    """Write the index and every feature file for the current revision."""
    write_features(features_dir(root, data), data.get("tasks", {}))
    path = index_path(root, data)
    dump(path, index(root, data))
    return path


def ensure(root: str | os.PathLike[str], data: dict[str, Any]) -> Path:
    """Bring the files up to date with `data` before anything reads them.

    Always a full re-export: task statuses move without a revision bump, and a
    projection that is cheap to rewrite is not worth a staleness rule that can
    be wrong. `data` may gain a plan id; a caller outside a transaction that
    wants it kept has to save it.
    """
    return export(root, data)
