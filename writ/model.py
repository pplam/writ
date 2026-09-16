"""Task and milestone semantics: statuses, readiness, and rollups."""
from __future__ import annotations

from typing import Any, Iterable

from .state import WritError, utcnow

TASK_STATUSES = ("planned", "ready", "running", "blocked", "completed", "failed")
ACCEPTANCE_STATUSES = ("pending", "passed", "failed")
TERMINAL_STATUSES = ("completed",)

#: statuses a user may set directly; `ready` is derived, never stored
SETTABLE_STATUSES = ("planned", "running", "blocked", "completed", "failed")


def get_task(data: dict[str, Any], task_id: str) -> dict[str, Any]:
    task = data["tasks"].get(task_id)
    if task is None:
        raise WritError(f"unknown task: {task_id}")
    return task


def get_milestone(data: dict[str, Any], milestone_id: str) -> dict[str, Any]:
    milestone = data["milestones"].get(milestone_id)
    if milestone is None:
        raise WritError(f"unknown milestone: {milestone_id}")
    return milestone


def find(data: dict[str, Any], item_id: str) -> tuple[str, dict[str, Any]]:
    """Resolve an id of any kind: task, milestone, run, or decision.

    Ids are distinguishable by shape (`M01`, `M01-001`, `M01-001-<stamp>`,
    `D-0001`), so the caller does not have to say which kind it holds. Checked
    most-specific first: a run id starts with its task id, so tasks would
    otherwise shadow it.
    """
    if item_id in data["tasks"]:
        return "task", data["tasks"][item_id]
    if item_id in data["milestones"]:
        return "milestone", data["milestones"][item_id]
    if item_id in data.get("runs", {}):
        return "run", data["runs"][item_id]
    for record in data.get("decisions", []):
        if record["id"] == item_id:
            return "decision", record
    raise WritError(f"unknown id: {item_id} (not a task, milestone, run, or decision)")


def blocking_dependencies(data: dict[str, Any], task: dict[str, Any]) -> list[str]:
    """Dependencies that are not yet complete."""
    blockers = []
    for dep_id in task.get("depends_on", []):
        dep = data["tasks"].get(dep_id)
        if dep is None:
            raise WritError(
                f"task {task['id']} depends on unknown task {dep_id}"
            )
        if dep["status"] not in TERMINAL_STATUSES:
            blockers.append(dep_id)
    return blockers


def effective_status(data: dict[str, Any], task: dict[str, Any]) -> str:
    """Stored status, refined to `ready` when a planned task is unblocked."""
    if task["status"] == "planned" and not blocking_dependencies(data, task):
        return "ready"
    return task["status"]


def acceptance_summary(task: dict[str, Any]) -> dict[str, int]:
    counts = {status: 0 for status in ACCEPTANCE_STATUSES}
    for item in task.get("acceptances", []):
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    counts["total"] = len(task.get("acceptances", []))
    return counts


def unmet_acceptances(task: dict[str, Any]) -> list[str]:
    return [
        item["text"]
        for item in task.get("acceptances", [])
        if item["status"] != "passed"
    ]


def milestone_tasks(data: dict[str, Any], milestone_id: str) -> list[dict[str, Any]]:
    return [
        task
        for task in data["tasks"].values()
        if task.get("milestone") == milestone_id
    ]


def milestone_status(data: dict[str, Any], milestone_id: str) -> str:
    """Derived from member tasks; a milestone is never set by hand."""
    tasks = milestone_tasks(data, milestone_id)
    if not tasks:
        return "empty"
    statuses = [effective_status(data, task) for task in tasks]
    if all(status == "completed" for status in statuses):
        return "completed"
    if any(status == "failed" for status in statuses):
        return "failed"
    if any(status == "running" for status in statuses):
        return "running"
    if any(status == "blocked" for status in statuses):
        return "blocked"
    if any(status in ("ready", "completed") for status in statuses):
        return "in-progress" if any(s == "completed" for s in statuses) else "ready"
    return "planned"


def refresh_milestones(data: dict[str, Any]) -> None:
    """Recompute every milestone rollup and its task membership order."""
    for milestone_id, milestone in data["milestones"].items():
        tasks = sorted(
            milestone_tasks(data, milestone_id), key=lambda task: task["id"]
        )
        milestone["tasks"] = [task["id"] for task in tasks]
        milestone["status"] = milestone_status(data, milestone_id)


