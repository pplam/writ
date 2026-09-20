"""Generative planning: a coding agent turns a design doc into a task DAG.

Text extraction (`planner.py`) can only repeat what a document already says in a
shape it recognises. Planning is a judgement call: which work is one bounded
session, what the real bar is, which components a task may touch, and what must
land first. So `writ plan` hands the design doc and the repository to a coding
agent and asks for a plan as JSON.

The agent's freedom stops at the schema. Everything it returns is validated
before it reaches project state, the raw artifact is kept under
`.writ/plans/<plan-id>/`, and a rejected plan says exactly which field was wrong.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import agents, plancheck, runner, state
from .plancheck import Requirement
from .planner import PlannedMilestone, PlannedTask, section_text
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
    "verification": ("verification", "verify", "verification_hints", "how"),
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
      "text": "one obligation the document states, in your own words",
      "source": "exact heading it came from",
      "priority": "must" | "should" | "may",
      "status": "planned" | "existing" | "out-of-scope" | "deferred",
      "evidence": "status existing only: the test or code that satisfies it",
      "reason": "status out-of-scope or deferred only: why it is not this plan's",
      "verification": ["how this can be demonstrated"]
    }
  ],
  "milestones": [
    {
      "id": "M01",
      "title": "short outcome, not a restatement of the heading",
      "notes": "what this milestone establishes, one or two sentences",
      "tasks": [
        {
          "id": "M01-001",
          "title": "imperative, specific: 'Add append-only event log writer'",
          "notes": "approach, key files, pitfalls found while reading the repo",
          "design_section": "exact heading from the design document",
          "requirement_ids": ["REQ-001", "REQ-004"],
          "acceptances": [
            "a criterion a person or command can check",
            "another one"
          ],
          "depends_on": ["M01-002"],
          "allowed": ["internal/store/"],
          "forbidden": ["api/"]
        }
      ]
    }
  ]
}"""

RULES = """\
Rules:
- Start with `requirements`, before any task. Read the document and write down
  every obligation it states: behaviour, constraint, interface, non-functional
  bar. One obligation per entry, in your own words, with the heading it came from.
  This inventory is what the plan is checked against, so an obligation you leave
  out is one nothing will ever verify — and one you invent becomes work with no
  mandate. Do not fold two requirements into one entry to make the list shorter.
- Every requirement must end somewhere. Either one or more tasks name it in
  `requirement_ids`, or it is `existing` with evidence naming the test or code
  that already satisfies it, or it is `out-of-scope`/`deferred` with a reason.
  Silently dropping one is the failure this inventory exists to prevent.
- Every task should name the requirements it covers. A task that covers none is
  either infrastructure — say so in its notes — or work nothing asked for.
- One task is one bounded agent session: a single coherent change with a stated
  bar. Split anything that spans unrelated components or that you could not
  review in one sitting. Do not emit a task called "implement the design".
- Each task states 2 to 6 acceptance criteria. Every criterion must be checkable:
  name the command, the observable behavior, or the artifact it produces.
  "Works correctly" and "code is clean" are not criteria.
- A criterion must be meetable by this task alone. Do not set a bar that depends
  on work outside its `allowed` list: on a fenced task, "the whole suite passes"
  is not such a bar, because tasks run in parallel and a sibling's half-finished
  module fails it for reasons this agent may not touch. Scope it to what the task
  owns — name the test file or the command that exercises this change.
- Prefer bars the document already states, in its own wording. Add your own only
  where the document is silent, and keep them consistent with it. Do not invent
  requirements the document does not support.
- depends_on holds task ids from this plan, or ids of the existing tasks listed
  above. It must form a DAG: no cycles, no self-references. Order milestones so
  earlier work unblocks later work, and leave independent tasks independent
  instead of chaining everything into one line.
- State every edge the work actually needs. A task with no `depends_on` is run as
  soon as the graph allows, possibly first and possibly beside any other — the
  plan's order is not an ordering. If B reads an interface A creates, B must say
  so; nothing else will notice.
- allowed and forbidden are repo-relative paths or packages that fence a task to
  the components it should touch. Omit them when a task is genuinely global.
- Two tasks that nothing orders must not list the same path in `allowed`. They
  can run at the same time, in the same working tree, and whichever finishes
  second loses its work. Give the file one owner and have the other depend on it.
- Where branches of the graph have to compose, say what checks that they do: a
  task depending on both, whose criteria exercise the combined behaviour. A plan
  that ends in several independent leaves has verified each of them alone.
- design_section must be a heading that appears verbatim in the design document,
  so a task can be traced back to what asked for it. Use "Parent / Child" for a
  nested heading.
- Where the document is ambiguous, or contradicts what the repository already
  does, record it in that task's notes. Do not silently pick a reading.
- Skip work the repository has already done, and say so in the milestone notes.

Write no code and change no file other than the plan JSON. You are planning."""


# --------------------------------------------------------------------------
# prompt


