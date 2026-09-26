"""Dispatching work to coding agents and tracking the resulting runs."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, IO, Iterable

from . import (
    agents,
    contracts,
    decisions,
    failures,
    planner,
    plans,
    procs,
    repair,
    state,
    verdict,
)
from .stream import Renderer
from .model import (
    DEFAULT_MAX_REWORK,
    add_evidence,
    blocking_dependencies,
    get_task,
    open_rework,
    refresh_milestones,
    rework_attempts,
)
from .state import WritError, utcnow

ACTIVE_RUN_STATUSES = ("starting", "running")

#: what a repair-planning run writes instead of a verdict
PATCH_FILENAME = "patch.json"

GUARDRAILS = """\
Working rules (non-negotiable):
1. Inspect the repository before editing; state contradictions and assumptions first.
2. Write the failing test first and run it so the failure is visible.
3. Implement the minimum change that satisfies the acceptance criteria.
4. Refactor only while tests are green.
5. Run the project's full verification (build, tests, vet/lint) before reporting.
6. Do not weaken an invariant, add a dependency, or use live network data to pass a test.
7. Do not modify components outside the allowed list.
"""


def build_prompt(
    data: dict[str, Any],
    task: dict[str, Any],
    root: Path,
    *,
    verdict_path: Path | None = None,
    verify: str | None = None,
) -> str:
    """Compose the agent prompt from the task, its gates, and the design doc."""
    lines: list[str] = []
    feature = contracts.is_feature(task)
    lines.append(
        "You are implementing ONE feature of this project: a subsystem you build "
        "on your own, inside its fence."
        if feature
        else "You are implementing ONE bounded task in this project."
    )
    lines.append("")
    docs = list(data.get("design_docs", []))
    if task.get("design_doc") and task["design_doc"] not in docs:
        docs.append(task["design_doc"])
    if docs:
        lines.append("Authoritative documents (read before editing):")
        lines.extend(f"- {doc}" for doc in docs)
        lines.append("")
    lines.append(f"Task {task['id']}: {task['title']}")
    if task.get("milestone"):
        milestone = data["milestones"].get(task["milestone"], {})
        lines.append(f"Milestone: {task['milestone']} — {milestone.get('title', '')}")
    if feature:
        lines.extend(_feature_section(data, task))
    lines.append("")
    lines.append("Acceptance criteria (each must be demonstrably met):")
    for index, item in enumerate(task.get("acceptances", []), start=1):
        marker = "x" if item["status"] == "passed" else " "
        lines.append(f"  {index}. [{marker}] {item['text']}")
    lines.append("")
    if task.get("depends_on") and not feature:
        lines.append(f"Completed prerequisites: {', '.join(task['depends_on'])}")
        lines.append("")
    upstream = _agreed_decisions(
        data, _work_under(data, task.get("depends_on", []))
    )
    if upstream:
        # What the work this builds on settled. An implementer used to see none
        # of it, and learnt the interface it had to meet by reading code — or by
        # deciding something else and leaving the incompatibility for a gate.
        lines.append(
            "Decisions the work you build on already made — build to these, and "
            f"record a decision of your own if you must depart from one "
            f"{_decisions_pointer(root)}:"
        )
        lines.extend(f"- {item}" for item in upstream)
        lines.append("")
    if task.get("allowed") and feature:
        lines.append("Fence (the component you own, and where tests go):")
        lines.extend(f"- {item}" for item in task["allowed"])
        lines.append("")
    elif task.get("allowed"):
        lines.append("Allowed files/packages:")
        lines.extend(f"- {item}" for item in task["allowed"])
        lines.append("")
    if task.get("forbidden"):
        lines.append("Forbidden (do not modify):")
        lines.extend(f"- {item}" for item in task["forbidden"])
        lines.append("")
    excerpt = _design_excerpt(task, root)
    if excerpt:
        lines.append("Relevant design section:")
        lines.append("---")
        lines.append(excerpt)
        lines.append("---")
        lines.append("")
    rework = _rework_section(task)
    if rework:
        lines.append(rework)
        lines.append("")
    if task.get("evidence"):
        recent = task["evidence"][-4:]
        lines.append("Previous attempts on this task recorded:")
        for entry in recent:
            actor = entry.get("actor", "operator")
            lines.append(f"- [{actor}] {entry['text']}")
        lines.append("")
    lines.extend(_verify_section(data, root, who="implementer", verify=verify))
    lines.append(GUARDRAILS)
    lines.append("")
    lines.append(_verdict_instructions(task, verdict_path))
    return "\n".join(lines)


def _feature_section(data: dict[str, Any], task: dict[str, Any]) -> list[str]:
    """A feature's goal, contracts and upstream, and the instruction to plan it.

    The plan named no files and no steps for a feature on purpose
    (docs/planning-redesign.md §4): they are decided here, by the agent that can
    see the repository as the upstream features actually left it. So this says
    what those features claim to have provided, not what the plan hoped.
    """
    lines: list[str] = []
    if task.get("goal"):
        lines.append(f"Goal: {task['goal']}")
    if task.get("requirement_ids"):
        requirements = plans.requirements(data)
        lines.append("Requirements this feature discharges (every detail counts):")
        for req_id in task["requirement_ids"]:
            record = requirements.get(req_id, {})
            lines.append(f"- {req_id}: {record.get('text', '')}")
            for detail in record.get("details") or []:
                lines.append(f"    · {detail}")
    if task.get("provides"):
        lines.append("You provide (others build on exactly this; keep the names):")
        lines.extend(f"- {item}" for item in task["provides"])
    if task.get("consumes"):
        lines.append("You consume:")
        lines.extend(f"- {item}" for item in task["consumes"])
    upstream = [dep for dep in task.get("depends_on", []) if dep in data["tasks"]]
    if upstream:
        lines.append("Upstream features, as they stand:")
        for dep_id in upstream:
            dep = data["tasks"][dep_id]
            lines.append(f"- {dep_id} [{dep.get('status')}] {dep.get('title', '')}")
            provided = dep.get("provides") or []
            if provided:
                lines.append(f"    provides: {'; '.join(provided)}")
            claim = (dep.get("last_verdict") or {}).get("summary", "")
            if claim:
                lines.append(f"    reported: {_first_sentence(claim)}")
    lines.append("")
    lines.append(
        "Plan your own steps. The plan names no files or tests for this feature: "
        "choose them yourself, inside the fence below, after reading the code the "
        "upstream features left. If a consumed interface is missing or differs "
        "from its contract, say so in your verdict rather than building around it."
    )
    return lines


def _rework_section(task: dict[str, Any]) -> str:
    """Hand a reworking agent the rejection it exists to answer.

    This is the whole reason a rejected task can go back to the queue rather than
    straight to `failed`. A re-dispatch that does not carry the review is just the
    same prompt again, and an agent given the same prompt has every reason to
    write the same code — so the rejection is stated first, in full, with the
    claim it contradicted beside it.

    Both sides on purpose. The previous agent's claim is not evidence, but the
    disagreement is informative: a criterion the implementer said it verified and
    the reviewer found broken points at a test that does not test what it says,
    while one the implementer never claimed points at work that was simply not
    done. Those need different second attempts.
    """
    record = open_rework(task)
    if not record:
        return ""
    attempt = record.get("attempt", 1)
    budget = record.get("budget", record.get("max", DEFAULT_MAX_REWORK))
    if record.get("kind") == "unfinished":
        return _unfinished_section(record, attempt, budget)
    reviewer = record.get("reviewer") or "a reviewer"
    lines = [
        f"THIS TASK WAS ALREADY IMPLEMENTED AND THE REVIEW REJECTED IT. "
        f"You are attempt {attempt + 1}, and writ allows {budget} rework "
        f"attempt{'s' if budget != 1 else ''} before the task is left failed for "
        "a human.",
        "",
        "The code from the previous attempt is still in the working tree. You are "
        "fixing it, not starting over — read it first, and keep whatever the "
        "review did not object to.",
        "",
        f"{reviewer} reviewed it and rejected it",
    ]
    if record.get("summary"):
        lines.append(f"  {record['summary']}")
    findings = record.get("findings") or []
    if findings:
        lines.append("")
        lines.append("What it found, by criterion:")
        claimed = {
            item.get("number"): item for item in record.get("claimed") or []
        }
        for finding in findings:
            number = finding.get("number")
            lines.append(
                f"  {number}. reviewer marked this {finding.get('status', 'failed')}"
            )
            if finding.get("evidence"):
                lines.append(f"     reviewer: {finding['evidence']}")
            prior = claimed.get(number) or {}
            if prior.get("status") == "passed" and prior.get("evidence"):
                # The most useful line in the section: the bar was claimed met,
                # with evidence, and an agent that did not write the code could
                # not reproduce it. Whatever that evidence was, it was not enough.
                lines.append(
                    f"     the previous attempt claimed this passed: "
                    f"{prior['evidence']}"
                )
    if record.get("notes"):
        lines.append("")
        lines.append(f"Reviewer's notes: {record['notes']}")
    if record.get("claimed_summary"):
        lines.append("")
        lines.append(
            f"The previous attempt described its own work as: "
            f"{record['claimed_summary']}"
        )
    lines.append("")
    lines.append(
        "Address every finding above. A criterion the reviewer marked failed "
        "needs the behaviour fixed and then demonstrated — re-running the same "
        "check that already passed for the last attempt is not an answer to it. "
        "If you conclude a finding is wrong, say so explicitly in your summary "
        "with what you ran to establish that; do not quietly re-claim the bar."
    )
    return "\n".join(lines)


def _unfinished_section(record: dict[str, Any], attempt: int, budget: int) -> str:
    """The rework section for an attempt that stopped, rather than one rejected.

    No reviewer read this work, so nothing here may read as a review. What the
    next agent needs is that it is not starting fresh, and whatever the last one
    said about where it got to.
    """
    lines = [
        f"A PREVIOUS ATTEMPT AT THIS TASK DID NOT FINISH: "
        f"{record.get('reason', 'it stopped without reporting')}. You are attempt "
        f"{attempt + 1}, and writ allows {budget} further attempt"
        f"{'s' if budget != 1 else ''} before the task is left failed for a human.",
        "",
        "Whatever it changed is still in the working tree. Read it before writing "
        "anything: keep what works, finish what does not, and check the whole "
        "task again rather than only the part it left open.",
    ]
    if record.get("summary"):
        lines += ["", f"It described its own work as: {record['summary']}"]
    findings = record.get("findings") or []
    if findings:
        lines += ["", "Criteria it left unmet:"]
        for finding in findings:
            text = f"  {finding.get('number')}. {finding.get('status', 'failed')}"
            if finding.get("evidence"):
                text += f" — {finding['evidence']}"
            lines.append(text)
    if record.get("notes"):
        lines += ["", f"Its notes: {record['notes']}"]
    return "\n".join(lines)


def _verdict_instructions(task: dict[str, Any], verdict_path: Path | None) -> str:
    """Tell the agent to report a machine-readable verdict, and how.

    Writ records the task's status from this file. Without it the run leaves the
    task untouched, so the instruction is explicit about the consequence rather
    than trusting the agent to infer that reporting matters.
    """
    path = verdict_path or Path(verdict.VERDICT_FILENAME)
    total = len(task.get("acceptances", []))
    lines = [
        "When you are done, report your verdict as JSON to this exact path:",
        f"  {path}",
        "",
        "That path, not one of your own choosing. It is where writ reads your "
        "report from; a verdict written anywhere else, however well named, is not "
        "the report you were asked for. Announcing a different path in your "
        "output does not substitute for writing this one.",
        "",
        "The file must contain JSON only — no prose, no code fence.",
        "",
        "Schema:",
        verdict.SCHEMA,
        "",
        verdict.RULES,
        "",
        verdict.DECISION_RULES,
        "",
        f"This task has {total} acceptance criteria, numbered 1 to {total}.",
        "",
        "Writ sets this task's status from that file, and an independent reviewer "
        "re-checks whatever you claim. If you do not write it, the task stays "
        "where it was and your work is not recorded.",
        "",
        "If you cannot write the file, print the same JSON to stdout inside a "
        "single ```json fenced block instead.",
    ]
    return "\n".join(lines)


def build_review_prompt(
    data: dict[str, Any],
    task: dict[str, Any],
    root: Path,
    *,
    verdict_path: Path | None = None,
    verify: str | None = None,
) -> str:
    """Compose the prompt for an agent reviewing someone else's work.

    The reviewer is told what was claimed and asked to verify it independently.
    It gets the claim because a review that cannot see the claim cannot tell a
    misleading one from an honest one; it is told not to trust it for the same
    reason.
    """
    lines: list[str] = []
    lines.append(
        "You are reviewing ONE completed task in this project. You did not write "
        "this code. Do not fix it — judge it."
    )
    lines.append("")
    docs = list(data.get("design_docs", []))
    if task.get("design_doc") and task["design_doc"] not in docs:
        docs.append(task["design_doc"])
    if docs:
        lines.append("Authoritative documents:")
        lines.extend(f"- {doc}" for doc in docs)
        lines.append("")
    lines.append(f"Task {task['id']}: {task['title']}")
    if task.get("milestone"):
        milestone = data["milestones"].get(task["milestone"], {})
        lines.append(f"Milestone: {task['milestone']} — {milestone.get('title', '')}")
    lines.append("")
    lines.append("Acceptance criteria to verify:")
    for index, item in enumerate(task.get("acceptances", []), start=1):
        lines.append(f"  {index}. {item['text']}")
        claimed = item.get("status", "pending")
        if item.get("evidence"):
            lines.append(f"     implementer claimed {claimed}: {item['evidence']}")
        else:
            lines.append(f"     implementer left this {claimed}")
    lines.append("")
    last = task.get("last_verdict") or {}
    if last.get("summary"):
        lines.append("The implementer summarised its work as:")
        lines.append(f"  {last['summary']}")
        lines.append("")
    if task.get("allowed"):
        lines.append("The task was scoped to these files/packages:")
        lines.extend(f"- {item}" for item in task["allowed"])
        lines.append("")
    if task.get("forbidden"):
        lines.append("It was forbidden from modifying:")
        lines.extend(f"- {item}" for item in task["forbidden"])
        lines.append("")
    excerpt = _design_excerpt(task, root)
    if excerpt:
        lines.append("Relevant design section:")
        lines.append("---")
        lines.append(excerpt)
        lines.append("---")
        lines.append("")
    prior = task.get("rework")
    if prior and not prior.get("resolved_at") and prior.get("kind") != "unfinished":
        # A re-review that does not know it is one re-derives the same objections
        # from scratch, or misses that its predecessor's were never answered. The
        # findings are given as a checklist, not as a conclusion: this reviewer
        # still decides for itself, and a previous rejection is not evidence.
        attempt = prior.get("attempt", 1)
        lines.append(
            f"This work was rejected {attempt} time{'s' if attempt != 1 else ''} "
            f"already, most recently by {prior.get('reviewer') or 'a reviewer'}, "
            "and has been reworked since. What that review objected to:"
        )
        for finding in prior.get("findings") or []:
            lines.append(
                f"  {finding.get('number')}. {finding.get('status', 'failed')}: "
                f"{finding.get('evidence', '')}".rstrip()
            )
        if prior.get("summary"):
            lines.append(f"  overall: {prior['summary']}")
        lines.append("")
        lines.append(
            "Check those specifically, on top of the criteria. They are the bars "
            "this attempt exists to clear, and an unaddressed one is a rejection. "
            "They are not a verdict either: judge what is in front of you, and if "
            "an earlier objection was wrong, say so in your summary."
        )
        lines.append("")
    lines.append(
        "Verify by running the project's tests yourself and reading the diff. "
        "Treat the implementer's claims as claims."
    )
    lines.append("")
    lines.extend(_verify_section(data, root, who="reviewer", verify=verify))
    lines.append(verdict.REVIEW_RULES)
    lines.append("")
    lines.append(verdict.DECISION_RULES)
    lines.append("")
    recorded = [
        item
        for item in data.get("decisions", [])
        if task["id"] in item.get("tasks", [])
    ]
    if recorded:
        lines.append("Decisions already recorded against this task:")
        for item in recorded:
            lines.append(f"- [{item['status']}] {item['title']}: {item['decision']}")
        lines.append("")
        lines.append(
            "Do not propose these again, even in different words. Propose a "
            "decision only for a fork none of the above covers, or say in your "
            "summary that one of them is wrong."
        )
        lines.append("")
    path = verdict_path or Path(verdict.VERDICT_FILENAME)
    lines.append("Write your review as JSON to this exact path:")
    lines.append(f"  {path}")
    lines.append("")
    lines.append(
        "That path, not one of your own choosing. It is where writ reads your "
        "review from; a verdict written anywhere else, however well named, is not "
        "the review you were asked for. Announcing a different path in your "
        "output does not substitute for writing this one."
    )
    lines.append("")
    lines.append("The file must contain JSON only — no prose, no code fence.")
    lines.append("")
    lines.append("Schema:")
    lines.append(verdict.REVIEW_SCHEMA)
    lines.append("")
    total = len(task.get("acceptances", []))
    lines.append(
        f"This task has {total} acceptance criteria, numbered 1 to {total}. "
        "Report on every one."
    )
    lines.append("")
    lines.append(
        "Writ completes or fails the task from your decision, so it is the last "
        "word. If you do not write the file, the task stays awaiting review."
    )
    lines.append("")
    lines.append(
        "If you cannot write the file, print the same JSON to stdout inside a "
        "single ```json fenced block instead."
    )
    return "\n".join(lines)


GATE_GUARDRAILS = """\
Working rules for a gate (non-negotiable):
1. Change nothing. No source edits, no new files, no fixes, no formatting. If the
   working tree differs when you finish, the gate is void.
