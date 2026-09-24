"""Deterministic plan review: what Writ can prove about a plan by itself.

`planning.load_document` checks that the JSON has the fields it needs and
`model.check_dag` checks that the graph is legal. This module checks the rest of
what is mechanical, and only that: the shape of each feature, whether every
requirement ends somewhere, whether every interface a feature consumes has a
provider, and whether the graph is acyclic. Anything that needs reading — is
this the right split, will this approach work here — belongs to the critics in
`critics.py`.

What used to be here besides, and is not any more: heuristics about acceptance
wording, paths that do not exist yet, and two tasks naming the same file. On a
plan for code that is not written yet, every one of them fired on work the plan
was proposing rather than on a defect, and the repair loop spent its rounds
answering them. File and test names are the executing agent's call now (see
`contracts.py` and docs/planning-redesign.md §4).

Findings are not exceptions. A plan with errors is still written down, because a
human reading the whole plan next to the objections is in a far better position
than one reading a single raised error with no plan attached. What errors do block
is *execution*: they hold the plan at `needs-approval` (see `plans.py`).

Two entry points, one check set: `from_plan` reads a plan document before its ids
exist, `from_state` reads the committed graph. Same checks either way, so
`writ plan --dry-run` and `writ check` cannot disagree.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import contracts

#: severity ordering, worst first. Only `error` blocks approval.
SEVERITIES = ("error", "warning", "note")

#: how many acceptance criteria a milestone task should state
MIN_ACCEPTANCES = 2
MAX_ACCEPTANCES = 6

#: a feature's behaviours: fewer is a feature nobody can judge done, more is two
#: features
MIN_BEHAVIOURS = 3
MAX_BEHAVIOURS = 6

#: past this many requirements, one agent is not building one subsystem
MAX_FEATURE_REQUIREMENTS = 6

#: how many features a plan should have. Advisory: a small document may need
#: three, and that is a judgement for the critics rather than a rule.
MIN_FEATURES = 4
MAX_FEATURES = 12

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
    #: set on a feature (see `contracts.py`); empty on a milestone task
    owns: list[str] = field(default_factory=list)
    provides: list[str] = field(default_factory=list)
    consumes: list[str] = field(default_factory=list)
    feature: bool = False


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
    #: the finer obligations this capability contains. Traceable, not numbered:
    #: the final gate checks them, and nothing earlier has to.
    details: list[str] = field(default_factory=list)
    #: how it could be demonstrated, from plans written before acceptance moved
    #: onto features. Carried, never asked for.
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
            "details": list(self.details),
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
            details=[str(item) for item in payload.get("details") or []],
            verification=list(payload.get("verification") or []),
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
                    milestone="" if getattr(milestone, "loose", False) else milestone.title,
                    owns=list(getattr(task, "owns", []) or []),
                    provides=list(getattr(task, "provides", []) or []),
                    consumes=list(getattr(task, "consumes", []) or []),
                    feature=bool(getattr(task, "feature", False)),
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
            owns=list(task.get("owns") or []),
            provides=list(task.get("provides") or []),
            consumes=list(task.get("consumes") or []),
            feature=contracts.is_feature(task),
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
    findings.extend(check_shape(snapshot))
    findings.extend(check_dependencies(snapshot))
    findings.extend(check_contracts(snapshot))
    findings.extend(check_coverage(snapshot))
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


def check_shape(snapshot: Snapshot) -> list[Finding]:
    """The schema a loader cannot enforce: counts, repeats and size."""
    findings: list[Finding] = []
    features = [item for item in snapshot.tasks if item.feature]
    if features and not (MIN_FEATURES <= len(features) <= MAX_FEATURES):
        findings.append(
            Finding(
                severity="warning",
                category="feature-count",
                message=(
                    f"the plan has {len(features)} features; {MIN_FEATURES}-"
                    f"{MAX_FEATURES} subsystems is the target"
                ),
                suggested_action=(
                    "merge features one agent could build together, or split one "
                    "that is several subsystems"
                ),
            )
        )
    for item in snapshot.tasks:
        findings.extend(
            _feature_shape(item) if item.feature else _task_shape(item)
        )
        seen: dict[str, int] = {}
        for number, text in enumerate(item.acceptances, start=1):
            key = " ".join(text.lower().split())
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
    return findings


def _task_shape(item: Item) -> list[Finding]:
    count = len(item.acceptances)
    if count < MIN_ACCEPTANCES:
        return [
            Finding(
                severity="warning",
                category="thin-acceptance",
                message=(
                    f"states {count} acceptance criterion; {MIN_ACCEPTANCES}-"
                    f"{MAX_ACCEPTANCES} is the bar for a task a reviewer has to judge"
                ),
                where=item.id,
                suggested_action="add the verification the task owns",
            )
        ]
    if count > MAX_ACCEPTANCES:
        return [
            Finding(
                severity="warning",
                category="wide-acceptance",
                message=(
                    f"states {count} acceptance criteria, more than "
                    f"{MAX_ACCEPTANCES}; a task with this many bars is usually two"
                ),
                where=item.id,
                suggested_action="split the task, or fold related bars together",
            )
        ]
    return []


def _feature_shape(item: Item) -> list[Finding]:
    findings: list[Finding] = []
    count = len(item.acceptances)
    if count < MIN_BEHAVIOURS:
        findings.append(
            Finding(
                severity="warning",
                category="thin-acceptance",
                message=(
                    f"states {count} behaviour(s); {MIN_BEHAVIOURS}-{MAX_BEHAVIOURS} "
                    "is the bar for a feature a reviewer has to judge"
                ),
                where=item.id,
                suggested_action="state what a user of this subsystem can observe",
            )
        )
    oversized = []
    if count > MAX_BEHAVIOURS:
        oversized.append(f"{count} behaviours")
    if len(item.requirement_ids) > MAX_FEATURE_REQUIREMENTS:
        oversized.append(f"{len(item.requirement_ids)} requirements")
    if oversized:
        findings.append(
            Finding(
                severity="warning",
                category="oversized-feature",
                message=(
                    f"carries {' and '.join(oversized)}; that is more than one "
                    "agent builds as one subsystem"
                ),
                where=item.id,
                requirement_ids=list(item.requirement_ids),
                suggested_action="split it along an interface it would provide",
            )
        )
    if not item.owns:
        findings.append(
            Finding(
                severity="warning",
                category="unowned-feature",
                message="owns no component, so nothing fences it",
                where=item.id,
                suggested_action="name the directory or package this feature builds",
            )
        )
    return findings


def check_dependencies(snapshot: Snapshot) -> list[Finding]:
    """What the DAG check reports as an exception, reported as findings."""
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
    ancestors = _ancestors(snapshot)
    for item in snapshot.items:
        if item.id in ancestors.get(item.id, set()) and item.id not in item.depends_on:
            findings.append(
                Finding(
                    severity="error",
                    category="cycle",
                    message="is its own transitive dependency",
                    where=item.id,
                    suggested_action=(
                        "break the cycle: one side of it should provide an "
                        "interface the other consumes, not both"
                    ),
                )
            )
    return findings


def check_contracts(snapshot: Snapshot) -> list[Finding]:
    """Every interface a feature consumes has exactly one provider.

    This is the whole of what used to be the missing-edge and shared-ownership
    arguments: edges are derived from these contracts (`contracts.edges`), so a
    consumed interface with a provider *is* an edge, and one without is a gap.
    """
    findings: list[Finding] = []
    features = {
        item.id: {"provides": item.provides, "consumes": item.consumes}
        for item in snapshot.tasks
        if item.feature
    }
    if not features:
        return findings
    by_name = contracts.providers(features)
    for key, owners in sorted(by_name.items()):
        if len(owners) > 1:
            findings.append(
                Finding(
                    severity="error",
                    category="contract-gap",
                    message=(
                        f"interface {key!r} is provided by {', '.join(owners)}; "
                        "a consumer cannot tell which one it depends on"
                    ),
                    where=owners[1],
                    suggested_action="give the interface one provider, or name them apart",
                )
            )
    for feature_id, record in sorted(features.items()):
        for line in record["consumes"]:
            key = contracts.name(line)
            if key and key not in by_name:
                findings.append(
                    Finding(
                        severity="error",
                        category="contract-gap",
                        message=f"consumes {key!r}, which no feature provides",
                        where=feature_id,
                        suggested_action=(
                            "add it to the `provides` of the feature that builds it, "
                            "or drop it if it already exists in the repository"
                        ),
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
            # by a gate with no task behind it is one nothing implements.
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
            message=f"nothing covers it: {_shorten(requirement.text)}",
            where=requirement.id,
            requirement_ids=[requirement.id],
            suggested_action=(
                "have a feature cover it, or mark it existing with evidence or "
                "out-of-scope with a reason"
            ),
        )
    )
    return findings


# --------------------------------------------------------------------------
# graph helpers


def _ancestors(snapshot: Snapshot) -> dict[str, set[str]]:
    """Every item each item transitively depends on.

    Tolerates a cycle rather than raising: a checker that crashed on a bad graph
    would withhold every other finding about it.
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
            if dep not in trail:
                found |= walk(dep, trail | {node})
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
