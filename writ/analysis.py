"""Staged planning: three analyses before anything is decomposed into tasks.

`writ plan` used to be one agent call. That agent read the design document, read
the repository, decided how the work could be verified, chose task boundaries,
inferred the dependency graph and wrote the acceptance bars — in one response,
with no artifact between any two of those judgements.

Those are different jobs, and running them together loses the thing that makes a
plan checkable. An obligation the document states and the planner did not notice
leaves no trace: there is no list it is missing from. A task fenced to a directory
that does not exist looks exactly like a task fenced to one that does. A criterion
that cannot be demonstrated reads the same as one that can, because nothing ever
asked how it would be.

So the judgements are separated, and each one writes down what it found before the
next one runs:

```text
requirements.json   what the document obliges, one entry per obligation
inventory.json      what the repository already is, and already does
verification.json   how each obligation could be demonstrated
        │
        ▼
plan.json           the decomposition, synthesized from all three
```

Two properties follow that a single call cannot have. Each artifact is *checkable
on its own* — writ validates the inventory's requirement references before the
planner ever sees it, so a hallucinated `REQ-009` fails at the stage that invented
it rather than becoming a task nobody asked for. And the synthesizer is *held to*
the earlier artifacts: it receives the requirement inventory as a fixed list, and
`reconcile` reports any id it dropped or invented as a finding on the plan.

What this deliberately does not do is generate several competing plans and pick
one. Candidate plans were in the original recommendation and are not here: with
`requirements.json` fixed, the useful disagreement is about *coverage of a known
list*, which the critics (`writ/critics.py`) provide by reading the one plan
adversarially. Two plans with no shared vocabulary would need a third agent to
choose between them, and that agent would be the unreviewed author again.

Stages are resumable. Artifacts live under `.writ/plans/<plan-id>/`, and a stage
whose artifact is already there is not re-run unless asked, so a pipeline that
failed at synthesis does not pay for three analyses again.
"""
from __future__ import annotations

import json
import re
import threading
from concurrent import futures
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from . import agents, runner, state
from .plancheck import Finding, Requirement
from .stream import truncated
from .state import WritError, utcnow

# --------------------------------------------------------------------------
# the stages


@dataclass(frozen=True)
class Stage:
    """One analysis: its artifact, what it is asked, and what it may not do."""

    name: str
    #: the file it writes under .writ/plans/<plan-id>/
    artifact: str
    #: one line, for the terminal and for `writ list stages`
    summary: str
    #: what this stage is for, in full, as the prompt's opening
    brief: str
    #: the JSON it must produce
    schema: str
    #: the rules it works under, one bullet per line
    rules: str
    #: what it must not do — the boundary that keeps it from being the planner
    out_of_scope: str = ""
    #: the artifacts it cannot run without. Only a hard prerequisite belongs here:
    #: verification is deciding how to prove each requirement, so it cannot start
    #: before there is a list of them. An artifact that merely *sharpens* a stage
    #: does not, which is what lets two stages share a wave — see `waves`.
    needs: tuple[str, ...] = ()
    #: the extra instruction to give this stage when the requirement inventory is
    #: not available to it, naming what it must therefore leave out. A stage that
    #: can run without the ids still must not invent them.
    without_requirements: str = ""

    @property
    def scope(self) -> str:
        """Its source tag in the findings ledger."""
        return f"stage:{self.name}"


REQUIREMENTS_SCHEMA = """\
{
  "requirements": [
    {
      "id": "REQ-001",
      "text": "one obligation the document states, in your own words",
      "source": "the exact heading it came from",
      "priority": "must" | "should" | "may",
      "status": "planned" | "existing" | "out-of-scope" | "deferred",
      "evidence": "status existing only: the test or code that satisfies it",
      "reason": "status out-of-scope or deferred only: why it is not this plan's",
      "verification": ["how this could be demonstrated, if the document says"]
    }
  ],
  "ambiguities": [
    {
      "id": "AMB-001",
      "question": "what the document leaves open, as a question",
      "requirement_ids": ["REQ-004"],
      "readings": ["one way to read it", "another"],
      "assumed": "the reading you would proceed on, if forced"
    }
  ]
}"""

REQUIREMENTS_RULES = """\
Rules:
- One obligation per entry. Do not fold two together to shorten the list: two
  obligations in one entry can only ever be half-covered, and nothing downstream
  can say which half.
- Write every obligation the document states, of every kind: behaviour,
  interface, constraint, data shape, error handling, performance bar,
  compatibility promise, operational requirement. A constraint stated once in
  prose is still an obligation.
- `source` must be a heading that appears verbatim in the document. Use
  "Parent / Child" for a nested heading. An obligation you cannot trace to a
  heading is one you may have invented.
- `priority` is the document's own emphasis, not your view of what matters.
  "must"/"shall"/"is required" is `must`; "should"/"prefer" is `should`;
  "may"/"could"/"optionally" is `may`. When the document is flat, `must`.
- `status` is `planned` unless you have read the repository and found the
  obligation already discharged, in which case `existing` with `evidence` naming
  the test or code. Do not guess: `planned` for something already built is a
  wasted task, which is cheaper than an obligation marked done that is not.
- Record what the document leaves genuinely open as an ambiguity, with the
  readings it could bear. Do not silently pick one. An ambiguity is not an
  excuse to omit the requirement — write the requirement too.
- Quote or paraphrase closely. This inventory becomes the list every later stage
  is held to, so an obligation phrased more weakly here is weakened everywhere."""

INVENTORY_SCHEMA = """\
{
  "components": [
    {
      "name": "event store",
      "paths": ["writ/state.py", "writ/runner.py"],
      "existing_behavior": "what it does today, not what it should do",
      "test_locations": ["tests/test_state.py"],
      "extension_points": ["where new work would attach"],
      "risks": ["what makes this component costly or dangerous to change"]
    }
  ],
  "existing_coverage": [
    {
      "requirement_id": "REQ-003",
      "status": "full" | "partial" | "none",
      "evidence": "the test or code that shows it, by path and name"
    }
  ],
  "conventions": ["how this repository does things, that new work should match"],
  "baseline_commands": ["pytest -q"],
  "baseline_result": {
    "status": "pass" | "fail" | "unknown",
    "summary": "what the command printed, in a line or two",
    "known_failures": ["a test that already fails, by name"]
  }
}"""

