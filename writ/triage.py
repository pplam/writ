"""Stuck tasks get a second look from the plan's side, not only a person's.

A task can end a run in two states nothing automatic moved it out of. `blocked`:
its implementer found a criterion it could not meet from inside its fence —
typically one that depends on code a later task writes, which is a planning
defect, not an implementation one. And `failed` after its rework budget: several
attempts, several rejections, and a fourth attempt with the same brief is not
going to be argued into passing. Both used to wait for a person, and on an
unattended build that was the end of the build.

Triage hands the stuck task to the plan's repair agent — the adjudicator's role,
because what is wrong is usually the plan — with the task's verdict, its criteria
and where they stand, and a working copy of the plan's features. It may:

- move a criterion the task cannot meet to a planned task or a gate that can,
  rewording it to fit there;
- rewrite the stuck task's criteria and fence, or add prerequisite work;
- change nothing and send the task back with guidance, when the brief was fine
  and the attempts were not;
- ask a question, when the way forward is a product decision.

Writ validates the copy as it validates an adjudication (see
`adjudicate.validate`), with two allowances for the stuck task: it may be edited
although it has started, and it may drop criteria, but only ones it moves. The bar
moves to where it can be met; it does not drop. Then the task goes back to
`planned` with the triage's guidance in its next prompt, and its previous work is
still in the tree.

Bounded per task (`MAX_TRIAGES`): a task two triages could not unstick needs a
person, and `writ override` or `writ set` is still how they say so. Under
`decisions.autonomous` `writ run` triages on its own; otherwise it stops and
names `writ unstick <id>`.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import adjudicate, agents, config, decisions, gates, planfiles, plans, prompts, runner, state
from .model import add_evidence, blocked_on, refresh_milestones
from .plancheck import Finding
from .prompts import Ref
from .state import WritError, utcnow

#: triages one task may have before it waits for a person
MAX_TRIAGES = 2
#: under the plan directory, one `<task>-<n>/` per triage
DIRNAME = "triage"
STUCK_FILENAME = "stuck.json"
ACTIONS = ("revise", "retry", "question")

RESPONSE_SCHEMA = """\
{
  "analysis": "why the task is stuck, in a few lines: the brief, the plan, or the attempts",
  "action": "revise | retry | question",
  "guidance": "what the next implementer should do differently; it is put in their prompt",
  "moved": [
    {
      "criterion": "the stuck task's criterion, word for word",
      "to": "FT-004 or G-FINAL: a planned task, or a gate that has not run",
      "as": "optional: the wording it has there, if you reworded it"
    }
  ],
  "question": {
    "question": "only for action `question`",
    "context": "the readings, and what each would change",
    "recommendation": "the answer you would give, stated as the decision"
  }
}"""

RULES = """\
Rules:
1. Unstick this one task. Do not re-plan the project or tidy features nothing
   is wrong with.
2. `revise` edits the workspace. The stuck task may be edited although it has
   started; every other feature that has started may not. Features of kind
   "gate" are writ's: to give a gate a criterion, list it in `moved` with the
   gate as `to`, and writ adds it.
3. The bar moves, never drops. A criterion you remove from the stuck task must
   be listed in `moved`, and a task it moves to must carry it in its own file
   (word for word, or as `as`). Only a task still "planned", or a gate that has
   not run, can take it: those are the ones that will still be judged. A
   criterion reworded in place is not removed.
4. Requirements stay covered, the inventory is fixed, and the graph stays a
   graph: every `depends_on` names a feature in the workspace, and no cycles.
5. `retry` changes nothing in the plan: the brief was sound and the attempts
   fell short. Say in `guidance` what the next attempt must do differently —
   an unchanged retry of a task that failed on merit fails again.
6. `question` is for a product decision the design does not settle. Give your
   `recommendation`.
7. The previous attempts' code is still in the working tree. The next
   implementer continues from it, so a criterion already passed stays passed
   unless you change it.

