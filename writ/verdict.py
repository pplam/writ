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
from typing import Any, Iterator

from .state import WritError, utcnow

#: name of the file an agent writes inside its run directory
VERDICT_FILENAME = "verdict.json"

#: what the implementing agent may claim about the task as a whole
OUTCOMES = ("complete", "incomplete", "blocked")

#: what a reviewer may decide
DECISIONS = ("accept", "reject")

#: what a gate may decide. A gate judges integrated work, so "reject" is not
#: enough: the useful distinction is between work that is wrong (repairable by
#: adding tasks) and a question the design does not answer (not repairable by any
#: amount of work). See writ/gates.py.
GATE_DECISIONS = ("pass", "needs-repair", "needs-decision")

#: severities a gate may attach to a finding, worst first
FINDING_SEVERITIES = ("blocking", "advisory")

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
4. `outcome` must be `complete` only when every criterion is `passed`. Writ
   lowers a `complete` that its own criteria contradict, so heading a report
   `complete` over an unmet bar gains nothing and only obscures what you did. A
   criterion you leave out entirely counts as `pending` for the same reason.
5. If a criterion cannot be met from inside this task's allowed files — a bar
   that depends on another task's code, or on something you are forbidden to
   touch — mark it `pending`, say so in `blocked_on`, and use outcome `blocked`.
   That is the honest report for it, and it is not counted against your work.
6. Do not claim `passed` on the strength of code you wrote but did not run.
7. Report honestly. A verdict is checked by a reviewer that did not write your
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

GATE_SCHEMA = """\
{
  "decision": "pass" | "needs-repair" | "needs-decision",
  "summary": "what you verified across the integrated work, and how",
  "criteria": [
    {
      "number": 1,
      "status": "passed" | "failed" | "pending",
      "evidence": "what you ran or read to reach this conclusion"
    }
  ],
  "findings": [
    {
      "category": "missing-integration" | "missing-coverage" | "contradiction" |
                  "regression" | "weak-verification" | "ambiguity",
      "severity": "blocking" | "advisory",
      "requirement_ids": ["REQ-014"],
      "summary": "one sentence: what is wrong",
      "evidence": "the command and its output, or the two files that disagree",
      "required_outcome": "what would have to be true for this to be closed",
      "where": "component, file, or task id"
    }
  ],
  "questions": [
    {
      "question": "only for decision needs-decision: what a human has to rule on",
      "context": "what makes it undecidable from the documents"
    }
  ],
  "notes": "risks, or anything the next gate attempt should know"
}"""

GATE_RULES = """\
Rules for a gate review:
1. You are judging integrated work, not one task. Read the requirements you were
   given and check them against the code as it now stands. Do not check the task
   list against itself: if the plan missed something the design asked for, the
   tasks will all look complete and the requirement will still not hold.
2. Run the project's verification yourself. A gate that passes on the strength of
   task reports has verified nothing that was not already claimed.
3. Check the seams specifically. Two tasks that each passed can still disagree:
   a caller expecting a shape its callee does not produce, two modules with
   incompatible assumptions about the same data, a value that is parsed and then
   dropped before it reaches what needs it. That class of defect is invisible to
   task review and is most of what a gate is for.
4. `pass` requires that you personally confirmed every criterion. Report an entry
   for each one, by its number. If you could not check one, it is `pending` and
   the gate does not pass — and leaving it out of the report is read the same way,
   so silence gains nothing.
5. `needs-repair` is for work that is wrong or missing — something a further task
   could fix. State a finding for each one: what is wrong, which requirement it
   affects, the evidence, and what outcome would close it. A repair planner reads
   only your findings, so a finding with no evidence produces a guess.
6. `needs-decision` is for a question no amount of implementation answers: the
   design is ambiguous, or two requirements contradict. Do not pick a reading and
   pass.
7. Do not modify the repository. You are reading and running, not fixing. Work you
   think is needed goes in `findings`, not in the working tree.
8. Be specific about what you are *not* saying. A finding names one defect; a
   general unease about the architecture is a note, not a blocking finding."""