2. Verify by running. Read the code, then run the project's tests and whatever
   commands the criteria name, and report what they actually printed.
3. Judge the integrated tree as it is now, not the task reports about it.
4. A bar you could not check is `pending`, not `passed`.
"""


def build_gate_prompt(
    data: dict[str, Any],
    task: dict[str, Any],
    root: Path,
    *,
    verdict_path: Path | None = None,
    verify: str | None = None,
) -> str:
    """Compose the prompt for a gate: judge integrated work against requirements.

    Two things make this different from a task review, and both matter.

    It is given the **requirement inventory**, not just the task list. A gate whose
    only input is "here are the tasks and their criteria" can do no better than the
    planner that wrote them: if the plan missed an obligation, every task passes and
    the gate agrees. Reading the requirements directly is the only way the omission
    becomes visible.

    It is given the **seams**: which tasks it covers, what each one claimed, and
    what interfaces they agreed on between them. The defects a gate exists to catch
    live between tasks, so the prompt points at the boundaries rather than asking
    for a general re-review of everything.
    """
    from . import gates, plans

    scope = task.get("scope") or ""
    final = scope == gates.FINAL_SCOPE
    lines: list[str] = []
    if final:
        lines.append(
            "You are the FINAL GATE on this project. Every implementation task is "
            "complete and every milestone gate has passed. Your question is whether "
            "the integrated product does what the design asked for."
        )
    else:
        lines.append(
            "You are a MILESTONE GATE. The tasks in this milestone are individually "
            "complete and each was reviewed on its own. Your question is whether "
            "they work as one thing."
        )
    lines.append("")
    lines.append(
        "You did not write this code, and you are not fixing it. You are judging "
        "an integrated outcome and reporting what is wrong with it."
    )
    lines.append("")
    docs = list(data.get("design_docs", []))
    if docs:
        lines.append("Authoritative documents — read these, not only the task list:")
        lines.extend(f"- {doc}" for doc in docs)
        lines.append("")
    lines.append(f"Gate {task['id']}: {task['title']}")
    if task.get("notes"):
        lines.append(task["notes"])
    lines.append("")
    covered = [
        dep for dep in task.get("depends_on", []) if dep in data.get("tasks", {})
    ]
    if covered:
        lines.append("Work under this gate:")
        for dep_id in covered:
            dep = data["tasks"][dep_id]
            kind = "gate" if dep.get("kind") == "gate" else "task"
            lines.append(f"- {dep_id} [{kind}, {dep['status']}] {dep['title']}")
            claim = (dep.get("last_verdict") or {}).get("summary", "")
            if claim:
                lines.append(f"    claimed: {_first_sentence(claim)}")
        lines.append("")
    requirements = _gate_requirements(data, task)
    if requirements:
        lines.append(
            "Requirements this gate is answerable for. Check each against the code "
            "as it stands, not against whether a task says it did it:"
        )
        for row in requirements:
            head = f"- {row['id']} [{row['priority']}] {row['text']}"
            lines.append(head)
            if row.get("source"):
                lines.append(f"    stated in: {row['source']}")
            if row.get("tasks"):
                lines.append(f"    implemented by: {', '.join(row['tasks'])}")
            else:
                lines.append(
                    "    implemented by: nothing in the plan — if this requirement "
                    "does not hold, that is a missing-coverage finding"
                )
        lines.append("")
    interfaces = _agreed_decisions(data, _work_under(data, covered))
    if interfaces:
        lines.append(
            "Decisions made during this work. Two tasks that decided "
            "incompatibly is exactly the defect this gate is for "
            f"{_decisions_pointer(root)}:"
        )
        lines.extend(f"- {item}" for item in interfaces)
        lines.append("")
    rulings = _rulings(data, task["id"])
    if rulings:
        lines.append(
            "Rulings on questions this gate raised before — settled; judge the "
            "work against them and do not ask them again:"
        )
        lines.extend(f"- {item}" for item in rulings)
        lines.append("")
    lines.append("Criteria this gate must establish:")
    for index, item in enumerate(task.get("acceptances", []), start=1):
        lines.append(f"  {index}. {item['text']}")
    lines.append("")
    previous = _previous_gate_attempts(task)
    if previous:
        lines.append(previous)
        lines.append("")
    lines.extend(_verify_section(data, root, who="gate", verify=verify))
    lines.append(GATE_GUARDRAILS)
    lines.append("")
    lines.append(_gate_verdict_instructions(task, verdict_path))
    return "\n".join(lines)


def _gate_requirements(
    data: dict[str, Any], task: dict[str, Any]
) -> list[dict[str, Any]]:
    """The requirement rows this gate answers for, with their coverage."""
    from . import plans

    wanted = set(task.get("requirement_ids", []))
    rows = plans.coverage(data)
    if not wanted:
        return rows
    return [row for row in rows if row["id"] in wanted]


#: how much of each decision a prompt carries. The title and the opening of the
#: ruling are what show two tasks deciding incompatibly; the full text is in the
#: decisions file, which the prompt names for anyone who needs the rest.
DECISION_LIMIT = 200


def _rulings(data: dict[str, Any], gate_id: str) -> list[str]:
    """Answered questions about this gate, in full: they are what it is held to."""
    return [
        f"{record['id']} {record['title']}: {record['decision']}"
        for record in data.get("decisions", [])
        if gate_id
        and gate_id in record.get("tasks", [])
        and record.get("status") == "active"
        and not decisions.undecided(record)
    ]


def _work_under(data: dict[str, Any], node_ids: Iterable[str]) -> list[str]:
    """The tasks beneath these nodes, looking through any gates on the way.

    A final gate depends on milestone gates, not on tasks, and decisions are
    recorded against tasks — so reading only direct dependencies found no
    decisions at all for exactly the gate that most needs them.
    """
    tasks = data.get("tasks", {})
    seen: set[str] = set()
    found: list[str] = []
    stack = list(node_ids)
    while stack:
        node_id = stack.pop()
        if node_id in seen or node_id not in tasks:
            continue
        seen.add(node_id)
        node = tasks[node_id]
        if node.get("kind") == "gate":
            stack.extend(node.get("depends_on", []))
        else:
            found.append(node_id)
    return sorted(found)


def _agreed_decisions(data: dict[str, Any], task_ids: Iterable[str]) -> list[str]:
    """Decisions recorded by this work, as one-liners.

    Rejected and superseded rulings are left out: they are not what the code
    was built to. Each is cut to `DECISION_LIMIT` — on a real run the final gate's
    prompt was 61% decisions, pasted whole, when the question it asks of them is
    only whether two disagree.
    """
    wanted = set(task_ids)
    found: list[str] = []
    for record in data.get("decisions", []):
        if record.get("status") in ("rejected", "superseded"):
            continue
        if decisions.undecided(record):
            continue  # a question, not something the work was built to
        if not wanted.intersection(record.get("tasks", [])):
            continue
        origin = ", ".join(record.get("tasks", [])) or "unknown"
        found.append(
            f"[{origin}] {record['title']}: "
            f"{_first_sentence(record['decision'], DECISION_LIMIT)}"
        )
    return found


def verify_commands(
    data: dict[str, Any], root: Path, verify: str | None = None
) -> list[str]:
    """How this project is verified: `--verify`, the config, else planning's.

    Named in every execution prompt. Without it each agent was told to "run the
    project's full verification" and left to rediscover what that meant — every
    implementer, reviewer and gate on a real run spent turns finding the same
    test command, and nothing guaranteed they all found the same one.
    """
    from . import config

    if verify and verify.strip():
        return [verify.strip()]
    try:
        configured = (config.load(root).get("run") or {}).get("verify")
    except WritError:
        configured = None
    if configured:
        return [configured]
    pipeline = (data.get("plan") or {}).get("pipeline") or {}
    baseline = pipeline.get("baseline") or {}
    return [str(item) for item in baseline.get("commands") or [] if str(item).strip()]


def _verify_section(
    data: dict[str, Any], root: Path, *, who: str, verify: str | None = None
) -> list[str]:
    commands = verify_commands(data, root, verify)
    if not commands:
        return []
    if who == "implementer":
        head = (
            "Verify with (run these before reporting; a criterion is not passed "
            "until they are green):"
        )
    else:
        head = (
            "The project's verification commands (run them yourself; do not take "
            "anyone's word that they pass):"
        )
    return [head, *(f"  {command}" for command in commands), ""]


def _decisions_pointer(root: Path) -> str:
    path = state.decisions_file(root)
    try:
        shown = path.relative_to(root)
    except ValueError:  # pragma: no cover - the store lives under the root
        shown = path
    return f"(abridged; the full text of each is in {shown})"


def _previous_gate_attempts(task: dict[str, Any]) -> str:
    """What this gate objected to last time, so a re-review can check it closed."""
    attempts = task.get("gate_attempts") or []
    if not attempts:
        return ""
    lines = [
        "You have reviewed this gate before. Repair work has landed since. Check "
        "that what you objected to is actually closed, and do not assume the rest "
        "is still fine — a repair can break what was working:"
    ]
    for number, attempt in enumerate(attempts, start=1):
        lines.append(
            f"  attempt {number}: {attempt.get('decision')} — "
            f"{_first_sentence(attempt.get('summary', '')) or 'no summary'}"
        )
        if attempt.get("findings"):
            lines.append(f"    findings: {', '.join(attempt['findings'])}")
    return "\n".join(lines)


def _gate_verdict_instructions(
    task: dict[str, Any], verdict_path: Path | None
) -> str:
    path = verdict_path or Path(verdict.VERDICT_FILENAME)
    total = len(task.get("acceptances", []))
    return "\n".join(
        [
            "Report your verdict as JSON to this exact path:",
            f"  {path}",
            "",
            "The file must contain JSON only — no prose, no code fence.",
            "",
            "Schema:",
            verdict.GATE_SCHEMA,
            "",
            verdict.GATE_RULES,
            "",
            f"This gate has {total} criteria, numbered 1 to {total}.",
            "",
            "What happens next depends on what you write. `pass` releases the work "
            "waiting behind this gate. `needs-repair` makes writ plan repair work "
            "from your findings and re-run this gate afterwards — so your findings "
            "are the entire brief for that repair. `needs-decision` stops and asks "
            "a human. If you write nothing, the gate stays where it is.",
            "",
            "If you cannot write the file, print the same JSON to stdout inside a "
            "single ```json fenced block instead.",
        ]
    )


def build_repair_prompt(
    data: dict[str, Any],
    task: dict[str, Any],
    root: Path,
    *,
    verdict_path: Path | None = None,
    request: dict[str, Any] | None = None,
    patch_path: Path | None = None,
) -> str:
    """Compose the prompt for planning a repair to the graph.

    Bounded on purpose. The repair planner is not re-planning the project: it sees
    the findings, the requirements they touch, the tasks that exist, and what
    earlier repair rounds already tried. Handing it the whole design document again
    invites it to rewrite the plan, which is how a repair loop turns into an
    unbounded one.
    """
    from . import gates, plans, repair as repair_module

    lines: list[str] = []
    lines.append(
        "You are planning a REPAIR to an executing plan. A gate reviewed the "
        "integrated work and found it wrong. Your job is to propose the work that "
        "makes it right — not to implement it, and not to re-plan the project."
    )
    lines.append("")
    lines.append(f"Repository root: {root.resolve()}")
    lines.append(f"Plan revision: {plans.revision(data)}")
    lines.append("")
    if request:
        lines.append(
            f"Repair request {request['id']} (round {request.get('round', 1)}) "
            f"raised by gate {request['gate']}."
        )
        if request.get("summary"):
            lines.append(f"The gate's account: {request['summary']}")
        lines.append("")
        findings = [
            finding
            for finding in plans.findings(data)
            if finding.id in request.get("findings", [])
        ]
        if findings:
            lines.append("Findings you must address:")
            for finding in findings:
                lines.append(f"- {finding.id} [{finding.severity}] {finding.message}")
                if finding.requirement_ids:
                    lines.append(
                        f"    affects: {', '.join(finding.requirement_ids)}"
                    )
                if finding.suggested_action:
                    lines.append(f"    required outcome: {finding.suggested_action}")
            lines.append("")
        rulings = _rulings(data, request.get("gate") or "")
        if rulings:
            lines.append(
                "Rulings on questions raised about this gate — settled; the "
                "repair must follow them:"
            )
            lines.extend(f"- {item}" for item in rulings)
            lines.append("")
        previous = _previous_repairs(data, request)
        if previous:
            lines.append(previous)
            lines.append("")
    requirements = plans.coverage(data)
    if requirements:
        lines.append("Requirement inventory (you may not add to or weaken this):")
        for row in requirements:
            lines.append(
                f"- {row['id']} [{row['priority']}, {row['state']}] {row['text']}"
            )
        lines.append("")
    lines.append("The graph as it stands:")
    for task_id in sorted(data.get("tasks", {})):
        entry = data["tasks"][task_id]
        kind = "gate" if entry.get("kind") == "gate" else "task"
        fence = (
            f" owns {', '.join(entry['allowed'])}" if entry.get("allowed") else ""
        )
        lines.append(
            f"- {task_id} [{kind}, {entry['status']}] {entry['title']}{fence}"
        )
        if entry.get("depends_on"):
            lines.append(f"    after: {', '.join(entry['depends_on'])}")
    lines.append("")
    lines.append(
        "Read the repository and the failing behaviour before proposing anything. "
        "A repair written from the findings alone, without confirming what the code "
        "actually does, is a guess."
    )
    lines.append("")
    lines.append("Write the patch as JSON to this exact path:")
    lines.append(f"  {patch_path or 'patch.json'}")
    lines.append("")
    lines.append("The file must contain JSON only — no prose, no code fence.")
    lines.append("")
    lines.append("Schema:")
    lines.append(repair_module.PATCH_SCHEMA)
    lines.append("")
    lines.append(repair_module.PATCH_RULES)
    lines.append("")
    lines.append(
        f"Set `base_revision` to {plans.revision(data)}. Writ refuses a patch "
        "planned against a revision the graph has moved past, so do not guess it."
    )
    lines.append("")
    lines.append(
        "Writ validates your patch before applying any of it: it will refuse one "
        "that weakens an acceptance criterion, drops a requirement, changes a task "
        "an agent is working on, or leaves a blocking finding with no disposition. "
        "A refusal is returned to you with its reasons."
    )
    return "\n".join(lines)


def _previous_repairs(data: dict[str, Any], request: dict[str, Any]) -> str:
    """What earlier rounds on this gate tried, so a repair does not repeat one."""
    from . import repair as repair_module

    earlier = [
        item
        for item in repair_module.requests(data)
        if item.get("gate") == request.get("gate") and item["id"] != request["id"]
    ]
    if not earlier:
        return ""
    lines = [
        "Earlier repair rounds on this gate. The gate failed again after these, so "
        "whatever they did was not enough — do not propose the same thing again:"
    ]
    for item in earlier:
        lines.append(
            f"  {item['id']} ({item.get('status')}): "
            f"{_first_sentence(item.get('analysis') or item.get('summary', ''))}"
        )
        for task_id in item.get("applied_tasks", []):
            entry = data.get("tasks", {}).get(task_id)
            if entry:
                lines.append(
                    f"    added {task_id} [{entry['status']}] {entry['title']}"
                )
    return "\n".join(lines)


def _first_sentence(text: str, limit: int = 160) -> str:
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _design_excerpt(task: dict[str, Any], root: Path, limit: int = 4000) -> str:
    doc = task.get("design_doc")
    section = task.get("design_section")
    if not doc or not section:
        return ""
    path = Path(doc)
    if not path.is_absolute():
        path = root / path
    text = planner.section_text(path, section)
    return text[:limit]


def new_run_id(task_id: str, taken: Iterable[str] = ()) -> str:
    """A unique run id for this task.

    Ids are timestamped to the second and two runs of the same task can easily
    start within one second — dispatch then review, or a quick retry — so a
    collision is disambiguated with a suffix rather than silently overwriting
    the earlier run's record.
    """
    base = f"{task_id}-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}"
    existing = set(taken)
    if base not in existing:
        return base
    for suffix in range(2, 100):
        candidate = f"{base}-{suffix}"
        if candidate not in existing:
            return candidate
    raise WritError(f"too many runs of {task_id} in one second")


TIMEOUT_NOTE = "writ: agent exceeded its timeout and was terminated"


def _tee(
    source: IO[str],
    sink: IO[str],
    mirror: IO[str] | None,
    prefix: str = "",
    lock: threading.Lock | None = None,
) -> None:
    """Copy a stream to a file and optionally to the terminal, line by line.

    Read in small chunks rather than by line: an agent that draws progress with
    carriage returns and no newline would otherwise appear frozen.

    `lock`, when given, makes each mirrored line one atomic write. Several agents
    running at once each have two of these pumps, and a character-at-a-time mirror
    would splice their output together mid-word — which is the reason `writ run`
    had nothing to mirror to at all. Holding the lock for a whole line is enough:
    the label at the start of a line is what makes interleaved output attributable,
    and it is only true if nothing else can write between the label and the text.
    The transcript on disk is unaffected either way, and stays byte-for-byte what
    the agent wrote.
    """
    at_line_start = True
    pending: list[str] = []

    def flush_line(final: bool = False) -> None:
        """Write one buffered line to the terminal under the lock."""
        if mirror is None or not pending:
            return
        line = "".join(pending)
        pending.clear()
        if final and not line.endswith(("\n", "\r")):
            line += "\n"
        if lock is not None:
            with lock:
                mirror.write(line)
                mirror.flush()
        else:
            mirror.write(line)
            mirror.flush()

    while True:
        chunk = source.read(1)
        if not chunk:
            break
        sink.write(chunk)
        sink.flush()
        if mirror is None:
            continue
        if prefix and at_line_start:
            pending.append(prefix)
        pending.append(chunk)
        at_line_start = chunk in ("\n", "\r")
        if at_line_start:
            flush_line()
    # Whatever the agent left without a trailing newline: a prompt it was waiting
    # on, or a progress line it never finished.
    flush_line(final=True)


#: the agent's own event stream, kept beside the transcript it was rendered into
EVENTS_FILENAME = "events.jsonl"


def _pump_events(
    source: IO[str],
    transcript: IO[str],
    raw: IO[str],
    shape: str,
    mirror: IO[str] | None,
    prefix: str = "",
    lock: threading.Lock | None = None,
    reasons: list[str] | None = None,
) -> None:
    """Read a structured event stream, showing activity and recording speech.

    Three destinations, because they answer different questions. `raw` gets every
    event byte for afterwards — it is the only place the stop reason survives.
    `transcript` gets just what the agent said, so `stdout.log` stays what text
    mode would have written and everything that parses it keeps working. `mirror`
    gets a line per event worth watching, which is the whole reason for asking
    for events in the first place.

    Line-oriented rather than chunked like `_tee`: an event is a line by
    definition, and a partial line carries nothing to render yet.
    """
    renderer = Renderer(shape)
    for line in source:
        raw.write(line)
        raw.flush()
        rendered = renderer.feed(line)
        if rendered.text:
            transcript.write(rendered.text)
            transcript.flush()
        if rendered.stop_reason is not None and reasons is not None:
            reasons.append(rendered.stop_reason)
        if mirror is None or not rendered.activity:
            continue
        shown = "".join(f"{prefix}{item}\n" for item in rendered.activity)
        if lock is not None:
            with lock:
                mirror.write(shown)
                mirror.flush()
        else:
            mirror.write(shown)
            mirror.flush()


def _feed(process: subprocess.Popen, prompt: str) -> None:
    """Deliver the prompt on stdin, tolerating an agent that never reads it."""
    if process.stdin is None:
        return
    try:
        process.stdin.write(prompt)
    except BrokenPipeError:
        # an agent that ignores stdin is the caller's problem to diagnose,
        # not a reason to fail the run here
        pass
    finally:
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass


def run_agent(
    command: list[str],
    prompt: str,
    directory: Path,
    cwd: str | Path,
    timeout: int | None,
    *,
    stream: bool = False,
    prefix: str = "",
    event_shape: str = "",
    stop_reasons: list[str] | None = None,
    mirror_lock: threading.Lock | None = None,
) -> int:
    """Run a coding agent to completion, leaving a full transcript on disk.

    Shared by task dispatch and by `writ plan`: prompt on stdin, streams to
    files, timeout enforced by killing the whole process group. Returns the exit
    code; 124 means it was killed for exceeding its timeout.

    With `stream`, output is mirrored to this terminal as it arrives, so a long
    agent run is visibly working instead of looking hung. The transcript on disk
    is written either way and is the same bytes.

    With `event_shape`, the agent was asked for structured events instead of
    prose. They are rendered into activity for the terminal and speech for the
    transcript, and kept verbatim in `events.jsonl`. `stdout.log` still holds
    only what the agent said, so this changes what a watching person sees without
    changing what anything downstream reads. Rendering happens whether or not
    anyone is watching: `stop_reasons`, filled from the stream, is how a caller
    tells a truncated run from one that never started, and `--quiet` should not
    cost that.

    `mirror_lock` is shared by every agent a caller runs at once, so each mirrored
    line reaches the terminal whole. Without it two concurrent agents splice their
    output together mid-word and the prefix that says which one is speaking stops
    meaning anything.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "prompt.txt").write_text(prompt, encoding="utf-8")
    piped = stream or bool(event_shape)
    with (directory / "stdout.log").open("w", encoding="utf-8") as out, (
        directory / "stderr.log"
    ).open("w", encoding="utf-8") as err:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE if piped else out,
            stderr=subprocess.PIPE if piped else err,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        if not piped:
            try:
                process.communicate(input=prompt, timeout=timeout)
                return process.returncode
            except subprocess.TimeoutExpired:
                return _timed_out(process, directory, out, err)

        # tee in threads: the agent may write a lot to either stream, and a
        # full pipe buffer would deadlock a single-threaded reader
        raw: IO[str] | None = None
        if event_shape:
            raw = (directory / EVENTS_FILENAME).open("w", encoding="utf-8")
        try:
            if event_shape and raw is not None:
                stdout_pump = threading.Thread(
                    target=_pump_events,
                    args=(
                        process.stdout,
                        out,
                        raw,
                        event_shape,
                        sys.stdout if stream else None,
                        prefix,
                        mirror_lock,
                        stop_reasons,
                    ),
                    daemon=True,
                )
            else:
                stdout_pump = threading.Thread(
                    target=_tee,
                    args=(
                        process.stdout,
                        out,
                        sys.stdout if stream else None,
                        prefix,
                        mirror_lock,
                    ),
                    daemon=True,
                )
            pumps = [
                stdout_pump,
                threading.Thread(
                    target=_tee,
                    args=(
                        process.stderr,
                        err,
                        sys.stderr if stream else None,
                        prefix,
                        mirror_lock,
                    ),
                    daemon=True,
                ),
            ]
            for pump in pumps:
                pump.start()
            try:
                _feed(process, prompt)
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                for pump in pumps:
                    pump.join(timeout=1)
                return _timed_out(process, directory, out, err, raw=raw)
            for pump in pumps:
                pump.join(timeout=5)
            return process.returncode
        finally:
            if raw is not None:
                raw.close()