Write no code, and change no file outside the workspace and the response."""

AUTONOMOUS_NOTE = """\
Writ is running autonomously: no person will answer a question. Prefer `revise`
or `retry`. A `question` is answered with its own `recommendation`, which the
next attempt is then told to build to, so make the recommendation the decision
you would stand behind."""


@dataclass
class Result:
    """One triage, and what came of it."""

    task_id: str
    number: int = 0
    directory: Path | None = None
    action: str = ""
    analysis: str = ""
    guidance: str = ""
    exit_code: int | None = None
    error: str = ""
    refused: list[Finding] = field(default_factory=list)
    applied: dict[str, Any] = field(default_factory=dict)
    moved: list[dict[str, str]] = field(default_factory=list)
    #: a question that is waiting for a person
    waiting: str = ""
    #: the task's status afterwards
    status: str = ""

    @property
    def unstuck(self) -> bool:
        return self.status == "planned"

    @property
    def summary(self) -> str:
        if self.error:
            return self.error
        if self.refused:
            return "writ refused the triage: " + "; ".join(
                finding.message for finding in self.refused[:3]
            )
        if self.waiting:
            return f"a question needs a person ({self.waiting})"
        parts = []
        if self.moved:
            parts.append(
                "moved "
                + ", ".join(
                    f"criterion to {entry.get('to')}" for entry in self.moved
                )
            )
        if self.applied.get("tasks"):
            parts.append("added " + ", ".join(self.applied["tasks"]))
        if self.applied.get("revised"):
            parts.append("revised " + ", ".join(self.applied["revised"]))
        head = "; ".join(parts) or "sent back with guidance"
        return f"{head}: {self.guidance or self.analysis}".strip()


# --------------------------------------------------------------------------
# which tasks are stuck


def attempts(task: dict[str, Any]) -> list[dict[str, Any]]:
    return list(task.get("triage") or [])


def triages_left(task: dict[str, Any]) -> bool:
    return len(attempts(task)) < MAX_TRIAGES


def is_stuck(task: dict[str, Any]) -> bool:
    """Blocked by its own report, or failed on merit with its rework spent.

    A task left by an infrastructure failure is not stuck in this sense: nothing
    about the plan or the brief is in question, and it is retried as such.
    """
    if gates.is_gate(task):
        return False
    if task.get("status") == "blocked":
        return True
    return task.get("status") == "failed" and bool(
        (task.get("rework") or {}).get("exhausted")
    )


def stuck(data: dict[str, Any], *, budgeted: bool = True) -> list[str]:
    """Stuck tasks, in id order; with `budgeted`, only those with triages left."""
    return [
        task_id
        for task_id, task in sorted(data["tasks"].items())
        if is_stuck(task) and (triages_left(task) or not budgeted)
    ]


def reason(task: dict[str, Any]) -> str:
    """Why the task is stuck, in the words its last verdict used."""
    said = blocked_on(task)
    if said:
        return said
    record = task.get("rework") or {}
    return str(record.get("summary") or (task.get("last_verdict") or {}).get("summary") or "")


def pending_guidance(task: dict[str, Any]) -> dict[str, Any] | None:
    """The last triage, while the task has not been attempted since it landed."""
    for record in reversed(attempts(task)):
        if not record.get("unstuck"):
            continue
        # The verdict the triage read; a newer one means the task has run since.
        since = str((task.get("last_verdict") or {}).get("at") or "")
        return record if str(record.get("verdict_at", "")) == since else None
    return None


# --------------------------------------------------------------------------
# one triage


def run(
    root: Path,
    task_id: str,
    *,
    agent: str,
    model: str | None,
    timeout: int | None,
    cwd: str | None = None,
    stream: bool = False,
    prefix: str = "",
    autonomous: bool = False,
    note: str = "",
    lock: Any = None,
) -> Result:
    """Triage one stuck task: prepare, run the agent, validate, apply or refuse.

    Never raises for what the agent did — a refusal, a missing response, a
    question are all results, recorded on the task so the bound holds across
    sessions. It raises only when the task is not stuck, or out of triages.

    `note` is a person's steer, from `writ unstick --note`, put in the prompt.
    """
    root = Path(root)
    resolved = agents.resolve(
        agent, [], model, events=True, dirs=config.agent_dirs(root, cwd)
    )
    with state.transaction(root) as data:
        task = data["tasks"].get(task_id)
        if task is None:
            raise WritError(f"unknown task: {task_id}")
        if not is_stuck(task):
            raise WritError(
                f"{task_id} is {task.get('status')}; only a blocked task, or one "
                "failed with its rework spent, is triaged"
            )
        if not triages_left(task):
            raise WritError(
                f"{task_id} has had {len(attempts(task))} triage(s), the most "
                "writ gives one task; it needs a person (writ override, writ set)"
            )
        number = len(attempts(task)) + 1
        directory = planfiles.directory(root, data) / DIRNAME / f"{task_id}-{number}"
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)
        base_revision = plans.revision(data)
        planfiles.dump(directory / STUCK_FILENAME, _stuck_record(root, data, task))
        planfiles.export(root, data)
        planfiles.write_features(directory / adjudicate.WORKING_DIRNAME, data["tasks"])
        previous = _previous(root, task)
        prompt = build_prompt(
            root=root,
            data=data,
            task=task,
            directory=directory,
            previous=previous,
            autonomous=autonomous,
            note=note,
        )
    result = Result(task_id=task_id, number=number, directory=directory)
    try:
        _run_agent(result, resolved, prompt, root, cwd, timeout, stream, prefix, lock)
        if not result.error:
            _settle(root, result, base_revision, autonomous=autonomous)
    finally:
        shutil.rmtree(directory / adjudicate.WORKING_DIRNAME, ignore_errors=True)
        _record(root, result)
    return result


def _run_agent(
    result: Result,
    resolved: agents.ResolvedAgent,
    prompt: str,
    root: Path,
    cwd: str | None,
    timeout: int | None,
    stream: bool,
    prefix: str,
    lock: Any,
) -> None:
    directory = result.directory
    assert directory is not None
    try:
        result.exit_code = runner.run_agent(
            resolved.command,
            prompt,
            directory,
            cwd or root,
            timeout,
            stream=stream,
            prefix=prefix,
            event_shape=resolved.event_shape,
            mirror_lock=lock,
        )
    except FileNotFoundError:
        result.error = f"triage agent not found: {resolved.command[0]}"
    except WritError as exc:
        result.error = str(exc)


def _settle(
    root: Path, result: Result, base_revision: int, *, autonomous: bool
) -> None:
    """Read the response back and act on it, in one transaction."""
    directory = result.directory
    assert directory is not None
    text = adjudicate._response_text(directory, directory / adjudicate.RESPONSE_FILENAME)
    if text is None:
        result.error = "the triage agent wrote no response"
        return
    try:
        response = load_response(text)
    except WritError as exc:
        result.error = f"unusable response: {exc}"
        return
    result.action = response["action"]
    result.analysis = response["analysis"]
    result.guidance = response["guidance"]
    result.moved = response["moved"]
    task_id = result.task_id
    with state.transaction(root) as data:
        task = data["tasks"][task_id]
        found, proposed, diff = adjudicate.validate(
            data,
            directory,
            {},
            finding_ids=[],
            base_revision=base_revision,
            root=root,
            stuck=task_id,
            moved=result.moved,
        )
        edits = any(diff.values())
        if result.action == "retry" and edits:
            found.append(
                adjudicate._refuse(
                    "retry-with-edits",
                    "the action is `retry` but the workspace was edited",
                    adjudicate.RESPONSE_FILENAME,
                    "use `revise` to change the plan, or restore the workspace",
                )
            )
        if result.action == "revise" and not edits:
            found.append(
                adjudicate._refuse(
                    "no-op",
                    "the action is `revise` but nothing in the workspace changed",
                    adjudicate.RESPONSE_FILENAME,
                    "edit the features, or use `retry` with guidance",
                )
            )
        if result.action == "retry" and not result.guidance:
            found.append(
                adjudicate._refuse(
                    "no-op",
                    "a retry with no guidance is the same attempt again",
                    adjudicate.RESPONSE_FILENAME,
                    "say what the next attempt must do differently",
                )
            )
        adjudicate._write_validation(directory, base_revision, found, diff)
        refused = [finding for finding in found if finding.blocking]
        if refused and result.action != "question":
            result.refused = refused
            result.status = task["status"]
            return
        if result.action == "question":
            # A question changes nothing in the plan: whatever the workspace
            # holds is set aside, and the ruling reaches the task as guidance.
            edits = False
            ruling = _ask(data, task, response["question"], autonomous=autonomous)
            if ruling is None:
                result.waiting = str(task.get("triage_question", ""))
                result.status = task["status"]
                return
            result.guidance = (
                f"{result.guidance} Build to this decision: {ruling}".strip()
            )
        else:
            record = decisions.propose(
                data,
                title=f"Unstick {task_id}: {result.action}"[:72],
                decision=result.guidance or result.analysis or result.action,
                context=f"{task_id} was {task['status']}: {reason(task)}. "
                f"{result.analysis}",
                consequences=_consequences(result),
                proposed_by="triage",
                tasks=[task_id],
            )
            if autonomous:
                # Otherwise it stays proposed: a person asked for the triage, not
                # for this particular change, and `writ set D-NNNN` is theirs.
                decisions.confirm(data, record["id"], actor=decisions.AUTONOMOUS)
        was = task["status"]
        # Back to `planned` before promotion, so the stuck task's edges are
        # re-derived with everyone else's (`adjudicate._rederive_edges` skips
        # anything that has started).
        task["status"] = "planned"
        if edits:
            _carry_to_gates(data, task_id, result.moved)
            request = {"id": f"TR-{task_id}-{result.number}", "round": result.number}
            result.applied = adjudicate.promote(
                data,
                request,
                proposed,
                diff,
                {"analysis": result.analysis, "dispositions": [], "questions": []},
                actor="triage",
            )
        _send_back(task, was)
        add_evidence(
            task,
            f"triage {result.number}: {result.summary}",
            actor="triage",
        )
        task["updated_at"] = utcnow()
        refresh_milestones(data)
        planfiles.export(root, data)
        result.status = task["status"]


def _ask(
    data: dict[str, Any],
    task: dict[str, Any],
    question: dict[str, Any],
    *,
    autonomous: bool,
) -> str | None:
    """Log the triage's question; the ruling, when writ may make it."""
    recommendation = str(question.get("recommendation") or "").strip()
    record = decisions.propose(
        data,
        title=str(question.get("question") or f"How to unstick {task['id']}")[:72],
        decision=(
            "Undecided: triage could not unstick the task without a ruling."
        ),
        context=str(question.get("context") or question.get("question") or "")
        + (f" Recommended: {recommendation}" if recommendation else ""),
        consequences=f"{task['id']} stays {task['status']} until this is settled.",
        proposed_by="triage",
        tasks=[task["id"]],
    )
    if autonomous and recommendation:
        decisions.answer(data, record["id"], recommendation)
        return recommendation
    task["triage_question"] = record["id"]
    return None


