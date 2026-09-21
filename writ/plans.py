"""The plan as a reviewed artifact: its status, its findings, its coverage.

Writ used to have one plan state: committed. `writ plan` wrote tasks and they were
immediately executable, which means every objection to the plan — that a
requirement has no task, that two parallel tasks own one file — arrived as a
surprise hours into a run, or not at all.

So the plan gets a status of its own, separate from the statuses of the tasks in
it:

    draft          written down, not yet checked
    needs-approval checked, and something blocking came back
    approved       checked clean, or a human signed off on the objections
    executing      an agent has started work under it
    complete       the final gate passed

`writ run` refuses to start below `approved`. That is the whole point of the
state: the objections are on the record and a human either fixed them or said
"go anyway" in a way that is written down, rather than the plan sliding into
execution unexamined.

Findings live here too, with ids (`F-0001`) so a repair can name the one it
answers, and so the second run of a check can tell a finding that persists from a
finding that is new. Requirements live here as a dict keyed by id, and the
coverage matrix is derived from them rather than stored twice.
"""
from __future__ import annotations

from typing import Any, Iterable

from . import plancheck
from .plancheck import Finding, Requirement
from .state import WritError, utcnow

PLAN_STATUSES = (
    "draft",
    "needs-approval",
    "approved",
    "executing",
    "complete",
)

#: statuses from which a `writ run` may dispatch work
RUNNABLE_STATUSES = ("approved", "executing")

#: what a finding's disposition can be once something has answered it
DISPOSITIONS = ("open", "accepted", "declined", "resolved")

#: the dispositions a human may set directly.
#:
#: Not `resolved`: that one is earned. A finding is resolved when a check or a gate
#: demonstrates the outcome it asked for, so letting a person assert it would make
#: the strongest word in the ledger the cheapest one to say. A human who wants the
#: plan to proceed anyway says `accepted` — same effect on the gate, honest about
#: why it moved. Not `open` either, which is where findings start.
SETTABLE_DISPOSITIONS = ("accepted", "declined")


def empty_plan_status() -> dict[str, Any]:
    """The plan record a project starts with, before anything is planned."""
    return {
        "status": "draft",
        "revision": 0,
        "checked_at": None,
        "approved_at": None,
        "approved_by": None,
        "approval_note": None,
        "forced": False,
    }


def plan_status(data: dict[str, Any]) -> dict[str, Any]:
    """The plan record, defaulted for a store written before it existed."""
    record = data.get("plan")
    if not isinstance(record, dict):
        record = empty_plan_status()
        data["plan"] = record
    for key, value in empty_plan_status().items():
        record.setdefault(key, value)
    return record


def revision(data: dict[str, Any]) -> int:
    """Which version of the graph this is.

    Bumped by anything that changes the shape of the plan: a commit, an approval,
    an applied repair patch. A patch names the revision it was planned against and
    is refused if the graph has moved on, which is the check that stops two
    repairs from racing each other onto one graph.
    """
    return int(plan_status(data).get("revision", 0))


def bump(data: dict[str, Any]) -> int:
    record = plan_status(data)
    record["revision"] = int(record.get("revision", 0)) + 1
    return record["revision"]


def set_status(data: dict[str, Any], status: str) -> dict[str, Any]:
    if status not in PLAN_STATUSES:
        raise WritError(
            f"unknown plan status {status!r}; choose from {', '.join(PLAN_STATUSES)}"
        )
    record = plan_status(data)
    record["status"] = status
    return record


def runnable(data: dict[str, Any]) -> bool:
    return plan_status(data)["status"] in RUNNABLE_STATUSES


def mark_executing(data: dict[str, Any]) -> None:
    """Move an approved plan to `executing` the first time work starts under it."""
    record = plan_status(data)
    if record["status"] == "approved":
        record["status"] = "executing"


