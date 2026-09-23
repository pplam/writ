"""The pre-execution repair loop: findings get adjudicated, not just recorded.

Writ had two halves of this and not the middle. `writ check` and `writ critique`
*produce* findings, and `repair.py` *resolves* them — but only for a gate, and a
gate does not exist until the plan is running. So before execution a blocking
finding had exactly two ends: somebody dispositioned it by hand, or
`writ approve --force` swept the lot through. Neither is a repair. The plan that
executed was the plan the critics objected to, with the objection accepted.

This is the middle:

    check + critics → findings → adjudicator proposes a patch → writ validates it
    → apply → re-check → re-run the critics → repeat, bounded

Everything structural is `repair.py`'s, which is the point. A patch is validated by
the same `repair.validate`, applied by the same `repair.apply_patch`, and bounded by
the same idea of a round. What is different is only what a request is *about*: a
gate that failed, or a plan that has not run. The one operation this occasion adds
is `revise_tasks`, safe here and nowhere else, because before execution no task's
contract has been met by anybody.

Two invariants are worth stating plainly, because they are what stops the loop from
being a way to make a bad plan pass:

1. A repair may change the strategy, never the bar. `repair.validate` refuses a
   patch that drops a requirement or leaves a task with fewer criteria.
2. A finding closes when a *check* says so, not when a patch claims it. The loop
   re-runs the deterministic checks and the critics after every applied patch, and
   a finding that comes back is reopened with its history intact.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from . import agents, critics, plancheck, plans, repair, runner, state
from .plancheck import Finding
from .state import WritError, utcnow

#: where an adjudicator writes its patch, under the round's own directory
PATCH_FILENAME = "patch.json"


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


def build_prompt(
    *,
    root: Path,
    doc: Path | None,
    plan_text: str,
    patch_path: Path,
    findings: Iterable[Finding],
    requirements: Iterable[dict[str, Any]] = (),
    prior_refusals: Iterable[Finding] = (),
    accepted_entries: Iterable[str] = (),
    round_number: int = 1,
    base_revision: int = 0,
) -> str:
    """Compose the adjudicator's prompt.

    It is given the findings it must answer, the plan as committed, and — if a
    previous attempt was refused — exactly which invariants it broke. That last part
    is what makes the retry bounded rather than hopeful: an adjudicator told "you
    dropped REQ-004" writes a different patch, while one told only "refused" writes
    the same one again.
    """
    blocking = [f for f in findings if f.severity == "error"]
    advisory = [f for f in findings if f.severity == "warning"]
    accepted_entries = list(accepted_entries)
    lines = [
        "You are repairing a PLAN that has not been executed yet.",
        "",
        "Independent checks and critics have read it and objected. Your job is to "
        "propose a patch that answers those objections — not to re-plan the "
        "project, and not to argue with the plan where nothing objected to it.",
        "",
        f"Repository root: {root.resolve()}",
        f"Plan revision: {base_revision} (your patch must state this as "
        f"`base_revision`)",
        f"Adjudication round: {round_number}",
    ]
    if doc is not None:
        lines.append(f"Design document: {doc}")
    lines.extend(
        [
            "",
            f"The {len(blocking)} blocking finding(s) you must answer. Every one "
            "needs a disposition:",
        ]
    )
    lines.extend(f"  {finding.line()}" for finding in blocking)
    if advisory:
        lines.extend(
            [
                "",
                f"{len(advisory)} advisory finding(s). Fix them if the same patch "
                "can, but they do not block and you do not have to answer them:",
            ]
        )
        lines.extend(f"  {finding.line()}" for finding in advisory[:20])
    refused = list(prior_refusals)
    if refused:
        lines.extend(
            [
                "",
                "Writ REFUSED your previous patch for these reasons. A patch that "
                "breaks the same rule will be refused again:",
            ]
        )
        lines.extend(f"  {finding.line()}" for finding in refused)
        if accepted_entries:
            lines.extend(
                [
                    "",
                    "Nothing was wrong with the rest of that patch. These entries "
                    "were sound and a patch is all-or-nothing, so send them again "
                    "unchanged and fix only what is listed above:",
                ]
            )
            lines.extend(f"  {entry}" for entry in accepted_entries)
    # The inventory is not repeated here. It travels inside `plan_text`, whose
    # `requirements` array is the same rows — and this copy was truncated at 6000
    # characters, which on a plan with 180 obligations meant the adjudicator was
    # handed a JSON array cut off mid-object and a rule saying it may not drop any
    # of the ids it could no longer read. The count is what this line was for.
    inventory = list(requirements)
    if inventory:
        lines.extend(
            [
                "",
                f"The plan states {len(inventory)} requirement(s); they are in "
                "`requirements` in the plan below. A patch may not drop or invent "
                "one.",
            ]
        )
    lines.extend(
        [
            "",
            "The plan as committed:",
            "```json",
            plan_text.strip(),
            "```",
            "",
            "Write your patch as JSON to this exact path:",
            f"  {patch_path}",
            "",
            "The file must contain JSON only — no prose, no code fence.",
            "",
            "Schema:",
            repair.PATCH_SCHEMA,
            "",
            "You may also revise the tasks already in the plan:",
            repair.REVISE_SCHEMA,
            "",
            repair.PATCH_RULES,
            "",
            repair.PLAN_PATCH_RULES,
            "",
            "If you cannot write the file, print the same JSON to stdout inside a "
            "single ```json fenced block instead.",
        ]
    )
    return "\n".join(lines)


def loop(
    *,
    root: Path,
    doc: Path | None,
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
) -> Result:
    """Run the bounded adjudication loop until the plan is clean or it stops.

    The loop stops for one of four reasons, and says which: nothing blocking is
    left; the budget is spent; a finding survived its own repair; or the adjudicator
    raised a question that needs a human. It never stops because a patch was
    refused — a refusal is information for the next attempt, and
    `repair.patches_left` bounds those separately.

    The budget counts patches that *landed*, not agent runs. A refused patch changed
    nothing, so spending the plan's repair allowance on it would stop the loop over
    a plan that had never been repaired once.

    `recheck` is how the critics get re-run between rounds. It is injected rather
    than called directly because that is a command-layer concern — it spends agents,
    it prints, and it belongs to whoever asked for the loop. Without it the loop
    still re-runs the deterministic checks, which is the cheaper half of the same
    idea.
    """
    resolved = agents.resolve(agent, [], model, events=True)
    directory.mkdir(parents=True, exist_ok=True)
    result = Result()
    budget = repair.DEFAULT_MAX_REPAIR_ROUNDS if max_rounds is None else max_rounds
    opened = _blocking_ids(state.load(root))
    # Two counters, because a refusal is not a round. `attempt` numbers the agent
    # runs, so each one gets its own directory and its own place in the record;
    # `landed` counts the patches that actually changed the plan, which is what the
    # budget is about. Counting attempts against the budget made two refused patches
    # spend the whole allowance — the adjudicator was never told what was wrong a
    # second time, the critics never re-read anything, and the loop stopped saying
    # the plan had been adjudicated twice when it had not been adjudicated at all.
    attempt = 0
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
        if landed >= budget:
            result.stopped = (
                (
                    f"the plan has been repaired {landed} time(s), its budget. What "
                    "is still open needs a decision rather than another patch."
                )
                if landed
                else "no repair was allowed (--max-rounds 0), so nothing was tried."
            )
            break
        stop = repair.plan_exhausted(data, max_rounds=budget)
        if stop and attempt > 1:
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
        )
        result.rounds.append(round_)
        if on_round is not None:
            on_round(round_)
        if round_.questions:
            result.stopped = (
                f"the adjudicator raised {len(round_.questions)} question(s) it "
                "could not settle itself; they are in the decision log "
                "(writ decisions)."
            )
            break
        if round_.error:
            # Named for whose failure it was. A patch writ validated and then could
            # not apply is writ's bug, and telling someone their adjudicator failed
            # sends them to read a transcript of an agent that did nothing wrong.
            whose = (
                "the repair could not be applied"
                if round_.error.startswith("the patch could not be applied")
                else "the adjudicator failed"
            )
            result.stopped = f"{whose}: {round_.error}"
            break
        if round_.refused and not round_.progressed:
            request = _open_request(root)
            if request is not None and not repair.patches_left(request):
                result.stopped = (
                    f"writ refused {repair.refusals(request)} patches for "
                    f"{request['id']}; what is being asked of the adjudicator is "
                    "what needs to change, not the wording of its patch."
                )
                break
            continue
        if round_.progressed:
            landed += 1
            if recheck is not None:
                # The patch landed, so every critic that passed the old revision has
                # now reviewed something else. Re-running them is what closes a
                # finding on evidence rather than on the patch's word.
                recheck()
    data = state.load(root)
    still_open = _blocking_ids(data)
    result.resolved = len(opened - still_open)
    result.remaining = len(still_open)
    return result


def _one_round(
    *,
    root: Path,
    doc: Path | None,
    directory: Path,
    resolved: agents.ResolvedAgent,
    timeout: int | None,
    cwd: str | None,
    number: int,
    blocking: list[Finding],
    stream: bool,
    on_start: Callable[[int, agents.ResolvedAgent], None] | None,
) -> Round:
    """One proposal: open a request, run the agent, validate, apply or refuse."""
    directory.mkdir(parents=True, exist_ok=True)
    patch_path = directory / PATCH_FILENAME
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
        prior = _refusal_findings(request)
        sound = _refusal_accepted(request)
        plan_text = _plan_json(data, blocking)
        inventory = [dict(record) for record in plans.requirements(data).values()]
    round_ = Round(
        number=number,
        request_id=request_id,
        revision=base_revision,
        blocking_before=len(blocking),
    )
    prompt = build_prompt(
        root=root,
        doc=doc,
        plan_text=plan_text,
        patch_path=patch_path,
        findings=blocking,
        requirements=inventory,
        prior_refusals=prior,
        accepted_entries=sound,
        round_number=number,
        base_revision=base_revision,
    )
    if on_start is not None:
        on_start(number, resolved)
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
    text = _patch_text(directory, patch_path)
    if text is None:
        round_.error = f"the adjudicator wrote no patch to {patch_path}"
        _reopen(root, request_id)
        return round_
    try:
        patch = repair.load_patch(text)
    except WritError as exc:
        round_.error = f"unusable patch: {exc}"
        _reopen(root, request_id)
        return round_
    # `validate` decides what may be applied, so a patch that fails *during* apply
    # has broken an invariant validation does not cover — writ's bug, not the
    # adjudicator's. It still must not take the loop down with it: raising here
    # escaped `loop` entirely, so `writ adjudicate` died on the round, printed
    # `repair did not run`, and left the request at `planning` with no attempt on
    # its record. The plan was then stuck: unrepaired, and out of anything that
    # would try again. A round that cannot apply is a failed round, which is what
    # every other failure in this function already is.
    try:
        with state.transaction(root) as data:
            request = repair.get_request(data, request_id)
            found = repair.validate(data, patch, request)
            refused = [finding for finding in found if finding.blocking]
            if refused:
                round_.refused = refused
                request["status"] = "open"
                request.setdefault("refusals", []).append(
                    {
                        "at": utcnow(),
                        "round": number,
                        "reasons": [finding.to_dict() for finding in refused],
                        # What the refusal was *not* about. A patch is atomic, so one
                        # bad entry costs all of it — on the plan this was written
                        # for, eleven sound revisions were discarded over a single
                        # objection to a twelfth, three rounds running, because the
                        # adjudicator was told only what broke and rewrote everything
                        # each time. Naming the entries that passed makes the retry a
                        # targeted edit instead of a fresh attempt.
                        "accepted_entries": _sound_entries(patch, refused),
                    }
                )
                return round_
            if patch.empty and patch.questions:
                round_.questions = list(patch.questions)
                _raise_questions(data, patch, request)
                return round_
            round_.applied = repair.apply_patch(
                data, patch, request, actor="adjudicator"
            )
            # Deterministic checks run against the patched plan immediately. A patch
            # that closed one finding and opened another says so here, before the
            # critics are spent on it.
            plans.run_check(data, root=root.resolve())
            round_.blocking_after = sum(
                1
                for finding in plans.findings(data, open_only=True)
                if finding.severity == "error"
            )
    except WritError as exc:
        # The transaction rolled back on the way out, so no half-applied patch is
        # in the store and the round left the plan as it found it.
        round_.error = f"the patch could not be applied: {exc}"
        # Not `None`: `progressed` reads this, and a round that applied nothing is
        # a round that changed nothing.
        round_.applied = {}
        _reopen(root, request_id)
        return round_
    return round_


def _raise_questions(
    data: dict[str, Any], patch: repair.Patch, request: dict[str, Any]
) -> None:
    """Put what the adjudicator could not settle into the decision log."""
    from . import decisions

    for question in patch.questions:
        decisions.propose(
            data,
            title=str(question.get("question", ""))[:72] or "adjudication question",
            decision=(
                "Undecided: the adjudicator could not close the finding without a "
                "ruling."
            ),
            context=str(question.get("context", question.get("question", ""))),
            consequences="The plan stays unapproved until this is settled.",
            proposed_by="adjudicator",
            tasks=[],
        )
    request["status"] = "proposed"
    request["questions"] = list(patch.questions)


def _sound_entries(patch: repair.Patch, refused: Iterable[Finding]) -> list[str]:
    """The ids in a refused patch that nothing objected to.

    A refusal's `where` is `<request>.revise_tasks[3]`-shaped, so the objected-to
    entries are identifiable by index and everything else in the patch stood.
    """
    faulted: set[str] = set()
    for finding in refused:
        match = re.search(r"\.(revise_tasks|add_tasks)\[(\d+)\]", str(finding.where or ""))
        if match is not None:
            faulted.add(f"{match.group(1)}[{match.group(2)}]")
    sound: list[str] = []
    for field_name, entries in (
        ("add_tasks", patch.add_tasks),
        ("revise_tasks", patch.revise_tasks),
    ):
        for index, entry in enumerate(entries):
            if f"{field_name}[{index}]" in faulted:
                continue
            ref = str(entry.get("id", "")).strip()
            if ref:
                sound.append(f"{field_name}: {ref}")
    return sound


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


def _refusal_findings(request: dict[str, Any]) -> list[Finding]:
    """The reasons writ turned down this request's last patch."""
    refusals = request.get("refusals") or []
    if not refusals:
        return []
    return [
        Finding.from_dict(payload)
        for payload in (refusals[-1].get("reasons") or [])
    ]