def _timed_out(
    process: subprocess.Popen,
    directory: Path,
    out: IO[str],
    err: IO[str],
    raw: IO[str] | None = None,
) -> int:
    """Kill a run that overran, recording whether it had said anything."""
    _terminate(process.pid)
    process.wait(timeout=10)
    out.flush()
    err.flush()
    # The event stream counts as having said something even when the transcript is
    # empty: an agent that spent the whole timeout thinking wrote nothing a person
    # would read, but it plainly did run, and calling that silent sends whoever
    # reads the message looking at authentication instead.
    if raw is not None:
        raw.flush()
    busy = raw is not None and raw.tell() > 0
    # measured before writing our own note, so callers can still tell a silent
    # hang from an agent that produced output and then stalled
    if err.tell() == 0 and out.tell() == 0 and not busy:
        (directory / "silent").write_text("", encoding="utf-8")
    err.write(f"\n{TIMEOUT_NOTE}\n")
    return 124


def produced_output(directory: Path) -> bool:
    """Whether the agent itself wrote anything, ignoring writ's own notes.

    An event stream counts. A run that thought for four minutes and was cut off
    before it could act leaves no transcript at all, and the question this answers
    is whether the agent ran — not whether it managed to say something.
    """
    if (directory / "silent").exists():
        return False
    events = directory / EVENTS_FILENAME
    if events.exists() and events.stat().st_size > 0:
        return True
    for name in ("stdout.log", "stderr.log"):
        path = directory / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace").replace(
            TIMEOUT_NOTE, ""
        )
        if text.strip():
            return True
    return False