def not_runnable_message(data: dict[str, Any]) -> str:
    """Why this plan cannot be run yet, and the one command that changes it."""
    record = plan_status(data)
    status = record["status"]
    if not data.get("tasks"):
        # No plan at all is not an unapproved plan. Saying "this has not been
        # checked" to someone who has not written one yet points at the wrong
        # command.
        return "no tasks (run `writ plan <design.md>` first)"
    if status == "complete":
        return (
            "this plan is complete; its final gate passed. Plan more work with "
            "`writ plan <design.md> --append`."
        )
    counts = plancheck.tally(findings(data, open_only=True))
    if status == "draft":
        return (
            "this plan has not been checked yet. Run `writ check` to see what "
            "Writ can prove about it, then `writ approve`."
        )
    if not counts["error"]:
        # Nothing blocking stands against this plan; it simply has not been signed
        # off. That is the ordinary state of a freshly planned project now that a
        # clean check no longer approves itself, so it gets the plain instruction
        # rather than being sent to `--force` to overrule objections that are not
        # there.
        return (
            f"this plan is {status} and nothing blocking stands against it. "
            "Read it with `writ check` and `writ coverage`, then approve it with "
            "`writ approve`. For automation, plan with `--auto-approve`."
        )
    return (
        f"this plan is {status}: {counts['error']} blocking "
        f"finding{'s' if counts['error'] != 1 else ''} stand against it. Read them "
        "with `writ check`, fix the plan and re-plan, answer them one at a time "
        "with `writ set F-NNNN accepted|declined --reason ...`, or accept them all "
        "with `writ approve --force --reason ...`."
    )


# --------------------------------------------------------------------------
# requirements


