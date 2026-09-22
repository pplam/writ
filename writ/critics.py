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


def build_prompt(
    critic: Critic,
    *,
    root: Path,
    doc: Path | None,
    plan_text: str,
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
            plan_text.strip(),
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


def review(
    *,
    root: Path,
    doc: Path | None,
    plan_text: str,
    directory: Path,
    chosen: Iterable[Critic],
    agent: str,
    model: str | None,
    timeout: int | None,
    cwd: str | None,
    found: Iterable[Finding] = (),
    stream: bool = False,
    on_start: Callable[[Critic, agents.ResolvedAgent], None] | None = None,
    on_finish: Callable[[Report], None] | None = None,
) -> list[Report]:
    """Run each critic and collect its report.

    Sequential. Critics are independent, so they could run at once, but they are
    reading a repository and running its tests — the feasibility critic is
    explicitly asked to — and five agents doing that concurrently interfere with
    each other in ways that show up as findings about a broken build.

    A critic that fails does not fail the review. Four reports and a named failure
    is worth more than nothing, and the failure is visible rather than silently
    reducing the review to whoever happened to succeed.
    """
    resolved = agents.resolve(agent, [], model, events=True)
    directory.mkdir(parents=True, exist_ok=True)
    reports: list[Report] = []
    for critic in chosen:
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
        if on_start is not None:
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
        reports.append(report)
        if on_finish is not None:
            on_finish(report)
    return reports


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