INVENTORY_RULES = """\
Rules:
- Report what the repository is, not what it should become. Every path you name
  must exist; check rather than assume. A plan fenced to a directory you imagined
  fails at execution with no useful error.
- Run the project's own verification before anything is planned, and record the
  result. This is the single most useful line in this file: without it, every
  failure during execution is ambiguous between "the new work broke it" and "it
  was already broken". If you cannot run it, say `unknown` and say why — do not
  report `pass` for a command you did not run.
- Name the tests that already exist per component. Work that has test coverage is
  work a plan can safely change; work that has none needs its own bar first.
- Map the requirements you were given onto what already exists. `full` means a
  test demonstrates it today — name that test. `partial` means some of it holds.
  Unevidenced `full` is worse than `none`: it deletes a requirement from the plan.
- `requirement_id` must be an id from the inventory you were given. Do not invent
  ids and do not renumber; if an obligation seems missing from that list, say so
  in `conventions` and carry on — the list is fixed at this point.
- Record the conventions that would make new work look like the existing code:
  layout, naming, error handling, how tests are written, what the project
  already depends on."""

VERIFICATION_SCHEMA = """\
{
  "verification": [
    {
      "requirement_id": "REQ-001",
      "methods": [
        {
          "kind": "test" | "command" | "artifact" | "inspection",
          "location": "tests/test_parser.py",
          "command": "pytest -q tests/test_parser.py",
          "observable": "malformed input produces a stable error, exit 2",
          "exists": true,
          "needs": "what has to be built before this can run, if anything"
        }
      ],
      "confidence": "high" | "medium" | "low"
    }
  ],
  "missing_infrastructure": [
    {
      "need": "there is no integration test harness",
      "blocks": ["REQ-007", "REQ-011"],
      "suggestion": "what would have to exist first"
    }
  ],
  "undemonstrable": [
    {
      "requirement_id": "REQ-014",
      "why": "why no method would actually demonstrate this",
      "closest": "the nearest thing that could be checked"
    }
  ]
}"""

VERIFICATION_RULES = """\
Rules:
- Every requirement you were given gets an entry. A requirement with no way to
  demonstrate it is the most expensive kind of plan defect — it produces work
  that is reported complete because nothing could show otherwise — so say so
  explicitly in `undemonstrable` rather than inventing a plausible command.
- A method must be concrete enough to run or observe. Name the command, the test
  file, the artifact, or the behaviour and how it is seen. "Verify it works" is
  not a method.
- `exists` is whether that test or command exists in the repository *now*. Check.
  Where it does not, `needs` says what must be built, which is how the planner
  knows a test is itself work rather than a bar it can just cite.
- Prefer verification the repository can already run. A method requiring new
  infrastructure needs that infrastructure planned, and a plan whose every bar
  needs new scaffolding will never demonstrate anything.
- Where a requirement can only be shown by inspection, say `inspection` honestly
  rather than dressing it as a test. A reviewer reading criteria needs to know
  which bars a machine can check.
- Do not decide who does the work, in what order, or in how many tasks. You are
  saying what proof would look like, not planning."""

#: the three analyses, in the order they run.
#:
#: Ordered by dependency rather than by cost: requirements is first because
#: everything later references its ids, inventory second because verification
#: needs to know which tests exist, verification last because it is the only one
#: that needs both. Each receives the artifacts of the ones before it.
#:
#: `needs` records which of those dependencies is real. Only verification has one:
#: it is asked how to prove each requirement, so a list of requirements is not
#: context but input. The inventory is surveying the repository, which the design
#: document does not change — it is given the requirement ids when they exist only
#: so its coverage claims can attach to them.
STAGES: tuple[Stage, ...] = (
    Stage(
        name="requirements",
        artifact="requirements.json",
        summary="what the design document obliges, as a numbered inventory",
        brief=(
            "You are reading a design document and writing down every obligation "
            "it states. You are not planning the work, and you are not deciding "
            "how it will be built."
        ),
        schema=REQUIREMENTS_SCHEMA,
        rules=REQUIREMENTS_RULES,
        out_of_scope=(
            "Do not propose milestones, tasks, ordering, or file boundaries. This "
            "inventory is the list the plan will be checked against, and a list "
            "written with a decomposition already in mind gets shaped to fit it."
        ),
    ),
    Stage(
        name="inventory",
        artifact="inventory.json",
        summary="what the repository already is, does, and tests",
        brief=(
            "You are surveying this repository so that the plan is grounded in "
            "what is actually here. You are not planning the work, and you are "
            "not changing anything."
        ),
        schema=INVENTORY_SCHEMA,
        rules=INVENTORY_RULES,
        out_of_scope=(
            "Do not propose tasks or an architecture for the new work. Do not "
            "judge whether the design is a good idea. Report the ground, not the "
            "route across it."
        ),
        without_requirements=(
            "You have not been given the requirement inventory: it is being written "
            "at the same time as this survey. So leave `existing_coverage` empty. "
            "Report what this repository has and what it proves in `components` and "
            "`baseline_commands` as fully as you can, and leave the question of "
            "which stated obligation that discharges to the stage that has the ids. "
            "Do not guess at requirement ids in order to fill the field."
        ),
    ),
    Stage(
        name="verification",
        artifact="verification.json",
        summary="how each obligation could be demonstrated",
        brief=(
            "You are deciding how each stated obligation could be proved to hold, "
            "given this repository. You are not planning the work that satisfies "
            "them."
        ),
        schema=VERIFICATION_SCHEMA,
        rules=VERIFICATION_RULES,
        needs=("requirements", "inventory"),
        out_of_scope=(
            "Do not group requirements into tasks, assign them an order, or write "
            "acceptance criteria for work that does not exist yet. One requirement "
            "at a time, and only how it would be shown."
        ),
    ),
)

