"""Per-project defaults, so a standing choice is made once rather than on every command.

`.writ/config.yaml` holds the few settings a project actually changes: which
agent fills each role, whether `writ plan` runs the critics and the repair loop,
whether it approves on its own, how wide `writ run` goes, and the port `writ
serve` binds. Everything else a flag can say keeps writ's own default and is
said on the command line when it is wanted, rather than sitting in a file where
nobody reads it.

Three rules govern the file.

**A flag always wins.** The config is a default, not a policy. For a boolean to
be overridable both ways, the parser cannot carry the default itself: every flag
parses with `default=None` and gets its real default here, from `DEFAULTS`.

**Everything in it is validated, and an unknown key is an error.** A silently
ignored `reviewr` would leave review on the model that wrote the code while the
file claims otherwise, so a misspelled key fails loudly and names what it meant.

**Writ writes it once, then leaves it alone.** `writ init` writes every setting
at writ's own default with a comment on each, so a fresh config behaves exactly
like none. After that the file is yours: `init --force` resets state and keeps it.

`FIELDS` is the schema: it validates the file and generates the starter one.
`DEFAULTS` maps each command's arguments onto a builtin and, for the settings in
`FIELDS`, the path that can replace it. Nothing is stated twice.
"""
from __future__ import annotations

import json
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import yamlish
from .state import WritError, store_dir

CONFIG_FILENAME = "config.yaml"

#: what the file was called before it was YAML; `writ init` converts it
LEGACY_FILENAME = "config.json"


def config_file(root: str | Path) -> Path:
    return store_dir(root) / CONFIG_FILENAME


def legacy_file(root: str | Path) -> Path:
    return store_dir(root) / LEGACY_FILENAME


#: The wall clock a planning, critique or review agent gets. An implementation
#: agent runs unbounded unless someone says otherwise: the honest timeout for
#: "implement this task" depends on the task.
AGENT_TIMEOUT = 1800

#: `--level`: the heading depth `--extract` treats as a milestone.
DEFAULT_LEVEL = 2

#: `--interval`: seconds between redraws under `writ status --watch`.
DEFAULT_INTERVAL = 2.0


def _max_repair_rounds() -> int:
    from .repair import DEFAULT_MAX_REPAIR_ROUNDS

    return DEFAULT_MAX_REPAIR_ROUNDS


#: `--max-rounds`, read from `repair.py` so the two cannot disagree
MAX_REPAIR_ROUNDS = _max_repair_rounds()


@dataclass(frozen=True)
class Field:
    """One setting: how it is checked, and the comment written beside it."""

    kind: str
    doc: str = ""
    #: the value the starter file holds when it is not the builtin (`None` for a
    #: role that falls back to another role rather than to a value)
    written: Any = ...


#: the agent roles a project can configure, and what each one does
ROLES: dict[str, str] = {
    "planner": "turns the design document into a plan (writ plan)",
    "critic": "reads the plan and reports findings, and repairs it (writ critique,"
    " writ adjudicate)",
    "implementer": "writes the code for one task, and runs gates (writ run)",
    "reviewer": "independently checks a completed task (writ review, writ run)",
}

#: the keys one role may set
ROLE_KEYS = ("command", "model", "timeout")

#: what an unset role resolves to, for the comment beside it
ROLE_NOTES: dict[str, str] = {
    "critic": "null command: the planner's",
    "implementer": "null timeout: no limit",
    "reviewer": "null: the implementer, so the model that wrote the code reviews it",
}

#: the role fields whose starter value is null because commands disagree about
#: what unset means: `writ review` falls back to `pi`, `writ run` to the implementer
_DEFERRED = {
    "agents.critic.command",
    "agents.reviewer.command",
    "agents.reviewer.timeout",
}


def _role_fields() -> dict[str, Field]:
    out: dict[str, Field] = {}
    for role in ROLES:
        kinds = (("command", "command"), ("model", "text"), ("timeout", "count"))
        for key, kind in kinds:
            path = f"agents.{role}.{key}"
            out[path] = Field(kind=kind, written=None if path in _DEFERRED else ...)
    return out


