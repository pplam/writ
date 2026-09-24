"""Independent adversarial review of a plan, before any of it is executed.

Writ's own checks (`writ/plancheck.py`) are deterministic: they can prove a
requirement has no feature, that an interface has no provider, that the graph has
a cycle. What they cannot do is read. A feature can pass every structural check
and still build the wrong thing, or rest on a toolchain the repository does not
have.

That judgement needs a reader, and it must not be the author. So this runs
critics that did not write the plan.

**Two critics, each with one question.** `fidelity` asks whether the plan builds
what the design asked for and whether its contracts compose. `feasibility` asks
whether it can be built here. There used to be five, and on a real plan they
produced several hundred findings, most of them about file and test names for
code that did not exist yet; the repair agent could not ingest them and the loop
never converged (docs/planning-redesign.md §5).

**Blocking is a closed vocabulary.** A critic may only block on one of
`BLOCKING_CATEGORIES`. Anything else it believes is recorded as advisory, and
each critic is held to `MAX_BLOCKING` and `MAX_ADVISORY` findings. What reaches
the repair agent is then short enough to answer in one round.

**From the second round, critics verify.** Given their own earlier blockers and
the features that changed, they say which blockers still stand, and may raise a
new one only on a changed feature. A critic re-reading the whole plan every
round found new things to object to every round.

**They report findings, not rewrites.** A critic that edited the plan would be a
second author, and nothing would then be reviewing what it wrote. Findings go
into the same ledger as writ's own, so `writ check` and `writ approve` need no
new vocabulary to handle them.
"""
from __future__ import annotations

import hashlib
import json
import threading
from concurrent import futures
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from . import agents, planning, plans, prompts, runner, state
from .plancheck import Finding
from .planner import DesignDocs
from .state import WritError, utcnow

#: what a critic writes, and where
REPORT_FILENAME = "findings.json"
#: what a critic in verify mode is asked to re-check
VERIFY_FILENAME = "verify.json"

#: severities a critic may use, mapped onto the ledger's own.
#:
#: A critic speaks in `blocking`/`advisory` like a gate rather than in the ledger's
#: three levels, because the judgement it is making is binary: either this should
#: stop the plan or it should be on the record.
SEVERITY_MAP = {
    "blocking": "error",
    "error": "error",
    "advisory": "warning",
    "warning": "warning",
    "note": "note",
}

#: the only reasons a critic may hold a plan. Each is something a repair can act
#: on in one edit: cover it, provide it, break it, split it, change the approach,
#: or ask a person.
BLOCKING_CATEGORIES = (
    "uncovered-requirement",
    "contract-gap",
    "cycle",
    "oversized-feature",
    "infeasible-env",
    "needs-decision",
)

#: how many findings of each kind one critic's report may carry
MAX_BLOCKING = 5
MAX_ADVISORY = 5

#: names the critics had before there were two, mapped onto who asks that
#: question now, so a config that names them still resolves
RENAMED = {
    "coverage": "fidelity",
    "dependency": "fidelity",
    "scope": "fidelity",
    "acceptance": "fidelity",
}


@dataclass(frozen=True)
class Critic:
    """One reviewer, one question."""

    name: str
    #: what this critic is for, in the plan's own terms
    brief: str
    #: the specific checks it must make, one per line
    checks: tuple[str, ...]
    #: what it must not spend its attention on
    out_of_scope: str = ""
    #: whether this critic is asked to *run* things in the repository, rather than
    #: only read it. The one measuring a baseline is told the tree is shared, and
    #: the other is told to leave the suite alone so the baseline means something.
    runs_commands: bool = False

    @property
    def scope(self) -> str:
        """Its source tag in the findings ledger."""
        return f"critic:{self.name}"


