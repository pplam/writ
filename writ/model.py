"""Task and milestone semantics: statuses, readiness, and rollups."""
from __future__ import annotations

from typing import Any, Iterable

from .state import WritError, utcnow

#: what a node in the graph is. See `add_task`.
TASK_KINDS = ("task", "gate")

TASK_STATUSES = (
    "planned",
    "ready",
    "running",
    "awaiting-review",
    "reviewing",
    "blocked",
    "completed",
    "failed",
)
ACCEPTANCE_STATUSES = ("pending", "passed", "failed")
TERMINAL_STATUSES = ("completed",)

#: statuses an operator may set directly.
#:
#: Deliberately small. `completed` is absent because completion is a judgement
#: about acceptance criteria, and that judgement belongs to the agent that did
#: the work and the reviewer that checked it — see writ/verdict.py. `ready` and
#: `awaiting-review` are absent because they are derived, not stored decisions.
#: An operator overriding any of these uses `writ override`, which says so.
SETTABLE_STATUSES = ("planned", "running", "blocked", "failed")

#: statuses only a verdict or an explicit override may produce
JUDGED_STATUSES = ("completed", "awaiting-review")

#: how many times a reviewer may send one task back before it is left failed.
#:
#: A rejection is a finding, not a dead end. The reviewer just produced the one
#: thing an implementing agent most needs — a named criterion, an actor who did
#: not write the code, and the evidence they went on — so the useful next move is
#: another attempt carrying that report, not a `failed` task waiting for someone
#: to notice it and retype the reviewer's words into a fresh dispatch.
#:
#: Bounded, because an agent that cannot satisfy a reviewer in a few tries is not
#: going to be argued into it by a fourth: at that point the task, the criteria,
#: or the design is what is wrong, and that is a human's call. Zero restores the
#: older behaviour of failing on the first rejection.
DEFAULT_MAX_REWORK = 2


def rework_attempts(task: dict[str, Any]) -> int:
    """How many times a reviewer has sent this task back for another attempt."""
    return int((task.get("rework") or {}).get("attempt", 0))


def open_rework(task: dict[str, Any]) -> dict[str, Any] | None:
    """The rejection this task is currently expected to answer, if any.

    A record is open until a reviewer accepts the work or the rework budget runs
    out. Kept after it closes — what a task was sent back for is part of its
    history — so the two are distinguished here rather than by deleting it.
    """
    record = task.get("rework")
    if not record or record.get("resolved_at") or record.get("exhausted"):
        return None
    return record


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


#: every kind of id `find` resolves, for the error when it resolves none of them.
FINDABLE = "task, milestone, run, decision, finding, or repair request"


def find(data: dict[str, Any], item_id: str) -> tuple[str, dict[str, Any]]:
    """Resolve an id of any kind.

    Ids are distinguishable by shape (`M01`, `M01-001`, `M01-001-<stamp>`,
    `D-0001`, `F-0001`, `RR-0001`), so the caller does not have to say which kind
    it holds. Checked most-specific first: a run id starts with its task id, so
    tasks would otherwise shadow it.

    Findings and repair requests are in here because writ sends people to them by
    id — a held gate's evidence says `writ show RR-0001`, and a blocking finding is
    reported as `F-0004`. An id writ prints as the place to look has to be an id
    writ can look up.
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
    for record in data.get("findings", []):
        if record.get("id") == item_id:
            return "finding", record
    for record in data.get("repairs", []):
        if record.get("id") == item_id:
            return "repair", record
    raise WritError(f"unknown id: {item_id} (not a {FINDABLE})")


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


#: how writ has always written a block reason into a task's evidence
BLOCKED_EVIDENCE_PREFIX = "blocked on: "


def blocked_on(task: dict[str, Any]) -> str:
    """Why a blocked task stopped, or "" when it is not blocked or did not say.

    Distinct from `blocking_dependencies`, and the distinction is the point: a task
    blocked by its own report has no unsatisfied dependency, so every dependency
    reads as met and nothing else on the record says what the obstacle was. Nothing
    clears a block on its own either — it waits for a person — so a reason that
    cannot be found is a task that sits indefinitely with no visible next step.

    Only for a task that is actually blocked: `last_verdict` keeps the previous
    report until a new one replaces it, and a stale reason on a task that has since
    moved on is worse than none.

    The evidence fallback covers tasks blocked before `last_verdict` carried the
    field, which is every one blocked by an earlier version. Reading back the line
    writ itself wrote is exact, and the alternative is a reason that exists in the
    store but appears nowhere a reader looks.
    """
    if task.get("status") != "blocked":
        return ""
    verdict = task.get("last_verdict") or {}
    if verdict.get("blocked_on"):
        return str(verdict["blocked_on"])
    for item in reversed(task.get("evidence", [])):
        text = str(item.get("text", ""))
        if text.startswith(BLOCKED_EVIDENCE_PREFIX):
            return text[len(BLOCKED_EVIDENCE_PREFIX) :]
    return ""


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
    if any(status in ("running", "reviewing") for status in statuses):
        return "running"
    if any(status == "awaiting-review" for status in statuses):
        return "awaiting-review"
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


def reviewable_tasks(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Tasks whose implementing agent has reported and that await a reviewer."""
    return [
        task
        for task in sorted(data["tasks"].values(), key=lambda item: item["id"])
        if task["status"] == "awaiting-review"
    ]