STAGE_NAMES = tuple(stage.name for stage in STAGES)


def by_name(names: Iterable[str] | None) -> list[Stage]:
    """The named stages in pipeline order, or all of them."""
    wanted = [name for name in (names or ()) if name]
    if not wanted:
        return list(STAGES)
    known = {stage.name: stage for stage in STAGES}
    unknown = [name for name in wanted if name not in known]
    if unknown:
        raise WritError(
            f"unknown stage(s): {', '.join(sorted(unknown))}. "
            f"Known stages: {', '.join(STAGE_NAMES)}"
        )
    return [stage for stage in STAGES if stage.name in set(wanted)]


def upto(name: str) -> list[Stage]:
    """Every stage through `name`, since a stage needs the ones before it."""
    known = {stage.name for stage in STAGES}
    if name not in known:
        raise WritError(
            f"unknown stage: {name}. Known stages: {', '.join(STAGE_NAMES)}"
        )
    taken: list[Stage] = []
    for stage in STAGES:
        taken.append(stage)
        if stage.name == name:
            break
    return taken


# --------------------------------------------------------------------------
# the artifacts


@dataclass
class RequirementsArtifact:
    """A validated requirements inventory, plus what the document left open."""

    requirements: list[Requirement] = field(default_factory=list)
    ambiguities: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ids(self) -> list[str]:
        return [requirement.id for requirement in self.requirements]

    @property
    def open_questions(self) -> list[dict[str, Any]]:
        """Ambiguities with no assumed reading — the ones a human must settle."""
        return [
            entry
            for entry in self.ambiguities
            if not str(entry.get("assumed", "")).strip()
        ]


@dataclass
class InventoryArtifact:
    """What the repository is, and what of the design it already satisfies."""

    components: list[dict[str, Any]] = field(default_factory=list)
    existing_coverage: list[dict[str, Any]] = field(default_factory=list)
    conventions: list[str] = field(default_factory=list)
    baseline_commands: list[str] = field(default_factory=list)
    baseline_result: dict[str, Any] = field(default_factory=dict)

    @property
    def baseline_status(self) -> str:
        return str(self.baseline_result.get("status", "unknown") or "unknown")

    @property
    def known_failures(self) -> list[str]:
        return [str(item) for item in self.baseline_result.get("known_failures", [])]

    def satisfied(self) -> list[str]:
        """Requirements this repository already covers in full, with evidence."""
        return sorted(
            str(entry.get("requirement_id", ""))
            for entry in self.existing_coverage
            if entry.get("status") == "full" and str(entry.get("evidence", "")).strip()
        )


@dataclass
class VerificationArtifact:
    """How each requirement could be demonstrated, and which cannot be."""

    verification: list[dict[str, Any]] = field(default_factory=list)
    missing_infrastructure: list[dict[str, Any]] = field(default_factory=list)
    undemonstrable: list[dict[str, Any]] = field(default_factory=list)

    def methods_for(self, requirement_id: str) -> list[dict[str, Any]]:
        for entry in self.verification:
            if entry.get("requirement_id") == requirement_id:
                return list(entry.get("methods", []))
        return []

    @property
    def covered(self) -> set[str]:
        return {
            str(entry.get("requirement_id", ""))
            for entry in self.verification
            if entry.get("methods")
        }


@dataclass
class Artifacts:
    """Whatever the pipeline has produced so far, for the stage that is next."""

    requirements: RequirementsArtifact | None = None
    inventory: InventoryArtifact | None = None
    verification: VerificationArtifact | None = None

    def get(self, name: str) -> Any:
        return getattr(self, name, None)

    @property
    def inventory_requirements(self) -> list[Requirement]:
        return list(self.requirements.requirements) if self.requirements else []


# --------------------------------------------------------------------------
# reading and validating an artifact


def _payload(text: str, stage: Stage, path: Path) -> dict[str, Any]:
    """Parse a stage artifact, recovering JSON printed among prose."""
    from .planning import extract_json  # local: planning imports nothing from here

    stripped = (text or "").strip()
    if not stripped:
        raise WritError(f"the {stage.name} stage wrote an empty artifact: {path}")
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        recovered = extract_json(stripped)
        if recovered is None:
            raise WritError(
                f"the {stage.name} artifact is not valid JSON: {exc} ({path})"
            ) from exc
        payload = json.loads(recovered)
    if not isinstance(payload, dict):
        raise WritError(
            f"the {stage.name} artifact must be a JSON object, not "
            f"{type(payload).__name__} ({path})"
        )
    return payload


