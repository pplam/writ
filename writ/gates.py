"""Gates: review nodes that judge an integrated outcome, not one task's work.

Task review asks "did this implementation meet this task's criteria?" and that is
all it can ask, because the reviewer sees one task. So a graph can finish with
every task accepted and still not do what the design asked: two tasks that each
passed against incompatible assumptions, a requirement whose only task covered
half of it, four branches nothing ever ran together.

A gate is the missing node. It depends on the work it judges, it reads the
requirements rather than the task list — otherwise it would inherit the planner's
omissions — and it writes no code. It can pass, or come back with findings that
ask for repair (see `repair.py`).

**A gate is a task.** Same dict, `kind: "gate"`, same `tasks` collection, same
dependency rules, same scheduler. That is deliberate: readiness, parallelism,
resumability, run records and the review-first ordering all already work, and a
second execution engine for gates would have to re-earn every one of them. What a
gate adds is a different prompt, a different verdict shape, and the ability to ask
for the graph to change.

**Cycles are avoided by direction.** A repair task never depends on the gate that
asked for it — the gate has already failed. The gate gains a dependency on the
repair task, so it waits for the fix and re-reviews when it lands, while
downstream work keeps waiting on the gate. The graph stays acyclic while a gate is
reviewed any number of times.
"""
from __future__ import annotations

from typing import Any, Iterable

from .model import TERMINAL_STATUSES, add_task, milestone_tasks, refresh_milestones
from .state import WritError, utcnow

#: scope prefixes. A milestone gate judges one milestone's integrated work; the
#: final gate judges the whole plan against the whole requirement inventory.
MILESTONE_SCOPE = "milestone"
FINAL_SCOPE = "final"

FINAL_GATE_ID = "G-FINAL"

#: what a gate may decide
GATE_DECISIONS = ("pass", "needs-repair", "needs-decision")


def gate_id(milestone_id: str) -> str:
    return f"G-{milestone_id}"


def is_gate(task: dict[str, Any]) -> bool:
    return task.get("kind") == "gate"


def gates(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        task
        for task in sorted(data.get("tasks", {}).values(), key=lambda t: t["id"])
        if is_gate(task)
    ]


