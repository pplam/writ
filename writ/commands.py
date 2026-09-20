"""Command implementations. Each takes parsed args and prints a result."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import (
    agents,
    config,
    critics,
    decisions,
    gates,
    orchestrator,
    plancheck,
    planner,
    planning,
    plans,
    render,
    repair,
    runner,
    server,
    state,
    verdict,
)
from .model import (
    acceptance_summary,
    add_evidence,
    add_milestone,
    add_task,
    check_dag,
    blocked_on,
    blocking_dependencies,
    effective_status,
    find,
    get_task,
    milestone_tasks,
    ready_tasks,
    refresh_milestones,
    reviewable_tasks,
    set_acceptance,
    set_status,
    SETTABLE_STATUSES,
)
from .state import WritError


# --------------------------------------------------------------------------
# project setup


def cmd_init(args) -> None:
    location = state.initialize(args.root, force=args.force)
    print(f"initialized Writ project at {location}")
    # Written here rather than in `state.initialize` because it is not state: it
    # is a file the project owns, and the store can be reset without it.
    path, created = config.ensure(args.root)
    if created:
        print(f"wrote {path.name} — writ's defaults, with a note explaining them")
    else:
        print(f"kept your existing {path.name}")
    print("next: writ plan <design.md>")


def cmd_plan(args) -> int:
    """Turn a design document into a task DAG.

    By default a coding agent does the planning: it reads the document and the
    repository and returns a plan as JSON, which Writ validates before any of it
    reaches project state. `--extract` uses the deterministic heading parser
    instead, and `--from-plan` re-imports a plan artifact without paying for
    another agent run.
    """
    root = Path(args.root)
    doc = Path(args.design).expanduser()
    if not doc.exists():
        raise WritError(f"design document not found: {doc}")

    plan_path: Path | None = None
    if args.from_plan:
        plan_path = Path(args.from_plan).expanduser()
        document = planning.read_document(plan_path)
        source = f"plan {plan_path.name}"
    elif args.extract:
        document = planning.PlanDocument(
            milestones=planner.parse(
                doc.read_text(encoding="utf-8"),
                milestone_level=args.level,
                split_subsections=not args.flat,
            )
        )
        source = f"{doc.name} (extracted)"
    else:
        data = state.load(root)
        # check the overwrite gate before paying for an agent run, not after
        if data["tasks"] and not (args.append or args.force or args.dry_run):
            raise WritError(
                "this project already has tasks; use --append to add, "
                "or --force to replace the plan"
            )
        context = planning.plan_context(data)
        if args.dry_run:
            print(
                planning.build_prompt(
                    root=root.resolve(),
                    doc=doc,
                    plan_path=state.plan_dir(root, "<plan-id>") / "plan.json",
                    instructions=args.instructions,
                    context=context,
                )
            )
            return 0
        print(f"planning {doc.name} with {args.agent}...")

        def announce(resolved: agents.ResolvedAgent, directory: Path) -> None:
            print(f"  running: {resolved.display}")
            if resolved.warning:
                print(f"  warning: {resolved.warning}", file=sys.stderr)
            print(f"  transcript: {directory}")
            if not args.quiet:
                print("  " + "─" * 60)
            sys.stdout.flush()

        document, plan_path, code = planning.generate(
            root=root,
            doc=doc,
            agent=args.agent,
            agent_args=list(getattr(args, "agent_args", []) or []),
            model=args.model,
            timeout=args.timeout,
            cwd=args.cwd,
            instructions=args.instructions,
            context=context,
            stream=not args.quiet,
            on_start=announce,
        )
        if not args.quiet:
            print("  " + "─" * 60)
        print(f"planning agent exited {code}; plan: {plan_path}")
        source = f"{doc.name} (agent)"

    milestones = document.milestones
    summary = planner.summarize(milestones)
    missing = planning.unresolved_sections(milestones, doc)

    if args.dry_run:
        _print_plan(document, summary, missing)
        _print_findings(
            plancheck.check(
                plancheck.from_plan(
                    milestones, document.requirements, root=root.resolve()
                )
            ),
            preamble="what Writ would object to:",
        )
        return 0

    created, findings = _commit_plan(
        args,
        root=root,
        doc=doc,
        document=document,
        plan_path=plan_path,
        source=source,
    )
    print(
        f"created {summary['milestones']} milestones and {created} tasks "
        f"from {source}"
    )
    if document.requirements:
        print(
            f"requirement inventory: {len(document.requirements)} entries "
            "(writ coverage)"
        )
    for section in missing:
        print(f"note: no section titled {section!r} in {doc.name}", file=sys.stderr)
    _print_findings(findings)
    if getattr(args, "critics", None) is not None:
        # `--critics` absent is None and runs nothing; `--critics` with no names is
        # `[]` and means all of them. The distinction matters because the flag is
        # opt-in — it spends an agent run per critic — so the empty list is a
        # request, not the absence of one.
        _run_critics(
            args,
            root=root,
            doc=doc,
            chosen=_chosen_critics(args),
            plan_path=plan_path,
        )
    data = state.load(root)
    if plans.runnable(data):
        print("plan approved: no blocking findings")
        print("next: writ run")
    else:
        counts = plancheck.tally(plans.findings(data, open_only=True))
        print(
            f"plan held at {plans.plan_status(data)['status']}: "
            f"{counts['error']} blocking, {counts['warning']} advisory"
        )
        print("next: writ check   (then writ approve, or re-plan)")
    return 0


def _print_plan(
    document: planning.PlanDocument,
    summary: dict[str, Any],
    missing: list[str],
) -> None:
    for requirement in document.requirements:
        flag = requirement.priority
        if requirement.status != "planned":
            flag = f"{flag}, {requirement.status}"
        print(f"{requirement.id} [{flag}] {requirement.text}")
    if document.requirements:
        print()
    for milestone in document.milestones:
        print(milestone.title)
        if milestone.notes:
            print(f"    {milestone.notes}")
        for task in milestone.tasks:
            print(f"  - {task.title}")
            if task.notes:
                print(f"      {task.notes}")
            if task.requirement_ids:
                print(f"      covers: {', '.join(task.requirement_ids)}")
            for item in task.acceptances:
                print(f"      · {item}")
            if task.depends_on:
                print(f"      after: {', '.join(task.depends_on)}")
            if task.allowed:
                print(f"      allowed: {', '.join(task.allowed)}")
            if task.forbidden:
                print(f"      forbidden: {', '.join(task.forbidden)}")
    print(
        f"\nwould create {summary['milestones']} milestones, "
        f"{summary['tasks']} tasks, {summary['acceptances']} acceptance criteria"
    )
    for section in missing:
        print(f"note: design section {section!r} was not found", file=sys.stderr)


def _commit_plan(
    args,
    *,
    root: Path,
    doc: Path,
    document: planning.PlanDocument,
    plan_path: Path | None,
    source: str,
) -> tuple[int, list[plancheck.Finding]]:
    """Write the plan to state, then check it and set the plan's status.

    Committed before it is approved, on purpose. The alternative — hold the plan
    outside the graph until a human signs it off — means the one view that would
    let them judge it (`writ graph`, `writ show`, `writ coverage`) cannot see it
    yet. So the tasks land, the objections land beside them, and what approval
    actually gates is `writ run`.
    """
    milestones = document.milestones
    with state.transaction(root) as data:
        if data["tasks"] and not (args.append or args.force):
            raise WritError(
                "this project already has tasks; use --append to add, "
                "or --force to replace the plan"
            )
        if args.force:
            data["tasks"] = {}
            data["milestones"] = {}
            data["findings"] = []
        offset = len(data["milestones"])
        doc_path = str(doc.resolve())
        if doc_path not in data["design_docs"]:
            data["design_docs"].append(doc_path)

        plans.set_requirements(data, document.requirements, replace=bool(args.force))
        built = planner.build_ids(milestones, offset)
        translate = planner.ref_map(built)
        previous: str | None = None
        if args.chain:
            existing = sorted(data["tasks"])
            previous = existing[-1] if existing else None
        created = 0
        for milestone_id, milestone, tasks in built:
            add_milestone(
                data,
                milestone_id=milestone_id,
                title=milestone.title,
                design_section=milestone.section,
            )
            for task_id, task in tasks:
                depends = _resolve_depends(
                    task, translate, data, previous, chain=args.chain
                )
                add_task(
                    data,
                    task_id=task_id,
                    title=task.title,
                    milestone=milestone_id,
                    depends_on=depends,
                    acceptances=task.acceptances,
                    allowed=task.allowed,
                    forbidden=task.forbidden,
                    design_section=task.section,
                    design_doc=doc_path,
                    requirement_ids=task.requirement_ids,
                    notes=task.notes,
                )
                if task.notes:
                    add_evidence(data["tasks"][task_id], f"plan: {task.notes}")
                created += 1
                previous = task_id
        check_dag(data)
        refresh_milestones(data)
        if args.gates:
            gates.install(data, milestones=[m_id for m_id, _, _ in built])
        data.setdefault("plans", []).append(
            {
                "source": source,
                "design_doc": doc_path,
                "artifact": str(plan_path) if plan_path else None,
                "milestones": [milestone_id for milestone_id, _, _ in built],
                "requirements": [req.id for req in document.requirements],
                "created_at": state.utcnow(),
            }
        )
        plans.bump(data)
        plans.set_status(data, "draft")
        findings = plans.run_check(data, root=root.resolve())
    return created, findings


def _resolve_depends(
    task: planner.PlannedTask,
    translate: dict[str, str],
    data: dict[str, Any],
    previous: str | None,
    *,
    chain: bool,
) -> list[str]:
    """Map a planned task's stated dependencies onto real task ids.

    A generated plan refers to tasks by the ids it invented, and may also refer
    to tasks that already exist. Anything we cannot resolve is an error, not a
    silently dropped edge.

    An omitted dependency stays omitted. Writ used to chain a task with no stated
    `depends_on` onto whatever task came before it in the plan, which looks
    conservative and is not: it manufactures an ordering the plan never claimed,
    so a plan that forgot "C needs A" still runs — C after some unrelated B — and
    the omission surfaces as a task failing for no visible reason rather than as a
    plan that is wrong. Independent by default means a missing edge shows up as
    what it is. `--chain` restores the old behaviour for a plan that really is
    meant to be a single line of work.
    """
    if not task.depends_on:
        return [previous] if chain and previous else []
    resolved: list[str] = []
    for ref in task.depends_on:
        target = translate.get(ref) or (ref if ref in data["tasks"] else None)
        if target is None:
            raise WritError(
                f"task {task.title!r} depends on {ref!r}, which is neither in "
                "this plan nor an existing task"
            )
        if target not in resolved:
            resolved.append(target)
    return resolved


def _print_findings(
    findings: list[plancheck.Finding],
    *,
    preamble: str = "",
    limit: int = 12,
) -> None:
    """Print findings worst-first, with what would close each one.

    Capped, because a plan with forty warnings is one nobody reads to the end, and
    the ones that matter are at the top. The count says what was withheld.

    The whole list goes to one stream, chosen by whether anything in it blocks.
    Routing each finding by its own severity splits one list across stdout and
    stderr: the reader sees a tally of four blocking findings with three notes
    above it and the blocking ones nowhere, and neither stream reads as a list.
    """
    if not findings:
        return
    counts = plancheck.tally(findings)
    stream = sys.stderr if counts["error"] else sys.stdout
    if preamble:
        print(preamble, file=stream)
    for finding in findings[:limit]:
        print(f"  {finding.line()}", file=stream)
        if finding.suggested_action:
            print(f"      → {finding.suggested_action}", file=stream)
    if len(findings) > limit:
        print(f"  … {len(findings) - limit} more (writ check)", file=stream)
    print(
        f"  {counts['error']} blocking, {counts['warning']} advisory, "
        f"{counts['note']} notes",
        file=stream,
    )


def cmd_check(args) -> int:
    """Re-check the committed plan and report what stands against it."""
    root = Path(args.root)
    with state.transaction(root) as data:
        found = plans.run_check(data, root=root.resolve())
        record = dict(plans.plan_status(data))
        listed = plans.findings(data, open_only=not args.all)
        coverage_rows = plans.coverage(data)
        # Which critics have not read the plan as it now stands. Structural checks
        # re-run here for free; a critic is an agent and does not, so the most this
        # can do is say that what a critic passed is not what is now committed.
        stale = critics.unreviewed(data)
    blocking = plancheck.blocking(listed)
    if args.json:
        render.emit_json(
            {
                "plan": record,
                "findings": [finding.to_dict() for finding in listed],
                "tally": plancheck.tally(listed),
                "coverage": coverage_rows,
                "unreviewed": stale,
            }
        )
        return 1 if blocking else 0
    if args.quiet:
        return 1 if blocking else 0
    print(f"plan {record['status']} at revision {record['revision']}")
    if coverage_rows:
        satisfied = sum(1 for row in coverage_rows if row["state"] == "satisfied")
        uncovered = [row["id"] for row in coverage_rows if row["state"] == "uncovered"]
        print(
            f"requirements: {len(coverage_rows)} total, {satisfied} satisfied"
            + (f", {len(uncovered)} uncovered" if uncovered else "")
        )
    if not listed:
        print("no findings stand against this plan")
        _print_stale(stale)
        if record["status"] not in plans.RUNNABLE_STATUSES:
            print("next: writ approve")
        return 0
    _print_findings(plancheck.sort_findings(listed), limit=40)
    _print_stale(stale)
    if blocking:
        print()
        print(
            "fix the plan and re-plan, answer them one at a time with "
            "`writ set F-NNNN accepted|declined --reason ...`, or accept them all "
            "with `writ approve --force --reason ...`"
        )
        return 1
    if record["status"] not in plans.RUNNABLE_STATUSES:
        print("next: writ approve")
    return 0


def _print_stale(stale: list[str]) -> None:
    """Say which critics have not read the plan as it stands.

    Not a finding. Writ cannot tell whether an unreviewed plan is wrong — that is
    the entire reason it asks agents — so this states the absence of a review rather
    than objecting to the plan, and does not block anything.
    """
    if not stale:
        return
    if len(stale) == len(critics.CRITICS):
        print("no critic has read this plan   (writ critique)")
        return
    print(f"not reviewed at this revision: {', '.join(stale)}   (writ critique)")


def cmd_critique(args) -> int:
    """Have independent critics read the committed plan and report on it.

    Deliberately separate from `writ check`. Check is deterministic and free: it
    runs every time the plan changes. This spends agents, so it is asked for.
    """
    root = Path(args.root)
    data = state.load(root)
    if not data["tasks"]:
        raise WritError("there is no plan to review (run `writ plan` first)")
    chosen = _chosen_critics(args)
    reports = _run_critics(args, root=root, doc=None, chosen=chosen, plan_path=None)
    if args.json:
        render.emit_json(
            {
                "reports": [
                    {
                        "critic": report.critic,
                        "summary": report.summary,
                        "confidence": report.confidence,
                        "error": report.error,
                        "findings": [f.to_dict() for f in report.findings],
                    }
                    for report in reports
                ],
                "plan": dict(plans.plan_status(state.load(root))),
            }
        )
    blocking = sum(report.blocking for report in reports if report.ok)
    return 1 if blocking or any(not report.ok for report in reports) else 0


def _chosen_critics(args) -> list[critics.Critic]:
    """Which critics to run: the ones named, or all of them."""
    named = getattr(args, "critics", None)
    return critics.by_name(named) if named else list(critics.CRITICS)


def _run_critics(args, *, root, doc, chosen, plan_path):
    """Run the critics over the committed plan and merge what they found.

    The plan they read is rebuilt from committed state rather than from the
    planner's artifact, so the critics review the ids and edges that actually
    exist — which are what will be executed, and not always what the plan proposed.
    """
    data = state.load(root)
    plan_text = _plan_json(data)
    found = plans.findings(data, open_only=True)
    directory = state.store_dir(root) / "reviews" / f"r{plans.revision(data)}"

    def announce(critic, resolved) -> None:
        if not args.json:
            print(f"critic {critic.name}: {critic.brief}")
            print(f"  running: {resolved.display}")
            sys.stdout.flush()

    def report_back(report) -> None:
        if args.json:
            return
        if not report.ok:
            print(f"  {report.critic} failed: {report.error}", file=sys.stderr)
            return
        counts = plancheck.tally(report.findings)
        print(
            f"  {counts['error']} blocking, {counts['warning']} advisory"
            + (f", confidence {report.confidence}" if report.confidence else "")
        )
        if report.summary:
            print(f"  {_first_line(report.summary)}")

    reports = critics.review(
        root=root,
        doc=doc,
        plan_text=plan_text,
        directory=directory,
        chosen=chosen,
        agent=getattr(args, "critic_agent", None) or args.agent,
        model=getattr(args, "critic_model", None) or args.model,
        timeout=args.timeout,
        cwd=args.cwd,
        found=found,
        stream=not args.quiet,
        on_start=announce,
        on_finish=report_back,
    )
    with state.transaction(root) as live:
        written = critics.record(live, reports, root=root.resolve())
    if not args.json:
        _print_findings(
            plancheck.sort_findings(written),
            preamble=f"what the critics found ({len(written)} recorded):",
        )
        if not written:
            print("the critics found nothing to report")
    return reports


def _plan_json(data: dict[str, Any]) -> str:
    """The committed plan as the critics read it: ids, edges, fences, bars."""
    return json.dumps(
        {
            "requirements": [
                dict(record) for record in plans.requirements(data).values()
            ],
            "tasks": [
                {
                    "id": task["id"],
                    "title": task.get("title", ""),
                    "kind": task.get("kind", "task"),
                    "milestone": task.get("milestone"),
                    "notes": task.get("notes", ""),
                    "design_section": task.get("design_section"),
                    "requirement_ids": task.get("requirement_ids", []),
                    "depends_on": task.get("depends_on", []),
                    "allowed": task.get("allowed", []),
                    "forbidden": task.get("forbidden", []),
                    "acceptances": [
                        item.get("text", "") for item in task.get("acceptances", [])
                    ],
                }
                for task in sorted(data["tasks"].values(), key=lambda t: t["id"])
            ],
        },
        indent=2,
    )


def cmd_approve(args) -> int:
    """Record human approval, which is what `writ run` actually requires."""
    root = Path(args.root)
    with state.transaction(root) as data:
        if not data["tasks"]:
            raise WritError("there is no plan to approve (run `writ plan` first)")
        result = plans.approve(
            data,
            actor=args.by,
            reason=args.reason or "",
            force=bool(args.force),
        )
        record = dict(result["plan"])
        accepted = result["accepted"]
    print(f"plan {record['status']} at revision {record['revision']}")
    if accepted:
        print(
            f"accepted {len(accepted)} open finding"
            f"{'s' if len(accepted) != 1 else ''} on the record: "
            f"{', '.join(accepted)}"
        )
        print(f"reason: {record['approval_note']}")
    print("next: writ run")
    return 0


def cmd_coverage(args) -> None:
    """Print the requirement coverage matrix."""
    data = state.load(Path(args.root))
    rows = plans.coverage(data)
    if args.requirement:
        rows = [row for row in rows if row["id"] == args.requirement]
        if not rows:
            raise WritError(f"unknown requirement: {args.requirement}")
    if args.uncovered:
        rows = [row for row in rows if row["state"] in ("uncovered", "unevidenced")]
    if args.json:
        render.emit_json(rows)
        return
    if not rows:
        if not plans.requirements(data):
            print(
                "this plan states no requirement inventory, so there is nothing to "
                "trace. A plan from `writ plan` without --extract records one."
            )
            return
        print("nothing matched")
        return
    width = max(len(row["id"]) for row in rows)
    for row in rows:
        marker = _COVERAGE_MARKS.get(row["state"], "?")
        print(
            f"{marker} {row['id']:<{width}} [{row['priority']}/{row['state']}] "
            f"{_first_line(row['text'], 78)}"
        )
        if row["tasks"]:
            done = f"{row['complete']}/{len(row['tasks'])} complete"
            print(f"    tasks: {', '.join(row['tasks'])} ({done})")
        if row["gates"]:
            print(f"    gates: {', '.join(row['gates'])}")
        if row["evidence"]:
            print(f"    evidence: {row['evidence']}")
        if row["reason"]:
            print(f"    reason: {row['reason']}")
        if row["state"] == "uncovered":
            print("    nothing implements this")
    states: dict[str, int] = {}
    for row in rows:
        states[row["state"]] = states.get(row["state"], 0) + 1
    print()
    print(", ".join(f"{count} {state}" for state, count in sorted(states.items())))


#: one character per coverage state, so a long matrix can be skimmed
_COVERAGE_MARKS = {
    "satisfied": "✓",
    "in-progress": "~",
    "planned": "·",
    "at-risk": "!",
    "uncovered": "✗",
    "unevidenced": "✗",
    "out-of-scope": "–",
    "deferred": "–",
}


# --------------------------------------------------------------------------
# listing and inspection


def cmd_list(args) -> None:
    """One listing command for every collection.

    Which noun you want is an argument, not a separate command: the filters and
    the JSON shape are the same idea in each case, and keeping them together
    means `--json` and `--limit` behave identically everywhere.
    """
    data = state.load(args.root)
    handler = {
        "tasks": _list_tasks,
        "milestones": _list_milestones,
        "runs": _list_runs,
        "decisions": _list_decisions,
        "findings": _list_findings,
        "requirements": _list_requirements,
        "gates": _list_gates,
        "repairs": _list_repairs,
    }[args.what]
    headers, rows, payload = handler(data, args)
    if args.limit:
        rows, payload = rows[: args.limit], payload[: args.limit]
    if args.json:
        render.emit_json(payload)
        return
    print(render.table(headers, rows))


def _list_tasks(data, args):
    refresh_milestones(data)
    rows, payload = [], []
    for task_id in sorted(data["tasks"]):
        task = data["tasks"][task_id]
        status = effective_status(data, task)
        if args.status and status != args.status:
            continue
        if args.milestone and task.get("milestone") != args.milestone:
            continue
        if args.ready and status != "ready":
            continue
        if getattr(args, "awaiting_review", False) and status != "awaiting-review":
            continue
        counts = acceptance_summary(task)
        rows.append(
            [
                render.mark(status),
                task_id,
                status,
                f"{counts['passed']}/{counts['total']}",
                ",".join(task.get("depends_on", [])) or "-",
                task["title"],
            ]
        )
        payload.append(
            {
                "id": task_id,
                "status": status,
                # A reader filtering this listing almost always wants one or the
                # other — the work, or the checks over it — and without `kind` the
                # only way to tell them apart is the shape of the id.
                "kind": task.get("kind", "task"),
                "title": task["title"],
                "milestone": task.get("milestone"),
                "depends_on": task.get("depends_on", []),
                "requirement_ids": task.get("requirement_ids", []),
                "acceptances": counts,
            }
        )
    return ["", "ID", "STATUS", "ACC", "DEPS", "TITLE"], rows, payload


def _list_milestones(data, args):
    refresh_milestones(data)
    rows, payload = [], []
    for milestone_id in sorted(data["milestones"]):
        milestone = data["milestones"][milestone_id]
        if args.status and milestone["status"] != args.status:
            continue
        tasks = milestone_tasks(data, milestone_id)
        done = sum(1 for task in tasks if task["status"] == "completed")
        rows.append(
            [
                render.mark(milestone["status"]),
                milestone_id,
                milestone["status"],
                f"{done}/{len(tasks)}",
                milestone["title"],
            ]
        )
        payload.append(
            {
                "id": milestone_id,
                "status": milestone["status"],
                "title": milestone["title"],
                "tasks_total": len(tasks),
                "tasks_completed": done,
            }
        )
    return ["", "ID", "STATUS", "DONE", "TITLE"], rows, payload


def _list_runs(data, args):
    rows, payload = [], []
    for run_id in sorted(data["runs"]):
        run = data["runs"][run_id]
        if args.task and run["task"] != args.task:
            continue
        if args.status and run["status"] != args.status:
            continue
        if args.active and run["status"] not in runner.ACTIVE_RUN_STATUSES:
            continue
        alive = runner.process_alive(run.get("pid"))
        rows.append(
            [
                render.mark(run["status"]),
                run_id,
                run["task"],
                run["status"],
                run.get("exit_code") if run.get("exit_code") is not None else "-",
                "yes" if alive else "no",
                run.get("started_at") or run.get("created_at") or "-",
            ]
        )
        payload.append({**run, "alive": alive})
    return ["", "RUN", "TASK", "STATUS", "EXIT", "ALIVE", "STARTED"], rows, payload


def _list_decisions(data, args):
    items = data["decisions"]
    if args.task:
        items = [item for item in items if args.task in item.get("tasks", [])]
    if args.status:
        items = [item for item in items if item["status"] == args.status]
    if getattr(args, "proposed", False):
        items = [item for item in items if item["status"] == "proposed"]
    rows = [
        [i["id"], i["status"], i.get("proposed_by") or "", i["title"]] for i in items
    ]
    return ["ID", "STATUS", "BY", "TITLE"], rows, list(items)


def _list_findings(data, args):
    """Everything wrong with the plan that anyone has recorded.

    One ledger for writ's own checks and the gates' reports, because the reader's
    question is "what is wrong with this plan", not "which component noticed".
    """
    items = plans.finding_records(data)
    if args.status:
        items = [item for item in items if item.get("disposition") == args.status]
    if getattr(args, "open", False):
        items = [item for item in items if item.get("disposition") == "open"]
    if args.task:
        items = [item for item in items if args.task in (item.get("where") or "")]
    rows = [
        [
            render.mark("failed" if i.get("severity") == "error" else "blocked"),
            i.get("id") or "-",
            i.get("severity", ""),
            i.get("disposition", ""),
            i.get("where") or "-",
            i.get("message", ""),
        ]
        for i in items
    ]
    return ["", "ID", "SEVERITY", "STATE", "WHERE", "FINDING"], rows, list(items)


def _list_requirements(data, args):
    """The inventory, with what covers each entry.

    The coverage column is the point: a requirement with no task against it is the
    hole this whole inventory exists to make visible, and a list that showed only
    the text would hide it behind having been written down.
    """
    rows, payload = [], []
    for entry in plans.coverage(data):
        state_ = entry["state"]
        if getattr(args, "uncovered", False) and state_ != "uncovered":
            continue
        if args.status and state_ != args.status:
            continue
        rows.append(
            [
                _COVERAGE_MARKS.get(state_, "?"),
                entry["id"],
                entry.get("priority", ""),
                state_,
                ",".join(entry.get("tasks", [])) or "-",
                entry.get("text", ""),
            ]
        )
        payload.append(entry)
    return ["", "ID", "PRI", "COVERAGE", "TASKS", "REQUIREMENT"], rows, payload


def _list_gates(data, args):
    """The plan-level checks, and what each has decided so far."""
    items = gates.gates(data)
    rows, payload = [], []
    held = orchestrator.held_gates(data)
    for gate in items:
        status = effective_status(data, gate)
        if args.status and status != args.status:
            continue
        attempts = gates.attempts(gate)
        last = attempts[-1] if attempts else {}
        rows.append(
            [
                render.mark(status),
                gate["id"],
                gate.get("scope") or "-",
                status,
                last.get("decision") or "-",
                str(len(attempts)),
                held.get(gate["id"], "") or gate.get("title", ""),
            ]
        )
        payload.append({**gate, "effective_status": status})
    return ["", "ID", "SCOPE", "STATUS", "LAST", "RUNS", "NOTE"], rows, payload


def _list_repairs(data, args):
    """Every time a gate has asked for the plan to change, and what came of it."""
    items = repair.requests(data)
    if args.status:
        items = [item for item in items if item.get("status") == args.status]
    if args.task:
        items = [item for item in items if item.get("gate") == args.task]
    rows = [
        [
            i.get("id", ""),
            i.get("gate", ""),
            i.get("status", ""),
            str(i.get("round", 1)),
            str(len(i.get("refusals") or [])),
            ",".join(i.get("findings") or []) or "-",
            _first_line(i.get("summary", ""), 48),
        ]
        for i in items
    ]
    return (
        ["ID", "GATE", "STATUS", "ROUND", "REFUSED", "FINDINGS", "SUMMARY"],
        rows,
        list(items),
    )


def cmd_show(args) -> None:
    """Show any one thing, whatever kind of id it is.

    Ids carry their own type (`M01`, `M01-001`, a run stamp, `D-0001`), so
    asking the user to also name the type would be redundant.
    """
    data = state.load(args.root)
    refresh_milestones(data)
    kind, item = find(data, args.id)
    if kind == "run" and getattr(args, "prompt", False):
        print(_run_prompt(item), end="")
        return
    if args.json:
        if kind == "milestone":
            item = dict(item)
            item["task_details"] = sorted(
                milestone_tasks(data, args.id), key=lambda t: t["id"]
            )
        elif kind == "run":
            item = {**item, "alive": runner.process_alive(item.get("pid"))}
        render.emit_json(item)
        return
    renderer = {
        "task": lambda: _render_task(data, item),
        "milestone": lambda: _render_milestone(
            data, item, verbose=getattr(args, "verbose", False)
        ),
        "run": lambda: _render_run(item),
        "decision": lambda: _render_decision(item),
        "finding": lambda: _render_finding(data, item),
        "repair": lambda: _render_repair(data, item),
    }[kind]
    print(renderer())


def _render_finding(data: dict[str, Any], record: dict[str, Any]) -> str:
    """One finding: what was said, who said it, and what became of it."""
    disposition = record.get("disposition", "open")
    lines = [f"{record['id']} — {record.get('category', 'unspecified')}"]
    lines.append(f"severity: {record.get('severity', 'warning')}")
    lines.append(f"disposition: {disposition}")
    lines.append(f"raised by: {record.get('source', 'writ')}")
    if record.get("where"):
        lines.append(f"about: {record['where']}")
    if record.get("requirement_ids"):
        lines.append(f"requirements: {', '.join(record['requirement_ids'])}")
    if record.get("first_seen_at"):
        lines.append(
            f"first seen: {record['first_seen_at']} "
            f"(revision {record.get('revision', '?')})"
        )
    if int(record.get("seen_count", 1)) > 1:
        # A finding re-raised by later checks is one the plan keeps reproducing,
        # which is worth more than the fact that it exists.
        lines.append(
            f"raised again since: {record['seen_count']} checks, last "
            f"{record.get('seen_at', '')}"
        )
    lines.append(f"\n{record.get('message', '')}")
    if record.get("suggested_action"):
        lines.append(f"\nsuggested:\n{record['suggested_action']}")
    if disposition != "open":
        # How it was answered is the point of reading a closed finding. A plan that
        # ran with a known objection is legible only if the reason it was overruled
        # is here, next to the objection, rather than in an approval note somewhere.
        who = record.get("disposed_by", "?")
        when = record.get("disposed_at", "")
        lines.append(f"\n{disposition} by {who}{f' ({when})' if when else ''}")
        if record.get("reason"):
            lines.append(f"reason: {record['reason']}")
        if record.get("change"):
            lines.append(f"change: {record['change']}")
    asked = [
        request
        for request in repair.requests(data)
        if record["id"] in (request.get("findings") or [])
    ]
    if asked:
        listed = ", ".join(request["id"] for request in asked)
        lines.append(f"\nrepair requested: {listed}   (writ show <id>)")
    if disposition == "open" and record.get("severity") == "error":
        # An open blocking finding is the reason a plan will not run, so the reader
        # is here to decide what to do about it, not only to read it.
        lines.append(
            f"\nblocking. answer it, with a reason either way:"
            f"\n  writ set {record['id']} accepted --reason ...   # stands, run anyway"
            f"\n  writ set {record['id']} declined --reason ...   # the reviewer is wrong"
        )
    return "\n".join(lines)


def _render_repair(data: dict[str, Any], record: dict[str, Any]) -> str:
    """One repair request: what a gate asked for, and every answer it got.

    The refusals are the substance when a gate is held as `repair-refused` — they
    are why writ would not apply what the planner proposed, and a reader sent here
    by that hold is here for exactly that.
    """
    lines = [f"{record['id']} — repair requested by {record.get('gate', '?')}"]
    lines.append(f"status: {record.get('status', 'open')}")
    lines.append(f"round: {record.get('round', 1)}")
    lines.append(
        f"opened: {record.get('opened_at', '')} by {record.get('opened_by', '?')}"
    )
    lines.append(f"plan revision when opened: {record.get('base_revision', '?')}")
    if record.get("closed_at"):
        lines.append(f"closed: {record['closed_at']}")
    if record.get("findings"):
        lines.append(f"findings to close: {', '.join(record['findings'])}")
    if record.get("summary"):
        lines.append(f"\nwhat the gate asked for:\n{record['summary']}")
    if record.get("note"):
        lines.append(f"\nnote:\n{record['note']}")
    for number, refusal in enumerate(record.get("refusals") or [], start=1):
        lines.append(
            f"\nrefused patch {number} ({refusal.get('at', '')}, "
            f"run {refusal.get('run', '?')}):"
        )
        for reason in refusal.get("reasons") or []:
            lines.append(f"  · {reason.get('message', '')}")
    for attempt in record.get("attempts") or []:
        lines.append(
            f"\napplied {attempt.get('at', '')}: "
            f"{_first_line(str(attempt.get('summary', '')), 64)}"
        )
    gate = data["tasks"].get(record.get("gate", ""))
    if gate is not None and gate.get("held"):
        held = gate["held"]
        lines.append(
            f"\n{gate['id']} is held ({held.get('reason', '')}) and will not "
            f"re-run until a human moves it."
        )
    return "\n".join(lines)


def _run_prompt(run: dict[str, Any]) -> str:
    path = Path(run["dir"]) / "prompt.txt"
    if not path.exists():
        raise WritError(f"no prompt recorded for run {run['id']}")
    return path.read_text(encoding="utf-8")


def _render_run(run: dict[str, Any]) -> str:
    lines = [f"{run['id']}  ({run['task']})"]
    lines.append(f"status: {run['status']}  exit: {run.get('exit_code')}")
    lines.append(f"command: {shlex.join(run['command'])}")
    if run.get("model"):
        lines.append(f"model: {run['model']}")
    lines.append(f"cwd: {run.get('cwd')}")
    lines.append(f"timeout: {run.get('timeout') or '-'}")
    lines.append(
        f"pid: {run.get('pid') or '-'}  "
        f"alive: {'yes' if runner.process_alive(run.get('pid')) else 'no'}"
    )
    if run.get("supervisor_pid"):
        lines.append(f"supervisor pid: {run['supervisor_pid']}")
    lines.append(f"created: {run.get('created_at')}")
    lines.append(f"started: {run.get('started_at') or '-'}")
    lines.append(f"finished: {run.get('finished_at') or '-'}")
    if run.get("note"):
        lines.append(f"note: {run['note']}")
    lines.append(f"dir: {run.get('dir')}")
    lines.append(f"\noutput: writ logs {run['id']}")
    lines.append(f"prompt: writ show {run['id']} --prompt")
    return "\n".join(lines)


def _render_decision(record: dict[str, Any]) -> str:
    lines = [f"{record['id']} — {record['title']}"]
    lines.append(f"date: {record['date']}")
    lines.append(f"status: {record['status']}")
    if record.get("proposed_by"):
        lines.append(f"proposed by: {record['proposed_by']}")
    if record.get("confirmed_by") and record["status"] != "proposed":
        lines.append(f"ruled by: {record['confirmed_by']} ({record['confirmed_at']})")
    if record.get("supersedes"):
        lines.append(f"supersedes: {record['supersedes']}")
    if record.get("superseded_by"):
        lines.append(f"superseded by: {record['superseded_by']}")
    if record.get("tasks"):
        lines.append(f"tasks: {', '.join(record['tasks'])}")
    if record.get("context"):
        lines.append(f"\ncontext:\n{record['context']}")
    lines.append(f"\ndecision:\n{record['decision']}")
    if record.get("consequences"):
        lines.append(f"\nconsequences:\n{record['consequences']}")
    if record.get("rejected_reason"):
        lines.append(f"\nrejected:\n{record['rejected_reason']}")
    if record["status"] == "proposed":
        lines.append(
            f"\nproposed by an agent and not yet confirmed."
            f"\n  writ set {record['id']} active"
            f"\n  writ set {record['id']} rejected --reason ..."
        )
    return "\n".join(lines)


def _render_milestone(
    data: dict[str, Any], milestone: dict[str, Any], *, verbose: bool = False
) -> str:
    """A milestone, its rollup, and its member tasks.

    With `verbose`, every member task is expanded in full, so one command can
    answer "what does this milestone actually commit me to".
    """
    tasks = sorted(milestone_tasks(data, milestone["id"]), key=lambda t: t["id"])
    done = sum(1 for task in tasks if task["status"] == "completed")
    lines = [f"{milestone['id']}  {milestone['title']}"]
    lines.append(f"status: {milestone['status']}")
    lines.append(f"tasks: {done}/{len(tasks)}  {render.bar(done, len(tasks))}")
    lines.append(f"design section: {milestone.get('design_section') or '-'}")
    criteria = sum(len(task.get("acceptances", [])) for task in tasks)
    passed = sum(
        1
        for task in tasks
        for item in task.get("acceptances", [])
        if item["status"] == "passed"
    )
    lines.append(f"acceptance criteria: {passed}/{criteria} passed")

    if verbose:
        for task in tasks:
            lines.append("")
            lines.append("-" * 60)
            lines.append(_render_task(data, task))
        return "\n".join(lines)

    lines.append("\ntasks:")
    lines.append(
        render.table(
            ["", "ID", "STATUS", "ACC", "DEPS", "TITLE"],
            [
                [
                    render.mark(effective_status(data, task)),
                    task["id"],
                    effective_status(data, task),
                    f"{acceptance_summary(task)['passed']}/"
                    f"{acceptance_summary(task)['total']}",
                    ",".join(task.get("depends_on", [])) or "-",
                    task["title"],
                ]
                for task in tasks
            ],
        )
    )
    lines.append(f"\nfull detail: writ show {milestone['id']} --verbose")
    return "\n".join(lines)


def _render_task(data: dict[str, Any], task: dict[str, Any]) -> str:
    """Everything recorded about one task, including what depends on it."""
    status = effective_status(data, task)
    lines = [f"{task['id']}  {task['title']}"]
    lines.append(f"status: {status}")
    milestone_id = task.get("milestone")
    if milestone_id:
        milestone = data["milestones"].get(milestone_id, {})
        lines.append(f"milestone: {milestone_id} — {milestone.get('title', '')}")
    else:
        lines.append("milestone: -")
    lines.append(f"depends on: {', '.join(task.get('depends_on', [])) or '-'}")
    blockers = blocking_dependencies(data, task)
    if blockers:
        lines.append(f"blocked by: {', '.join(blockers)}")
    # Near the status rather than down in the evidence. A task blocked by its own
    # report has no unsatisfied dependency, so `blocked by:` above says nothing and
    # every dependency reads as met; without this the reason is one history line
    # under the criteria, and nothing clears a block on its own.
    reason = blocked_on(task)
    if reason:
        lines.append(f"blocked on: {reason}")
    dependents = [
        other["id"]
        for other in sorted(data["tasks"].values(), key=lambda t: t["id"])
        if task["id"] in other.get("depends_on", [])
    ]
    if dependents:
        lines.append(f"blocks: {', '.join(dependents)}")
    lines.append(f"design: {task.get('design_doc') or '-'}")
    lines.append(f"section: {task.get('design_section') or '-'}")
    counts = acceptance_summary(task)
    lines.append(
        f"\nacceptance criteria ({counts['passed']}/{counts['total']} passed):"
    )
    for index, acceptance in enumerate(task.get("acceptances", []), start=1):
        lines.extend(render.acceptance_detail(index, acceptance))
    if not task.get("acceptances"):
        lines.append("  (none recorded)")
    last = task.get("last_verdict")
    if last:
        who = last.get("actor") or last.get("role", "agent")
        claim = last.get("decision") or last.get("outcome")
        lines.append(f"\nlast verdict: {claim} by {who} at {last.get('at')}")
        if last.get("summary"):
            lines.append(f"  {last['summary']}")
    record = task.get("rework")
    if record:
        attempt = record.get("attempt", 0)
        head = (
            f"\nrework: attempt {attempt} of "
            f"{record.get('budget', record.get('max'))}, "
            f"rejected by {record.get('reviewer') or '-'} at {record.get('at')}"
        )
        if record.get("resolved_at"):
            head += f" — answered, accepted at {record['resolved_at']}"
        elif record.get("exhausted"):
            head += " — budget spent, left failed"
        lines.append(head)
        for finding in record.get("findings") or []:
            lines.append(
                f"  {finding.get('number')}. {finding.get('status')}: "
                f"{finding.get('evidence', '')}".rstrip()
            )
        if record.get("notes"):
            lines.append(f"  notes: {record['notes']}")
    if status == "awaiting-review":
        lines.append(f"\nawaiting review: writ review {task['id']}")
    if task.get("allowed"):
        lines.append("\nallowed:")
        lines.extend(f"  - {entry}" for entry in task["allowed"])
    if task.get("forbidden"):
        lines.append("\nforbidden:")
        lines.extend(f"  - {entry}" for entry in task["forbidden"])
    if task.get("runs"):
        lines.append("\nruns:")
        for run_id in task["runs"]:
            run = data["runs"].get(run_id, {})
            role = run.get("role", "agent")
            lines.append(
                f"  - {run_id}  [{role}]  {run.get('status')}  "
                f"exit={run.get('exit_code')}"
            )
            if run.get("verdict_error"):
                lines.append(f"      unusable verdict: {run['verdict_error']}")
            elif run.get("no_verdict"):
                lines.append(f"      no verdict: {run['no_verdict']}")
            if run.get("verdict_downgraded"):
                lines.append(f"      downgraded: {run['verdict_downgraded']}")
            if run.get("verdict_misplaced"):
                lines.append(f"      verdict found at: {run['verdict_misplaced']}")
    if task.get("evidence"):
        lines.append("\nevidence:")
        for entry in task["evidence"]:
            actor = entry.get("actor", "operator")
            lines.append(f"  - {entry['at']}  [{actor}] {entry['text']}")
    return "\n".join(lines)


def _status_payload(data: dict[str, Any]) -> dict[str, Any]:
    refresh_milestones(data)
    tasks = data["tasks"]
    counts: dict[str, int] = {}
    for task in tasks.values():
        status = effective_status(data, task)
        counts[status] = counts.get(status, 0) + 1
    active_runs = [
        run
        for run in data["runs"].values()
        if run["status"] in runner.ACTIVE_RUN_STATUSES
    ]
    return {
        "design_docs": data.get("design_docs", []),
        "milestones": len(data["milestones"]),
        "milestones_completed": sum(
            1 for m in data["milestones"].values() if m["status"] == "completed"
        ),
        "tasks": len(tasks),
        "tasks_completed": counts.get("completed", 0),
        "counts": counts,
        "ready": [task["id"] for task in ready_tasks(data)],
        "awaiting_review": [task["id"] for task in reviewable_tasks(data)],
        "running": [
            task_id
            for task_id, task in sorted(tasks.items())
            if task["status"] in ("running", "reviewing")
        ],
        "failed": [
            task_id
            for task_id, task in sorted(tasks.items())
            if task["status"] == "failed"
        ],
        "active_runs": [
            {
                "id": run["id"],
                "task": run["task"],
                "status": run["status"],
                "pid": run.get("pid"),
                "started_at": run.get("started_at"),
                "alive": runner.process_alive(run.get("pid")),
            }
            for run in active_runs
        ],
        "decisions": len(data["decisions"]),
        "proposed_decisions": [
            item["id"] for item in decisions.proposed(data)
        ],
    }


def cmd_status(args) -> None:
    """Progress and live runs, once or repeatedly.

    Following is a mode of looking at status, not a different question, so it is
    a flag rather than a `watch` command.
    """
    if getattr(args, "watch", False):
        _watch_status(args)
        return
    data = state.load(args.root)
    payload = _status_payload(data)
    if args.json:
        render.emit_json(payload)
        return
    print(_render_status(payload))


def _watch_status(args) -> None:  # pragma: no cover - interactive loop
    try:
        while True:
            payload = _status_payload(state.load(args.root))
            if not args.no_clear:
                os.system("clear" if shutil.which("clear") else "")
            print(f"writ status --watch — {state.utcnow()}  (ctrl-c to exit)\n")
            print(_render_status(payload))
            if not payload["active_runs"] and args.until_idle:
                return
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()


def _render_status(payload: dict[str, Any]) -> str:
    lines = []
    lines.append(
        f"tasks {payload['tasks_completed']}/{payload['tasks']}  "
        + render.bar(payload["tasks_completed"], payload["tasks"])
    )
    lines.append(
        f"milestones {payload['milestones_completed']}/{payload['milestones']}  "
        + render.bar(payload["milestones_completed"], payload["milestones"])
    )
    breakdown = "  ".join(
        f"{status}:{count}" for status, count in sorted(payload["counts"].items())
    )
    lines.append(f"by status: {breakdown or '-'}")
    lines.append(f"decisions: {payload['decisions']}")
    if payload["running"]:
        lines.append(f"running: {', '.join(payload['running'])}")
    if payload["failed"]:
        lines.append(f"failed: {', '.join(payload['failed'])}")
    if payload["ready"]:
        lines.append(f"ready to dispatch: {', '.join(payload['ready'][:8])}")
    if payload.get("awaiting_review"):
        listed = ", ".join(payload["awaiting_review"][:8])
        lines.append(f"awaiting review: {listed}   (writ review)")
    if payload.get("proposed_decisions"):
        listed = ", ".join(payload["proposed_decisions"][:8])
        lines.append(f"decisions proposed: {listed}   (writ show <id>)")
    if payload["active_runs"]:
        lines.append("")
        lines.append(
            render.table(
                ["RUN", "TASK", "STATUS", "PID", "ALIVE", "STARTED"],
                [
                    [
                        run["id"],
                        run["task"],
                        run["status"],
                        run["pid"] or "-",
                        "yes" if run["alive"] else "no",
                        run["started_at"] or "-",
                    ]
                    for run in payload["active_runs"]
                ],
            )
        )
    return "\n".join(lines)


def cmd_graph(args) -> None:
    """Draw the dependency DAG.

    The default is a tree following dependencies forwards, because the useful
    questions about a DAG are shape questions: what unlocks next, where the work
    forks, what one task is holding up. A per-task list of dependencies answers
    none of those without the reader assembling the graph in their head.
    """
    data = state.load(args.root)
    check_dag(data)
    tasks = data["tasks"]
    if args.dot:
        _graph_dot(data, tasks)
        return
    if args.json:
        render.emit_json(
            {
                "levels": render.dag_levels(tasks),
                "tasks": {
                    task_id: {
                        "status": effective_status(data, task),
                        "depends_on": task.get("depends_on", []),
                        "blocks": sorted(
                            other
                            for other, item in tasks.items()
                            if task_id in item.get("depends_on", [])
                        ),
                    }
                    for task_id, task in tasks.items()
                },
            }
        )
        return
    if not tasks:
        print("(no tasks)")
        return
    if args.levels:
        print(_graph_levels(data, tasks))
        return
    print(_graph_tree(data, tasks, verbose=args.verbose))


def _graph_label(data, tasks, task_id: str, *, verbose: bool) -> str:
    task = tasks[task_id]
    status = effective_status(data, task)
    label = f"{render.mark(status)} {task_id}"
    if verbose:
        summary = acceptance_summary(task)
        label += f"  {task['title']}"
        label += f"  [{status}"
        if summary["total"]:
            label += f", {summary['passed']}/{summary['total']}"
        label += "]"
    else:
        label += f"  {task['title']}"
    return label


def _graph_tree(data, tasks, *, verbose: bool) -> str:
    lines = render.dag_tree(
        tasks, label=lambda t: _graph_label(data, tasks, t, verbose=verbose)
    )
    orphans = _graph_orphans(data, tasks, verbose=verbose)
    body = "\n".join(lines)
    if orphans:
        body += "\n\n" + "\n".join(orphans)
    return body + "\n\n" + _graph_legend(tasks)


def _graph_orphans(data, tasks, *, verbose: bool) -> list[str]:
    """Tasks the tree cannot reach, which only happens in a broken store.

    `check_dag` rules out cycles, so this should be empty. Printing it anyway
    beats silently dropping a task from a view someone is using to plan.
    """
    drawn = set()
    for line in render.dag_tree(tasks, label=lambda t: t):
        stripped = line.strip().lstrip("├└─│↩ ")
        if stripped:
            drawn.add(stripped.split()[0])
    missing = sorted(set(tasks) - drawn)
    if not missing:
        return []
    return ["unreachable (report this):"] + [
        f"  {_graph_label(data, tasks, t, verbose=verbose)}" for t in missing
    ]


def _graph_legend(tasks) -> str:
    levels = render.dag_levels(tasks)
    widest = max(len(level) for level in levels)
    count = len(tasks)
    line = f"{count} task{'' if count == 1 else 's'}, {len(levels)} deep"
    if widest > 1:
        line += f", up to {widest} in parallel"
    if any(
        len([d for d in task.get("depends_on", []) if d in tasks]) > 1
        for task in tasks.values()
    ):
        line += "   ↩ joins a task drawn under its last dependency"
    return line


def _graph_levels(data, tasks) -> str:
    """The DAG by dependency level: what could run at the same time."""
    lines = []
    for index, level in enumerate(render.dag_levels(tasks), start=1):
        count = len(level)
        lines.append(f"level {index}  ({count} task{'' if count == 1 else 's'})")
        for task_id in level:
            task = tasks[task_id]
            deps = ", ".join(task.get("depends_on", []))
            suffix = f"   after {deps}" if deps else ""
            status = effective_status(data, task)
            lines.append(
                f"  {render.mark(status)} {task_id}  {task['title']}{suffix}"
            )
        lines.append("")
    return "\n".join(lines).rstrip()


#: fill colours by status, for `--dot`. Muted on purpose: the graph is read for
#: its shape, and saturated fills fight the structure for attention.
DOT_FILLS = {
    "completed": "#d8ece0",
    "awaiting-review": "#fdf0cf",
    "reviewing": "#fdf0cf",
    "running": "#d9e7f7",
    "failed": "#f8d9d9",
    "blocked": "#f8d9d9",
    "cancelled": "#eeeeee",
    "ready": "#ffffff",
    "planned": "#f7f7f7",
}


def _graph_dot(data, tasks) -> None:
    """Emit graphviz, carrying the status the terminal view shows.

    A rendered graph is where progress is most legible, so dropping status here
    would make the prettier output the less useful one.
    """
    print("digraph writ {")
    print('  rankdir=LR;')
    print('  graph [fontname="Helvetica", fontsize=11];')
    print(
        '  node [shape=box, style="rounded,filled", fontname="Helvetica", '
        'fontsize=10, color="#999999"];'
    )
    print('  edge [color="#777777", arrowsize=0.7];')
    for milestone_id in sorted(data["milestones"]):
        members = [
            task_id
            for task_id in sorted(tasks)
            if tasks[task_id].get("milestone") == milestone_id
        ]
        if not members:
            continue
        title = _dot_escape(data["milestones"][milestone_id]["title"])
        # An autocreated milestone is titled with its own id; "M02  M02" is noise.
        heading = milestone_id if title == milestone_id else f"{milestone_id}  {title}"
        print(f"  subgraph cluster_{milestone_id.replace('-', '_')} {{")
        print(
            f'    label="{heading}"; style=rounded; '
            'color="#bbbbbb"; fontsize=11;'
        )
        for task_id in members:
            print(f"    {_dot_node(data, tasks[task_id], task_id)}")
        print("  }")
    loose = [t for t in sorted(tasks) if tasks[t].get("milestone") not in data["milestones"]]
    for task_id in loose:
        print(f"  {_dot_node(data, tasks[task_id], task_id)}")
    for task_id in sorted(tasks):
        for dep in tasks[task_id].get("depends_on", []):
            print(f'  "{dep}" -> "{task_id}";')
    print("}")


def _dot_node(data, task, task_id: str) -> str:
    status = effective_status(data, task)
    summary = acceptance_summary(task)
    label = f"{task_id}\\n{_dot_escape(task['title'])}"
    if summary["total"]:
        label += f"\\n{status}  {summary['passed']}/{summary['total']}"
    else:
        label += f"\\n{status}"
    fill = DOT_FILLS.get(status, "#f7f7f7")
    extra = ' penwidth=2 color="#555555"' if status == "ready" else ""
    return f'"{task_id}" [label="{label}", fillcolor="{fill}"{extra}];'


def _dot_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', "'")


# --------------------------------------------------------------------------
# mutation


def cmd_set(args) -> None:
    """Set a status on whatever the id points at.

    Tasks, decisions and findings all have a state a human may legitimately move,
    and which one you meant is already in the id, so one verb covers all three.
    What each of them refuses to accept differs, and says something about where
    authority sits: a task cannot be set `completed` (the reviewer judges that), and
    a finding cannot be set `resolved` (a check or a gate demonstrates that).
    """
    with state.transaction(args.root) as data:
        kind, _ = find(data, args.id)
        if kind == "decision":
            _set_decision(args, data)
            return
        if kind == "finding":
            _set_finding(args, data)
            return
        if kind != "task":
            raise WritError(
                f"{args.id} is a {kind}; only tasks, decisions and findings have "
                "a status you can set"
            )
        if args.status not in SETTABLE_STATUSES:
            raise WritError(
                f"{args.status!r} is a decision status, not a task status "
                f"(tasks accept {', '.join(SETTABLE_STATUSES)})"
            )
        set_status(
            data, args.id, args.status, evidence=args.evidence, force=args.force
        )
    print(f"{args.id} -> {args.status}")


def _set_finding(args, data) -> None:
    """Dispose of one finding, rather than accepting every one of them.

    `writ approve --force` is the wholesale lever: it accepts every open finding
    under a single reason. That is the wrong instrument for disagreeing with one
    finding, because it also silently accepts the ones the reader never looked at.
    Here each finding gets its own answer and its own reason, which is what makes
    the ledger an audit trail instead of a list that was once overruled in bulk.
    """
    if args.status not in plans.SETTABLE_DISPOSITIONS:
        if args.status == "resolved":
            raise WritError(
                f"{args.id} cannot be set resolved by hand: a finding resolves when "
                "a check or a gate demonstrates the outcome it asked for. To let the "
                "plan proceed with this finding standing, accept it."
            )
        raise WritError(
            f"{args.status!r} is not a finding disposition "
            f"(findings accept {', '.join(plans.SETTABLE_DISPOSITIONS)})"
        )
    if not args.reason:
        # Both answers need one. An accepted finding without a reason is the
        # silent ignoring the report is against, and a declined one without a
        # reason is an assertion that the reviewer was wrong, unargued.
        verb = {"accepted": "accept", "declined": "decline"}[args.status]
        raise WritError(
            f"--reason is required to {verb} a finding: say why {args.id} "
            "does not block the plan"
        )
    record = plans.dispose(
        data,
        args.id,
        args.status,
        actor=getattr(args, "by", None) or "operator",
        reason=args.reason,
        change=args.evidence or "",
    )
    print(f"{record['id']} {args.status}: {_first_line(record.get('message', ''), 64)}")
    print(f"reason: {record['reason']}")
    blocking = [
        finding
        for finding in plans.findings(data, open_only=True)
        if finding.severity == "error"
    ]
    if blocking:
        listed = ", ".join(finding.id for finding in blocking)
        print(f"still blocking: {listed}")
    elif not plans.runnable(data):
        # Disposing of the last blocker does not itself approve the plan. The
        # status comes from a check, so point at the one that will grant it rather
        # than leaving the reader to guess why `writ run` still refuses.
        print("no blocking findings left — next: writ check")


def _set_decision(args, data) -> None:
    """Rule on a decision an agent proposed."""
    if args.status not in decisions.SETTABLE_DECISION_STATUSES:
        raise WritError(
            f"{args.status!r} is a task status, not a decision status "
            f"(decisions accept {', '.join(decisions.SETTABLE_DECISION_STATUSES)})"
        )
    if args.status == "rejected":
        if not args.reason:
            raise WritError("rejecting a decision needs --reason")
        record = decisions.reject(data, args.id, reason=args.reason)
        decisions.sync_markdown(args.root, data)
        print(f"{record['id']} rejected: {record['title']}")
        print(f"reason: {record['rejected_reason']}")
        return
    record = decisions.confirm(data, args.id, supersedes=args.supersedes)
    decisions.sync_markdown(args.root, data)
    print(f"{record['id']} active: {record['title']}")
    if record.get("supersedes"):
        print(f"supersedes: {record['supersedes']}")
    print(f"mirror: {state.decisions_file(args.root)}")


def cmd_override(args) -> None:
    """Let a human take a decision the agents own, and say that they did.

    Writ routes judgements through agents, but a tool that cannot be overridden
    is a tool that traps you when a model is wrong or unavailable. The escape
    hatch exists; it just refuses to disguise itself as an agent's verdict.
    """
    with state.transaction(args.root) as data:
        task = get_task(data, args.id)
        for spec in args.accept or []:
            number, _, status = spec.partition("=")
            if not number.strip().isdigit():
                raise WritError(f"--accept expects N or N=STATUS, got {spec!r}")
            set_acceptance(
                data,
                args.id,
                int(number),
                (status or "passed").strip(),
                actor="operator",
                evidence=f"operator override: {args.reason}",
            )
        set_status(
            data,
            args.id,
            args.status,
            evidence=f"operator override to {args.status}: {args.reason}",
            force=True,
            actor="operator",
            allow_judged=True,
        )
        task["last_verdict"] = {
            "role": "operator",
            "actor": "operator",
            "outcome": args.status,
            "decision": None,
            "summary": args.reason,
            "at": state.utcnow(),
        }
    print(f"{args.id} -> {args.status} (operator override)")
    print(f"recorded reason: {args.reason}")


def cmd_task(args) -> None:
    """Create a task, or amend an existing one.

    Creating and amending take the same fields and differ only in whether the id
    already exists, so they are one command. Passing a known id amends it;
    omitting the id creates.
    """
    if args.id:
        _amend_task(args)
        return
    if not args.title:
        raise WritError("creating a task needs --title")
    with state.transaction(args.root) as data:
        milestone = args.milestone
        if milestone and milestone not in data["milestones"]:
            add_milestone(data, milestone_id=milestone, title=milestone)
        task_id = _next_task_id(data, milestone)
        add_task(
            data,
            task_id=task_id,
            title=args.title,
            milestone=milestone,
            depends_on=args.depends or [],
            acceptances=args.acceptance or [],
            allowed=args.allow or [],
            forbidden=args.forbid or [],
        )
        check_dag(data)
        refresh_milestones(data)
    print(f"created {task_id}")


def _amend_task(args) -> None:
    with state.transaction(args.root) as data:
        task = get_task(data, args.id)
        if args.title:
            task["title"] = args.title
        if args.acceptance:
            task["acceptances"].extend(
                {"text": text, "status": "pending"} for text in args.acceptance
            )
        if args.depends:
            for dep in args.depends:
                if dep not in data["tasks"]:
                    raise WritError(f"unknown dependency: {dep}")
            task["depends_on"] = args.depends
        if args.allow:
            task["allowed"] = args.allow
        if args.forbid:
            task["forbidden"] = args.forbid
        if args.milestone:
            if args.milestone not in data["milestones"]:
                raise WritError(f"unknown milestone: {args.milestone}")
            task["milestone"] = args.milestone
        task["updated_at"] = state.utcnow()
        check_dag(data)
        refresh_milestones(data)
    print(f"updated {args.id}")


def _next_task_id(data: dict[str, Any], milestone: str | None) -> str:
    prefix = milestone or "T"
    existing = [key for key in data["tasks"] if key.startswith(f"{prefix}-")]
    return f"{prefix}-{len(existing) + 1:03d}"


def cmd_dispatch(args) -> int:
    extra = list(getattr(args, "agent_args", []) or [])
    if args.dry_run:
        data = state.load(args.root)
        task = get_task(data, args.id)
        print(runner.build_prompt(data, task, Path(args.root)))
        return 0
    return _run_agent_on_task(args, role="agent", task_id=args.id, extra=extra)


def cmd_review(args) -> int:
    """Have an agent that did not write the code decide whether it is done.

    Self-assessment is not evidence, so the implementing agent's verdict only
    reaches `awaiting-review`. This is the step that can complete a task.
    """
    data = state.load(args.root)
    if args.dry_run:
        task = get_task(data, args.id) if args.id else None
        if task is None:
            raise WritError("--dry-run needs a task id")
        print(runner.build_review_prompt(data, task, Path(args.root)))
        return 0

    if args.id:
        targets = [args.id]
    else:
        targets = [task["id"] for task in reviewable_tasks(data)]
        if not targets:
            print("nothing is awaiting review")
            return 0
        print(f"reviewing {len(targets)} task(s): {', '.join(targets)}")

    worst = 0
    for index, task_id in enumerate(targets):
        if index:
            print()
        code = _run_agent_on_task(args, role="reviewer", task_id=task_id, extra=[])
        worst = worst or code
    return worst


def _run_agent_on_task(args, *, role: str, task_id: str, extra: list[str]) -> int:
    """Shared body of dispatch and review: run one agent, report its verdict."""
    root = Path(args.root)
    run_id, directory, _, resolved = runner.prepare(
        root,
        task_id,
        args.agent,
        extra,
        model=args.model,
        timeout=args.timeout,
        cwd=args.cwd,
        force=args.force,
        role=role,
        max_rework=getattr(args, "max_rework", None),
    )
    if resolved.warning:
        print(f"warning: {resolved.warning}", file=sys.stderr)
    verb = "reviewing" if role == "reviewer" else "dispatched"
    if getattr(args, "detach", False):
        pid = runner.detach(root, run_id)
        print(f"{verb} {task_id} as run {run_id} (detached, supervisor pid {pid})")
        print(f"logs: writ logs {run_id} --follow")
        return 0
    print(f"{verb} {task_id} as run {run_id}")
    print(f"running: {resolved.display}")
    print(f"logs: {directory}")
    if not args.quiet:
        print("" + "─" * 62)
        sys.stdout.flush()
    code = runner.execute(root, run_id, stream=not args.quiet, prefix="| ")
    if not args.quiet:
        print("" + "─" * 62)
    print(f"run {run_id} finished with exit code {code}")
    if not runner.produced_output(directory):
        if code == 124:
            print(agents.hang_hint(resolved), file=sys.stderr)
        else:
            # An agent that exits on its own without a word is the case that
            # otherwise reads as "it worked but did not report". Say so here,
            # where the invocation is still on screen.
            print(agents.silent_exit_hint(resolved, code), file=sys.stderr)
    _report_verdict(root, run_id, task_id, directory, role)
    return code


def _report_verdict(
    root: Path, run_id: str, task_id: str, directory: Path, role: str
) -> None:
    """Say what the agent claimed and what writ did about it.

    The status change is the interesting part of a run, so it is reported
    explicitly rather than left for the user to go and look up.
    """
    report = runner.verdict_summary(root, run_id)
    if report.error:
        print(f"warning: {report.error}", file=sys.stderr)
    if report.downgraded:
        print(f"warning: {report.downgraded}", file=sys.stderr)
    if report.misplaced:
        print(
            f"warning: the {role} wrote its verdict to {report.misplaced} rather "
            f"than the path it was given; writ used it from there",
            file=sys.stderr,
        )
    status = report.status
    if status is None:
        print(verdict.missing_message(task_id, directory, role), file=sys.stderr)
        return
    data = state.load(root)
    task = data["tasks"][task_id]
    counts = acceptance_summary(task)
    print(
        f"{task_id} -> {status} "
        f"({counts['passed']}/{counts['total']} criteria passed, "
        f"judged by the {role})"
    )
    record = task.get("rework") or {}
    if status == "awaiting-review":
        print(f"next: writ review {task_id}")
    elif status == "planned" and role == "reviewer" and record.get("attempt"):
        # The case this would otherwise report as a bare `-> planned`, which looks
        # like the run undid itself. It is the rejection being turned into another
        # attempt, and the next command is a dispatch, not an investigation.
        print(
            f"sent back for rework ({record['attempt']} of "
            f"{record.get('budget', record.get('max'))}): "
            "the next agent on this task is given this review"
        )
        print(f"next: writ dispatch {task_id}")
    elif status == "failed":
        if record.get("exhausted"):
            print(
                f"rework budget of {record.get('budget', record.get('max'))} "
                "attempts is spent, so this is left failed for you"
            )
        print(f"next: writ show {task_id}   # see what it could not meet")


def cmd_agents(args) -> None:
    """Show the headless invocation writ will use.

    Agent CLIs open an interactive session by default, which hangs when the
    prompt arrives on a pipe. This is how to check what writ will actually run
    before committing a long planning job to it.
    """
    if args.agent:
        resolved = agents.resolve(args.agent, [], args.model)
        if args.json:
            render.emit_json(
                {
                    "agent": resolved.name,
                    "command": resolved.command,
                    "known": resolved.profile is not None,
                    "warning": resolved.warning,
                }
            )
            return
        print(resolved.display)
        if resolved.profile and resolved.profile.note:
            print(f"note: {resolved.profile.note}")
        if resolved.warning:
            print(f"warning: {resolved.warning}", file=sys.stderr)
        return

    rows = []
    payload = []
    for name in agents.KNOWN_AGENTS:
        resolved = agents.resolve(name, [], None)
        profile = agents.PROFILES[name]
        available = "yes" if shutil.which(name) else "no"
        rows.append(
            [
                name,
                available,
                shlex.join(resolved.command),
                profile.model_flag or "-",
            ]
        )
        payload.append(
            {
                "agent": name,
                "installed": available == "yes",
                "command": resolved.command,
                "model_flag": profile.model_flag,
                "note": profile.note,
            }
        )
    configured = _configured_roles(args.root)
    if args.json:
        render.emit_json({"agents": payload, "roles": configured})
        return
    print(render.table(["AGENT", "FOUND", "HEADLESS INVOCATION", "MODEL FLAG"], rows))
    print("\nany other command is passed through unchanged; add its own")
    print("non-interactive flag to --agent so it does not wait on a terminal")
    _print_roles(configured, args.root)


def _configured_roles(root) -> list[dict[str, Any]]:
    """What this project has settled on for each role, and where that came from.

    Reported even when nothing is configured, because the default worth knowing
    about is the reviewer's: with no config and no `--reviewer`, review runs on the
    implementing agent, and the table saying so is how that stops being a surprise.
    """
    try:
        loaded = config.load(root)
    except WritError as exc:
        # A broken config is worth saying here rather than raising: this command
        # exists to explain what writ will run, and "your config is unreadable" is
        # the most useful thing it can say when that is true.
        return [{"role": "-", "error": str(exc)}]
    out = []
    for role, purpose in config.ROLES.items():
        entry = (loaded.get("agents") or {}).get(role) or {}
        out.append(
            {
                "role": role,
                "purpose": purpose,
                "command": entry.get("command"),
                "model": entry.get("model"),
                "timeout": entry.get("timeout"),
                "source": config.FROM_CONFIG if entry else config.FROM_BUILTIN,
            }
        )
    return out


#: what a role falls back to when the config does not name it
ROLE_FALLBACKS = {
    "planner": "pi",
    "critic": "the planning agent",
    "implementer": "pi",
    "reviewer": "the implementing agent",
}


def _print_roles(configured: list[dict[str, Any]], root) -> None:
    broken = next((row for row in configured if row.get("error")), None)
    if broken:
        print(f"\nwarning: {broken['error']}", file=sys.stderr)
        return
    rows = [
        [
            row["role"],
            row["command"] or f"({ROLE_FALLBACKS[row['role']]})",
            row["model"] or "-",
            str(row["timeout"]) if row["timeout"] else "-",
        ]
        for row in configured
    ]
    print()
    print(render.table(["ROLE", "COMMAND", "MODEL", "TIMEOUT"], rows))
    path = config.config_file(root)
    if any(row["source"] == config.FROM_CONFIG for row in configured):
        print(f"\nfrom {path}; a flag overrides any of it")
    else:
        print(f"\nno {path}; bracketed values are what writ falls back to")
    # Keyed on the reviewer rather than on whether anything is configured. Since
    # `writ init` writes every field, "this project has a config" stopped implying
    # "this project chose a reviewer" — and the unset reviewer is the whole reason
    # this warning exists.
    reviewer = next(row for row in configured if row["role"] == "reviewer")
    if not reviewer["command"]:
        print("\nreviewer is the implementing agent, the weakest of these:")
        print("a model checking its own work agrees with itself more than it should.")


def cmd_supervise(args) -> int:
    """Internal: owns a detached run until the agent exits."""
    return runner.execute(Path(args.root), args.run_id)


def cmd_serve(args) -> None:
    """Serve the read-only dashboard.

    A separate command rather than a flag on `graph`: it is not another rendering
    of the DAG, it is every view writ has — runs, prompts, logs, verdicts,
    decisions — and naming it after one of them would undersell it.
    """
    check_dag(state.load(args.root))  # fail at the prompt, not as a broken page
    server.serve(
        args.root,
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
    )


def cmd_logs(args) -> None:
    root = Path(args.root)
    data = state.load(root)
    run_id = args.id
    if run_id in data["tasks"]:
        latest = runner.latest_run_for(data, run_id)
        if latest is None:
            raise WritError(f"task {run_id} has no runs yet")
        run_id = latest
    stream = "stderr" if args.stderr else "stdout"
    path = runner.log_path(root, run_id, stream)
    if not path.exists():
        raise WritError(f"no {stream} log yet for {run_id}")
    if not args.follow:
        text = path.read_text(encoding="utf-8", errors="replace")
        if args.tail:
            text = "\n".join(text.splitlines()[-args.tail :])
        print(text, end="" if text.endswith("\n") else "\n")
        return
    _follow(path, root, run_id)


def _follow(path: Path, root: Path, run_id: str) -> None:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        try:
            while True:
                chunk = handle.read()
                if chunk:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                    continue
                data = state.load(root)
                run = data["runs"].get(run_id, {})
                if run.get("status") not in runner.ACTIVE_RUN_STATUSES:
                    remaining = handle.read()
                    if remaining:
                        sys.stdout.write(remaining)
                    print(
                        f"\n-- run {run_id} {run.get('status')} "
                        f"(exit {run.get('exit_code')}) --"
                    )
                    return
                time.sleep(0.3)
        except KeyboardInterrupt:  # pragma: no cover - interactive
            print()


def cmd_cancel(args) -> None:
    """Stop one active run, or reconcile every run whose process is gone.

    Both are the same intent: make recorded state match reality. With an id we
    kill a live run; without one we reap the records whose owner already died.
    """
    if args.id:
        runner.cancel(Path(args.root), args.id)
        print(f"cancelled {args.id}")
        return
    reaped = runner.reap(Path(args.root))
    if not reaped:
        print("no stale runs")
        return
    for run_id in reaped:
        print(f"marked {run_id} interrupted")





# --------------------------------------------------------------------------
# autonomous run


def cmd_run(args) -> int:
    """Walk the DAG: dispatch what is ready, review what is reported, repeat.

    The individual commands each move one task one step. This is the one that
    finishes a project, so its output is a progress log rather than a transcript:
    with several agents interleaved, mirroring their stdout would be unreadable.
    Each agent's full output is on disk, and `writ logs <task>` shows it.
    """
    root = Path(args.root)
    data = state.load(root)

    if args.dry_run:
        return _run_preview(data, args)

    # An unapproved plan does not execute. Checked before the session is claimed
    # and before anything is reaped, so a project held at `needs-approval` reads
    # as a plan waiting for review rather than as a run that failed.
    if not plans.runnable(data):
        message = plans.not_runnable_message(data)
        if not data.get("tasks"):
            # An empty project is not a refusal, it is an empty project. Exit 0
            # with the next command, the same as a project with nothing ready.
            if args.json:
                render.emit_json({"event": "idle", "reason": message})
            else:
                print(message)
            return 0
        raise WritError(message)

    existing = orchestrator.active_session(root)
    if existing and not args.force:
        raise WritError(
            f"another writ run is active (pid {existing}). Wait for it, stop it, "
            "or pass --force if you know it is gone."
        )

    # Reconcile before deciding there is nothing to do. A previous session that
    # was killed leaves tasks parked mid-flight, and they are exactly the work a
    # resume should pick up first.
    reaped = runner.reap(root)
    if reaped and not args.json:
        print(f"resuming: reconciled {len(reaped)} interrupted run(s)")
    data = state.load(root)

    jobs = orchestrator.preview(data, budget=args.max_tasks, order=args.order)
    if not jobs:
        if args.json:
            render.emit_json({"event": "idle", "reason": _nothing_to_run(data)})
        else:
            print(_nothing_to_run(data))
        return 0

    orchestrator.claim_session(root, force=args.force)
    parallel = max(1, args.parallel)
    if not args.json:
        print(
            f"running up to {parallel} agent{'s' if parallel > 1 else ''} at a time"
            + (f", at most {args.max_tasks} tasks" if args.max_tasks else "")
            + (
                f", deepest work first"
                if args.order == "depth"
                else ", most-unblocking first"
                if args.order == "unlocks"
                else ""
            )
        )
        print(f"logs: {state.runs_dir(root)}")
        print("─" * 62)
    reporter = _RunReporter(quiet=args.quiet, json_events=args.json)
    try:
        session = orchestrator.run(
            root,
            agent=args.agent,
            model=args.model,
            reviewer=args.reviewer,
            reviewer_model=args.reviewer_model,
            reviewer_timeout=getattr(args, "reviewer_timeout", None),
            parallel=parallel,
            max_tasks=args.max_tasks,
            order=args.order,
            timeout=args.timeout,
            cwd=args.cwd,
            max_rework=getattr(args, "max_rework", None),
            on_event=reporter,
        )
    finally:
        orchestrator.release_session(root)
    data = state.load(root)
    if args.json:
        render.emit_json(
            {
                "event": "summary",
                "agents": session.agent_runs,
                "tasks": len(set(session.dispatched)),
                "completed": session.completed,
                "failed": session.failed,
                "reworked": session.reworked,
                "errors": session.errors,
                "stopped": session.stopped or session.aborted,
                "remaining": [
                    task_id
                    for task_id, task in sorted(data["tasks"].items())
                    if task["status"] != "completed"
                ],
            }
        )
        return 1 if (session.failed or session.errors) else 0
    print("─" * 62)
    for line in orchestrator.summary(data, session):
        print(line)
    if session.stopped or session.aborted:
        print()
        print("stopped early; `writ run` again picks up where this left off")
    if session.errors:
        for message in session.errors:
            print(f"error: {message}", file=sys.stderr)
        return 1
    return 1 if session.failed else 0


def _run_preview(data, args) -> int:
    """Show the intended walk without spending anything."""
    jobs = orchestrator.preview(data, budget=args.max_tasks, order=args.order)
    if args.json:
        render.emit_json(
            {
                "event": "preview",
                "parallel": max(1, args.parallel),
                "order": args.order,
                "invocations": [
                    {"role": job.role, "task": job.task_id} for job in jobs
                ],
            }
        )
        return 0
    if not jobs:
        print(_nothing_to_run(data))
        return 0
    parallel = max(1, args.parallel)
    print(
        f"would run {len(jobs)} agent invocations, up to {parallel} at a time:"
    )
    for index, job in enumerate(jobs, start=1):
        print(f"  {index:>2}. {job.verb:<8} {job.task_id}")
    print()
    print(
        "a projection, not a promise: a rejected verdict changes what comes next"
    )
    return 0


def _nothing_to_run(data) -> str:
    """Say which kind of nothing this is; they need different responses."""
    tasks = data["tasks"]
    if not tasks:
        return "no tasks (run `writ plan <doc>` first)"
    if all(task["status"] == "completed" for task in tasks.values()):
        return "every task is complete"
    held = orchestrator.held_gates(data)
    if held:
        # A held gate is the commonest reason a project with unfinished tasks has
        # nothing to run, and it is the one the old message described worst: the
        # work is not failed, it is waiting, and what it waits for is named here.
        listed = ", ".join(f"{gate} ({reason})" for gate, reason in sorted(held.items()))
        return (
            f"nothing can start: {listed}. See `writ list gates` and "
            "`writ list repairs`"
        )
    stalled = orchestrator._stalled(data)
    if stalled:
        return (
            "nothing can start: "
            + ", ".join(stalled)
            + " wait on failed work (see `writ list --status failed`)"
        )
    return "nothing is ready to dispatch or awaiting review"


def _first_line(text: str, limit: int = 96) -> str:
    """The agent's summary as one line, since the log is one line per event."""
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


