"""Structured verdicts: how an agent reports what it actually achieved.

An exit code says a process ended, not that work is done. A task has acceptance
criteria, and the only useful report is per-criterion: which bars are met, and
what evidence meets them. This module defines that report, validates it, and
applies it to project state.

Two roles produce verdicts, and they are deliberately not equal:

* the **implementing** agent reports what it did. Its verdict can pass criteria
  and move a task to `awaiting-review`, but it cannot mark the task complete —
  an agent grading its own homework is not evidence.
* a **reviewer** agent independently re-checks the criteria without having
  written the code, and its verdict is what completes or fails a task.

Both must cite evidence for anything they pass. A verdict that claims a
criterion passed without saying how is rejected, which is the same standard the
task prompt asks the agent to hold itself to.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .state import WritError, utcnow

#: name of the file an agent writes inside its run directory
VERDICT_FILENAME = "verdict.json"

#: what the implementing agent may claim about the task as a whole
OUTCOMES = ("complete", "incomplete", "blocked")

#: what a reviewer may decide
DECISIONS = ("accept", "reject")

CRITERION_STATUSES = ("passed", "failed", "pending")


SCHEMA = """\
{
  "outcome": "complete" | "incomplete" | "blocked",
  "summary": "what you changed and what it now does, a few lines",
  "criteria": [
    {
      "number": 1,
      "status": "passed" | "failed" | "pending",
      "evidence": "the exact command you ran and its result, or the file and \
behaviour that demonstrates this bar is met"
    }
  ],
  "decisions": [
    {
      "title": "short name for the choice",
      "decision": "what you decided, in one or two sentences",
      "context": "what the document left open, or what forced the choice",
      "consequences": "what this commits the project to, or rules out"
    }
  ],
  "blocked_on": "only when outcome is blocked: what stopped you",
  "notes": "assumptions, deviations, risks, anything the next agent needs"
}"""

RULES = """\
Rules for the verdict:
1. Include an entry for every acceptance criterion, by its number, in order.
2. `passed` requires evidence that someone else could re-run or re-read. Naming
   a command and its result is evidence; "implemented" and "works" are not.
3. Mark a criterion `failed` if you could not meet it and `pending` if you could
   not check it. Both are acceptable outcomes and are more useful than a guess.
4. `outcome` must be `complete` only when every criterion is `passed`.
5. Do not claim `passed` on the strength of code you wrote but did not run.
6. Report honestly. A verdict is checked by a reviewer that did not write your
   code, and a false pass is worse than an admitted failure."""

DECISION_RULES = """\
Record decisions for choices the design document did not make for you.

You will hit questions the document leaves open: a data format, an error
semantic, a boundary case, a dependency, a name that will be hard to change
later. Whichever way you answer one, the next agent inherits it and cannot tell
your deliberate choice from an accident. Put it in `decisions`.

The test is consequence, not effort. Record a choice when a later agent who
decided differently would have to change your code, or when someone reading the
code a month from now would ask "why this way?" and the document would not
answer them.

What belongs there:
- an interpretation you had to pick between, where the document allowed both
- a behaviour you defined because the document was silent on it
- a constraint you accepted, or a simpler approach you rejected and why
- anything you wrote in `notes` starting with "assumed" or "decided"

What does not:
- restating a requirement the document already fixed
- ordinary implementation detail with no consequence for later work
- routine facts about how you worked: where you put files, that you ran the
  tests, that you added no dependencies
- one entry per file you touched; these are decisions, not a changelog

