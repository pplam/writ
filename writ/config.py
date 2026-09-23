"""Per-project defaults, so a choice is made once rather than on every command.

Every agent and model writ uses came from a flag, and the default was the string
`pi` written into five argparse calls. That is fine for one command and wrong for
the way the tool is actually used: a project settles on a planner, a reviewer that
is deliberately not the planner, and a parallelism its graph can absorb, and then
retypes all of it on every invocation. The flags that matter most are the ones
easiest to forget — `--reviewer` above all, since leaving it off silently hands
the review to the model that wrote the code.

So `.writ/config.json` holds those defaults — every one of them. Not just the
agents and the run knobs: anything a flag can say that is a standing decision
rather than a description of this one invocation. That distinction is the whole
design of the file, and it cuts in a specific place.

**What belongs here** is a setting a project would give the same answer to every
time. Which agent plans, which one reviews, whether the staged pipeline runs,
whether gates are added, which critics read a committed plan, how wide `writ run`
goes, what port `writ serve` binds. `--no-stages` swapping the staged pipeline for
the older single-shot planner is exactly the kind of decision that should be
written down once, and exactly the kind nobody remembers to retype.

**What does not** is anything naming *this* piece of work — a task id, a design
document, `--stage` stopping after one analysis, `--plan-id` resuming a particular
pipeline — and the filters on the inspection commands, because `list --status` or
`coverage --uncovered` as a standing default would leave writ reporting a subset of
the project while looking like it reported all of it. That is the same failure as a
silently ignored key: the file on disk would be lying about what you are seeing.
`--force` is left out for the same reason and a sharper one: every `--force` in
writ overrides a safety check — resetting state, dispatching past an unmet
dependency, approving over blocking findings — and a project that has quietly
turned all of those off has disabled the thing writ is for.

Three rules govern what is here.

**A flag always wins.** The config is a default, not a policy: it answers "what
did this project decide", and the flag answers "what am I doing right now". For
that to be true of a boolean, the parser cannot carry the default itself — with
`action="store_true"`, an absent `--quiet` and an explicit one are both `False`,
and nothing downstream can tell them apart. So every configurable flag parses with
`default=None` and gets its real default here, which is what makes `quiet: true`
in a config overridable and `--gates/--no-gates` meaningful in both directions.

**Everything in it is validated, and an unknown key is an error.** A config is
hand-edited, which means a typo in one is as likely as a typo in a flag — and a
silently ignored `"reviewr"` would leave the review running on the implementing
model while the file on disk says otherwise. Writ would then be lying about
something it explicitly warns about, so a misspelled key fails loudly, naming
what it should have been.

**Writ writes it once, then leaves it alone.** `writ init` drops a starter config
into a new project, because a default nobody knows about is a default nobody
sets, and the setting most worth making — `reviewer` — is the easiest to never
think about. It names every field writ accepts, each one holding the value writ
would have used anyway, so a fresh config behaves exactly like no config at all
and editing it is a matter of changing a value rather than finding out what can
be set. Where writ's default is not a value — an unset reviewer, which means the
implementing agent — the field is `null`, and one comment line says what that
resolves to. After that the file is yours: nothing writ does will reformat it,
edit it, or overwrite it, and `init --force` resets project state while keeping
it, since how this project runs agents is as true of the next plan as the last.

One table, `FIELDS`, is the whole schema: it validates the file, generates the
starter one, and writes the comments that explain it. `DEFAULTS` maps each
command's arguments onto paths in it. Nothing is stated twice, so a field cannot
be accepted but undocumented, documented but unwritten, or written with a default
that is not the one writ actually uses.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .state import WritError, store_dir

CONFIG_FILENAME = "config.json"


def config_file(root: str | Path) -> Path:
    return store_dir(root) / CONFIG_FILENAME


#: The wall clock a planning, analysis, critique or review agent gets.
#:
#: There is no equivalent for `writ run` and `writ dispatch`: an implementation
#: agent runs unbounded unless someone says otherwise, because the honest timeout
#: for "implement this task" depends on the task, and a wrong guess kills work that
#: was going fine. Planning, analysis, critique and review are one bounded question
#: each, so a ceiling there is a safety net rather than a guess.
AGENT_TIMEOUT = 1800

#: `--level`: the heading depth `--extract` treats as a milestone.
DEFAULT_LEVEL = 2

#: `--interval`: seconds between redraws under `writ status --watch`.
DEFAULT_INTERVAL = 2.0

#: `--max-rounds`: how many repairs may land on a plan before it stops for a human.
#:
#: Read from `repair.py` rather than restated, because the same bound governs the
#: gate loop and the pre-execution one, and two numbers that had to agree would
#: eventually not.
def _max_repair_rounds() -> int:
    from .repair import DEFAULT_MAX_REPAIR_ROUNDS

    return DEFAULT_MAX_REPAIR_ROUNDS


MAX_REPAIR_ROUNDS = _max_repair_rounds()

#: Sentinel: derive this field's written default from `DEFAULTS`.
#:
#: Most fields have exactly one builtin, stated once in the `Default` that backs
#: an argument, and copying it into the field table would be the drift this module
#: exists to prevent. `DERIVED` says "read it from there"; a field only states its
#: own default when the commands disagree about it, which happens where one command
#: falls back to another role's setting rather than to a value.
DERIVED = object()


@dataclass(frozen=True)
class Field:
    """One setting: how it is checked, what it means, and what writ does without it.

    `kind` drives validation. `doc` is the line written into the generated file.
    `written` is the value that file holds, normally `DERIVED`.
    """

    kind: str
    doc: str
    #: the flag this stands in for, for the generated comment to name
    flag: str = ""
    #: what the starter config holds; `DERIVED` reads it from `DEFAULTS`
    written: Any = DERIVED
    #: the allowed values, for `kind="choice"`; a callable, so it can be read from
    #: the module that owns the list rather than duplicated here
    choices: Any = None
    #: `True` when the flag is the negative of the setting — `serve.open` is
    #: configured positively and reaches argparse as `no_open`
    invert: bool = False


def _orders() -> tuple[str, ...]:
    from .orchestrator import ORDERS

    return ORDERS


def _critic_names() -> tuple[str, ...]:
    from .critics import CRITICS

    return tuple(critic.name for critic in CRITICS)


#: the agent roles a project can configure, and what each one does.
#:
#: Five. Gates and repair planners still have no entry of their own: they run on
#: the `implementer` setting, because `_prepare` in orchestrator.py branches only
#: on `reviewer`. That is worth knowing and arguably worth changing — a gate is a
#: review by definition, and the agent that wrote the code is the wrong one to ask
#: whether the milestone holds — but it is what writ does today, and a config key
#: that claimed otherwise would be the same lie as a silently ignored typo.
ROLES: dict[str, str] = {
    "planner": "turns the design document into a plan (writ plan)",
    "critic": "reads the committed plan and reports findings (writ critique)",
    "stage": "runs one analysis of the staged pipeline (writ plan)",
    "implementer": "writes the code for one task, and runs gates and repairs",
    "reviewer": "independently checks a completed task (writ review)",
}

#: the keys one role may set
ROLE_KEYS = ("command", "model", "timeout")

#: one line per role: what it is answerable for, and what an unset one falls to.
#:
#: One line, not one per field. `command`, `model` and `timeout` mean the same
#: thing in all five, so spelling that out five times buries the part that
#: differs — which is what each role is for, and what writ does without it.
ROLE_DOCS: dict[str, str] = {
    "planner": "turns the design doc into the plan (writ plan)",
    "critic": "reports findings against the plan (writ critique). null: the planner",
    "stage": "runs the requirements, inventory and verification analyses"
    " (@STAGE_FLAG@). null: the planner's own setting",
    "implementer": "writes a task's code, and runs gates and repairs (writ run,"
    " writ dispatch). null timeout: no limit",
    "reviewer": "checks a finished task (writ review, writ run). null: the"
    " implementer, so the model that wrote the code reviews it",
}


def _role_fields() -> dict[str, Field]:
    """Every `agents.<role>.<key>`, since all five roles take the same three keys.

    Written out by loop rather than by hand: five roles times three keys is fifteen
    near-identical entries, and the one that got a stale `doc` pasted into it would
    read exactly like the others. The roles are documented per role in `ROLE_DOCS`;
    these entries carry only the validation.
    """
    #: The fields whose default is another role's setting rather than a value, so
    #: the starter config writes null and `ROLE_DOCS` says what that resolves to.
    #:
    #: Exactly the three the commands disagree about: `writ review` falls back to
    #: `pi` and its own timeout ceiling, while under `writ run` an unset reviewer is
    #: the implementing agent with the implementer's timeout. Every other field is
    #: derived, and `_written_default` raises if one of those turns out to be
    #: ambiguous too — this set is the list of known ambiguities, not a way to avoid
    #: thinking about new ones.
    deferred = {
        "agents.critic.command",
        "agents.reviewer.command",
        "agents.reviewer.timeout",
    }
    out: dict[str, Field] = {}
    for role in ROLES:
        for key, kind in (
            ("command", "command"),
            ("model", "text"),
            ("timeout", "count"),
        ):
            path = f"agents.{role}.{key}"
            out[path] = Field(
                kind=kind,
                doc="",  # documented per role, in ROLE_DOCS
                written=None if path in deferred else DERIVED,
            )
    return out


#: Every field the config accepts, in the order it is written, and what it means.
#:
#: This table is the schema. `validate` walks it, `default_document` generates from
#: it, and the comment block in the generated file is built out of the `doc` lines,
#: so a field cannot be accepted but undocumented or documented but unwritten.
#:
#: `@default@` and `@flag@` in a doc line are filled from the field itself, and the
#: named tokens from the code that owns them, so a written default cannot drift from
#: the real one.
FIELDS: dict[str, Field] = {
    **_role_fields(),
    # ------------------------------------------------------------------ run
    "run.parallel": Field(
        kind="count",
        flag="--parallel",
        doc="tasks worked at once (@flag@, default @default@); dependencies"
        " still bound it",
    ),
    "run.order": Field(
        kind="choice",
        flag="--order",
        choices=_orders,
        doc="which ready task goes first (@flag@): @ORDERS@",
    ),
    "run.max_rework": Field(
        kind="tally",
        flag="--max-rework",
        doc="times a rejected task goes back before writ leaves it for a human"
        " (@flag@); 0 fails on the first rejection",
    ),
    "run.max_tasks": Field(
        kind="count",
        flag="--max-tasks",
        doc="stop after starting this many tasks (@flag@); null walks the whole"
        " graph, and reviews of started tasks still finish",
    ),
    "run.stream": Field(
        kind="flag",
        flag="--no-stream",
        invert=True,
        doc="mirror each agent's own output to the terminal as it arrives, tagged"
        " with the task and role it came from (@flag@ sets this false). The"
        " transcript is written to disk either way; this is about whether a long"
        " run looks alive while it works",
    ),
    # ----------------------------------------------------------------- plan
    "plan.stages": Field(
        kind="flag",
        flag="--stages/--no-stages",
        doc="run the staged pipeline — @STAGES@ — before a synthesis agent"
        " decomposes the work (@flag@). false is the older single-shot planner,"
        " which makes every one of those judgements in one response",
    ),
    "plan.gates": Field(
        kind="flag",
        flag="--gates/--no-gates",
        doc="add a review gate per milestone and a final gate over the plan"
        " (@flag@). A gate judges integrated work against the requirements",
    ),
    "plan.chain": Field(
        kind="flag",
        flag="--chain",
        doc="order tasks the plan left independent, each after the last (@flag@)."
        " false leaves an omitted dependency omitted, so a plan that forgot an"
        " edge fails visibly instead of running in plan order",
    ),
    "plan.critics": Field(
        kind="flag",
        flag="--critics",
        doc="have the critics read the plan straight after committing it"
        " (@flag@). Costs an agent run each, which is why writ does not do it"
        " unasked; which critics run is @CRITICS_PATH@",
    ),
    "plan.parallel_stages": Field(
        kind="flag",
        flag="--parallel-stages",
        doc="run the analysis stages that need nothing from each other at once"
        " (@flag@) — requirements beside inventory. Buys a stage's wall-clock"
        " and costs the inventory its coverage claims, which need requirement"
        " ids it does not have yet",
    ),
    "plan.parallel_critics": Field(
        kind="flag",
        flag="--parallel-critics",
        doc="run the critics that only read the repository at once (@flag@),"
        " each critic that runs the project's commands alone. The interference"
        " writ avoids by running them one at a time comes from the commands,"
        " not the reading",
    ),
    "critique.parallel": Field(
        kind="flag",
        flag="--parallel-critics",
        doc="the same for writ critique (@flag@)",
    ),
    "plan.repair": Field(
        kind="flag",
        flag="--repair",
        doc="answer the plan's blocking findings with the bounded repair loop"
        " before approval (@flag@), the loop writ adjudicate runs. Costs agent"
        " runs, so writ does not do it unasked; the bound is"
        " @MAX_ROUNDS@",
    ),
    "plan.auto_approve": Field(
        kind="flag",
        flag="--auto-approve",
        doc="approve a plan nothing blocking stands against, with no human"
        " (@flag@). A clean check means writ proved nothing wrong, not that"
        " anyone read it; blocking findings are never overridden this way."
        " Judged after the critics and @REPAIR_PATH@ have had their say, so"
        " what it approves is the plan as everything that read it left it",
    ),
    "plan.refresh": Field(
        kind="flag",
        flag="--refresh",
        doc="re-run pipeline stages that already have an artifact instead of"
        " reusing them (@flag@)",
    ),
    "plan.instructions": Field(
        kind="text",
        flag="--instructions",
        doc="standing guidance for the planning agent — scope, priorities,"
        " constraints this project always wants honoured (@flag@)",
    ),
    "plan.extract": Field(
        kind="flag",
        flag="--extract",
        doc="skip the agent and derive tasks from headings and gate markers"
        " alone (@flag@)",
    ),
    "plan.level": Field(
        kind="count",
        flag="--level",
        doc="heading level that becomes a milestone under @EXTRACT_FLAG@"
        " (@flag@, default @default@)",
    ),
    "plan.flat": Field(
        kind="flag",
        flag="--flat",
        doc="one task per milestone under @EXTRACT_FLAG@, rather than splitting"
        " sub-sections (@flag@)",
    ),
    # ------------------------------------------------------------- critique
    "critique.critics": Field(
        kind="names",
        flag="--critics",
        choices=_critic_names,
        doc="which critics run, of @CRITICS@ (@flag@). null is all of them."
        " @PLAN_CRITICS@ decides whether writ plan runs them at all",
    ),
    # ---------------------------------------------------------- adjudicate
    "adjudicate.max_rounds": Field(
        # `tally`, not `count`: 0 is meaningful and means adjudicate nothing, which
        # is how a project turns the loop off without turning the command off.
        kind="tally",
        flag="--max-rounds",
        doc="how many repairs may land on a plan before it stops for a human"
        " (@flag@). A finding still open past this needs a decision, not a patch",
    ),
    "adjudicate.critics": Field(
        kind="flag",
        flag="--no-critics",
        # Configured as a person would say it and reaching argparse as `no_critics`,
        # so the value has to be flipped on the way. See `_for_args`.
        invert=True,
        doc="re-run the critics after each applied patch (@flag@ turns it off)."
        " Off is cheaper and blind to whatever only a critic sees",
    ),
    # ------------------------------------------------------------- dispatch
    "dispatch.detach": Field(
        kind="flag",
        flag="--detach",
        doc="run a dispatched agent in the background under a supervisor (@flag@)",
    ),
    # ---------------------------------------------------------------- check
    "check.all": Field(
        kind="flag",
        flag="--all",
        doc="include findings already accepted or resolved (@flag@)",
    ),
    # ------------------------------------------------------------- coverage
    "coverage.uncovered": Field(
        kind="flag",
        flag="--uncovered",
        doc="show only requirements nothing stands behind (@flag@)",
    ),
    # --------------------------------------------------------------- status
    "status.watch": Field(
        kind="flag",
        flag="--watch",
        doc="redraw until interrupted, or until no run is active (@flag@)",
    ),
    "status.interval": Field(
        kind="interval",
        flag="--interval",
        doc="seconds between redraws under @WATCH_FLAG@ (@flag@, default"
        " @default@)",
    ),
    "status.until_idle": Field(
        kind="flag",
        flag="--until-idle",
        doc="under @WATCH_FLAG@, exit once nothing is running (@flag@)",
    ),
    "status.clear": Field(
        kind="flag",
        flag="--no-clear",
        invert=True,
        doc="under @WATCH_FLAG@, clear the screen between redraws (@flag@ sets"
        " this false)",
    ),
    # ----------------------------------------------------------------- list
    "list.limit": Field(
        kind="count",
        flag="--limit",
        doc="show at most this many rows (@flag@); null shows all of them",
    ),
    # ----------------------------------------------------------------- show
    "show.verbose": Field(
        kind="flag",
        flag="--verbose",
        doc="for a milestone, expand every member task in full (@flag@)",
    ),
    "show.prompt": Field(
        kind="flag",
        flag="--prompt",
        doc="for a run, print the prompt it was given (@flag@)",
    ),
    # ---------------------------------------------------------------- graph
    "graph.levels": Field(
        kind="flag",
        flag="--levels",
        doc="group by dependency depth: what could run at the same time (@flag@)",
    ),
    "graph.verbose": Field(
        kind="flag",
        flag="--verbose",
        doc="add status and acceptance counts to each node (@flag@)",
    ),
    "graph.dot": Field(
        kind="flag",
        flag="--dot",
        doc="emit graphviz dot rather than text (@flag@)",
    ),
    # ----------------------------------------------------------------- logs
    "logs.follow": Field(
        kind="flag",
        flag="--follow",
        doc="stream a run's output until it ends (@flag@)",
    ),
    "logs.stderr": Field(
        kind="flag",
        flag="--stderr",
        doc="show stderr instead of stdout (@flag@)",
    ),
    "logs.tail": Field(
        kind="count",
        flag="--tail",
        doc="print only the last N lines (@flag@); null prints all of them",
    ),
    # ---------------------------------------------------------------- serve
    "serve.port": Field(
        kind="port",
        flag="--port",
        doc="port the web view listens on (@flag@, default @default@)",
    ),
    "serve.host": Field(
        kind="text",
        flag="--host",
        doc="interface to bind (@flag@, default @default@, this machine only)."
        " The page has no authentication and needs none while nothing else can"
        " reach it, so widen this only if you have read that sentence",
    ),
    "serve.open": Field(
        kind="flag",
        flag="--no-open",
        invert=True,
        doc="open a browser rather than printing the url (@flag@ sets this false)",
    ),
    # --------------------------------------------------------------- common
    "common.cwd": Field(
        kind="text",
        flag="--cwd",
        doc="working directory for every agent (@flag@); null is the project root",
    ),
    "common.quiet": Field(
        kind="flag",
        flag="--quiet",
        doc="do not mirror agent output to the terminal (@flag@). The run is still"
        " recorded in full, and `writ logs` still prints it",
    ),
    "common.json": Field(
        kind="flag",
        flag="--json",
        doc="machine-readable output wherever a command supports it (@flag@)",
    ),
    "common.dry_run": Field(
        kind="flag",
        flag="--dry-run",
        doc="print what would be done — the prompt, or the intended walk — and"
        " write nothing (@flag@)",
    ),
}


#: the sections of the file, in written order, derived from the field table
SECTIONS: tuple[str, ...] = tuple(
    dict.fromkeys(path.split(".")[0] for path in FIELDS)
)

#: run settings mapped to the flag each one stands in for
RUN_KEYS: dict[str, str] = {
    path.split(".", 1)[1]: entry.flag
    for path, entry in FIELDS.items()
    if path.startswith("run.")
}


# --------------------------------------------------------------------------
# applying it to parsed arguments


@dataclass(frozen=True)
class Default:
    """Where one argument's value comes from when the flag is absent."""

    #: dotted path into the config, e.g. `agents.reviewer.command`
    path: str
    #: what writ uses when the config is silent too
    builtin: Any = None