REVIEW_RULES = """\
Rules for the review:
1. Verify independently. Re-run the tests and re-read the code; do not take the
   implementer's report as evidence for itself.
2. Judge only the acceptance criteria of this task. Style you dislike and work
   belonging to another task are not grounds to reject.
3. `accept` requires that you personally confirmed every criterion passes, with
   an entry for each one by its number. A criterion you leave out is read as
   `pending`, which is a rejection.
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
class GateFinding:
    """One defect a gate found in integrated work.

    Richer than a task criterion on purpose. A criterion is judged against a bar
    that already exists; a gate finding has to be enough for a repair planner that
    was not there to decide what work would close it, so `required_outcome` — what
    would have to become true — is as important as what is broken.
    """

    category: str
    summary: str
    severity: str = "blocking"
    evidence: str = ""
    required_outcome: str = ""
    where: str = ""
    requirement_ids: list[str] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return self.severity == "blocking"

    def to_finding(self, *, scope: str):
        """As a plan finding, so gates and Writ's own checks share one ledger."""
        from .plancheck import Finding

        message = self.summary
        if self.evidence:
            message = f"{message} (evidence: {_shorten(self.evidence)})"
        return Finding(
            severity="error" if self.blocking else "warning",
            category=self.category,
            message=message,
            where=self.where,
            suggested_action=self.required_outcome,
            requirement_ids=list(self.requirement_ids),
            source=scope,
        )


@dataclass
class GateQuestion:
    """Something a gate could not decide from the documents it was given."""

    question: str
    context: str = ""


def _shorten(text: str, limit: int = 160) -> str:
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


@dataclass
class Verdict:
    """An agent's report on a task, validated but not yet applied."""

    outcome: str
    summary: str = ""
    criteria: list[Criterion] = field(default_factory=list)
    decisions: list[ProposedDecision] = field(default_factory=list)
    blocked_on: str | None = None
    notes: str | None = None
    #: `agent` for the implementer, `reviewer` for an independent check,
    #: `gate` for a review of integrated work
    role: str = "agent"
    #: set for reviewer and gate verdicts
    decision: str | None = None
    #: set when the headline claim contradicted the criteria and writ lowered it
    downgraded: str | None = None
    #: gate verdicts only: what a repair would have to address
    findings: list[GateFinding] = field(default_factory=list)
    #: gate verdicts only: what a human would have to rule on
    questions: list[GateQuestion] = field(default_factory=list)

    @property
    def passed(self) -> list[int]:
        return [c.number for c in self.criteria if c.status == "passed"]

    @property
    def unmet(self) -> list[int]:
        return [c.number for c in self.criteria if c.status != "passed"]

    @property
    def blocking_findings(self) -> list[GateFinding]:
        return [finding for finding in self.findings if finding.blocking]


FENCE = re.compile(r"```(?:json)?\s*(.+?)```", re.S)


def find(directory: Path) -> Path | None:
    """The verdict file an agent was asked to write, if it wrote it."""
    path = directory / VERDICT_FILENAME
    return path if path.exists() else None


#: how deep under the project root to look for a misplaced verdict
SEARCH_DEPTH = 3

#: directories never worth walking for one small JSON file
SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "target",
        "dist",
        "build",
        ".tox",
    }
)

#: give up rather than stat a huge tree; a verdict is written near the top
SEARCH_LIMIT = 2000


def misplaced(
    root: Path, run_directory: Path, *, since: float, role: str = "agent"
) -> Path | None:
    """A verdict this run wrote somewhere other than where it was asked to.

    Agents invent their own conventions. One told to write
    `.writ/runs/<id>/verdict.json` wrote `.reviews/<task>-verdict.json` instead,
    announced it in prose, and writ read that as no verdict at all — discarding a
    complete review that had already done the work of re-running the tests.

    The file has to be attributable to this run, so a candidate must be named
    like a verdict, have been modified since the run started, and parse as one
    for this role. That is the same trust already extended to the agent, which
    writes its own verdict and could have written it to the right path; it is not
    a new one. Nothing here is silent — the caller reports where it was found,
    because an agent that keeps missing the path is a bug to fix, not to absorb.
    """
    seen = 0
    for candidate in _candidates(root, run_directory):
        seen += 1
        if seen > SEARCH_LIMIT:
            return None
        try:
            if candidate.stat().st_mtime < since:
                continue
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not _looks_like_verdict(text):
            continue
        try:
            parse(text, role=role, where=str(candidate))
        except WritError:
            # A file that names itself a verdict and does not validate as one is
            # not a rescue. Reporting it as the reason would send the reader to a
            # file the agent may not even have meant as its report.
            continue
        return candidate
    return None


