"""Feedback-driven replanning: a failed gate changes the graph instead of the run.

Writ already treats a rejected *task* as recoverable — the rejection goes back
with the next attempt. A rejected *plan* had no such path: if a milestone's work
did not compose, the gate failed and the run stopped, because nothing could add
the work the plan had missed.

This is that path, and its shape is set by one rule from the review:

> a reviewer should report findings and request repair — not directly rewrite the
> live graph.

So there are three parties, and none of them is trusted with the other's job. The
**gate** says what is wrong, which requirement is affected, and what evidence
shows it. A **repair planner** proposes a patch: tasks to add, edges to add, gates
to re-run. **Writ** validates the patch against invariants an agent cannot be
allowed to decide for itself, applies it atomically, and resumes.

The invariant that matters most: a repair may change the *strategy*, never the
*bar*. A patch that weakened an acceptance criterion, dropped a requirement, or
rewrote a task that is currently running is refused. Otherwise a repair planner
could close a failing gate by deleting the thing it was failing.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import plancheck, plans
from .model import TERMINAL_STATUSES, add_task, check_dag, refresh_milestones
from .plancheck import Finding
from .state import WritError, utcnow

#: a repair request's life
REQUEST_STATUSES = ("open", "planning", "proposed", "applied", "failed", "abandoned")

#: how many times one gate may ask for repair before it needs a human.
#:
#: Separate from the task rework budget on purpose. A task being reworked is one
#: agent failing to meet a fixed bar; a gate asking for repair again means the
#: *plan* was wrong again, and the useful limit for that is much smaller. Past it,
#: more tasks are not the answer.
DEFAULT_MAX_REPAIR_ROUNDS = 2

#: a finding that keeps coming back after this many repairs is escalated whatever
#: the gate's own budget says
REPEAT_FINDING_LIMIT = 2

#: how many patches writ will refuse for one request before giving up on it.
#:
#: A refusal is not a failed repair — it is a patch that broke one of writ's
#: invariants, and the planner is told which one, so the next attempt is a better
#: informed one rather than a repeat. Bounded for the same reason rework is: a
#: planner that cannot produce a valid patch in a couple of tries is being asked
#: the wrong question, and that is a human's call rather than a retry's.
MAX_PATCH_ATTEMPTS = 2

PATCH_SCHEMA = """\
{
  "base_revision": 7,
  "analysis": "what actually went wrong, in a few lines",
  "add_tasks": [
    {
      "id": "proposed-timeout-fix",
      "title": "Propagate the CLI timeout into execution",
      "notes": "where the value is lost and how this reconnects it",
      "requirement_ids": ["REQ-014"],
      "resolves_findings": ["F-0021"],
      "milestone": "M03",
      "depends_on": ["M02-003", "M03-002"],
      "allowed": ["writ/cli.py", "writ/runner.py", "tests/test_timeout.py"],
      "forbidden": [],
      "acceptances": [
        "a failing test in tests/test_timeout.py reproduces the lost timeout",
        "`pytest -q tests/test_timeout.py` passes with the timeout honoured"
      ]
    }
  ],
  "add_dependencies": [
    {
      "from": "M03-004",
      "to": "proposed-timeout-fix",
      "reason": "M03-004 reads the value this restores"
    }
  ],
  "dispositions": [
    {
      "finding_id": "F-0021",
      "resolution": "accepted",
      "change": "added the timeout propagation task",
      "reason": ""
    }
  ],
  "questions": [
    {
      "id": "Q-001",
      "question": "only when a finding cannot be repaired without a human ruling"
    }
  ]
}"""

PATCH_RULES = """\
Rules for the patch:
1. Repair the findings you were given. Do not re-plan the project, do not tidy
   unrelated work, and do not restate tasks that already exist.
2. Every blocking finding needs a disposition. `accepted` means you added work
   that closes it — name that work in `change`. `declined` means you believe the
   finding is wrong, and `reason` has to say why with evidence.
