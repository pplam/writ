"""Knowing how to invoke a coding agent without a terminal.

Every coding agent CLI starts an interactive session by default. Piping a prompt
into `pi`, `claude`, or `codex` does not run it headless — it opens a TUI that
waits on a terminal that is not there, and the process hangs until something
kills it. Each one spells its non-interactive mode differently (`-p`, `exec -`,
`run`), and each spells model selection differently too.

So Writ keeps a small table. `--agent pi --model x` becomes `pi -p --model x`,
`--agent codex` becomes `codex exec -`. Anything not in the table still works:
Writ passes it through untouched and says it could not verify the invocation,
which is the honest answer. Explicit flags always win — if you already wrote
`-p`, Writ does not add a second one.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path

from .state import WritError


@dataclass(frozen=True)
class AgentProfile:
    """How to make one agent run headless and pick a model."""

    #: flags inserted directly after the executable, e.g. `-p` or `exec`
    prefix: tuple[str, ...] = ()
    #: trailing argument that means "prompt arrives on stdin", e.g. codex's `-`
    suffix: tuple[str, ...] = ()
    #: flag that selects a model, or None when Writ does not know one
    model_flag: str | None = None
    #: any of these already present means the caller chose the mode themselves
    interactive_optouts: tuple[str, ...] = ()
    #: subcommands that already imply non-interactive
    headless_subcommands: tuple[str, ...] = ()
    note: str = ""


PROFILES: dict[str, AgentProfile] = {
    "pi": AgentProfile(
        prefix=("-p",),
        model_flag="--model",
        interactive_optouts=("-p", "--print", "--mode", "--export"),
    ),
    "claude": AgentProfile(
        prefix=("-p",),
        model_flag="--model",
        interactive_optouts=("-p", "--print"),
    ),
    "codex": AgentProfile(
        prefix=("exec",),
        suffix=("-",),
        model_flag="--model",
        headless_subcommands=("exec",),
        note="prompt is read from stdin via `exec -`",
    ),
    "gemini": AgentProfile(
        # gemini goes headless on its own when stdin is not a terminal
        model_flag="--model",
        interactive_optouts=("-p", "--prompt"),
        note="headless because stdin is a pipe",
    ),
    "cursor-agent": AgentProfile(
        prefix=("-p",),
        model_flag="--model",
        interactive_optouts=("-p", "--print"),
    ),
    "opencode": AgentProfile(
        prefix=("run",),
        model_flag="--model",
        headless_subcommands=("run",),
    ),
    "amp": AgentProfile(
        prefix=("-x",),
        interactive_optouts=("-x", "--execute"),
    ),
}

#: agents whose default invocation blocks on a terminal we cannot provide
KNOWN_AGENTS = tuple(sorted(PROFILES))


@dataclass
class ResolvedAgent:
    command: list[str]
    profile: AgentProfile | None
    name: str
    #: set when Writ could not verify the command runs without a terminal
    warning: str | None = None

    @property
    def display(self) -> str:
        return shlex.join(self.command)


def resolve(
    agent: str,
    extra_args: list[str] | None = None,
    model: str | None = None,
) -> ResolvedAgent:
    """Build the argv for a headless agent run.

    `agent` is a command string, `extra_args` are the operator's own arguments
    from after `--`, and `model` is the `--model` value to translate. Explicit
    tokens are never overridden: they are the operator saying they know better.
    """
    tokens = shlex.split(agent)
    if not tokens:
        raise WritError("the agent command is empty")
    extra = list(extra_args or [])
    executable, rest = tokens[0], tokens[1:]
    name = Path(executable).name
    profile = PROFILES.get(name)
    supplied = set(rest) | set(extra)

    if profile is None:
        if model:
            raise WritError(
                f"writ does not know how to pass a model to {name!r}; "
                f"put the flag in --agent (e.g. --agent '{agent} --model {model}') "
                f"or after `--`. Known agents: {', '.join(KNOWN_AGENTS)}"
            )
        return ResolvedAgent(
            command=[executable, *rest, *extra],
            profile=None,
            name=name,
            warning=(
                f"{name} is not a known agent, so writ cannot confirm it runs "
                "without a terminal; if the run hangs, add its non-interactive "
                "flag to --agent"
            ),
        )

    prefix = list(profile.prefix)
    if supplied & set(profile.interactive_optouts):
        prefix = []
    if profile.headless_subcommands and any(
        token in profile.headless_subcommands for token in rest
    ):
        prefix = []

    model_args: list[str] = []
    if model:
        if profile.model_flag is None:
            raise WritError(
                f"writ does not know a model flag for {name!r}; "
                "pass it after `--` instead"
            )
        if profile.model_flag in supplied:
            raise WritError(
                f"{profile.model_flag} was given twice: once via --model and once "
                "in the agent command; keep one"
            )
        model_args = [profile.model_flag, model]

    suffix = [token for token in profile.suffix if token not in supplied]
    command = [executable, *prefix, *rest, *extra, *model_args, *suffix]
    return ResolvedAgent(command=command, profile=profile, name=name)


def silent_exit_hint(resolved: ResolvedAgent, code: int) -> str:
    """Guidance for a run that exited on its own having written nothing.

    Distinct from `hang_hint`, which is about a run that had to be killed. An
    agent that exits promptly and prints nothing at all usually never reached a
    model: an unknown model id, a provider it is not authenticated for, or an
    exhausted quota. Several agent CLIs report exactly that as exit 0 with an
    empty stdout, which reads on the dashboard as "the agent worked and forgot
    to report" — the opposite of what happened, and a much worse thing to go
    looking for.
    """
    lines = [
        f"{resolved.name} exited {code} without printing anything, so it "
        "probably never ran: no output means no model call, not a missing "
        "report.",
        f"  it was invoked as `{resolved.display}`",
    ]
    if resolved.profile is not None and resolved.profile.model_flag:
        lines.append(
            f"  check the model id and that {resolved.name} is authenticated "
            "for that provider, then run the same command by hand"
        )
    else:
        lines.append("  run the same command by hand to see what it reports")
    return "\n".join(lines)


def hang_hint(resolved: ResolvedAgent) -> str:
    """Guidance for a run that was killed without producing output."""
    if resolved.profile is None:
        return (
            f"{resolved.name} produced no output before the timeout. Most agent "
            "CLIs open an interactive session unless told otherwise, and an "
            "interactive session waits forever on a terminal writ cannot give "
            f"it. Check {resolved.name}'s non-interactive flag (often -p) and "
            "add it to --agent."
        )
    return (
        f"{resolved.name} produced no output before the timeout. It was invoked "
        f"as `{resolved.display}`, which should be non-interactive; the agent may "
        "be waiting on a prompt, a login, or a slow model. Raise --timeout, or "
        f"check `{resolved.name}` runs on its own first."
    )
