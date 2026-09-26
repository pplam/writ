"""The pre-execution repair loop: findings get adjudicated, not just recorded.

Writ had two halves of this and not the middle. `writ check` and `writ critique`
*produce* findings, and `repair.py` *resolves* them — but only for a gate, and a
gate does not exist until the plan is running. So before execution a blocking
finding had exactly two ends: somebody dispositioned it by hand, or
`writ approve --force` swept the lot through. Neither is a repair. The plan that
executed was the plan the critics objected to, with the objection accepted.

This is the middle:

    check + critics → findings → adjudicator edits a working copy of the plan
    → writ validates the copy → promote → re-check → re-run the critics → repeat

A round is a directory (`rounds/r<rev>/round-<n>/` under the plan directory):
`to-fix.json` holds the blocking findings, `plan/` holds a copy of the plan
files, and the adjudicator edits `plan/features/` in place — revising a feature
by editing its file, adding one by creating a file, removing one by deleting
it — then answers each finding in `response.json`. Writ reads the copy back,
writes what it thinks of it to `validation.json`, and promotes a valid copy into
`state.json` in one transaction. A refused copy is where the next attempt
starts, so the sound edits in it are not redone.

This is not the gate repair of `repair.py`, and on purpose. A gate repair
happens mid-run, around work that is done, so it may only add. Before execution
nothing has run, and the natural fix for most findings — a vaguer criterion
made checkable, two overlapping features merged, a task split in two — is an
edit to the plan, which a patch language expressed badly or not at all.

Two invariants are worth stating plainly, because they are what stops the loop from
being a way to make a bad plan pass:

1. A repair may change the strategy, never the bar. Validation refuses a copy
   that drops a requirement's coverage, invents or edits requirements, or leaves
   a surviving feature with fewer criteria.
2. A finding closes when a *check* says so, not when a response claims it. The loop
   re-runs the deterministic checks and the critics after every promoted copy, and
   a finding that comes back is reopened with its history intact.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from . import agents, contracts, gates, planfiles, plans, prompts, repair, runner, state
from .model import add_task, check_dag, refresh_milestones
from .plancheck import Finding, sort_findings
from .planner import DesignDocs
from .prompts import Ref
from .state import WritError, utcnow

TO_FIX_FILENAME = "to-fix.json"
RESPONSE_FILENAME = "response.json"
VALIDATION_FILENAME = "validation.json"
#: the working copy inside a round directory: `plan.json` and `features/`
WORKING_DIRNAME = "plan"

#: the words a round's error starts with when writ, not the agent, failed it
PROMOTION_FAILED = "the working copy could not be promoted"

#: what the adjudicator is told in autonomous mode, where nobody will answer
AUTONOMOUS_NOTE = """\
Writ is running autonomously: no person will answer a question. A finding that
tells you to decide it is yours to decide. Choose the reading that best serves
the design and its requirements (prefer the simplest one that satisfies them
all), change the plan to follow it, answer it `accepted`, and state the choice
in `decision` as a ruling later agents will build to. Raise a question only
when no reading can satisfy the requirements, and give your `recommendation`."""

RESPONSE_SCHEMA = """\
{
  "analysis": "what was actually wrong, in a few lines",
  "dispositions": [
    {
      "finding_id": "F-0007",
      "disposition": "accepted",
      "change": "M01-002: replaced the vague criterion with a runnable check",
      "decision": "only for a finding you were told to decide: what you decided"
    },
    {
      "finding_id": "F-0009",
      "disposition": "declined",
      "reason": "the design says the cache is optional (section 3), so ..."
    }
  ],
  "questions": [
    {
      "finding_id": "F-0011",
      "question": "only when a finding cannot be closed without a human ruling",
      "context": "what the two readings are and what each would change",
      "recommendation": "the answer you would give, stated as the decision"
    }
  ]
}"""

#: a blocking category no repair can close: the plan needs a ruling, so the
#: loop hands these to a human instead of to the adjudicator
DECISION_CATEGORY = "needs-decision"

CONTRACT_EXAMPLE = """\
{
  "id": "new-retention",
  "title": "Retention and compaction",
  "goal": "one paragraph: what exists when this is done",
  "requirement_ids": ["REQ-014"],
  "owns": ["mmm/retention/"],
  "provides": ["Compactor: compact(before) -> removed count"],
  "consumes": ["EventLog: append(event) -> offset; read(from) -> events"],
  "acceptances": [
    "events older than the retention window are gone after a compaction",
    "a read that spans a compaction returns every surviving event in order",
    "compaction never removes an event a reader has not acknowledged"
  ],
  "notes": "ambiguities, risks"
}"""

CONTRACT_RULES = """\
Rules:
1. Repair the findings in to-fix.json. Do not re-plan the project and do not
   tidy features nothing objected to.
2. The requirement inventory is fixed. Do not edit plan/plan.json, and do not
   name a requirement id that is not in it.
3. The bar may move, never drop. A feature you keep may not end with fewer
   acceptance criteria than it has now, and every requirement some feature
   covers now must still be covered by some feature afterwards.
4. Features of kind "gate" are writ's: do not edit or delete them.
5. Only features with status "planned" may be edited or removed. `id`, `kind`
   and `status` are not yours to change.
6. Do not write `depends_on` between features: writ derives it from the
   contracts. A feature depends on whoever `provides` an interface it
   `consumes`, matched on the name before the colon. To add an edge, add the
   interface; every consumed interface needs exactly one provider, and the
   contracts may not form a cycle.
7. A feature is a subsystem one agent can build on its own: a goal, the
   component or directory it `owns` (never a file list), 3 to 6 observable
   behaviours as acceptance criteria, and no file or test names. Writ fences
   it to `owns` plus the test directories; `allowed` follows `owns`.
8. Every finding in to-fix.json needs an answer in the response: `accepted`
   with `change` naming what you edited, or `declined` with `reason` giving the
   evidence that the finding is wrong. A finding that needs a product decision
   goes in `questions` with its `finding_id` and your `recommendation` instead;
   do not guess. A finding whose `suggested_action` gives a ruling is already
   decided: apply the ruling and answer it `accepted`.
9. A response that edits nothing and asks nothing is refused: an unchanged plan
   draws the same findings again. If every finding is wrong, raise a question.

Write no code, and change no file outside the working copy and the response."""

FEATURE_EXAMPLE = """\
{
  "id": "new-timeout-propagation",
  "title": "Propagate the CLI timeout into execution",
  "milestone": "M03",
  "notes": "why this feature exists and where its boundary is",
  "design_section": "Execution",
  "requirement_ids": ["REQ-014"],
  "depends_on": ["M02-003"],
  "acceptances": [
    "a timeout given on the command line bounds the agent run",
    "the project's test suite passes"
  ],
  "allowed": ["writ/runner/", "tests/"],
  "forbidden": []
}"""

RULES = """\
Rules:
1. Repair the findings in to-fix.json. Do not re-plan the project and do not
   tidy features nothing objected to.
2. The requirement inventory is fixed. Do not edit plan/plan.json, and do not
   name a requirement id that is not in it.
3. The bar may move, never drop. A feature you keep may not end with fewer
   acceptance criteria than it has now, and every requirement some task covers
   now must still be covered by some task afterwards.