def _consequences(result: Result) -> str:
    moved = [f"{entry.get('to')}" for entry in result.moved]
    if moved:
        return (
            f"{result.task_id} no longer carries the criteria moved to "
            f"{', '.join(moved)}; they are judged there."
        )
    return f"{result.task_id} is attempted again with this guidance."


def _carry_to_gates(
    data: dict[str, Any], task_id: str, moved: list[dict[str, str]]
) -> None:
    """Append each criterion moved to a gate, marked with where it came from."""
    for entry in moved:
        gate = data["tasks"].get(str(entry.get("to", "")))
        if gate is None or not gates.is_gate(gate):
            continue
        text = str(entry.get("as") or entry.get("criterion") or "").strip()
        if not text or any(item["text"] == text for item in gate.get("acceptances", [])):
            continue
        gate.setdefault("acceptances", []).append(
            {"text": text, "status": "pending", "from": task_id}
        )


def _send_back(task: dict[str, Any], was: str) -> None:
    """Give a failed task a fresh rework budget: a triaged brief is a new brief.

    The count keeps climbing, as it does when a person moves an exhausted task
    back (`model.set_status`), and the budget is extended rather than reset.
    """
    record = task.get("rework") or {}
    if was == "failed" and record.get("exhausted"):
        task["rework"] = {
            **record,
            "allowance": int(record.get("allowance", 0)) + int(record.get("max", 0)),
            "exhausted": False,
            "reset_by": "triage",
            "reset_at": utcnow(),
        }
    task.pop("triage_question", None)