class _RunReporter:
    """Turns scheduler events into a readable progress log.

    Interleaved agent output is noise, so this reports transitions instead: what
    started, what it produced, and what that unblocked.
    """

    def __init__(self, *, quiet: bool, json_events: bool) -> None:
        self.quiet = quiet
        self.json_events = json_events
        self.lock = threading.Lock()

    def __call__(self, name: str, payload: dict[str, Any]) -> None:
        with self.lock:
            if self.json_events:
                render.emit_json({"event": name, **payload})
                return
            line = self._format(name, payload)
            if line is not None:
                print(line, flush=True)

    def _format(self, name: str, payload: dict[str, Any]) -> str | None:
        if name == "reaped":
            runs = ", ".join(payload["runs"])
            return f"reaped {len(payload['runs'])} interrupted run(s): {runs}"
        if name == "started":
            if self.quiet:
                return None
            if payload["role"] == "reviewer":
                verb = "review  "
            elif payload.get("attempt"):
                verb = "rework  "
            else:
                verb = "dispatch"
            return f"{verb} {payload['task']}  ->  {payload['command']}"
        if name == "finished":
            return self._finished(payload)
        if name == "stopping":
            return "\nstopping: finishing the agents already running (^C again to kill)"
        if name == "abort":
            return "\naborting: killing the agents still running"
        if name == "cancelled":
            return f"cancelled {payload['task']} (run {payload['run']})"
        if name == "error":
            return f"error    {payload['task']}: {payload['message']}"
        return None

    def _finished(self, payload: dict[str, Any]) -> str:
        """One line per transition: the mark, the new status, and the reason.

        A bare `x M01-002  failed` sends the reader to `writ show` to find out
        why, which is the wrong default for the one line they will actually see.
        The agent already wrote a one-line account; use it.
        """
        status = payload["status"] or "unknown"
        rework = payload.get("rework")
        if rework:
            # `planned` is the truthful status and a useless thing to print: the
            # reader's question about a rejected task is whether anything happens
            # next, and a bare `planned` reads as though it never ran.
            attempt, budget = rework
            parts = [
                f"{render.mark('planned')} {payload['task']}  "
                f"rework {attempt}/{budget}"
            ]
        else:
            parts = [f"{render.mark(status)} {payload['task']}  {status}"]

        counts = payload.get("criteria")
        if counts and counts.get("total"):
            parts.append(f"{counts['passed']}/{counts['total']}")

        unmet = payload.get("unmet")
        if unmet:
            parts.append(
                "unmet " + ", ".join(str(number) for number in unmet)
            )

        line = "  ".join(parts)
        if payload["error"]:
            line += f"  ({payload['error']})"
        elif payload["exit_code"] not in (0, None):
            line += f"  (exit {payload['exit_code']})"

        detail = []
        reason = payload.get("summary")
        if reason and (rework or status in ("failed", "blocked")):
            detail.append(_first_line(reason))
        proposed = payload.get("decisions")
        if proposed:
            detail.append(
                f"proposed {len(proposed)} decision"
                + ("s" if len(proposed) > 1 else "")
                + ": "
                + "; ".join(proposed[:2])
                + (" …" if len(proposed) > 2 else "")
            )
        out = [f"         {line}"]
        out += [f"           {item}" for item in detail]
        return "\n".join(out)
