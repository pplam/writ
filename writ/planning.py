"""Generative planning: a coding agent turns a design doc into features.

Text extraction (`planner.py`) can only repeat what a document already says in a
shape it recognises. Planning is a judgement call: which subsystems the work
splits into, what each one owns, which interfaces connect them, and what the
real bar is. So `writ plan` hands the design doc and the repository to a coding
agent and asks for a plan as JSON: capabilities, and the features that build
them (docs/planning-redesign.md §4). Edges are not written by the agent; they
are derived from the features' contracts (`contracts.py`).

The older milestone/task shape still loads, for `--extract` and for drafts
written before features existed.

The agent's freedom stops at the schema. Everything it returns is validated
before it reaches project state, the raw artifact is kept under
`.writ/plans/<plan-id>/`, and a rejected plan says exactly which field was wrong.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import agents, contracts, plancheck, planfiles, prompts, runner, state
from .stream import truncated
from .plancheck import Finding, Requirement
from .planner import (
    DesignDocs,
    PlannedMilestone,
    PlannedTask,
    doc_list,
    doc_names,
    find_section,
)
from .state import WritError

#: keys accepted for each field, in priority order — model output varies
TASK_ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id", "task_id", "ref"),
    "title": ("title", "name", "task"),
    "acceptances": (
        "acceptances",
        "acceptance",
        "acceptance_criteria",
        "criteria",
        "gates",
    ),
    "depends_on": ("depends_on", "dependencies", "deps", "after"),
    "allowed": ("allowed", "allow", "allowed_paths", "files"),
    "forbidden": ("forbidden", "forbid", "forbidden_paths"),
    "design_section": ("design_section", "section", "heading"),
    "notes": ("notes", "intent", "approach", "summary"),
    "requirement_ids": (
        "requirement_ids",
        "requirements",
        "requirement",
        "covers",
        "reqs",
    ),
}
REQUIREMENT_ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id", "requirement_id", "ref"),
    "text": ("text", "requirement", "statement", "description", "title"),
    "priority": ("priority", "importance", "level"),
    "status": ("status", "disposition", "coverage"),
    "source": ("source", "section", "design_section", "where", "quote"),
    "evidence": ("evidence", "proof", "existing_evidence"),
    "reason": ("reason", "justification", "why"),
    "details": ("details", "obligations", "sub_requirements"),
    "verification": ("verification", "verify", "verification_hints", "how"),
}
FEATURE_ALIASES: dict[str, tuple[str, ...]] = {
    "goal": ("goal", "outcome", "summary"),
    "owns": ("owns", "component", "components", "owned"),
    "provides": ("provides", "exports", "provided"),
    "consumes": ("consumes", "imports", "uses", "consumed"),
    "notes": ("notes", "risks", "intent"),
}
MILESTONE_ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id", "milestone_id", "ref"),
    "title": ("title", "name", "milestone"),
    "notes": ("notes", "summary", "goal", "intent"),
    "tasks": ("tasks", "items"),
}

SCHEMA = """\
{
  "requirements": [
    {
      "id": "REQ-001",
      "text": "one capability the document asks for, named as a whole",
      "source": "exact heading it came from",
      "priority": "must" | "should" | "may",
      "status": "planned" | "existing" | "out-of-scope" | "deferred",
      "evidence": "status existing only: the test or code that satisfies it",
      "reason": "status out-of-scope or deferred only: why it is not this plan's",
      "details": ["a finer obligation this capability contains", "another"]
    }
  ],
  "features": [
    {
      "id": "store",
      "title": "Event store",
      "goal": "one paragraph: what exists when this feature is done",
      "design_section": "exact heading from the design document",
      "requirement_ids": ["REQ-003", "REQ-004"],
      "owns": ["pkg/store/"],
      "provides": ["EventLog: append(event) -> offset; read(from) -> events"],
      "consumes": ["Config: typed settings loaded from the project file"],
      "acceptance": [
        "an observable behaviour a reviewer can check",
        "another one"
      ],
      "notes": "ambiguities, risks, conventions to follow"
    }
  ]
}"""

RULES = """\
Rules:
- Start with `requirements`. Write down the capabilities the document asks for:
  roughly one per major section, 10 to 25 for a full design and fewer for a
  short one, never more than 25. A capability is something a user of the system
  could name ("durable event store"), not one sentence of the document. Put the
  finer obligations it contains — each behaviour, constraint, limit — in its
  `details`, so nothing the document states is lost, but do not give them ids.