#: Every setting the config accepts, in the order it is written.
FIELDS: dict[str, Field] = {
    **_role_fields(),
    "plan.critics": Field(
        kind="flag",
        doc="have the critics read the plan once it is committed (--critics)",
    ),
    "plan.repair": Field(
        kind="flag",
        doc="answer blocking findings with the repair loop before approval (--repair)",
    ),
    "plan.max_rounds": Field(
        # `tally`: 0 is meaningful, and means repair nothing
        kind="tally",
        doc="repair rounds before the plan stops for a human (--max-rounds)",
    ),
    "plan.auto_approve": Field(
        kind="flag",
        doc="approve a plan nothing blocking stands against (--auto-approve)",
    ),
    "plan.instructions": Field(
        kind="text",
        doc="standing guidance for the planning agent (--instructions)",
    ),
    "run.parallel": Field(kind="count", doc="tasks worked at once (--parallel)"),
    "run.max_rework": Field(
        kind="tally",
        doc="times a rejected task goes back before it waits for a human"
        " (--max-rework)",
    ),
    "serve.port": Field(kind="port", doc="port the web view listens on (--port)"),
}

#: the sections of the file, in written order
SECTIONS: tuple[str, ...] = tuple(dict.fromkeys(path.split(".")[0] for path in FIELDS))

SECTION_DOCS: dict[str, str] = {
    "agents": "who runs each role: the agent command, its model, and a timeout in"
    " seconds",
    "plan": "what `writ plan` does after it commits a plan",
    "run": "how `writ run` walks the graph",
    "serve": "the read-only web view",
}

NOTE = (
    "writ project config. Every value below is writ's own default, so this file as"
    " written changes nothing. A command-line flag overrides anything here, and"
    " `writ agents` prints what is in effect. null means writ's default. An"
    " unknown key is an error. Writ wrote this file at `writ init` and will not"
    " touch it again."
)


# --------------------------------------------------------------------------
# applying it to parsed arguments


@dataclass(frozen=True)
class Default:
    """Where one argument's value comes from when its flag is absent."""

    #: dotted path into the config, or `None` for a flag the config does not set
    path: str | None = None
    #: what writ uses when nothing else speaks
    builtin: Any = None
    #: `True` when the argument is the negative of its default (`no_open`)
    invert: bool = False


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
    if default.builtin is None and attribute in LATE_BUILTINS:
        return LATE_BUILTINS[attribute]()
    return default.builtin


#: arguments almost every command shares; `apply` skips any a parser lacks
COMMON: dict[str, Default] = {
    "quiet": Default(builtin=False),
    "json": Default(builtin=False),
    "dry_run": Default(builtin=False),
}

_PLANNER = {
    "agent": Default("agents.planner.command", "pi"),
    "model": Default("agents.planner.model"),
    "timeout": Default("agents.planner.timeout", AGENT_TIMEOUT),
}

#: The critics, and the adjudicator that answers them, share one role: it is the
#: same kind of work, reading a plan against findings.
_CRITIC = {
    "agent": Default("agents.critic.command", "pi"),
    "model": Default("agents.critic.model"),
    "timeout": Default("agents.critic.timeout", AGENT_TIMEOUT),
}

#: For each subcommand, the default behind each of its arguments. Written out per
#: command because one flag means a different role per verb: `--agent` is the
#: planner under `writ plan` and the reviewer under `writ review`.
DEFAULTS: dict[str, dict[str, Default]] = {
    command: {**COMMON, **entries}
    for command, entries in {
        "init": {},
        "plan": {
            **_PLANNER,
            "critic_agent": Default("agents.critic.command"),
            "critic_model": Default("agents.critic.model"),
            "adjudicator_agent": Default("agents.critic.command"),
            "adjudicator_model": Default("agents.critic.model"),
            "instructions": Default("plan.instructions"),
            "extract": Default(builtin=False),
            "level": Default(builtin=DEFAULT_LEVEL),
            "flat": Default(builtin=False),
            "chain": Default(builtin=False),
            "gates": Default(builtin=True),
            "stages": Default(builtin=True),
            "refresh": Default(builtin=False),
            "repair": Default("plan.repair", False),
            "max_rounds": Default("plan.max_rounds", MAX_REPAIR_ROUNDS),
            "auto_approve": Default("plan.auto_approve", False),
            "critics": Default("plan.critics", False),
        },
        "critique": {**_CRITIC},
        "adjudicate": {
            **_CRITIC,
            "max_rounds": Default("plan.max_rounds", MAX_REPAIR_ROUNDS),
            "critic_agent": Default("agents.critic.command"),
            "critic_model": Default("agents.critic.model"),
            "no_critics": Default(builtin=True, invert=True),
        },
        "check": {"all": Default(builtin=False)},
        "coverage": {"uncovered": Default(builtin=False)},
        "status": {
            "watch": Default(builtin=False),
            "interval": Default(builtin=DEFAULT_INTERVAL),
            "until_idle": Default(builtin=False),
            "no_clear": Default(builtin=True, invert=True),
        },
        "show": {"verbose": Default(builtin=False), "prompt": Default(builtin=False)},
        "graph": {
            "levels": Default(builtin=False),
            "verbose": Default(builtin=False),
            "dot": Default(builtin=False),
        },
        "logs": {"follow": Default(builtin=False), "stderr": Default(builtin=False)},
        "serve": {
            "port": Default("serve.port"),
            "host": Default(),
            "no_open": Default(builtin=True, invert=True),
        },
        "dispatch": {
            "agent": Default("agents.implementer.command", "pi"),
            "model": Default("agents.implementer.model"),
            "timeout": Default("agents.implementer.timeout"),
            "detach": Default(builtin=False),
        },
        "review": {
            "agent": Default("agents.reviewer.command", "pi"),
            "model": Default("agents.reviewer.model"),
            "timeout": Default("agents.reviewer.timeout", AGENT_TIMEOUT),
            # the same budget `writ run` enforces, so one setting backs both
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
            "order": Default(),
            "max_rework": Default("run.max_rework"),
            "no_stream": Default(builtin=True, invert=True),
        },
    }.items()
}