Prefer two or three real ones to a long list. Every entry costs a human a
decision, so an entry that does not need ruling on is a cost with no return.
Leave `decisions` empty if the document genuinely settled everything; an empty
list is a real answer, and padding it is not."""

REVIEW_SCHEMA = """\
{
  "decision": "accept" | "reject",
  "summary": "what you verified and how",
  "criteria": [
    {
      "number": 1,
      "status": "passed" | "failed" | "pending",
      "evidence": "what you ran or read to reach this conclusion"
    }
  ],
  "decisions": [
    {
      "title": "short name for the choice",
      "decision": "what was decided, in one or two sentences",
      "context": "what the document left open",
      "consequences": "what it commits the project to"
    }
  ],
  "notes": "anything the implementer missed, or risks worth recording"
}"""

REVIEW_RULES = """\
Rules for the review:
1. Verify independently. Re-run the tests and re-read the code; do not take the
   implementer's report as evidence for itself.
2. Judge only the acceptance criteria of this task. Style you dislike and work
   belonging to another task are not grounds to reject.
3. `accept` requires that you personally confirmed every criterion passes.
4. Reject if a criterion is unmet, if the evidence does not support the claim, or
   if the tests do not actually exercise the behaviour they name.
5. Do not modify the repository. You are reading and running, not fixing.
6. Use `decisions` for consequential choices the implementer made silently, or
   made without recording. A choice you can see in the diff but not in their
   decisions is exactly what this field is for."""


# --------------------------------------------------------------------------
# parsing


@dataclass
class Criterion:
    """One acceptance criterion as judged by an agent."""

    number: int
    status: str
    evidence: str = ""


@dataclass
class ProposedDecision:
    """A choice an agent made that outlives the task it was made in.

    Proposed, not recorded: an agent may not commit the project to an
    architectural position on its own say-so. These land in the log as
    `proposed` and need confirming.
    """

    title: str
    decision: str
    context: str = ""
    consequences: str = ""


@dataclass
class Verdict:
    """An agent's report on a task, validated but not yet applied."""

    outcome: str
    summary: str = ""
    criteria: list[Criterion] = field(default_factory=list)
    decisions: list[ProposedDecision] = field(default_factory=list)
    blocked_on: str | None = None
    notes: str | None = None
    #: `agent` for the implementer, `reviewer` for an independent check
    role: str = "agent"
    #: set for reviewer verdicts
    decision: str | None = None

    @property
    def passed(self) -> list[int]:
        return [c.number for c in self.criteria if c.status == "passed"]

    @property
    def unmet(self) -> list[int]:
        return [c.number for c in self.criteria if c.status != "passed"]


FENCE = re.compile(r"```(?:json)?\s*(.+?)```", re.S)


def find(directory: Path) -> Path | None:
    """The verdict file an agent was asked to write, if it wrote it."""
    path = directory / VERDICT_FILENAME
    return path if path.exists() else None


def read(directory: Path, *, role: str = "agent") -> Verdict | None:
    """Load a verdict from a run directory, falling back to stdout chatter.

    Returns None when the agent left no parseable verdict at all, which the
    caller treats as "no claim made" rather than as a failure to report: an
    agent that crashed early never got the chance.
    """
    path = find(directory)
    if path is not None:
        return parse(path.read_text(encoding="utf-8"), role=role, where=str(path))
    log = directory / "stdout.log"
    if not log.exists():
        return None
    recovered = _from_text(log.read_text(encoding="utf-8", errors="replace"))
    if recovered is None:
        return None
    return parse(recovered, role=role, where=str(log))


def _from_text(text: str) -> str | None:
    """Recover verdict JSON from prose: fenced block first, then braces."""
    for candidate in reversed(FENCE.findall(text)):
        if _looks_like_verdict(candidate):
            return candidate
    start = text.find("{")
    while start != -1:
        candidate = _balanced(text, start)
        if candidate and _looks_like_verdict(candidate):
            return candidate
        start = text.find("{", start + 1)
    return None


def _looks_like_verdict(candidate: str) -> bool:
    try:
        parsed = json.loads(candidate)
    except (ValueError, TypeError):
        return False
    return isinstance(parsed, dict) and (
        "outcome" in parsed or "decision" in parsed or "criteria" in parsed
    )