3. You may add tasks and add dependencies. You may not weaken an acceptance
   criterion, remove a requirement, delete a task, or change a task that is
   running or already completed. Writ refuses a patch that tries, so a patch that
   needs one of those is a `questions` entry instead.
4. A new task is a normal Writ task: one bounded session, 2 to 6 checkable
   criteria, a fence naming the files it owns. The same rules the plan was held to.
5. Do not depend on the gate that reported the finding. It is waiting for your
   work; depending on it would be a cycle. Depend on the completed tasks whose
   code you need.
6. Prefer a failing test first: a repair whose first criterion reproduces the
   defect is one a reviewer can actually check.
7. If a finding cannot be closed without a product decision — the design is
   ambiguous, or two requirements contradict — do not guess. Put it in
   `questions` and leave the finding open.

Write no code and change no file other than the patch JSON. You are planning a
repair."""


# --------------------------------------------------------------------------
# requests


@dataclass
class Patch:
    """A validated proposal to change the graph."""

    base_revision: int
    analysis: str = ""
    add_tasks: list[dict[str, Any]] = field(default_factory=list)
    add_dependencies: list[dict[str, Any]] = field(default_factory=list)
    dispositions: list[dict[str, Any]] = field(default_factory=list)
    questions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.add_tasks and not self.add_dependencies


def requests(data: dict[str, Any]) -> list[dict[str, Any]]:
    record = data.get("repairs")
    if not isinstance(record, list):
        record = []
        data["repairs"] = record
    return record


def open_requests(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Requests that still need something to happen before the run is done."""
    return [
        request
        for request in requests(data)
        if request.get("status") in ("open", "planning", "proposed")
    ]


def request_for_gate(data: dict[str, Any], gate_id: str) -> dict[str, Any] | None:
    for request in reversed(requests(data)):
        if request.get("gate") == gate_id and request.get("status") in (
            "open",
            "planning",
            "proposed",
        ):
            return request
    return None


def get_request(data: dict[str, Any], request_id: str) -> dict[str, Any]:
    for request in requests(data):
        if request.get("id") == request_id:
            return request
    raise WritError(f"unknown repair request: {request_id}")


def open_request(
    data: dict[str, Any],
    *,
    gate_id: str,
    finding_ids: Iterable[str],
    summary: str,
    actor: str,
) -> dict[str, Any]:
    """Record that a gate has asked for the plan to change."""
    counters = data.setdefault("counters", {})
    counters["repair"] = int(counters.get("repair", 0)) + 1
    request = {
        "id": f"RR-{counters['repair']:04d}",
        "gate": gate_id,
        "findings": list(finding_ids),
        "summary": summary,
        "status": "open",
        "opened_at": utcnow(),
        "opened_by": actor,
        "base_revision": plans.revision(data),
        "round": 1,
        "attempts": [],
    }
    prior = [
        item
        for item in requests(data)
        if item.get("gate") == gate_id and item.get("status") == "applied"
    ]
    request["round"] = len(prior) + 1
    requests(data).append(request)
    return request


def close_request(
    data: dict[str, Any], request_id: str, status: str, *, note: str = ""
) -> dict[str, Any]:
    if status not in REQUEST_STATUSES:
        raise WritError(f"unknown repair status: {status}")
    request = get_request(data, request_id)
    request["status"] = status
    request["closed_at"] = utcnow()
    if note:
        request["note"] = note
    return request


# --------------------------------------------------------------------------
# escalation


def exhausted(
    data: dict[str, Any], gate: dict[str, Any], *, max_rounds: int | None = None
) -> str:
    """Why this gate should stop asking for repair, or "" while it may continue.

    Two independent limits, because they catch different failures. The round count
    catches a gate that keeps failing for new reasons — the plan was wrong in more
    than one way, and at some point that is a design problem. The repeat-finding
    check catches the worse case: the same objection surviving repair after repair,
    which means the repairs are not addressing it and more of them will not either.
    """
    from . import gates

    budget = DEFAULT_MAX_REPAIR_ROUNDS if max_rounds is None else max_rounds
    rounds = gates.rounds(gate)
    if rounds > budget:
        return (
            f"{gate['id']} has asked for repair {rounds} times, past its budget of "
            f"{budget}. More tasks are unlikely to help: the milestone's design or "
            "its requirements are what need a human."
        )
    repeated = repeat_findings(data, gate["id"])
    if repeated:
        listed = ", ".join(repeated)
        return (
            f"{listed} survived {REPEAT_FINDING_LIMIT} repairs on {gate['id']}. The "
            "repairs are not addressing the finding, so the next one will not "
            "either."
        )
    return ""