def open_gates(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [gate for gate in gates(data) if gate["status"] not in TERMINAL_STATUSES]


def final_gate(data: dict[str, Any]) -> dict[str, Any] | None:
    return data.get("tasks", {}).get(FINAL_GATE_ID)


def milestone_of(gate: dict[str, Any]) -> str | None:
    scope = gate.get("scope") or ""
    if scope.startswith(f"{MILESTONE_SCOPE}:"):
        return scope.split(":", 1)[1]
    return None


# --------------------------------------------------------------------------
# installing gates


def install(
    data: dict[str, Any], *, milestones: Iterable[str] | None = None, final: bool = True
) -> list[str]:
    """Add a gate per milestone, and one final gate over all of them.

    Idempotent per milestone: planning again with `--append` gates only the new
    milestones and leaves the existing gates alone, since re-creating a gate would
    throw away its review history.

    The final gate is rebuilt rather than skipped when it already exists: its
    whole job is to depend on every milestone gate, so a new milestone has to be
    added to it or the plan would complete without the new work being judged. A
    final gate that has already passed is left alone — reopening a satisfied
    judgement is a decision for a human or a repair, not a side effect of
    planning.
    """
    created: list[str] = []
    milestone_ids = (
        list(milestones)
        if milestones is not None
        else sorted(data.get("milestones", {}))
    )
    for milestone_id in milestone_ids:
        identifier = gate_id(milestone_id)
        if identifier in data["tasks"]:
            continue
        members = [
            task["id"]
            for task in milestone_tasks(data, milestone_id)
            if not is_gate(task)
        ]
        if not members:
            continue
        milestone = data["milestones"].get(milestone_id, {})
        requirement_ids = _requirements_of(data, members)
        add_task(
            data,
            task_id=identifier,
            title=f"{milestone_id} integrates: {milestone.get('title', milestone_id)}",
            milestone=milestone_id,
            depends_on=members,
            acceptances=milestone_criteria(data, milestone_id, requirement_ids),
            design_section=milestone.get("design_section"),
            requirement_ids=requirement_ids,
            kind="gate",
            scope=f"{MILESTONE_SCOPE}:{milestone_id}",
            notes=(
                "Judge the milestone's tasks as one integrated change, not one at a "
                "time. Write no code."
            ),
        )
        created.append(identifier)
    if final:
        identifier = _install_final(data)
        if identifier:
            created.append(identifier)
    refresh_milestones(data)
    return created


def answerable_requirements(data: dict[str, Any]) -> list[str]:
    """Requirement ids a gate can actually be held to.

    `out-of-scope` and `deferred` entries are in the inventory so that a reader can
    see the obligation was considered and consciously left out — which is the whole
    reason for writing them down. Holding a gate to them would ask it to judge work
    the plan says it is not doing, and would put every one of them in the coverage
    matrix as something a gate is checking.
    """
    return sorted(
        req_id
        for req_id, record in data.get("requirements", {}).items()
        if record.get("status") not in ("out-of-scope", "deferred")
    )


def _install_final(data: dict[str, Any]) -> str | None:
    """Create or re-point the final gate at every milestone gate."""
    milestone_gates = [
        gate["id"] for gate in gates(data) if gate["id"] != FINAL_GATE_ID
    ]
    loose = [
        task["id"]
        for task in data["tasks"].values()
        if not is_gate(task)
        and not any(
            task["id"] in data["tasks"][gate_ref]["depends_on"]
            for gate_ref in milestone_gates
        )
    ]
    depends = sorted(set(milestone_gates) | set(loose))
    if not depends:
        return None
    existing = data["tasks"].get(FINAL_GATE_ID)
    if existing is not None:
        if existing["status"] in TERMINAL_STATUSES:
            return None
        existing["depends_on"] = depends
        existing["requirement_ids"] = answerable_requirements(data)
        existing["acceptances"] = _merge_criteria(
            existing.get("acceptances", []), final_criteria(data)
        )
        existing["updated_at"] = utcnow()
        return None
    add_task(
        data,
        task_id=FINAL_GATE_ID,
        title="The plan satisfies the design",
        milestone=None,
        depends_on=depends,
        acceptances=final_criteria(data),
        requirement_ids=answerable_requirements(data),
        kind="gate",
        scope=FINAL_SCOPE,
        notes=(
            "Judge the integrated product against the requirement inventory and "
            "the design documents, not against the task list. Write no code."
        ),
    )
    return FINAL_GATE_ID


def _merge_criteria(
    existing: list[dict[str, Any]], wanted: list[str]
) -> list[dict[str, Any]]:
    """Add newly-required criteria without resetting judgements already made.

    A gate that has been reviewed once carries per-criterion evidence. Rebuilding
    its criteria list from scratch would discard that, so a criterion already
    there keeps its record and only genuinely new ones are appended as pending.
    """
    by_text = {item["text"]: item for item in existing}
    merged = list(existing)
    for text in wanted:
        if text not in by_text:
            merged.append({"text": text, "status": "pending"})
    return merged


def _requirements_of(data: dict[str, Any], task_ids: Iterable[str]) -> list[str]:
    found: set[str] = set()
    for task_id in task_ids:
        found.update(data["tasks"][task_id].get("requirement_ids", []))
    return sorted(found)


def milestone_criteria(
    data: dict[str, Any], milestone_id: str, requirement_ids: list[str]
) -> list[str]:
    """What a milestone gate has to establish.

    Deliberately short. A gate inherits the per-requirement detail from the
    inventory it is handed in its prompt, so restating every requirement as its
    own criterion would produce a fifty-bar gate that is judged by skimming.
    """
    milestone = data["milestones"].get(milestone_id, {})
    title = milestone.get("title", milestone_id)
    criteria = [
        f"The tasks in {milestone_id} work together: {title} is achieved by the "
        "integrated code, not by each task in isolation",
        "The interfaces the tasks agreed on match: no caller expects a shape its "
        "callee does not produce",
        "The project's own verification passes on the integrated tree",
    ]
    if requirement_ids:
        criteria.append(
            "Every requirement this milestone covers holds in the integrated "
            f"code: {', '.join(requirement_ids)}"
        )
    return criteria


def final_criteria(data: dict[str, Any]) -> list[str]:
    """What the final gate has to establish before a plan is complete."""
    criteria = [
        "Every `must` requirement in the inventory is satisfied by the integrated "
        "code, or is recorded as existing with evidence, or out of scope with a "
        "reason",
        "The design documents' stated behaviour holds end to end, not only per "
        "component",
        "The project's full verification passes from a clean checkout",
        "No requirement was satisfied by weakening what it asked for",
    ]
    return criteria


# --------------------------------------------------------------------------
# gate attempts


def attempts(gate: dict[str, Any]) -> list[dict[str, Any]]:
    record = gate.get("gate_attempts")
    if not isinstance(record, list):
        record = []
        gate["gate_attempts"] = record
    return record


def record_attempt(
    gate: dict[str, Any],
    *,
    decision: str,
    actor: str,
    summary: str,
    findings: list[str],
    revision: int,
) -> dict[str, Any]:
    """Remember one review of this gate, and what the code looked like then.

    A gate is satisfied only for the code it actually reviewed. Keeping the plan
    revision with each attempt is what lets a later reader — or a repair — tell
    "this gate passed" from "this gate passed two revisions ago, before the
    interface changed underneath it".
    """
    record = {
        "at": utcnow(),
        "decision": decision,
        "actor": actor,
        "summary": summary,
        "findings": list(findings),
        "revision": revision,
    }
    attempts(gate).append(record)
    return record


def rounds(gate: dict[str, Any]) -> int:
    """How many times this gate has asked for repair."""
    return sum(
        1 for record in attempts(gate) if record.get("decision") == "needs-repair"
    )


def require_gate(data: dict[str, Any], gate_id_: str) -> dict[str, Any]:
    gate = data.get("tasks", {}).get(gate_id_)
    if gate is None:
        raise WritError(f"unknown gate: {gate_id_}")
    if not is_gate(gate):
        raise WritError(f"{gate_id_} is a task, not a gate")
    return gate
