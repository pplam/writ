"""Argument parsing and entry point.

The command surface is deliberately small. Four rules keep it that way:

1. One verb per job, not one verb per noun. `show` resolves any id — task,
   milestone, run, or decision — because "show me this thing" is one intent.
   `list` does the same for collections.
2. A status is a value, not a verb. `writ set <id> <status>` replaces the
   start/complete/fail/block/reset family, so adding a status never adds a
   command, and the legal values are visible in one place.
3. Agents judge their own work; operators do not. Whether a task is done is a
   claim about its acceptance criteria, so it is made by the agent that did the
   work and checked by a reviewer agent — not typed in by hand. `set` cannot
   reach `completed`; `override` can, and records that a human said so.

   The same rule runs the decision log. An agent that resolves a question the
   design left open proposes a record as part of its verdict, because the choice
   outlives the task. Proposals are inert until confirmed: an agent may report
   what it decided, but it may not commit the project on its own authority.
4. A loop over a command is not a new kind of command. `run` walks the graph by
   doing exactly what `dispatch` and `review` do, one task at a time, so
   anything true of them stays true of it: the same prompts, the same verdicts,
   the same records. It adds scheduling, not semantics, which is why stopping it
   halfway leaves a project you can still drive by hand.
"""
from __future__ import annotations

import argparse
import sys

from . import commands, config, critics
from .server import DEFAULT_PORT
from .decisions import SETTABLE_DECISION_STATUSES
from .model import DEFAULT_MAX_REWORK, JUDGED_STATUSES, SETTABLE_STATUSES
from .orchestrator import DEFAULT_ORDER, ORDERS
from .plans import SETTABLE_DISPOSITIONS
from .state import WritError

DESCRIPTION = """\
Writ turns a design document into an executable task DAG, dispatches tasks to
coding agents, tracks acceptance criteria, and keeps an append-only decision log.
State lives in <root>/.writ as JSON, markdown, and plain logs.

Typical flow:
  writ init
  writ plan design.md          an agent reads the doc and the repo
  writ run --parallel 3        work the whole graph, three agents at a time
  writ status                  where it got to

Or drive it one step at a time:
  writ dispatch M01-001        an agent implements it and reports a verdict
  writ review M01-001          a second agent checks the claim and signs off

Statuses, acceptance criteria, and decision records come from those agents rather
than by hand. Use `writ override` when a human needs the last word on a task, and
`writ set D-0001 active` to confirm a decision an agent proposed.
"""

EPILOG = """\
ids are resolved by shape, so one command serves every kind of thing:
  writ show M01          a milestone and its tasks
  writ show M01-001      a task, its gates, its runs
  writ show M01-001-2026…  one agent run
  writ show D-0001       one decision
  writ show F-0001       one finding, and how to dispose of it
  writ show RR-0001      one repair request, and every patch writ refused

every command accepts --root <project> and --json.

defaults marked `agents.x` or `run.x` come from <root>/.writ/config.json, so a
project's planner, reviewer and parallelism are chosen once rather than retyped.
A flag always overrides it; `writ agents` prints what is in effect. `writ init`
writes that file holding writ's own defaults, with a note explaining each role
and run setting, so changing one is an edit rather than a lookup.
"""