def _builtin_order() -> str:
    from .orchestrator import DEFAULT_ORDER

    return DEFAULT_ORDER


def _builtin_rework() -> int:
    from .model import DEFAULT_MAX_REWORK

    return DEFAULT_MAX_REWORK


def _builtin_port() -> int:
    from .server import DEFAULT_PORT

    return DEFAULT_PORT


def _builtin_host() -> str:
    from .server import DEFAULT_HOST

    return DEFAULT_HOST


#: fallbacks that live in another module, so they are read rather than copied
LATE_BUILTINS = {
    "order": _builtin_order,
    "max_rework": _builtin_rework,
    "port": _builtin_port,
    "host": _builtin_host,
}


def _resolved_builtin(attribute: str, default: Default) -> Any:
    """One argument's builtin, including the ones read from another module."""
    builtin = default.builtin
    if builtin is None and attribute in LATE_BUILTINS:
        return LATE_BUILTINS[attribute]()
    return builtin


#: Arguments almost every command shares, and the paths behind them.
#:
#: Merged into each command below rather than repeated eighteen times. `apply`
#: skips an attribute the parser did not define, so a command without `--dry-run`
#: is unaffected by its presence here.
COMMON: dict[str, Default] = {
    "cwd": Default("common.cwd"),
    "quiet": Default("common.quiet", False),
    "json": Default("common.json", False),
    "dry_run": Default("common.dry_run", False),
}