def _refusal_accepted(request: dict[str, Any]) -> list[str]:
    """The entries writ did not object to in this request's last refused patch."""
    refusals = request.get("refusals") or []
    if not refusals:
        return []
    return [str(entry) for entry in (refusals[-1].get("accepted_entries") or [])]


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


def _patch_text(directory: Path, patch_path: Path) -> str | None:
    """The patch file, or JSON the adjudicator printed to stdout instead."""
    from .planning import extract_json

    if patch_path.exists():
        text = patch_path.read_text(encoding="utf-8").strip()
        if text:
            return text
    stdout = directory / "stdout.log"
    if not stdout.exists():
        return None
    text = stdout.read_text(encoding="utf-8").strip()
    if not text:
        return None
    return extract_json(text)


#: what an adjudicator needs of a requirement record.
#:
#: Not `verification`, which is the hints the requirements stage collected and by
#: far the largest field — 38KB of 57KB on the plan this was measured against — and
#: not the timestamps, which say when a row was written and nothing about the
#: obligation. A patch is judged on which ids it covers and whether it weakened a
#: `must`, so that is what is sent.
REQUIREMENT_FIELDS = (
    "id",
    "text",
    "priority",
    "status",
    "source",
    "evidence",
    "reason",
)


def _requirement_view(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: record[key]
        for key in REQUIREMENT_FIELDS
        if record.get(key) not in (None, "", [])
    }