- Every requirement must end somewhere: a feature names it in `requirement_ids`,
  or it is `existing` with evidence naming the code that already satisfies it, or
  it is `out-of-scope`/`deferred` with a reason.
- A feature is a subsystem one agent can build on its own, from an empty
  directory to working, tested behaviour. Aim for 4 to 12 of them. Do not split a
  subsystem into steps; the agent that builds it decides its own steps.
- `owns` names the component or directory the feature builds, never a list of
  files. File names are decided when the feature runs, not now. Two features
  must not own the same directory.
- `provides` and `consumes` name the interfaces between features, one line each,
  as `Name: what it is`. The name before the colon is what connects them: a
  feature that consumes `EventLog` depends on the feature that provides
  `EventLog`. Every interface a feature consumes must be provided by exactly one
  feature in this plan, unless it already exists in the repository — then leave
  it out of `consumes` and say so in `notes`. Keep the graph acyclic.
- Do not write `depends_on` between features. Writ derives the edges from
  `provides` and `consumes`. An edge onto an existing task (listed below, if
  any) may be written in `depends_on`.
- `acceptance` states 3 to 6 behaviours a reviewer can observe when the feature
  is done: what a caller can do, what it refuses, what survives a restart. Do not
  name test files, test functions, or source files.
- Prefer bars the document already states. Where the document is ambiguous, or
  contradicts what the repository already does, record it in the feature's
  notes rather than silently picking a reading.
- design_section must be a heading that appears verbatim in the design document.
  Use "Parent / Child" for a nested heading.
- Skip work the repository has already done.

Write no code and change no file other than the plan JSON. You are planning."""

#: what changes when the plan is synthesized from analyses rather than written cold.
#:
#: Almost all of it is about the requirement inventory being *fixed*. A single-shot
#: planner writes the inventory and the features together, so the two cannot
#: disagree. Splitting the stages makes that disagreement possible and therefore
#: detectable, and these rules are what the synthesizer is held to. `reconcile`
#: checks them afterwards.
SYNTHESIS_RULES = """\
Because the analyses above are established, this plan is held to them:
- Copy the requirement inventory into your `requirements` array exactly: every id
  that was given to you, with the same text, priority, source and details. You
  may change a `status` — to `existing` if the repository summary shows it is
  already done, or to `out-of-scope`/`deferred` with a reason — but you may not
  drop an entry or add one. A requirement missing from your plan is reported as
  dropped, and an id that was not given to you is reported as invented; both hold
  the plan.
- Where the repository summary found a requirement already satisfied with
  evidence, mark it `existing` and carry that evidence across rather than
  planning the work again.
- Match the repository summary's language, components and conventions: a
  feature's `owns` should sit where this repository puts that kind of code.