#: for each subcommand, which config entry stands behind each of its arguments.
#:
#: Written out per command rather than inferred, because the same flag name means
#: a different role depending on the verb: `--agent` is the planner under `writ
#: plan`, a critic under `writ critique`, the reviewer under `writ review`, and
#: the implementer under `writ run`. A rule that mapped `--agent` to one role
#: would quietly give three of those four the wrong default.
DEFAULTS: dict[str, dict[str, Default]] = {
    command: {**COMMON, **entries}
    for command, entries in {
        "init": {},
        "plan": {
            "agent": Default("agents.planner.command", "pi"),
            "model": Default("agents.planner.model"),
            "timeout": Default("agents.planner.timeout", AGENT_TIMEOUT),
            "critic_agent": Default("agents.critic.command"),
            "critic_model": Default("agents.critic.model"),
            "stage_agent": Default("agents.stage.command"),
            "stage_model": Default("agents.stage.model"),
            "stage_timeout": Default("agents.stage.timeout"),
            "instructions": Default("plan.instructions"),
            "extract": Default("plan.extract", False),
            "level": Default("plan.level", DEFAULT_LEVEL),
            "flat": Default("plan.flat", False),
            "chain": Default("plan.chain", False),
            "gates": Default("plan.gates", True),
            "stages": Default("plan.stages", True),
            "refresh": Default("plan.refresh", False),
            "parallel_stages": Default("plan.parallel_stages", False),
            "parallel_critics": Default("plan.parallel_critics", False),
            "repair": Default("plan.repair", False),
            "max_rounds": Default("adjudicate.max_rounds", MAX_REPAIR_ROUNDS),
            # The adjudicator is the critics' kind of work, so it follows their
            # agent rather than the planner's — the same choice `writ adjudicate`
            # makes, read from the same place.
            "adjudicator_agent": Default("agents.critic.command"),
            "adjudicator_model": Default("agents.critic.model"),
            "auto_approve": Default("plan.auto_approve", False),
            "critics": Default("plan.critics", False),
            # not a flag: a slot the config fills, so `plan.critics: true` can run
            # the set `critique.critics` names without restating it here
            "critic_names": Default("critique.critics"),
        },
        "critique": {
            "agent": Default("agents.critic.command", "pi"),
            "model": Default("agents.critic.model"),
            "timeout": Default("agents.critic.timeout", AGENT_TIMEOUT),
            "critics": Default("critique.critics"),
            "parallel_critics": Default("critique.parallel", False),
        },
        "adjudicate": {
            # The adjudicator runs on the critic's agent, not the planner's. It is
            # doing the critics' kind of work — reading a plan against findings —
            # and a project that pointed the critics at a stronger model meant that
            # for this too.
            "agent": Default("agents.critic.command", "pi"),
            "model": Default("agents.critic.model"),
            "timeout": Default("agents.critic.timeout", AGENT_TIMEOUT),
            "max_rounds": Default("adjudicate.max_rounds", MAX_REPAIR_ROUNDS),
            "critic_agent": Default("agents.critic.command"),
            "critic_model": Default("agents.critic.model"),
            "critics": Default("critique.critics"),
            "no_critics": Default("adjudicate.critics", True),
            # The re-review after a patch is the same five agents `writ plan` runs,
            # so it follows the same setting. Without this the flag parsed and was
            # then always false: a project that had asked for parallel critics got
            # them on the first pass and one-at-a-time on every re-read, which is
            # the slower half of the loop and the half nobody was watching.
            "parallel_critics": Default("critique.parallel", False),
        },
        "check": {"all": Default("check.all", False)},
        "coverage": {"uncovered": Default("coverage.uncovered", False)},
        "status": {
            "watch": Default("status.watch", False),
            "interval": Default("status.interval", DEFAULT_INTERVAL),
            "until_idle": Default("status.until_idle", False),
            "no_clear": Default("status.clear", True),
        },
        "list": {"limit": Default("list.limit")},
        "show": {
            "verbose": Default("show.verbose", False),
            "prompt": Default("show.prompt", False),
        },
        "graph": {
            "levels": Default("graph.levels", False),
            "verbose": Default("graph.verbose", False),
            "dot": Default("graph.dot", False),
        },
        "logs": {
            "follow": Default("logs.follow", False),
            "stderr": Default("logs.stderr", False),
            "tail": Default("logs.tail"),
        },
        "serve": {
            "port": Default("serve.port"),
            "host": Default("serve.host"),
            "no_open": Default("serve.open", True),
        },
        "dispatch": {
            "agent": Default("agents.implementer.command", "pi"),
            "model": Default("agents.implementer.model"),
            "timeout": Default("agents.implementer.timeout"),
            "detach": Default("dispatch.detach", False),
        },
        "review": {
            "agent": Default("agents.reviewer.command", "pi"),
            "model": Default("agents.reviewer.model"),
            "timeout": Default("agents.reviewer.timeout", AGENT_TIMEOUT),
            # `writ review` enforces the same budget `writ run` does, so one setting
            # backs both rather than a project meaning it in only one of them.
            "max_rework": Default("run.max_rework"),
        },
        "run": {
            "agent": Default("agents.implementer.command", "pi"),
            "model": Default("agents.implementer.model"),
            "timeout": Default("agents.implementer.timeout"),
            "reviewer": Default("agents.reviewer.command"),
            "reviewer_model": Default("agents.reviewer.model"),
            "reviewer_timeout": Default("agents.reviewer.timeout"),
            "parallel": Default("run.parallel", 1),
            "order": Default("run.order"),
            "max_rework": Default("run.max_rework"),
            "max_tasks": Default("run.max_tasks"),
            "no_stream": Default("run.stream", True),
        },
    }.items()
}