# A tool call that arrived as text rather than being made. Agent harnesses parse
# these tags out of a model's reply and execute them, so seeing them in the
# transcript means the harness did not recognise this one and printed it instead.
UNPARSED_CALL = re.compile(r"<\s*.{0,4}?invoke\s+name\s*=", re.IGNORECASE)


# What a model provider or agent harness prints when the call itself failed.
# Anchored to words rather than bare status codes, because a transcript full of
# code is full of numbers, and read only from the end of the output, where a
# failure that ended the run would be.
PROVIDER_ERROR = re.compile(
    r"(?i)\b(?:rate[ _-]?limit(?:ed|_error)?|too many requests|overloaded(?:_error)?"
    r"|insufficient_quota|quota (?:exceeded|exhausted)|service unavailable"
    r"|bad gateway|gateway time-?out|ECONNRESET|ETIMEDOUT|EAI_AGAIN"
    r"|connection (?:reset|refused|error)|api_error"
    r"|(?:http|status|error|code)[\s:=\"']{0,4}(?:429|500|502|503|504|529))\b"
)

#: how much of the end of each log `provider_error` reads
PROVIDER_TAIL = 8192


def provider_error(directory: Path) -> str | None:
    """The provider failure the transcript ends on, if it ends on one.

    Only asked of a run that exited non-zero with no verdict. That shape used to
    fail the task outright — a 429 or an overloaded provider was recorded as the
    work having failed — when it is exactly the failure an infrastructure retry
    exists for.
    """
    for name in ("stderr.log", "stdout.log"):
        path = directory / name
        if not path.exists():
            continue
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - PROVIDER_TAIL))
            text = handle.read().decode("utf-8", errors="replace")
        found = PROVIDER_ERROR.search(text)
        if found:
            return found.group(0)
    return None