- Where the requirements analysis recorded an unresolved ambiguity, note it in the
  notes of the feature it affects. Do not silently pick a reading."""


def _artifact_refs(folder: Path, artifacts: Any) -> list[prompts.Ref]:
    """The analyses, as files to read, each with what it is binding for."""
    refs: list[prompts.Ref] = []
    if getattr(artifacts, "requirements", None) is not None:
        refs.append(
            prompts.Ref(
                folder / "requirements.json",
                "REQUIREMENTS, the fixed inventory. Reproduce every id in your "
                "`requirements` array; do not add, drop, or renumber. Its "
                "unresolved `ambiguities` go in the affected feature's notes",
            )
        )
    if getattr(artifacts, "inventory", None) is not None:
        refs.append(
            prompts.Ref(
                folder / "inventory.json",
                "REPOSITORY, the short summary of what is already here: "
                "language, test command, test directories, baseline, and the "
                "components new work attaches to",
            )
        )
    return refs


# --------------------------------------------------------------------------
# prompt


def new_plan_id(doc: DesignDocs) -> str:
    docs = doc_list(doc)
    return planfiles.new_id(docs[0] if docs else None)


def synthesis_dir(root: Path, plan_id: str) -> Path:
    """Where the synthesizer's (or single-shot planner's) transcript goes."""
    return state.plan_dir(root, plan_id) / "synthesis"


def build_prompt(
    *,
    root: Path,
    doc: DesignDocs,
    plan_path: Path,
    instructions: str | None = None,
    context: dict[str, Any] | None = None,
    artifacts: Any = None,
) -> str:
    """Compose the planning prompt: read the doc and repo, emit plan JSON.

    With `artifacts` — the analyses from `writ/analysis.py` — this becomes the
    synthesis stage instead, and the prompt changes shape accordingly: the agent
    is no longer asked to work out what the document requires, what the repository
    holds. Those are given, and its one job is the decomposition. The requirement inventory in particular arrives as a fixed
    list it must account for, rather than one it writes for itself.
    """
    context = context or {}
    synthesizing = artifacts is not None and getattr(artifacts, "requirements", None)
    lines: list[str] = []
    if synthesizing:
        lines.append(
            "You are decomposing already-analysed work into an executable plan "
            "for this repository. You are not implementing it, and you are not "
            "re-deciding what it requires."
        )
    else:
        lines.append(
            "You are planning implementation work for this repository. "
            "You are not implementing it."
        )
    lines.append("")
    lines.append(prompts.root_line(root))
    lines.append("")
    first = prompts.design_refs(doc, "the design document")
    if synthesizing:
        first.extend(_artifact_refs(plan_path.parent, artifacts))
    as_needed = [
        prompts.Ref(path, "another document already registered for this project")
        for path in prompts.other_docs(context.get("design_docs", []), doc)
    ]
    lines.extend(prompts.references(root, first=first, as_needed=as_needed))
    lines.extend(prompts.design_note(doc))
    if synthesizing:
        lines.append(
            "Two analyses have already been done for you: what the document "
            "requires, and a short summary of what the repository already is. "
            "Read them as established. Read the design document too — the "
            "analyses are a reading of it, not a replacement for it."
        )
    else:
        lines.append(
            "Read the design document in full, then read enough of the repository to "
            "ground the plan in existing conventions and work that is already done."
        )
    lines.append("")
    existing_tasks = context.get("tasks", [])
    if existing_tasks:
        lines.append(
            "This project already has a plan. Plan only the work that is missing. "
            "A new feature may consume an interface an existing feature provides, "
            "or name an existing task in `depends_on`:"
        )
        for entry in existing_tasks:
            line = f"- {entry['id']} [{entry['status']}] {entry['title']}"
            if entry.get("provides"):
                line += f" (provides: {'; '.join(entry['provides'])})"
            lines.append(line)
        lines.append("")
    if instructions:
        lines.append("Additional instructions from the operator (these win):")
        lines.append(instructions)
        lines.append("")
    lines.extend(prompts.output(root, "plan", plan_path))
    lines.append("")
    lines.append("The file must contain JSON only — no prose, no code fence.")
    lines.append("")
    lines.append("Schema:")
    lines.append(SCHEMA)
    lines.append("")
    lines.append(RULES)
    if synthesizing:
        lines.append("")
        lines.append(SYNTHESIS_RULES)
    lines.append("")
    lines.append(
        "If you cannot write that file, print the same JSON to stdout inside a "
        "single ```json fenced block instead."
    )
    lines.append("")
    lines.append(
        "When the file is written, summarise in a few lines: how many features, "
        "the interfaces that connect them, and anything in the document you could "
        "not turn into a checkable behaviour."
    )
    return "\n".join(lines)


def plan_context(data: dict[str, Any]) -> dict[str, Any]:
    """The slice of project state the planning agent needs to see."""
    return {
        "design_docs": list(data.get("design_docs", [])),
        "milestone_offset": len(data.get("milestones", {})),
        "tasks": [
            {
                "id": task_id,
                "status": data["tasks"][task_id]["status"],
                "title": data["tasks"][task_id]["title"],
                "provides": list(data["tasks"][task_id].get("provides") or []),
            }
            for task_id in sorted(data.get("tasks", {}))
        ],
    }


# --------------------------------------------------------------------------
# running the planning agent


def generate(
    *,
    root: Path,
    doc: DesignDocs,
    agent: str,
    agent_args: list[str],
    model: str | None,
    timeout: int | None,
    cwd: str | None,
    instructions: str | None,
    context: dict[str, Any],
    stream: bool = False,
    on_start=None,
    artifacts: Any = None,
    plan_id: str | None = None,
) -> tuple[PlanDocument, Path, int]:
    """Run the planning agent and return the validated plan it produced.

    With `artifacts`, this is the synthesis stage of a staged pipeline and writes
    into that pipeline's directory, beside the analyses it was built from. Without
    them it is the older single-shot planner, which decides everything at once.
    """
    resolved = agents.resolve(agent, agent_args, model, events=True)
    plan_id = plan_id or new_plan_id(doc)
    # The draft sits beside the analyses it was built from; the synthesizer's
    # transcript gets its own folder, as each analysis stage's does. `plan.json`
    # is not this: it is the committed index, which speaks writ's ids.
    plan_path = state.plan_dir(root, plan_id) / planfiles.DRAFT_FILENAME
    directory = synthesis_dir(root, plan_id)
    directory.mkdir(parents=True, exist_ok=True)
    prompt = build_prompt(
        root=root.resolve(),
        doc=doc,
        plan_path=plan_path,
        instructions=instructions,
        context=context,
        artifacts=artifacts,
    )
    if on_start is not None:
        on_start(resolved, directory)
    stop_reasons: list[str] = []
    try:
        code = runner.run_agent(
            resolved.command,
            prompt,
            directory,
            cwd or root,
            timeout,
            stream=stream,
            prefix="  | " if stream else "",
            event_shape=resolved.event_shape,
            stop_reasons=stop_reasons,
        )
    except FileNotFoundError as exc:
        raise WritError(f"planning agent not found: {resolved.command[0]}") from exc

    text = _plan_text(directory, plan_path)
    if text is None:
        raise WritError(
            _no_plan_message(resolved, directory, code, stop_reasons=stop_reasons)
        )
    try:
        document = load_document(text)
    except WritError as exc:
        raise WritError(f"{exc} (plan artifact: {plan_path})") from exc
    return document, plan_path, code


def _no_plan_message(
    resolved: agents.ResolvedAgent,
    directory: Path,
    code: int,
    stop_reasons: list[str] | None = None,
) -> str:
    """Explain a planner that produced nothing, and why it may have hung."""
    produced_output = runner.produced_output(directory)
    if code == 124:
        detail = "the planning agent was killed for exceeding its timeout"
        if not produced_output:
            detail += f"\n  {agents.hang_hint(resolved)}"
    elif truncated(stop_reasons or []):
        # The agent ran, worked, and was cut off mid-turn having spent its whole
        # output allowance. Said explicitly because the alternative reading —
        # "it wrote nothing, so it never started" — sends whoever reads this to
        # check a model id and an API key that were never the problem.
        detail = (
            "the planning agent ran out of output budget before it could write "
            "the plan"
        )
        detail += (
            "\n  its last turn ended on the model's output ceiling, so nothing "
            "was written and nothing was printed"
        )
        detail += (
            "\n  a plan for this many requirements needs a model with more output "
            "headroom; the same model on another provider may have far more"
        )
    else:
        detail = f"the planning agent exited {code} without producing a plan"
        if not produced_output:
            detail += (
                f"\n  it wrote nothing at all, invoked as `{resolved.display}`"
            )
    return f"{detail}\n  transcript: {directory}"


def _plan_text(directory: Path, plan_path: Path) -> str | None:
    """Prefer the plan file; fall back to JSON printed on stdout."""
    if plan_path.exists():
        text = plan_path.read_text(encoding="utf-8").strip()
        if text:
            return text
    stdout = directory / "stdout.log"
    if not stdout.exists():
        return None
    embedded = extract_json(stdout.read_text(encoding="utf-8", errors="replace"))
    if embedded is None:
        return None
    # keep the artifact wherever it came from, so --from-plan can reuse it
    plan_path.write_text(embedded + "\n", encoding="utf-8")
    return embedded


FENCE_PATTERN = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def extract_json(text: str) -> str | None:
    """Recover a JSON object from agent chatter: fenced block, else braces."""
    fenced = FENCE_PATTERN.findall(text)
    for candidate in reversed(fenced):
        if _is_json(candidate):
            return candidate
    start = text.find("{")
    while start != -1:
        candidate = _balanced(text, start)
        if candidate and _is_json(candidate):
            return candidate
        start = text.find("{", start + 1)
    return None


def _is_json(candidate: str) -> bool:
    try:
        json.loads(candidate)
    except json.JSONDecodeError:
        return False
    return True


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


# --------------------------------------------------------------------------
# validation


@dataclass
class PlanDocument:
    """A validated plan: what the design requires, and the work that covers it.

    The requirement inventory is the half `load_plan` used to throw away. It is
    optional — `--extract` produces none, and so does any plan written before
    Writ asked for one — so an empty list means "this plan makes no claim about
    coverage", not "this plan covers nothing. Everything that reads it treats
    those two differently.
    """

    milestones: list[PlannedMilestone] = field(default_factory=list)
    requirements: list[Requirement] = field(default_factory=list)

    @property
    def features(self) -> bool:
        """Whether this is a features plan rather than milestones and tasks."""
        return any(milestone.loose for milestone in self.milestones)

    @property
    def requirement_ids(self) -> set[str]:
        return {requirement.id for requirement in self.requirements}


def read_plan(path: Path) -> list[PlannedMilestone]:
    if not path.exists():
        raise WritError(f"plan file not found: {path}")
    return load_plan(path.read_text(encoding="utf-8"))


def read_document(path: Path) -> PlanDocument:
    if not path.exists():
        raise WritError(f"plan file not found: {path}")
    return load_document(path.read_text(encoding="utf-8"))


def load_plan(text: str) -> list[PlannedMilestone]:
    """Just the milestones, for callers that do not care about coverage."""
    return load_document(text).milestones


def load_document(text: str) -> PlanDocument:
    """Validate a plan document: its requirement inventory and its milestones."""
    stripped = text.strip()
    if not stripped:
        raise WritError("the plan is empty")
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        recovered = extract_json(stripped)
        if recovered is None:
            raise WritError(f"the plan is not valid JSON: {exc}") from exc
        payload = json.loads(recovered)

    raw_milestones = payload
    requirements: list[Requirement] = []
    if isinstance(payload, dict):
        requirements = _requirements(payload.get("requirements"))
        if payload.get("features") is not None:
            return PlanDocument(
                milestones=[_features(payload["features"])],
                requirements=requirements,
            )
        raw_milestones = _pick(payload, MILESTONE_ALIASES["tasks"] + ("milestones",))
        if raw_milestones is None:
            raise WritError("the plan has no `features` list")
    if not isinstance(raw_milestones, list) or not raw_milestones:
        raise WritError("`milestones` must be a non-empty list")

    milestones: list[PlannedMilestone] = []
    seen_refs: set[str] = set()
    for index, raw in enumerate(raw_milestones):
        where = f"milestones[{index}]"
        if not isinstance(raw, dict):
            raise WritError(f"{where} must be an object")
        title = _text(_pick(raw, MILESTONE_ALIASES["title"]), f"{where}.title")
        raw_tasks = _pick(raw, MILESTONE_ALIASES["tasks"])
        if not isinstance(raw_tasks, list) or not raw_tasks:
            raise WritError(f"{where}.tasks must be a non-empty list")
        milestone = PlannedMilestone(
            title=title,
            section=_optional_text(_pick(raw, TASK_ALIASES["design_section"])) or title,
            ref=_optional_text(_pick(raw, MILESTONE_ALIASES["id"])),
            notes=_optional_text(_pick(raw, MILESTONE_ALIASES["notes"])) or "",
        )
        for task_index, raw_task in enumerate(raw_tasks):
            task = _task(raw_task, f"{where}.tasks[{task_index}]", milestone.title)
            if task.ref:
                if task.ref in seen_refs:
                    raise WritError(f"duplicate task id in the plan: {task.ref}")
                seen_refs.add(task.ref)
            milestone.tasks.append(task)
        milestones.append(milestone)
    _check_refs(milestones)
    return PlanDocument(milestones=milestones, requirements=requirements)


def _features(value: Any) -> PlannedMilestone:
    """Validate a features list into one loose group, with derived edges.

    Each feature needs a title and a bar. A feature the author left unnamed gets
    a positional ref, because edges are keyed by ref and a derived edge onto a
    nameless feature would otherwise have nothing to point at.
    """
    if not isinstance(value, list) or not value:
        raise WritError("`features` must be a non-empty list")
    group = PlannedMilestone(title="Features", section="", loose=True)
    seen: set[str] = set()
    for index, raw in enumerate(value):
        where = f"features[{index}]"
        task = _task(raw, where, "")
        if not task.ref:
            task.ref = f"feature-{index + 1}"
        if task.ref in seen:
            raise WritError(f"duplicate feature id in the plan: {task.ref}")
        seen.add(task.ref)
        task.feature = True
        task.goal = _optional_text(_pick(raw, FEATURE_ALIASES["goal"])) or ""
        task.owns = _strings(_pick(raw, FEATURE_ALIASES["owns"]), f"{where}.owns")
        task.provides = _strings(
            _pick(raw, FEATURE_ALIASES["provides"]), f"{where}.provides"
        )
        task.consumes = _strings(
            _pick(raw, FEATURE_ALIASES["consumes"]), f"{where}.consumes"
        )
        task.notes = _optional_text(_pick(raw, FEATURE_ALIASES["notes"])) or ""
        if not task.stated_section:
            task.section = task.title
        # The fence until commit adds the repository's test directories.
        task.allowed = contracts.fence(task.owns, task.allowed)
        group.tasks.append(task)
    derived = contracts.edges(
        {
            task.ref: {"provides": task.provides, "consumes": task.consumes}
            for task in group.tasks
        }
    )
    for task in group.tasks:
        for dep in derived.get(task.ref, []):
            if dep not in task.depends_on:
                task.depends_on.append(dep)
    _check_refs([group])
    return group


def load_requirement_inventory(value: Any) -> list[Requirement]:
    """Validate a requirement inventory from any source.

    Public because the requirements *stage* (`writ/analysis.py`) writes the same
    object in its own artifact, and a second validator would let an inventory be
    legal in one file and illegal in the other.
    """
    return _requirements(value)


def _requirements(value: Any) -> list[Requirement]:
    """Validate the requirement inventory, or accept its absence.

    Absent is legal: `--extract` states no requirements, and neither does a plan
    from before Writ asked for them. Present but malformed is not, because a
    coverage check run against a broken inventory would report absences that are
    really parse failures — which is worse than no coverage check at all.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise WritError("`requirements` must be a list")
    requirements: list[Requirement] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        where = f"requirements[{index}]"
        if not isinstance(raw, dict):
            raise WritError(f"{where} must be an object")
        req_id = _text(_pick(raw, REQUIREMENT_ALIASES["id"]), f"{where}.id")
        if req_id in seen:
            raise WritError(f"duplicate requirement id in the plan: {req_id}")
        seen.add(req_id)
        status = (
            _optional_text(_pick(raw, REQUIREMENT_ALIASES["status"])) or "planned"
        ).lower()
        if status not in plancheck.REQUIREMENT_STATUSES:
            raise WritError(
                f"{where}.status is {status!r}; expected one of "
                f"{', '.join(plancheck.REQUIREMENT_STATUSES)}"
            )
        requirements.append(
            Requirement(
                id=req_id,
                text=_text(_pick(raw, REQUIREMENT_ALIASES["text"]), f"{where}.text"),
                priority=(
                    _optional_text(_pick(raw, REQUIREMENT_ALIASES["priority"])) or "must"
                ).lower(),
                status=status,
                source=_optional_text(_pick(raw, REQUIREMENT_ALIASES["source"])) or "",
                evidence=_optional_text(_pick(raw, REQUIREMENT_ALIASES["evidence"])) or "",
                reason=_optional_text(_pick(raw, REQUIREMENT_ALIASES["reason"])) or "",
                details=_strings(
                    _pick(raw, REQUIREMENT_ALIASES["details"]), f"{where}.details"
                ),
                verification=_strings(
                    _pick(raw, REQUIREMENT_ALIASES["verification"]),
                    f"{where}.verification",
                ),
            )
        )
    return requirements