#: where a resolved value came from, for `writ agents` to report
FROM_FLAG = "flag"
FROM_CONFIG = "config"
FROM_BUILTIN = "default"

#: which argparse attribute is the negative of its setting, by path
INVERTED: dict[str, bool] = {
    path: entry.invert for path, entry in FIELDS.items() if entry.invert
}


def _written_default(path: str) -> Any:
    """What the starter config holds for one field.

    `DERIVED` means "whatever the code already says", read out of `DEFAULTS` so the
    generated file cannot disagree with the resolution it documents. Two commands
    claiming different builtins for one path is a real ambiguity — `writ review`
    falls back to `pi`, `writ run` falls back to the implementing agent — and the
    field has to say so explicitly rather than have one of them picked silently.
    """
    entry = FIELDS[path]
    if entry.written is not DERIVED:
        return entry.written
    seen = {
        _resolved_builtin(attribute, default)
        for command in DEFAULTS.values()
        for attribute, default in command.items()
        if default.path == path
    }
    if len(seen) > 1:
        raise AssertionError(
            f"{path}: commands disagree about the default ({sorted(map(repr, seen))});"
            " state it on the Field so the generated config is not guessing"
        )
    return seen.pop() if seen else None


#: every field of the config, in the order it is written, and its default.
#:
#: `None` means writ has no value there, which is not the same as having no
#: default: an unset reviewer command resolves to the implementing agent. The
#: comment block says which, since only a comment can.
DEFAULT_VALUES: dict[str, Any] = {path: _written_default(path) for path in FIELDS}