#: where a resolved value came from, for `writ agents` to report
FROM_FLAG = "flag"
FROM_CONFIG = "config"
FROM_BUILTIN = "default"


def _written_default(path: str) -> Any:
    """What the starter config holds for one field: the builtin behind it.

    Read out of `DEFAULTS` so the generated file cannot disagree with the
    resolution it documents. Two commands claiming different builtins for one
    path is an ambiguity the field has to settle with `written`.
    """
    entry = FIELDS[path]
    if entry.written is not ...:
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
            " state it on the Field"
        )
    return seen.pop() if seen else None


DEFAULT_VALUES: dict[str, Any] = {path: _written_default(path) for path in FIELDS}


# --------------------------------------------------------------------------
# reading and checking it


def _schema() -> dict[str, Any]:
    tree: dict[str, Any] = {}
    for path, entry in FIELDS.items():
        node = tree
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = entry
    return tree




def load(root: str | Path) -> dict[str, Any]:
    """Read and validate the project's config. An absent file is an empty one.

    A `config.json` from before the file was YAML is not read: most of its keys
    no longer exist, so `writ init` converts it rather than this guessing.
    """
    path = config_file(root)
    if not path.exists():
        if legacy_file(root).exists():
            raise WritError(
                f"{legacy_file(root)} is no longer read; run `writ init` to convert"
                f" it to {CONFIG_FILENAME}"
            )
        return {}
    raw = yamlish.loads(path.read_text(encoding="utf-8"), where=str(path))
    return validate(raw, where=str(path))