def _balanced(text: str, start: int) -> str | None:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def parse(text: str, *, role: str = "agent", where: str = "verdict") -> Verdict:
    """Validate a verdict, naming the offending field when it is wrong."""
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise WritError(f"{where}: not valid JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise WritError(f"{where}: expected a JSON object")

    criteria = _criteria(raw.get("criteria"), where)
    proposals = _decisions(raw.get("decisions"), where)
    summary = _text(raw.get("summary"))
    notes = _text(raw.get("notes")) or None

    if role == "reviewer":
        decision = _one_of(raw.get("decision"), DECISIONS, "decision", where)
        if decision == "accept":
            unmet = [c.number for c in criteria if c.status != "passed"]
            if unmet:
                listed = ", ".join(str(n) for n in unmet)
                raise WritError(
                    f"{where}: decision is 'accept' but criteria {listed} "
                    "are not passed"
                )
        return Verdict(
            outcome="complete" if decision == "accept" else "incomplete",
            summary=summary,
            criteria=criteria,
            decisions=proposals,
            notes=notes,
            role="reviewer",
            decision=decision,
        )

    outcome = _one_of(raw.get("outcome"), OUTCOMES, "outcome", where)
    blocked_on = _text(raw.get("blocked_on")) or None
    if outcome == "complete":
        unmet = [c.number for c in criteria if c.status != "passed"]
        if unmet:
            listed = ", ".join(str(n) for n in unmet)
            raise WritError(
                f"{where}: outcome is 'complete' but criteria {listed} are not passed"
            )
    if outcome == "blocked" and not blocked_on:
        raise WritError(f"{where}: outcome is 'blocked' but blocked_on is empty")
    return Verdict(
        outcome=outcome,
        summary=summary,
        criteria=criteria,
        decisions=proposals,
        blocked_on=blocked_on,
        notes=notes,
        role="agent",
    )


#: a proposal with no more text than this is a label, not a decision
MIN_DECISION_CHARS = 12


def _decisions(value: Any, where: str) -> list[ProposedDecision]:
    """Validate proposed decisions, rejecting the empty gestures.

    An agent asked for decisions will sometimes produce a title and nothing else,
    which costs a reader more than it gives them. A proposal has to actually say
    what was decided.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise WritError(f"{where}: decisions must be a list")
    out: list[ProposedDecision] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise WritError(f"{where}: decisions[{index}] must be an object")
        title = _text(_pick(item, ("title", "name", "summary")))
        statement = _text(_pick(item, ("decision", "choice", "what", "text")))
        if not title:
            raise WritError(f"{where}: decisions[{index}] has no title")
        if not statement:
            raise WritError(
                f"{where}: decision {title!r} does not say what was decided"
            )
        if len(statement) < MIN_DECISION_CHARS:
            raise WritError(
                f"{where}: decision {title!r} is too thin to be useful "
                f"({statement!r}); say what was chosen and why"
            )
        out.append(
            ProposedDecision(
                title=title,
                decision=statement,
                context=_text(_pick(item, ("context", "why", "problem"))),
                consequences=_text(
                    _pick(item, ("consequences", "consequence", "implications"))
                ),
            )
        )
    return out


def _pick(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """First present key, so a model's near-miss field name still lands."""
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def _criteria(value: Any, where: str) -> list[Criterion]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise WritError(f"{where}: criteria must be a list")
    seen: set[int] = set()
    out: list[Criterion] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise WritError(f"{where}: criteria[{index}] must be an object")
        number = item.get("number", index)
        if isinstance(number, str) and number.strip().isdigit():
            number = int(number.strip())
        if not isinstance(number, int) or number < 1:
            raise WritError(
                f"{where}: criteria[{index}].number must be a positive integer"
            )
        if number in seen:
            raise WritError(f"{where}: criterion {number} reported twice")
        seen.add(number)
        status = _one_of(
            item.get("status"), CRITERION_STATUSES, f"criteria[{index}].status", where
        )
        evidence = _text(item.get("evidence"))
        if status == "passed" and not evidence:
            raise WritError(
                f"{where}: criterion {number} is marked passed with no evidence"
            )
        out.append(Criterion(number=number, status=status, evidence=evidence))
    return sorted(out, key=lambda c: c.number)


def _one_of(value: Any, allowed: tuple[str, ...], field_name: str, where: str) -> str:
    if not isinstance(value, str) or value.strip().lower() not in allowed:
        listed = ", ".join(allowed)
        raise WritError(f"{where}: {field_name} must be one of {listed} (got {value!r})")
    return value.strip().lower()


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "\n".join(str(item).strip() for item in value if str(item).strip())
    return str(value).strip()


# --------------------------------------------------------------------------
# applying a verdict to project state


def check_scope(verdict: Verdict, task: dict[str, Any], where: str) -> None:
    """Reject a verdict that talks about criteria this task does not have."""
    total = len(task.get("acceptances", []))
    stray = [c.number for c in verdict.criteria if c.number > total]
    if stray:
        listed = ", ".join(str(n) for n in stray)
        raise WritError(
            f"{where}: reports criteria {listed} but {task['id']} has {total}"
        )


def apply(
    data: dict[str, Any],
    task: dict[str, Any],
    verdict: Verdict,
    *,
    actor: str,
    review_required: bool = True,
) -> str:
    """Record a verdict against a task and return the status it produced.

    The implementing agent's verdict never reaches `completed` while review is
    required; it parks the task at `awaiting-review` instead. This is the whole
    point of the split, so it is enforced here rather than left to the caller.
    """
    from . import decisions as decision_log
    from .model import add_evidence, refresh_milestones

    for criterion in verdict.criteria:
        entry = task["acceptances"][criterion.number - 1]
        entry["status"] = criterion.status
        entry["evidence"] = criterion.evidence
        entry["judged_by"] = actor
        entry["judged_at"] = utcnow()

    if verdict.outcome == "blocked":
        status = "blocked"
    elif verdict.outcome == "incomplete":
        status = "failed"
    elif verdict.role == "reviewer" or not review_required:
        status = "completed"
    else:
        status = "awaiting-review"

    task["status"] = status
    task["updated_at"] = utcnow()
    task["last_verdict"] = {
        "role": verdict.role,
        "actor": actor,
        "outcome": verdict.outcome,
        "decision": verdict.decision,
        "summary": verdict.summary,
        "at": utcnow(),
    }
    add_evidence(task, _evidence_line(verdict, actor), actor=actor)
    if verdict.notes:
        add_evidence(task, f"notes: {verdict.notes}", actor=actor)
    if verdict.blocked_on:
        add_evidence(task, f"blocked on: {verdict.blocked_on}", actor=actor)
    for proposal in verdict.decisions:
        decision_log.propose(
            data,
            title=proposal.title,
            decision=proposal.decision,
            context=proposal.context,
            consequences=proposal.consequences,
            proposed_by=actor,
            tasks=[task["id"]],
        )
    refresh_milestones(data)
    return status


def _evidence_line(verdict: Verdict, actor: str) -> str:
    counts = f"{len(verdict.passed)}/{len(verdict.criteria)} criteria passed"
    if verdict.role == "reviewer":
        head = f"review {verdict.decision}ed"
    else:
        head = f"reported {verdict.outcome}"
    line = f"{head} ({counts})"
    if verdict.summary:
        line = f"{line}: {verdict.summary}"
    return line


def missing_message(task_id: str, directory: Path, role: str = "agent") -> str:
    """Explain that an agent finished without reporting, and how to recover."""
    who = "reviewer" if role == "reviewer" else "agent"
    return (
        f"{task_id}: the {who} exited without a usable {VERDICT_FILENAME}, so its "
        f"criteria were left untouched.\n"
        f"  transcript: {directory}\n"
        f"  it was asked to write: {directory / VERDICT_FILENAME}"
    )
