"""Per-project defaults, so a choice is made once rather than on every command.

Every agent and model writ uses came from a flag, and the default was the string
`pi` written into five argparse calls. That is fine for one command and wrong for
the way the tool is actually used: a project settles on a planner, a reviewer that
is deliberately not the planner, and a parallelism its graph can absorb, and then
retypes all of it on every invocation. The flags that matter most are the ones
easiest to forget — `--reviewer` above all, since leaving it off silently hands
the review to the model that wrote the code.

So `.writ/config.json` holds those defaults:

    {
      "agents": {
        "planner":     {"command": "claude", "model": "opus"},
        "critic":      {"command": "codex",  "model": "gpt-5-codex"},
        "implementer": {"command": "claude", "model": "sonnet"},
        "reviewer":    {"command": "codex",  "model": "gpt-5-codex"}
      },
      "run": {"parallel": 3, "order": "depth", "max_rework": 2}
    }

Three rules.

**A flag always wins.** The config is a default, not a policy: it answers "what
did this project decide", and the flag answers "what am I doing right now".

**Everything in it is validated, and an unknown key is an error.** A config is
hand-edited, which means a typo in one is as likely as a typo in a flag — and a
silently ignored `"reviewr"` would leave the review running on the implementing
model while the file on disk says otherwise. Writ would then be lying about
something it explicitly warns about, so a misspelled key fails loudly, naming
what it should have been.

**Writ does not write this file.** It is yours. `writ init` does not create one,
nothing edits it, and the example lives in the repository as
`config.example.json` rather than being generated into your project — a config
writ writes is a config writ can silently change.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .state import WritError, store_dir

CONFIG_FILENAME = "config.json"

#: the agent roles a project can configure, and what each one does.
#:
#: Four, not six. Gates and repair planners have no entry of their own: they run on
#: the `implementer` setting, because `_prepare` in orchestrator.py branches only
#: on `reviewer`. That is worth knowing and arguably worth changing — a gate is a
#: review by definition, and the agent that wrote the code is the wrong one to ask
#: whether the milestone holds — but it is what writ does today, and a config key
#: that claimed otherwise would be the same lie as a silently ignored typo.
ROLES: dict[str, str] = {
    "planner": "turns the design document into a plan (writ plan)",
    "critic": "reads the committed plan and reports findings (writ critique)",
    "implementer": "writes the code for one task, and runs gates and repairs",
    "reviewer": "independently checks a completed task (writ review)",
}

#: the keys one role may set
ROLE_KEYS = ("command", "model", "timeout")

#: run defaults, mapped to the flag each one stands in for
RUN_KEYS: dict[str, str] = {
    "parallel": "--parallel",
    "order": "--order",
    "max_rework": "--max-rework",
}

SECTIONS = ("agents", "run")


def config_file(root: str | Path) -> Path:
    return store_dir(root) / CONFIG_FILENAME


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
    """
    if not isinstance(raw, dict):
        raise WritError(f"{where}: expected a JSON object")
    _reject_unknown(raw, SECTIONS, where, "section")
    out: dict[str, Any] = {}
    if "agents" in raw:
        out["agents"] = _agents(raw["agents"], f"{where}: agents")
    if "run" in raw:
        out["run"] = _run(raw["run"], f"{where}: run")
    return out


