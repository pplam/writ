"""Independent adversarial review of a plan, before any of it is executed.

Writ's own checks (`writ/plancheck.py`) are deterministic: they can prove a
criterion names no command, that two unordered tasks own one file, that a
requirement has no task. What they cannot do is read. "Add caching to the resolver"
passes every structural check and may still be the wrong task, sized wrong, fenced
wrong, and resting on an assumption the design never made.

That judgement needs a reader, and it must not be the author. A planner asked to
review its own plan produces agreement — it already made every call it would be
checking, and the reasons it made them are the reasons it would give. So this runs
critics that did not write the plan.

**Each critic gets one question.** Not "review this plan": five specific reviews
find more than one general one, because a reviewer asked about everything grades
the plan as a whole and reports the first thing it notices, while a reviewer asked
only about dependency edges has to go and look at every edge. The narrow brief is
also what makes disagreement useful — two critics flagging the same task from
different angles is signal, and a single verdict cannot produce it.

**They report findings, not rewrites.** Same rule as a gate: a critic that edited
the plan would be a second author, and nothing would then be reviewing what it
wrote. Findings go into the same ledger as writ's own, with the same severities and
the same dispositions, so `writ check` and `writ approve` need no new vocabulary to
handle them and a blocking critic finding holds the plan exactly as a structural
one does.
"""
from __future__ import annotations

import json
import threading
from concurrent import futures
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from . import agents, plancheck, planning, plans, runner, state
from .plancheck import Finding
from .state import WritError, utcnow

#: what a critic writes, and where
REPORT_FILENAME = "findings.json"

#: severities a critic may use, mapped onto the ledger's own.
#:
#: A critic speaks in `blocking`/`advisory` like a gate rather than in the ledger's
#: three levels, because the judgement it is making is binary: either this should
#: stop the plan or it should be on the record. `note` is writ's own register for
#: "probably fine, look if you like", and a critic that had it would use it to
#: hedge.
SEVERITY_MAP = {
    "blocking": "error",
    "error": "error",
    "advisory": "warning",
    "warning": "warning",
    "note": "note",
}


@dataclass(frozen=True)
class Critic:
    """One reviewer, one question."""

    name: str
    #: what this critic is for, in the plan's own terms
    brief: str
    #: the specific checks it must make, one per line
    checks: tuple[str, ...]
    #: what it must not spend its attention on, so five critics do not all
    #: report the same thing
    out_of_scope: str = ""
    #: whether this critic is asked to *run* things in the repository, rather than
    #: only read it. It no longer decides the schedule — everything runs at once
    #: (see `waves`) — but it still decides what the prompt says: the one critic
    #: measuring a build and test baseline is told the tree is shared, and the four
    #: that are not are told to leave that suite alone so the baseline means
    #: something.
    runs_commands: bool = False
    #: whether this critic is shown each requirement's `verification` hints. Only
    #: coverage is: its second check asks whether a requirement has any way of being
    #: verified at all. On a real inventory those hints are the single largest thing
    #: in the plan view, and the other four critics are told in as many words that
    #: they belong to somebody else — so sending them is paying for attention spent
    #: on the wrong question.
    reads_verification: bool = False

    @property
    def scope(self) -> str:
        """Its source tag in the findings ledger."""
        return f"critic:{self.name}"


