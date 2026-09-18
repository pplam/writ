"""The read model behind `writ serve`.

Every function here takes the store and returns plain JSON-able data. Nothing
mutates, nothing caches, and no HTTP or presentation detail leaks in — the
handler is a thin shell over these, and the TypeScript app is a renderer for what
they return.

Two rules keep the payloads honest:

**One request, one consistent read.** Each view loads the store once and answers
from that snapshot, so a page cannot paint a task as running from one read and
its run as finished from another.

**Derived state is derived here, not in the browser.** `ready` is computed from
dependencies, milestone progress from its tasks, durations from timestamps. The
alternative is reimplementing writ's rules in TypeScript and having them drift.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from . import render, runner, state, verdict
from .model import (
    acceptance_summary,
    blocking_dependencies,
    effective_status,
    milestone_tasks,
)

#: Log tail sent with a run detail. Enough to see how an agent is doing without
#: shipping a megabyte of transcript into a JSON payload; the full file stays one
#: request away.
LOG_TAIL_BYTES = 60_000

#: Node geometry for the graph view. Here rather than in the TypeScript because
#: the layout maths that uses it is here.
NODE_WIDTH = 200
NODE_HEIGHT = 64
COLUMN_GAP = 92
ROW_GAP = 24
MARGIN = 28


# ---------------------------------------------------------------- overview


def overview(data: dict[str, Any]) -> dict[str, Any]:
    """The header and the summary panels: where the project stands."""
    tasks = data["tasks"]
    counts: dict[str, int] = {}
    for task in tasks.values():
        status = _status(data, task)
        counts[status] = counts.get(status, 0) + 1
    active = [
        run
        for run in data["runs"].values()
        if run["status"] in runner.ACTIVE_RUN_STATUSES
    ]
    return {
        "project": data.get("project", {}).get("name") or "",
        "design_docs": data.get("design_docs", []),
        "counts": counts,
        "tasks": len(tasks),
        "completed": counts.get("completed", 0),
        "live": counts.get("running", 0) + counts.get("reviewing", 0),
        "milestones": [_milestone_row(data, m) for m in _sorted_milestones(data)],
        "active_runs": [_run_row(run) for run in sorted(active, key=_run_key)],
        "proposed_decisions": sum(
            1 for d in data.get("decisions", []) if d.get("status") == "proposed"
        ),
        "throughput": _throughput(data),
    }


def _throughput(data: dict[str, Any]) -> dict[str, Any]:
    """What the agents have cost so far, in runs and in time.

    A project's real progress metric is not how many tasks exist but how much
    agent work went into them, and how often a reviewer sent one back.
    """
    runs = list(data["runs"].values())
    finished = [r for r in runs if r.get("started_at") and r.get("finished_at")]
    durations = [_duration(r) for r in finished]
    reviews = [r for r in runs if r.get("role") == "reviewer" and r.get("verdict")]
    rejected = sum(1 for r in reviews if r["verdict"].get("decision") == "reject")
    return {
        "runs": len(runs),
        "agent_seconds": round(sum(d for d in durations if d), 1),
        "median_seconds": round(_median([d for d in durations if d]), 1),
        "reviews": len(reviews),
        "rejected": rejected,
        "failures": sum(1 for r in runs if r["status"] == "failed"),
    }


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


# ---------------------------------------------------------------- milestones


def milestones(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [_milestone_row(data, m) for m in _sorted_milestones(data)]


def milestone(data: dict[str, Any], milestone_id: str) -> dict[str, Any]:
    found = data["milestones"].get(milestone_id)
    if found is None:
        raise KeyError(milestone_id)
    row = _milestone_row(data, found)
    row["tasks"] = [
        task_row(data, task) for task in milestone_tasks(data, milestone_id)
    ]
    row["design_section"] = found.get("design_section") or ""
    row["notes"] = found.get("notes") or ""
    return row


def _sorted_milestones(data: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(data["milestones"].values(), key=lambda m: m["id"])


def _milestone_row(data: dict[str, Any], found: dict[str, Any]) -> dict[str, Any]:
    tasks = milestone_tasks(data, found["id"])
    done = sum(1 for task in tasks if task["status"] == "completed")
    statuses: dict[str, int] = {}
    for task in tasks:
        status = _status(data, task)
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "id": found["id"],
        "title": found.get("title", ""),
        "status": found.get("status", ""),
        "done": done,
        "total": len(tasks),
        "counts": statuses,
    }


# ---------------------------------------------------------------- tasks


def tasks(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        task_row(data, task)
        for task in sorted(data["tasks"].values(), key=lambda t: t["id"])
    ]


def task_row(data: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    """The summary form: enough for a list row or a graph node."""
    counts = acceptance_summary(task)
    return {
        "id": task["id"],
        "title": task.get("title", ""),
        "milestone": task.get("milestone", ""),
        "status": _status(data, task),
        "stored_status": task["status"],
        "passed": counts["passed"],
        "total": counts["total"],
        "depends_on": [d for d in task.get("depends_on", []) if d in data["tasks"]],
        "unknown_deps": _unknown_deps(data, task),
        "blocked_by": _blocked_by(data, task),
        "blocks": sorted(
            other["id"]
            for other in data["tasks"].values()
            if task["id"] in other.get("depends_on", [])
        ),
        "runs": len(task.get("runs", [])),
    }


def task(data: dict[str, Any], task_id: str) -> dict[str, Any]:
    """The detail form: criteria with evidence, guardrails, history, runs."""
    found = data["tasks"].get(task_id)
    if found is None:
        raise KeyError(task_id)
    row = task_row(data, found)
    row.update(
        {
            "design_doc": found.get("design_doc") or "",
            "design_section": found.get("design_section") or "",
            "notes": found.get("notes") or "",
            "allowed": found.get("allowed", []),
            "forbidden": found.get("forbidden", []),
            "acceptances": [
                {
                    "number": index,
                    "text": item.get("text", ""),
                    "status": item.get("status", "unmet"),
                    "evidence": item.get("evidence", ""),
                    "by": item.get("by", ""),
                    "at": item.get("at", ""),
                }
                for index, item in enumerate(found.get("acceptances", []), start=1)
            ],
            "evidence": found.get("evidence", []),
            "run_list": [
                _run_row(data["runs"][run_id])
                for run_id in found.get("runs", [])
                if run_id in data["runs"]
            ],
            "created_at": found.get("created_at", ""),
            "updated_at": found.get("updated_at", ""),
        }
    )
    return row


def _blocked_by(data: dict[str, Any], task: dict[str, Any]) -> list[str]:
    """Incomplete dependencies, or [] for a task whose deps are unknown.

    `blocking_dependencies` raises on a dangling id, which is right for a command
    that should refuse a corrupt store. A read model that raised here would take
    the whole page down over one bad edge — and this is the surface someone uses
    to find out *what* is wrong, so failing to render is the least helpful thing
    it could do. It answers what it can, and `unknown_deps` names what it could
    not, so the omission is reported rather than silent.
    """
    try:
        return blocking_dependencies(data, task)
    except Exception:
        return []


def _unknown_deps(data: dict[str, Any], task: dict[str, Any]) -> list[str]:
    """Dependency ids that name no task. Empty for every healthy store."""
    return [d for d in task.get("depends_on", []) if d not in data["tasks"]]


def _status(data: dict[str, Any], task: dict[str, Any]) -> str:
    """`effective_status`, but a corrupt edge does not stop the page rendering.

    A task whose dependencies cannot be resolved has no derivable readiness, so
    it is reported with the status actually stored against it. `unknown_deps` on
    the same row is what tells the reader why it looks stuck.
    """
    try:
        return effective_status(data, task)
    except Exception:
        return task["status"]


# ---------------------------------------------------------------- runs


def runs(data: dict[str, Any], *, limit: int = 200) -> list[dict[str, Any]]:
    """Newest first: a run log is read from the top."""
    ordered = sorted(data["runs"].values(), key=_run_key, reverse=True)
    return [_run_row(run) for run in ordered[:limit]]


def _run_key(run: dict[str, Any]) -> str:
    return run.get("started_at") or run.get("created_at") or run["id"]


def _run_row(run: dict[str, Any]) -> dict[str, Any]:
    reported = run.get("verdict") or {}
    return {
        "id": run["id"],
        "task": run.get("task", ""),
        "role": run.get("role", "agent"),
        "status": run["status"],
        "command": " ".join(run.get("command", [])),
        "model": run.get("model") or "",
        "exit_code": run.get("exit_code"),
        "created_at": run.get("created_at", ""),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "duration": _duration(run),
        "resulting_status": run.get("resulting_status") or "",
        "verdict_error": run.get("verdict_error") or "",
        # Distinct from verdict_error: nothing was written, rather than something
        # unusable. The confusing case on a page, because exit 0 looks fine.
        "no_verdict": run.get("no_verdict") or "",
        # ...and within that, whether the agent said anything at all. A silent
        # exit is a failed invocation, not a skipped report, and the two have
        # completely different remedies.
        "no_output": bool(run.get("no_output")),
        # ...or said a great deal and still never acted, because its last tool call
        # was printed rather than made. Points at the model's call syntax, which is
        # a different remedy again.
        "unparsed_tool_call": bool(run.get("unparsed_tool_call")),
        # A claim writ lowered to match the criteria. Not an error: the verdict
        # was applied, just not as headlined.
        "verdict_downgraded": run.get("verdict_downgraded") or "",
        # Where a verdict was read from, when the agent did not use the path it
        # was given. Not an error — the report was used — but worth seeing, since
        # an agent that does this once will do it again.
        "verdict_misplaced": run.get("verdict_misplaced") or "",
        "decision": reported.get("decision") or reported.get("outcome") or "",
        "summary": reported.get("summary") or "",
        "unmet": reported.get("unmet", []),
        "decisions": reported.get("decisions", []),
        "note": run.get("note") or "",
    }


def run(data: dict[str, Any], root: Path, run_id: str) -> dict[str, Any]:
    """One run, with the prompt it was given and the output it produced.

    The prompt is the most useful and least visible artifact writ has: it is
    exactly what the agent was told, and reading it is how you find out why an
    agent did something strange. It is on disk already; this just surfaces it.
    """
    found = data["runs"].get(run_id)
    if found is None:
        raise KeyError(run_id)
    row = _run_row(found)
    directory = Path(found.get("dir", ""))
    row.update(
        {
            "dir": str(directory),
            "cwd": found.get("cwd", ""),
            "timeout": found.get("timeout"),
            "pid": found.get("pid"),
            "prompt": _read(directory / "prompt.txt"),
            "stdout": _tail(directory / "stdout.log"),
            "stderr": _tail(directory / "stderr.log"),
            "verdict": _verdict_file(directory),
            "verdict_raw": _read(directory / verdict.VERDICT_FILENAME),
        }
    )
    return row


def log(root: Path, data: dict[str, Any], run_id: str, stream: str) -> str:
    """A whole log file, for the download link on a run page."""
    found = data["runs"].get(run_id)
    if found is None:
        raise KeyError(run_id)
    if stream not in ("stdout", "stderr", "prompt"):
        raise ValueError(stream)
    name = "prompt.txt" if stream == "prompt" else f"{stream}.log"
    return _read(Path(found.get("dir", "")) / name)


def _verdict_file(directory: Path) -> dict[str, Any] | None:
    """The verdict as structured data, if the agent wrote one that parses."""
    path = directory / verdict.VERDICT_FILENAME
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None  # shown raw instead, so a malformed file is still readable


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _tail(path: Path, limit: int = LOG_TAIL_BYTES) -> dict[str, Any]:
    """The end of a log, with a flag so the page can say it is partial."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > limit:
                handle.seek(size - limit)
            body = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return {"text": "", "bytes": 0, "truncated": False}
    return {"text": body, "bytes": size, "truncated": size > limit}