# --------------------------------------------------------------------------
# reading and checking it


def _schema() -> dict[str, Any]:
    """The field table as the nested shape of the document it describes."""
    tree: dict[str, Any] = {}
    for path in FIELDS:
        node = tree
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = FIELDS[path]
    return tree


#: what to call an unknown key at each depth, so the error names the right noun
NOUNS = ("section", "role", "key")


def load(root: str | Path) -> dict[str, Any]:
    """Read and validate the project's config. An absent file is an empty one.

    Absent is not an error: a project that has never needed a config should not
    have to have one. A *malformed* config is an error, because the alternative is
    running with defaults the file on disk contradicts.
    """
    path = config_file(root)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise WritError(f"{path}: not valid JSON ({exc})") from exc
    return validate(raw, where=str(path))


def validate(raw: Any, *, where: str = CONFIG_FILENAME) -> dict[str, Any]:
    """Check a config document, naming the offending key.

    Deliberately strict about names. A value writ does not recognise is not a
    value writ can honour, so reporting it is the only honest option — the same
    reason a plan with an unknown field is rejected rather than trimmed.

    Walks `FIELDS` rather than checking each section by hand, so a field added to
    that table is validated by having been added to it.
    """
    if not isinstance(raw, dict):
        raise WritError(f"{where}: expected a JSON object")
    return _node(raw, _schema(), where, depth=0)