def unparsed_tool_call(directory: Path) -> bool:
    """Whether the transcript shows a tool call the harness failed to make.

    A model that garbles its own call syntax — a stray character inside the tag,
    unbalanced closing tags — gets that text printed rather than executed. The
    turn then ends as if the model had merely spoken, and the harness exits 0
    having done nothing, so the run looks like an agent that worked and forgot to
    write its verdict. It never got as far as working.

    Matched loosely on purpose: it is the mangling that produces this, so the tag
    is by definition not well formed, and a pattern strict enough to require
    valid syntax would miss precisely the cases worth reporting.
    """
    log = directory / "stdout.log"
    if not log.exists():
        return False
    return UNPARSED_CALL.search(
        log.read_text(encoding="utf-8", errors="replace")
    ) is not None


def prepare(
    root: Path,
    task_id: str,
    agent: str,
    agent_args: list[str],
    *,
    model: str | None = None,
    timeout: int | None,
    cwd: str | None,
    force: bool,
    role: str = "agent",
    max_rework: int | None = None,
    verify: str | None = None,
) -> tuple[str, Path, str, agents.ResolvedAgent]:
    """Create the run directory and record the run as `starting`.

    `role` selects the prompt and how the resulting verdict is applied: an
    implementing agent parks the task at `awaiting-review`, a reviewer completes
    or fails it.

    `max_rework` is recorded on the run rather than read when the verdict lands,
    because the verdict is applied by `_finish` in a worker thread that has the
    run and nothing else. Recording it also makes the run say which budget it was
    judged under, which a run read weeks later otherwise cannot tell you.
    """
    resolved = agents.resolve(agent, agent_args, model)
    with state.transaction(root) as data:
        task = get_task(data, task_id)
        # Refuse to put a second agent on a task that already has a live one.
        # `writ run` avoids this by selecting on one thread, but two processes
        # (a `--force` run, or a detached dispatch alongside a run) can still
        # both get here. This check is inside the lock, so the store settles it
        # rather than a session file: whoever commits first owns the task.
        active = _live_run_for(data, task_id)
        if active is not None:
            raise WritError(
                f"{task_id} already has a running agent (run {active}). "
                f"Wait for it, or stop it with `writ cancel {active}`."
            )
        if role == "gate":
            blockers = blocking_dependencies(data, task)
            if blockers and not force:
                raise WritError(
                    f"{task_id} cannot review yet: {', '.join(blockers)} are not "
                    "complete (use --force to gate it anyway)"
                )
        elif role == "repair":
            request = repair.request_for_gate(data, task_id)
            if request is None and not force:
                raise WritError(f"{task_id} has no open repair request")
        elif role == "reviewer":
            if task["status"] not in ("awaiting-review", "reviewing") and not force:
                raise WritError(
                    f"{task_id} is {task['status']}, not awaiting review "
                    "(use --force to review it anyway)"
                )
        elif not force:
            blockers = blocking_dependencies(data, task)
            if blockers:
                raise WritError(
                    f"{task_id} is blocked by incomplete dependencies: "
                    f"{', '.join(blockers)} (use --force to override)"
                )
        run_id = new_run_id(task_id, data["runs"])
        directory = state.run_dir(root, run_id)
        directory.mkdir(parents=True, exist_ok=True)
        verdict_path = directory / verdict.VERDICT_FILENAME
        patch_path = directory / PATCH_FILENAME
        if role == "repair":
            request = repair.request_for_gate(data, task_id)
            prompt = build_repair_prompt(
                data,
                task,
                Path(root),
                verdict_path=verdict_path,
                request=request,
                patch_path=patch_path,
            )
            if request is not None:
                request["status"] = "planning"
                request.setdefault("attempts", []).append(
                    {"run": run_id, "at": utcnow()}
                )
        elif role == "gate":
            prompt = build_gate_prompt(
                data, task, Path(root), verdict_path=verdict_path, verify=verify
            )
        elif role == "reviewer":
            prompt = build_review_prompt(
                data, task, Path(root), verdict_path=verdict_path, verify=verify
            )
        else:
            prompt = build_prompt(
                data, task, Path(root), verdict_path=verdict_path, verify=verify
            )
        (directory / "prompt.txt").write_text(prompt, encoding="utf-8")
        data["runs"][run_id] = {
            "id": run_id,
            "task": task_id,
            "role": role,
            "status": "starting",
            "command": resolved.command,
            "model": model,
            "cwd": str(Path(cwd or root).expanduser().resolve()),
            "timeout": timeout,
            "max_rework": DEFAULT_MAX_REWORK if max_rework is None else max_rework,
            "created_at": utcnow(),
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
            "pid": None,
            # The process that claimed this task, recorded before any agent
            # starts. Without it there is a window between claiming and running
            # in which the run has no live pid and looks abandoned, so a second
            # process could claim the same task.
            #
            # `owner` is the composite identity — pid, start time, host, token —
            # and `owner_pid` is the same pid on its own, kept because a project
            # planned by an older writ has only that field and everything that
            # reads ownership still has to work. A bare pid cannot survive being
            # reused, which is what `owner` is for.
            "owner": procs.identify().to_dict(),
            "owner_pid": os.getpid(),
            "dir": str(directory),
        }
        task.setdefault("runs", []).append(run_id)
        if role == "repair":
            # A repair planner does not hold the gate; the gate is already held,
            # waiting for the work this run will propose. Moving it to `running`
            # would say an agent is working *on the gate*, which no reader would
            # read as "its repair is being planned".
            pass
        elif role in ("reviewer", "gate"):
            task["status"] = "reviewing"
        else:
            task["status"] = "running"
        task["updated_at"] = utcnow()
        refresh_milestones(data)
    return run_id, directory, prompt, resolved


def run_owner(run: dict[str, Any]) -> Any:
    """The one process responsible for this run, or None if nothing is recorded.

    A precedence chain, not a set, and the precedence is the point. Three things
    can own a run over its life, and the later ones supersede the earlier:

    * a detached supervisor, which outlives the CLI that spawned it;
    * the agent's own process, once it has been spawned;
    * the process that claimed the task, which covers only the window before an
      agent has a pid at all.

    So an agent that has died is a dead run even though the scheduler that claimed
    it is still very much alive — asking whether *anything* on the list is running
    would report every crashed agent as working, for as long as the terminal that
    started it stayed open.

    Returned as whatever the store holds: a composite identity where one was
    recorded, a bare pid from an older writ, and `procs` accepts both.
    """
    for candidate in (
        run.get("supervisor") or run.get("supervisor_pid"),
        run.get("identity") or run.get("pid"),
        run.get("owner") or run.get("owner_pid"),
    ):
        if candidate:
            return candidate
    return None


def run_alive(run: dict[str, Any]) -> bool:
    """Whether the process responsible for this run is still running.

    Identity-checked, so a pid the kernel has since handed to an unrelated
    process no longer counts as this run still working.

    A settled run is never alive, whatever its recorded owner is doing. For a run
    that finished in this very process the owner *is* the caller asking the
    question, and without this a completed run would report itself live.
    """
    if run.get("status") not in ACTIVE_RUN_STATUSES:
        return False
    return procs.alive(run_owner(run))


def run_abandoned(run: dict[str, Any]) -> bool:
    """Whether the process responsible for this run is *provably* gone.

    The inverse of `run_alive` is not good enough for reaping. Reaping rewrites a
    task's status, so it wants proof rather than a failed probe: a run recorded on
    another host, or one whose pid is alive but whose identity cannot be checked,
    is left alone.

    A run with no recorded owner is abandoned — nothing was ever claimed, so there
    is nothing that could still be making progress.
    """
    owner = run_owner(run)
    if owner is None:
        return True
    return procs.confirmed_dead(owner)


def _live_run_for(data: dict[str, Any], task_id: str) -> str | None:
    """The id of a run on this task whose process is still alive, if any.

    A recorded-but-dead run does not count: that is what `reap` is for, and
    treating it as live would make a crashed agent block its task forever.
    """
    for run_id in reversed(data["tasks"].get(task_id, {}).get("runs", [])):
        run = data["runs"].get(run_id)
        if run is None or run["status"] not in ACTIVE_RUN_STATUSES:
            continue
        if run_alive(run):
            return run_id
    return None