#: the five reviews, each narrow enough that a reader has to go and look.
#:
#: Split along the lines the failures actually fall on rather than by component:
#: coverage failures are about the design, dependency failures about the graph,
#: ownership about the fences, acceptance about the bars, feasibility about the
#: repository. A critic that owned two of these would trade one off against the
#: other, which is how "the plan is broadly fine" gets written.
CRITICS: tuple[Critic, ...] = (
    Critic(
        name="coverage",
        reads_verification=True,
        brief=(
            "whether this plan actually builds what the design asked for, or only "
            "something adjacent to it"
        ),
        checks=(
            "Every `must` requirement in the inventory is covered by at least one "
            "task. Name the requirement id.",
            "Every requirement has some way of being verified — not merely a task "
            "attached, but a criterion somewhere that would fail if it were missing.",
            "No requirement in the inventory is missing from the design document, "
            "and no obligation the document states is missing from the inventory. "
            "Read the document; do not trust the inventory to be complete.",
            "Cross-cutting requirements (logging, errors, concurrency, migration, "
            "backwards compatibility) are somebody's job, not assumed.",
            "No task exists that no requirement asked for and no infrastructure "
            "need justifies.",
        ),
        out_of_scope=(
            "Do not review dependency edges, path fences, or whether criteria are "
            "phrased checkably. Other critics have those."
        ),
    ),
    Critic(
        name="dependency",
        brief="whether the graph's shape reflects real contracts between tasks",
        checks=(
            "Missing edges: a task that would fail if another had not landed first, "
            "with no edge saying so. This is the most damaging defect in a plan, "
            "because it runs correctly until it is parallelised.",
            "Unnecessary edges: an edge that only records the order someone wrote "
            "the tasks in. Each one narrows the graph for no reason.",
            "Every edge rests on something concrete — a type, a file, a schema, a "
            "command — not on 'this feels like it comes first'. Say which.",
            "Branches that run in parallel can really coexist: they do not both "
            "need to define the same interface, migrate the same schema, or own "
            "the same file.",
            "Work that has to be brought together has a task that brings it "
            "together. Two branches that only meet in a gate have nobody "
            "integrating them.",
        ),
        out_of_scope=(
            "Do not review requirement coverage or acceptance wording. Report "
            "ownership overlap only where it makes two tasks unorderable."
        ),
    ),
    Critic(
        name="scope",
        brief="whether each task is one bounded piece of work with a real fence",
        checks=(
            "Tasks that are too broad for one agent session: more than one "
            "component, more than one decision, or a title that names the project.",
            "Tasks too small to be worth a dispatch and a review, which should be "
            "folded into the work they belong to.",
            "Tasks that cannot be implemented inside their own `allowed` paths. "
            "Read the repository: if the change needs a caller in a file the fence "
            "excludes, the fence is wrong or the task is.",
            "Two tasks owning the same file with no edge between them.",
            "Tasks that will have to touch a widely shared file, which is worth "
            "knowing before three of them do it at once.",
        ),
        out_of_scope=(
            "Do not re-report missing coverage or missing edges except where the "
            "fence is what makes them impossible."
        ),
    ),
    Critic(
        name="acceptance",
        brief="whether a second party could tell, from the criteria alone, that a task is done",
        checks=(
            "Each criterion is observable: it names a command, a path, an artifact, "
            "or a behaviour someone could go and check. 'It works' is not one.",
            "Each criterion is task-local: satisfiable by this task alone, without "
            "waiting on a sibling. A project-wide bar on a fenced task is unmeetable.",
            "The commands named would actually run in this repository, and the "
            "files named exist or are created by this task.",
            "The criteria are sufficient: a task could pass all of them and still "
            "not have done what its requirement asked. Say which requirement.",
            "Testing is real work in the criteria, not an afterthought — "
            "especially a test that fails before the change and passes after.",
        ),
        out_of_scope="Do not review the graph's shape or the requirement inventory.",
    ),
    Critic(
        name="feasibility",
        # The one critic told to run the project's own commands, which is why it
        # never shares the repository with another critic.
        runs_commands=True,
        brief="whether this plan can be executed in this repository as it actually is",
        checks=(
            "Referenced paths exist, or are clearly new files this plan creates. "
            "Check them.",
            "The build, test and lint commands the plan assumes exist and work. "
            "Run them if you can, and say what the baseline is — a plan whose "
            "criteria say 'tests pass' against an already-failing suite is not "
            "executable.",
            "The proposed approach matches how this repository already does things. "
            "A plan that introduces a second pattern for something already solved "
            "will be reviewed against the wrong conventions.",
            "Libraries, tools and services the plan assumes are available.",
            "Work the plan treats as new that already exists here.",
        ),
        out_of_scope=(
            "Do not review acceptance wording or coverage in the abstract; your "
            "question is only whether this can be done here."
        ),
    ),
)

SCHEMA = """\
{
  "findings": [
    {
      "severity": "blocking" | "advisory",
      "category": "short-kebab-case-label",
      "where": "M02-003, or REQ-007, or a path",
      "message": "what is wrong, specifically, in one or two sentences",
      "suggested_action": "what would close this finding",
      "requirement_ids": ["REQ-007"],
      "evidence": "what you read or ran that shows it"
    }
  ],
  "summary": "one or two sentences on the plan's state from your angle",
  "confidence": "high" | "medium" | "low"
}"""