def repeat_findings(data: dict[str, Any], gate_id: str) -> list[str]:
    """Findings this gate has raised again after a repair claimed to close them."""
    counts: dict[str, int] = {}
    for payload in plans.finding_records(data):
        if payload.get("scope") != f"gate:{gate_id}":
            continue
        seen = int(payload.get("seen_count", 1))
        if payload.get("reopened_at") or seen > REPEAT_FINDING_LIMIT:
            counts[payload["id"]] = seen
    return sorted(
        finding_id
        for finding_id, seen in counts.items()
        if seen > REPEAT_FINDING_LIMIT
    )


# --------------------------------------------------------------------------
# reading a patch


def refusals(request: dict[str, Any]) -> int:
    """How many patches writ has turned down for this request."""
    return len(request.get("refusals") or [])


def patches_left(
    request: dict[str, Any], *, limit: int = MAX_PATCH_ATTEMPTS
) -> bool:
    """Whether this request may be planned against again.

    Counts refusals, not attempts: an accepted patch closes the request, so every
    attempt still on the record is one writ turned down.
    """
    return refusals(request) < max(1, limit)


def load_patch(text: str) -> Patch:
    """Parse and shape-check a proposed patch. Semantics come later."""
    from .planning import extract_json

    stripped = (text or "").strip()
    if not stripped:
        raise WritError("the patch is empty")
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        recovered = extract_json(stripped)
        if recovered is None:
            raise WritError(f"the patch is not valid JSON: {exc}") from exc
        payload = json.loads(recovered)
    if not isinstance(payload, dict):
        raise WritError("the patch must be a JSON object")
    base = payload.get("base_revision", payload.get("base_plan_revision"))
    if base is None:
        raise WritError("the patch must state `base_revision`")
    try:
        base_revision = int(base)
    except (TypeError, ValueError) as exc:
        raise WritError("`base_revision` must be a number") from exc
    return Patch(
        base_revision=base_revision,
        analysis=str(payload.get("analysis", "")).strip(),
        add_tasks=_list_of_objects(payload.get("add_tasks"), "add_tasks"),
        add_dependencies=_list_of_objects(
            payload.get("add_dependencies"), "add_dependencies"
        ),
        dispositions=_list_of_objects(payload.get("dispositions"), "dispositions"),
        questions=_list_of_objects(payload.get("questions"), "questions"),
    )