def execute(
    root: Path,
    run_id: str,
    *,
    stream: bool = False,
    prefix: str = "",
    lock: threading.Lock | None = None,
) -> int:
    """Run the agent synchronously and record the outcome.

    Unlike `run_agent`, this records the pid in project state so another
    terminal can watch or cancel the run, and it converts the exit code into
    task status. With `stream`, output is also mirrored to this terminal.

    `lock` is shared by every concurrent caller mirroring to the same terminal, so
    `writ run` can stream several agents at once and have each line stay whole and
    labelled. Without it a single run streams exactly as before.
    """
    data = state.load(root)
    run = data["runs"].get(run_id)
    if run is None:
        raise WritError(f"unknown run: {run_id}")
    directory = Path(run["dir"])
    prompt = (directory / "prompt.txt").read_text(encoding="utf-8")
    stdout_path = directory / "stdout.log"
    stderr_path = directory / "stderr.log"
    try:
        with stdout_path.open("w", encoding="utf-8") as out, stderr_path.open(
            "w", encoding="utf-8"
        ) as err:
            process = subprocess.Popen(
                run["command"],
                cwd=run["cwd"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE if stream else out,
                stderr=subprocess.PIPE if stream else err,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            _mark_running(root, run_id, process.pid)
            pumps: list[threading.Thread] = []
            if stream:
                pumps = [
                    threading.Thread(
                        target=_tee,
                        args=(process.stdout, out, sys.stdout, prefix, lock),
                        daemon=True,
                    ),
                    threading.Thread(
                        target=_tee,
                        args=(process.stderr, err, sys.stderr, prefix, lock),
                        daemon=True,
                    ),
                ]
                for pump in pumps:
                    pump.start()
            try:
                if stream:
                    _feed(process, prompt)
                    process.wait(timeout=run.get("timeout"))
                else:
                    process.communicate(input=prompt, timeout=run.get("timeout"))
                code = process.returncode
            except subprocess.TimeoutExpired:
                for pump in pumps:
                    pump.join(timeout=1)
                code = _timed_out(process, directory, out, err)
            else:
                for pump in pumps:
                    pump.join(timeout=5)
    except FileNotFoundError as exc:
        _finish(root, run_id, 127, note=f"agent not found: {exc}")
        raise WritError(f"agent command not found: {run['command'][0]}") from exc
    _finish(root, run_id, code)
    return code


def execute_guarded(root: Path, run_id: str, **kwargs: Any) -> int:
    """`execute`, with the guarantee that a raise still settles the run.

    For the single-shot entry points — `writ dispatch`, `writ review`, and the
    detached supervisor — which have the same exposure the scheduler had: the task
    is already claimed by `prepare`, so an exception between there and `_finish`
    leaves it claimed by a process that is about to exit.

    A later `reap` would recover it, since the owner really is dead by then. But
    "recovered whenever someone next runs writ" is not the same as recovered: the
    task is invisible to every queue until then, and the record says nothing about
    what went wrong. The exception is re-raised unchanged, so the CLI reports it
    exactly as before.
    """
    try:
        return execute(root, run_id, **kwargs)
    except BaseException as exc:
        try:
            reconcile(root, run_id, failures.classify(exc))
        except Exception:  # pragma: no cover - the store is unavailable
            # The original failure is the one worth raising. Reaping remains the
            # backstop for the record.
            pass
        raise


def _finish_repair(
    data: dict[str, Any],
    run: dict[str, Any],
    gate: dict[str, Any],
    directory: Path,
    actor: str,
) -> None:
    """Validate a proposed patch and apply it, or return it with its reasons.

    Writ owns this, not the agent. The patch has already been written by something
    that wants its own proposal accepted, so every invariant that matters — the
    revision it targets, the bars it must not weaken, the tasks it must not touch —
    is checked here, and a patch that fails any of them changes nothing.

    A refused patch leaves the request open. The gate stays held, the findings stay
    open, and the next round is given this attempt's refusal, so the loop is bounded
    by `repair.exhausted` rather than by hoping the next patch is better.
    """
    from .model import add_evidence

    request = repair.request_for_gate(data, gate["id"])
    if request is None:
        run["patch_error"] = "no open repair request for this gate"
        return
    text = _patch_text(directory)
    if text is None:
        run["patch_error"] = "the repair planner wrote no patch"
        add_evidence(
            gate,
            f"{actor} planned no repair: it wrote no patch to "
            f"{directory / PATCH_FILENAME}",
            actor="writ",
        )
        request["status"] = "open"
        return
    try:
        patch = repair.load_patch(text)
    except WritError as exc:
        run["patch_error"] = str(exc)
        add_evidence(gate, f"unusable repair patch from {actor}: {exc}", actor="writ")
        request["status"] = "open"
        return
    findings = repair.validate(data, patch, request)
    blocking = [finding for finding in findings if finding.blocking]
    run["patch_findings"] = [finding.to_dict() for finding in findings]
    if blocking:
        reasons = "; ".join(finding.message for finding in blocking)
        run["patch_error"] = reasons
        add_evidence(
            gate,
            f"writ refused {actor}'s patch: {reasons}",
            actor="writ",
        )
        request["status"] = "open"
        request.setdefault("refusals", []).append(
            {
                "at": utcnow(),
                "run": run["id"],
                "reasons": [finding.to_dict() for finding in blocking],
            }
        )
        if not repair.patches_left(request):
            # Out of patches. The gate stops for a human rather than cycling: what
            # is wrong is not the wording of the patch but what is being asked of
            # it, and another round would refuse for the same reason.
            gate["held"] = {
                "reason": "repair-refused",
                "at": utcnow(),
                "request": request["id"],
            }
            add_evidence(
                gate,
                f"writ refused {repair.refusals(request)} patches for "
                f"{request['id']}; {gate['id']} is held for a human "
                f"(writ show {request['id']})",
                actor="writ",
            )
        return
    if patch.empty and patch.questions:
        ruled = verdict._rule_for_gate(data, gate, patch.questions, actor=actor)
        if ruled is not None:
            # Autonomous: its recommendations are the rulings, and it plans the
            # repair again with them in its prompt.
            request["status"] = "open"
            request["questions"] = list(patch.questions)
            add_evidence(
                gate,
                f"{actor} asked {len(patch.questions)} question(s); decided "
                f"autonomously ({', '.join(ruled)}), and the repair is planned "
                "again to follow them",
                actor="writ",
            )
            return
        # The planner could not repair this without a ruling. Same destination as a
        # gate's own `needs-decision`: a human, through the decision log.
        for question in patch.questions:
            decisions.propose(
                data,
                title=str(question.get("question", ""))[:72] or "repair question",
                decision=(
                    "Undecided: the repair planner could not close the finding "
                    "without a ruling."
                ),
                context=str(question.get("context", question.get("question", "")))
                + (
                    f" Recommended: {question['recommendation']}"
                    if question.get("recommendation")
                    else ""
                ),
                consequences=f"{gate['id']} stays held until this is settled.",
                proposed_by=actor,
                tasks=[gate["id"]],
            )
        request["status"] = "proposed"
        request["questions"] = list(patch.questions)
        gate["held"] = {
            "reason": "needs-decision",
            "at": utcnow(),
            "request": request["id"],
        }
        add_evidence(
            gate,
            f"{actor} raised {len(patch.questions)} question(s) rather than "
            "proposing repair work; recorded in the decision log",
            actor="writ",
        )
        return
    applied = repair.apply_patch(data, patch, request, actor=actor)
    run["patch_applied"] = applied
    add_evidence(
        gate,
        f"repair {request['id']} applied: added {', '.join(applied['tasks'])}; "
        f"this gate now waits for them (plan revision {applied['revision']})",
        actor="writ",
    )


def _patch_text(directory: Path) -> str | None:
    """The patch file, or JSON the planner printed to stdout instead."""
    from .planning import extract_json

    path = directory / PATCH_FILENAME
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    stdout = directory / "stdout.log"
    if not stdout.exists():
        return None
    embedded = extract_json(stdout.read_text(encoding="utf-8", errors="replace"))
    if embedded is None:
        return None
    path.write_text(embedded + "\n", encoding="utf-8")
    return embedded


def _mark_running(root: Path, run_id: str, pid: int) -> None:
    # The agent's identity is probed rather than stamped: its start time is read
    # from the process itself, so a later check compares two readings of the same
    # thing instead of trusting a clock we happened to look at. That is what makes
    # `cancel` refuse to signal a pid the kernel has recycled since.
    identity = procs.identify(pid)
    with state.transaction(root) as data:
        run = data["runs"][run_id]
        run["status"] = "running"
        run["pid"] = pid
        run["identity"] = identity.to_dict()
        run["started_at"] = utcnow()


def _finish(root: Path, run_id: str, code: int, note: str | None = None) -> None:
    """Record the run's outcome, and apply the agent's verdict to its task.

    An exit code is not a judgement. A process can exit 0 having done nothing and
    exit non-zero after finishing the work, so the task's status comes from the
    verdict the agent wrote, not from `code`. A missing or invalid verdict leaves
    the task's criteria untouched and says so — silently guessing is what this
    whole mechanism exists to avoid.
    """
    with state.transaction(root) as data:
        run = data["runs"][run_id]
        # A cancelled run has already been settled by `cancel`, which killed the
        # process. Reaching here means this thread lost the race with it, and
        # rewriting the record would turn a deliberate stop into a failure.
        if run["status"] == "cancelled":
            return
        run["status"] = "completed" if code == 0 else "failed"
        run["exit_code"] = code
        run["finished_at"] = utcnow()
        if note:
            run["note"] = note
        role = run.get("role", "agent")
        task = data["tasks"].get(run["task"])
        if task is None:
            refresh_milestones(data)
            return
        directory = Path(run["dir"])
        actor = _actor(run)
        if role == "repair":
            _finish_repair(data, run, task, directory, actor)
            refresh_milestones(data)
            return
        # The run's own start time bounds the search for a misplaced verdict, so
        # a file left by an earlier run cannot be mistaken for this one's.
        since = _started_epoch(run)
        found_at: Path | None = None
        try:
            reported, found_at = verdict.read(
                directory,
                role=role,
                root=Path(run.get("cwd") or root),
                since=since,
            )
        except WritError as exc:
            reported = None
            run["verdict_error"] = str(exc)
            add_evidence(task, f"unusable verdict from {actor}: {exc}", actor="writ")

        if reported is not None:
            try:
                verdict.check_scope(reported, task, str(directory))
                verdict.check_coverage(reported, task, str(directory))
            except WritError as exc:
                run["verdict_error"] = str(exc)
                add_evidence(task, f"unusable verdict from {actor}: {exc}", actor="writ")
            else:
                if found_at is not None:
                    # Used, and said out loud. An agent that writes its report to
                    # a path of its own choosing will keep doing it, and the
                    # remedy is in the prompt, not in a wider search next time.
                    misplaced = (
                        f"{actor} wrote its verdict to {found_at} instead of "
                        f"{directory / verdict.VERDICT_FILENAME}; writ used it "
                        "from there"
                    )
                    run["verdict_misplaced"] = str(found_at)
                    add_evidence(task, misplaced, actor="writ")
                run["verdict"] = {
                    "outcome": reported.outcome,
                    "decision": reported.decision,
                    "summary": reported.summary,
                    "passed": reported.passed,
                    "unmet": reported.unmet,
                    "decisions": [p.title for p in reported.decisions],
                }
                if reported.downgraded:
                    # A claim writ lowered is not an unusable verdict: it was
                    # applied, just not as claimed. Kept in its own field so
                    # neither reads as the other.
                    run["verdict_downgraded"] = reported.downgraded
                status = verdict.apply(
                    data,
                    task,
                    reported,
                    actor=actor,
                    max_rework=run.get("max_rework"),
                )
                run["resulting_status"] = status
                refresh_milestones(data)
                if reported.decisions:
                    decisions.sync_markdown(root, data)
                return

        # No usable verdict. Record what happened without inventing a judgement.
        reason = "exited without writing a usable verdict" + (
            f" ({note})" if note else ""
        )
        # An agent that printed nothing did not "forget to report" — it almost
        # certainly never ran. Recorded as a separate field so the run says which
        # of the two happened; the reason string is the operator-facing sentence
        # and carries it too, because that is what the dashboard shows.
        #
        # Not for a timeout (124) or a run that already carries a note: a killed
        # agent is a hang, not a failed invocation, and a note already says what
        # went wrong. Guessing over either would replace a true explanation with
        # a plausible wrong one.
        silent = code != 124 and not note and not produced_output(directory)
        if silent:
            run["no_output"] = True
            reason += (
                " — and printed no output at all, so it most likely never "
                "reached a model (unknown model id, missing provider "
                "credentials, or exhausted quota)"
            )
        # A provider that refused the call (rate limit, overload, a 5xx) reached
        # the model and was turned away: nothing about the task was judged, so
        # this is named for `failures.from_run` to retry rather than send back.
        elif code not in (0, 124) and not note and (
            marker := provider_error(directory)
        ):
            run["provider_error"] = marker
            reason += (
                f" — and its output reports a provider error ({marker!r}), so "
                "the model call failed rather than the work"
            )
        # The opposite shape of a silent run: plenty of output, none of it a
        # report, because the agent's last act was a tool call that was printed
        # instead of run. That is the model breaking its own call syntax, so the
        # remedy is the model rather than the prompt — and re-reading a transcript
        # that ends mid-call tells you nothing unless something names what the
        # fragment at the end of it is.
        elif code != 124 and not note and unparsed_tool_call(directory):
            run["unparsed_tool_call"] = True
            reason += (
                " — and its transcript ends in a tool call that was printed "
                "rather than made, so the model garbled the call syntax and the "
                "turn ended without it doing the work"
            )
        # Classified before the task's status is decided, because the
        # classification is what decides it. Two of the ways a run ends unjudged
        # are writ's own machinery failing rather than the work coming out wrong
        # — a killed hang, and an agent that never reached a model — and neither
        # raises, so neither reached `failures.classify`. Both used to land the
        # task at `failed`, which reads as "this work was judged and found
        # wanting" and is the exact signal corruption the classification exists
        # to stop. It needs the `no_output` flag above, hence the order.
        failure = failures.from_run(run)
        if failure is not None:
            run["failure"] = failure.to_dict()
            if failure.retryable:
                # Counted on the task, in the same place `reconcile` counts the
                # exception path. The budget has to be the same budget whichever
                # way the job died, and it has to survive a scheduler that is
                # killed mid-backoff and resumed.
                _note_infrastructure_failure(data, run, failure)
        # Where the task lands depends on which role failed to report, the same
        # distinction `reap` draws: a lost implementation returns to the queue,
        # but a lost *review* leaves the implementation standing and only the
        # judgement missing, so the task goes back to `awaiting-review`. Sending
        # it to `planned` discarded a completed implementation's place in the
        # queue and left criteria marked passed under a status that says the work
        # has not started — a state no reader can make sense of.
        if task["status"] in ("running", "reviewing"):
            if failure is not None:
                # Nothing judged this, so the task is returned to the status it
                # was claimed from and is selectable again. Under the scheduler
                # the retry comes out of the infrastructure budget; under a bare
                # `dispatch` it means the operator fixes the timeout or the model
                # id and runs it again, rather than first having to undo a status
                # that says the work failed.
                task["status"] = INTERRUPTED_STATUS.get(task["status"], "planned")
            # Nothing classified it, so it is the agent's own doing — but it is
            # still not a judgement, and none of these lands at `failed` any
            # more. A crashed *reviewer* used to fail the implementation it was
            # reading, and a crashed gate failed the gate and with it everything
            # behind it, when in both cases the work under review was untouched.
            elif role == "reviewer":
                task["status"] = "awaiting-review"
            elif role == "gate":
                task["status"] = "planned"
            else:
                # An implementation that stopped without reporting is an attempt
                # at the work that came to nothing: counted against the rework
                # budget, so it is retried with the reason in hand, and bounded.
                task["status"] = verdict.send_back_unfinished(
                    task,
                    reason=(
                        f"the previous attempt exited {code} without a usable "
                        "verdict"
                    ),
                    notes=run.get("verdict_error", ""),
                    max_rework=(
                        DEFAULT_MAX_REWORK
                        if run.get("max_rework") is None
                        else int(run["max_rework"])
                    ),
                )
            task["updated_at"] = utcnow()
        # Recorded on this path too, not only after a verdict is applied. The first
        # question about a run that judged nothing is where the task ended up, and
        # the answer is no longer the same for every such run: a lost review holds
        # at `awaiting-review` while a lost implementation returns to the queue.
        # Left unset, anything reporting this had to guess, and the dashboard
        # guessed one of them for both.
        run["resulting_status"] = task["status"]
        # On the run as well as the task. `dispatch` explains this at the time,
        # but a run read later is the confusing case: exit 0, status completed,
        # and nothing moved. Without this the record cannot answer why.
        #
        # A distinct field, not `verdict_error`: that one means "a verdict was
        # written and rejected", and the CLI's own reporting keys off it. Here
        # nothing was written at all, which is a different failure with a
        # different remedy — so only set it when there is no error to show.
        if not run.get("verdict_error"):
            run["no_verdict"] = reason
        add_evidence(
            task,
            f"run {run_id} exited {code} without a usable verdict"
            + (" and without any output" if silent else "")
            + (f" ({note})" if note else "")
            + (
                f"; {failure.described} — {run['task']} returned to "
                f"{task['status']} without being judged"
                if failure is not None
                else ""
            ),
            actor="writ",
        )
        refresh_milestones(data)


def _started_epoch(run: dict[str, Any]) -> float:
    """When this run began, as a unix timestamp, for bounding a file search.

    Falls back to the creation time, and then to 0.0 — a run with no recorded
    time at all should search everything rather than nothing, since the point is
    to find a report that exists.
    """
    for key in ("started_at", "created_at"):
        stamp = run.get(key)
        if not stamp:
            continue
        try:
            return datetime.fromisoformat(stamp).timestamp()
        except ValueError:  # pragma: no cover - stored by utcnow(), always valid
            continue
    return 0.0


def _actor(run: dict[str, Any]) -> str:
    """A short name for who produced a verdict, for the evidence log."""
    role = run.get("role", "agent")
    command = run.get("command") or []
    name = Path(command[0]).name if command else "agent"
    if run.get("model"):
        name = f"{name}:{run['model']}"
    return f"{role}({name})"


@dataclass
class RunReport:
    """What a finished run has to say for itself, for the CLI to relay.

    A record rather than a tuple of four optional strings: these are independent
    things that can each be absent, and positional unpacking of them was already
    at the point where adding the next one would be a silent breakage.
    """

    #: the task status a verdict produced, or None when no verdict was applied.
    #: Deliberately not "where the task ended up": the run records that for every
    #: run, including the ones that judged nothing, and the CLI needs to tell a
    #: status that was *decided* from one the task merely fell back to.
    status: str | None = None
    #: a verdict that was written and rejected
    error: str | None = None
    #: a headline claim writ lowered to match its own criteria
    downgraded: str | None = None
    #: where a verdict was found, when not the path the agent was given
    misplaced: str | None = None


def verdict_summary(root: Path, run_id: str) -> RunReport:
    """Everything a finished run recorded about its own verdict."""
    data = state.load(root)
    run = data["runs"].get(run_id) or {}
    # `resulting_status` is now set on the no-verdict path too, so that a run can
    # say where the task went. Reading it as "a verdict produced this" would have
    # the CLI announce a judgement for a run that made none.
    judged = None if run.get("no_verdict") else run.get("resulting_status")
    return RunReport(
        status=judged,
        error=run.get("verdict_error"),
        downgraded=run.get("verdict_downgraded"),
        misplaced=run.get("verdict_misplaced"),
    )


def detach(root: Path, run_id: str) -> int:
    """Spawn a supervisor process that owns the run after we exit."""
    log = state.run_dir(root, run_id) / "supervisor.log"
    with log.open("w", encoding="utf-8") as handle:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "writ",
                "--root",
                str(root),
                "supervise",
                run_id,
            ],
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    identity = procs.identify(process.pid)
    with state.transaction(root) as data:
        data["runs"][run_id]["supervisor"] = identity.to_dict()
        data["runs"][run_id]["supervisor_pid"] = process.pid
        data["runs"][run_id]["detached"] = True
    return process.pid


def _terminate(record: Any) -> None:
    """Signal a process group, but only one this run can prove is its own.

    `killpg` is the one thing writ does that it cannot take back, and a bare pid
    is not enough to aim it: a run recorded days ago may name a number the kernel
    has since given to something else, and the blast radius is a process *group*.
    `procs.safe_to_signal` returns None unless the recorded identity still
    matches, and None means this does nothing at all.
    """
    pid = procs.safe_to_signal(record)
    if pid is None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.2)
        if not process_alive(pid):
            return