def _objects(value: Any, where: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise WritError(f"{where} must be a list of objects")
    entries: list[dict[str, Any]] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise WritError(f"{where}[{index}] must be an object")
        entries.append(entry)
    return entries


def _strings(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise WritError(f"{where} must be a list of strings")
    return [str(entry).strip() for entry in value if str(entry).strip()]


def load_requirements(text: str, *, path: Path | None = None) -> RequirementsArtifact:
    """Validate a requirements artifact.

    Reuses `planning`'s requirement validator rather than a second one, because
    the inventory this stage writes and the inventory a single-shot plan states
    are the same object and must be rejected for the same reasons.
    """
    from .planning import load_requirement_inventory

    where = path or Path("requirements.json")
    payload = _payload(text, STAGES[0], where)
    requirements = load_requirement_inventory(payload.get("requirements"))
    if not requirements:
        raise WritError(
            f"the requirements stage found no obligations at all ({where}). "
            "A design document that states nothing cannot be planned; re-run the "
            "stage, or plan with --no-stages if this document really is empty."
        )
    ambiguities = _objects(payload.get("ambiguities"), "ambiguities")
    known = {requirement.id for requirement in requirements}
    for index, entry in enumerate(ambiguities):
        for req_id in _strings(
            entry.get("requirement_ids"), f"ambiguities[{index}].requirement_ids"
        ):
            if req_id not in known:
                raise WritError(
                    f"ambiguities[{index}] refers to {req_id}, which is not in "
                    f"this inventory ({where})"
                )
    return RequirementsArtifact(requirements=requirements, ambiguities=ambiguities)


def load_inventory(
    text: str, *, known: Iterable[str] = (), path: Path | None = None
) -> InventoryArtifact:
    """Validate a repository inventory against the requirement ids it may cite.

    An unknown id is an error rather than a warning. The whole reason this stage
    runs after the requirements stage is so that its coverage claims attach to
    real obligations; a claim about `REQ-009` when no such requirement exists is
    the analyst filling in a gap it imagined, and accepting it would let a
    hallucinated obligation be marked already-satisfied.
    """
    where = path or Path("inventory.json")
    payload = _payload(text, STAGES[1], where)
    ids = set(known)
    components = _objects(payload.get("components"), "components")
    for index, component in enumerate(components):
        if not str(component.get("name", "")).strip():
            raise WritError(f"components[{index}] has no name ({where})")
    coverage = _objects(payload.get("existing_coverage"), "existing_coverage")
    for index, entry in enumerate(coverage):
        req_id = str(entry.get("requirement_id", "")).strip()
        if not req_id:
            raise WritError(
                f"existing_coverage[{index}] names no requirement_id ({where})"
            )
        if ids and req_id not in ids:
            raise WritError(
                f"existing_coverage[{index}] claims coverage of {req_id}, which is "
                f"not in the requirement inventory ({where}). Re-run the inventory "
                "stage; it may not invent requirement ids."
            )
        status = str(entry.get("status", "")).strip() or "none"
        if status not in ("full", "partial", "none"):
            raise WritError(
                f"existing_coverage[{index}].status must be full, partial or none, "
                f"not {status!r} ({where})"
            )
        entry["status"] = status
    result = payload.get("baseline_result") or {}
    if not isinstance(result, dict):
        raise WritError(f"baseline_result must be an object ({where})")
    status = str(result.get("status", "")).strip() or "unknown"
    if status not in ("pass", "fail", "unknown"):
        raise WritError(
            f"baseline_result.status must be pass, fail or unknown, not "
            f"{status!r} ({where})"
        )
    result["status"] = status
    result["known_failures"] = _strings(
        result.get("known_failures"), "baseline_result.known_failures"
    )
    return InventoryArtifact(
        components=components,
        existing_coverage=coverage,
        conventions=_strings(payload.get("conventions"), "conventions"),
        baseline_commands=_strings(
            payload.get("baseline_commands"), "baseline_commands"
        ),
        baseline_result=result,
    )


def load_verification(
    text: str, *, known: Iterable[str] = (), path: Path | None = None
) -> VerificationArtifact:
    """Validate a verification strategy against the requirements it must cover."""
    where = path or Path("verification.json")
    payload = _payload(text, STAGES[2], where)
    ids = set(known)
    entries = _objects(payload.get("verification"), "verification")
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        req_id = str(entry.get("requirement_id", "")).strip()
        if not req_id:
            raise WritError(f"verification[{index}] names no requirement_id ({where})")
        if ids and req_id not in ids:
            raise WritError(
                f"verification[{index}] is for {req_id}, which is not in the "
                f"requirement inventory ({where}). Re-run the verification stage; "
                "it may not invent requirement ids."
            )
        if req_id in seen:
            raise WritError(
                f"verification[{index}] is a second entry for {req_id}; one entry "
                f"per requirement, with every method in its `methods` ({where})"
            )
        seen.add(req_id)
        methods = _objects(entry.get("methods"), f"verification[{index}].methods")
        for position, method in enumerate(methods):
            detail = " ".join(
                str(method.get(key, ""))
                for key in ("command", "location", "observable")
            ).strip()
            if not detail:
                raise WritError(
                    f"verification[{index}].methods[{position}] states no command, "
                    f"location or observable, so it verifies nothing ({where})"
                )
        entry["methods"] = methods
    undemonstrable = _objects(payload.get("undemonstrable"), "undemonstrable")
    for index, entry in enumerate(undemonstrable):
        req_id = str(entry.get("requirement_id", "")).strip()
        if ids and req_id and req_id not in ids:
            raise WritError(
                f"undemonstrable[{index}] is for {req_id}, which is not in the "
                f"requirement inventory ({where})"
            )
    return VerificationArtifact(
        verification=entries,
        missing_infrastructure=_objects(
            payload.get("missing_infrastructure"), "missing_infrastructure"
        ),
        undemonstrable=undemonstrable,
    )


LOADERS: dict[str, Callable[..., Any]] = {
    "requirements": load_requirements,
    "inventory": load_inventory,
    "verification": load_verification,
}


def load(stage: Stage, text: str, *, known: Iterable[str] = (), path: Path | None = None):
    """Validate one stage's artifact text."""
    loader = LOADERS[stage.name]
    if stage.name == "requirements":
        return loader(text, path=path)
    return loader(text, known=known, path=path)


def read(stage: Stage, directory: Path, *, known: Iterable[str] = ()):
    """Read and validate a stage artifact already on disk, or None."""
    path = directory / stage.artifact
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return None
    return load(stage, text, known=known, path=path)


def read_all(directory: Path) -> Artifacts:
    """Every artifact present in a plan directory, validated in pipeline order.

    Reads forward so each artifact is checked against the ids the one before it
    established, which is the same order they were written in and the same order
    they are validated during a run.
    """
    artifacts = Artifacts()
    for stage in STAGES:
        known = [req.id for req in artifacts.inventory_requirements]
        found = read(stage, directory, known=known)
        if found is None:
            continue
        setattr(artifacts, stage.name, found)
    return artifacts


# --------------------------------------------------------------------------
# prompts


def _render_requirements(requirements: Iterable[Requirement]) -> list[str]:
    lines: list[str] = []
    for requirement in requirements:
        head = f"- {requirement.id} [{requirement.priority}/{requirement.status}]"
        lines.append(f"{head} {requirement.text}")
        if requirement.source:
            lines.append(f"    source: {requirement.source}")
        if requirement.verification:
            lines.append(f"    verification hints: {'; '.join(requirement.verification)}")
    return lines


def build_prompt(
    stage: Stage,
    *,
    root: Path,
    doc: Path,
    artifact_path: Path,
    artifacts: Artifacts,
    instructions: str | None = None,
    context: dict[str, Any] | None = None,
) -> str:
    """Compose one stage's prompt: its brief, what came before, and its schema."""
    context = context or {}
    lines: list[str] = [stage.brief, ""]
    lines.append(f"Repository root: {root}")
    lines.append(f"Design document: {doc}")
    lines.append("")
    if stage.name == "requirements":
        lines.append(
            "Read the design document in full before writing anything. Read enough "
            "of the repository to tell an obligation that is already discharged "
            "from one that is not."
        )
    elif stage.name == "inventory":
        lines.append(
            "Read the repository. Run its own verification command. The design "
            "document is here for context — you are surveying what exists, not "
            "what it asks for."
        )
    else:
        lines.append(
            "Read the repository's existing tests and the design document. For "
            "each obligation below, decide what would actually demonstrate it."
        )
    lines.append("")

    other_docs = [path for path in context.get("design_docs", []) if path != str(doc)]
    if other_docs:
        lines.append("Other documents already registered for this project:")
        lines.extend(f"- {path}" for path in other_docs)
        lines.append("")

    if (
        artifacts.requirements is None
        and stage.name != "requirements"
        and stage.without_requirements
    ):
        lines.append(stage.without_requirements)
        lines.append("")

    if artifacts.requirements is not None and stage.name != "requirements":
        lines.append(
            "The requirement inventory, already established. These ids are fixed: "
            "use them exactly, and do not add or renumber any."
        )
        lines.extend(_render_requirements(artifacts.requirements.requirements))
        lines.append("")
        if artifacts.requirements.ambiguities:
            lines.append("Ambiguities already recorded in the document:")
            for entry in artifacts.requirements.ambiguities:
                question = str(entry.get("question", "")).strip()
                assumed = str(entry.get("assumed", "")).strip()
                lines.append(
                    f"- {entry.get('id', 'AMB')}: {question}"
                    + (f" (assumed: {assumed})" if assumed else " (unresolved)")
                )
            lines.append("")

    if artifacts.inventory is not None and stage.name == "verification":
        inventory = artifacts.inventory
        lines.append("What the repository already is, from the inventory stage:")
        for component in inventory.components:
            paths = ", ".join(str(p) for p in component.get("paths", []))
            tests = ", ".join(str(p) for p in component.get("test_locations", []))
            lines.append(f"- {component.get('name')}: {paths or 'no paths given'}")
            if tests:
                lines.append(f"    tests: {tests}")
        if inventory.baseline_commands:
            lines.append(
                f"  baseline commands: {', '.join(inventory.baseline_commands)} "
                f"(currently: {inventory.baseline_status})"
            )
        if inventory.known_failures:
            lines.append(
                f"  already failing before any new work: "
                f"{', '.join(inventory.known_failures)}"
            )
        lines.append("")

    if instructions:
        lines.append("Additional instructions from the operator (these win):")
        lines.append(instructions)
        lines.append("")

    lines.append(f"Write your findings as JSON to this exact path:")
    lines.append(f"  {artifact_path}")
    lines.append("")
    lines.append("The file must contain JSON only — no prose, no code fence.")
    lines.append("")
    lines.append("Schema:")
    lines.append(stage.schema)
    lines.append("")
    lines.append(stage.rules)
    if stage.out_of_scope:
        lines.append("")
        lines.append(f"Out of scope: {stage.out_of_scope}")
    lines.append("")
    lines.append(
        "If you cannot write that file, print the same JSON to stdout inside a "
        "single ```json fenced block instead."
    )
    lines.append("")
    lines.append("Write no code and change no file other than that artifact.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# running a stage


@dataclass
class Result:
    """What one stage run produced."""

    stage: str
    path: Path
    exit_code: int = 0
    error: str = ""
    artifact: Any = None
    #: True when the artifact was already on disk and the stage was not re-run
    reused: bool = False

    @property
    def ok(self) -> bool:
        return not self.error and self.artifact is not None


def _artifact_text(directory: Path, path: Path) -> tuple[str, bool] | None:
    """The artifact, and whether it came from the file writ asked for."""
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text, True
    stdout = directory / "stdout.log"
    if stdout.exists():
        from .planning import extract_json

        raw = stdout.read_text(encoding="utf-8", errors="replace")
        embedded = extract_json(raw)
        if embedded:
            path.write_text(embedded + "\n", encoding="utf-8")
            return embedded, False
    return None


def run_stage(
    stage: Stage,
    *,
    root: Path,
    doc: Path,
    directory: Path,
    artifacts: Artifacts,
    agent: str,
    agent_args: list[str] | None = None,
    model: str | None = None,
    timeout: int | None = None,
    cwd: str | None = None,
    instructions: str | None = None,
    context: dict[str, Any] | None = None,
    refresh: bool = False,
    stream: bool = False,
    mirror_lock: threading.Lock | None = None,
    on_start: Callable[[Stage, agents.ResolvedAgent], None] | None = None,
    on_launch: Callable[[Stage, agents.ResolvedAgent, Path], None] | None = None,
) -> Result:
    """Run one analysis stage, or reuse the artifact it already wrote.

    Reuse is the default because these stages are expensive and independent of
    each other's failures: a pipeline that died at synthesis should not pay for
    three analyses again to retry the one thing that broke. `refresh` re-runs it.
    """
    known = [req.id for req in artifacts.inventory_requirements]
    artifact_path = directory / stage.artifact
    if not refresh:
        try:
            existing = read(stage, directory, known=known)
        except WritError:
            # A malformed artifact on disk is not reusable, but it is also not a
            # reason to refuse: re-running the stage is exactly the repair.
            existing = None
        if existing is not None:
            return Result(
                stage=stage.name,
                path=artifact_path,
                artifact=existing,
                reused=True,
            )
    if artifact_path.exists():
        artifact_path.unlink()
    resolved = agents.resolve(agent, list(agent_args or []), model, events=True)
    where = directory / stage.name
    where.mkdir(parents=True, exist_ok=True)
    prompt = build_prompt(
        stage,
        root=root.resolve(),
        doc=doc,
        artifact_path=artifact_path,
        artifacts=artifacts,
        instructions=instructions,
        context=context,
    )
    # Two hooks, and the difference between them is the lock. `on_start` prints,
    # so it is held behind `mirror_lock` to keep two concurrent stages from
    # interleaving halfway through a line. `on_launch` records, which means a state
    # transaction — an flock acquisition and two fsyncs — and holding the terminal
    # lock across that would stall the other stage's output on a file lock it has
    # no interest in. Worse, a contended state lock raises, and raising inside
    # `mirror_lock` would kill a stage over a bookkeeping write.
    if on_launch is not None:
        on_launch(stage, resolved, where)
    if on_start is not None:
        with mirror_lock if mirror_lock is not None else nullcontext():
            on_start(stage, resolved)
    result = Result(stage=stage.name, path=artifact_path)
    stop_reasons: list[str] = []
    try:
        result.exit_code = runner.run_agent(
            resolved.command,
            prompt,
            where,
            cwd or root,
            timeout,
            stream=stream,
            prefix=f"  {stage.name} | " if stream else "",
            event_shape=resolved.event_shape,
            stop_reasons=stop_reasons,
            mirror_lock=mirror_lock,
        )
    except FileNotFoundError:
        result.error = f"{stage.name} agent not found: {resolved.command[0]}"
        return result
    except WritError as exc:
        result.error = str(exc)
        return result

    written = _artifact_text(where, artifact_path)
    if written is None:
        if result.exit_code == 124 and not runner.produced_output(where):
            detail = agents.hang_hint(resolved)
        elif truncated(stop_reasons):
            # Named as what it is. A stage whose artifact nearly fills the model's
            # output allowance fails this way only sometimes — thinking length
            # varies per run — so the same command succeeding yesterday is not
            # evidence against it.
            detail = (
                f"exit {result.exit_code}, out of output budget: its last turn "
                "ended on the model's output ceiling, so the artifact was never "
                "written. A model with more output headroom is the fix"
            )
        else:
            detail = f"exit {result.exit_code}"
        result.error = (
            f"the {stage.name} stage wrote no artifact to {artifact_path} "
            f"({detail}; transcript: {where})"
        )
        return result
    text, _ = written
    try:
        result.artifact = load(stage, text, known=known, path=artifact_path)
    except WritError as exc:
        result.error = str(exc)
    return result


def waves(chosen: Iterable[Stage]) -> list[list[Stage]]:
    """The chosen stages grouped into what may run at the same time.

    A stage joins the current wave if none of the stages in it produce something
    it `needs`, and starts a new one otherwise. For the three analyses that means
    requirements and inventory together, then verification — which needs both — on
    its own. The grouping is derived from the declared dependencies rather than
    hardcoded, so a stage added later is placed by what it says it needs.

    Order is preserved, both between waves and within one, so a sequential run and
    a concurrent one visit the stages in the same order.
    """
    grouped: list[list[Stage]] = []
    current: list[Stage] = []
    produced: set[str] = set()
    for stage in chosen:
        if current and any(need in produced for need in stage.needs):
            grouped.append(current)
            current = []
        current.append(stage)
        produced.add(stage.name)
    if current:
        grouped.append(current)
    return grouped


def verify_coverage_ids(artifact: InventoryArtifact, *, known: Iterable[str]) -> None:
    """Re-check an inventory's coverage claims once the requirement ids exist.

    An inventory that ran beside the requirements stage was validated against no
    ids at all, because there were none yet. This is that validation, deferred:
    the claims still have to attach to real obligations, since a hallucinated
    `REQ-009` marked already-satisfied is how a requirement nothing asked for gets
    treated as done.

    Raises rather than dropping the entry. The stage was told to leave the field
    empty when it has no ids; one that filled it anyway did not misread the schema,
    it answered a question it had not been given, and the rest of its survey was
    written by the same reasoning.
    """
    ids = set(known)
    if not ids:
        return
    for index, entry in enumerate(artifact.existing_coverage):
        req_id = str(entry.get("requirement_id", "")).strip()
        if req_id not in ids:
            raise WritError(
                f"existing_coverage[{index}] claims coverage of {req_id}, which is "
                "not in the requirement inventory. The inventory stage ran beside "
                "the requirements stage, so it was told to leave existing_coverage "
                "empty; re-run it with --refresh, sequentially, to have it cite "
                "real ids."
            )


def run_pipeline(
    *,
    root: Path,
    doc: Path,
    directory: Path,
    chosen: Iterable[Stage],
    agent: str,
    agent_args: list[str] | None = None,
    model: str | None = None,
    timeout: int | None = None,
    cwd: str | None = None,
    instructions: str | None = None,
    context: dict[str, Any] | None = None,
    refresh: bool = False,
    stream: bool = False,
    parallel: bool = False,
    on_start: Callable[[Stage, agents.ResolvedAgent], None] | None = None,
    on_finish: Callable[[Result], None] | None = None,
    on_launch: Callable[[Stage, agents.ResolvedAgent, Path], None] | None = None,
) -> tuple[Artifacts, list[Result]]:
    """Run the analysis stages in order, stopping at the first that fails.

    Sequential and fail-fast by default, unlike the critics. A critic that fails
    costs one perspective on a plan that still exists; a stage that fails leaves
    the next stage with nothing to work from — verification cannot decide how to
    prove a list of obligations it was never given. Stopping at the failure means
    the error names the stage that actually broke.

    With `parallel`, stages that need nothing from each other run at once (see
    `waves`), and a wave that fails stops the pipeline as a single stage would.
    Only requirements and inventory qualify: the survey of the repository does not
    depend on what the document asks for. What it loses is the ability to say which
    stated obligation the existing code already discharges, because it has no ids
    to say it with — so it is told to leave `existing_coverage` empty, and the
    claims it would have made are checked for afterwards rather than trusted (see
    `verify_coverage_ids`). That is the trade: one agent's wall-clock against the
    `replanned-requirement` finding and the synthesizer's note about what is
    already covered.
    """
    directory.mkdir(parents=True, exist_ok=True)
    artifacts = read_all(directory)
    results: list[Result] = []

    def run(stage: Stage, lock: threading.Lock | None = None) -> Result:
        return run_stage(
            stage,
            root=root,
            doc=doc,
            directory=directory,
            artifacts=artifacts,
            agent=agent,
            agent_args=agent_args,
            model=model,
            timeout=timeout,
            cwd=cwd,
            instructions=instructions,
            context=context,
            refresh=refresh,
            stream=stream,
            mirror_lock=lock,
            on_start=on_start,
            on_launch=on_launch,
        )

    grouped = waves(chosen) if parallel else [[stage] for stage in chosen]
    lock = threading.Lock()
    for wave in grouped:
        if len(wave) == 1:
            landed = [(wave[0], run(wave[0]))]
        else:
            with futures.ThreadPoolExecutor(max_workers=len(wave)) as pool:
                submitted = {pool.submit(run, stage, lock): stage for stage in wave}
                done = {}
                for future in futures.as_completed(submitted):
                    stage = submitted[future]
                    try:
                        done[stage.name] = future.result()
                    except Exception as exc:  # pragma: no cover - defensive
                        done[stage.name] = Result(
                            stage=stage.name,
                            path=directory / stage.artifact,
                            error=f"the {stage.name} stage did not run: {exc}",
                        )
                # Reported in the order they were asked for, not the order they
                # finished, so the record of a run does not depend on which agent
                # happened to be quicker.
                landed = [(stage, done[stage.name]) for stage in wave]

        for stage, result in landed:
            if result.ok:
                setattr(artifacts, stage.name, result.artifact)

        # Deferred validation, once every artifact in the wave has landed and
        # before any of them is reported: an inventory that ran without the
        # requirement ids is only now checkable against them, and a stage whose
        # artifact does not survive that check did not succeed.
        if len(wave) > 1 and artifacts.inventory is not None:
            known = [req.id for req in artifacts.inventory_requirements]
            try:
                verify_coverage_ids(artifacts.inventory, known=known)
            except WritError as exc:
                for stage, result in landed:
                    if stage.name == "inventory":
                        result.error = str(exc)
                        result.artifact = None
                artifacts.inventory = None

        for stage, result in landed:
            results.append(result)
            if on_finish is not None:
                on_finish(result)
        if any(not result.ok for _, result in landed):
            break
    return artifacts, results


# --------------------------------------------------------------------------
# holding the synthesizer to the analyses


def reconcile(document: Any, artifacts: Artifacts) -> list[Finding]:
    """Check that the synthesized plan honoured the artifacts it was given.

    This is the check that makes staging worth its cost. A single-shot planner
    writes the requirement inventory and the tasks in one response, so the two can
    never disagree — it does not record an obligation it was not planning to
    cover, and the omission leaves no trace anywhere. Fixing the inventory first
    makes the disagreement possible, and therefore findable:

    - a requirement in the inventory and not in the plan was **dropped**
    - a requirement in the plan and not in the inventory was **invented**
    - a requirement the verification stage could describe, whose covering tasks
      cite none of it, has a **bar nobody worked out**
    - a requirement the inventory evidenced as already done, planned again, is
      **duplicated work**

    Findings, not errors. A dropped `must` holds the plan through the normal
    approval gate, which is the same treatment a structural defect gets and can
    be overruled the same way with the same record. Raising instead would throw
    away a plan that is mostly right and offer nothing to look at.
    """
    if artifacts.requirements is None:
        return []
    findings: list[Finding] = []
    expected = {req.id: req for req in artifacts.requirements.requirements}
    stated = {req.id: req for req in getattr(document, "requirements", [])}

    for req_id, requirement in expected.items():
        if req_id in stated:
            continue
        must = requirement.priority == "must"
        findings.append(
            Finding(
                severity="error" if must else "warning",
                category="dropped-requirement",
                where=req_id,
                requirement_ids=[req_id],
                source="stage:synthesis",
                message=(
                    f"{req_id} is in the requirement inventory but not in the "
                    f"plan: {_shorten(requirement.text)}"
                ),
                suggested_action=(
                    "The synthesizer may not drop an obligation the requirements "
                    "stage recorded. Cover it with a task, or state it as "
                    "existing with evidence, or out-of-scope with a reason."
                ),
            )
        )

    for req_id, requirement in stated.items():
        if req_id in expected:
            continue
        findings.append(
            Finding(
                severity="error",
                category="invented-requirement",
                where=req_id,
                requirement_ids=[req_id],
                source="stage:synthesis",
                message=(
                    f"the plan states {req_id}, which the requirements stage never "
                    f"recorded: {_shorten(requirement.text)}"
                ),
                suggested_action=(
                    "Either the design document states this and the requirements "
                    "stage missed it — re-run that stage — or the plan invented "
                    "work nothing asked for."
                ),
            )
        )

    covered = _covered_requirements(document)
    if artifacts.verification is not None:
        for req_id in sorted(artifacts.verification.covered & set(stated)):
            tasks = covered.get(req_id, [])
            if not tasks:
                continue
            methods = artifacts.verification.methods_for(req_id)
            cited = [
                token
                for method in methods
                for token in (
                    str(method.get("command", "")).strip(),
                    str(method.get("location", "")).strip(),
                )
                if token
            ]
            if not cited:
                continue
            text = " ".join(
                criterion.lower()
                for task in tasks
                for criterion in task.acceptances
            )
            if _cites_verification(text, cited):
                continue
            findings.append(
                Finding(
                    severity="warning",
                    category="unused-verification",
                    where=", ".join(task.ref or task.title for task in tasks),
                    requirement_ids=[req_id],
                    source="stage:synthesis",
                    message=(
                        f"the verification stage worked out how to demonstrate "
                        f"{req_id} ({_shorten(cited[0])}), and no task covering it "
                        "states that as a bar"
                    ),
                    suggested_action=(
                        "Use the verification that was already established, or say "
                        "in the task's notes why a different bar is better."
                    ),
                )
            )

    if artifacts.inventory is not None:
        satisfied = set(artifacts.inventory.satisfied())
        for req_id in sorted(satisfied):
            requirement = stated.get(req_id)
            if requirement is None or requirement.status == "existing":
                continue
            if not covered.get(req_id):
                continue
            evidence = next(
                (
                    str(entry.get("evidence", ""))
                    for entry in artifacts.inventory.existing_coverage
                    if entry.get("requirement_id") == req_id
                ),
                "",
            )
            findings.append(
                Finding(
                    severity="warning",
                    category="replanned-requirement",
                    where=req_id,
                    requirement_ids=[req_id],
                    source="stage:synthesis",
                    message=(
                        f"the inventory found {req_id} already satisfied "
                        f"({_shorten(evidence)}), and the plan has tasks for it "
                        "anyway"
                    ),
                    suggested_action=(
                        "Mark it `existing` with that evidence, or say in the "
                        "task's notes why the existing implementation is not "
                        "sufficient."
                    ),
                )
            )
    return findings


def _cites_verification(text: str, cited: Iterable[str]) -> bool:
    """Whether a task's criteria reference the verification that was worked out.

    A whole-string substring match was too literal to be useful. The verification
    stage writes `python3 -m pytest -q tests/test_fts.py::test_bm25_lexical_search`
    and the task states "`pytest tests/test_fts.py::test_bm25_lexical_search`
    passes" — the same bar, reported as ignored, 32 times on one plan. What actually
    identifies a method is its *target*: the test node id, or the file path. So the
    runner prefix, its flags, and the interpreter are stripped and the target is what
    is compared.
    """
    lowered = text.lower()
    for token in cited:
        candidate = token.lower().strip()
        if not candidate:
            continue
        if candidate in lowered:
            return True
        for target in _verification_targets(candidate):
            if target and target in lowered:
                return True
    return False


#: a pytest node id, or a path with a test-ish extension, inside a longer command
_TARGET_PATTERN = re.compile(
    r"[\w./-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|kt|rb|sql|sh)(?:::[\w:\[\]-]+)?"
)


def _verification_targets(command: str) -> list[str]:
    """The parts of a verification method that identify what it checks.

    The node id if there is one, then the path, then the bare test name — each is
    something a criterion could reasonably cite on its own. Flags and the runner
    itself are not: every pytest command shares them, so matching on those would
    pass any criterion that mentioned pytest at all.
    """
    targets: list[str] = []
    for match in _TARGET_PATTERN.findall(command):
        targets.append(match)
        if "::" in match:
            path, _, node = match.partition("::")
            targets.append(path)
            leaf = node.rsplit("::", 1)[-1]
            if leaf:
                targets.append(leaf)
    return targets


def _covered_requirements(document: Any) -> dict[str, list[Any]]:
    """Which planned tasks claim each requirement."""
    covered: dict[str, list[Any]] = {}
    for milestone in getattr(document, "milestones", []):
        for task in milestone.tasks:
            for req_id in getattr(task, "requirement_ids", []) or []:
                covered.setdefault(req_id, []).append(task)
    return covered


def _shorten(text: str, limit: int = 90) -> str:
    collapsed = " ".join(str(text).split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def record(
    data: dict[str, Any],
    *,
    plan_id: str,
    directory: Path,
    results: Iterable[Result],
    artifacts: Artifacts,
) -> dict[str, Any]:
    """Write the pipeline's artifacts and outcomes into plan state.

    Kept on the plan record rather than only on disk so that `writ status` and the
    API can say which analyses a plan rests on without reading the filesystem, and
    so a re-plan of the same project leaves the earlier pipeline's provenance
    intact.
    """
    from . import plans

    record = plans.plan_status(data)
    pipeline = record.setdefault("pipeline", {})
    pipeline["plan_id"] = plan_id
    pipeline["directory"] = str(directory)
    pipeline["at"] = utcnow()
    stages = pipeline.setdefault("stages", {})
    for result in results:
        stages[result.stage] = {
            "artifact": str(result.path),
            "reused": result.reused,
            "exit_code": result.exit_code,
            "error": result.error,
            "at": utcnow(),
        }
    if artifacts.requirements is not None:
        pipeline["requirement_ids"] = artifacts.requirements.ids
        pipeline["ambiguities"] = len(artifacts.requirements.ambiguities)
        pipeline["unresolved_ambiguities"] = len(
            artifacts.requirements.open_questions
        )
    if artifacts.inventory is not None:
        pipeline["baseline"] = {
            "commands": artifacts.inventory.baseline_commands,
            "status": artifacts.inventory.baseline_status,
            "known_failures": artifacts.inventory.known_failures,
        }
    if artifacts.verification is not None:
        pipeline["undemonstrable"] = [
            str(entry.get("requirement_id", ""))
            for entry in artifacts.verification.undemonstrable
        ]
    return pipeline