def _record(root: Path, result: Result) -> None:
    """Put the triage on the task, whatever came of it: it counts toward the bound."""
    with state.transaction(root) as data:
        task = data["tasks"].get(result.task_id)
        if task is None:
            return
        task.setdefault("triage", []).append(
            {
                "number": result.number,
                "at": utcnow(),
                "verdict_at": str((task.get("last_verdict") or {}).get("at") or ""),
                "directory": planfiles.rel(root, result.directory)
                if result.directory
                else "",
                "action": result.action,
                "analysis": result.analysis,
                "guidance": result.guidance,
                "moved": result.moved,
                "added": list(result.applied.get("tasks", [])),
                "revised": list(result.applied.get("revised", [])),
                "unstuck": result.unstuck,
                "error": result.error,
                "refused": [finding.to_dict() for finding in result.refused],
                "waiting": result.waiting,
            }
        )


def _previous(root: Path, task: dict[str, Any]) -> Path | None:
    """The last refused triage's validation report, required reading on a retry."""
    for record in reversed(attempts(task)):
        if record.get("refused") and record.get("directory"):
            report = root / record["directory"] / adjudicate.VALIDATION_FILENAME
            return report if report.exists() else None
        return None
    return None


def _stuck_record(root: Path, data: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    """Everything about the stuck task an agent should not have to dig for."""
    task_id = task["id"]
    run_id = runner.latest_run_for(data, task_id)
    return {
        "task": task_id,
        "title": task.get("title", ""),
        "status": task.get("status"),
        "why": reason(task),
        "criteria": [
            {
                "number": index,
                "text": item.get("text", ""),
                "status": item.get("status", "pending"),
                "evidence": item.get("evidence", ""),
            }
            for index, item in enumerate(task.get("acceptances", []), start=1)
        ],
        "last_verdict": task.get("last_verdict") or {},
        "rework": task.get("rework") or {},
        "evidence": (task.get("evidence") or [])[-6:],
        "last_run": planfiles.rel(root, state.run_dir(root, run_id)) if run_id else "",
        "depends_on": {
            dep: data["tasks"][dep].get("status")
            for dep in task.get("depends_on", [])
            if dep in data["tasks"]
        },
        "dependents": {
            other_id: other.get("status")
            for other_id, other in sorted(data["tasks"].items())
            if task_id in other.get("depends_on", [])
        },
        "earlier_triages": attempts(task),
    }


# --------------------------------------------------------------------------
# the prompt and the response


def build_prompt(
    *,
    root: Path,
    data: dict[str, Any],
    task: dict[str, Any],
    directory: Path,
    previous: Path | None = None,
    autonomous: bool = False,
    note: str = "",
) -> str:
    work = directory / adjudicate.WORKING_DIRNAME
    first = [
        Ref(
            directory / STUCK_FILENAME,
            "the stuck task: why it stopped, its criteria and where each stands, "
            "its last verdict, and what depends on it",
        )
    ]
    if previous is not None:
        first.append(
            Ref(previous, "why writ REFUSED the previous triage of this task: fix "
                "what it lists")
        )
    first.append(
        Ref(
            planfiles.index_path(root, data),
            "the plan at a glance, one row per feature with its status. Reference "
            "only; edit the workspace instead",
        )
    )
    docs = [Path(path) for path in data.get("design_docs") or [] if Path(path).exists()]
    first.extend(Ref(path, "the design document the plan implements") for path in docs)
    stuck_record = directory / STUCK_FILENAME
    last_run = json.loads(stuck_record.read_text(encoding="utf-8")).get("last_run")
    as_needed = [Ref(work, "the workspace, one file per feature: edit these")]
    if last_run:
        as_needed.append(
            Ref(root / last_run, "the last run on the task: its prompt, verdict and transcript")
        )
    lines = [
        f"A task in a RUNNING plan is stuck: {task['id']} ({task.get('title', '')}) "
        f"is {task['status']}.",
        "",
        "Nothing automatic moves it on. Work out why, and unstick it: usually the "
        "plan asked the task for something it cannot do from where it sits — a "
        "criterion that needs code a later task writes, say — and sometimes the "
        "attempts fell short of a sound brief.",
        "",
        prompts.root_line(root),
        f"Plan revision: {plans.revision(data)}",
        f"Triage: {len(attempts(task)) + 1} of {MAX_TRIAGES}",
        "",
        *prompts.references(root, first=first, as_needed=as_needed),
        *(["A person said about this task:", f"  {note}", ""] if note else []),
        f"Choose one action, and for `revise` edit the workspace in "
        f"{planfiles.rel(root, work)} (edit a feature's file, create a file to add "
        "one, delete one to remove it):",
        "  - revise: change the plan — move criteria, reword the stuck task's, add "
        "prerequisite work;",
        "  - retry: change nothing, and say what the next attempt must do;",
        "  - question: a decision the design does not settle.",
        "",
        RULES,
        "",
        *([AUTONOMOUS_NOTE, ""] if autonomous else []),
        *prompts.output(root, "response", directory / adjudicate.RESPONSE_FILENAME),
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


def load_response(text: str) -> dict[str, Any]:
    """Parse and shape-check the triage response."""
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
    action = str(payload.get("action", "")).strip().lower()
    if action not in ACTIONS:
        raise WritError(f"`action` must be one of {', '.join(ACTIONS)}, not {action!r}")
    moved = payload.get("moved") or []
    if not isinstance(moved, list) or not all(isinstance(item, dict) for item in moved):
        raise WritError("`moved` must be a list of objects")
    question = payload.get("question") or {}
    if not isinstance(question, dict):
        raise WritError("`question` must be an object")
    if action == "question" and not str(question.get("question", "")).strip():
        raise WritError("the action is `question` but `question.question` is empty")
    return {
        "action": action,
        "analysis": str(payload.get("analysis", "")).strip(),
        "guidance": str(payload.get("guidance", "")).strip(),
        "moved": [
            {key: str(value).strip() for key, value in item.items() if value}
            for item in moved
        ],
        "question": question,
    }