def process_alive(pid: int | None) -> bool:
    """Whether anything is running at this pid.

    The cheap liveness probe, kept for what it is good for: telling a reader
    whether a run's process is still there. It cannot tell a recycled pid from
    the original, so nothing that *acts* on the answer — reaping, cancelling,
    refusing a second claim — uses it. Those go through `procs`, which compares
    the whole recorded identity.
    """
    return procs.running(pid)


def cancel(root: Path, run_id: str) -> None:
    """Stop a running agent and mark the run cancelled.

    Marks the run cancelled *before* killing the process, because the thread
    inside `execute` will race to record an outcome the moment the process dies.
    `_finish` declines to touch a run already marked cancelled, so claiming it
    first is what makes a deliberate stop distinguishable from a failure.
    """
    with state.transaction(root) as data:
        run = data["runs"].get(run_id)
        if run is None:
            raise WritError(f"unknown run: {run_id}")
        if run["status"] not in ACTIVE_RUN_STATUSES:
            raise WritError(f"run {run_id} is not active (status: {run['status']})")
        pid = run.get("identity") or run.get("pid")
        supervisor = run.get("supervisor") or run.get("supervisor_pid")
        run["status"] = "cancelled"
        run["finished_at"] = utcnow()
        task = data["tasks"].get(run["task"])
        if task is not None and task["status"] in INTERRUPTED_STATUS:
            task["status"] = INTERRUPTED_STATUS[task["status"]]
            task["updated_at"] = utcnow()
            add_evidence(
                task,
                f"run {run_id} cancelled; returned to {task['status']}",
                actor="writ",
            )
        refresh_milestones(data)
    for candidate in (supervisor, pid):
        if candidate:
            _terminate(candidate)