def _task(raw: Any, where: str, milestone_title: str) -> PlannedTask:
    if not isinstance(raw, dict):
        raise WritError(f"{where} must be an object")
    title = _text(_pick(raw, TASK_ALIASES["title"]), f"{where}.title")
    acceptances = _acceptances(
        _pick(raw, TASK_ALIASES["acceptances"]), f"{where}.acceptances"
    )
    section = _optional_text(_pick(raw, TASK_ALIASES["design_section"]))
    return PlannedTask(
        title=title,
        acceptances=acceptances,
        section=section or f"{milestone_title} / {title}",
        ref=_optional_text(_pick(raw, TASK_ALIASES["id"])),
        notes=_optional_text(_pick(raw, TASK_ALIASES["notes"])) or "",
        depends_on=_strings(_pick(raw, TASK_ALIASES["depends_on"]), f"{where}.depends_on"),
        allowed=_strings(_pick(raw, TASK_ALIASES["allowed"]), f"{where}.allowed"),
        forbidden=_strings(_pick(raw, TASK_ALIASES["forbidden"]), f"{where}.forbidden"),
        requirement_ids=_strings(
            _pick(raw, TASK_ALIASES["requirement_ids"]), f"{where}.requirement_ids"
        ),
        stated_section=bool(section),
    )