def ready_tasks(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Tasks that can be dispatched right now, in dependency-friendly order."""
    return [
        task
        for task in sorted(data["tasks"].values(), key=lambda item: item["id"])
        if effective_status(data, task) == "ready"
    ]


def set_status(
    data: dict[str, Any],
    task_id: str,
    status: str,
    *,
    evidence: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Transition a task, enforcing dependency and acceptance gates."""
    if status not in SETTABLE_STATUSES:
        raise WritError(
            f"cannot set status {status!r}; choose from {', '.join(SETTABLE_STATUSES)}"
        )
    task = get_task(data, task_id)
    if status == "running" and not force:
        blockers = blocking_dependencies(data, task)
        if blockers:
            raise WritError(
                f"{task_id} is blocked by incomplete dependencies: "
                f"{', '.join(blockers)} (use --force to override)"
            )
    if status == "completed" and not force:
        unmet = unmet_acceptances(task)
        if unmet:
            listed = "; ".join(unmet)
            raise WritError(
                f"{task_id} has unmet acceptance criteria: {listed} "
                "(mark them with `writ accept`, or use --force)"
            )
    task["status"] = status
    task["updated_at"] = utcnow()
    if evidence:
        add_evidence(task, evidence)
    refresh_milestones(data)
    return task


def add_evidence(task: dict[str, Any], text: str) -> None:
    task.setdefault("evidence", []).append({"at": utcnow(), "text": text})


def set_acceptance(
    data: dict[str, Any], task_id: str, number: int, status: str
) -> dict[str, Any]:
    if status not in ACCEPTANCE_STATUSES:
        raise WritError(
            f"acceptance status must be one of {', '.join(ACCEPTANCE_STATUSES)}"
        )
    task = get_task(data, task_id)
    acceptances = task.get("acceptances", [])
    if number < 1 or number > len(acceptances):
        raise WritError(
            f"{task_id} has {len(acceptances)} acceptance criteria; {number} is out of range"
        )
    acceptances[number - 1]["status"] = status
    task["updated_at"] = utcnow()
    return task


def add_task(
    data: dict[str, Any],
    *,
    task_id: str,
    title: str,
    milestone: str | None,
    depends_on: Iterable[str] = (),
    acceptances: Iterable[str] = (),
    allowed: Iterable[str] = (),
    forbidden: Iterable[str] = (),
    design_section: str | None = None,
    design_doc: str | None = None,
) -> dict[str, Any]:
    if task_id in data["tasks"]:
        raise WritError(f"task {task_id} already exists")
    if milestone and milestone not in data["milestones"]:
        raise WritError(f"unknown milestone: {milestone}")
    deps = list(depends_on)
    for dep in deps:
        if dep not in data["tasks"]:
            raise WritError(f"unknown dependency: {dep}")
    task = {
        "id": task_id,
        "title": title,
        "milestone": milestone,
        "status": "planned",
        "depends_on": deps,
        "acceptances": [
            {"text": text, "status": "pending"} for text in acceptances
        ],
        "allowed": list(allowed),
        "forbidden": list(forbidden),
        "design_section": design_section,
        "design_doc": design_doc,
        "evidence": [],
        "runs": [],
        "created_at": utcnow(),
        "updated_at": utcnow(),
    }
    data["tasks"][task_id] = task
    refresh_milestones(data)
    return task


def add_milestone(
    data: dict[str, Any], *, milestone_id: str, title: str, design_section: str | None = None
) -> dict[str, Any]:
    if milestone_id in data["milestones"]:
        raise WritError(f"milestone {milestone_id} already exists")
    milestone = {
        "id": milestone_id,
        "title": title,
        "status": "planned",
        "tasks": [],
        "design_section": design_section,
        "created_at": utcnow(),
    }
    data["milestones"][milestone_id] = milestone
    return milestone


def check_dag(data: dict[str, Any]) -> None:
    """Reject cycles and dangling dependencies."""
    tasks = data["tasks"]
    state: dict[str, int] = {}

    def visit(node: str, trail: list[str]) -> None:
        mark = state.get(node, 0)
        if mark == 1:
            cycle = " -> ".join(trail + [node])
            raise WritError(f"dependency cycle: {cycle}")
        if mark == 2:
            return
        state[node] = 1
        for dep in tasks[node].get("depends_on", []):
            if dep not in tasks:
                raise WritError(f"task {node} depends on unknown task {dep}")
            visit(dep, trail + [node])
        state[node] = 2

    for task_id in tasks:
        visit(task_id, [])
