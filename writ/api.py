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
dependencies, durations from timestamps. The
alternative is reimplementing writ's rules in TypeScript and having them drift.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from . import (
    analysis,
    orchestrator,
    phases,
    plancheck,
    plans,
    render,
    repair,
    runner,
    state,
    stream,
    verdict,
)
from .model import (
    acceptance_summary,
    blocked_on,
    blocking_dependencies,
    effective_status,
    open_rework,
    rework_attempts,
)

#: Log tail sent with a run detail. Enough to see how an agent is doing without
#: shipping a megabyte of transcript into a JSON payload; the full file stays one
#: request away.
LOG_TAIL_BYTES = 60_000

#: Activity lines sent with a step's live output. The terminal shows every line
#: an agent produces; a page catching up mid-run wants the recent past, not all of
#: it, and the event log stays on disk for anyone who wants the rest.
STEP_ACTIVITY_LINES = 300

#: How much of an event log is rendered per request. This is polled every second
#: while a reader watches a step, and a long agent turn can put megabytes in that
#: file, so an uncapped read would re-parse all of it every second to show the last
#: screenful. Past the cap the oldest events are dropped rather than the newest.
STEP_EVENT_BYTES = 2_000_000

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
        "active_runs": [_run_row(run) for run in sorted(active, key=_run_key)],
        "proposed_decisions": sum(
            1 for d in data.get("decisions", []) if d.get("status") == "proposed"
        ),
        "throughput": _throughput(data),
        # The plan's own standing. A dashboard that showed only task counts would
        # read the same for a project executing an approved plan and one sitting at
        # `needs-approval` with nothing dispatched, which are opposite situations.
        "plan": plan(data),
    }


def plan(data: dict[str, Any]) -> dict[str, Any]:
    """Where the plan stands as an artifact: status, revision, what stands against it."""
    record = plans.plan_status(data)
    open_findings = plans.findings(data, open_only=True)
    rows = plans.coverage(data)
    held = orchestrator.held_gates(data)
    return {
        "status": record.get("status", "draft"),
        "revision": int(record.get("revision", 0)),
        "approved_by": record.get("approved_by") or "",
        "approved_at": record.get("approved_at") or "",
        "approval_note": record.get("approval_note") or "",
        "forced": bool(record.get("forced")),
        "runnable": plans.runnable(data),
        "blocking": sum(1 for f in open_findings if f.severity == "error"),
        "advisory": sum(1 for f in open_findings if f.severity == "warning"),
        "requirements": len(rows),
        "uncovered": [row["id"] for row in rows if row["state"] == "uncovered"],
        "open_repairs": [r["id"] for r in repair.open_requests(data)],
        "held_gates": [{"id": k, "reason": v} for k, v in sorted(held.items())],
        # What the plan was built on, when it came from the staged pipeline. The
        # baseline is the part worth a dashboard's space: a project whose suite
        # was already failing when planning started will attribute that failure to
        # whichever task trips over it first, and nothing else on this payload
        # would say so.
        "pipeline": _pipeline(record),
    }


def _pipeline(record: dict[str, Any]) -> dict[str, Any]:
    """The staged pipeline's provenance, or empty for a plan without one."""
    pipeline = record.get("pipeline")
    if not isinstance(pipeline, dict) or not pipeline:
        return {}
    baseline = pipeline.get("baseline") or {}
    return {
        "plan_id": pipeline.get("plan_id", ""),
        "directory": pipeline.get("directory", ""),
        "at": pipeline.get("at", ""),
        "stages": sorted(pipeline.get("stages", {})),
        # The pipeline as a pipeline: every stage writ knows about, in the order it
        # runs, whether or not this plan got that far. `stages` above is a set of
        # names and reads the same for a pipeline that stopped at `requirements` as
        # for one that never ran that stage — which are different situations, and
        # the second one is the one worth seeing.
        "stage_rows": _stage_rows(pipeline),
        "requirements": len(pipeline.get("requirement_ids", []) or []),
        "ambiguities": int(pipeline.get("ambiguities", 0) or 0),
        "unresolved_ambiguities": int(pipeline.get("unresolved_ambiguities", 0) or 0),
        "undemonstrable": list(pipeline.get("undemonstrable", []) or []),
        "baseline": {
            "status": baseline.get("status", "unknown"),
            "commands": list(baseline.get("commands", []) or []),
            "known_failures": list(baseline.get("known_failures", []) or []),
        },
    }