CRITICS: tuple[Critic, ...] = (
    Critic(
        name="fidelity",
        brief=(
            "whether this plan builds what the design asked for, and whether its "
            "features' contracts compose into one system"
        ),
        checks=(
            "Every `must` requirement is covered by a feature whose goal and "
            "behaviours would actually deliver it, including the requirement's "
            "`details`. A feature that names a requirement and builds something "
            "adjacent does not cover it (`uncovered-requirement`).",
            "Every interface a feature consumes is provided by another feature, "
            "and what the provider says it provides is what the consumer needs "
            "(`contract-gap`). Two features that must share something with no "
            "interface between them is also a gap.",
            "No feature is several subsystems at once: one agent should be able "
            "to build it from its goal alone (`oversized-feature`).",
            "Where the design is ambiguous in a way that changes what gets built, "
            "and the plan silently picked a reading, a person must decide "
            "(`needs-decision`).",
        ),
        out_of_scope=(
            "Do not review whether the plan can run in this environment; the "
            "feasibility critic has that."
        ),
    ),
    Critic(
        name="feasibility",
        runs_commands=True,
        brief="whether this plan can be built in this repository as it actually is",
        checks=(
            "The language, toolchain, libraries and services the plan assumes are "
            "available, or the plan includes getting them (`infeasible-env`).",
            "The test command works, and say what the baseline is. A plan whose "
            "behaviours are judged against an already-failing suite is not "
            "executable as stated (`infeasible-env`).",
            "Each feature's `owns` fits how this repository is laid out and what "
            "it already has. Work the plan treats as new that already exists here "
            "is worth an advisory finding.",
        ),
        out_of_scope=(
            "Do not review requirement coverage or contracts; the fidelity critic "
            "has those."
        ),
    ),
)

SCHEMA = """\
{
  "findings": [
    {
      "severity": "blocking" | "advisory",
      "category": "one of the blocking categories, or any short label if advisory",
      "where": "FT-003, or REQ-007",
      "message": "what is wrong, specifically, in one or two sentences",
      "suggested_action": "what would close this finding",
      "requirement_ids": ["REQ-007"],
      "evidence": "what you read or ran that shows it"
    }
  ],
  "summary": "one or two sentences on the plan's state from your angle",
  "confidence": "high" | "medium" | "low"
}"""

VERIFY_SCHEMA = """\
{
  "still_open": ["F-0012"],
  "findings": [ ...new findings, same shape as below... ],
  "summary": "one or two sentences",
  "confidence": "high" | "medium" | "low"
}"""

RULES = f"""\
Rules:
1. Report findings. Do not rewrite the plan or propose a replacement for it.
2. `blocking` is only for these categories: {", ".join(BLOCKING_CATEGORIES)}.
   A blocking finding with any other category is recorded as advisory. Use
   `advisory` for everything else.
3. At most {MAX_BLOCKING} blocking and {MAX_ADVISORY} advisory findings. Report
   the ones that matter most; writ drops the rest.
4. The plan has not been executed. Files that don't exist yet are expected; don't
   judge file names or test names. A feature's `owns` is where its work goes,
   and the agent that builds it decides the files.
5. `where` must name a feature id or a requirement id.
6. Finding nothing is a legitimate result. Write an empty `findings` list and say
   in `summary` what you checked.
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
    #: in verify mode, the earlier blocking findings this critic says still stand
    still_open: list[str] = field(default_factory=list)

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
    A name from before there were two critics resolves to the one that asks its
    question now (`RENAMED`).
    """
    known = {critic.name: critic for critic in CRITICS}
    wanted: set[str] = set()
    for entry in names:
        for name in str(entry).split(","):
            name = RENAMED.get(name.strip(), name.strip())
            if not name:
                continue
            if name not in known:
                raise WritError(
                    f"unknown critic {name!r}; choose from {', '.join(known)}"
                )
            wanted.add(name)
    return [critic for critic in CRITICS if critic.name in wanted]


KNOWN_FINDINGS_FILENAME = "known-findings.json"


@dataclass(frozen=True)
class PlanFiles:
    """Where the plan under review lives on disk, for the prompt to point at."""

    index: Path
    features: Path
    #: the short repo summary, when the plan came through the staged pipeline
    inventory: Path | None = None


@dataclass
class Verify:
    """What a critic in verify mode is asked to re-check.

    `open` are its own earlier blocking findings that a silence would close, as
    the ledger holds them. `advisory` are its earlier advisory findings, carried
    forward untouched. `changed` are the features whose plan fields differ from
    what it last read, which are the only places it may raise a new blocker.
    """

    open: list[dict[str, Any]] = field(default_factory=list)
    advisory: list[dict[str, Any]] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)

    @property
    def open_ids(self) -> list[str]:
        return [str(payload.get("id")) for payload in self.open if payload.get("id")]