RULES = """\
Rules:
1. Report findings. Do not rewrite the plan, do not propose a replacement plan,
   and do not add tasks. Something else decides what to do about what you find.
2. `blocking` means this plan should not be executed as it stands. Use it when the
   defect would produce wrong work, wasted work, or work nobody asked for. Use
   `advisory` for everything else. A long list of blocking findings is a review
   nobody can act on.
3. Read the repository and the design document. A finding derived only from the
   plan JSON is a guess, and `evidence` is where you show it is not.
4. `where` must name something real: a task id, a requirement id, or a path. A
   finding nobody can locate cannot be fixed.
5. Stay inside your brief. Another critic is reading the same plan for the things
   you have been told to leave alone, and two reports of one problem are worth
   less than two problems found.
6. Finding nothing is a legitimate result. Write an empty `findings` list and say
   in `summary` what you checked. Do not manufacture an objection to look useful.
"""


@dataclass
class Report:
    """One critic's review, as writ recorded it."""

    critic: str
    findings: list[Finding] = field(default_factory=list)
    summary: str = ""
    confidence: str = ""
    exit_code: int = 0
    path: Path | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def blocking(self) -> int:
        return sum(1 for finding in self.findings if finding.blocking)


def by_name(names: Iterable[str]) -> list[Critic]:
    """Resolve critic names, or raise naming the ones that exist.

    Takes them space- or comma-separated: the flag is `nargs="*"`, so a shell gives
    them as separate words, but `--critics coverage,scope` is what people type and
    refusing it teaches nothing. Order follows `CRITICS` rather than the argument, so
    two runs of the same set read the same way, and a name given twice runs once.
    """
    known = {critic.name: critic for critic in CRITICS}
    wanted: set[str] = set()
    for entry in names:
        for name in str(entry).split(","):
            name = name.strip()
            if not name:
                continue
            if name not in known:
                raise WritError(
                    f"unknown critic {name!r}; choose from {', '.join(known)}"
                )
            wanted.add(name)
    return [critic for critic in CRITICS if critic.name in wanted]


#: the plan as a critic reads it: either one text for all of them, or a function of
#: the critic, for a caller that tailors the view to the question being asked.
PlanView = str | Callable[[Critic], str]


def plan_for(critic: Critic, plan_text: PlanView) -> str:
    return plan_text if isinstance(plan_text, str) else plan_text(critic)


def build_prompt(
    critic: Critic,
    *,
    root: Path,
    doc: Path | None,
    plan_text: PlanView,
    report_path: Path,
    found: Iterable[Finding] = (),
) -> str:
    """Compose one critic's prompt.

    It is given writ's own findings. Not to re-report them — it is told so — but
    because they say where the plan is already known to be thin, and a critic that
    did not know would spend its attention re-deriving them.
    """
    lines = [
        f"You are reviewing a PLAN before any of it is executed. You did not write "
        f"it, and you are not fixing it.",
        "",
        f"Your brief, and nothing else: {critic.brief}.",
        "",
        f"Repository root: {root.resolve()}",
    ]
    if doc is not None:
        lines.append(f"Design document: {doc}")
    lines.extend(["", "Check each of these, in order:"])
    lines.extend(f"{n}. {check}" for n, check in enumerate(critic.checks, start=1))
    if critic.out_of_scope:
        lines.extend(["", critic.out_of_scope])
    # The critics run concurrently in one working tree, so the suite is spoken for.
    # Only one of them is measuring a baseline from it, and a second run alongside
    # that one is how a green repository comes back looking broken.
    if critic.runs_commands:
        lines.extend(
            [
                "",
                "Other critics are reading this repository at the same time as you. "
                "They have been told not to run the build or the test suite, so the "
                "baseline you measure is yours — but they are reading files while "
                "you run, so treat a timing-dependent or load-dependent failure as "
                "unproven rather than as a baseline failure.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "Do not run the project's build, test or lint suite. Another critic "
                "is running it to establish the baseline, and a second run in the "
                "same working tree corrupts that measurement. Read whatever you "
                "need — including test files, build configuration and scripts, "
                "which is how you check that a command or path is real.",
            ]
        )
    already = [finding for finding in found if finding.severity != "note"]
    if already:
        lines.extend(
            [
                "",
                "Writ's own deterministic checks already found these. They are on "
                "the record, so do not re-report them — but they tell you where the "
                "plan is thin:",
            ]
        )
        lines.extend(f"  {finding.line()}" for finding in already[:20])
    lines.extend(
        [
            "",
            "The plan under review:",
            "```json",
            plan_for(critic, plan_text).strip(),
            "```",
            "",
            "Write your findings as JSON to this exact path:",
            f"  {report_path}",
            "",
            "The file must contain JSON only — no prose, no code fence.",
            "",
            "Schema:",
            SCHEMA,
            "",
            RULES,
            "",
            "If you cannot write the file, print the same JSON to stdout inside a "
            "single ```json fenced block instead.",
        ]
    )
    return "\n".join(lines)