def new_plan_id(doc: Path) -> str:
    stem = re.sub(r"[^A-Za-z0-9]+", "-", doc.stem).strip("-").lower() or "plan"
    return f"{stem}-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}"


def build_prompt(
    *,
    root: Path,
    doc: Path,
    plan_path: Path,
    instructions: str | None = None,
    context: dict[str, Any] | None = None,
) -> str:
    """Compose the planning prompt: read the doc and repo, emit plan JSON."""
    context = context or {}
    lines: list[str] = []
    lines.append(
        "You are planning implementation work for this repository. "
        "You are not implementing it."
    )
    lines.append("")
    lines.append(f"Repository root: {root}")
    lines.append(f"Design document: {doc}")
    lines.append("")
    lines.append(
        "Read the design document in full, then read enough of the repository to "
        "ground the plan in real paths, existing conventions, and work that is "
        "already done."
    )
    lines.append("")
    existing_docs = [
        path for path in context.get("design_docs", []) if path != str(doc)
    ]
    if existing_docs:
        lines.append("Other documents already registered for this project:")
        lines.extend(f"- {path}" for path in existing_docs)
        lines.append("")
    existing_tasks = context.get("tasks", [])
    if existing_tasks:
        lines.append(
            "This project already has a plan. Plan only the work that is missing, "
            "and depend on these existing tasks where the new work needs them:"
        )
        for entry in existing_tasks:
            lines.append(
                f"- {entry['id']} [{entry['status']}] {entry['title']}"
            )
        offset = int(context.get("milestone_offset", 0))
        lines.append("")
        lines.append(
            f"Number new milestones from M{offset + 1:02d} onward so ids do not collide."
        )
        lines.append("")
    if instructions:
        lines.append("Additional instructions from the operator (these win):")
        lines.append(instructions)
        lines.append("")
    lines.append("Write the plan as JSON to this exact path:")
    lines.append(f"  {plan_path}")
    lines.append("")
    lines.append("The file must contain JSON only — no prose, no code fence.")
    lines.append("")
    lines.append("Schema:")
    lines.append(SCHEMA)
    lines.append("")
    lines.append(RULES)
    lines.append("")
    lines.append(
        "If you cannot write that file, print the same JSON to stdout inside a "
        "single ```json fenced block instead."
    )
    lines.append("")
    lines.append(
        "When the file is written, summarise in a few lines: how many milestones "
        "and tasks, the ordering you chose and why, and anything in the document "
        "you could not turn into a checkable bar."
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
            }
            for task_id in sorted(data.get("tasks", {}))
        ],
    }


# --------------------------------------------------------------------------
# running the planning agent


def generate(
    *,
    root: Path,
    doc: Path,
    agent: str,
    agent_args: list[str],
    model: str | None,
    timeout: int | None,
    cwd: str | None,
    instructions: str | None,
    context: dict[str, Any],
    stream: bool = False,
    on_start=None,
) -> tuple[PlanDocument, Path, int]:
    """Run the planning agent and return the validated plan it produced."""
    resolved = agents.resolve(agent, agent_args, model)
    plan_id = new_plan_id(doc)
    directory = state.plan_dir(root, plan_id)
    directory.mkdir(parents=True, exist_ok=True)
    plan_path = directory / "plan.json"
    prompt = build_prompt(
        root=root.resolve(),
        doc=doc,
        plan_path=plan_path,
        instructions=instructions,
        context=context,
    )
    if on_start is not None:
        on_start(resolved, directory)
    try:
        code = runner.run_agent(
            resolved.command,
            prompt,
            directory,
            cwd or root,
            timeout,
            stream=stream,
            prefix="  | " if stream else "",
        )
    except FileNotFoundError as exc:
        raise WritError(f"planning agent not found: {resolved.command[0]}") from exc

    text = _plan_text(directory, plan_path)
    if text is None:
        raise WritError(_no_plan_message(resolved, directory, code))
    try:
        document = load_document(text)
    except WritError as exc:
        raise WritError(f"{exc} (plan artifact: {plan_path})") from exc
    return document, plan_path, code


def _no_plan_message(
    resolved: agents.ResolvedAgent, directory: Path, code: int
) -> str:
    """Explain a planner that produced nothing, and why it may have hung."""
    produced_output = runner.produced_output(directory)
    if code == 124:
        detail = "the planning agent was killed for exceeding its timeout"
        if not produced_output:
            detail += f"\n  {agents.hang_hint(resolved)}"
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
        raw_milestones = _pick(payload, MILESTONE_ALIASES["tasks"] + ("milestones",))
        if raw_milestones is None:
            raise WritError("the plan has no `milestones` list")
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


def unresolved_sections(milestones: list[PlannedMilestone], doc: Path) -> list[str]:
    """Design sections the plan claims but the document does not contain."""
    missing = []
    for milestone in milestones:
        for task in milestone.tasks:
            if not task.stated_section:
                continue
            if not section_text(doc, task.section):
                missing.append(task.section)
    return sorted(set(missing))