def _candidates(root: Path, run_directory: Path) -> Iterator[Path]:
    """Verdict-shaped filenames under `root`, nearest first, bounded in depth."""
    root = root.resolve()
    try:
        skip_runs = run_directory.resolve().parent
    except OSError:  # pragma: no cover - resolve on a live directory
        skip_runs = None
    queue: list[tuple[Path, int]] = [(root, 0)]
    while queue:
        directory, depth = queue.pop(0)
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if depth >= SEARCH_DEPTH or entry.name in SKIP_DIRS:
                    continue
                # Every other run's directory holds a real verdict for a
                # different run, which is the one thing that must never be
                # mistaken for this one's.
                if skip_runs is not None and entry.resolve() == skip_runs:
                    continue
                queue.append((entry, depth + 1))
            elif entry.suffix == ".json" and "verdict" in entry.name.lower():
                yield entry


def read(
    directory: Path,
    *,
    role: str = "agent",
    root: Path | None = None,
    since: float | None = None,
) -> tuple[Verdict | None, Path | None]:
    """Load a verdict, and say where it came from if not the expected path.

    Order is exactness first: the file the agent was told to write, then JSON it
    printed to stdout, then a verdict-shaped file it wrote elsewhere in the
    project. The last is only searched when `root` and `since` are given, so
    parsing a run directory in isolation stays a pure function of that directory.

    Returns `(verdict, found_at)`, where `found_at` is set only when the verdict
    turned up somewhere other than where it was asked for. Returns `(None, None)`
    when the agent left no parseable verdict at all, which the caller treats as
    "no claim made" rather than as a failure to report: an agent that crashed
    early never got the chance.
    """
    path = find(directory)
    if path is not None:
        return parse(path.read_text(encoding="utf-8"), role=role, where=str(path)), None
    log = directory / "stdout.log"
    if log.exists():
        recovered = _from_text(log.read_text(encoding="utf-8", errors="replace"))
        if recovered is not None:
            return parse(recovered, role=role, where=str(log)), None
    if root is None or since is None:
        return None, None
    elsewhere = misplaced(root, directory, since=since, role=role)
    if elsewhere is None:
        return None, None
    return (
        parse(
            elsewhere.read_text(encoding="utf-8", errors="replace"),
            role=role,
            where=str(elsewhere),
        ),
        elsewhere,
    )


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

    if role == "gate":
        return _gate_verdict(raw, criteria, summary, notes, proposals, where)

    if role == "reviewer":
        decision = _one_of(raw.get("decision"), DECISIONS, "decision", where)
        downgraded = None
        if decision == "accept":
            unmet = [c.number for c in criteria if c.status != "passed"]
            if unmet:
                decision = "reject"
                downgraded = _mismatch(where, "decision", "accept", "reject", unmet)
        return Verdict(
            outcome="complete" if decision == "accept" else "incomplete",
            summary=summary,
            criteria=criteria,
            decisions=proposals,
            notes=notes,
            role="reviewer",
            decision=decision,
            downgraded=downgraded,
        )

    outcome = _one_of(raw.get("outcome"), OUTCOMES, "outcome", where)
    blocked_on = _text(raw.get("blocked_on")) or None
    downgraded = None
    if outcome == "complete":
        unmet = [c.number for c in criteria if c.status != "passed"]
        if unmet:
            outcome = "incomplete"
            downgraded = _mismatch(where, "outcome", "complete", "incomplete", unmet)
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
        downgraded=downgraded,
    )