#: the fields that make up a feature as a critic reads it
_HASHED_FIELDS = (
    "title",
    "goal",
    "owns",
    "provides",
    "consumes",
    "requirement_ids",
    "depends_on",
    "notes",
    "allowed",
)


def feature_hashes(data: dict[str, Any]) -> dict[str, str]:
    """One short digest per planned task, over what a critic reads of it."""
    hashes: dict[str, str] = {}
    for task_id, task in sorted(data.get("tasks", {}).items()):
        view = {key: task.get(key) for key in _HASHED_FIELDS}
        view["acceptances"] = [item.get("text") for item in task.get("acceptances", [])]
        encoded = json.dumps(view, sort_keys=True, ensure_ascii=False).encode("utf-8")
        hashes[task_id] = hashlib.sha256(encoded).hexdigest()[:16]
    return hashes


def verify_context(data: dict[str, Any], critic: Critic) -> Verify | None:
    """What this critic should re-check, or None when it has never reviewed.

    A critic with no earlier successful review has nothing to verify against, so
    it reviews in full.
    """
    last = next(
        (
            entry
            for entry in reversed(reviews(data))
            if entry.get("critic") == critic.name
            and not entry.get("error")
            and isinstance(entry.get("features"), dict)
        ),
        None,
    )
    if last is None:
        return None
    before = last["features"]
    now = feature_hashes(data)
    changed = sorted(
        task_id
        for task_id in set(before) | set(now)
        if before.get(task_id) != now.get(task_id) and task_id in now
    )
    context = Verify(changed=changed)
    for payload in plans.finding_records(data):
        if payload.get("source") != critic.scope:
            continue
        if payload.get("disposition") == "resolved":
            continue
        if payload.get("severity") == "error":
            if plans.closeable(payload):
                context.open.append(payload)
        elif payload.get("severity") == "warning":
            context.advisory.append(payload)
    return context


def _write_verify(path: Path, verify: Verify) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "open": [
                    {
                        key: payload.get(key)
                        for key in (
                            "id",
                            "category",
                            "where",
                            "message",
                            "suggested_action",
                        )
                    }
                    for payload in verify.open
                ],
                "changed_features": verify.changed,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def write_known(path: Path, found: Iterable[Finding]) -> Path | None:
    """What writ already found, pre-filtered so a critic reads all of it.

    Blocking and advisory only: notes are writ talking to itself. Returns None,
    and writes nothing, when there is nothing to say.
    """
    already = [finding for finding in found if finding.severity != "note"]
    if not already:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            [
                {
                    "severity": finding.severity,
                    "category": finding.category,
                    "where": finding.where,
                    "message": finding.message,
                }
                for finding in already
            ],
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def build_prompt(
    critic: Critic,
    *,
    root: Path,
    doc: DesignDocs,
    plan: PlanFiles,
    report_path: Path,
    known_findings: Path | None = None,
    verify: Verify | None = None,
    verify_path: Path | None = None,
) -> str:
    """Compose one critic's prompt: its brief, the files to read, where to write.

    With `verify`, the prompt narrows to re-checking the critic's own earlier
    blockers and the features that changed since it last read the plan.
    """
    first: list[prompts.Ref] = [
        prompts.Ref(
            plan.index,
            "the plan's index: the fixed requirement inventory, and one row per "
            "feature with its contracts, dependencies and requirement ids",
        )
    ]
    if verify is not None and verify_path is not None:
        first.insert(
            0,
            prompts.Ref(
                verify_path,
                "YOUR earlier blocking findings (`open`) and the features changed "
                "since you last read the plan (`changed_features`)",
            ),
        )
    first.extend(prompts.design_refs(doc, "the design document the plan answers to"))
    if known_findings is not None and verify is None:
        first.append(
            prompts.Ref(
                known_findings,
                "what writ's own deterministic checks already found. They are on "
                "the record, so do not re-report them",
            )
        )
    as_needed = [
        prompts.Ref(
            plan.features,
            "one file per feature, in full: goal, contracts, behaviours, notes",
        )
    ]
    if plan.inventory is not None:
        as_needed.append(
            prompts.Ref(
                plan.inventory,
                "the short repo summary: language, test command, baseline",
            )
        )
    lines = [
        "You are reviewing a PLAN before any of it is executed. You did not write "
        "it, and you are not fixing it.",
        "",
        f"Your brief, and nothing else: {critic.brief}.",
        "",
        prompts.root_line(root),
        "",
    ]
    lines.extend(prompts.references(root, first=first, as_needed=as_needed))
    lines.append(
        "Files that don't exist yet are expected; don't judge file names or test "
        "names. File names are decided when a feature runs."
    )
    if verify is not None:
        lines.extend(
            [
                "",
                "This is a VERIFY pass. The plan was repaired after your last "
                "review. Do not review it afresh:",
                "1. For each finding in `open`, decide whether the plan as it now "
                "stands still has that defect. List the ids that still stand in "
                "`still_open`. Leave out the ones that are resolved.",
                "2. You may raise a new blocking finding only on a feature listed in "
                "`changed_features`, and only if the change introduced it. Anything "
                "else you notice is advisory.",
                "",
                "Your brief's checks, for reference:",
            ]
        )
    else:
        lines.extend(["", "Check each of these, in order:"])
    lines.extend(f"{n}. {check}" for n, check in enumerate(critic.checks, start=1))
    if critic.out_of_scope:
        lines.extend(["", critic.out_of_scope])
    if critic.runs_commands:
        lines.extend(
            [
                "",
                "Another critic is reading this repository at the same time as you. "
                "It has been told not to run the build or the test suite, so the "
                "baseline you measure is yours.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "Do not run the project's build, test or lint suite. Another critic "
                "is running it to establish the baseline, and a second run in the "
                "same working tree corrupts that measurement.",
            ]
        )
    lines.append("")
    lines.extend(prompts.output(root, "findings", report_path))
    lines.extend(
        [
            "",
            "The file must contain JSON only — no prose, no code fence.",
            "",
            "Schema:",
        ]
    )
    if verify is not None:
        lines.extend([VERIFY_SCHEMA, "", "where each finding is:"])
    lines.extend(
        [
            SCHEMA,
            "",
            RULES,
            "",
            "If you cannot write the file, print the same JSON to stdout inside a "
            "single ```json fenced block instead.",
        ]
    )
    return "\n".join(lines)