def _agents(value: Any, where: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise WritError(f"{where}: expected an object of role -> settings")
    _reject_unknown(value, tuple(ROLES), where, "role")
    out: dict[str, dict[str, Any]] = {}
    for role, settings in value.items():
        if is_comment(role):
            continue
        at = f"{where}.{role}"
        if not isinstance(settings, dict):
            raise WritError(
                f"{at}: expected an object like "
                '{"command": "claude", "model": "opus"}'
            )
        _reject_unknown(settings, ROLE_KEYS, at, "key")
        entry: dict[str, Any] = {}
        if "command" in settings:
            entry["command"] = _command(settings["command"], f"{at}.command")
        if "model" in settings:
            entry["model"] = _text(settings["model"], f"{at}.model")
        if "timeout" in settings:
            entry["timeout"] = _positive_int(settings["timeout"], f"{at}.timeout")
        out[role] = entry
    return out


def _run(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WritError(f"{where}: expected an object of setting -> value")
    _reject_unknown(value, tuple(RUN_KEYS), where, "setting")
    from .orchestrator import ORDERS

    out: dict[str, Any] = {}
    if "parallel" in value:
        out["parallel"] = _positive_int(value["parallel"], f"{where}.parallel")
    if "order" in value:
        order = _text(value["order"], f"{where}.order")
        if order not in ORDERS:
            raise WritError(
                f"{where}.order: {order!r} is not an order; "
                f"choose from {', '.join(ORDERS)}"
            )
        out["order"] = order
    if "max_rework" in value:
        # Zero is meaningful here — it is `--max-rework 0`, fail on first
        # rejection — so this one is not `_positive_int`.
        rework = value["max_rework"]
        if not isinstance(rework, int) or isinstance(rework, bool) or rework < 0:
            raise WritError(
                f"{where}.max_rework: expected a whole number of attempts, "
                f"0 or more (got {rework!r})"
            )
        out["max_rework"] = rework
    return out


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


def _command(value: Any, where: str) -> str:
    text = _text(value, where)
    if not text:
        raise WritError(
            f"{where}: expected an agent command like \"claude\" or "
            "\"codex exec\", not an empty string"
        )
    return text


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise WritError(f"{where}: expected a string (got {type(value).__name__})")
    return value.strip()


def _positive_int(value: Any, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise WritError(f"{where}: expected a whole number above 0 (got {value!r})")
    return value


# --------------------------------------------------------------------------
# applying it to parsed arguments


#: The default each argument takes when neither a flag nor the config sets it.
#:
#: `AGENT_TIMEOUT` is the wall clock a planning or review agent gets. There is no
#: equivalent for `writ run` and `writ dispatch`: an implementation agent runs
#: unbounded unless someone says otherwise, because the honest timeout for
#: "implement this task" depends on the task, and a wrong guess kills work that
#: was going fine. Planning and critique are one bounded question each, so a
#: ceiling there is a safety net rather than a guess.
AGENT_TIMEOUT = 1800


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


#: for each subcommand, which config entry stands behind each of its arguments.
#:
#: Written out per command rather than inferred, because the same flag name means
#: a different role depending on the verb: `--agent` is the planner under `writ
#: plan`, a critic under `writ critique`, the reviewer under `writ review`, and
#: the implementer under `writ run`. A rule that mapped `--agent` to one role
#: would quietly give three of those four the wrong default.
DEFAULTS: dict[str, dict[str, Default]] = {
    "plan": {
        "agent": Default("agents.planner.command", "pi"),
        "model": Default("agents.planner.model"),
        "timeout": Default("agents.planner.timeout", AGENT_TIMEOUT),
        "critic_agent": Default("agents.critic.command"),
        "critic_model": Default("agents.critic.model"),
    },
    "critique": {
        "agent": Default("agents.critic.command", "pi"),
        "model": Default("agents.critic.model"),
        "timeout": Default("agents.critic.timeout", AGENT_TIMEOUT),
    },
    "dispatch": {
        "agent": Default("agents.implementer.command", "pi"),
        "model": Default("agents.implementer.model"),
        "timeout": Default("agents.implementer.timeout"),
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
    },
}

#: fallbacks that live in another module, so they are read rather than copied
LATE_BUILTINS = {"order": _builtin_order, "max_rework": _builtin_rework}

#: where a resolved value came from, for `writ agents` to report
FROM_FLAG = "flag"
FROM_CONFIG = "config"
FROM_BUILTIN = "default"


def apply(args: Any, loaded: dict[str, Any]) -> dict[str, tuple[str, Any]]:
    """Fill in every unset argument from the config, and say what came from where.

    Only `None` is filled, which is what makes "a flag always wins" true rather
    than aspirational: the parser leaves an unsupplied option as `None`, so an
    attribute still holding `None` here is one nobody asked about. That only works
    because the parser no longer carries the defaults itself — with
    `default="pi"`, an explicit `--agent pi` and an absent flag are the same value,
    and the config could not tell them apart to know whether to override.

    Returns the provenance of each value, for `writ agents` to show.
    """
    wanted = DEFAULTS.get(getattr(args, "command", None) or "", {})
    decided: dict[str, tuple[str, Any]] = {}
    for attribute, default in wanted.items():
        if not hasattr(args, attribute):
            continue
        if getattr(args, attribute) is not None:
            decided[attribute] = (FROM_FLAG, getattr(args, attribute))
            continue
        value = _lookup(loaded, default.path)
        if value is not None:
            setattr(args, attribute, value)
            decided[attribute] = (FROM_CONFIG, value)
            continue
        builtin = default.builtin
        if builtin is None and attribute in LATE_BUILTINS:
            builtin = LATE_BUILTINS[attribute]()
        if builtin is not None:
            setattr(args, attribute, builtin)
        decided[attribute] = (FROM_BUILTIN, builtin)
    return decided


def _lookup(loaded: dict[str, Any], path: str) -> Any:
    """Read an `agents.reviewer.command`-style path out of a loaded config."""
    node: Any = loaded
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node if node not in ("", None) else None