def _duration(run: dict[str, Any]) -> float | None:
    started, finished = run.get("started_at"), run.get("finished_at")
    if not started:
        return None
    try:
        begin = datetime.fromisoformat(started)
        end = datetime.fromisoformat(finished) if finished else _now(begin)
    except ValueError:
        return None
    return round((end - begin).total_seconds(), 1)


def _now(reference: datetime) -> datetime:
    """Now, in the same awareness as the stored timestamp.

    Run timestamps are written by `state.utcnow`; comparing one of those to a
    naive `datetime.now()` raises, and a live run would show no duration at all.
    """
    from datetime import timezone

    if reference.tzinfo is None:
        return datetime.utcnow()
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- decisions


def decisions(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Newest first, proposals before settled ones.

    A proposal is a question waiting on a human; an active decision is history.
    Sorting the questions to the top is the whole reason this view exists.
    """
    order = {"proposed": 0, "active": 1, "superseded": 2, "rejected": 3}
    items = [
        {
            "id": item["id"],
            "title": item.get("title", ""),
            "status": item.get("status", ""),
            "by": item.get("by", ""),
            "at": item.get("at", ""),
            "context": item.get("context", ""),
            "decision": item.get("decision", ""),
            "consequences": item.get("consequences", ""),
            "task": item.get("task", ""),
            "reason": item.get("reason", ""),
            "supersedes": item.get("supersedes", ""),
        }
        for item in data.get("decisions", [])
    ]
    return sorted(
        items, key=lambda d: (order.get(d["status"], 9), d["id"]), reverse=False
    )


# ---------------------------------------------------------------- graph


def graph(data: dict[str, Any]) -> dict[str, Any]:
    """The DAG with geometry, laid out by dependency depth."""
    task_map = data["tasks"]
    nodes = _layout(data, task_map)
    placed = {node["id"]: node for node in nodes}
    return {
        "nodes": nodes,
        "edges": _edges(task_map, placed),
        "width": max((n["x"] + NODE_WIDTH for n in nodes), default=0) + MARGIN,
        "height": max((n["y"] + NODE_HEIGHT for n in nodes), default=0) + MARGIN,
        "levels": len(render.dag_levels(task_map)),
    }


def _layout(data: dict[str, Any], task_map: dict[str, Any]) -> list[dict[str, Any]]:
    """Depth across, siblings down.

    Depth is what decides when a task can start, so a column is "work that could
    run at once" and the picture shows the parallelism the DAG allows. Within a
    column, tasks are ordered by the mean row of their dependencies, which keeps
    an edge from crossing the whole diagram when a short one would do.
    """
    levels = render.dag_levels(task_map)
    rows: dict[str, int] = {}
    nodes: list[dict[str, Any]] = []
    for column, level in enumerate(levels):

        def anchor(task_id: str) -> tuple[float, str]:
            deps = [d for d in task_map[task_id].get("depends_on", []) if d in rows]
            mean = sum(rows[d] for d in deps) / len(deps) if deps else -1.0
            return (mean, task_id)

        for row, task_id in enumerate(sorted(level, key=anchor)):
            rows[task_id] = row
            node = task_row(data, task_map[task_id])
            node.update(
                {
                    "x": MARGIN + column * (NODE_WIDTH + COLUMN_GAP),
                    "y": MARGIN + row * (NODE_HEIGHT + ROW_GAP),
                    "column": column,
                }
            )
            nodes.append(node)
    return nodes


def _edges(
    task_map: dict[str, Any], placed: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    out = []
    for task_id, task in sorted(task_map.items()):
        for dep in task.get("depends_on", []):
            if dep not in placed or task_id not in placed:
                continue
            source, target = placed[dep], placed[task_id]
            out.append(
                {
                    "from": dep,
                    "to": task_id,
                    "x1": source["x"] + NODE_WIDTH,
                    "y1": source["y"] + NODE_HEIGHT / 2,
                    "x2": target["x"],
                    "y2": target["y"] + NODE_HEIGHT / 2,
                    "satisfied": source["status"] == "completed",
                }
            )
    return out


# ---------------------------------------------------------------- activity


def activity(data: dict[str, Any], *, limit: int = 60) -> list[dict[str, Any]]:
    """A merged timeline of what has happened, newest first.

    Runs, verdicts and decisions are three separate records that describe one
    sequence of events. Reading them side by side is how you reconstruct a
    session, so the server merges them rather than making the page do it.
    """
    events: list[dict[str, Any]] = []
    for run in data["runs"].values():
        verb = "review" if run.get("role") == "reviewer" else "dispatch"
        if run.get("started_at"):
            events.append(
                {
                    "at": run["started_at"],
                    "kind": "run-started",
                    "text": f"{verb} {run['task']}",
                    "task": run.get("task", ""),
                    "run": run["id"],
                    "status": "running",
                }
            )
        if run.get("finished_at"):
            reported = run.get("verdict") or {}
            outcome = run.get("resulting_status") or run["status"]
            events.append(
                {
                    "at": run["finished_at"],
                    "kind": "run-finished",
                    "text": f"{run['task']} {outcome}",
                    "task": run.get("task", ""),
                    "run": run["id"],
                    "status": outcome,
                    "summary": reported.get("summary", ""),
                    "exit_code": run.get("exit_code"),
                }
            )
    for item in data.get("decisions", []):
        events.append(
            {
                "at": item.get("at", ""),
                "kind": "decision",
                "text": f"{item['id']} {item.get('title', '')}",
                "status": item.get("status", ""),
                "task": item.get("task", ""),
                "decision": item["id"],
            }
        )
    events.sort(key=lambda event: event["at"] or "", reverse=True)
    return events[:limit]


# ---------------------------------------------------------------- everything


def everything(root: Path) -> dict[str, Any]:
    """One document with every view, for the initial load and each push.

    A single payload rather than a request per panel: the views have to agree
    with each other, and a page that fetched them separately could show a task as
    running in one panel and finished in the next. It is small — a few hundred
    tasks of metadata — and it means the push path and the load path are the same
    code, so they cannot drift.
    """
    data = state.load(root)
    return {
        "overview": overview(data),
        "milestones": milestones(data),
        "tasks": tasks(data),
        "runs": runs(data),
        "decisions": decisions(data),
        "graph": graph(data),
        "activity": activity(data),
        "generated_at": state.utcnow(),
    }
