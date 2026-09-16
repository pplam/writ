"""Command implementations. Each takes parsed args and prints a result."""
from __future__ import annotations

import os
import shlex
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from . import agents, decisions, planner, planning, render, runner, state, verdict
from .model import (
    acceptance_summary,
    add_evidence,
    add_milestone,
    add_task,
    check_dag,
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
)
from .state import WritError


# --------------------------------------------------------------------------
# project setup


def cmd_init(args) -> None:
    location = state.initialize(args.root, force=args.force)
    print(f"initialized Writ project at {location}")
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
        milestones = planning.read_plan(plan_path)
        source = f"plan {plan_path.name}"
    elif args.extract:
        milestones = planner.parse(
            doc.read_text(encoding="utf-8"),
            milestone_level=args.level,
            split_subsections=not args.flat,
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

        milestones, plan_path, code = planning.generate(
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

    summary = planner.summarize(milestones)
    missing = planning.unresolved_sections(milestones, doc)

    if args.dry_run:
        _print_plan(milestones, summary, missing)
        return 0

    created = _commit_plan(
        args,
        root=root,
        doc=doc,
        milestones=milestones,
        plan_path=plan_path,
        source=source,
    )
    print(
        f"created {summary['milestones']} milestones and {created} tasks "
        f"from {source}"
    )
    for section in missing:
        print(f"note: no section titled {section!r} in {doc.name}", file=sys.stderr)
    print("next: writ status")
    return 0


def _print_plan(
    milestones: list[planner.PlannedMilestone],
    summary: dict[str, Any],
    missing: list[str],
) -> None:
    for milestone in milestones:
        print(milestone.title)
        if milestone.notes:
            print(f"    {milestone.notes}")
        for task in milestone.tasks:
            print(f"  - {task.title}")
            if task.notes:
                print(f"      {task.notes}")
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
    milestones: list[planner.PlannedMilestone],
    plan_path: Path | None,
    source: str,
) -> int:
    with state.transaction(root) as data:
        if data["tasks"] and not (args.append or args.force):
            raise WritError(
                "this project already has tasks; use --append to add, "
                "or --force to replace the plan"
            )
        if args.force:
            data["tasks"] = {}
            data["milestones"] = {}
        offset = len(data["milestones"])
        doc_path = str(doc.resolve())
        if doc_path not in data["design_docs"]:
            data["design_docs"].append(doc_path)

        built = planner.build_ids(milestones, offset)
        translate = planner.ref_map(built)
        previous: str | None = None
        if args.append:
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
                    task, translate, data, previous, chain=not args.parallel
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
                )
                if task.notes:
                    add_evidence(data["tasks"][task_id], f"plan: {task.notes}")
                created += 1
                previous = task_id
        check_dag(data)
        refresh_milestones(data)
        data.setdefault("plans", []).append(
            {
                "source": source,
                "design_doc": doc_path,
                "artifact": str(plan_path) if plan_path else None,
                "milestones": [milestone_id for milestone_id, _, _ in built],
                "created_at": state.utcnow(),
            }
        )
    return created


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
    silently dropped edge. When the plan states no dependencies at all we fall
    back to the historical behaviour: chain onto the previous task unless
    `--parallel` said to leave tasks independent.
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
                "title": task["title"],
                "milestone": task.get("milestone"),
                "depends_on": task.get("depends_on", []),
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
    rows = [[i["id"], i["status"], i["date"], i["title"]] for i in items]
    return ["ID", "STATUS", "DATE", "TITLE"], rows, list(items)


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
    }[kind]
    print(renderer())


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
    data = state.load(args.root)
    check_dag(data)
    if args.dot:
        print("digraph writ {")
        print('  rankdir=LR; node [shape=box, fontname="Helvetica"];')
        for task_id in sorted(data["tasks"]):
            task = data["tasks"][task_id]
            label = task["title"].replace('"', "'")
            print(f'  "{task_id}" [label="{task_id}\\n{label}"];')
            for dep in task.get("depends_on", []):
                print(f'  "{dep}" -> "{task_id}";')
        print("}")
        return
    for task_id in sorted(data["tasks"]):
        task = data["tasks"][task_id]
        deps = ", ".join(task.get("depends_on", [])) or "-"
        status = effective_status(data, task)
        print(f"{render.mark(status)} {task_id}  <- {deps}")


# --------------------------------------------------------------------------
# mutation


def cmd_set(args) -> None:
    with state.transaction(args.root) as data:
        set_status(
            data, args.id, args.status, evidence=args.evidence, force=args.force
        )
    print(f"{args.id} -> {args.status}")


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
    if code == 124 and not runner.produced_output(directory):
        print(agents.hang_hint(resolved), file=sys.stderr)
    _report_verdict(root, run_id, task_id, directory, role)
    return code


def _report_verdict(
    root: Path, run_id: str, task_id: str, directory: Path, role: str
) -> None:
    """Say what the agent claimed and what writ did about it.

    The status change is the interesting part of a run, so it is reported
    explicitly rather than left for the user to go and look up.
    """
    status, error = runner.verdict_summary(root, run_id)
    if error:
        print(f"warning: {error}", file=sys.stderr)
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
    if status == "awaiting-review":
        print(f"next: writ review {task_id}")
    elif status == "failed":
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
    if args.json:
        render.emit_json(payload)
        return
    print(render.table(["AGENT", "FOUND", "HEADLESS INVOCATION", "MODEL FLAG"], rows))
    print("\nany other command is passed through unchanged; add its own")
    print("non-interactive flag to --agent so it does not wait on a terminal")


def cmd_supervise(args) -> int:
    """Internal: owns a detached run until the agent exits."""
    return runner.execute(Path(args.root), args.run_id)


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


def cmd_decide(args) -> None:
    """Append a decision. Reading them is `writ list decisions` / `writ show`."""
    with state.transaction(args.root) as data:
        record = decisions.add(
            data,
            title=args.title,
            decision=args.decision,
            context=args.context or "",
            consequences=args.consequences or "",
            supersedes=args.supersedes,
            tasks=args.task or [],
        )
        decisions.sync_markdown(args.root, data)
        markdown = decisions.render_markdown(data)
    if args.export:
        Path(args.export).expanduser().write_text(markdown, encoding="utf-8")
    print(f"recorded {record['id']}: {record['title']}")
    print(f"mirror: {state.decisions_file(args.root)}")
    if args.export:
        print(f"exported: {args.export}")