def _gate_verdict(
    raw: dict[str, Any],
    criteria: list[Criterion],
    summary: str,
    notes: str | None,
    proposals: list[ProposedDecision],
    where: str,
) -> Verdict:
    """Validate a gate's report.

    Two rules are enforced here rather than trusted. A `pass` whose own criteria
    are not all passed is lowered to `needs-repair`, the same way an implementer's
    `complete` is lowered — a gate is the last thing standing between a plan and
    "done", so it is the worst place to accept a headline that contradicts its own
    detail. And `needs-repair` with no findings is refused outright: a repair
    planner reads nothing but findings, so a bare request for repair would produce
    an agent inventing work from a summary line.
    """
    decision = _one_of(raw.get("decision"), GATE_DECISIONS, "decision", where)
    findings = _gate_findings(raw.get("findings"), where)
    questions = _gate_questions(raw.get("questions"), where)
    downgraded = None
    unmet = [c.number for c in criteria if c.status != "passed"]
    if decision == "pass" and unmet:
        decision = "needs-repair"
        downgraded = _mismatch(where, "decision", "pass", "needs-repair", unmet)
        if not findings:
            # It claimed a pass, so it wrote no findings; the unmet criteria are
            # the finding. Synthesised rather than rejected, because the criteria
            # it did report are real evidence and refusing the verdict would
            # discard them.
            findings = [
                GateFinding(
                    category="unmet-gate-criterion",
                    summary=(
                        f"gate criterion {criterion.number} is not met: "
                        f"{criterion.status}"
                    ),
                    evidence=criterion.evidence,
                    required_outcome="the criterion passes on the integrated code",
                )
                for criterion in criteria
                if criterion.status != "passed"
            ]
    if decision == "needs-repair" and not findings:
        raise WritError(
            f"{where}: decision is 'needs-repair' but no findings were reported. "
            "A repair planner is given nothing but these findings, so it cannot "
            "act on a request that does not say what is wrong."
        )
    if decision == "needs-decision" and not questions:
        raise WritError(
            f"{where}: decision is 'needs-decision' but no questions were asked. "
            "Say what a human has to rule on."
        )
    return Verdict(
        outcome="complete" if decision == "pass" else "incomplete",
        summary=summary,
        criteria=criteria,
        decisions=proposals,
        notes=notes,
        role="gate",
        decision=decision,
        downgraded=downgraded,
        findings=findings,
        questions=questions,
    )


GATE_FINDING_ALIASES = {
    "category": ("category", "kind", "type"),
    "summary": ("summary", "message", "finding", "description", "title"),
    "severity": ("severity", "level"),
    "evidence": ("evidence", "proof", "detail"),
    "required_outcome": ("required_outcome", "outcome", "expected", "resolution"),
    "where": ("where", "component", "location", "task", "affected_components"),
}


def _gate_findings(value: Any, where: str) -> list[GateFinding]:
    if value is None:
        return []
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        raise WritError(f"{where}.findings must be a list")
    findings: list[GateFinding] = []
    for index, raw in enumerate(value):
        at = f"{where}.findings[{index}]"
        if isinstance(raw, str):
            raw = {"summary": raw}
        if not isinstance(raw, dict):
            raise WritError(f"{at} must be an object")
        summary = _text(_pick(raw, GATE_FINDING_ALIASES["summary"]))
        if not summary:
            raise WritError(f"{at}.summary is required; say what is wrong")
        severity = (
            _text(_pick(raw, GATE_FINDING_ALIASES["severity"])) or "blocking"
        ).lower()
        if severity not in FINDING_SEVERITIES:
            severity = "blocking" if severity in ("error", "critical") else "advisory"
        location = _pick(raw, GATE_FINDING_ALIASES["where"])
        if isinstance(location, list):
            location = ", ".join(str(item) for item in location)
        findings.append(
            GateFinding(
                category=(
                    _text(_pick(raw, GATE_FINDING_ALIASES["category"])) or "gate-finding"
                ),
                summary=summary,
                severity=severity,
                evidence=_text(_pick(raw, GATE_FINDING_ALIASES["evidence"])),
                required_outcome=_text(
                    _pick(raw, GATE_FINDING_ALIASES["required_outcome"])
                ),
                where=_text(location),
                requirement_ids=[
                    str(item)
                    for item in (
                        raw.get("requirement_ids")
                        or raw.get("requirements")
                        or []
                    )
                ],
            )
        )
    return findings


