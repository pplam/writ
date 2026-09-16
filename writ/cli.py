"""Argument parsing and entry point."""
from __future__ import annotations

import argparse
import sys

from . import commands
from .model import ACCEPTANCE_STATUSES
from .state import WritError

DESCRIPTION = """\
Writ turns a design document into an executable task DAG, dispatches tasks to
coding agents, tracks acceptance criteria, and keeps an append-only decision log.
State lives in <root>/.writ as JSON, markdown, and plain logs.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="writ",
        description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--root",
        default=".",
        help="project directory containing .writ (default: current directory)",
    )
    parser.add_argument(
        "--json", action="store_true", help="machine-readable output where supported"
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    # setup
    p = sub.add_parser("init", help="create a Writ project in --root")
    p.add_argument("--force", action="store_true", help="overwrite existing state")
    p.set_defaults(func=commands.cmd_init)

    p = sub.add_parser("plan", help="derive milestones and tasks from a design doc")
    p.add_argument("design", help="path to a markdown design document")
    p.add_argument("--level", type=int, default=2, help="heading level for milestones")
    p.add_argument(
        "--flat",
        action="store_true",
        help="one task per milestone (do not split sub-sections)",
    )
    p.add_argument(
        "--parallel",
        action="store_true",
        help="do not chain tasks; leave them independent",
    )
    p.add_argument("--append", action="store_true", help="add to an existing plan")
    p.add_argument("--force", action="store_true", help="replace the existing plan")
    p.add_argument(
        "--dry-run", action="store_true", help="print the plan without writing state"
    )
    p.add_argument("--agent", default="pi", help="agent command (default: pi)")
    p.add_argument("--model", help="model for the planning agent")
    p.add_argument(
        "--timeout", type=int, default=1800, help="seconds before the agent is killed"
    )
    p.add_argument(
        "--extract",
        action="store_true",
        help="derive the plan by parsing headings instead of asking an agent",
    )
    p.add_argument("--from-plan", help="commit a plan.json an agent already wrote")
    p.add_argument("--quiet", "-q", action="store_true", help="do not mirror output")
    p.add_argument("--cwd", help="directory to run the planning agent in")
    p.add_argument(
        "--instructions",
        help="extra guidance for the planning agent (scope, priorities, constraints)",
    )
    p.set_defaults(func=commands.cmd_plan)

    # inspection
    p = sub.add_parser("status", help="overall progress, active runs, what is ready")
    p.set_defaults(func=commands.cmd_status)

    p = sub.add_parser("tasks", help="list tasks")
    p.add_argument("--status", help="filter by status")
    p.add_argument("--milestone", help="filter by milestone id")
    p.add_argument("--ready", action="store_true", help="only dispatchable tasks")
    tsub = p.add_subparsers(dest="task_cmd")
    tv = tsub.add_parser("show", help="show one task in detail")
    tv.add_argument("id")
    tv.set_defaults(func=commands.cmd_task_show)
    p.set_defaults(func=commands.cmd_tasks)

    p = sub.add_parser("milestones", help="list milestones with rollups")
    msub = p.add_subparsers(dest="milestone_cmd")
    m = msub.add_parser("show", help="show one milestone in detail")
    m.add_argument("id")
    m.add_argument("--verbose", "-v", action="store_true", help="expand every task")
    m.set_defaults(func=commands.cmd_milestone_show)
    p.set_defaults(func=commands.cmd_milestones)

    p = sub.add_parser("show", help="show a task or milestone in detail")
    p.add_argument("id")
    p.add_argument("--verbose", "-v", action="store_true", help="expand every task")
    p.set_defaults(func=commands.cmd_show)

    p = sub.add_parser("next", help="what can be dispatched now")
    p.add_argument("--limit", type=int, default=5)
    p.set_defaults(func=commands.cmd_next)

    p = sub.add_parser("graph", help="print the dependency DAG")
    p.add_argument("--dot", action="store_true", help="emit graphviz dot")
    p.set_defaults(func=commands.cmd_graph)

    # task mutation
    for name, status, helptext in (
        ("start", "running", "mark a task as being worked on"),
        ("complete", "completed", "mark a task complete (requires acceptances passed)"),
        ("fail", "failed", "mark a task failed"),
        ("block", "blocked", "mark a task blocked"),
        ("reset", "planned", "return a task to planned"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("id")
        p.add_argument("--evidence", help="note recorded with the transition")
        p.add_argument(
            "--force", action="store_true", help="bypass dependency/acceptance gates"
        )
        p.set_defaults(
            func=lambda args, status=status: commands.cmd_set_status(args, status)
        )

    p = sub.add_parser("accept", help="set an acceptance criterion's status")
    p.add_argument("id")
    p.add_argument("number", help="1-based index from `writ show`")
    p.add_argument("status", choices=ACCEPTANCE_STATUSES)
    p.set_defaults(func=commands.cmd_accept)

    p = sub.add_parser("add-task", help="create a task by hand")
    p.add_argument("title")
    p.add_argument("--id", help="explicit task id")
    p.add_argument("--milestone")
    p.add_argument("--depends", action="append", help="dependency task id (repeatable)")
    p.add_argument(
        "--acceptance", action="append", help="acceptance criterion (repeatable)"
    )
    p.add_argument("--allow", action="append", help="allowed file/package (repeatable)")
    p.add_argument("--forbid", action="append", help="forbidden path (repeatable)")
    p.set_defaults(func=commands.cmd_add_task)

    p = sub.add_parser("edit-task", help="amend a task")
    p.add_argument("id")
    p.add_argument("--title")
    p.add_argument("--depends", action="append")
    p.add_argument("--acceptance", action="append", help="append a criterion")
    p.add_argument("--allow", action="append")
    p.add_argument("--forbid", action="append")
    p.set_defaults(func=commands.cmd_edit_task)

    # dispatch
    p = sub.add_parser("dispatch", help="hand a task to a coding agent")
    p.add_argument("id")
    p.add_argument("--agent", default="pi", help="agent command (default: pi)")
    p.add_argument("--timeout", type=int, default=None, help="seconds before kill")
    p.add_argument("--cwd", help="working directory for the agent (default: --root)")
    p.add_argument(
        "--detach", action="store_true", help="run in the background under a supervisor"
    )
    p.add_argument("--force", action="store_true", help="ignore dependency gate")
    p.add_argument(
        "--dry-run", action="store_true", help="print the prompt and exit"
    )
    p.add_argument("--model", help="model for the agent")
    p.add_argument("--quiet", "-q", action="store_true", help="do not mirror output")
    p.set_defaults(func=commands.cmd_dispatch)

    p = sub.add_parser("agents", help="show how writ invokes each known agent")
    p.add_argument("--agent", help="show one agent")
    p.add_argument("--model", help="include a model flag")
    p.set_defaults(func=commands.cmd_agents)

    p = sub.add_parser("supervise", help=argparse.SUPPRESS)
    p.add_argument("run_id")
    p.set_defaults(func=commands.cmd_supervise)

    # monitoring
    p = sub.add_parser("runs", help="list agent runs")
    p.add_argument("--task", help="filter by task id")
    p.add_argument("--active", action="store_true", help="only live runs")
    p.set_defaults(func=commands.cmd_runs)

    p = sub.add_parser("run", help="show one run")
    p.add_argument("run_id")
    p.set_defaults(func=commands.cmd_run_show)

    p = sub.add_parser("logs", help="print or follow a run's output")
    p.add_argument("run_id", help="run id, or a task id for its latest run")
    p.add_argument("--follow", "-f", action="store_true", help="stream until the run ends")
    p.add_argument("--stderr", action="store_true", help="show stderr instead of stdout")
    p.add_argument("--tail", type=int, help="only the last N lines")
    p.set_defaults(func=commands.cmd_logs)

    p = sub.add_parser("cancel", help="stop an active run")
    p.add_argument("run_id")
    p.set_defaults(func=commands.cmd_cancel)

    p = sub.add_parser("reap", help="reconcile runs whose process died")
    p.set_defaults(func=commands.cmd_reap)

    p = sub.add_parser("watch", help="live status view")
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--once", action="store_true", help="render a single frame")
    p.add_argument("--until-idle", action="store_true", help="exit when no run is active")
    p.add_argument("--no-clear", action="store_true", help="do not clear the screen")
    p.set_defaults(func=commands.cmd_watch)

    # decision log
    p = sub.add_parser("decision", help="manage the decision log")
    dsub = p.add_subparsers(dest="decision_command", required=True, metavar="<action>")

    d = dsub.add_parser("add", help="append a decision")
    d.add_argument("--title", required=True)
    d.add_argument("--decision", required=True, help="the decision itself")
    d.add_argument("--context", help="why the decision was needed")
    d.add_argument("--consequences", help="what it commits or invalidates")
    d.add_argument("--supersedes", help="decision id this replaces")
    d.add_argument("--task", action="append", help="related task id (repeatable)")
    d.set_defaults(func=commands.cmd_decision_add)

    d = dsub.add_parser("list", help="list decisions")
    d.add_argument("--task", help="only decisions touching this task")
    d.add_argument("--active", action="store_true", help="hide superseded records")
    d.set_defaults(func=commands.cmd_decision_list)

    d = dsub.add_parser("show", help="show one decision")
    d.add_argument("decision_id")
    d.set_defaults(func=commands.cmd_decision_show)

    d = dsub.add_parser("export", help="render the log as markdown")
    d.add_argument("--out", help="write to a file instead of stdout")
    d.set_defaults(func=commands.cmd_decision_export)

    return parser


def split_agent_args(argv: list[str]) -> tuple[list[str], list[str]]:
    """Everything after the first bare `--` belongs to the dispatched agent.

    Handled before argparse because `argparse.REMAINDER` also swallows sibling
    flags such as `--agent` and `--dry-run`.
    """
    if "--" not in argv:
        return argv, []
    index = argv.index("--")
    return argv[:index], argv[index + 1 :]


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    head, agent_args = split_agent_args(raw)
    parser = build_parser()
    args = parser.parse_args(head)
    args.agent_args = agent_args
    try:
        result = args.func(args)
    except WritError as exc:
        print(f"writ: {exc}", file=sys.stderr)
        return 2
    except BrokenPipeError:  # pragma: no cover - piping to head
        return 0
    return int(result) if isinstance(result, int) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