class _Parser(argparse.ArgumentParser):
    """Report argument errors on Writ's own exit path.

    argparse calls `sys.exit` itself, which makes usage errors indistinguishable
    from a crash to any caller that embeds `main`. Raising instead keeps every
    failure — bad flag or bad state — on one code path with one exit code.
    """

    def error(self, message: str):  # pragma: no cover - exercised via main
        raise WritError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="writ",
        description=DESCRIPTION,
        epilog=EPILOG,
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
    sub = parser.add_subparsers(
        dest="command", required=True, metavar="<command>", parser_class=_Parser
    )

    # ---------------------------------------------------------------- setup
    p = sub.add_parser(
        "init", help="create a Writ project, and a default config.json, in --root"
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="reset existing state; your config.json is kept either way",
    )
    p.set_defaults(func=commands.cmd_init)

    p = sub.add_parser(
        "plan",
        help="have a coding agent derive milestones and tasks from a design doc",
    )
    p.add_argument("design", help="path to a markdown design document")
    p.add_argument(
        "--agent", help="planning agent command (default: agents.planner, else pi)"
    )
    p.add_argument(
        "--model",
        help="model for the planning agent (translated to its own flag)",
    )
    p.add_argument(
        "--instructions",
        help="extra guidance for the planning agent (scope, priorities, constraints)",
    )
    p.add_argument(
        "--timeout", type=int, help="seconds before the planner is killed"
    )
    p.add_argument("--cwd", help="working directory for the agent (default: --root)")
    p.add_argument(
        "--extract",
        action="store_true",
        help="skip the agent; derive tasks from headings and gate markers only",
    )
    p.add_argument(
        "--from-plan",
        metavar="PATH",
        help="import a plan JSON artifact instead of running an agent",
    )
    p.add_argument(
        "--level", type=int, default=2, help="heading level for milestones (--extract)"
    )
    p.add_argument(
        "--flat",
        action="store_true",
        help="one task per milestone, do not split sub-sections (--extract)",
    )
    p.add_argument(
        "--parallel",
        action="store_true",
        help=argparse.SUPPRESS,  # now the default; kept so old invocations still work
    )
    p.add_argument(
        "--chain",
        action="store_true",
        help=(
            "order tasks the plan left independent, each after the last. Off by "
            "default: an omitted dependency stays omitted, so a plan that forgot "
            "an edge fails visibly instead of running in plan order"
        ),
    )
    p.add_argument(
        "--gates",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "add a review gate per milestone and a final gate over the plan "
            "(default: on). A gate judges integrated work against the "
            "requirements and can ask for the plan to be repaired"
        ),
    )
    p.add_argument(
        "--critics",
        nargs="*",
        metavar="NAME",
        help=(
            "after committing, have independent critics read the plan and report "
            "findings. Spends an agent run each, so it is opt-in: "
            + ", ".join(critic.name for critic in critics.CRITICS)
        ),
    )
    p.add_argument(
        "--critic-agent",
        metavar="CMD",
        help="agent for the critics (default: --agent). A different one reviews better",
    )
    p.add_argument("--critic-model", metavar="NAME", help="model for the critics")
    p.add_argument("--append", action="store_true", help="add to an existing plan")
    p.add_argument("--force", action="store_true", help="replace the existing plan")
    p.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="do not mirror the planning agent's output to this terminal",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print the planning prompt, or a previewed plan, without writing state",
    )
    p.set_defaults(func=commands.cmd_plan)

    p = sub.add_parser(
        "check",
        help="what Writ can prove about the current plan, as findings",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="include findings already accepted or resolved",
    )
    p.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="print nothing; exit 1 if anything blocking stands",
    )
    p.set_defaults(func=commands.cmd_check)

    p = sub.add_parser(
        "critique",
        help="have independent critics read the plan and report findings",
    )
    p.add_argument(
        "--agent",
        help="critic agent command (default: agents.critic, else the planner)",
    )
    p.add_argument("--model", help="model for the critic agents")
    p.add_argument(
        "--timeout", type=int, help="seconds before a critic is killed"
    )
    p.add_argument("--cwd", help="working directory for the agent (default: --root)")
    p.add_argument(
        "--critics",
        nargs="*",
        metavar="NAME",
        help=(
            "which critics to run (default: all). "
            + ", ".join(critic.name for critic in critics.CRITICS)
        ),
    )
    p.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="do not mirror the critics' output to the terminal",
    )
    p.set_defaults(func=commands.cmd_critique)

    p = sub.add_parser(
        "approve",
        help="record that the plan may be executed",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="approve despite blocking findings, accepting them on the record",
    )
    p.add_argument("--reason", help="why (required with --force)")
    p.add_argument(
        "--by", default="operator", help="who is approving (default: operator)"
    )
    p.set_defaults(func=commands.cmd_approve)

    p = sub.add_parser(
        "coverage",
        help="the requirement coverage matrix: what the design asked for, and where it went",
    )
    p.add_argument(
        "--uncovered",
        action="store_true",
        help="only requirements nothing stands behind",
    )
    p.add_argument("--requirement", help="one requirement id")
    p.set_defaults(func=commands.cmd_coverage)

    # ----------------------------------------------------------- inspection
    p = sub.add_parser(
        "status",
        help="progress, ready work, and live runs (--watch to follow)",
    )
    p.add_argument(
        "--watch",
        "-w",
        action="store_true",
        help="redraw until interrupted, or until no run is active",
    )
    p.add_argument("--interval", type=float, default=2.0, help="--watch seconds")
    p.add_argument(
        "--until-idle", action="store_true", help="with --watch, exit when idle"
    )
    p.add_argument(
        "--no-clear", action="store_true", help="with --watch, do not clear the screen"
    )
    p.set_defaults(func=commands.cmd_status)

    p = sub.add_parser(
        "list",
        help=(
            "list tasks (default), milestones, runs, decisions, findings, "
            "requirements, gates, or repairs"
        ),
    )
    p.add_argument(
        "what",
        nargs="?",
        default="tasks",
        choices=(
            "tasks",
            "milestones",
            "runs",
            "decisions",
            "findings",
            "requirements",
            "gates",
            "repairs",
        ),
        help="what to list (default: tasks)",
    )
    p.add_argument("--status", help="filter by status")
    p.add_argument("--milestone", help="filter by milestone id")
    p.add_argument("--task", help="filter by task id (runs, decisions, repairs)")
    p.add_argument(
        "--ready", action="store_true", help="only tasks that can be dispatched now"
    )
    p.add_argument(
        "--awaiting-review",
        action="store_true",
        help="only tasks an agent has reported and a reviewer has not checked",
    )
    p.add_argument(
        "--proposed",
        action="store_true",
        help="only decisions an agent proposed and nobody has ruled on",
    )
    p.add_argument(
        "--open",
        action="store_true",
        help="only findings nobody has dispositioned",
    )
    p.add_argument(
        "--uncovered",
        action="store_true",
        help="only requirements no task covers",
    )
    p.add_argument("--active", action="store_true", help="only live runs")
    p.add_argument("--limit", type=int, help="show at most N rows")
    p.set_defaults(func=commands.cmd_list)

    p = sub.add_parser(
        "show",
        help="show a task, milestone, run, or decision by id",
    )
    p.add_argument("id", help="M01, M01-001, a run id, or D-0001")
    p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="for a milestone, expand every member task in full",
    )
    p.add_argument(
        "--prompt", action="store_true", help="for a run, print the prompt it was given"
    )
    p.set_defaults(func=commands.cmd_show)

    p = sub.add_parser(
        "graph",
        help="draw the dependency DAG",
        description=(
            "Follows dependencies forwards, so the shape of the work is visible: "
            "what unlocks next, where it forks, what one task is holding up. A "
            "task reachable by several paths is expanded once and referenced "
            "with ↩ elsewhere, because it is one piece of work, not several.\n\n"
            "For a live view of the same graph alongside runs, prompts and logs, "
            "use `writ serve`."
        ),
    )
    p.add_argument(
        "--levels",
        action="store_true",
        help="group by dependency depth: what could run at the same time",
    )
    p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="add status and acceptance counts to each node",
    )
    p.add_argument("--dot", action="store_true", help="emit graphviz dot")
    p.set_defaults(func=commands.cmd_graph)

    p = sub.add_parser(
        "serve",
        help="a live web view of the whole project",
        description=(
            "Serves everything writ knows — the graph, milestones, task detail, "
            "run history, the exact prompt each agent was given, its output, and "
            "the verdict it wrote — and follows the store, so a long run can be "
            "watched instead of repeatedly re-read.\n\n"
            "It is read-only. No route changes anything, so the page cannot "
            "dispatch, cancel, override, or rule on a decision; driving the "
            "project stays in the terminal where the flags and the reasons are. "
            "It binds to this machine only, because the page has no "
            "authentication and does not need any while nothing else can reach it."
        ),
    )
    p.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"port to listen on (default {DEFAULT_PORT})",
    )
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="interface to bind (default 127.0.0.1, this machine only)",
    )
    p.add_argument(
        "--no-open",
        action="store_true",
        help="print the url instead of opening a browser",
    )
    p.set_defaults(func=commands.cmd_serve)

    p = sub.add_parser("logs", help="print or follow a run's output")
    p.add_argument("id", help="run id, or a task id for its latest run")
    p.add_argument(
        "--follow", "-f", action="store_true", help="stream until the run ends"
    )
    p.add_argument("--stderr", action="store_true", help="show stderr instead of stdout")
    p.add_argument("--tail", type=int, help="only the last N lines")
    p.set_defaults(func=commands.cmd_logs)

    # -------------------------------------------------------------- mutation
    p = sub.add_parser(
        "set",
        help="set the status of a task, a proposed decision, or a finding",
        description=(
            "Move a task around the board, rule on a decision an agent proposed, "
            "or dispose of a finding. Task statuses: "
            f"{', '.join(SETTABLE_STATUSES)}. Decision statuses: "
            f"{', '.join(SETTABLE_DECISION_STATUSES)}. Finding dispositions: "
            f"{', '.join(SETTABLE_DISPOSITIONS)}.\n\n"
            "This cannot mark a task completed: completion is a judgement about "
            "acceptance criteria, made by the agent that did the work and "
            "checked by `writ review`. Use `writ override` if a human has to "
            "decide. Nor can it resolve a finding: a finding resolves when a "
            "check or a gate demonstrates the outcome it asked for."
        ),
    )
    p.add_argument("id", help="task id, a D-NNNN decision, or an F-NNNN finding")
    p.add_argument(
        "status",
        choices=sorted(
            set(
                SETTABLE_STATUSES
                + SETTABLE_DECISION_STATUSES
                + SETTABLE_DISPOSITIONS
            )
        ),
    )
    p.add_argument("--evidence", help="note recorded with the transition")
    p.add_argument(
        "--reason",
        help="why: required to reject a decision or dispose of a finding",
    )
    p.add_argument(
        "--by", help="who is answering a finding (default: operator)"
    )
    p.add_argument(
        "--supersedes", help="when confirming a decision, the id it replaces"
    )
    p.add_argument(
        "--force", action="store_true", help="bypass the dependency gate"
    )
    p.set_defaults(func=commands.cmd_set)

    p = sub.add_parser(
        "review",
        help="have a second agent verify a task and sign it off",
        description=(
            "Dispatch a reviewer agent that re-checks the acceptance criteria "
            "without having written the code. Its decision completes or fails "
            "the task. With no id, reviews everything awaiting review."
        ),
    )
    p.add_argument("id", nargs="?", help="task to review (default: all awaiting)")
    p.add_argument(
        "--agent", help="reviewer command (default: agents.reviewer, else pi)"
    )
    p.add_argument("--model", help="model for the reviewer")
    p.add_argument("--timeout", type=int, help="seconds before kill")
    p.add_argument("--cwd", help="directory to run the reviewer in")
    p.add_argument(
        "--force", action="store_true", help="review a task that is not awaiting review"
    )
    p.add_argument("--quiet", "-q", action="store_true", help="do not mirror output")
    p.add_argument(
        "--dry-run", action="store_true", help="print the review prompt and stop"
    )
    p.add_argument(
        "--max-rework",
        type=int,
        metavar="N",
        help=(
            f"times a rejected task is re-dispatched with the review attached "
            f"before it is left failed (default: {DEFAULT_MAX_REWORK}, 0 to fail "
            "on the first rejection)"
        ),
    )
    p.set_defaults(func=commands.cmd_review)

    p = sub.add_parser(
        "override",
        help="record a human judgement that an agent could not reach",
        description=(
            "The escape hatch for when the agents are wrong or unavailable. "
            "Everything it writes is attributed to you rather than to an agent, "
            "so the evidence log stays honest about who decided what."
        ),
    )
    p.add_argument("id", help="task id")
    p.add_argument(
        "status",
        choices=sorted(set(SETTABLE_STATUSES + JUDGED_STATUSES)),
        help="status to force",
    )
    p.add_argument(
        "--reason", required=True, help="why the agents' judgement is being overridden"
    )
    p.add_argument(
        "--accept",
        action="append",
        metavar="N[=STATUS]",
        help="also set criterion N (default passed); repeatable",
    )
    p.set_defaults(func=commands.cmd_override)

    p = sub.add_parser(
        "task",
        help="create a task, or amend one by passing an existing id",
    )
    p.add_argument("id", nargs="?", help="task id to amend; omit to create")
    p.add_argument("--title", help="required when creating")
    p.add_argument("--milestone")
    p.add_argument("--depends", action="append", help="dependency task id (repeatable)")
    p.add_argument(
        "--acceptance", action="append", help="acceptance criterion (repeatable)"
    )
    p.add_argument("--allow", action="append", help="allowed file/package (repeatable)")
    p.add_argument("--forbid", action="append", help="forbidden path (repeatable)")
    p.set_defaults(func=commands.cmd_task)

    # -------------------------------------------------------------- dispatch
    p = sub.add_parser(
        "run",
        help="work the whole DAG: dispatch, review, repeat",
        description=(
            "Walks the task graph until it runs out of work. Dispatches what is "
            "ready, reviews what agents report, and lets each completion unblock "
            "the next tasks. Reviews are preferred over new dispatches, because "
            "only a completed task opens more of the graph.\n\n"
            "Stop it whenever you like: ^C finishes the agents already running, a "
            "second ^C kills them. Either way the store stays consistent and "
            "`writ run` again resumes from it."
        ),
    )
    p.add_argument(
        "--parallel",
        "-p",
        type=int,
        metavar="N",
        help="agents to run at once (default: 1)",
    )
    p.add_argument(
        "--max-tasks",
        type=int,
        default=None,
        metavar="N",
        help="stop after starting N tasks (reviews of them still finish)",
    )
    p.add_argument(
        "--max-rework",
        type=int,
        metavar="N",
        help=(
            f"times a rejected task is re-dispatched with the review attached "
            f"before it is left failed (default: {DEFAULT_MAX_REWORK}, 0 to fail "
            "on the first rejection)"
        ),
    )
    p.add_argument(
        "--order",
        choices=ORDERS,
        help=(
            "which ready task to start first: id follows the plan's numbering "
            "(default), depth prefers the longest remaining chain, unlocks "
            "prefers the task the most others wait on"
        ),
    )
    p.add_argument(
        "--agent", help="agent command (default: agents.implementer, else pi)"
    )
    p.add_argument("--model", help="model for the implementing agent")
    p.add_argument(
        "--reviewer",
        help=(
            "reviewer command (default: agents.reviewer, else --agent), so review "
            "can be independent"
        ),
    )
    p.add_argument("--reviewer-model", help="model for the reviewer")
    p.add_argument(
        "--reviewer-timeout",
        type=int,
        metavar="S",
        help="seconds before killing a reviewer (default: --timeout)",
    )
    p.add_argument(
        "--timeout", type=int, default=None, help="seconds before killing an agent"
    )
    p.add_argument("--cwd", help="working directory for the agents (default: --root)")
    p.add_argument(
        "--force",
        action="store_true",
        help="start even if another run session looks active",
    )
    p.add_argument(
        "--quiet", "-q", action="store_true", help="only report finished work"
    )
    p.add_argument(
        "--dry-run", action="store_true", help="print the intended walk and stop"
    )
    p.set_defaults(func=commands.cmd_run)

    p = sub.add_parser("dispatch", help="hand one task to a coding agent")
    p.add_argument("id")
    p.add_argument(
        "--agent", help="agent command (default: agents.implementer, else pi)"
    )
    p.add_argument(
        "--model", help="model for the agent (translated to its own flag)"
    )
    p.add_argument("--timeout", type=int, default=None, help="seconds before kill")
    p.add_argument("--cwd", help="working directory for the agent (default: --root)")
    p.add_argument(
        "--detach", action="store_true", help="run in the background under a supervisor"
    )
    p.add_argument("--force", action="store_true", help="ignore dependency gate")
    p.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="do not mirror the agent's output to this terminal",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="print the prompt and exit"
    )
    p.set_defaults(func=commands.cmd_dispatch)

    p = sub.add_parser(
        "cancel", help="stop an active run, or reconcile runs whose process died"
    )
    p.add_argument(
        "id",
        nargs="?",
        help="run id; omit to reap every run whose owning process is gone",
    )
    p.set_defaults(func=commands.cmd_cancel)

    p = sub.add_parser(
        "agents", help="show how writ invokes each known agent headlessly"
    )
    p.add_argument("--agent", help="preview one agent command")
    p.add_argument("--model", help="preview with this model")
    p.set_defaults(func=commands.cmd_agents)

    p = sub.add_parser("supervise", help=argparse.SUPPRESS)
    p.add_argument("run_id")
    p.set_defaults(func=commands.cmd_supervise)

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
    try:
        args = parser.parse_args(head)
        args.agent_args = agent_args
        # Between parsing and running: the flags say what this invocation wants,
        # and anything they left unset comes from the project's own defaults. A
        # malformed config fails here, before an agent is started, rather than
        # three tasks into a run.
        args.resolved_from = config.apply(args, config.load(args.root))
        result = args.func(args)
    except WritError as exc:
        print(f"writ: {exc}", file=sys.stderr)
        return 2
    except BrokenPipeError:  # pragma: no cover - piping to head
        return 0
    return int(result) if isinstance(result, int) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