def _node(raw: Any, schema: dict[str, Any], where: str, *, depth: int) -> dict[str, Any]:
    """One level of the document, against one level of the schema."""
    if not isinstance(raw, dict):
        raise WritError(f"{where}: {_shape(schema)}")
    _reject_unknown(raw, tuple(schema), where, NOUNS[min(depth, len(NOUNS) - 1)])
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if is_comment(key):
            continue
        entry = schema[key]
        at = f"{where}.{key}" if depth else f"{where}: {key}"
        if isinstance(entry, dict):
            out[key] = _node(value, entry, at, depth=depth + 1)
            continue
        # `null` is how a field says "writ's default", which for some of these is
        # not a value at all — an unset reviewer timeout means no timeout, and an
        # unset reviewer command means the implementer's. That is what lets the
        # config `writ init` writes name every field instead of only the ones with
        # a value to name, and it is the same as leaving the key out.
        if value is None:
            continue
        out[key] = _value(value, entry, at)
    return out


def _shape(schema: dict[str, Any]) -> str:
    """What this level should have looked like, in its own terms.

    Built from the schema rather than written out, so the example names keys that
    actually exist at the level that was got wrong.
    """
    keys = [key for key in schema if not isinstance(schema[key], dict)]
    if keys == list(ROLE_KEYS):
        return 'expected an object like {"command": "claude", "model": "opus"}'
    if keys:
        return f"expected an object of setting -> value ({', '.join(keys)})"
    return f"expected an object of {' or '.join(schema) if schema else 'settings'}"


#: how each kind of value is checked
def _value(value: Any, entry: Field, where: str) -> Any:
    checker = _CHECKS[entry.kind]
    return checker(value, where, entry)


def _command(value: Any, where: str, entry: Field) -> str:
    text = _text(value, where, entry)
    if not text:
        raise WritError(
            f'{where}: expected an agent command like "claude" or '
            '"codex exec", not an empty string'
        )
    return text


def _text(value: Any, where: str, entry: Field) -> str:
    if not isinstance(value, str):
        raise WritError(f"{where}: expected a string (got {type(value).__name__})")
    return value.strip()


def _count(value: Any, where: str, entry: Field) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise WritError(f"{where}: expected a whole number above 0 (got {value!r})")
    return value


def _tally(value: Any, where: str, entry: Field) -> int:
    # Zero is meaningful here — it is `--max-rework 0`, fail on first rejection —
    # so this one is not `_count`.
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise WritError(
            f"{where}: expected a whole number of attempts, 0 or more "
            f"(got {value!r})"
        )
    return value


def _port(value: Any, where: str, entry: Field) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
        raise WritError(f"{where}: expected a port from 1 to 65535 (got {value!r})")
    return value


def _interval(value: Any, where: str, entry: Field) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise WritError(f"{where}: expected a number of seconds above 0 (got {value!r})")
    return float(value)


def _flag(value: Any, where: str, entry: Field) -> bool:
    if not isinstance(value, bool):
        raise WritError(f"{where}: expected true or false (got {value!r})")
    return value


def _choice(value: Any, where: str, entry: Field) -> str:
    allowed = entry.choices()
    text = _text(value, where, entry)
    if text not in allowed:
        noun = where.rsplit(".", 1)[-1]
        raise WritError(
            f"{where}: {text!r} is not an {noun}; choose from {', '.join(allowed)}"
        )
    return text


def _names(value: Any, where: str, entry: Field) -> list[str]:
    """A list of names from a fixed set, like which critics to run.

    An empty list is kept rather than treated as absent: `"critics": []` is a
    project saying "none", which is not the same as saying nothing.
    """
    allowed = entry.choices()
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise WritError(
            f"{where}: expected a list of names, like "
            f'["{allowed[0]}"] (got {type(value).__name__})'
        )
    out = []
    for index, item in enumerate(value):
        name = _text(item, f"{where}[{index}]", entry)
        if name not in allowed:
            detail = f"{where}[{index}]: {name!r} is not one writ knows"
            if match := _closest(name, tuple(allowed)):
                detail = f"{where}[{index}]: {name!r} (did you mean {match!r}?)"
            raise WritError(f"{detail}; known: {', '.join(allowed)}")
        out.append(name)
    return out


_CHECKS = {
    "command": _command,
    "text": _text,
    "count": _count,
    "tally": _tally,
    "port": _port,
    "interval": _interval,
    "flag": _flag,
    "choice": _choice,
    "names": _names,
}


def is_comment(key: str) -> bool:
    """Whether a key is a note to a human rather than a setting.

    JSON has nowhere to put a comment, and this file is written by hand and read
    later by whoever inherits the project — "codex here because it caught the
    seam bugs claude missed" is worth more than the setting it annotates. So any
    key beginning with `_` is ignored at every level, which also means the shipped
    `config.example.json` can explain itself and still be copied verbatim.
    """
    return key.startswith("_")


