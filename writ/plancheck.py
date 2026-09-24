"""Deterministic plan review: what Writ can prove about a plan by itself.

A plan can be perfectly well-formed and still be a bad plan. `planning.load_plan`
checks that the JSON has the fields it needs and `model.check_dag` checks that the
graph is legal, and neither of them can tell you that a task's only acceptance
criterion is "it works", that two tasks meant to run side by side both own
`writ/state.py`, or that a requirement the design states has no task at all.

Those are the failures that survive validation and surface hours later as an agent
stuck on a bar it cannot meet. Judging a plan is mostly a judgement call — that is
what the critics in `critics.py` are for — but a useful share of it is mechanical,
and anything mechanical should be settled before tokens are spent on it. This
module is that share: it reads a plan, or the committed graph, and returns
`Finding` records.

Findings are not exceptions. A plan with errors is still written down, because a
human reading the whole plan next to the objections is in a far better position
than one reading a single raised error with no plan attached. What errors do block
is *execution*: they hold the plan at `needs-approval` (see `plans.py`), so the
gate is explicit and a human can overrule it on the record.

Two entry points, one check set: `from_plan` reads a plan document before its ids
exist, `from_state` reads the committed graph. Same checks either way, so
`writ plan --dry-run` and `writ check` cannot disagree.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

#: severity ordering, worst first. Only `error` blocks approval.
SEVERITIES = ("error", "warning", "note")

#: acceptance wording that is never checkable, whatever the task is. Each entry
#: is matched as a substring of the lowercased criterion.
VAGUE_PHRASES = (
    "works correctly",
    "works as expected",
    "works as intended",
    "it works",
    "code is clean",
    "clean code",
    "fully implemented",
    "implementation is complete",
    "implemented correctly",
    "all requirements met",
    "all requirements are met",
    "meets all requirements",
    "the complete product works",
    "no bugs",
    "bug free",
    "bug-free",
    "production ready",
    "production-ready",
    "properly implemented",
    "is correct",
    "looks good",
    "makes sense",
    "well tested",
    "well-tested",
    "good test coverage",
    "as appropriate",
    "where appropriate",
    "if necessary",
)

#: bars that are only unmeetable when the task is fenced. A task allowed to touch
#: the whole repository may legitimately be asked to leave the suite green; one
#: fenced to `parser/` cannot be, because a sibling's half-finished module fails
#: it for reasons this agent may not touch.
SUITE_WIDE_PHRASES = (
    "whole suite",
    "all tests pass",
    "all the tests pass",
    "entire test suite",
    "full test suite",
    "every test passes",
    "whole test suite",
    "complete test suite",
    "all existing tests",
)

#: titles that describe the project rather than one bounded session
WHOLE_PROJECT_TITLES = (
    "implement the design",
    "implement the entire",
    "implement everything",
    "implement the whole",
    "build the entire",
    "build the whole",
    "build everything",
    "complete the design",
    "the entire feature",
    "the whole feature",
    "do the rest",
    "finish the project",
)

#: tokens that make a criterion something a second party can re-run or re-read
COMMAND_HINTS = (
    "pytest",
    "npm ",
    "npx ",
    "yarn ",
    "make ",
    "go test",
    "go build",
    "go vet",
    "cargo ",
    "mvn ",
    "gradle",
    "tox",
    "ruff",
    "mypy",
    "eslint",
    "tsc",
    "writ ",
    "curl ",
    "docker ",
    "python -m",
    "bash ",
    "./",
    "$ ",
)

#: verbs that name an observable outcome rather than an internal state of mind
OBSERVABLE_HINTS = (
    "returns",
    "rejects",
    "accepts",
    "prints",
    "exits",
    "fails",
    "raises",
    "logs",
    "persists",
    "appends",
    "writes",
    "reads",
    "matches",
    "produces",
    "responds",
    "emits",
    "renders",
    "contains",
    "reports",
    "refuses",
    "round-trips",
    "round trips",
    "is rejected",
    "is recorded",
    "is written",
    "replays",
    "surfaces",
    "shows",
    "lists",
    "validates",
    "blocks",
    "holds",
    # A criterion that names a test and says what it does is the *most* checkable
    # shape there is, and the list missed it: `fails` was here and `passes` was not,
    # so "test_x passes: no row is written" was reported as naming no observable
    # behaviour. On a real plan that one omission produced 81 of 294 findings.
    "passes",
    "passing",
    "succeeds",
    "asserts",
    "no row",
    "no record",
    "exists",
    "stored",
    "recorded",
    "returned",
    "created",
    "updated",
    "deleted",
    "set to",
    "equal to",
    "non-empty",
    "must match",
    "conforms",
)

#: a test function or node id, which names something a second party can run.
#:
#: `test_foo`, `TestFoo`, `it("...")`, `describe(`, and a pytest node id. A
#: criterion naming one of these is checkable by construction — you run it — and
#: treating it as unobservable because it lacks a verb from a fixed list is the
#: heuristic failing on its best input.
TEST_NAME_PATTERN = re.compile(
    r"\b(?:test_\w+|\w+_test\b|Test[A-Z]\w+|it\(|describe\(|::\w+)"
)

#: file extensions that make a path token recognisable as a path
PATH_PATTERN = re.compile(
    r"[\w./-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|kt|rb|sql|md|json|yaml|yml|toml|sh|c|h|cpp|cs)\b"
)
BACKTICK_PATTERN = re.compile(r"`[^`]+`")

#: how many acceptance criteria a generated task should state
#: the criteria writ writes itself when a design section states none.
#:
#: Checked against so the per-criterion checks do not fault the plan's author for
#: words writ supplied. They are unobservable by construction — they have to be,
#: to fit any task — and twelve warnings about writ's own boilerplate bury the
#: one finding that matters, which is that this task has no bar of its own.
GENERIC_ACCEPTANCES = (
    "behavior specified by this section is implemented",
    "a failing test preceded the implementation and now passes",
    "project build tests and lint pass",
    "agent reported assumptions deviations and remaining risks",
)

MIN_ACCEPTANCES = 2
MAX_ACCEPTANCES = 6

#: `allowed` entries that fence a task to nothing in particular
BROAD_PATHS = ("", ".", "./", "/", "*", "**", "all", "everything", "repo", "root")

REQUIREMENT_PRIORITIES = ("must", "should", "may")
REQUIREMENT_STATUSES = ("planned", "existing", "out-of-scope", "deferred")


# --------------------------------------------------------------------------
# what a check reads and what it returns


@dataclass
class Finding:
    """One objection to a plan, from Writ's own checks or from a critic.

    Findings travel: the same record is produced here, produced by a critic agent,
    persisted on the plan, answered by a repair, and printed by `writ check`. So it
    carries where it came from (`source`) and what would close it
    (`suggested_action`) rather than only a message, and `id` is assigned once it is
    written down so a disposition can name it.
    """

    severity: str
    category: str
    message: str
    where: str = ""
    suggested_action: str = ""
    requirement_ids: list[str] = field(default_factory=list)
    source: str = "writ"
    id: str = ""

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"unknown severity: {self.severity}")

    @property
    def blocking(self) -> bool:
        return self.severity == "error"

    def line(self) -> str:
        """One line for a terminal: severity, where, message."""
        head = f"{self.severity}"
        if self.id:
            head = f"{self.id} {head}"
        if self.where:
            head = f"{head} [{self.where}]"
        return f"{head}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "severity": self.severity,
            "category": self.category,
            "message": self.message,
            "where": self.where,
            "suggested_action": self.suggested_action,
            "requirement_ids": list(self.requirement_ids),
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Finding":
        return cls(
            severity=str(payload.get("severity", "warning")),
            category=str(payload.get("category", "unspecified")),
            message=str(payload.get("message", "")),
            where=str(payload.get("where", "")),
            suggested_action=str(payload.get("suggested_action", "")),
            requirement_ids=list(payload.get("requirement_ids", [])),
            source=str(payload.get("source", "writ")),
            id=str(payload.get("id", "")),
        )


@dataclass
class Item:
    """A task as the checks see it, whether or not Writ has named it yet."""

    id: str
    title: str
    acceptances: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    allowed: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    requirement_ids: list[str] = field(default_factory=list)
    kind: str = "task"
    milestone: str = ""
    status: str = "planned"

    @property
    def fenced(self) -> bool:
        return bool(self.allowed)


@dataclass
class Requirement:
    """One thing the design asks for, as the plan claims to have understood it."""

    id: str
    text: str
    priority: str = "must"
    status: str = "planned"
    source: str = ""
    evidence: str = ""
    reason: str = ""
    verification: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "priority": self.priority,
            "status": self.status,
            "source": self.source,
            "evidence": self.evidence,
            "reason": self.reason,
            "verification": list(self.verification),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Requirement":
        return cls(
            id=str(payload.get("id", "")),
            text=str(payload.get("text", "")),
            priority=str(payload.get("priority", "must")),
            status=str(payload.get("status", "planned")),
            source=str(payload.get("source", "")),
            evidence=str(payload.get("evidence", "")),
            reason=str(payload.get("reason", "")),
            verification=list(payload.get("verification", [])),
        )


@dataclass
class Snapshot:
    """Everything the deterministic checks need, from either side of commit."""

    items: list[Item] = field(default_factory=list)
    requirements: list[Requirement] = field(default_factory=list)
    root: Path | None = None
    #: True once ids are Writ's own, which changes only how findings read
    committed: bool = False

    @property
    def tasks(self) -> list[Item]:
        return [item for item in self.items if item.kind == "task"]

    @property
    def gates(self) -> list[Item]:
        return [item for item in self.items if item.kind == "gate"]

    @property
    def populated(self) -> bool:
        """Whether this repository has a source tree for a fence to be wrong about.

        A greenfield project is planned entirely out of files that do not exist, so
        "this path does not exist" is true of every fence in the plan and evidence
        of nothing. Reporting it there produced a note per fenced file and buried
        the findings that mattered underneath them.

        Only a *source* tree counts. A repository holding nothing but the design
        documents the plan is derived from is greenfield in every way that matters
        here, and `docs/` is not a fence being right about anything.
        """
        if self.root is None or not self.root.is_dir():
            return False
        ignore = {
            ".git",
            ".writ",
            ".venv",
            "node_modules",
            "__pycache__",
            "docs",
            "doc",
            "design",
            "designs",
            "spec",
            "specs",
        }
        for entry in self.root.iterdir():
            if entry.name in ignore or entry.name.startswith("."):
                continue
            if entry.is_dir():
                return True
        return False

# --------------------------------------------------------------------------
# adapters


def from_plan(
    milestones: Iterable[Any],
    requirements: Iterable[Requirement] = (),
    *,
    root: Path | None = None,
) -> Snapshot:
    """A snapshot of a plan document, keyed by the ids its author invented.

    Tasks the author left unnamed get a positional stand-in, so a finding can
    still point at one of them in a `--dry-run` where no real id exists yet.
    """
    items: list[Item] = []
    for position, milestone in enumerate(milestones, start=1):
        for index, task in enumerate(milestone.tasks, start=1):
            items.append(
                Item(
                    id=task.ref or f"{milestone.ref or f'M{position:02d}'}#{index}",
                    title=task.title,
                    acceptances=list(task.acceptances),
                    depends_on=list(task.depends_on),
                    allowed=list(task.allowed),
                    forbidden=list(task.forbidden),
                    requirement_ids=list(getattr(task, "requirement_ids", []) or []),
                    milestone=milestone.title,
                )
            )
    return Snapshot(
        items=items, requirements=list(requirements), root=root, committed=False
    )


def from_state(data: dict[str, Any], *, root: Path | None = None) -> Snapshot:
    """A snapshot of the committed graph."""
    items = [
        Item(
            id=task["id"],
            title=task.get("title", ""),
            acceptances=[item["text"] for item in task.get("acceptances", [])],
            depends_on=list(task.get("depends_on", [])),
            allowed=list(task.get("allowed", [])),
            forbidden=list(task.get("forbidden", [])),
            requirement_ids=list(task.get("requirement_ids", [])),
            kind=task.get("kind", "task"),
            milestone=task.get("milestone") or "",
            status=task.get("status", "planned"),
        )
        for task in sorted(data.get("tasks", {}).values(), key=lambda t: t["id"])
    ]
    requirements = [
        Requirement.from_dict(payload)
        for payload in sorted(
            data.get("requirements", {}).values(), key=lambda r: r.get("id", "")
        )
    ]
    return Snapshot(items=items, requirements=requirements, root=root, committed=True)


# --------------------------------------------------------------------------
# the checks


def check(snapshot: Snapshot) -> list[Finding]:
    """Every deterministic objection to this plan, worst first."""
    findings: list[Finding] = []
    findings.extend(check_titles(snapshot))
    findings.extend(check_acceptances(snapshot))
    findings.extend(check_fences(snapshot))
    findings.extend(check_dependencies(snapshot))
    findings.extend(check_coverage(snapshot))
    findings.extend(check_integration(snapshot))
    return sort_findings(findings)


def sort_findings(findings: Iterable[Finding]) -> list[Finding]:
    """Worst first, then by category and location, so output is stable."""
    order = {severity: index for index, severity in enumerate(SEVERITIES)}
    return sorted(
        findings,
        key=lambda f: (order.get(f.severity, len(SEVERITIES)), f.category, f.where, f.message),
    )


def blocking(findings: Iterable[Finding]) -> list[Finding]:
    return [finding for finding in findings if finding.blocking]


def tally(findings: Iterable[Finding]) -> dict[str, int]:
    counts = {severity: 0 for severity in SEVERITIES}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    counts["total"] = sum(counts[severity] for severity in SEVERITIES)
    return counts


def check_titles(snapshot: Snapshot) -> list[Finding]:
    """A task named after the whole project is not one bounded session."""
    findings: list[Finding] = []
    seen: dict[str, str] = {}
    for item in snapshot.tasks:
        lowered = item.title.lower()
        for phrase in WHOLE_PROJECT_TITLES:
            if phrase in lowered:
                findings.append(
                    Finding(
                        severity="error",
                        category="task-too-broad",
                        message=(
                            f"{item.title!r} describes the whole project, not one "
                            "bounded agent session"
                        ),
                        where=item.id,
                        suggested_action=(
                            "split it into tasks that each own a component and "
                            "state their own bar"
                        ),
                    )
                )
                break
        key = lowered.strip()
        if key in seen:
            findings.append(
                Finding(
                    severity="warning",
                    category="duplicate-task",
                    message=f"same title as {seen[key]}: {item.title!r}",
                    where=item.id,
                    suggested_action="say what differs, or merge them",
                )
            )
        else:
            seen[key] = item.id
    return findings


def check_acceptances(snapshot: Snapshot) -> list[Finding]:
    """The bar has to be checkable, task-local, and actually stated."""
    findings: list[Finding] = []
    for item in snapshot.tasks:
        count = len(item.acceptances)
        if count < MIN_ACCEPTANCES:
            findings.append(
                Finding(
                    severity="warning",
                    category="thin-acceptance",
                    message=(
                        f"states {count} acceptance criterion; "
                        f"{MIN_ACCEPTANCES}-{MAX_ACCEPTANCES} is the bar for a "
                        "task a reviewer has to judge"
                    ),
                    where=item.id,
                    suggested_action="add the verification the task owns",
                )
            )
        elif count > MAX_ACCEPTANCES:
            findings.append(
                Finding(
                    severity="warning",
                    category="wide-acceptance",
                    message=(
                        f"states {count} acceptance criteria, more than "
                        f"{MAX_ACCEPTANCES}; a task with this many bars is "
                        "usually two tasks"
                    ),
                    where=item.id,
                    suggested_action="split the task, or fold related bars together",
                )
            )
        seen: dict[str, int] = {}
        for number, text in enumerate(item.acceptances, start=1):
            key = _normalize_criterion(text)
            if key in seen:
                findings.append(
                    Finding(
                        severity="error",
                        category="duplicate-acceptance",
                        message=(
                            f"criterion {number} repeats criterion {seen[key]}: {text!r}"
                        ),
                        where=item.id,
                        suggested_action="drop one, or state what is different about it",
                    )
                )
            else:
                seen[key] = number
            if key in GENERIC_ACCEPTANCES:
                continue
            findings.extend(_criterion_findings(item, number, text))
        if item.acceptances and all(
            _normalize_criterion(text) in GENERIC_ACCEPTANCES
            for text in item.acceptances
        ):
            findings.append(
                Finding(
                    severity="warning",
                    category="generic-acceptance",
                    message=(
                        "every acceptance criterion is writ's fallback; the design "
                        "section states no bar of its own"
                    ),
                    where=item.id,
                    suggested_action=(
                        "name what this task specifically has to demonstrate, or "
                        "say in the design what would count as done"
                    ),
                )
            )
    return findings


def _criterion_findings(item: Item, number: int, text: str) -> list[Finding]:
    findings: list[Finding] = []
    lowered = text.lower()
    for phrase in VAGUE_PHRASES:
        if phrase in lowered:
            findings.append(
                Finding(
                    severity="error",
                    category="vague-acceptance",
                    message=(
                        f"criterion {number} is not checkable ({phrase!r}): {text!r}"
                    ),
                    where=item.id,
                    suggested_action=(
                        "name the command, the observable behaviour, or the "
                        "artifact that demonstrates it"
                    ),
                )
            )
            return findings
    if item.fenced:
        for phrase in SUITE_WIDE_PHRASES:
            if phrase in lowered:
                findings.append(
                    Finding(
                        severity="error",
                        category="unmeetable-acceptance",
                        message=(
                            f"criterion {number} sets a project-wide bar on a task "
                            f"fenced to {', '.join(item.allowed)}: {text!r}"
                        ),
                        where=item.id,
                        suggested_action=(
                            "scope it to the tests this task owns, or move it to a "
                            "milestone gate"
                        ),
                    )
                )
                return findings
    if not _observable(text):
        findings.append(
            Finding(
                severity="warning",
                category="unobservable-acceptance",
                message=(
                    f"criterion {number} names no command, path, or observable "
                    f"behaviour: {text!r}"
                ),
                where=item.id,
                suggested_action=(
                    "say what a second party would run or read to confirm it"
                ),
            )
        )
    return findings


def _normalize_criterion(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _observable(text: str) -> bool:
    """Whether a criterion names something a second party could go and check.

    Deliberately generous. This gates a *warning*, and the cost of the two errors
    is not symmetric: a missed vague criterion is one finding a critic will also
    read the plan for, while a false positive on a well-written criterion is noise
    that buries every real finding beside it. Judging whether a criterion truly
    demonstrates its requirement is the acceptance critic's job, not a word list's.
    """
    lowered = text.lower()
    if BACKTICK_PATTERN.search(text) or PATH_PATTERN.search(text):
        return True
    if TEST_NAME_PATTERN.search(text):
        return True
    if any(hint in lowered for hint in COMMAND_HINTS):
        return True
    if _has_measurable_shape(text):
        return True
    return any(hint in lowered for hint in OBSERVABLE_HINTS)


def _has_measurable_shape(text: str) -> bool:
    """A regex, a number with a unit, a comparison, or a quoted literal.

    "Timestamps match ^\\d{4}-..." states exactly what a reader checks, and names
    none of the verbs in `OBSERVABLE_HINTS`. So does "within 200ms" and
    "status is 'rejected'".
    """
    if re.search(r"[\^$]|\\d\{|\[\^|\.\*|\+\?", text):
        return True
    if re.search(r"\b\d+\s?(?:ms|s|kb|mb|gb|rows?|items?|%)\b", text, re.I):
        return True
    if re.search(r"[<>=]=?\s*\d|\bat (?:most|least)\b|\bno more than\b", text, re.I):
        return True
    return bool(re.search(r"'[^']{2,}'|\"[^\"]{2,}\"", text))


def check_fences(snapshot: Snapshot) -> list[Finding]:
    """Ownership has to be real, self-consistent, and not shared with a sibling."""
    findings: list[Finding] = []
    #: task id -> the fenced paths that do not exist, reported together below
    missing: dict[str, list[str]] = {}
    for item in snapshot.items:
        for path in item.allowed:
            if _is_broad(path):
                findings.append(
                    Finding(
                        severity="error",
                        category="broad-fence",
                        message=(
                            f"allowed path {path!r} fences the task to the whole "
                            "repository, which is not a fence"
                        ),
                        where=item.id,
                        suggested_action=(
                            "name the components this task owns, or omit `allowed` "
                            "and say in notes why it is global"
                        ),
                    )
                )
            elif snapshot.root is not None and not _path_exists(snapshot.root, path):
                missing.setdefault(item.id, []).append(path)
        findings.extend(_contradictory_fence(item))
    findings.extend(_unknown_paths(snapshot, missing))
    findings.extend(_shared_ownership(snapshot))
    return findings


def _unknown_paths(
    snapshot: Snapshot, missing: dict[str, list[str]]
) -> list[Finding]:
    """One finding per task for fences naming paths that do not exist yet.

    A note per path was the single largest source of noise writ produced: on a
    greenfield plan every file is new, so it fired 104 times on one plan and 221
    of 294 findings were writ's own checks rather than anything a critic said. The
    signal is not "this file does not exist" — for a plan that builds the project,
    that is the normal case and says nothing. It is "this looks like a *typo*",
    which only shows up in the shape of the path, so that is what is reported.

    Whether the project is greenfield decides the severity of the rest. A path
    under a directory that exists is worth a note, because its siblings are there
    and this one is not. A path in a repository that has no source tree at all is
    not worth reporting: nothing exists, so nothing is evidence.
    """
    findings: list[Finding] = []
    for item_id, paths in missing.items():
        suspect = [path for path in paths if _looks_like_typo(snapshot.root, path)]
        if suspect:
            findings.append(
                Finding(
                    severity="warning",
                    category="suspect-path",
                    message=(
                        f"fence names {_count(len(suspect), 'path')} whose parent "
                        f"directory exists but which do not: "
                        f"{', '.join(sorted(suspect)[:6])}"
                        + (" …" if len(suspect) > 6 else "")
                        + " — a new file here is fine, a misspelt one is not"
                    ),
                    where=item_id,
                    suggested_action=(
                        "check the spelling against the directory, or confirm the "
                        "task creates these"
                    ),
                )
            )
        rest = [path for path in paths if path not in set(suspect)]
        if rest and snapshot.populated:
            findings.append(
                Finding(
                    severity="note",
                    category="unknown-path",
                    message=(
                        f"fence names {_count(len(rest), 'path')} that do not exist "
                        f"yet: {', '.join(sorted(rest)[:6])}"
                        + (" …" if len(rest) > 6 else "")
                    ),
                    where=item_id,
                    suggested_action="confirm the task creates them",
                )
            )
    return findings


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _looks_like_typo(root: Path | None, path: str) -> bool:
    """Whether a missing path sits in a directory that already exists.

    That is the only cheap evidence of a misspelling. `writ/stat.py` next to a real
    `writ/` is worth a look; `mmm/schema.sql` in a repository with no `mmm/` is just
    a file the plan is about to create.
    """
    if root is None:
        return False
    cleaned = _normalize_path(path).strip("/")
    if not cleaned or "/" not in cleaned:
        return False
    parent = root / cleaned.rsplit("/", 1)[0]
    return parent.is_dir()


def _contradictory_fence(item: Item) -> list[Finding]:
    """A fence that forbids what it allows leaves the agent nothing legal to do.

    Only a contradiction counts. `allowed: writ/` with `forbidden: writ/state.py`
    is a carve-out — touch the package but not that file — and is exactly what the
    two fields are for, so it passes. The reverse does not: `allowed:
    writ/state.py` under `forbidden: writ/` permits one file inside a directory
    the task may not touch.
    """
    findings: list[Finding] = []
    allowed = [_normalize_path(path) for path in item.allowed]
    for raw in item.forbidden:
        forbidden = _normalize_path(raw)
        for permitted in allowed:
            if forbidden == permitted:
                findings.append(
                    Finding(
                        severity="error",
                        category="contradictory-fence",
                        message=f"{raw!r} is both allowed and forbidden",
                        where=item.id,
                        suggested_action="decide which one it is",
                    )
                )
            elif _contains(forbidden, permitted):
                findings.append(
                    Finding(
                        severity="error",
                        category="contradictory-fence",
                        message=(
                            f"forbidden {raw!r} contains allowed {permitted!r}, so "
                            "the task may not touch what it is scoped to"
                        ),
                        where=item.id,
                        suggested_action=(
                            "forbid the siblings instead, or widen `allowed`"
                        ),
                    )
                )
    return findings


def _shared_ownership(snapshot: Snapshot) -> list[Finding]:
    """Two tasks that can run at once must not own the same files.

    This is the parallel-execution failure the graph cannot see: nothing orders
    the two tasks, both agents edit the same file, and whichever finishes second
    either loses its work or fails a bar for reasons it did not cause.

    Ordered tasks are fine — one finishes before the other starts — so this only
    looks at pairs with no path between them.
    """
    findings: list[Finding] = []
    ancestors = _ancestors(snapshot)
    items = [item for item in snapshot.items if item.allowed]
    for index, first in enumerate(items):
        for second in items[index + 1 :]:
            if second.id in ancestors.get(first.id, set()):
                continue
            if first.id in ancestors.get(second.id, set()):
                continue
            shared = _overlaps(first.allowed, second.allowed)
            if not shared:
                continue
            path = shared[0]
            precise = _looks_like_file(path)
            findings.append(
                Finding(
                    severity="error" if precise else "warning",
                    category="shared-ownership",
                    message=(
                        f"{first.id} and {second.id} both own {path!r} and nothing "
                        "orders them, so they can run at the same time"
                    ),
                    where=first.id,
                    suggested_action=(
                        f"give one of them the file and depend on it, or order "
                        f"{second.id} after {first.id}"
                    ),
                )
            )
    return findings


def check_dependencies(snapshot: Snapshot) -> list[Finding]:
    """What the DAG check cannot see: repeats, and edges pointing nowhere."""
    findings: list[Finding] = []
    known = {item.id for item in snapshot.items}
    for item in snapshot.items:
        seen: set[str] = set()
        for dep in item.depends_on:
            if dep == item.id:
                findings.append(
                    Finding(
                        severity="error",
                        category="self-dependency",
                        message="depends on itself",
                        where=item.id,
                        suggested_action="remove the edge",
                    )
                )
            elif dep in seen:
                findings.append(
                    Finding(
                        severity="warning",
                        category="duplicate-dependency",
                        message=f"depends on {dep} twice",
                        where=item.id,
                        suggested_action="state the edge once",
                    )
                )
            seen.add(dep)
            if snapshot.committed and dep not in known:
                findings.append(
                    Finding(
                        severity="error",
                        category="unknown-dependency",
                        message=f"depends on {dep}, which is not a task",
                        where=item.id,
                        suggested_action="point it at a real task, or drop the edge",
                    )
                )
    return findings


def check_coverage(snapshot: Snapshot) -> list[Finding]:
    """Every requirement ends somewhere, and every task exists for a reason.

    Skipped entirely when the plan declared no requirements: an extracted plan or
    one from an older Writ has nothing to map, and inventing a complaint about a
    missing inventory on every task would bury the findings that matter.
    """
    findings: list[Finding] = []
    if not snapshot.requirements:
        return findings
    by_id = {requirement.id: requirement for requirement in snapshot.requirements}
    covered: dict[str, list[str]] = {req_id: [] for req_id in by_id}
    for item in snapshot.items:
        for req_id in item.requirement_ids:
            if req_id not in by_id:
                findings.append(
                    Finding(
                        severity="error",
                        category="unknown-requirement",
                        message=f"claims to cover {req_id}, which is not in the inventory",
                        where=item.id,
                        requirement_ids=[req_id],
                        suggested_action="fix the reference, or add the requirement",
                    )
                )
                continue
            # Only implementation work counts as coverage. A gate that names a
            # requirement is saying it will *check* it, and a requirement checked
            # by a gate with no task behind it is one nothing implements — which
            # is precisely the hole this check exists to find.
            if item.kind == "task":
                covered[req_id].append(item.id)
        if item.kind == "task" and not item.requirement_ids:
            findings.append(
                Finding(
                    severity="warning",
                    category="unjustified-task",
                    message=(
                        "references no requirement, so nothing in the design asks "
                        "for it"
                    ),
                    where=item.id,
                    suggested_action=(
                        "name the requirement it serves, or say in notes why the "
                        "work is infrastructure"
                    ),
                )
            )
    for requirement in snapshot.requirements:
        findings.extend(_requirement_findings(requirement, covered[requirement.id]))
    return findings


def _requirement_findings(requirement: Requirement, tasks: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    if requirement.priority not in REQUIREMENT_PRIORITIES:
        findings.append(
            Finding(
                severity="warning",
                category="requirement-shape",
                message=(
                    f"priority {requirement.priority!r} is not one of "
                    f"{', '.join(REQUIREMENT_PRIORITIES)}"
                ),
                where=requirement.id,
                requirement_ids=[requirement.id],
                suggested_action="use must, should, or may",
            )
        )
    if requirement.status == "existing" and not requirement.evidence:
        findings.append(
            Finding(
                severity="error",
                category="unevidenced-requirement",
                message=(
                    "is claimed already implemented with no evidence, so nothing "
                    "shows it holds"
                ),
                where=requirement.id,
                requirement_ids=[requirement.id],
                suggested_action="name the test or the code that satisfies it",
            )
        )
    if requirement.status in ("out-of-scope", "deferred") and not requirement.reason:
        findings.append(
            Finding(
                severity="error",
                category="undeclared-exclusion",
                message=f"is marked {requirement.status} with no reason given",
                where=requirement.id,
                requirement_ids=[requirement.id],
                suggested_action="say why it is out of this plan's scope",
            )
        )
    if tasks or requirement.status != "planned":
        return findings
    findings.append(
        Finding(
            severity="error" if requirement.priority == "must" else "warning",
            category="missing-coverage",
            message=(
                f"no task covers it: {_shorten(requirement.text)}"
            ),
            where=requirement.id,
            requirement_ids=[requirement.id],
            suggested_action=(
                "add a task that covers it, or mark it existing with evidence or "
                "out-of-scope with a reason"
            ),
        )
    )
    return findings


def check_integration(snapshot: Snapshot) -> list[Finding]:
    """Parallel branches that nothing joins are work nobody verifies together.

    A graph can be acyclic, fully covered, and still end in four independent
    leaves with no task or gate that checks they compose. Task-local review cannot
    catch that by construction: each leaf satisfied its own criteria.
    """
    findings: list[Finding] = []
    tasks = snapshot.tasks
    if len(tasks) < 2:
        return findings
    if any(gate.kind == "gate" for gate in snapshot.gates):
        return findings
    depended_on = {dep for item in snapshot.items for dep in item.depends_on}
    sinks = [item.id for item in tasks if item.id not in depended_on]
    if len(sinks) < 2:
        return findings
    findings.append(
        Finding(
            severity="warning",
            category="missing-integration",
            message=(
                f"{len(sinks)} tasks end the graph with nothing verifying them "
                f"together: {', '.join(sinks[:6])}"
                + (" …" if len(sinks) > 6 else "")
            ),
            where="",
            suggested_action=(
                "add a gate or an integration task that depends on them and checks "
                "the combined behaviour"
            ),
        )
    )
    return findings


# --------------------------------------------------------------------------
# path and graph helpers


def _normalize_path(path: str) -> str:
    cleaned = path.strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.strip("/")


def _is_broad(path: str) -> bool:
    return _normalize_path(path).lower() in BROAD_PATHS


def _contains(parent: str, child: str) -> bool:
    """Whether `parent` is a directory prefix of `child`, at a path boundary."""
    if not parent or parent == child:
        return False
    return child.startswith(parent + "/")


def _overlaps(first: Iterable[str], second: Iterable[str]) -> list[str]:
    """The paths two fences share, either equal or one inside the other."""
    shared: list[str] = []
    left = [_normalize_path(path) for path in first if _normalize_path(path)]
    right = [_normalize_path(path) for path in second if _normalize_path(path)]
    for one in left:
        for other in right:
            if one == other:
                shared.append(one)
            elif _contains(one, other):
                shared.append(other)
            elif _contains(other, one):
                shared.append(one)
    seen: set[str] = set()
    unique = []
    for path in shared:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _looks_like_file(path: str) -> bool:
    tail = _normalize_path(path).rsplit("/", 1)[-1]
    return "." in tail


def _path_exists(root: Path, path: str) -> bool:
    candidate = _normalize_path(path)
    if not candidate:
        return False
    return (root / candidate).exists()


def _ancestors(snapshot: Snapshot) -> dict[str, set[str]]:
    """Every item each item transitively depends on.

    Tolerates a cycle rather than raising: `check_dag` is the place that rejects
    one, and a checker that crashed on a bad graph would withhold every other
    finding about it.
    """
    edges = {item.id: list(item.depends_on) for item in snapshot.items}
    resolved: dict[str, set[str]] = {}

    def walk(node: str, trail: frozenset[str]) -> set[str]:
        if node in resolved:
            return resolved[node]
        if node in trail:
            return set()
        found: set[str] = set()
        for dep in edges.get(node, ()):
            found.add(dep)
            found |= walk(dep, trail | {node})
        if node not in trail:
            resolved[node] = found
        return found

    for item_id in edges:
        walk(item_id, frozenset())
    return resolved


def _shorten(text: str, limit: int = 90) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"