4. Features of kind "gate" are writ's: do not edit or delete them. Writ
   recomputes what each gate depends on from its milestone.
5. Only features with status "planned" may be edited or removed. `id`, `kind`,
   `milestone` and `status` are not yours to change; to move a feature to
   another milestone, delete it and add a new one there.
6. Every `depends_on` must name a feature in the working copy. No feature may
   depend on itself, and there may be no cycles.
7. A feature is one bounded session of work for an independent agent: a clear
   outcome, 2 to 6 checkable acceptance criteria, and a fence (`allowed`) naming
   the areas it owns — directories or modules, not files that do not exist yet.
8. Every finding in to-fix.json needs an answer in the response: `accepted`
   with `change` naming what you edited, or `declined` with `reason` giving the
   evidence that the finding is wrong. A finding that needs a product decision
   goes in `questions` with its `finding_id` and your `recommendation` instead;
   do not guess. A finding whose `suggested_action` gives a ruling is already
   decided: apply the ruling and answer it `accepted`.
9. A response that edits nothing and asks nothing is refused: an unchanged plan
   draws the same findings again. If every finding is wrong, raise a question.

Write no code, and change no file outside the working copy and the response."""


@dataclass
class Round:
    """One pass of the loop, and what came of it."""

    number: int
    request_id: str = ""
    revision: int = 0
    exit_code: int | None = None
    error: str = ""
    refused: list[Finding] = field(default_factory=list)
    applied: dict[str, Any] = field(default_factory=dict)
    questions: list[dict[str, Any]] = field(default_factory=list)
    #: the questions still waiting for a person; in autonomous mode, the ones
    #: that could not be answered with their own recommendation
    unanswered: list[str] = field(default_factory=list)
    blocking_before: int = 0
    blocking_after: int = 0

    @property
    def ok(self) -> bool:
        return not self.error and not self.refused

    @property
    def progressed(self) -> bool:
        """Whether this round actually changed the plan."""
        return bool(self.applied)


@dataclass
class Result:
    """The whole loop: every round, and why it stopped."""

    rounds: list[Round] = field(default_factory=list)
    stopped: str = ""
    resolved: int = 0
    remaining: int = 0

    @property
    def clean(self) -> bool:
        return self.remaining == 0


# --------------------------------------------------------------------------
# the prompt


def build_prompt(
    *,
    root: Path,
    doc: DesignDocs,
    directory: Path,
    blocking: int,
    round_number: int = 1,
    base_revision: int = 0,
    previous: Path | None = None,
    extra: Iterable[Ref] = (),
    features: bool = False,
    autonomous: bool = False,
) -> str:
    """Compose the adjudicator's prompt: what to read, what to edit, where to answer.

    Nothing is pasted. The findings are in `to-fix.json`, the plan is the working
    copy, and a refused attempt's reasons are its `validation.json` — which is
    required reading on a retry, because an adjudicator told "you dropped
    REQ-004" makes a different edit, while one told only "refused" makes the same
    one again.
    """
    work = directory / WORKING_DIRNAME
    first = [
        Ref(
            directory / TO_FIX_FILENAME,
            f"the {blocking} blocking finding(s) you must answer, and nothing else",
        )
    ]
    if previous is not None:
        first.append(
            Ref(
                previous,
                "why writ REFUSED your previous attempt. The working copy still holds "
                "that attempt's edits: fix what this lists and keep the rest",
            )
        )
    first.append(
        Ref(
            work / planfiles.INDEX_FILENAME,
            "the plan at a glance: requirements, milestones, one row per feature. "
            "Reference only; do not edit it",
        )
    )
    first.extend(prompts.design_refs(doc, "the design document the plan implements"))
    as_needed = [
        Ref(
            work / planfiles.FEATURES_DIRNAME,
            "the working copy, one file per feature: edit these",
        ),
        *extra,
    ]
    lines = [
        "You are repairing a PLAN that has not been executed yet.",
        "",
        "Independent checks and critics have read it and objected. Answer those "
        "objections by editing a working copy of the plan — not by re-planning the "
        "project, and not by changing what nothing objected to.",
        "",
        prompts.root_line(root),
        f"Plan revision: {base_revision}",
        f"Adjudication round: {round_number}",
        "",
        *prompts.references(root, first=first, as_needed=as_needed),
        f"Edit the working copy in {planfiles.rel(root, work / planfiles.FEATURES_DIRNAME)}:",
        "  - revise a feature by editing its file;",
        "  - add a feature by creating <new-id>.json with an id of your choosing. "
        + (
            "Writ assigns the real id;"
            if features
            else "Writ assigns the real id and rewrites every depends_on that names it;"
        ),
        "  - remove a feature by deleting its file, for example when merging two "
        "that overlap into one.",
        "The feature files define which features exist; plan.json is not updated "
        "by you. A new feature looks like:",
        CONTRACT_EXAMPLE if features else FEATURE_EXAMPLE,
        "",
        CONTRACT_RULES if features else RULES,
        "",
        *([AUTONOMOUS_NOTE, ""] if autonomous else []),
        *prompts.output(root, "response", directory / RESPONSE_FILENAME),
        "",
        "The file must contain JSON only — no prose, no code fence.",
        "",
        "Schema:",
        RESPONSE_SCHEMA,
        "",
        "If you cannot write the file, print the same JSON to stdout inside a "
        "single ```json fenced block instead.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# the loop


def loop(
    *,
    root: Path,
    doc: DesignDocs,
    directory: Path,
    agent: str,
    model: str | None,
    timeout: int | None,
    cwd: str | None,
    max_rounds: int | None = None,
    recheck: Callable[[], list[str]] | None = None,
    stream: bool = False,
    on_round: Callable[[Round], None] | None = None,
    on_start: Callable[[int, agents.ResolvedAgent], None] | None = None,
    autonomous: bool = False,
) -> Result:
    """Run the bounded adjudication loop until the plan is clean or it stops.

    The loop stops for one of four reasons, and says which: nothing blocking is
    left; the budget is spent; a finding survived its own repair; or the adjudicator
    raised a question that needs a human. It never stops because a copy was
    refused — a refusal is information for the next attempt, and
    `repair.patches_left` bounds those separately.

    The budget counts repairs that *landed*, not agent runs. A refused copy changed
    nothing, so spending the plan's repair allowance on it would stop the loop over
    a plan that had never been repaired once.

    `directory` holds one `round-<n>/` per attempt. Numbering continues from the
    rounds already there, so resuming with `writ adjudicate` never overwrites the
    working copy a refused attempt left behind.

    `recheck` is how the critics get re-run between rounds. It is injected rather
    than called directly because that is a command-layer concern — it spends agents,
    it prints, and it belongs to whoever asked for the loop. Without it the loop
    still re-runs the deterministic checks, which is the cheaper half of the same
    idea.

    `autonomous` is for a run nobody is watching: a `needs-decision` finding goes
    to the adjudicator to decide rather than to a person, and a question it raises
    with a recommendation is answered with it. Both still land in the decision
    log, confirmed by `autonomous`, so what was decided on whose word is kept.
    """
    resolved = agents.resolve(agent, [], model, events=True)
    directory.mkdir(parents=True, exist_ok=True)
    result = Result()
    budget = repair.DEFAULT_MAX_REPAIR_ROUNDS if max_rounds is None else max_rounds
    opened = _blocking_ids(state.load(root))
    # Two counters, because a refusal is not a round. `attempt` numbers the agent
    # runs, so each one gets its own directory and its own place in the record;
    # `landed` counts the copies that actually changed the plan, which is what the
    # budget is about.
    attempt = rounds_on_disk(directory)
    first_attempt = attempt + 1
    landed = 0
    while True:
        attempt += 1
        data = state.load(root)
        blocking = [
            finding
            for finding in plans.findings(data, open_only=True)
            if finding.severity == "error"
        ]
        if not blocking:
            result.stopped = "clean"
            break
        # A finding that asks for a ruling is not the adjudicator's to answer:
        # repairing around it is a guess. It goes to the decision log, and only
        # what a repair can close is handed on.
        # Once a person has ruled, the finding is an ordinary repair: the plan
        # has to be changed to say what they decided. Without this the ruling was
        # recorded and nothing ever acted on it, so the finding stood forever.
        # A ruling on any other finding is one the adjudicator asked for earlier;
        # it is handed back with the finding for the same reason.
        decide = [f for f in blocking if f.category == DECISION_CATEGORY]
        ruled = [_with_ruling(data, f) for f in decide]
        waiting = [f for f, answer in zip(decide, ruled) if answer is None]
        if waiting:
            _route_decisions(root, waiting)
        blocking = [
            _with_ruling(data, f) or f
            for f in blocking
            if f.category != DECISION_CATEGORY
        ] + [answer for answer in ruled if answer is not None]
        if autonomous:
            # Nobody is coming to rule, so the adjudicator does. The question
            # stays in the log and gets its answer from the response.
            blocking += [_to_decide(f) for f in waiting]
            waiting = []
        if not blocking:
            result.stopped = awaiting_ruling(state.load(root), waiting)
            break
        if landed >= budget:
            result.stopped = (
                (
                    f"the plan has been repaired {landed} time(s), its budget. What "
                    "is still open needs a decision rather than another repair."
                )
                if landed
                else "no repair was allowed (--max-rounds 0), so nothing was tried."
            )
            break
        stop = repair.plan_exhausted(data, max_rounds=budget)
        if stop and attempt > first_attempt:
            result.stopped = stop
            break
        round_ = _one_round(
            root=root,
            doc=doc,
            directory=directory / f"round-{attempt}",
            resolved=resolved,
            timeout=timeout,
            cwd=cwd,
            number=attempt,
            blocking=blocking,
            stream=stream,
            on_start=on_start,
            autonomous=autonomous,
        )
        result.rounds.append(round_)
        if on_round is not None:
            on_round(round_)
        if round_.questions and autonomous and not round_.unanswered:
            # Every question came with a recommendation and was answered with it;
            # the next round repairs the plan to follow those answers.
            if round_.progressed:
                landed += 1
                if recheck is not None:
                    recheck()
            continue
        if round_.questions:
            result.stopped = (
                f"the adjudicator raised {len(round_.questions)} question(s) it "
                "could not settle itself; they are in the decision log "
                "(writ decisions)."
            )
            if round_.progressed and recheck is not None:
                recheck()
            break
        if round_.error:
            # Named for whose failure it was. A copy writ validated and then could
            # not promote is writ's bug, and telling someone their adjudicator failed
            # sends them to read a transcript of an agent that did nothing wrong.
            whose = (
                "the repair could not be applied"
                if round_.error.startswith(PROMOTION_FAILED)
                else "the adjudicator failed"
            )
            result.stopped = f"{whose}: {round_.error}"
            break
        if round_.refused and not round_.progressed:
            request = _open_request(root)
            if request is not None and not repair.patches_left(request):
                result.stopped = (
                    f"writ refused {repair.refusals(request)} attempts for "
                    f"{request['id']}; what is being asked of the adjudicator is "
                    "what needs to change, not the wording of its edit."
                )
                break
            continue
        if round_.progressed:
            landed += 1
            if recheck is not None:
                # The copy landed, so every critic that passed the old revision has
                # now reviewed something else. Re-running them is what closes a
                # finding on evidence rather than on the response's word.
                recheck()
    data = state.load(root)
    if data.get("decisions"):
        from . import decisions

        decisions.sync_markdown(root, data)
    still_open = _blocking_ids(data)
    result.resolved = len(opened - still_open)
    result.remaining = len(still_open)
    return result


def rounds_on_disk(directory: Path) -> int:
    """The highest `round-<n>` already in `directory`, or 0."""
    numbers = [
        int(path.name.split("-", 1)[1])
        for path in directory.glob("round-*")
        if path.is_dir() and path.name.split("-", 1)[1].isdigit()
    ]
    return max(numbers, default=0)


def _one_round(
    *,
    root: Path,
    doc: DesignDocs,
    directory: Path,
    resolved: agents.ResolvedAgent,
    timeout: int | None,
    cwd: str | None,
    number: int,
    blocking: list[Finding],
    stream: bool,
    on_start: Callable[[int, agents.ResolvedAgent], None] | None,
    autonomous: bool = False,
) -> Round:
    """One attempt: prepare the round, run the agent, validate, promote or refuse."""
    directory.mkdir(parents=True, exist_ok=True)
    with state.transaction(root) as data:
        request = repair.plan_request(data)
        if request is None:
            request = repair.open_request(
                data,
                gate_id=None,
                finding_ids=[f.id for f in blocking if f.id],
                summary=_summary(blocking),
                actor="adjudicator",
            )
        else:
            # An open request from a refused round. Its findings are re-stated, so a
            # finding that appeared since is answered too.
            request["findings"] = [f.id for f in blocking if f.id]
        request["status"] = "planning"
        request_id = request["id"]
        base_revision = plans.revision(data)
        seed, previous = _previous_attempt(root, request, base_revision, directory)
        prepare(root, data, directory, blocking, seed=seed)
        extra = _artifact_refs(root, data)
        features = _has_features(data)
    round_ = Round(
        number=number,
        request_id=request_id,
        revision=base_revision,
        blocking_before=len(blocking),
    )
    prompt = build_prompt(
        root=root,
        doc=doc,
        directory=directory,
        blocking=len(blocking),
        round_number=number,
        base_revision=base_revision,
        previous=previous,
        extra=extra,
        features=features,
        autonomous=autonomous,
    )
    if on_start is not None:
        on_start(number, resolved)
    response_path = directory / RESPONSE_FILENAME
    try:
        round_.exit_code = runner.run_agent(
            resolved.command,
            prompt,
            directory,
            cwd or root,
            timeout,
            stream=stream,
            prefix=f"  adjudicate {number} | " if stream else "",
            event_shape=resolved.event_shape,
        )
    except FileNotFoundError:
        round_.error = f"adjudicator agent not found: {resolved.command[0]}"
        _reopen(root, request_id)
        return round_
    except WritError as exc:
        round_.error = str(exc)
        _reopen(root, request_id)
        return round_
    text = _response_text(directory, response_path)
    if text is None:
        round_.error = (
            f"the adjudicator wrote no response to {planfiles.rel(root, response_path)}"
        )
        _reopen(root, request_id)
        return round_
    try:
        response = load_response(text)
    except WritError as exc:
        round_.error = f"unusable response: {exc}"
        _reopen(root, request_id)
        return round_
    # Validation decides what may be promoted, so a copy that fails *during*
    # promotion has broken an invariant validation does not cover — writ's bug, not
    # the adjudicator's. It still must not take the loop down with it: the round
    # fails, the transaction rolls back, and the request stays open.
    try:
        with state.transaction(root) as data:
            request = repair.get_request(data, request_id)
            found, proposed, diff = validate(
                data,
                directory,
                response,
                finding_ids=[f.id for f in blocking if f.id],
                base_revision=base_revision,
            )
            refused = [finding for finding in found if finding.blocking]
            _write_validation(directory, base_revision, found, diff)
            if refused:
                round_.refused = refused
                request["status"] = "open"
                request.setdefault("refusals", []).append(
                    {
                        "at": utcnow(),
                        "round": number,
                        "revision": base_revision,
                        "directory": planfiles.rel(root, directory),
                        "reasons": [finding.to_dict() for finding in refused],
                    }
                )
                return round_
            questions = response["questions"]
            if _no_edits(diff):
                round_.questions = list(questions)
                round_.unanswered = _raise_questions(
                    data, questions, request, autonomous=autonomous
                )
                return round_
            round_.applied = promote(
                data, request, proposed, diff, response, actor="adjudicator"
            )
            if autonomous:
                _record_rulings(data, response)
            if questions:
                # Edits and questions together: the edits land, and the questions
                # still stop the loop for the human who has to answer them.
                round_.questions = list(questions)
                round_.unanswered = _raise_questions(
                    data, questions, request, status="applied", autonomous=autonomous
                )
            # Deterministic checks run against the promoted plan immediately. A copy
            # that closed one finding and opened another says so here, before the
            # critics are spent on it.
            plans.run_check(data, root=root.resolve())
            planfiles.export(root, data)
            round_.blocking_after = sum(
                1
                for finding in plans.findings(data, open_only=True)
                if finding.severity == "error"
            )
    except WritError as exc:
        round_.error = f"{PROMOTION_FAILED}: {exc}"
        round_.applied = {}
        round_.questions = []
        _reopen(root, request_id)
        return round_
    return round_


# --------------------------------------------------------------------------
# preparing a round


def prepare(
    root: Path,
    data: dict[str, Any],
    directory: Path,
    blocking: Iterable[Finding],
    *,
    seed: Path | None = None,
) -> Path:
    """Write `to-fix.json` and the working copy for one attempt.

    `seed` is a refused attempt's `plan/features/`. Starting from it is what makes
    a retry a correction instead of a fresh attempt: the edits that were sound are
    still there, and only what `validation.json` listed needs changing.
    """
    planfiles.dump(
        directory / TO_FIX_FILENAME,
        [finding.to_dict() for finding in blocking],
    )
    work = directory / WORKING_DIRNAME
    features = work / planfiles.FEATURES_DIRNAME
    if features.exists():
        shutil.rmtree(features)
    if seed is not None and seed.is_dir():
        shutil.copytree(seed, features)
    else:
        planfiles.write_features(features, data.get("tasks", {}))
    index = planfiles.index(root, data)
    for row in index["features"]:
        row["file"] = planfiles.rel(root, features / f"{row['id']}.json")
    planfiles.dump(work / planfiles.INDEX_FILENAME, index)
    return work


def _previous_attempt(
    root: Path, request: dict[str, Any], revision: int, directory: Path
) -> tuple[Path | None, Path | None]:
    """The refused working copy to start from, and its validation report.

    Only a refusal against this same revision counts: a copy of an older plan
    would undo whatever landed since.
    """
    refusals = request.get("refusals") or []
    if not refusals:
        return None, None
    last = refusals[-1]
    if last.get("revision") != revision or not last.get("directory"):
        return None, None
    folder = Path(root) / str(last["directory"])
    if folder.resolve() == directory.resolve():
        return None, None
    seed = folder / WORKING_DIRNAME / planfiles.FEATURES_DIRNAME
    report = folder / VALIDATION_FILENAME
    return (
        seed if seed.is_dir() else None,
        report if report.exists() else None,
    )


def _artifact_refs(root: Path, data: dict[str, Any]) -> list[Ref]:
    """The analysis artifacts next to the plan, when they exist."""
    folder = planfiles.directory(root, data)
    refs = []
    for name, purpose in (
        ("inventory.json", "the repo summary: language, tests, components"),
        ("requirements.json", "each requirement and its details"),
    ):
        if (folder / name).exists():
            refs.append(Ref(folder / name, purpose))
    return refs


# --------------------------------------------------------------------------
# reading the attempt back


def load_response(text: str) -> dict[str, Any]:
    """Parse and shape-check the adjudicator's response. Semantics come later."""
    from .planning import extract_json

    stripped = (text or "").strip()
    if not stripped:
        raise WritError("the response is empty")
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        recovered = extract_json(stripped)
        if recovered is None:
            raise WritError(f"the response is not valid JSON: {exc}") from exc
        payload = json.loads(recovered)
    if not isinstance(payload, dict):
        raise WritError("the response must be a JSON object")
    return {
        "analysis": str(payload.get("analysis", "")).strip(),
        "dispositions": _objects(payload.get("dispositions"), "dispositions"),
        "questions": _objects(payload.get("questions"), "questions"),
    }