def parse(text: str, critic: Critic, *, verify: Verify | None = None) -> Report:
    """Read a critic's report into ledger findings.

    Holds the report to the rules the prompt stated, rather than trusting them:
    a blocking finding outside `BLOCKING_CATEGORIES` is recorded as advisory, and
    each kind is truncated to its cap. In verify mode, the earlier blockers the
    critic says still stand, and all of its earlier advisory findings, are
    re-emitted as the ledger holds them, so that the ones it no longer names are
    exactly the ones `plans.record_findings` closes. A new blocker on a feature
    that did not change is advisory.
    """
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
    fresh = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise WritError(f"{critic.name}: finding {index} is not an object")
        fresh.append(_finding(entry, critic, index))
    if verify is not None:
        changed = set(verify.changed)
        for finding in fresh:
            if finding.blocking and not _touches(finding, changed):
                finding.severity = "warning"
    fresh = _capped(fresh)
    still_open: list[str] = []
    carried: list[Finding] = []
    if verify is not None:
        named = raw.get("still_open") or []
        if not isinstance(named, list):
            raise WritError(f"{critic.name}: 'still_open' must be a list")
        wanted = {str(item).strip() for item in named}
        for payload in verify.open:
            if str(payload.get("id")) in wanted:
                still_open.append(str(payload["id"]))
                carried.append(_carried(payload))
        carried.extend(_carried(payload) for payload in verify.advisory)
    return Report(
        critic=critic.name,
        findings=carried + fresh,
        summary=str(raw.get("summary", "")),
        confidence=str(raw.get("confidence", "")),
        still_open=still_open,
    )


def _capped(findings: list[Finding]) -> list[Finding]:
    """At most `MAX_BLOCKING` blocking and `MAX_ADVISORY` other findings."""
    blocking = [finding for finding in findings if finding.blocking][:MAX_BLOCKING]
    rest = [finding for finding in findings if not finding.blocking][:MAX_ADVISORY]
    return blocking + rest