def requirements(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    record = data.get("requirements")
    if not isinstance(record, dict):
        record = {}
        data["requirements"] = record
    return record


def set_requirements(
    data: dict[str, Any], inventory: Iterable[Requirement], *, replace: bool = False
) -> list[str]:
    """Write a requirement inventory into project state.

    Appending a plan merges: the second design document's requirements join the
    first's rather than replacing them, because the project's obligations are the
    union of what every document asked for. A re-planned project (`--force`)
    replaces, since the old inventory described a graph that no longer exists.
    """
    store = requirements(data)
    if replace:
        store.clear()
    written: list[str] = []
    for requirement in inventory:
        existing = store.get(requirement.id)
        payload = requirement.to_dict()
        if existing:
            # An id that comes back with the same text is the same requirement;
            # keep the first record's timestamp and let the new one refine the
            # rest of the fields.
            payload["created_at"] = existing.get("created_at", utcnow())
        else:
            payload["created_at"] = utcnow()
        payload["updated_at"] = utcnow()
        store[requirement.id] = payload
        written.append(requirement.id)
    return written


def requirement_list(data: dict[str, Any]) -> list[Requirement]:
    return [
        Requirement.from_dict(payload)
        for payload in sorted(requirements(data).values(), key=lambda r: r.get("id", ""))
    ]


def coverage(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The requirement coverage matrix: obligation → tasks → verification state.

    Derived rather than stored. A stored matrix is a second copy of the graph that
    goes stale the moment a repair adds a task, and the thing a reader wants from
    it — "is this requirement actually done?" — is a question about current task
    status, not about what the plan said at commit time.
    """
    tasks = data.get("tasks", {})
    rows: list[dict[str, Any]] = []
    for requirement in requirement_list(data):
        covering = sorted(
            task["id"]
            for task in tasks.values()
            if requirement.id in task.get("requirement_ids", [])
            and task.get("kind", "task") == "task"
        )
        verifying = sorted(
            task["id"]
            for task in tasks.values()
            if requirement.id in task.get("requirement_ids", [])
            and task.get("kind") == "gate"
        )
        statuses = [tasks[task_id]["status"] for task_id in covering]
        rows.append(
            {
                "id": requirement.id,
                "text": requirement.text,
                "priority": requirement.priority,
                "declared": requirement.status,
                "source": requirement.source,
                "evidence": requirement.evidence,
                "reason": requirement.reason,
                "tasks": covering,
                "gates": verifying,
                "state": _coverage_state(requirement, covering, statuses),
                "complete": sum(1 for status in statuses if status == "completed"),
            }
        )
    return rows


def _coverage_state(
    requirement: Requirement, covering: list[str], statuses: list[str]
) -> str:
    """One word for where this requirement stands.

    `satisfied` is deliberately reserved for work a reviewer accepted or evidence
    a human recorded. A requirement whose tasks are all `awaiting-review` is
    `in-progress`, not satisfied, because nothing has independently checked it
    yet — which is the distinction the whole review split exists to keep.
    """
    if requirement.status == "existing":
        return "satisfied" if requirement.evidence else "unevidenced"
    if requirement.status in ("out-of-scope", "deferred"):
        return requirement.status
    if not covering:
        return "uncovered"
    if all(status == "completed" for status in statuses):
        return "satisfied"
    if any(status in ("failed", "blocked") for status in statuses):
        return "at-risk"
    if any(status != "planned" for status in statuses):
        return "in-progress"
    return "planned"


def uncovered(data: dict[str, Any]) -> list[str]:
    """Requirements with nothing standing behind them, worst first."""
    return [
        row["id"]
        for row in coverage(data)
        if row["state"] in ("uncovered", "unevidenced")
    ]


# --------------------------------------------------------------------------
# findings


def findings(
    data: dict[str, Any], *, open_only: bool = False, source: str | None = None
) -> list[Finding]:
    records = data.get("findings", [])
    out: list[Finding] = []
    for payload in records:
        if open_only and payload.get("disposition", "open") != "open":
            continue
        if source is not None and payload.get("source") != source:
            continue
        out.append(Finding.from_dict(payload))
    return out


def finding_records(data: dict[str, Any]) -> list[dict[str, Any]]:
    record = data.get("findings")
    if not isinstance(record, list):
        record = []
        data["findings"] = record
    return record


def record_findings(
    data: dict[str, Any], incoming: Iterable[Finding], *, scope: str = "plan"
) -> list[Finding]:
    """Write findings to the ledger, keeping the ones already there.

    A re-check is not a fresh start. A finding that is still true keeps its id and
    its first-seen time, so "this has been objected to since revision 2 and is
    still open" survives; one that has gone away is closed as `resolved` with the
    revision that resolved it, rather than silently vanishing. That history is
    what makes a repair loop auditable — see the repeat-finding detection in
    `repair.py`.
    """
    store = finding_records(data)
    current = revision(data)
    by_key = {_finding_key(payload): payload for payload in store}
    written: list[Finding] = []
    seen_keys: set[str] = set()
    for finding in incoming:
        key = _finding_key(finding.to_dict())
        seen_keys.add(key)
        existing = by_key.get(key)
        if existing is not None:
            existing["severity"] = finding.severity
            existing["message"] = finding.message
            existing["suggested_action"] = finding.suggested_action
            existing["seen_at"] = utcnow()
            existing["seen_count"] = int(existing.get("seen_count", 1)) + 1
            existing["revision"] = current
            if existing.get("disposition") == "resolved":
                # It came back. Reopen rather than record a second finding: the
                # useful fact is that this objection has now survived a repair.
                existing["disposition"] = "open"
                existing["reopened_at"] = utcnow()
            finding.id = existing["id"]
            written.append(finding)
            continue
        finding.id = _next_finding_id(data)
        payload = finding.to_dict()
        payload.update(
            {
                "scope": scope,
                "disposition": "open",
                "first_seen_at": utcnow(),
                "seen_at": utcnow(),
                "seen_count": 1,
                "revision": current,
            }
        )
        store.append(payload)
        by_key[key] = payload
        written.append(finding)
    for key, payload in by_key.items():
        if (
            key not in seen_keys
            and payload.get("scope") == scope
            and payload.get("disposition") == "open"
            and payload.get("source") in ("writ", None)
        ):
            # Only Writ's own findings are auto-closed. A critic's or a gate's
            # objection is not disproved by a deterministic re-check that never
            # looked for it.
            payload["disposition"] = "resolved"
            payload["resolved_at"] = utcnow()
            payload["resolved_by"] = "writ"
            payload["resolved_revision"] = current
    return written


def _finding_key(payload: dict[str, Any]) -> str:
    """Identity of a finding: same category, place, and source is the same one."""
    return "|".join(
        (
            str(payload.get("source", "writ")),
            str(payload.get("category", "")),
            str(payload.get("where", "")),
            str(payload.get("message", ""))[:120],
        )
    )


def _next_finding_id(data: dict[str, Any]) -> str:
    counters = data.setdefault("counters", {})
    counters["finding"] = int(counters.get("finding", 0)) + 1
    return f"F-{counters['finding']:04d}"


def get_finding(data: dict[str, Any], finding_id: str) -> dict[str, Any]:
    for payload in finding_records(data):
        if payload.get("id") == finding_id:
            return payload
    raise WritError(f"unknown finding: {finding_id}")


def dispose(
    data: dict[str, Any],
    finding_id: str,
    disposition: str,
    *,
    actor: str = "operator",
    reason: str = "",
    change: str = "",
) -> dict[str, Any]:
    """Record what was done about a finding. Every close needs an actor."""
    if disposition not in DISPOSITIONS:
        raise WritError(
            f"unknown disposition {disposition!r}; choose from {', '.join(DISPOSITIONS)}"
        )
    payload = get_finding(data, finding_id)
    payload["disposition"] = disposition
    payload["disposed_at"] = utcnow()
    payload["disposed_by"] = actor
    if reason:
        payload["reason"] = reason
    if change:
        payload["change"] = change
    return payload


def accept_all(data: dict[str, Any], *, actor: str, reason: str) -> list[str]:
    """A human overruling every open objection, on the record.

    This is what `writ approve --force` does. It does not delete the findings or
    weaken them: they stay readable, marked accepted, with who accepted them and
    why, so a later reader can see the plan ran with known objections and which
    ones they were.
    """
    accepted = []
    for payload in finding_records(data):
        if payload.get("disposition", "open") != "open":
            continue
        payload["disposition"] = "accepted"
        payload["disposed_at"] = utcnow()
        payload["disposed_by"] = actor
        payload["reason"] = reason
        accepted.append(payload["id"])
    return accepted


# --------------------------------------------------------------------------
# checking and approving


def run_check(
    data: dict[str, Any], *, root: Any = None, extra: Iterable[Finding] = ()
) -> list[Finding]:
    """Check the committed graph and set the plan's status from the result.

    A clean check moves a draft to `needs-approval`, not to `approved`. Writ used
    to approve it outright, which collapsed two different facts into one status:
    "nothing writ can prove is wrong with this plan" and "somebody signed this
    plan off". The first is what a check establishes, and it is a much weaker
    claim — every defect in §3 of the review (an omitted requirement, a dependency
    that is legal but incorrect, a criterion nothing can demonstrate) passes a
    clean check by construction. Approval is a judgement, so it needs an actor:
    `writ approve`, or `writ plan --auto-approve` for automation that has chosen
    to make it in advance.

    An approved plan that still checks clean stays approved — a re-check is not a
    reason to ask for approval again. One that has acquired a blocking finding
    since approval goes back to `needs-approval`, because whatever was signed off
    is no longer what is there.
    """
    snapshot = plancheck.from_state(data, root=root)
    found = plancheck.sort_findings(list(plancheck.check(snapshot)) + list(extra))
    record_findings(data, found, scope="plan")
    record = plan_status(data)
    record["checked_at"] = utcnow()
    open_blocking = [
        finding
        for finding in findings(data, open_only=True)
        if finding.severity == "error"
    ]
    if open_blocking:
        if record["status"] in ("draft", "approved", "needs-approval"):
            set_status(data, "needs-approval")
    elif record["status"] == "draft":
        set_status(data, "needs-approval")
    return found


def approve(
    data: dict[str, Any], *, actor: str = "operator", reason: str = "", force: bool = False
) -> dict[str, Any]:
    """Record human approval of the plan. Blocking findings need `force`."""
    record = plan_status(data)
    if record["status"] == "complete":
        raise WritError("this plan is already complete")
    open_blocking = [
        finding
        for finding in findings(data, open_only=True)
        if finding.severity == "error"
    ]
    if open_blocking and not force:
        listed = "\n".join(f"  {finding.line()}" for finding in open_blocking[:8])
        more = (
            f"\n  … and {len(open_blocking) - 8} more"
            if len(open_blocking) > 8
            else ""
        )
        raise WritError(
            f"{len(open_blocking)} blocking finding"
            f"{'s' if len(open_blocking) != 1 else ''} stand against this plan:\n"
            f"{listed}{more}\n"
            "Fix the plan and re-plan, or accept them on the record with "
            "`writ approve --force --reason ...`"
        )
    if force and open_blocking and not reason:
        raise WritError(
            "--force needs --reason: accepting a blocking finding is a judgement, "
            "and the record has to say whose and why"
        )
    accepted = accept_all(data, actor=actor, reason=reason) if force else []
    if record["status"] not in ("executing",):
        set_status(data, "approved")
    record["approved_at"] = utcnow()
    record["approved_by"] = actor
    record["approval_note"] = reason
    record["forced"] = bool(force and accepted)
    bump(data)
    return {"accepted": accepted, "plan": record}