#: where a task goes when the process working on it dies. An interrupted
#: implementation returns to the queue; an interrupted review returns to the
#: queue of things awaiting review, because the work itself still stands and only
#: the judgement was lost.
INTERRUPTED_STATUS = {"running": "planned", "reviewing": "awaiting-review"}


def reap(root: Path) -> list[str]:
    """Reconcile runs whose owning process died without recording an outcome.

    This is what makes a killed session resumable, so it has to cover both roles.
    A review interrupted halfway would otherwise leave its task in `reviewing`
    forever: not running, not awaiting review, and invisible to every queue.
    """
    reaped: list[str] = []
    # The other half of recovering from a crash. A process that died mid-write
    # left a `state.json.tmp.*` behind, and this is the one place writ already
    # runs to clean up after a death, so it is where the sweep belongs.
    state.sweep_temporaries(root)
    with state.transaction(root) as data:
        for run_id, run in data["runs"].items():
            if run["status"] not in ACTIVE_RUN_STATUSES:
                continue
            if not run_abandoned(run):
                continue
            run["status"] = "interrupted"
            run["finished_at"] = utcnow()
            task = data["tasks"].get(run["task"])
            if task is not None and task["status"] in INTERRUPTED_STATUS:
                task["status"] = INTERRUPTED_STATUS[task["status"]]
                task["updated_at"] = utcnow()
                add_evidence(
                    task,
                    f"run {run_id} was interrupted; returned to {task['status']}",
                    actor="writ",
                )
            reaped.append(run_id)
        refresh_milestones(data)
    return reaped


@dataclass
class Reconciliation:
    """What settling a stranded run came to.

    `attempt` is the count of infrastructure retries this task has now had, which
    the scheduler reads to decide whether another is allowed. It is derived from
    the durable record rather than from a counter in the scheduler's memory, so a
    resumed session does not hand a task a fresh budget it already spent.
    """

    run_id: str
    #: the status the run was left in
    status: str
    #: where the task ended up
    task_status: str | None = None
    #: whether this call was the one that settled it, as opposed to finding it
    #: already settled by the worker it raced
    settled: bool = False
    attempt: int = 0


def reconcile(
    root: Path, run_id: str, failure: failures.Failure
) -> Reconciliation:
    """Settle a run whose worker did not get to settle it itself.

    `prepare` claims a task by marking it `running` or `reviewing`. Everything
    after that assumed the worker would reach `_finish` and record an outcome, so
    an exception anywhere in between — a provider crash, a subprocess that could
    not spawn, a state lock that timed out — left the task claimed by a process
    that no longer exists. The run read as active, the pid was gone, and the task
    was invisible to every queue until something thought to reap it.

    This is the `finally` half of that: for every prepared run, exactly one of the
    worker's own recording or this reconciliation happens.

    Idempotent on purpose. A worker can raise *after* `_finish` committed — the
    exception may come from the code that reads the result back — and in that case
    the run is already settled and its recorded outcome is the true one. Then this
    only attaches the classification, because how the worker died is still worth
    knowing even when what it did is already written down.
    """
    with state.transaction(root) as data:
        run = data["runs"].get(run_id)
        if run is None:  # pragma: no cover - defensive
            raise WritError(f"unknown run: {run_id}")
        task = data["tasks"].get(run["task"])
        run["failure"] = failure.to_dict()
        settled = run["status"] in ACTIVE_RUN_STATUSES
        if settled:
            # `interrupted` for something that may yet succeed, `failed` for
            # something that will not. The distinction is the same one `reap`
            # draws, and it is what stops a transient provider timeout from
            # reading, forever after, as a run that failed on its merits.
            run["status"] = "interrupted" if failure.retryable else "failed"
            run["finished_at"] = utcnow()
            run.setdefault("note", failure.described)
        if task is not None:
            if settled and task["status"] in INTERRUPTED_STATUS:
                task["status"] = INTERRUPTED_STATUS[task["status"]]
                task["updated_at"] = utcnow()
            if settled:
                add_evidence(
                    task,
                    f"run {run_id} did not finish: {failure.described}"
                    + (
                        f"; {run['task']} returned to {task['status']}"
                        if task["status"] in INTERRUPTED_STATUS.values()
                        else ""
                    ),
                    actor="writ",
                )
        if settled and run.get("role") == "repair":
            # A repair planner does not hold its gate, so there is no task status
            # to restore — but `prepare` moved the request to `planning`, and a
            # request stuck there is one the scheduler will not pick up again.
            # Same treatment a refused patch gets: back to `open`.
            request = repair.request_for_gate(data, run["task"])
            if request is not None and request.get("status") == "planning":
                request["status"] = "open"
        attempt = (
            _note_infrastructure_failure(data, run, failure)
            if failure.retryable and task is not None
            else infrastructure_attempts(task, role=run.get("role", "agent"))
        )
        refresh_milestones(data)
        return Reconciliation(
            run_id=run_id,
            status=run["status"],
            task_status=None if task is None else task["status"],
            settled=settled,
            attempt=attempt,
        )


def _note_infrastructure_failure(
    data: dict[str, Any],
    run: dict[str, Any],
    failure: failures.Failure,
) -> int:
    """Record one infrastructure retry against the task, durably.

    On the task and not in the scheduler, because the budget has to survive the
    scheduler. A session killed mid-backoff and resumed would otherwise give the
    task a fresh set of retries, and a genuinely broken provider would be retried
    without bound across enough restarts.

    Deliberately *not* `open_rework`. The rework budget is for work a reviewer
    read and rejected; nothing here read the work at all.
    """
    task = data["tasks"][run["task"]]
    record = task.setdefault(
        "infrastructure", {"attempts": [], "exhausted": False}
    )
    attempts = record.setdefault("attempts", [])
    role = run.get("role", "agent")
    round_ = _round(task, role)
    attempt = _infra_attempts(task, role=role, round_=round_) + 1
    # A new failure after the budget was spent in an earlier round starts that
    # round's count afresh, so the flag describes the round it was set in.
    record["exhausted"] = False
    attempts.append(
        {
            "attempt": attempt,
            "at": utcnow(),
            "run": run["id"],
            "role": role,
            "round": round_,
            "category": failure.category,
            "reason": failure.reason,
            # The logical attempt this run was, so two runs that are the same
            # attempt retried can be told from two genuine attempts.
            "key": failures.idempotency_key(run["task"], role, round_, attempt),
        }
    )
    return attempt


def _round(task: dict[str, Any], role: str) -> int:
    """Which attempt at the work an infrastructure failure happened in.

    The same number the scheduler keys its jobs on: rework rounds for a task,
    repair rounds for a gate.
    """
    from . import gates

    return gates.rounds(task) if role == "gate" else rework_attempts(task)


def _infra_attempts(
    task: dict[str, Any] | None,
    *,
    role: str | None = None,
    round_: int | None = None,
) -> int:
    """Infrastructure failures recorded on this task, optionally for one job.

    Scoped to a role and round when given. The budget used to be the task's
    whole lifetime, shared across roles: two provider timeouts during the first
    implementation left the reviewer, and every later rework round, with no
    retries at all — so one bad afternoon at the provider could strand a task
    that went on to do everything right. An attempt recorded before rounds were
    stored counts against round 0, which is the round it will have been.
    """
    if task is None:
        return 0
    record = task.get("infrastructure") or {}
    return sum(
        1
        for entry in record.get("attempts") or []
        if (role is None or entry.get("role", "agent") == role)
        and (round_ is None or int(entry.get("round", 0)) == round_)
    )


def infrastructure_attempts(
    task: dict[str, Any] | None, *, role: str | None = None
) -> int:
    """Infrastructure retries spent, for one role's current round when given."""
    if task is None or role is None:
        return _infra_attempts(task)
    return _infra_attempts(task, role=role, round_=_round(task, role))


def mark_infrastructure_exhausted(root: Path, task_id: str, reason: str) -> None:
    """Record that a task has run out of infrastructure retries.

    Its own field, and its own sentence in the evidence log. A task that stopped
    because the machinery around it kept failing must not be readable as a task
    whose implementation was rejected — that is the signal corruption this whole
    classification exists to prevent.
    """
    with state.transaction(root) as data:
        task = data["tasks"].get(task_id)
        if task is None:  # pragma: no cover - defensive
            return
        record = task.setdefault("infrastructure", {"attempts": []})
        record["exhausted"] = True
        record["exhausted_at"] = utcnow()
        record["reason"] = reason
        add_evidence(
            task,
            f"out of infrastructure retries: {reason}. The task was not rejected "
            "on technical merit — no reviewer judged this work.",
            actor="writ",
        )
        refresh_milestones(data)


def log_path(root: Path, run_id: str, stream: str) -> Path:
    data = state.load(root)
    run = data["runs"].get(run_id)
    if run is None:
        raise WritError(f"unknown run: {run_id}")
    return Path(run["dir"]) / f"{stream}.log"


def resolve_run(data: dict[str, Any], run_id: str) -> dict[str, Any]:
    run = data["runs"].get(run_id)
    if run is None:
        raise WritError(f"unknown run: {run_id}")
    return run


def latest_run_for(data: dict[str, Any], task_id: str) -> str | None:
    runs = data["tasks"].get(task_id, {}).get("runs", [])
    return runs[-1] if runs else None