def _gate_questions(value: Any, where: str) -> list[GateQuestion]:
    if value is None:
        return []
    if isinstance(value, (dict, str)):
        value = [value]
    if not isinstance(value, list):
        raise WritError(f"{where}.questions must be a list")
    questions: list[GateQuestion] = []
    for index, raw in enumerate(value):
        at = f"{where}.questions[{index}]"
        if isinstance(raw, str):
            raw = {"question": raw}
        if not isinstance(raw, dict):
            raise WritError(f"{at} must be an object")
        text = _text(_pick(raw, ("question", "text", "summary", "ask")))
        if not text:
            raise WritError(f"{at}.question is required")
        questions.append(
            GateQuestion(question=text, context=_text(raw.get("context")))
        )
    return questions


def _mismatch(
    where: str, field_name: str, claimed: str, applied: str, unmet: list[int]
) -> str:
    """Describe a headline claim that its own criteria contradict.

    Writ used to reject the whole verdict here. That is the safe direction on the
    claim and the wrong one on everything else: the per-criterion reports are the
    substance, they each carry their own evidence, and throwing them away leaves a
    task that was largely done looking untouched — with the reason living only in
    a run record, where the next agent will not see it.

    So the contradiction is resolved in the criteria's favour, which can only ever
    lower the claim. `complete` with an unmet criterion means `incomplete`, and an
    `accept` with one means `reject`. Nothing is credited that the agent did not
    itself mark passed, and the mismatch is reported rather than quietly fixed.
    """
    listed = ", ".join(str(n) for n in unmet)
    return (
        f"{where}: {field_name} was {claimed!r} but criteria {listed} are not "
        f"passed, so writ recorded {applied!r}"
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


#: the headline each role claims when it is saying the work is done, and what
#: that claim becomes when the report does not cover every criterion
_PASSING = {
    "agent": ("complete", "incomplete"),
    "reviewer": ("accept", "reject"),
    "gate": ("pass", "needs-repair"),
}


def check_coverage(verdict: Verdict, task: dict[str, Any], where: str) -> None:
    """Lower a passing headline that left criteria unreported.

    `parse` can only weigh the headline against the criteria the agent chose to
    report, so silence passed. A verdict claiming `complete` while saying nothing
    about criterion 3 was treated exactly like one that passed criterion 3, and the
    empty case was the worst of it: a gate reporting no criteria at all had no
    unmet criteria, so its `pass` always stood — the last check before "done"
    confirming nothing. Every prompt already says a bar you could not check is
    `pending`; this makes leaving it out mean the same thing, which is what the
    agent would have had to write to reach this outcome honestly.

    Needs the task, which is why it lives here and not in `parse`: which criteria a
    verdict owed a report on is a fact about the task, not about the report.
    """
    total = len(task.get("acceptances", []))
    if not total:
        return
    reported = {criterion.number for criterion in verdict.criteria}
    missing = [number for number in range(1, total + 1) if number not in reported]
    if not missing:
        return
    claimed, lowered = _PASSING[verdict.role]
    if (verdict.decision or verdict.outcome) != claimed:
        # Not a passing claim, so there is nothing to lower. Either the agent
        # said so itself or `parse` already lowered it over a criterion it did
        # report, and that first reason is the more specific one to keep.
        return
    verdict.downgraded = _unreported(where, verdict.role, claimed, lowered, missing)
    verdict.outcome = "incomplete"
    if verdict.role == "agent":
        verdict.outcome = lowered
    else:
        verdict.decision = lowered
    if verdict.role == "gate":
        # A gate lowered to `needs-repair` has to carry findings or it is unusable:
        # a repair planner reads nothing else. The unreported criteria are the
        # finding, the same synthesis `_gate_verdict` does for unmet ones.
        verdict.findings = list(verdict.findings) + [
            GateFinding(
                category="unchecked-gate-criterion",
                summary=(
                    f"gate criterion {number} was not reported on, so nothing "
                    "confirmed it"
                ),
                evidence="the gate's own report is silent on this criterion",
                required_outcome=(
                    "the criterion is checked and passes on the integrated code"
                ),
                where=str(task.get("id", "")),
            )
            for number in missing
        ]


def _unreported(
    where: str, role: str, claimed: str, applied: str, missing: list[int]
) -> str:
    """Describe a headline claim that its own report does not cover."""
    listed = ", ".join(str(n) for n in missing)
    field_name = "decision" if role in ("reviewer", "gate") else "outcome"
    one = len(missing) == 1
    return (
        f"{where}: {field_name} was {claimed!r} but "
        f"{'criterion' if one else 'criteria'} {listed} "
        f"{'was' if one else 'were'} not reported on, so nothing confirmed "
        f"{'it' if one else 'them'}; writ recorded {applied!r}"
    )


def apply(
    data: dict[str, Any],
    task: dict[str, Any],
    verdict: Verdict,
    *,
    actor: str,
    review_required: bool = True,
    max_rework: int | None = None,
) -> str:
    """Record a verdict against a task and return the status it produced.

    The implementing agent's verdict never reaches `completed` while review is
    required; it parks the task at `awaiting-review` instead. This is the whole
    point of the split, so it is enforced here rather than left to the caller.

    A reviewer's rejection returns the task to the queue while its rework budget
    holds, carrying the rejection with it — see `_send_back`. Past that budget,
    and for an implementer that reports its own work incomplete, the task fails.
    """
    from . import decisions as decision_log
    from .model import DEFAULT_MAX_REWORK, add_evidence, refresh_milestones

    if max_rework is None:
        max_rework = DEFAULT_MAX_REWORK

    # Taken before the reviewer's judgement overwrites them: what the implementer
    # claimed, per criterion, is half of what the next attempt needs to see. The
    # other half is what the reviewer made of it, and after this loop the task
    # only holds the second.
    claimed = [dict(item) for item in task.get("acceptances", [])]
    claimed_verdict = dict(task.get("last_verdict") or {})

    for criterion in verdict.criteria:
        entry = task["acceptances"][criterion.number - 1]
        entry["status"] = criterion.status
        entry["evidence"] = criterion.evidence
        entry["judged_by"] = actor
        entry["judged_at"] = utcnow()

    if verdict.role == "gate":
        status = _apply_gate(data, task, verdict, actor=actor)
        task["status"] = status
        task["updated_at"] = utcnow()
        task["last_verdict"] = {
            "role": "gate",
            "actor": actor,
            "outcome": verdict.outcome,
            "decision": verdict.decision,
            "summary": verdict.summary,
            "blocked_on": verdict.blocked_on,
            "at": utcnow(),
        }
        add_evidence(task, _evidence_line(verdict, actor), actor=actor)
        if verdict.downgraded:
            add_evidence(task, verdict.downgraded, actor="writ")
        if verdict.notes:
            add_evidence(task, f"notes: {verdict.notes}", actor=actor)
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

    rejected = verdict.role == "reviewer" and verdict.decision == "reject"
    if verdict.outcome == "blocked":
        status = "blocked"
    elif rejected:
        status = _send_back(
            task,
            verdict,
            actor=actor,
            max_rework=max_rework,
            claimed=claimed,
            claimed_verdict=claimed_verdict,
        )
    elif verdict.outcome == "incomplete":
        status = "failed"
    elif verdict.role == "reviewer" or not review_required:
        status = "completed"
        # The work was accepted, so whatever it was last sent back for has been
        # answered. Closed rather than deleted: the record is how a reader later
        # sees that this task took three attempts and what the first two missed.
        if task.get("rework"):
            task["rework"]["resolved_at"] = utcnow()
            task["rework"]["resolved_by"] = actor
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
        # Kept, because a blocked task's whole meaning is this sentence. Dropping it
        # left the reason recoverable only as one evidence line among several, and
        # a `blocked` status with satisfied dependencies and no stated cause reads
        # as writ having lost track of why it stopped.
        "blocked_on": verdict.blocked_on,
        "at": utcnow(),
    }
    add_evidence(task, _evidence_line(verdict, actor), actor=actor)
    if verdict.downgraded:
        # On the task, not only the run: the next agent to pick this up reads the
        # task's evidence, and "you claimed done, your own criteria said
        # otherwise" is the single most useful thing it can be told.
        add_evidence(task, verdict.downgraded, actor="writ")
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