def set_status(
    data: dict[str, Any],
    task_id: str,
    status: str,
    *,
    evidence: str | None = None,
    force: bool = False,
    actor: str = "operator",
    allow_judged: bool = False,
) -> dict[str, Any]:
    """Transition a task, enforcing dependency and acceptance gates.

    `allow_judged` is how a verdict or an explicit override reaches `completed`;
    ordinary `writ set` cannot, because completion is a judgement about the
    acceptance criteria rather than a bookkeeping change.
    """
    permitted = SETTABLE_STATUSES + (JUDGED_STATUSES if allow_judged else ())
    if status not in permitted:
        if status in JUDGED_STATUSES:
            raise WritError(
                f"{status!r} is not set by hand: it is the outcome of an agent "
                "verdict. Let the agent report (`writ dispatch`), have a "
                f"reviewer check it (`writ review {task_id}`), or record an "
                f"explicit human judgement with `writ override {task_id} "
                f"{status} --reason ...`"
            )
        raise WritError(
            f"cannot set status {status!r}; choose from {', '.join(permitted)}"
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
                "(only a passing verdict clears these)"
            )
    if status == "planned" and (task.get("rework") or {}).get("exhausted"):
        # A task whose rework budget ran out is failed until a human moves it, and
        # moving it back to `planned` is that human saying "try again". Leaving the
        # spent counter in place would make the next rejection immediately final,
        # which is the opposite of what they just asked for. The record stays for
        # its history; only the budget is reset, and the evidence log still carries
        # every round that led here.
        record = task["rework"]
        # The attempt counter keeps climbing — it is the honest count of how many
        # times this task has been rejected, and the next agent should see it. The
        # budget is extended instead, so "attempt 4 of 2+2" rather than a counter
        # that lies about the history.
        task["rework"] = {
            **record,
            "allowance": int(record.get("allowance", 0)) + int(record.get("max", 0)),
            "exhausted": False,
            "reset_by": actor,
            "reset_at": utcnow(),
        }
    task["status"] = status
    task["updated_at"] = utcnow()
    if evidence:
        add_evidence(task, evidence, actor=actor)
    refresh_milestones(data)
    return task


def add_evidence(task: dict[str, Any], text: str, *, actor: str = "operator") -> None:
    """Append to the task's evidence log, recording who claimed it.

    The actor matters: "tests pass" from the agent that wrote the code and from
    an independent reviewer are different claims, and the log has to keep them
    apart to be worth anything.
    """
    task.setdefault("evidence", []).append(
        {"at": utcnow(), "actor": actor, "text": text}
    )


def set_acceptance(
    data: dict[str, Any],
    task_id: str,
    number: int,
    status: str,
    *,
    actor: str = "operator",
    evidence: str | None = None,
) -> dict[str, Any]:
    """Record a judgement on one criterion. Normally written by a verdict."""
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
    entry = acceptances[number - 1]
    entry["status"] = status
    entry["judged_by"] = actor
    entry["judged_at"] = utcnow()
    if evidence:
        entry["evidence"] = evidence
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
    requirement_ids: Iterable[str] = (),
    kind: str = "task",
    scope: str | None = None,
    notes: str = "",
    feature: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Insert a planned task. `feature` carries a feature's goal, owns,
    provides and consumes (docs/planning-redesign.md §4); a plain task has none.
    """
    if task_id in data["tasks"]:
        raise WritError(f"task {task_id} already exists")
    if milestone and milestone not in data["milestones"]:
        raise WritError(f"unknown milestone: {milestone}")
    deps = list(depends_on)
    for dep in deps:
        if dep not in data["tasks"]:
            raise WritError(f"unknown dependency: {dep}")
    if kind not in TASK_KINDS:
        raise WritError(
            f"unknown task kind {kind!r}; choose from {', '.join(TASK_KINDS)}"
        )
    task = {
        "id": task_id,
        "title": title,
        "milestone": milestone,
        "status": "planned",
        # `task` is implementation work; `gate` is a review of an integrated
        # outcome that writes no code. Both live in `tasks` because both are
        # nodes the same scheduler walks and the same dependency rules order —
        # see writ/gates.py for why that is the whole trick.
        "kind": kind,
        "scope": scope,
        "notes": notes,
        "requirement_ids": list(requirement_ids),
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
    if feature is not None:
        task["goal"] = str(feature.get("goal") or "")
        for key in ("owns", "provides", "consumes"):
            task[key] = [str(item) for item in feature.get(key) or ()]
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