def parse(text: str, critic: Critic) -> Report:
    """Read a critic's report into ledger findings."""
    candidate = planning.extract_json(text) or text
    try:
        raw = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise WritError(f"{critic.name}: report is not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise WritError(f"{critic.name}: report must be a JSON object")
    entries = raw.get("findings", [])
    if not isinstance(entries, list):
        raise WritError(f"{critic.name}: 'findings' must be a list")
    findings = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise WritError(f"{critic.name}: finding {index} is not an object")
        findings.append(_finding(entry, critic, index))
    return Report(
        critic=critic.name,
        findings=findings,
        summary=str(raw.get("summary", "")),
        confidence=str(raw.get("confidence", "")),
    )


def _finding(entry: dict[str, Any], critic: Critic, index: int) -> Finding:
    severity = str(entry.get("severity", "advisory")).strip().lower()
    if severity not in SEVERITY_MAP:
        raise WritError(
            f"{critic.name}: finding {index} has severity {severity!r}; "
            f"choose from {', '.join(sorted(set(SEVERITY_MAP)))}"
        )
    message = str(entry.get("message") or entry.get("summary") or "").strip()
    if not message:
        raise WritError(f"{critic.name}: finding {index} says nothing")
    where = str(entry.get("where") or "").strip()
    if not where:
        raise WritError(
            f"{critic.name}: finding {index} names no task, requirement or path; "
            "a finding nobody can locate cannot be fixed"
        )
    evidence = str(entry.get("evidence") or "").strip()
    if evidence:
        message = f"{message} (evidence: {_shorten(evidence)})"
    requirement_ids = entry.get("requirement_ids") or []
    if not isinstance(requirement_ids, list):
        raise WritError(
            f"{critic.name}: finding {index} has a non-list 'requirement_ids'"
        )
    return Finding(
        severity=SEVERITY_MAP[severity],
        category=str(entry.get("category") or "critic").strip() or "critic",
        message=message,
        where=where,
        suggested_action=str(entry.get("suggested_action") or "").strip(),
        requirement_ids=[str(item) for item in requirement_ids],
        source=critic.scope,
    )


def _shorten(text: str, limit: int = 200) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def waves(chosen: Iterable[Critic]) -> list[list[Critic]]:
    """The critics grouped into what may run at the same time: all of them.

    This used to hold the feasibility critic back into a wave of its own, because
    it is asked to run the build and the tests and a suite sharing a working tree
    with other agents reports the interference as if it were a finding about the
    plan. Two things retired that:

    The collision being avoided needs two critics running commands, and there is
    only one. The other four read files and write their own report; a reader
    reading while a suite runs costs the suite some CPU, not its correctness.

    And `writ run --parallel N` already puts N implementing agents in one working
    tree, each told by its guardrails to run the project's full verification. The
    critics were being held to a stricter rule than the agents doing the riskier
    thing.

    What is left of the risk is a suite that flakes under concurrent load, which
    would hand the feasibility critic a baseline that is failing for reasons the
    repository is not — so the other four are told not to run it (`build_prompt`),
    and feasibility is told the tree is shared. Order is preserved, so this stays
    a partition of `chosen` and nothing is dropped.
    """
    chosen = list(chosen)
    return [chosen] if chosen else []


def review(
    *,
    root: Path,
    doc: Path | None,
    plan_text: PlanView,
    directory: Path,
    chosen: Iterable[Critic],
    agent: str,
    model: str | None,
    timeout: int | None,
    cwd: str | None,
    found: Iterable[Finding] = (),
    stream: bool = False,
    parallel: bool = False,
    on_start: Callable[[Critic, agents.ResolvedAgent], None] | None = None,
    on_finish: Callable[[Report], None] | None = None,
    on_launch: Callable[[Critic, agents.ResolvedAgent, Path], None] | None = None,
    on_close: Callable[[Report], None] | None = None,
) -> list[Report]:
    """Run each critic and collect its report.

    Sequential by default. Critics are independent, so they could run at once, but
    they are reading a repository and running its tests — the feasibility critic is
    explicitly asked to — and agents doing that concurrently interfere with each
    other in ways that show up as findings about a broken build.

    With `parallel`, all of them run at once (see `waves`). The interference writ
    was avoiding needed two critics running the test suite, and only one is asked
    to; the other four are told not to touch it, which is what makes one wave safe.
    Streamed output is serialised a line at a time so the prefix naming each critic
    keeps meaning something.

    A critic that fails does not fail the review. Four reports and a named failure
    is worth more than nothing, and the failure is visible rather than silently
    reducing the review to whoever happened to succeed. Reports come back in the
    order the critics were given, whatever order they finished in, so a review
    reads the same whether or not it ran concurrently.
    """
    resolved = agents.resolve(agent, [], model, events=True)
    directory.mkdir(parents=True, exist_ok=True)
    chosen = list(chosen)
    if not parallel:
        return [
            _review_one(
                critic,
                resolved=resolved,
                root=root,
                doc=doc,
                plan_text=plan_text,
                directory=directory,
                timeout=timeout,
                cwd=cwd,
                found=found,
                stream=stream,
                on_start=on_start,
                on_finish=on_finish,
                on_launch=on_launch,
                on_close=on_close,
            )
            for critic in chosen
        ]

    # One lock for the whole review, not one per wave: it guards the terminal,
    # which is shared by everything that prints, including the callbacks that
    # announce a critic starting and finishing.
    lock = threading.Lock()
    collected: dict[str, Report] = {}
    for wave in waves(chosen):
        if len(wave) == 1:
            critic = wave[0]
            collected[critic.name] = _review_one(
                critic,
                resolved=resolved,
                root=root,
                doc=doc,
                plan_text=plan_text,
                directory=directory,
                timeout=timeout,
                cwd=cwd,
                found=found,
                stream=stream,
                on_start=on_start,
                on_finish=on_finish,
                on_launch=on_launch,
                on_close=on_close,
            )
            continue
        with futures.ThreadPoolExecutor(max_workers=len(wave)) as pool:
            submitted = {
                pool.submit(
                    _review_one,
                    critic,
                    resolved=resolved,
                    root=root,
                    doc=doc,
                    plan_text=plan_text,
                    directory=directory,
                    timeout=timeout,
                    cwd=cwd,
                    found=found,
                    stream=stream,
                    on_start=on_start,
                    on_finish=on_finish,
                    on_launch=on_launch,
                    on_close=on_close,
                    lock=lock,
                ): critic
                for critic in wave
            }
            for future in futures.as_completed(submitted):
                critic = submitted[future]
                # `_review_one` turns a critic's own failure into a report, so an
                # exception here is writ's bug rather than the critic's. It still
                # must not lose the other four reports.
                try:
                    collected[critic.name] = future.result()
                except Exception as exc:  # pragma: no cover - defensive
                    collected[critic.name] = Report(
                        critic=critic.name,
                        path=directory / critic.name / REPORT_FILENAME,
                        error=f"review did not run: {exc}",
                    )
    return [collected[critic.name] for critic in chosen]


def _review_one(
    critic: Critic,
    *,
    resolved: agents.ResolvedAgent,
    root: Path,
    doc: Path | None,
    plan_text: PlanView,
    directory: Path,
    timeout: int | None,
    cwd: str | None,
    found: Iterable[Finding],
    stream: bool,
    on_start: Callable[[Critic, agents.ResolvedAgent], None] | None,
    on_finish: Callable[[Report], None] | None,
    on_launch: Callable[[Critic, agents.ResolvedAgent, Path], None] | None = None,
    on_close: Callable[[Report], None] | None = None,
    lock: threading.Lock | None = None,
) -> Report:
    """One critic's run, from prompt to parsed report.

    Never raises for anything the critic did: a missing agent, a timeout, a missing
    or malformed report all come back as a `Report` carrying the reason, because the
    caller is collecting four other reviews that are still worth having.

    `lock`, when the critic is one of several running at once, serialises both the
    mirrored agent output and the start/finish announcements, so two critics cannot
    interleave halfway through a line.

    `on_launch` and `on_close` are the same two moments for a caller that records
    rather than prints, and they are deliberately outside that lock: a state write
    takes a file lock of its own and fsyncs twice, so holding the terminal across it
    would stall another critic's output on something it has no stake in — and a
    contended state lock raises, which inside `mirror_lock` would take a critic down
    with it.
    """
    where = directory / critic.name
    where.mkdir(parents=True, exist_ok=True)
    report_path = where / REPORT_FILENAME
    prompt = build_prompt(
        critic,
        root=root,
        doc=doc,
        plan_text=plan_text,
        report_path=report_path,
        found=found,
    )
    if on_launch is not None:
        on_launch(critic, resolved, where)
    if on_start is not None:
        with lock if lock is not None else nullcontext():
            on_start(critic, resolved)
    report = Report(critic=critic.name, path=report_path)
    try:
        report.exit_code = runner.run_agent(
            resolved.command,
            prompt,
            where,
            cwd or root,
            timeout,
            stream=stream,
            prefix=f"  {critic.name} | " if stream else "",
            event_shape=resolved.event_shape,
            mirror_lock=lock,
        )
    except FileNotFoundError:
        report.error = f"critic agent not found: {resolved.command[0]}"
    except WritError as exc:
        report.error = str(exc)
    if not report.error:
        missing = (
            f"wrote no report to {report_path} "
            f"(exit {report.exit_code}; see {where})"
        )
        written = _report_text(where, report_path)
        if written is None:
            report.error = missing
        else:
            text, from_file = written
            try:
                parsed = parse(text, critic)
            except WritError as exc:
                # Unreadable stdout is not a malformed report. The file is the
                # contract and stdout only a courtesy for a critic that prints
                # its JSON instead of writing it, so prose on stdout means the
                # critic wrote no report -- calling that prose invalid JSON
                # blames the format of something that was never a report.
                report.error = str(exc) if from_file else missing
            else:
                report.findings = parsed.findings
                report.summary = parsed.summary
                report.confidence = parsed.confidence
    if on_finish is not None:
        with lock if lock is not None else nullcontext():
            on_finish(report)
    if on_close is not None:
        on_close(report)
    return report


def _report_text(directory: Path, report_path: Path) -> tuple[str, bool] | None:
    """The report, and whether it came from the file writ asked for.

    The caller needs the second half to report a failure honestly: a bad report
    file is a critic that reported badly, while unparseable stdout is a critic
    that did not report at all.
    """
    if report_path.exists():
        text = report_path.read_text(encoding="utf-8").strip()
        if text:
            return text, True
    stdout = directory / "stdout.log"
    if stdout.exists():
        text = stdout.read_text(encoding="utf-8").strip()
        if text:
            return text, False
    return None


def record(
    data: dict[str, Any], reports: Iterable[Report], *, root: Path | None = None
) -> list[Finding]:
    """Write every critic's findings to the plan's ledger and re-check.

    One ledger, deliberately. A critic finding and a structural one are the same
    kind of thing to everyone downstream — they hold the plan the same way, they
    are dispositioned the same way, and `writ approve` should not need to know
    which kind it is overruling.
    """
    written: list[Finding] = []
    for report in reports:
        if not report.ok:
            continue
        written.extend(
            plans.record_findings(
                data,
                report.findings,
                scope=f"critic:{report.critic}",
                # This critic is the reporter, so this is the one pass that may
                # close its own earlier findings. A critic that read the patched
                # plan and no longer objects has produced the evidence; nobody
                # else's re-check can speak for it.
                reporter=f"critic:{report.critic}",
            )
        )
        reviews(data).append(
            {
                "critic": report.critic,
                "at": utcnow(),
                "revision": plans.revision(data),
                "summary": report.summary,
                "confidence": report.confidence,
                "findings": [finding.id for finding in report.findings if finding.id],
                "blocking": report.blocking,
            }
        )
    for report in reports:
        if not report.ok:
            reviews(data).append(
                {
                    "critic": report.critic,
                    "at": utcnow(),
                    "revision": plans.revision(data),
                    "error": report.error,
                    "findings": [],
                    "blocking": 0,
                }
            )
    plans.run_check(data, root=root)
    return written


def reviews(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Every critic review this plan has had."""
    record = data.get("reviews")
    if not isinstance(record, list):
        record = []
        data["reviews"] = record
    return record


def unreviewed(data: dict[str, Any]) -> list[str]:
    """Critics that have not read the plan at its current revision.

    The revision matters: a critic that passed the plan two repairs ago reviewed
    something else. This is what lets `writ check` say the review is stale rather
    than reporting it as done.
    """
    revision = plans.revision(data)
    seen = {
        entry["critic"]
        for entry in reviews(data)
        if entry.get("revision") == revision and not entry.get("error")
    }
    return [critic.name for critic in CRITICS if critic.name not in seen]