def _apply_gate(
    data: dict[str, Any], gate: dict[str, Any], verdict: Verdict, *, actor: str
) -> str:
    """Record a gate's judgement and decide what happens to the graph.

    A pass completes the gate, which is what releases the work waiting behind it.
    Anything else holds it: the gate does not fail, because a failed gate would
    poison everything downstream permanently, and what has actually happened is
    that the *plan* needs to change. So the findings are written to the plan's
    ledger, a repair request is opened against them, and the gate sits at `blocked`
    with its reason recorded until a repair lands and re-arms it.

    The one case that does fail is an exhausted budget. A gate that has asked for
    repair too many times, or whose findings keep coming back, has stopped being
    evidence that another task would help — see `repair.exhausted`.
    """
    from . import gates, plans, repair
    from .model import add_evidence

    revision = plans.revision(data)
    scope = f"gate:{gate['id']}"
    recorded: list[str] = []
    if verdict.findings:
        written = plans.record_findings(
            data,
            [finding.to_finding(scope=scope) for finding in verdict.findings],
            scope=scope,
        )
        recorded = [finding.id for finding in written]
    gates.record_attempt(
        gate,
        decision=verdict.decision or "needs-repair",
        actor=actor,
        summary=verdict.summary,
        findings=recorded,
        revision=revision,
    )
    if verdict.decision == "pass":
        for finding_id in _gate_finding_ids(data, scope):
            plans.dispose(
                data,
                finding_id,
                "resolved",
                actor=actor,
                change="the gate passed on the integrated code",
            )
        if gate.get("scope") == gates.FINAL_SCOPE:
            plans.set_status(data, "complete")
        return "completed"

    if verdict.decision == "needs-decision":
        for question in verdict.questions:
            decision_log_propose_question(data, gate, question, actor=actor)
        add_evidence(
            gate,
            "held for a human ruling: "
            + "; ".join(question.question for question in verdict.questions),
            actor="writ",
        )
        gate["held"] = {
            "reason": "needs-decision",
            "at": utcnow(),
            "questions": [question.question for question in verdict.questions],
        }
        return "blocked"

    blocking = [
        finding.id
        for finding in plans.findings(data, open_only=True, source=scope)
        if finding.severity == "error"
    ]
    stop = repair.exhausted(data, gate)
    if stop:
        add_evidence(gate, stop, actor="writ")
        gate["held"] = {"reason": "repair-exhausted", "at": utcnow(), "detail": stop}
        return "failed"
    if not blocking:
        # Advisory findings only. Nothing is broken enough to hold the graph for,
        # so the gate passes and the findings stay open for a reader.
        add_evidence(
            gate,
            "advisory findings only, so the gate passed; they remain open on the "
            "plan",
            actor="writ",
        )
        return "completed"
    request = repair.open_request(
        data,
        gate_id=gate["id"],
        finding_ids=blocking,
        summary=verdict.summary,
        actor=actor,
    )
    add_evidence(
        gate,
        f"asked for plan repair ({request['id']}, round {request['round']}) over "
        f"{len(blocking)} blocking finding"
        f"{'s' if len(blocking) != 1 else ''}: {', '.join(blocking)}",
        actor="writ",
    )
    gate["held"] = {
        "reason": "awaiting-repair",
        "at": utcnow(),
        "request": request["id"],
    }
    return "blocked"