def _reject_unknown(
    mapping: dict[str, Any], allowed: tuple[str, ...], where: str, noun: str
) -> None:
    """Name a key writ does not know, and the nearest one it does.

    The suggestion matters more than the rejection. `"reviewr"` is a typo with a
    right answer three characters away, and a reader who is told only that it is
    unknown has to go and find the list.
    """
    unknown = [key for key in mapping if key not in allowed and not is_comment(key)]
    if not unknown:
        return
    named = ", ".join(repr(key) for key in sorted(unknown))
    detail = f"{where}: unknown {noun} {named}"
    close = [
        f"{key!r} (did you mean {match!r}?)"
        for key in sorted(unknown)
        if (match := _closest(key, allowed))
    ]
    if close:
        detail = f"{where}: unknown {noun} {', '.join(close)}"
    raise WritError(f"{detail}; known {noun}s: {', '.join(allowed)}")


def _closest(key: str, allowed: tuple[str, ...]) -> str | None:
    """The one allowed name a typo was probably reaching for, if there is one."""
    import difflib

    matches = difflib.get_close_matches(key, allowed, n=1, cutoff=0.7)
    return matches[0] if matches else None


# --------------------------------------------------------------------------
# writing the starter file


#: one line per section, naming what it governs, so the file's shape is legible
#: before any single field is read
SECTION_DOCS: dict[str, str] = {
    "agents": "who runs what. Each role takes a command, a model and a timeout in"
    " seconds, and each of the three falls back on its own",
    "run": "how `writ run` walks the graph",
    "plan": "what `writ plan` does before and after it commits a plan",
    "critique": "which critics read a committed plan (writ critique)",
    "adjudicate": "the bounded repair loop that answers a plan's findings before"
    " it executes (writ adjudicate)",
    "dispatch": "how one task is handed to an agent (writ dispatch)",
    "check": "what `writ check` reports",
    "coverage": "what `writ coverage` reports",
    "status": "what `writ status` shows, and how it follows",
    "list": "what `writ list` shows",
    "show": "what `writ show` shows",
    "graph": "how `writ graph` draws the DAG",
    "logs": "what `writ logs` prints",
    "serve": "the read-only web view (writ serve)",
    "common": "flags most commands share; each applies wherever that command"
    " accepts it",
}

#: kept as its own name, since it was one before every section had docs
RUN_DOCS: dict[str, str] = {
    path.split(".", 1)[1]: entry.doc
    for path, entry in FIELDS.items()
    if path.startswith("run.")
}

#: the prose above the lists
NOTE: tuple[str, ...] = (
    "Per-project defaults for writ. Every value below is writ's own default, so"
    " this file as written changes nothing; edit one and it becomes this"
    " project's default. A command-line flag still overrides anything here, and"
    " `writ agents` prints what is in effect.",
    "null means writ's default, the same as deleting the key — for some fields"
    " that is another role's setting rather than a value, noted below.",
    "Every flag that is a standing decision is here. The ones that are not: a"
    " design document, a task id, `--stage`, `--plan-id`, the filters on list and"
    " coverage — which as a default would leave writ reporting a subset of the"
    " project while looking like it reported all of it — and every `--force`,"
    " each of which overrides a check writ exists to make.",
    "Writ wrote this file once, at `writ init`, and will not touch it again:"
    " nothing here is reformatted or edited behind you, and `init --force`"
    " resets project state while leaving it alone. Any key starting with _ is a"
    " comment; an unknown key is refused rather than ignored, so a misspelled"
    " 'reviewer' fails loudly instead of leaving review on the model that wrote"
    " the code while this file claims otherwise.",
)

#: how wide the generated comment lines are allowed to get
STARTER_WIDTH = 88

#: how far every continuation and list line is indented past the left edge.
#:
#: One width for all of it, rather than each label carrying its own. Labels differ
#: in length, and indenting to `len(label)` would step the list items in and out
#: under each heading for no reason a reader benefits from.
INDENT = " " * 6