def _touches(finding: Finding, changed: set[str]) -> bool:
    """Whether a finding is located on one of these features."""
    tokens = {
        token.strip(" .;:()[]")
        for token in finding.where.replace(",", " ").split()
    }
    return bool(tokens & changed)


def _carried(payload: dict[str, Any]) -> Finding:
    """An earlier finding, re-emitted exactly as the ledger keys it."""
    return Finding(
        severity=str(payload.get("severity", "warning")),
        category=str(payload.get("category", "")),
        message=str(payload.get("message", "")),
        where=str(payload.get("where", "")),
        suggested_action=str(payload.get("suggested_action", "")),
        requirement_ids=list(payload.get("requirement_ids") or []),
        source=str(payload.get("source", "")),
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
            f"{critic.name}: finding {index} names no feature or requirement; "
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
    category = str(entry.get("category") or "critic").strip() or "critic"
    level = SEVERITY_MAP[severity]
    if level == "error" and category not in BLOCKING_CATEGORIES:
        # Not a reason this plan may be held for. Kept, on the record.
        level = "warning"
    return Finding(
        severity=level,
        category=category,
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

    Only one critic runs the project's commands, and the other is told not to
    (`build_prompt`), so they share a working tree safely. Order is preserved, so
    this stays a partition of `chosen` and nothing is dropped.
    """
    chosen = list(chosen)
    return [chosen] if chosen else []


def review(
    *,
    root: Path,
    doc: DesignDocs,
    plan: PlanFiles,
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
    verify: dict[str, Verify] | None = None,
) -> list[Report]:
    """Run each critic and collect its report.

    `verify` maps a critic's name to what it should re-check; a critic not in it
    reviews in full.

    Sequential by default. With `parallel`, both run at once (see `waves`).
    Streamed output is serialised a line at a time so the prefix naming each critic
    keeps meaning something.

    A critic that fails does not fail the review. One report and a named failure
    is worth more than nothing, and the failure is visible rather than silently
    reducing the review to whoever happened to succeed. Reports come back in the
    order the critics were given, whatever order they finished in, so a review
    reads the same whether or not it ran concurrently.
    """
    resolved = agents.resolve(agent, [], model, events=True)
    directory.mkdir(parents=True, exist_ok=True)
    chosen = list(chosen)
    known = write_known(directory / KNOWN_FINDINGS_FILENAME, found)
    if not parallel:
        return [
            _review_one(
                critic,
                resolved=resolved,
                root=root,
                doc=doc,
                plan=plan,
                directory=directory,
                timeout=timeout,
                cwd=cwd,
                known=known,
                verify=(verify or {}).get(critic.name),
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
                plan=plan,
                directory=directory,
                timeout=timeout,
                cwd=cwd,
                known=known,
                verify=(verify or {}).get(critic.name),
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
                    plan=plan,
                    directory=directory,
                    timeout=timeout,
                    cwd=cwd,
                    known=known,
                    verify=(verify or {}).get(critic.name),
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
                # must not lose the other report.
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
    doc: DesignDocs,
    plan: PlanFiles,
    directory: Path,
    timeout: int | None,
    cwd: str | None,
    known: Path | None,
    stream: bool,
    verify: Verify | None = None,
    on_start: Callable[[Critic, agents.ResolvedAgent], None] | None,
    on_finish: Callable[[Report], None] | None,
    on_launch: Callable[[Critic, agents.ResolvedAgent, Path], None] | None = None,
    on_close: Callable[[Report], None] | None = None,
    lock: threading.Lock | None = None,
) -> Report:
    """One critic's run, from prompt to parsed report.

    Never raises for anything the critic did: a missing agent, a timeout, a missing
    or malformed report all come back as a `Report` carrying the reason, because the
    caller is collecting another review that is still worth having.

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
    verify_path = _write_verify(where / VERIFY_FILENAME, verify) if verify else None
    prompt = build_prompt(
        critic,
        root=root,
        doc=doc,
        plan=plan,
        report_path=report_path,
        known_findings=known,
        verify=verify,
        verify_path=verify_path,
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
                parsed = parse(text, critic, verify=verify)
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
                report.still_open = parsed.still_open
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
    hashes = feature_hashes(data)
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
                # What it read, so the next pass can tell it what changed.
                "features": hashes,
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