#: how many tasks the adjudicator is shown in full before the view is narrowed.
#:
#: Below this the whole graph is cheaper to send than to explain, and a small plan
#: read whole is a better-informed patch. Above it, the full graph is mostly tasks
#: no finding mentions: on the plan this bound was written for, 58 tasks and 180
#: requirements made a 240KB prompt of which the 5 blocking findings touched 6 tasks.
FULL_VIEW_TASKS = 25


def _plan_json(
    data: dict[str, Any], findings: Iterable[Finding] = (), *, full: bool = False
) -> str:
    """The committed plan, as the adjudicator reads it.

    Statuses are included, which the critics' view does not need: an adjudicator has
    to know which tasks it may revise, and `planned` is the only answer.

    Narrowed to what the findings are about once the plan is large. An adjudicator
    is answering specific objections, not re-planning, and a patch is validated
    against the whole graph afterwards whatever it was shown — so the tasks a
    finding names, everything those depend on or that depends on them, and their
    milestones' gates are the working set. The rest is listed by id and title so
    nothing is invisible and the ids stay citable, and the requirement inventory is
    filtered to what the findings and the shown tasks actually reference.

    `full` forces the whole graph, which is what a small plan gets anyway.
    """
    tasks = data.get("tasks", {})
    requirements = plans.requirements(data)
    shown = set(tasks)
    if not full and len(tasks) > FULL_VIEW_TASKS:
        shown = _relevant_tasks(data, findings)
    detailed = [
        {
            "id": task["id"],
            "title": task.get("title", ""),
            "kind": task.get("kind", "task"),
            "status": task.get("status"),
            "milestone": task.get("milestone"),
            "notes": task.get("notes", ""),
            "design_section": task.get("design_section"),
            "requirement_ids": task.get("requirement_ids", []),
            "depends_on": task.get("depends_on", []),
            "allowed": task.get("allowed", []),
            "forbidden": task.get("forbidden", []),
            "acceptances": task.get("acceptances", []),
        }
        for task_id, task in tasks.items()
        if task_id in shown
    ]
    payload: dict[str, Any] = {
        "revision": plans.revision(data),
        "milestones": [
            {"id": m["id"], "title": m.get("title", "")}
            for m in data.get("milestones", {}).values()
        ],
        "tasks": detailed,
    }
    if len(shown) < len(tasks):
        payload["tasks_not_shown"] = [
            {
                "id": task["id"],
                "title": task.get("title", ""),
                "milestone": task.get("milestone"),
                "requirement_ids": task.get("requirement_ids", []),
            }
            for task_id, task in tasks.items()
            if task_id not in shown
        ]
        payload["note"] = (
            "`tasks` holds every task the findings touch, in full. "
            "`tasks_not_shown` is the rest of the plan by id, so you can see it "
            "exists and depend on it — ask for nothing from it and revise none of "
            "it. Writ validates your patch against the whole graph."
        )
        wanted = {
            req
            for task_id in shown
            for req in (tasks[task_id].get("requirement_ids") or [])
        }
        wanted.update(
            req for finding in findings for req in (finding.requirement_ids or [])
        )
        payload["requirements"] = [
            _requirement_view(record)
            for req_id, record in requirements.items()
            if req_id in wanted
        ]
        payload["requirements_not_shown"] = sorted(set(requirements) - wanted)
    else:
        payload["requirements"] = [
            _requirement_view(record) for record in requirements.values()
        ]
    return json.dumps(payload, indent=2, sort_keys=True)