#: dispositions a passing gate is entitled to close.
#:
#: `accepted` is in here because of what it means on a gate finding: a repair
#: planner claiming it added work that closes the finding. That is a claim about
#: the future, not a demonstrated outcome — the report's rule is that a finding
#: closes when verification shows the required outcome, not when its repair task
#: reports completion. The gate passing on the repaired code *is* that
#: verification, so it is what promotes `accepted` to `resolved`.
#:
#: `declined` is not in here. A declined finding already has its resolution on the
#: record — someone argued the finding was wrong, with a reason — and overwriting
#: that with `resolved` would lose the disagreement.
_UNVERIFIED = ("open", "accepted")


def _gate_finding_ids(data: dict[str, Any], scope: str) -> list[str]:
    from . import plans

    return [
        record["id"]
        for record in plans.finding_records(data)
        if record.get("source") == scope
        and record.get("disposition", "open") in _UNVERIFIED
    ]


def decision_log_propose_question(
    data: dict[str, Any], gate: dict[str, Any], question: "GateQuestion", *, actor: str
) -> None:
    """A gate's unanswerable question, in the log a human already reads.

    Not a new kind of record: the decision log exists for exactly this — a choice
    the documents did not make, proposed by an agent, inert until a person rules on
    it. A gate asking "is the timeout per request or per operation?" is the same
    shape, and putting it anywhere else would create a second queue of things
    waiting on a human.
    """
    from . import decisions as decision_log

    decision_log.propose(
        data,
        title=_shorten(question.question, 72),
        decision=(
            "Undecided: the gate could not rule on this from the documents and "
            "stopped rather than guess."
        ),
        context=question.context or question.question,
        consequences=(
            f"{gate['id']} cannot pass until this is settled; work behind it is held."
        ),
        proposed_by=actor,
        tasks=[gate["id"]],
    )