def validate(raw: Any, *, where: str = CONFIG_FILENAME) -> dict[str, Any]:
    """Check a config document, naming the offending key and what it meant."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise WritError(f"{where}: expected sections like `agents:` and `plan:`")
    return _node(raw, _schema(), where, path=())


def _noun(path: tuple[str, ...]) -> str:
    """What to call an unknown key here, so the error names the right thing."""
    if not path:
        return "section"
    return "role" if path == ("agents",) else "key"


def _node(
    raw: Any, schema: dict[str, Any], where: str, *, path: tuple[str, ...]
) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        keys = ", ".join(schema)
        raise WritError(f"{where}: expected a mapping of {keys}")
    _reject_unknown(raw, tuple(schema), where, _noun(path))
    out: dict[str, Any] = {}
    for key, value in raw.items():
        entry = schema[key]
        at = f"{where}.{key}" if path else f"{where}: {key}"
        if isinstance(entry, dict):
            out[key] = _node(value, entry, at, path=(*path, key))
        elif value is not None:
            # null is writ's default, the same as leaving the key out
            out[key] = _CHECKS[entry.kind](value, at)
    return out


def _command(value: Any, where: str) -> str:
    text = _text(value, where)
    if not text:
        raise WritError(
            f'{where}: expected an agent command like "claude" or "codex exec",'
            " not an empty string"
        )
    return text


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise WritError(f"{where}: expected a string (got {type(value).__name__})")
    return value.strip()


def _count(value: Any, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise WritError(f"{where}: expected a whole number above 0 (got {value!r})")
    return value


def _tally(value: Any, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise WritError(f"{where}: expected a whole number, 0 or more (got {value!r})")
    return value


def _port(value: Any, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
        raise WritError(f"{where}: expected a port from 1 to 65535 (got {value!r})")
    return value


def _flag(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise WritError(f"{where}: expected true or false (got {value!r})")
    return value


_CHECKS = {
    "command": _command,
    "text": _text,
    "count": _count,
    "tally": _tally,
    "port": _port,
    "flag": _flag,
}


def _reject_unknown(
    mapping: dict[str, Any], allowed: tuple[str, ...], where: str, noun: str
) -> None:
    """Name a key writ does not know, and the nearest one it does."""
    unknown = [key for key in mapping if key not in allowed]
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
    import difflib

    matches = difflib.get_close_matches(key, allowed, n=1, cutoff=0.7)
    return matches[0] if matches else None


# --------------------------------------------------------------------------
# writing the starter file

STARTER_WIDTH = 80


def _comment(text: str, indent: str = "") -> list[str]:
    return [
        f"{indent}# {line}"
        for line in textwrap.wrap(text, width=STARTER_WIDTH - len(indent) - 2)
    ]


def default_document(values: dict[str, Any] | None = None) -> str:
    """The starter config: every setting, a comment on each, writ's defaults.

    `values` replaces defaults by path, which is how a legacy `config.json` is
    carried over. Generated from `FIELDS`, so the file cannot drift from what
    writ does.
    """
    chosen = {**DEFAULT_VALUES, **(values or {})}
    lines = _comment(NOTE)
    for section in SECTIONS:
        lines.extend(["", *_comment(SECTION_DOCS[section]), f"{section}:"])
        if section == "agents":
            for role, purpose in ROLES.items():
                doc = purpose
                if role in ROLE_NOTES:
                    doc += f". {ROLE_NOTES[role]}"
                lines.extend([*_comment(doc, "  "), f"  {role}:"])
                for key in ROLE_KEYS:
                    path = f"agents.{role}.{key}"
                    lines.append(f"    {key}: {yamlish.scalar(chosen[path])}")
            continue
        paths = [path for path in FIELDS if path.split(".")[0] == section]
        for path in paths:
            leaf = path.split(".", 1)[1]
            value = yamlish.scalar(chosen[path])
            lines.extend([*_comment(FIELDS[path].doc, "  "), f"  {leaf}: {value}"])
    return "\n".join(lines) + "\n"


#: where a setting lived in the legacy `config.json`, when it has moved
LEGACY_PATHS = {"plan.max_rounds": "adjudicate.max_rounds"}


def _legacy_values(path: Path) -> dict[str, Any]:
    """The settings a legacy `config.json` made that this file still has."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise WritError(f"{path}: not valid JSON ({exc}); fix or delete it") from exc
    out: dict[str, Any] = {}
    for field_path, entry in FIELDS.items():
        node: Any = raw
        for part in LEGACY_PATHS.get(field_path, field_path).split("."):
            node = node.get(part) if isinstance(node, dict) else None
        if node is not None and node != "":
            out[field_path] = _CHECKS[entry.kind](node, f"{path}: {field_path}")
    return out


def ensure(root: str | Path) -> tuple[Path, str]:
    """Write the starter config if the project has none. Never overwrites one.

    Returns the path and what happened: `created`, `kept`, or `converted` when a
    legacy `config.json` supplied the values. The JSON file is left where it is.
    """
    path = config_file(root)
    if path.exists():
        return path, "kept"
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy = legacy_file(root)
    if legacy.exists():
        path.write_text(default_document(_legacy_values(legacy)), encoding="utf-8")
        return path, "converted"
    path.write_text(default_document(), encoding="utf-8")
    return path, "created"


def apply(args: Any, loaded: dict[str, Any]) -> dict[str, tuple[str, Any]]:
    """Fill in every unset argument from the config or the builtin, with provenance.

    Only `None` is filled, which is what makes "a flag always wins" true: the
    parser leaves an unsupplied option as `None`, so an attribute still `None`
    here is one nobody asked about.
    """
    wanted = DEFAULTS.get(getattr(args, "command", None) or "", {})
    decided: dict[str, tuple[str, Any]] = {}
    for attribute, default in wanted.items():
        if not hasattr(args, attribute):
            continue
        if getattr(args, attribute) is not None:
            decided[attribute] = (FROM_FLAG, getattr(args, attribute))
            continue
        value = _lookup(loaded, default.path) if default.path else None
        if value is not None:
            setattr(args, attribute, _for_args(value, default.invert))
            decided[attribute] = (FROM_CONFIG, value)
            continue
        builtin = _resolved_builtin(attribute, default)
        if builtin is not None:
            setattr(args, attribute, _for_args(builtin, default.invert))
        decided[attribute] = (FROM_BUILTIN, builtin)
    return decided


def _for_args(value: Any, invert: bool) -> Any:
    """A positively-stated default, as a `--no-x` attribute spells it."""
    return (not value) if invert else value


def _lookup(loaded: dict[str, Any], path: str) -> Any:
    node: Any = loaded
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    # `False` and `0` are answers; `""` and a missing key are not
    if node is None or node == "":
        return None
    return node