def _stage_rows(pipeline: dict[str, Any]) -> list[dict[str, Any]]:
    """Each analysis stage in pipeline order, with what it produced.

    Ordered by `analysis.STAGES` rather than by what the record happens to hold,
    so the dashboard draws the pipeline's shape and marks where it stopped. A
    stage with no entry is `pending`: never reached, as against run and failed.
    """
    stored = pipeline.get("stages")
    stored = stored if isinstance(stored, dict) else {}
    rows = []
    for stage in analysis.STAGES:
        entry = stored.get(stage.name)
        entry = entry if isinstance(entry, dict) else None
        if entry is None:
            state_name = "pending"
        elif entry.get("error"):
            state_name = "failed"
        elif entry.get("reused"):
            state_name = "reused"
        else:
            state_name = "ok"
        rows.append(
            {
                "name": stage.name,
                "summary": stage.summary,
                "state": state_name,
                "artifact": Path(str(entry.get("artifact", ""))).name if entry else "",
                "error": str(entry.get("error") or "") if entry else "",
                "exit_code": entry.get("exit_code") if entry else None,
                "at": str(entry.get("at") or "") if entry else "",
            }
        )
    return rows


def findings(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The one ledger, worst first: writ's own checks and the gates' reports."""
    records = plancheck.sort_findings(plans.findings(data))
    by_id = {r["id"]: r for r in plans.finding_records(data) if r.get("id")}
    out = []
    for finding in records:
        stored = by_id.get(finding.id, {})
        out.append(
            {
                "id": finding.id or "",
                "severity": finding.severity,
                "category": finding.category,
                "message": finding.message,
                "where": finding.where,
                "suggested_action": finding.suggested_action,
                "requirement_ids": list(finding.requirement_ids),
                "source": finding.source,
                "disposition": stored.get("disposition", "open"),
                "reason": stored.get("reason", ""),
                "change": stored.get("change", ""),
                "first_seen_at": stored.get("first_seen_at", ""),
            }
        )
    return out


def coverage(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The requirement matrix, derived from the current graph."""
    return plans.coverage(data)


def repairs(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Every request to change the plan: from a gate, or from adjudication.

    `gate` stays as it was for a gate-scoped request and is empty for a plan-scoped
    one; `scope` is the field to read when what matters is which occasion it was.
    """
    return [
        {
            "id": request.get("id", ""),
            "gate": request.get("gate", ""),
            "scope": repair.scope_of(request),
            "status": request.get("status", ""),
            "round": int(request.get("round", 1)),
            "summary": request.get("summary", ""),
            "findings": list(request.get("findings", [])),
            "applied_tasks": list(request.get("applied_tasks", [])),
            "revised_tasks": list(request.get("revised_tasks", [])),
            "refusals": len(request.get("refusals") or []),
            "opened_at": request.get("opened_at", ""),
        }
        for request in repair.requests(data)
    ]


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
        "status": _status(data, task),
        "stored_status": task["status"],
        # `task` or `gate`. A reader almost always wants one or the other, and a
        # graph that drew them identically would hide the fact that a node
        # holding up the plan writes no code.
        "kind": task.get("kind", "task"),
        "requirement_ids": task.get("requirement_ids", []),
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
        # How many times a reviewer sent this task back. A task on its third
        # attempt and one on its first are in the same status and are not the same
        # situation, and the row is where that has to show.
        "rework_attempts": rework_attempts(task),
        "awaiting_rework": open_rework(task) is not None,
        **_timing(data, task),
    }


def _timing(data: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    """How long agents have spent on a task, split so a page can keep counting.

    `agent_seconds` is the finished runs only. A live run is reported by when it
    started instead, because a figure computed here goes stale the moment it is
    sent: the page adds the time since `live_since` itself, every second, so a
    running task's clock moves between snapshots rather than jumping at each one.
    """
    found = [data["runs"][run_id] for run_id in task.get("runs", []) if run_id in data["runs"]]
    finished = 0.0
    live_since = None
    for item in found:
        if item["status"] in runner.ACTIVE_RUN_STATUSES:
            if item.get("started_at") and (live_since is None or item["started_at"] < live_since):
                live_since = item["started_at"]
            continue
        finished += _duration(item) or 0.0
    starts = [item["started_at"] for item in found if item.get("started_at")]
    ends = [item["finished_at"] for item in found if item.get("finished_at")]
    return {
        "agent_seconds": round(finished, 1),
        "live_since": live_since,
        "started_at": min(starts) if starts else None,
        "finished_at": max(ends) if ends and live_since is None else None,
    }


#: What each run role is called on a page. `agent` is the implementer, and
#: "dispatch" is the word the terminal already uses for sending one.
ROLE_LABELS = {"agent": "dispatch", "reviewer": "review", "gate": "gate", "repair": "repair"}


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
            # Why the agent stopped, for a task it could not finish. The one thing a
            # reader of a blocked task is looking for, and `blocked_by` cannot supply
            # it: a task blocked by its own report has no unsatisfied dependency, so
            # every dependency reads as met and nothing says what the obstacle was.
            #
            # Falls back to the evidence line, which is where this lived before
            # `last_verdict` kept the field, so tasks blocked by an earlier version
            # still explain themselves.
            "blocked_on": blocked_on(found),
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
            "rework": found.get("rework") or None,
            "run_list": [
                _run_row(data["runs"][run_id])
                for run_id in found.get("runs", [])
                if run_id in data["runs"]
            ],
            "created_at": found.get("created_at", ""),
            "updated_at": found.get("updated_at", ""),
            # For a gate: what it has decided, and why it is waiting if it is.
            # Empty on an ordinary task, so one detail shape serves both.
            "gate_attempts": found.get("gate_attempts", []),
            "held": found.get("held") or None,
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
        # How a run that did not finish was classified. An infrastructure failure
        # and a rejected implementation both leave a run that did not complete, and
        # a page that showed them identically would have a reader debugging code
        # when the provider was down.
        "failure": run.get("failure") or None,
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


# ---------------------------------------------------------------- phase


def phase(data: dict[str, Any]) -> dict[str, Any]:
    """The most recent planning attempt, laid out as a graph.

    Empty for a project planned by a writ that did not keep this record, which is
    the one case the page has to handle by falling back: `plan.pipeline.stage_rows`
    still describes what the pipeline produced, retroactively, and is what the Plan
    page showed before there was anything to watch.

    The newest attempt rather than all of them. A failed planning run stays on the
    record — `phases` accumulates like `plans` does — but the question the page
    answers is "what is happening, or what just happened", and that is one attempt.
    """
    record = phases.current(data)
    if not record:
        return {}
    described = phases.describe(record)
    nodes = _phase_layout(described["steps"])
    placed = {node["id"]: node for node in nodes}
    described.update(
        {
            "steps": nodes,
            "edges": _phase_edges(nodes, placed),
            "width": max((n["x"] + NODE_WIDTH for n in nodes), default=0) + MARGIN,
            "height": max((n["y"] + NODE_HEIGHT for n in nodes), default=0) + MARGIN,
            "counts": _phase_counts(nodes),
            "live": [n["id"] for n in nodes if n["status"] == "running"],
        }
    )
    return described


def _phase_layout(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Waves across, members down — the same formula the task graph uses.

    A column is a wave, and a wave is what `analysis.waves` and `critics.waves`
    already decided may run at once. So the picture is those rules drawn: two boxes
    side by side means writ really will run them together, and the same constants
    place them as place a task, because one implementation laying out both is one
    fewer thing that can disagree.
    """
    rows: dict[int, int] = {}
    nodes = []
    for entry in sorted(steps, key=lambda e: (int(e.get("wave", 0)), e.get("id", ""))):
        column = int(entry.get("wave", 0))
        row = rows.get(column, 0)
        rows[column] = row + 1
        node = dict(entry)
        node.update(
            {
                "id": str(entry.get("id", "")),
                "artifact": Path(str(entry.get("artifact", ""))).name,
                "directory": str(entry.get("directory", "")),
                "command": " ".join(str(part) for part in entry.get("command", [])),
                "duration": _phase_duration(entry),
                "x": MARGIN + column * (NODE_WIDTH + COLUMN_GAP),
                "y": MARGIN + row * (NODE_HEIGHT + ROW_GAP),
                "column": column,
                # Whether this step has a transcript worth polling. A commit or an
                # approval is writ's own work and has no agent behind it, so the
                # page must not offer an output pane that would always be empty.
                "has_output": bool(entry.get("directory")),
            }
        )
        nodes.append(node)
    return nodes


def _phase_edges(
    nodes: list[dict[str, Any]], placed: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """What each step waited for, drawn from its own declaration.

    `depends_on` is what the declaration said, and it is empty for a step that was
    appended mid-phase — nobody declared a second repair round's edges. Those fall
    back to the whole previous column, which is what actually had to finish.
    """
    by_column: dict[int, list[str]] = {}
    for node in nodes:
        by_column.setdefault(int(node["column"]), []).append(node["id"])
    out = []
    for node in nodes:
        sources = [dep for dep in node.get("depends_on", []) if dep in placed]
        if not sources:
            sources = by_column.get(int(node["column"]) - 1, [])
        for dep in sources:
            source, target = placed[dep], node
            out.append(
                {
                    "from": dep,
                    "to": node["id"],
                    "x1": source["x"] + NODE_WIDTH,
                    "y1": source["y"] + NODE_HEIGHT / 2,
                    "x2": target["x"],
                    "y2": target["y"] + NODE_HEIGHT / 2,
                    "satisfied": source["status"] in ("ok", "reused"),
                }
            )
    return out


def _phase_counts(nodes: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in nodes:
        status = str(node.get("status", ""))
        counts[status] = counts.get(status, 0) + 1
    return counts


def _phase_duration(entry: dict[str, Any]) -> float | None:
    """How long the step has been going, or took. None when it never started."""
    started = entry.get("started_at")
    if not started:
        return None
    try:
        begin = datetime.fromisoformat(str(started))
        finished = entry.get("finished_at")
        end = datetime.fromisoformat(str(finished)) if finished else _now(begin)
    except ValueError:
        return None
    return max(0.0, (end - begin).total_seconds())


def step_output(data: dict[str, Any], root: Path, step_id: str) -> dict[str, Any]:
    """What the agent behind one step is saying, right now.

    Rendered through `writ/stream.py` — the same renderer the terminal mirrors
    through — so the page shows `· tool`, `~ thinking…` and `> text` because it is
    running writ's renderer over writ's events, not a second implementation of the
    same idea in TypeScript that would drift from it.

    The path comes from the record, never from the request. `step_id` selects a
    step and the directory is whatever that step wrote down when it started, so a
    crafted id cannot name a file: an unknown one is a `KeyError` the server turns
    into a 404.
    """
    record = phases.current(data)
    if not record:
        raise KeyError(step_id)
    entry = phases.step(record, step_id)
    if entry is None:
        raise KeyError(step_id)
    directory = Path(str(entry.get("directory") or ""))
    payload = {
        "step": step_id,
        "status": _live_status(record, step_id),
        "activity": [],
        "text": {"text": "", "bytes": 0, "truncated": False},
        "directory": str(directory),
    }
    if not directory.exists():
        return payload
    payload["activity"] = _activity_lines(directory, entry)
    # The tail regardless. Events carry what the agent did; stdout carries what it
    # wrote, and a step that produced no events at all — an agent whose event
    # shape writ does not know, or one that died before its first event — has the
    # log and nothing else.
    payload["text"] = _tail(directory / "stdout.log")
    return payload


def _live_status(record: dict[str, Any], step_id: str) -> str:
    """The step's status as `describe` reports it, with a dead owner accounted for."""
    for entry in phases.describe(record)["steps"]:
        if entry.get("id") == step_id:
            return str(entry.get("status", ""))
    return ""


def _activity_lines(directory: Path, entry: dict[str, Any]) -> list[str]:
    """The step's event log as activity lines, most recent last.

    Rendered from the start of the file rather than from the tail, because the
    renderer is stateful: a tool call is announced when it opens and named again
    only if it fails, so beginning halfway through would report failures for calls
    it never saw announced. Past `STEP_EVENT_BYTES` that stops being affordable at
    one request per second, so the oldest events are dropped — which costs a few
    unattributed failures at the top of a very long turn, not the recent activity
    anyone is actually reading.
    """
    shape = str(entry.get("event_shape") or "")
    if not shape:
        return []
    renderer = stream.Renderer(shape)
    lines: list[str] = []
    try:
        for line in stream.event_lines(directory, tail_bytes=STEP_EVENT_BYTES):
            lines.extend(renderer.feed(line).activity)
    except (OSError, EOFError):
        return []
    return lines[-STEP_ACTIVITY_LINES:]


# ---------------------------------------------------------------- activity


def activity(data: dict[str, Any], *, limit: int = 60) -> list[dict[str, Any]]:
    """A merged timeline of what has happened, newest first.

    Runs, verdicts and decisions are three separate records that describe one
    sequence of events. Reading them side by side is how you reconstruct a
    session, so the server merges them rather than making the page do it.
    """
    events: list[dict[str, Any]] = []
    for run in data["runs"].values():
        verb = ROLE_LABELS.get(run.get("role", "agent"), run.get("role", "agent"))
        if run.get("started_at"):
            events.append(
                {
                    "at": run["started_at"],
                    "kind": "run-started",
                    "text": f"{verb} {run['task']}",
                    "task": run.get("task", ""),
                    "run": run["id"],
                    "role": run.get("role", "agent"),
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
                    "role": run.get("role", "agent"),
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
        "tasks": tasks(data),
        "runs": runs(data),
        "decisions": decisions(data),
        "graph": graph(data),
        # What is happening before anything is executed. Written as the plan phase
        # runs rather than after it, so this is the one part of the payload that
        # can be non-empty while nothing at all has been dispatched.
        "phase": phase(data),
        "activity": activity(data),
        "findings": findings(data),
        "coverage": coverage(data),
        "repairs": repairs(data),
        "generated_at": state.utcnow(),
    }