def _rendered(value: Any) -> str:
    """One written default, as it appears in a comment line."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _fill(text: str, path: str | None = None) -> str:
    """Substitute every documented default from the code that defines it.

    `@default@` and `@flag@` come from the field being documented; the named
    tokens from the module that owns the value. Nothing a comment states about
    writ's behaviour is typed into the comment.
    """
    from .analysis import STAGE_NAMES

    out = (
        text.replace("@PI@", str(DEFAULTS["plan"]["agent"].builtin))
        .replace("@AGENT_T@", str(AGENT_TIMEOUT))
        .replace("@ORDERS@", ", ".join(_orders()))
        .replace("@STAGES@", ", ".join(STAGE_NAMES))
        .replace("@CRITICS@", ", ".join(_critic_names()))
        .replace("@STAGE_FLAG@", "--stage-agent")
        .replace("@EXTRACT_FLAG@", FIELDS["plan.extract"].flag)
        .replace("@WATCH_FLAG@", FIELDS["status.watch"].flag)
        .replace("@CRITICS_PATH@", "critique.critics")
        .replace("@PLAN_CRITICS@", "plan.critics")
        .replace("@REPAIR_PATH@", "plan.repair")
        .replace("@MAX_ROUNDS@", "adjudicate.max_rounds")
    )
    if path:
        out = out.replace("@default@", _rendered(DEFAULT_VALUES[path])).replace(
            "@flag@", FIELDS[path].flag
        )
    return out


def _entries(docs: dict[str, str], paths: dict[str, str] | None = None) -> list[str]:
    """`name - what it does`, names padded so the descriptions line up."""
    import textwrap

    width = max(len(name) for name in docs)
    lines = []
    for name, purpose in docs.items():
        lead = f"{name:<{width}} - "
        lines.extend(
            textwrap.wrap(
                _fill(purpose, (paths or {}).get(name)),
                width=STARTER_WIDTH - len(INDENT),
                initial_indent=lead,
                subsequent_indent=" " * len(lead),
            )
        )
    return [f"{INDENT}{line}" for line in lines]


def _section_docs(section: str) -> tuple[dict[str, str], dict[str, str]]:
    """What to list under one section heading, and the path behind each entry.

    `agents` is documented per role rather than per field: `command`, `model` and
    `timeout` mean the same thing in all five roles, and repeating that fifteen
    times would bury the part that differs, which is what each role is for.
    """
    if section == "agents":
        return dict(ROLE_DOCS), {}
    docs, paths = {}, {}
    for path, entry in FIELDS.items():
        head, _, leaf = path.partition(".")
        if head == section:
            docs[leaf] = entry.doc
            paths[leaf] = path
    return docs, paths


def _comment_block() -> list[str]:
    """The whole `_` key: the prose, then one labelled list per section.

    Labels down the left edge with everything else indented under one of them, so
    the shape of the file is legible before any of it is read.
    """
    import textwrap

    lines: list[str] = []
    for paragraph in NOTE:
        wrapped = textwrap.wrap(_fill(paragraph), width=STARTER_WIDTH - len(INDENT))
        if lines:
            # a continuation paragraph: indented under Note, with no second label
            lines.append("")
            lines.extend(f"{INDENT}{line}" for line in wrapped)
            continue
        lines.append(f"Note: {wrapped[0]}")
        lines.extend(f"{INDENT}{line}" for line in wrapped[1:])
    for section in SECTIONS:
        docs, paths = _section_docs(section)
        heading = textwrap.wrap(
            f"{section} — {_fill(SECTION_DOCS[section])}", width=STARTER_WIDTH
        )
        lines.extend(["", *heading, *_entries(docs, paths)])
    return lines


def _defaults_tree() -> dict[str, Any]:
    """The field table as the nested document it describes."""
    tree: dict[str, Any] = {}
    for path, default in DEFAULT_VALUES.items():
        node = tree
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = default
    return tree


def default_document() -> str:
    """The starter config: writ's defaults, and one comment block explaining them.

    Generated from the tables above rather than typed out, so the file `writ init`
    leaves in a project cannot drift from what writ does — not the values in it,
    and not the defaults its comments describe. A config still explaining that an
    unset reviewer falls back to the implementing agent, after that had stopped
    being true, would be worse than no config at all.

    One comment block, at the top. A reader opening this file wants either the
    explanation or the values, and interleaving them puts whichever one they came
    for twice as far apart as it needs to be.
    """
    body = json.dumps(_defaults_tree(), indent=2, ensure_ascii=False)
    # spliced in rather than dumped with the rest, so the block keeps its line
    # breaks instead of becoming one unreadable string
    rendered = (
        "[\n"
        + ",\n".join(
            f"    {json.dumps(line, ensure_ascii=False)}" for line in _comment_block()
        )
        + "\n  ]"
    )
    return body.replace("{\n", '{\n  "_": ' + rendered + ",\n", 1) + "\n"


def ensure(root: str | Path) -> tuple[Path, bool]:
    """Write the starter config if the project has none. Never overwrites one.

    Returns the path and whether this call created it. The no-overwrite rule is
    not politeness: this file is hand-edited, holds the reason behind each choice
    in its comments, and is not recoverable from anything else in `.writ`. So
    `--force`, which resets state, deliberately stops short of it.
    """
    path = config_file(root)
    if path.exists():
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(default_document(), encoding="utf-8")
    return path, True


def apply(args: Any, loaded: dict[str, Any]) -> dict[str, tuple[str, Any]]:
    """Fill in every unset argument from the config, and say what came from where.

    Only `None` is filled, which is what makes "a flag always wins" true rather
    than aspirational: the parser leaves an unsupplied option as `None`, so an
    attribute still holding `None` here is one nobody asked about. That only works
    because the parser no longer carries the defaults itself — with
    `default="pi"`, an explicit `--agent pi` and an absent flag are the same value,
    and the config could not tell them apart to know whether to override. The same
    is true of every boolean: `action="store_true"` would make an absent `--quiet`
    indistinguishable from an explicit one, so those parse with `default=None` too
    and take their real default from here.

    Returns the provenance of each value, for `writ agents` to show.
    """
    wanted = DEFAULTS.get(getattr(args, "command", None) or "", {})
    decided: dict[str, tuple[str, Any]] = {}
    for attribute, default in wanted.items():
        if not hasattr(args, attribute):
            continue
        invert = INVERTED.get(default.path, False)
        if getattr(args, attribute) is not None:
            decided[attribute] = (FROM_FLAG, getattr(args, attribute))
            continue
        value = _lookup(loaded, default.path)
        if value is not None:
            setattr(args, attribute, _for_args(value, invert))
            decided[attribute] = (FROM_CONFIG, value)
            continue
        builtin = _resolved_builtin(attribute, default)
        if builtin is not None:
            setattr(args, attribute, _for_args(builtin, invert))
        decided[attribute] = (FROM_BUILTIN, builtin)
    return decided


def _for_args(value: Any, invert: bool) -> Any:
    """One resolved value, as the argparse attribute behind it spells things.

    Two settings are configured the way a person would say them and reach argparse
    as their opposite: `serve.open` arrives as `no_open`, `status.clear` as
    `no_clear`. Every value on the way to those attributes has to be flipped —
    including the builtin, which is stated positively in `DEFAULTS` for the same
    reason the config field is. Inverting only the config value is a bug that hides
    itself: a generated config, holding writ's own defaults, would then resolve to
    the opposite of having no config at all.
    """
    return (not value) if invert else value


def _lookup(loaded: dict[str, Any], path: str) -> Any:
    """Read an `agents.reviewer.command`-style path out of a loaded config."""
    node: Any = loaded
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    # `False` and `0` are answers; `""` and a missing key are not. A flag set false
    # in a config has to survive this, or every boolean would be unsettable.
    if node is None or node == "":
        return None
    return node