def _relevant_tasks(
    data: dict[str, Any], findings: Iterable[Finding]
) -> set[str]:
    """The tasks a set of findings is about, plus one hop of graph around them.

    One hop in both directions, because the commonest plan repair is an ordering or
    ownership problem between a task and its neighbour, and a patch that cannot see
    the neighbour cannot fix it. Gates of the affected milestones come too: a gate's
    criteria are what the tasks under it are held to, and an adjudicator that cannot
    read them will propose work the gate does not ask for.
    """
    tasks = data.get("tasks", {})
    seeds: set[str] = set()
    for finding in findings:
        for token in re.split(r"[\s,/]+", str(finding.where or "")):
            token = token.strip().strip("().")
            if token in tasks:
                seeds.add(token)
    if not seeds:
        # No finding named a task, so fall back to what covers the requirements they
        # are about. Second choice, not first: a `must` can be covered by a dozen
        # tasks, and seeding on it pulled in most of the graph — which is how the
        # narrowing came out 212KB against 223KB and bought nothing.
        wanted_reqs = {
            req for finding in findings for req in (finding.requirement_ids or ())
        }
        for task_id, task in tasks.items():
            if set(task.get("requirement_ids") or ()) & wanted_reqs:
                seeds.add(task_id)
    if not seeds:
        # Nothing resolvable at all. Better to send the whole plan than a view
        # chosen by an empty seed set.
        return set(tasks)
    plain = {
        task_id
        for task_id, task in tasks.items()
        if task.get("kind") != "gate"
    }
    wanted = set(seeds)
    for task_id in seeds:
        wanted.update(
            dep
            for dep in (tasks[task_id].get("depends_on") or ())
            if dep in plain
        )
    # The reverse hop is restricted to ordinary tasks. Every gate depends on its
    # milestone's tasks, so following dependents blindly drew in all eleven gates
    # and the final gate, which depends on everything, drew in the whole plan.
    for task_id in plain:
        if set(tasks[task_id].get("depends_on") or ()) & seeds:
            wanted.add(task_id)
    milestones = {
        tasks[task_id].get("milestone")
        for task_id in seeds
        if tasks[task_id].get("kind") != "gate"
    } - {None}
    for task_id, task in tasks.items():
        if task.get("kind") == "gate" and task.get("milestone") in milestones:
            wanted.add(task_id)
    return wanted