def _list_of_objects(value: Any, where: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise WritError(f"`{where}` must be a list")
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise WritError(f"{where}[{index}] must be an object")
    return list(value)


# --------------------------------------------------------------------------
# validating a patch


def validate(
    data: dict[str, Any], patch: Patch, request: dict[str, Any]
) -> list[Finding]:
    """Everything Writ enforces about a patch, as findings rather than one raise.

    A list, because a patch with three problems should say so once. The caller
    refuses the patch if any finding is blocking, and the agent's next attempt
    gets all of them.
    """
    found: list[Finding] = []
    current = plans.revision(data)
    if patch.base_revision != current:
        found.append(
            Finding(
                severity="error",
                category="stale-patch",
                message=(
                    f"planned against revision {patch.base_revision}, but the graph "
                    f"is at {current}; it has changed underneath this patch"
                ),
                where=request["id"],
                suggested_action="re-read the graph and propose again",
                source="writ",
            )
        )
    if patch.empty and not patch.questions:
        found.append(
            Finding(
                severity="error",
                category="empty-patch",
                message=(
                    "proposes no tasks, no edges, and asks no question, so nothing "
                    "would change and the gate would fail again identically"
                ),
                where=request["id"],
                suggested_action=(
                    "add the work that closes the findings, or raise a question"
                ),
                source="writ",
            )
        )
    found.extend(_validate_new_tasks(data, patch, request))
    found.extend(_validate_edges(data, patch, request))
    found.extend(_validate_dispositions(data, patch, request))
    return plancheck.sort_findings(found)


def _validate_new_tasks(
    data: dict[str, Any], patch: Patch, request: dict[str, Any]
) -> list[Finding]:
    found: list[Finding] = []
    existing = data.get("tasks", {})
    proposed_ids = {str(entry.get("id", "")) for entry in patch.add_tasks}
    known_requirements = set(data.get("requirements", {}))
    for index, entry in enumerate(patch.add_tasks):
        where = f"{request['id']}.add_tasks[{index}]"
        ref = str(entry.get("id", "")).strip()
        title = str(entry.get("title", "")).strip()
        if not title:
            found.append(
                Finding(
                    severity="error",
                    category="patch-shape",
                    message="a proposed task has no title",
                    where=where,
                    suggested_action="name the work",
                    source="writ",
                )
            )
        if ref and ref in existing:
            found.append(
                Finding(
                    severity="error",
                    category="task-collision",
                    message=(
                        f"proposes to add {ref}, which already exists; a patch adds "
                        "work, it does not rewrite existing tasks"
                    ),
                    where=where,
                    suggested_action=(
                        "give the new task its own id, or raise a question if the "
                        "existing task has to change"
                    ),
                    source="writ",
                )
            )
        acceptances = entry.get("acceptances") or []
        if not isinstance(acceptances, list) or not acceptances:
            found.append(
                Finding(
                    severity="error",
                    category="patch-shape",
                    message=f"proposed task {ref or title!r} states no acceptance criteria",
                    where=where,
                    suggested_action="state the bar a reviewer will hold it to",
                    source="writ",
                )
            )
        for dep in entry.get("depends_on") or []:
            if dep in proposed_ids or dep in existing:
                continue
            found.append(
                Finding(
                    severity="error",
                    category="unknown-dependency",
                    message=f"proposed task {ref or title!r} depends on unknown {dep}",
                    where=where,
                    suggested_action="depend on a task that exists, or on one this patch adds",
                    source="writ",
                )
            )
        if request.get("gate") in (entry.get("depends_on") or []):
            found.append(
                Finding(
                    severity="error",
                    category="gate-cycle",
                    message=(
                        f"proposed task {ref or title!r} depends on {request['gate']}, "
                        "the gate waiting for it"
                    ),
                    where=where,
                    suggested_action="depend on the completed work it needs instead",
                    source="writ",
                )
            )
        for req_id in entry.get("requirement_ids") or []:
            if req_id not in known_requirements:
                found.append(
                    Finding(
                        severity="error",
                        category="unknown-requirement",
                        message=(
                            f"proposed task {ref or title!r} claims requirement "
                            f"{req_id}, which is not in the inventory"
                        ),
                        where=where,
                        suggested_action=(
                            "reference a real requirement, or none; a repair may not "
                            "invent obligations"
                        ),
                        source="writ",
                    )
                )
    return found


def _validate_edges(
    data: dict[str, Any], patch: Patch, request: dict[str, Any]
) -> list[Finding]:
    """New edges must point at real tasks and must not reorder work in flight."""
    found: list[Finding] = []
    existing = data.get("tasks", {})
    proposed_ids = {str(entry.get("id", "")) for entry in patch.add_tasks}
    for index, edge in enumerate(patch.add_dependencies):
        where = f"{request['id']}.add_dependencies[{index}]"
        source = str(edge.get("from", "")).strip()
        target = str(edge.get("to", "")).strip()
        if not source or not target:
            found.append(
                Finding(
                    severity="error",
                    category="patch-shape",
                    message="an edge is missing `from` or `to`",
                    where=where,
                    suggested_action="state both ends",
                    source="writ",
                )
            )
            continue
        for end in (source, target):
            if end not in existing and end not in proposed_ids:
                found.append(
                    Finding(
                        severity="error",
                        category="unknown-dependency",
                        message=f"edge names {end}, which is not a task",
                        where=where,
                        suggested_action="point it at a real task",
                        source="writ",
                    )
                )
        dependent = existing.get(source)
        if dependent is None:
            continue
        if dependent.get("status") in ("running", "reviewing"):
            found.append(
                Finding(
                    severity="error",
                    category="contract-in-flight",
                    message=(
                        f"would add a prerequisite to {source}, which has an agent "
                        "working on it right now"
                    ),
                    where=where,
                    suggested_action=(
                        "let it finish; a task's contract does not change underneath "
                        "the agent holding it"
                    ),
                    source="writ",
                )
            )
        elif dependent.get("status") in TERMINAL_STATUSES:
            found.append(
                Finding(
                    severity="warning",
                    category="retroactive-edge",
                    message=(
                        f"{source} is already completed, so ordering it after "
                        f"{target} changes nothing about what ran"
                    ),
                    where=where,
                    suggested_action=(
                        "if its work is now wrong, add a task that fixes it rather "
                        "than an edge that cannot"
                    ),
                    source="writ",
                )
            )
    return found


def _validate_dispositions(
    data: dict[str, Any], patch: Patch, request: dict[str, Any]
) -> list[Finding]:
    """Every blocking finding needs an answer, and a decline needs a reason."""
    found: list[Finding] = []
    stated = {
        str(entry.get("finding_id", "")): entry for entry in patch.dispositions
    }
    open_questions = {
        str(question.get("finding_id", "")) for question in patch.questions
    }
    for finding_id in request.get("findings", []):
        try:
            record = plans.get_finding(data, finding_id)
        except WritError:
            continue
        if record.get("severity") != "error":
            continue
        entry = stated.get(finding_id)
        if entry is None:
            if finding_id in open_questions:
                continue
            found.append(
                Finding(
                    severity="error",
                    category="undisposed-finding",
                    message=(
                        f"{finding_id} was reported by the gate and this patch does "
                        "not say what it does about it"
                    ),
                    where=request["id"],
                    suggested_action=(
                        "accept it and name the work that closes it, decline it with "
                        "evidence, or raise it as a question"
                    ),
                    source="writ",
                )
            )
            continue
        resolution = str(entry.get("resolution", "")).lower()
        if resolution == "declined" and not str(entry.get("reason", "")).strip():
            found.append(
                Finding(
                    severity="error",
                    category="undisposed-finding",
                    message=f"{finding_id} is declined with no reason given",
                    where=request["id"],
                    suggested_action="say why the finding is wrong, with evidence",
                    source="writ",
                )
            )
        if resolution == "accepted" and not _closes(patch, finding_id):
            found.append(
                Finding(
                    severity="error",
                    category="undisposed-finding",
                    message=(
                        f"{finding_id} is accepted but no proposed task says it "
                        "resolves it"
                    ),
                    where=request["id"],
                    suggested_action=(
                        "name the finding in the task's `resolves_findings`"
                    ),
                    source="writ",
                )
            )
    return found


def _closes(patch: Patch, finding_id: str) -> bool:
    return any(
        finding_id in (entry.get("resolves_findings") or [])
        for entry in patch.add_tasks
    )


# --------------------------------------------------------------------------
# applying a patch


def apply_patch(
    data: dict[str, Any],
    patch: Patch,
    request: dict[str, Any],
    *,
    actor: str = "repair-planner",
) -> dict[str, Any]:
    """Insert the repair into the graph, atomically, and re-arm its gate.

    The order matters. Tasks first, so an edge can name one. Then the gate gains a
    dependency on every task the patch added for it — that is what makes the gate
    wait for the repair instead of re-reviewing the same tree — and only then is
    the gate returned to the queue. `check_dag` runs before anything is considered
    applied, so a patch that would have made the graph illegal leaves no trace.

    The caller holds the state transaction, so a raise here rolls the whole patch
    back rather than leaving half a repair in the store.
    """
    from . import gates

    gate = gates.require_gate(data, request["gate"])
    milestone_id = gates.milestone_of(gate)
    added: list[str] = []
    translate: dict[str, str] = {}
    for entry in patch.add_tasks:
        task_id = _next_repair_id(data, milestone_id)
        ref = str(entry.get("id", "")).strip()
        if ref:
            translate[ref] = task_id
        added.append(task_id)
    for task_id, entry in zip(added, patch.add_tasks):
        depends = [
            translate.get(dep, dep) for dep in (entry.get("depends_on") or [])
        ]
        add_task(
            data,
            task_id=task_id,
            title=str(entry.get("title", "")).strip() or f"Repair {task_id}",
            milestone=entry.get("milestone") or milestone_id,
            depends_on=[dep for dep in depends if dep in data["tasks"]],
            acceptances=[
                str(item.get("text", item)) if isinstance(item, dict) else str(item)
                for item in (entry.get("acceptances") or [])
            ],
            allowed=[str(path) for path in (entry.get("allowed") or [])],
            forbidden=[str(path) for path in (entry.get("forbidden") or [])],
            design_section=gate.get("design_section"),
            requirement_ids=[
                str(req) for req in (entry.get("requirement_ids") or [])
            ],
            notes=str(entry.get("notes", "")).strip(),
        )
        task = data["tasks"][task_id]
        task["repair"] = {
            "request": request["id"],
            "gate": gate["id"],
            "resolves": [
                str(item) for item in (entry.get("resolves_findings") or [])
            ],
            "round": request.get("round", 1),
        }
    for edge in patch.add_dependencies:
        source = translate.get(str(edge.get("from", "")), str(edge.get("from", "")))
        target = translate.get(str(edge.get("to", "")), str(edge.get("to", "")))
        task = data["tasks"].get(source)
        if task is None or target not in data["tasks"]:
            continue
        if target not in task["depends_on"]:
            task["depends_on"].append(target)
            task["updated_at"] = utcnow()
    # The gate waits for its repair. Without this the gate is ready the moment it
    # is un-held and would re-review the identical tree.
    for task_id in added:
        if task_id not in gate["depends_on"]:
            gate["depends_on"].append(task_id)
    for entry in patch.dispositions:
        finding_id = str(entry.get("finding_id", ""))
        resolution = str(entry.get("resolution", "open")).lower()
        if resolution not in plans.DISPOSITIONS:
            resolution = "open"
        try:
            plans.dispose(
                data,
                finding_id,
                resolution if resolution != "resolved" else "accepted",
                actor=actor,
                reason=str(entry.get("reason", "")),
                change=str(entry.get("change", "")),
            )
        except WritError:
            continue
    gate["status"] = "planned"
    gate["updated_at"] = utcnow()
    gate.pop("held", None)
    request["status"] = "applied"
    request["applied_at"] = utcnow()
    request["applied_tasks"] = added
    request["analysis"] = patch.analysis
    request["questions"] = list(patch.questions)
    check_dag(data)
    refresh_milestones(data)
    plans.bump(data)
    return {"tasks": added, "gate": gate["id"], "revision": plans.revision(data)}


def _next_repair_id(data: dict[str, Any], milestone_id: str | None) -> str:
    """The next free task id, numbered into its milestone like any other task.

    A repair is ordinary work and is numbered as such. Nothing downstream should
    have to know a task arrived by patch — that is what the `repair` record on the
    task is for.
    """
    if milestone_id:
        prefix = f"{milestone_id}-"
        taken = [
            int(task_id[len(prefix) :])
            for task_id in data["tasks"]
            if task_id.startswith(prefix) and task_id[len(prefix) :].isdigit()
        ]
        return f"{prefix}{(max(taken) + 1) if taken else 1:03d}"
    taken = [
        int(task_id[len("R-") :])
        for task_id in data["tasks"]
        if task_id.startswith("R-") and task_id[len("R-") :].isdigit()
    ]
    return f"R-{(max(taken) + 1) if taken else 1:03d}"