def _acceptances(value: Any, where: str) -> list[str]:
    if value is None:
        raise WritError(f"{where} is required; every task needs a stated bar")
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value:
        raise WritError(f"{where} must be a non-empty list of criteria")
    items: list[str] = []
    for index, entry in enumerate(value):
        if isinstance(entry, dict):
            entry = _pick(entry, ("text", "criterion", "description", "title"))
        items.append(_text(entry, f"{where}[{index}]"))
    return items


def _strings(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise WritError(f"{where} must be a list of strings")
    return [_text(entry, f"{where}[{i}]") for i, entry in enumerate(value)]


def _pick(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """First alias actually present.

    An empty list is returned rather than skipped: `"acceptances": []` is a plan
    that stated no bar, and the field's own validator should say so, instead of
    this reporting the key as missing entirely.
    """
    for key in keys:
        if key in mapping and mapping[key] is not None and mapping[key] != "":
            return mapping[key]
    return None


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WritError(f"{where} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _check_refs(milestones: list[PlannedMilestone]) -> None:
    """Self-references and dependencies on nothing are the plan's own fault."""
    for milestone in milestones:
        for task in milestone.tasks:
            if task.ref and task.ref in task.depends_on:
                raise WritError(f"task {task.ref} depends on itself")


def unresolved_sections(
    milestones: list[PlannedMilestone], doc: DesignDocs
) -> list[str]:
    """Design sections the plan claims but the document does not contain."""
    missing = []
    for milestone in milestones:
        for task in milestone.tasks:
            if not task.stated_section:
                continue
            if not find_section(doc, task.section)[1]:
                missing.append(task.section)
    return sorted(set(missing))


def untraceable_requirements(
    requirements: list[Requirement], doc: DesignDocs
) -> list[Finding]:
    """Requirements citing a heading the design document does not have.

    The same check `unresolved_sections` makes for tasks, pointed at the inventory.
    It is worth making deterministically rather than leaving to the fidelity critic,
    because it is the cheapest available evidence that a requirement was *read*
    rather than assumed: an obligation traced to a heading that does not exist is
    one the stage may have supplied from its own expectations of what a document
    like this would say, and that is how work with no mandate enters a plan.

    Advisory, not blocking. A document can be restructured after planning, and a
    heading cited loosely is a citation problem rather than an invented obligation —
    a reader given the id and the claimed source can settle it in a moment.
    """
    findings: list[Finding] = []
    for requirement in requirements:
        source = (requirement.source or "").strip()
        if not source:
            continue
        if find_section(doc, source)[1]:
            continue
        findings.append(
            Finding(
                severity="warning",
                category="untraceable-requirement",
                where=requirement.id,
                requirement_ids=[requirement.id],
                message=(
                    f"{requirement.id} cites {source!r}, which is not a heading in "
                    f"{doc_names(doc)}"
                ),
                suggested_action=(
                    "Cite the heading the obligation actually came from, or check "
                    "that the document states it at all."
                ),
                source="stage:requirements",
            )
        )
    return findings