def _send_back(
    task: dict[str, Any],
    verdict: Verdict,
    *,
    actor: str,
    max_rework: int,
    claimed: list[dict[str, Any]],
    claimed_verdict: dict[str, Any],
) -> str:
    """Record a rejection and decide whether the task gets another attempt.

    Returns `planned` — back into the ready set, since the criteria it must meet
    have not changed — or `failed` once the budget is spent. Either way the
    rejection is written to `task["rework"]`, because the reason is the part worth
    keeping: a task that failed after three attempts and one that failed on the
    first look the same without it.

    The record holds both sides of the disagreement. The reviewer's judgement is
    already on the criteria, but the claim it contradicted is not — it was just
    overwritten — and "you said you ran the tests, the reviewer ran them and got
    two failures" is a far more useful thing to hand the next attempt than either
    half alone.
    """
    from .model import add_evidence

    prior = task.get("rework") or {}
    attempt = int(prior.get("attempt", 0)) + 1
    # An operator who moves an exhausted task back to `planned` is asking for more
    # attempts, and `set_status` records that as an allowance on top of the flag's
    # budget rather than by resetting the count.
    allowance = int(prior.get("allowance", 0))
    budget = max_rework + allowance
    exhausted = attempt > budget
    record = {
        "attempt": attempt,
        "max": max_rework,
        "allowance": allowance,
        "budget": budget,
        "at": utcnow(),
        "reviewer": actor,
        "summary": verdict.summary,
        "notes": verdict.notes or "",
        "unmet": list(verdict.unmet),
        # The reviewer's own words, per criterion. `task["acceptances"]` carries
        # the same evidence now, but the next reviewer's verdict will overwrite
        # it, and by then this is what says what the last one objected to.
        "findings": [
            {
                "number": criterion.number,
                "status": criterion.status,
                "evidence": criterion.evidence,
            }
            for criterion in verdict.criteria
            if criterion.status != "passed"
        ],
        "claimed": [
            {
                "number": index,
                "status": item.get("status", "pending"),
                "evidence": item.get("evidence", ""),
            }
            for index, item in enumerate(claimed, start=1)
        ],
        "claimed_by": claimed_verdict.get("actor", ""),
        "claimed_summary": claimed_verdict.get("summary", ""),
        "exhausted": exhausted,
    }
    task["rework"] = record
    if exhausted:
        add_evidence(
            task,
            f"{actor} rejected this for the {_ordinal(attempt)} time; "
            f"the rework budget of {budget} is spent, so the task is left "
            "failed for a human to look at",
            actor="writ",
        )
        return "failed"
    add_evidence(
        task,
        f"sent back for rework ({attempt} of {budget}) after {actor} "
        "rejected it; the next agent on this task is given the rejection",
        actor="writ",
    )
    return "planned"


#: 1 -> "first". Only ever used for small rework counts, so the table stops
#: where the rework budget realistically does.
ORDINALS = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth"}


def _ordinal(number: int) -> str:
    return ORDINALS.get(number, f"{number}th")


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
