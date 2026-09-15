"""Command implementations. Each takes parsed args and prints a result."""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from . import decisions, planner, render, runner, state
from .model import (
    acceptance_summary,
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
    set_acceptance,
    set_status,
)
from .state import ForgeError


# --------------------------------------------------------------------------
# project setup


def cmd_init(args) -> None:
    location = state.initialize(args.root, force=args.force)
    print(f"initialized Forge project at {location}")
    print("next: forge plan <design.md>")


def cmd_plan(args) -> None:
    doc = Path(args.design).expanduser()
    if not doc.exists():
        raise ForgeError(f"design document not found: {doc}")
    milestones = planner.parse(
        doc.read_text(encoding="utf-8"),
        milestone_level=args.level,
        split_subsections=not args.flat,
    )
    summary = planner.summarize(milestones)
    if args.dry_run:
        for milestone in milestones:
            print(f"{milestone.title}")
            for task in milestone.tasks:
                print(f"  - {task.title}")
                for item in task.acceptances:
                    print(f"      · {item}")
        print(
            f"\nwould create {summary['milestones']} milestones, "
            f"{summary['tasks']} tasks, {summary['acceptances']} acceptance criteria"
        )
        return

    with state.transaction(args.root) as data:
        if data["tasks"] and not (args.append or args.force):
            raise ForgeError(
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
        previous: str | None = None
        if args.append:
            existing = sorted(data["tasks"])
            previous = existing[-1] if existing else None
        created_tasks = 0
        for milestone_id, milestone, tasks in planner.build_ids(milestones, offset):
            add_milestone(
                data,
                milestone_id=milestone_id,
                title=milestone.title,
                design_section=milestone.section,
            )
            for task_id, task in tasks:
                add_task(
                    data,
                    task_id=task_id,
                    title=task.title,
                    milestone=milestone_id,
                    depends_on=[previous] if previous and not args.parallel else [],
                    acceptances=task.acceptances,
                    design_section=task.section,
                    design_doc=doc_path,
                )
                created_tasks += 1
                if not args.parallel:
                    previous = task_id
        check_dag(data)
        refresh_milestones(data)
    print(
        f"created {summary['milestones']} milestones and {created_tasks} tasks "
        f"from {doc.name}"
    )
    print("next: forge status")


# --------------------------------------------------------------------------
# listing and inspection


def cmd_milestones(args) -> None:
    data = state.load(args.root)
    refresh_milestones(data)
    rows = []
    payload = []
    for milestone_id in sorted(data["milestones"]):
        milestone = data["milestones"][milestone_id]
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
    if args.json:
        render.emit_json(payload)
        return
    print(render.table(["", "ID", "STATUS", "DONE", "TITLE"], rows))


def cmd_tasks(args) -> None:
    data = state.load(args.root)
    rows = []
    payload = []
    for task_id in sorted(data["tasks"]):
        task = data["tasks"][task_id]
        status = effective_status(data, task)
        if args.status and status != args.status:
            continue
        if args.milestone and task.get("milestone") != args.milestone:
            continue
        if args.ready and status != "ready":
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
    if args.json:
        render.emit_json(payload)
        return
    print(render.table(["", "ID", "STATUS", "ACC", "DEPS", "TITLE"], rows))


def cmd_show(args) -> None:
    data = state.load(args.root)
    refresh_milestones(data)
    kind, item = find(data, args.id)
    if args.json:
        render.emit_json(item)
        return
    if kind == "milestone":
        print(f"{item['id']}  {item['title']}")
        print(f"status: {item['status']}")
        print(f"design section: {item.get('design_section') or '-'}")
        print("\ntasks:")
        rows = [
            [
                render.mark(effective_status(data, task)),
                task["id"],
                effective_status(data, task),
                task["title"],
            ]
            for task in sorted(milestone_tasks(data, item["id"]), key=lambda t: t["id"])
        ]
        print(render.table(["", "ID", "STATUS", "TITLE"], rows))
        return

    status = effective_status(data, item)
    print(f"{item['id']}  {item['title']}")
    print(f"status: {status}")
    print(f"milestone: {item.get('milestone') or '-'}")
    print(f"depends on: {', '.join(item.get('depends_on', [])) or '-'}")
    blockers = blocking_dependencies(data, item)
    if blockers:
        print(f"blocked by: {', '.join(blockers)}")
    print(f"design: {item.get('design_doc') or '-'}")
    print(f"section: {item.get('design_section') or '-'}")
    print("\nacceptance criteria:")
    for index, acceptance in enumerate(item.get("acceptances", []), start=1):
        print(render.acceptance_line(index, acceptance))
    if item.get("allowed"):
        print("\nallowed:")
        for entry in item["allowed"]:
            print(f"  - {entry}")
    if item.get("forbidden"):
        print("\nforbidden:")
        for entry in item["forbidden"]:
            print(f"  - {entry}")
    if item.get("runs"):
        print("\nruns:")
        for run_id in item["runs"]:
            run = data["runs"].get(run_id, {})
            print(f"  - {run_id}  {run.get('status')}  exit={run.get('exit_code')}")
    if item.get("evidence"):
        print("\nevidence:")
        for entry in item["evidence"]:
            print(f"  - {entry['at']}  {entry['text']}")


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
        "running": [
            task_id
            for task_id, task in sorted(tasks.items())
            if task["status"] == "running"
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
    data = state.load(args.root)
    payload = _status_payload(data)
    if args.json:
        render.emit_json(payload)
        return
    print(_render_status(payload))


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


def cmd_next(args) -> None:
    data = state.load(args.root)
    candidates = ready_tasks(data)[: args.limit]
    if args.json:
        render.emit_json([task["id"] for task in candidates])
        return
    if not candidates:
        print("nothing is ready; run `forge status` to see what is blocking")
        return
    print(
        render.table(
            ["ID", "MILESTONE", "TITLE"],
            [[t["id"], t.get("milestone") or "-", t["title"]] for t in candidates],
        )
    )


def cmd_graph(args) -> None:
    data = state.load(args.root)
    check_dag(data)
    if args.dot:
        print("digraph forge {")
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


def cmd_set_status(args, status: str) -> None:
    with state.transaction(args.root) as data:
        set_status(
            data, args.id, status, evidence=args.evidence, force=args.force
        )
    print(f"{args.id} -> {status}")


def cmd_accept(args) -> None:
    with state.transaction(args.root) as data:
        task = set_acceptance(data, args.id, int(args.number), args.status)
        counts = acceptance_summary(task)
    print(
        f"{args.id} acceptance {args.number} -> {args.status} "
        f"({counts['passed']}/{counts['total']} passed)"
    )


def cmd_add_task(args) -> None:
    with state.transaction(args.root) as data:
        milestone = args.milestone
        if milestone and milestone not in data["milestones"]:
            add_milestone(data, milestone_id=milestone, title=milestone)
        task_id = args.id or _next_task_id(data, milestone)
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


def _next_task_id(data: dict[str, Any], milestone: str | None) -> str:
    prefix = milestone or "T"
    existing = [key for key in data["tasks"] if key.startswith(f"{prefix}-")]
    return f"{prefix}-{len(existing) + 1:03d}"


def cmd_edit_task(args) -> None:
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
                    raise ForgeError(f"unknown dependency: {dep}")
            task["depends_on"] = args.depends
        if args.allow:
            task["allowed"] = args.allow
        if args.forbid:
            task["forbidden"] = args.forbid
        task["updated_at"] = state.utcnow()
        check_dag(data)
        refresh_milestones(data)
    print(f"updated {args.id}")


# --------------------------------------------------------------------------
# dispatch and monitoring


def cmd_dispatch(args) -> int:
    extra = list(getattr(args, "agent_args", []) or [])
    if args.dry_run:
        data = state.load(args.root)
        task = get_task(data, args.id)
        print(runner.build_prompt(data, task, Path(args.root)))
        return 0
    run_id, directory, _ = runner.prepare(
        Path(args.root),
        args.id,
        args.agent,
        extra,
        timeout=args.timeout,
        cwd=args.cwd,
        force=args.force,
    )
    if args.detach:
        pid = runner.detach(Path(args.root), run_id)
        print(f"dispatched {args.id} as run {run_id} (detached, supervisor pid {pid})")
        print(f"logs: forge logs {run_id} --follow")
        return 0
    print(f"dispatched {args.id} as run {run_id}")
    print(f"logs: {directory}")
    code = runner.execute(Path(args.root), run_id)
    print(f"run {run_id} finished with exit code {code}")
    if code == 0:
        print(
            f"next: verify acceptances, then `forge accept {args.id} <n> passed` "
            f"and `forge complete {args.id}`"
        )
    return code


def cmd_supervise(args) -> int:
    """Internal: owns a detached run until the agent exits."""
    return runner.execute(Path(args.root), args.run_id)


def cmd_runs(args) -> None:
    data = state.load(args.root)
    rows = []
    payload = []
    for run_id in sorted(data["runs"]):
        run = data["runs"][run_id]
        if args.task and run["task"] != args.task:
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
    if args.json:
        render.emit_json(payload)
        return
    print(
        render.table(
            ["", "RUN", "TASK", "STATUS", "EXIT", "ALIVE", "STARTED"], rows
        )
    )


def cmd_run_show(args) -> None:
    data = state.load(args.root)
    run = runner.resolve_run(data, args.run_id)
    if args.json:
        render.emit_json(run)
        return
    for key in (
        "id",
        "task",
        "status",
        "exit_code",
        "pid",
        "supervisor_pid",
        "cwd",
        "timeout",
        "created_at",
        "started_at",
        "finished_at",
        "dir",
    ):
        if key in run:
            print(f"{key}: {run[key]}")
    print(f"command: {' '.join(run['command'])}")
    print(f"alive: {'yes' if runner.process_alive(run.get('pid')) else 'no'}")


def cmd_logs(args) -> None:
    root = Path(args.root)
    data = state.load(root)
    run_id = args.run_id
    if run_id in data["tasks"]:
        latest = runner.latest_run_for(data, run_id)
        if latest is None:
            raise ForgeError(f"task {run_id} has no runs yet")
        run_id = latest
    stream = "stderr" if args.stderr else "stdout"
    path = runner.log_path(root, run_id, stream)
    if not path.exists():
        raise ForgeError(f"no {stream} log yet for {run_id}")
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
    runner.cancel(Path(args.root), args.run_id)
    print(f"cancelled {args.run_id}")


def cmd_reap(args) -> None:
    reaped = runner.reap(Path(args.root))
    if not reaped:
        print("no stale runs")
        return
    for run_id in reaped:
        print(f"marked {run_id} interrupted")


def cmd_watch(args) -> None:  # pragma: no cover - interactive loop
    try:
        while True:
            data = state.load(args.root)
            payload = _status_payload(data)
            if not args.no_clear:
                os.system("clear" if shutil.which("clear") else "")
            print(f"forge watch — {state.utcnow()}  (ctrl-c to exit)\n")
            print(_render_status(payload))
            if args.once or not payload["active_runs"] and args.until_idle:
                return
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()


# --------------------------------------------------------------------------
# decision log


def cmd_decision_add(args) -> None:
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
    print(f"recorded {record['id']}: {record['title']}")
    print(f"mirror: {state.decisions_file(args.root)}")


def cmd_decision_list(args) -> None:
    data = state.load(args.root)
    items = data["decisions"]
    if args.task:
        items = [item for item in items if args.task in item.get("tasks", [])]
    if args.active:
        items = [item for item in items if item["status"] == "active"]
    if args.json:
        render.emit_json(items)
        return
    print(
        render.table(
            ["ID", "STATUS", "DATE", "TITLE"],
            [[i["id"], i["status"], i["date"], i["title"]] for i in items],
        )
    )


def cmd_decision_show(args) -> None:
    data = state.load(args.root)
    record = decisions.get(data, args.decision_id)
    if args.json:
        render.emit_json(record)
        return
    print(f"{record['id']} — {record['title']}")
    print(f"date: {record['date']}")
    print(f"status: {record['status']}")
    if record.get("supersedes"):
        print(f"supersedes: {record['supersedes']}")
    if record.get("superseded_by"):
        print(f"superseded by: {record['superseded_by']}")
    if record.get("tasks"):
        print(f"tasks: {', '.join(record['tasks'])}")
    if record.get("context"):
        print(f"\ncontext:\n{record['context']}")
    print(f"\ndecision:\n{record['decision']}")
    if record.get("consequences"):
        print(f"\nconsequences:\n{record['consequences']}")


def cmd_decision_export(args) -> None:
    data = state.load(args.root)
    markdown = decisions.render_markdown(data)
    if args.out:
        Path(args.out).expanduser().write_text(markdown, encoding="utf-8")
        print(f"wrote {args.out}")
        return
    print(markdown, end="")