def _objects(value: Any, where: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise WritError(f"`{where}` must be a list")
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise WritError(f"{where}[{index}] must be an object")
    return list(value)


def _response_text(directory: Path, path: Path) -> str | None:
    """The response file, or JSON the adjudicator printed to stdout instead."""
    from .planning import extract_json

    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    stdout = directory / "stdout.log"
    if not stdout.exists():
        return None
    text = stdout.read_text(encoding="utf-8").strip()
    if not text:
        return None
    return extract_json(text)


LIST_FIELDS = (
    "requirement_ids",
    "depends_on",
    "acceptances",
    "allowed",
    "forbidden",
    "owns",
    "provides",
    "consumes",
)


def _criterion(item: Any) -> str:
    return str(item.get("text", "")) if isinstance(item, dict) else str(item)


def normal(entry: dict[str, Any]) -> dict[str, Any]:
    """A feature's editable fields, in the form two of them are compared in."""
    return {
        "title": str(entry.get("title") or "").strip(),
        "goal": str(entry.get("goal") or "").strip(),
        "owns": [str(item) for item in entry.get("owns") or []],
        "provides": [str(item) for item in entry.get("provides") or []],
        "consumes": [str(item) for item in entry.get("consumes") or []],
        "notes": str(entry.get("notes") or "").strip(),
        "design_section": entry.get("design_section") or None,
        "requirement_ids": [str(item) for item in entry.get("requirement_ids") or []],
        "depends_on": [str(item) for item in entry.get("depends_on") or []],
        "acceptances": [
            _criterion(item).strip() for item in entry.get("acceptances") or []
        ],
        "allowed": [str(item) for item in entry.get("allowed") or []],
        "forbidden": [str(item) for item in entry.get("forbidden") or []],
    }


def _refuse(category: str, message: str, where: str, action: str = "") -> Finding:
    return Finding(
        severity="error",
        category=category,
        message=message,
        where=where,
        suggested_action=action,
        source="writ",
    )


def read_copy(work: Path) -> tuple[dict[str, dict[str, Any]], list[Finding]]:
    """Every feature file in the working copy, and what is wrong with its shape."""
    found: list[Finding] = []
    proposed: dict[str, dict[str, Any]] = {}
    folder = work / planfiles.FEATURES_DIRNAME
    for path in sorted(folder.glob("*.json")):
        where = f"{WORKING_DIRNAME}/{planfiles.FEATURES_DIRNAME}/{path.name}"
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            found.append(_refuse("feature-shape", f"is not valid JSON: {exc}", where))
            continue
        if not isinstance(entry, dict):
            found.append(_refuse("feature-shape", "must be a JSON object", where))
            continue
        problems = []
        if str(entry.get("id", "")) != path.stem:
            problems.append(
                f"its id {entry.get('id')!r} does not match its filename {path.stem!r}"
            )
        if not str(entry.get("title") or "").strip():
            problems.append("has no title")
        for key in LIST_FIELDS:
            if entry.get(key) is not None and not isinstance(entry.get(key), list):
                problems.append(f"`{key}` must be a list")
        if isinstance(entry.get("acceptances"), list) and not any(
            _criterion(item).strip() for item in entry["acceptances"]
        ):
            problems.append("has no acceptance criteria")
        elif entry.get("acceptances") is None:
            problems.append("has no acceptance criteria")
        if problems:
            found.extend(_refuse("feature-shape", problem, where) for problem in problems)
            continue
        proposed[path.stem] = entry
    return proposed, found


def _digest(rows: Any) -> str:
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def validate(
    data: dict[str, Any],
    directory: Path,
    response: dict[str, Any],
    *,
    finding_ids: Iterable[str],
    base_revision: int,
) -> tuple[list[Finding], dict[str, dict[str, Any]], dict[str, list[str]]]:
    """Everything writ enforces about a working copy, as findings rather than a raise.

    A list, because a copy with three problems should say so once. Returns the
    findings, the features the copy proposes, and the diff against the committed
    plan (`revised`, `added`, `removed`).
    """
    work = directory / WORKING_DIRNAME
    tasks = data.get("tasks", {})
    inventory = plans.requirements(data)
    proposed, found = read_copy(work)
    index_where = f"{WORKING_DIRNAME}/{planfiles.INDEX_FILENAME}"

    if plans.revision(data) != base_revision:
        found.append(
            _refuse(
                "stale-copy",
                f"the copy was made from revision {base_revision}, but the plan is "
                f"at {plans.revision(data)}; it changed underneath this attempt",
                index_where,
            )
        )
    try:
        index = json.loads((work / planfiles.INDEX_FILENAME).read_text(encoding="utf-8"))
        rows = index.get("requirements") if isinstance(index, dict) else None
    except (OSError, json.JSONDecodeError):
        rows = None
    if rows is None or _digest(rows) != _digest(planfiles.requirement_rows(data)):
        found.append(
            _refuse(
                "requirements-edited",
                "the requirement inventory in the copied index was changed or "
                "removed; the inventory is fixed",
                index_where,
                "leave plan/plan.json exactly as writ wrote it",
            )
        )

    committed = {task_id: normal(planfiles.feature(task)) for task_id, task in tasks.items()}
    diff: dict[str, list[str]] = {"revised": [], "added": [], "removed": []}
    for task_id, task in tasks.items():
        where = f"{WORKING_DIRNAME}/{planfiles.FEATURES_DIRNAME}/{task_id}.json"
        entry = proposed.get(task_id)
        is_gate = task.get("kind") == "gate"
        if entry is None:
            if is_gate:
                found.append(
                    _refuse("gate-edited", f"gate {task_id} was deleted", where,
                            "restore the file; gates are writ's")
                )
            elif task.get("status") != "planned":
                found.append(
                    _refuse(
                        "started-task-edited",
                        f"{task_id} is {task.get('status')} and was deleted",
                        where,
                        "restore it, or raise a question",
                    )
                )
            else:
                diff["removed"].append(task_id)
            continue
        for key in ("kind", "milestone"):
            if key in entry and entry.get(key) != task.get(key):
                found.append(
                    _refuse(
                        "fixed-field-edited",
                        f"`{key}` changed from {task.get(key)!r} to {entry.get(key)!r}",
                        where,
                        "to move a feature, delete it and add a new one",
                    )
                )
        after = normal(entry)
        before = committed[task_id]
        if is_gate:
            # What a gate depends on and covers is recomputed from its milestone,
            # so only the parts writ does not derive are compared.
            derived = ("depends_on", "requirement_ids")
            if {k: v for k, v in after.items() if k not in derived} != {
                k: v for k, v in before.items() if k not in derived
            }:
                found.append(
                    _refuse("gate-edited", f"gate {task_id} was edited", where,
                            "restore the file; gates are writ's")
                )
            continue
        if after == before:
            continue
        if task.get("status") != "planned":
            found.append(
                _refuse(
                    "started-task-edited",
                    f"{task_id} is {task.get('status')} and was edited",
                    where,
                    "restore it, or raise a question",
                )
            )
            continue
        if len(after["acceptances"]) < len(before["acceptances"]):
            found.append(
                _refuse(
                    "weakened-criteria",
                    f"{task_id} ends with {len(after['acceptances'])} criteria; it "
                    f"had {len(before['acceptances'])}",
                    where,
                    "sharpen criteria rather than removing them",
                )
            )
        diff["revised"].append(task_id)
    for ref, entry in proposed.items():
        if ref in tasks:
            continue
        where = f"{WORKING_DIRNAME}/{planfiles.FEATURES_DIRNAME}/{ref}.json"
        if str(entry.get("kind") or "task") != "task":
            found.append(
                _refuse("feature-shape", "a new feature must be of kind `task`", where)
            )
        milestone = entry.get("milestone")
        milestones = data.get("milestones", {})
        if milestone and milestone not in milestones:
            found.append(
                _refuse("feature-shape", f"names unknown milestone {milestone!r}", where)
            )
        elif not milestone and milestones and not contracts.is_feature(entry):
            found.append(
                _refuse(
                    "feature-shape",
                    "a new feature needs a `milestone`",
                    where,
                    f"one of {', '.join(sorted(milestones))}",
                )
            )
        diff["added"].append(ref)

    # Requirements: none invented, and none a task covered left uncovered.
    for ref, entry in proposed.items():
        unknown = [req for req in normal(entry)["requirement_ids"] if req not in inventory]
        if unknown and inventory:
            found.append(
                _refuse(
                    "unknown-requirement",
                    f"names requirement(s) not in the inventory: {', '.join(unknown)}",
                    f"{WORKING_DIRNAME}/{planfiles.FEATURES_DIRNAME}/{ref}.json",
                )
            )

    def covered(features: dict[str, dict[str, Any]], kinds: dict[str, str]) -> set[str]:
        return {
            req
            for ref, entry in features.items()
            if kinds.get(ref, "task") != "gate"
            for req in entry["requirement_ids"]
        }

    kinds = {task_id: task.get("kind", "task") for task_id, task in tasks.items()}
    lost = covered(committed, kinds) - covered(
        {ref: normal(entry) for ref, entry in proposed.items()}, kinds
    )
    if lost:
        found.append(
            _refuse(
                "coverage-regression",
                f"no task covers {', '.join(sorted(lost))} any more",
                f"{WORKING_DIRNAME}/{planfiles.FEATURES_DIRNAME}/",
                "keep every requirement covered by some task",
            )
        )

    found.extend(_validate_graph(tasks, proposed))
    found.extend(_validate_answers(response, finding_ids, diff))
    return sort_findings(found), proposed, diff


def _gate_edges(nodes: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """What each gate depends on, derived from milestone membership.

    `nodes` maps an id to a record with `kind`, `milestone` and, for a gate,
    `scope`. A milestone gate waits for the milestone's tasks; the final gate
    waits for every milestone gate and every task no milestone gate covers.
    """
    plain = {ref: node for ref, node in nodes.items() if node.get("kind") != "gate"}
    edges: dict[str, list[str]] = {}
    milestone_gates = [
        ref
        for ref, node in nodes.items()
        if node.get("kind") == "gate" and ref != gates.FINAL_GATE_ID
    ]
    for ref in milestone_gates:
        milestone = gates.milestone_of(nodes[ref]) or nodes[ref].get("milestone")
        edges[ref] = sorted(
            other for other, node in plain.items() if node.get("milestone") == milestone
        )
    if gates.FINAL_GATE_ID in nodes:
        held = {dep for deps in edges.values() for dep in deps}
        edges[gates.FINAL_GATE_ID] = sorted(
            set(milestone_gates) | {ref for ref in plain if ref not in held}
        )
    return edges


def _validate_graph(
    tasks: dict[str, dict[str, Any]], proposed: dict[str, dict[str, Any]]
) -> list[Finding]:
    """Dependencies name features in the copy, and there is no cycle."""
    found: list[Finding] = []
    nodes: dict[str, dict[str, Any]] = {}
    for ref, entry in proposed.items():
        task = tasks.get(ref)
        nodes[ref] = {
            "kind": task.get("kind", "task") if task else "task",
            "milestone": task.get("milestone") if task else entry.get("milestone"),
            "scope": task.get("scope") if task else None,
        }
    edges: dict[str, list[str]] = {}
    for ref, entry in proposed.items():
        if nodes[ref]["kind"] == "gate":
            continue
        where = f"{WORKING_DIRNAME}/{planfiles.FEATURES_DIRNAME}/{ref}.json"
        deps = normal(entry)["depends_on"]
        if contracts.is_feature(entry):
            # a feature's edges to other features are derived on promotion, so
            # a stale one left in the file is ignored rather than refused
            deps = [
                dep
                for dep in deps
                if not contracts.is_feature(tasks.get(dep) or proposed.get(dep) or {})
                and not (dep in tasks and dep not in proposed)
            ]
        for dep in deps:
            if dep == ref:
                found.append(_refuse("bad-dependency", "depends on itself", where))
            elif dep not in proposed:
                found.append(
                    _refuse(
                        "bad-dependency",
                        f"depends on {dep}, which is not in the working copy",
                        where,
                    )
                )
        edges[ref] = [dep for dep in deps if dep in proposed and dep != ref]
    # the edges writ will derive from the contracts on promotion, so a cycle the
    # contracts make is refused here rather than failing the promotion
    for ref, deps in contracts.edges(_feature_entries(proposed)).items():
        edges[ref] = sorted(set(edges.get(ref, [])) | set(deps))
    edges.update(_gate_edges(nodes))
    cycle = _cycle(edges)
    if cycle:
        found.append(
            _refuse(
                "dependency-cycle",
                "dependency cycle: " + " -> ".join(cycle),
                f"{WORKING_DIRNAME}/{planfiles.FEATURES_DIRNAME}/",
                "a gate waits for its milestone's tasks, so a task may not depend "
                "on its own milestone's gate",
            )
        )
    return found


def _cycle(edges: dict[str, list[str]]) -> list[str]:
    marks: dict[str, int] = {}
    trail: list[str] = []

    def visit(node: str) -> list[str]:
        mark = marks.get(node, 0)
        if mark == 1:
            return trail[trail.index(node):] + [node]
        if mark == 2:
            return []
        marks[node] = 1
        trail.append(node)
        for dep in edges.get(node, []):
            found = visit(dep)
            if found:
                return found
        trail.pop()
        marks[node] = 2
        return []

    for node in edges:
        found = visit(node)
        if found:
            return found
    return []


def _no_edits(diff: dict[str, list[str]]) -> bool:
    return not any(diff.values())


def _validate_answers(
    response: dict[str, Any], finding_ids: Iterable[str], diff: dict[str, list[str]]
) -> list[Finding]:
    """Every finding is answered, and the attempt did something."""
    found: list[Finding] = []
    questions = response["questions"]
    if _no_edits(diff) and not questions:
        found.append(
            _refuse(
                "no-op",
                "edits nothing and asks nothing, so the plan would draw the same "
                "findings again",
                RESPONSE_FILENAME,
                "edit the features the findings are about, or raise a question",
            )
        )
        return found
    if _no_edits(diff):
        # "I cannot repair any of this without a ruling" answers the whole round, so
        # its questions need not name each finding one by one.
        return found
    stated = {
        str(entry.get("finding_id", "")): entry for entry in response["dispositions"]
    }
    asked = {str(question.get("finding_id", "")) for question in questions}
    for finding_id in finding_ids:
        entry = stated.get(finding_id)
        if entry is None:
            if finding_id not in asked:
                found.append(
                    _refuse(
                        "undisposed-finding",
                        f"{finding_id} is in to-fix.json and the response does not "
                        "answer it",
                        RESPONSE_FILENAME,
                        "accept it and name the change, decline it with evidence, or "
                        "raise it as a question",
                    )
                )
            continue
        disposition = str(entry.get("disposition", entry.get("resolution", ""))).lower()
        if disposition == "declined":
            if not str(entry.get("reason", "")).strip():
                found.append(
                    _refuse(
                        "undisposed-finding",
                        f"{finding_id} is declined with no reason given",
                        RESPONSE_FILENAME,
                        "say why the finding is wrong, with evidence",
                    )
                )
        elif disposition == "accepted":
            if not str(entry.get("change", "")).strip():
                found.append(
                    _refuse(
                        "undisposed-finding",
                        f"{finding_id} is accepted but `change` does not say what "
                        "was edited",
                        RESPONSE_FILENAME,
                        "name the features you changed and how",
                    )
                )
        else:
            found.append(
                _refuse(
                    "undisposed-finding",
                    f"{finding_id} has disposition {disposition!r}; use `accepted` "
                    "or `declined`",
                    RESPONSE_FILENAME,
                )
            )
    return found


def _write_validation(
    directory: Path, revision: int, found: list[Finding], diff: dict[str, list[str]]
) -> None:
    refused = [finding for finding in found if finding.blocking]
    planfiles.dump(
        directory / VALIDATION_FILENAME,
        {
            "accepted": not refused,
            "revision": revision,
            "problems": [finding.to_dict() for finding in found],
            "changes": diff,
        },
    )


# --------------------------------------------------------------------------
# promotion


def promote(
    data: dict[str, Any],
    request: dict[str, Any],
    proposed: dict[str, dict[str, Any]],
    diff: dict[str, list[str]],
    response: dict[str, Any],
    *,
    actor: str = "adjudicator",
) -> dict[str, Any]:
    """Apply a validated working copy to the graph.

    The caller holds the state transaction, so a raise here rolls the whole
    promotion back rather than leaving half a repair in the store.
    """
    tasks = data["tasks"]
    removed = list(diff["removed"])
    translate: dict[str, str] = {}
    minted: list[str] = []
    for ref in diff["added"]:
        milestone = proposed[ref].get("milestone") or None
        if contracts.is_feature(proposed[ref]) and milestone not in data.get(
            "milestones", {}
        ):
            task_id = _next_feature_id(data, minted)
        else:
            task_id = repair._next_repair_id(
                data,
                milestone if milestone in data.get("milestones", {}) else None,
                minted=minted,
            )
        minted.append(task_id)
        translate[ref] = task_id

    for task_id in removed:
        tasks.pop(task_id, None)
    for task in tasks.values():
        if any(dep in removed for dep in task.get("depends_on", [])):
            task["depends_on"] = [
                dep for dep in task["depends_on"] if dep not in removed
            ]

    extras = _fence_extras(tasks)
    for ref in diff["added"]:
        entry = normal(proposed[ref])
        feature = contracts.is_feature(proposed[ref])
        milestone = proposed[ref].get("milestone") or None
        task = add_task(
            data,
            task_id=translate[ref],
            title=entry["title"],
            milestone=milestone if milestone in data.get("milestones", {}) else None,
            acceptances=entry["acceptances"],
            allowed=(
                contracts.fence(entry["owns"], [*entry["allowed"], *extras])
                if feature
                else entry["allowed"]
            ),
            forbidden=entry["forbidden"],
            design_section=entry["design_section"] or entry["title"],
            requirement_ids=entry["requirement_ids"],
            notes=entry["notes"],
            feature=(
                {key: entry[key] for key in contracts_fields()} if feature else None
            ),
        )
        task["repair"] = {
            "request": request["id"],
            "gate": "",
            "scope": repair.scope_of(request),
            "proposed_as": ref,
            "round": request.get("round", 1),
        }

    revised: list[str] = []
    for ref in [*diff["revised"], *diff["added"]]:
        entry = normal(proposed[ref])
        task = tasks[translate.get(ref, ref)]
        before = normal(planfiles.feature(task))
        entry["depends_on"] = [translate.get(dep, dep) for dep in entry["depends_on"]]
        if ref in translate:
            task["depends_on"] = entry["depends_on"]
            continue
        changed = sorted(key for key in entry if entry[key] != before[key])
        if "owns" in changed and "allowed" not in changed:
            # The fence follows the component: what was fenced beyond the old
            # `owns` (the test directories, anything added by hand) stays.
            kept = [path for path in before["allowed"] if path not in before["owns"]]
            entry["allowed"] = contracts.fence(entry["owns"], kept)
            if entry["allowed"] != before["allowed"]:
                changed = sorted({*changed, "allowed"})
        for key in changed:
            if key == "acceptances":
                # A criterion is a record, not a string: it carries the status a
                # reviewer will set. One whose wording is unchanged keeps its record.
                held = {item["text"]: item for item in task.get("acceptances", [])}
                task["acceptances"] = [
                    dict(held[text]) if text in held else {"text": text, "status": "pending"}
                    for text in entry["acceptances"]
                ]
            else:
                task[key] = entry[key]
        if changed:
            task.setdefault("revisions", []).append(
                {"request": request["id"], "at": utcnow(), "fields": changed}
            )
            task["updated_at"] = utcnow()
            revised.append(ref)

    _rederive_edges(data)
    _recompute_gates(data)
    refresh_milestones(data)
    check_dag(data)

    for entry in response["dispositions"]:
        disposition = str(entry.get("disposition", entry.get("resolution", ""))).lower()
        if disposition not in ("accepted", "declined"):
            continue
        try:
            plans.dispose(
                data,
                str(entry.get("finding_id", "")),
                disposition,
                actor=actor,
                reason=str(entry.get("reason", "")),
                change=str(entry.get("change", "")),
            )
        except WritError:
            continue

    added = [translate[ref] for ref in diff["added"]]
    request["status"] = "applied"
    request["applied_at"] = utcnow()
    request["applied_tasks"] = added
    request["revised_tasks"] = revised
    request["removed_tasks"] = removed
    request["analysis"] = response["analysis"]
    request["questions"] = list(response["questions"])
    plans.bump(data)
    return {
        "tasks": added,
        "revised": revised,
        "removed": removed,
        "scope": repair.scope_of(request),
        "revision": plans.revision(data),
    }


def contracts_fields() -> tuple[str, ...]:
    return planfiles.CONTRACT_FIELDS


def _has_features(data: dict[str, Any]) -> bool:
    return any(contracts.is_feature(task) for task in data.get("tasks", {}).values())


def _feature_entries(entries: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {ref: entry for ref, entry in entries.items() if contracts.is_feature(entry)}


def _next_feature_id(data: dict[str, Any], minted: Iterable[str]) -> str:
    claimed = set(data["tasks"]) | set(minted)
    taken = [
        int(task_id[3:])
        for task_id in claimed
        if task_id.startswith("FT-") and task_id[3:].isdigit()
    ]
    return f"FT-{(max(taken) + 1) if taken else 1:03d}"


def _fence_extras(tasks: dict[str, dict[str, Any]]) -> list[str]:
    """What every feature is fenced to beyond what it owns: the test directories.

    Read off the committed features rather than the repo summary, because they
    are what the summary put there at commit, and promotion has no other copy.
    """
    extras: list[set[str]] = [
        set(task.get("allowed") or []) - set(task.get("owns") or [])
        for task in tasks.values()
        if contracts.is_feature(task) and task.get("kind") != "gate"
    ]
    if not extras:
        return []
    return sorted(set.intersection(*extras))


def _rederive_edges(data: dict[str, Any]) -> None:
    """Replace every planned feature's edges to other features with the derived ones.

    Edges between features are the contracts' to state (docs/planning-redesign.md
    §4). An edge to a plain task was stated by hand and is kept; an edge to a
    feature is recomputed, so a contract a repair removed takes its edge with it.
    """
    tasks = data["tasks"]
    features = {
        task_id: task for task_id, task in tasks.items() if contracts.is_feature(task)
    }
    for task_id, deps in contracts.edges(features).items():
        task = features[task_id]
        if task.get("status") != "planned":
            continue
        stated = [
            dep
            for dep in task.get("depends_on", [])
            if dep in tasks and dep not in features
        ]
        task["depends_on"] = stated + [dep for dep in deps if dep not in stated]


def _with_ruling(data: dict[str, Any], finding: Finding) -> Finding | None:
    """This `needs-decision` finding restated as the change its ruling asks for."""
    from . import decisions

    record = decisions.ruling(data, finding.id)
    if record is None:
        return None
    who = (
        "Writ decided this autonomously"
        if record.get("confirmed_by") == decisions.AUTONOMOUS
        else "A person has ruled on this"
    )
    return dataclasses.replace(
        finding,
        suggested_action=(
            f"{who} ({record['id']}): {record['decision']} "
            "Change the plan so it follows this ruling; do not raise it as a "
            "question again."
        ),
    )


def _to_decide(finding: Finding) -> Finding:
    """A `needs-decision` finding handed to the adjudicator to settle itself."""
    return dataclasses.replace(
        finding,
        suggested_action=(
            "Nobody will rule on this: decide it yourself (see the note on "
            "autonomous runs), change the plan to follow your decision, answer "
            "`accepted`, and state it in `decision`."
            + (
                f" The critic suggested: {finding.suggested_action}"
                if finding.suggested_action
                else ""
            )
        ),
    )


def _record_rulings(data: dict[str, Any], response: dict[str, Any]) -> list[str]:
    """Answer each question the adjudicator just decided, from its response.

    The question was put in the log when the finding was routed; this fills in
    what was chosen, so an autonomous plan's rulings are as findable as a
    person's. `change` stands in for a `decision` the adjudicator left out:
    it says what the plan now does, which is the ruling in effect. A declined
    finding is a ruling too: that the plan's reading stands, and why.
    """
    from . import decisions

    answered = []
    for entry in response.get("dispositions", []):
        finding_id = str(entry.get("finding_id", ""))
        record = decisions.asked(data, finding_id) if finding_id else None
        if record is None:
            continue
        disposition = str(entry.get("disposition", "")).lower()
        if disposition == "accepted":
            ruling = str(entry.get("decision") or entry.get("change") or "").strip()
        elif disposition == "declined" and str(entry.get("reason", "")).strip():
            ruling = f"The plan stands as written: {str(entry['reason']).strip()}"
        else:
            continue
        if not ruling:
            continue
        decisions.answer(data, record["id"], ruling)
        answered.append(record["id"])
    return answered


def awaiting_ruling(data: dict[str, Any], findings: list[Finding]) -> str:
    """Why the loop stopped for a person, naming what to answer and how."""
    from . import decisions

    lines = [
        f"{len(findings)} finding(s) need your ruling rather than a repair. Answer "
        "each with `writ set D-NNNN active --decision \"...\"` and run the "
        "repair again (`writ build` or `writ adjudicate`); or, to build the plan "
        "as it stands, `writ set F-NNNN accepted --reason ...`:"
    ]
    for finding in findings:
        record = decisions.asked(data, finding.id)
        lines.append(
            f"    {record['id'] if record else '?'} for {finding.id} "
            f"[{finding.where}]: {finding.message[:100]}"
        )
    return "\n".join(lines)


def _route_decisions(root: Path, findings: list[Finding]) -> None:
    """Put each `needs-decision` finding in the decision log, once."""
    from . import decisions

    with state.transaction(root) as data:
        raised = {item.get("finding") for item in data.get("decisions", [])}
        for finding in findings:
            if not finding.id or finding.id in raised:
                continue
            record = decisions.propose(
                data,
                title=finding.message[:72] or "a plan decision",
                decision=decisions.UNDECIDED,
                context=(
                    f"{finding.id} ({finding.source}, {finding.where}): "
                    f"{finding.message}"
                    + (f" Suggested: {finding.suggested_action}" if finding.suggested_action else "")
                ),
                consequences="The plan stays unapproved until this is settled.",
                proposed_by=finding.source or "critic",
                tasks=[finding.where] if finding.where else [],
            )
            record["finding"] = finding.id


def _recompute_gates(data: dict[str, Any]) -> None:
    """Point every unfinished gate at its milestone's tasks as they now stand."""
    tasks = data["tasks"]
    for gate_id, deps in _gate_edges(tasks).items():
        gate = tasks[gate_id]
        if gate.get("status") != "planned":
            continue
        gate["depends_on"] = deps
        if gate_id == gates.FINAL_GATE_ID:
            gate["requirement_ids"] = gates.answerable_requirements(data)
            continue
        milestone = gates.milestone_of(gate) or gate.get("milestone")
        requirement_ids = sorted(
            {req for dep in deps for req in tasks[dep].get("requirement_ids", [])}
        )
        gate["requirement_ids"] = requirement_ids
        if milestone in data.get("milestones", {}):
            held = {item["text"]: item for item in gate.get("acceptances", [])}
            gate["acceptances"] = [
                dict(held[text]) if text in held else {"text": text, "status": "pending"}
                for text in gates.milestone_criteria(data, milestone, requirement_ids)
            ]


# --------------------------------------------------------------------------
# requests and questions


def _raise_questions(
    data: dict[str, Any],
    questions: list[dict[str, Any]],
    request: dict[str, Any],
    *,
    status: str = "proposed",
    autonomous: bool = False,
) -> list[str]:
    """Put what the adjudicator could not settle into the decision log.

    Returns the ids of the questions left for a person. In autonomous mode a
    question is answered with its own recommendation, but only once per finding:
    a finding asked about again after it was answered is going round in a
    circle, and a person is the way out of that.
    """
    from . import decisions

    waiting: list[str] = []
    for question in questions:
        finding_id = str(question.get("finding_id") or "").strip()
        recommendation = str(question.get("recommendation") or "").strip()
        record = decisions.asked(data, finding_id) if finding_id else None
        if record is None:
            record = decisions.propose(
                data,
                title=str(question.get("question", ""))[:72]
                or "adjudication question",
                decision=(
                    "Undecided: the adjudicator could not close the finding "
                    "without a ruling."
                ),
                context=str(question.get("context", question.get("question", "")))
                + (f" Recommended: {recommendation}" if recommendation else ""),
                consequences="The plan stays unapproved until this is settled.",
                proposed_by="adjudicator",
                tasks=[],
            )
            if finding_id:
                record["finding"] = finding_id
        answerable = (
            autonomous
            and recommendation
            and finding_id
            and decisions.ruling(data, finding_id) is None
        )
        if answerable:
            decisions.answer(data, record["id"], recommendation)
        else:
            waiting.append(record["id"])
    request["status"] = status
    request["questions"] = list(questions)
    return waiting


def _reopen(root: Path, request_id: str) -> None:
    """Leave a failed round's request open so the next one continues it."""
    with state.transaction(root) as data:
        try:
            request = repair.get_request(data, request_id)
        except WritError:
            return
        request["status"] = "open"


def _open_request(root: Path) -> dict[str, Any] | None:
    return repair.plan_request(state.load(root))


def _summary(blocking: list[Finding]) -> str:
    categories = sorted({finding.category for finding in blocking if finding.category})
    listed = ", ".join(categories[:4])
    return (
        f"{len(blocking)} blocking finding(s) stand against the plan"
        + (f": {listed}" if listed else "")
    )


def _blocking_ids(data: dict[str, Any]) -> set[str]:
    return {
        finding.id
        for finding in plans.findings(data, open_only=True)
        if finding.severity == "error" and finding.id
    }
