"""Turning an agent's structured event stream into something a person can watch.

Every agent CLI has two output modes. In text mode it prints the model's final
message and nothing else, so a run that spends four minutes reading a repository
and thinking is four minutes of an empty terminal — indistinguishable from a
hang, and indistinguishable afterwards from an agent that never started. In
structured mode it emits one JSON event per line as things happen: each tool
call, each block of thinking, each thing the model says.

So Writ asks for the structured stream and renders it. Two products come out of
one pass:

- *activity lines* for the terminal, one per event worth seeing, which is what
  makes a long run legible while it runs.
- the *transcript*, which stays exactly what text mode would have written: the
  agent's own words, no tool names, no thinking, no event envelopes. Everything
  that reads `stdout.log` afterwards — the JSON recovery in `planning`, the
  verdict parser, a critic's report fallback — keeps reading what it always did.

The raw events are kept too, beside the transcript, because they carry what no
rendering should be allowed to lose: the `stopReason` that says whether a silent
agent was truncated at its output ceiling or simply never ran.

Shapes differ per CLI, so each one gets a small adapter. An unknown shape is not
an error: the line is kept for the raw log and rendered as nothing, which
degrades to the old behaviour rather than dropping the run.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

#: how much of a tool argument or a line of speech to show
HINT = 110
LINE = 140

#: argument keys worth showing, most specific first — a tool call is legible from
#: the file it touches or the command it runs, and nothing else it carries
HINT_KEYS = ("file_path", "path", "command", "pattern", "query", "url")


@dataclass
class Rendered:
    """One event's worth of output: what to show, and what to record."""

    #: lines for the terminal, already prefixed with their own marker
    activity: list[str] = field(default_factory=list)
    #: text the agent actually said, destined for the transcript verbatim
    text: str = ""
    #: why the turn ended, when the event says so
    stop_reason: str | None = None


def hint(args: dict) -> str:
    """The one detail that identifies a tool call, on one line."""
    for key in HINT_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:HINT]
    return ""


def first_line(text: str, limit: int = LINE) -> str:
    """The opening line of something the model wrote, for a one-line summary."""
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:limit]
    return ""


class Renderer:
    """Feeds on event lines and yields what to print and what to record.

    Stateful on purpose: a tool call is announced when it starts and only
    mentioned again if it fails, so the renderer has to remember what it already
    said. One renderer per agent run.
    """

    def __init__(self, shape: str) -> None:
        self.shape = shape
        #: tool call id -> what we announced it as, so a failure can name it
        self.pending: dict[str, str] = {}

    def feed(self, line: str) -> Rendered:
        """Render one line of the event stream."""
        stripped = line.strip()
        if not stripped.startswith("{"):
            return Rendered()
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            # A half-written line, or a shape we do not know. The raw log keeps
            # it; there is nothing to show.
            return Rendered()
        if not isinstance(event, dict):
            return Rendered()
        if self.shape == "pi":
            return self._pi(event)
        if self.shape == "claude":
            return self._claude(event)
        return Rendered()

    # ---------------------------------------------------------------- pi

    def _pi(self, event: dict) -> Rendered:
        """pi's `--mode json`: one event per line, deltas plus lifecycle."""
        kind = event.get("type")
        out = Rendered()
        if kind == "tool_execution_start":
            name = str(event.get("toolName") or "tool")
            args = event.get("args")
            detail = hint(args if isinstance(args, dict) else {})
            call_id = str(event.get("toolCallId") or "")
            if call_id:
                self.pending[call_id] = name
            out.activity.append(f"· {name} {detail}".rstrip())
            return out
        if kind == "tool_execution_end":
            call_id = str(event.get("toolCallId") or "")
            name = self.pending.pop(call_id, str(event.get("toolName") or "tool"))
            if event.get("isError"):
                out.activity.append(f"! {name} failed")
            return out
        if kind == "turn_end":
            message = event.get("message")
            if isinstance(message, dict):
                reason = message.get("stopReason")
                out.stop_reason = str(reason) if reason else None
            return out
        if kind != "message_update":
            return out
        inner = event.get("assistantMessageEvent")
        if not isinstance(inner, dict):
            return out
        what = inner.get("type")
        if what == "thinking_start":
            # Said as soon as thinking begins rather than when it ends: the whole
            # point is to show life during the minutes it can take.
            out.activity.append("~ thinking…")
        elif what == "thinking_end":
            summary = first_line(str(inner.get("content") or ""))
            if summary:
                out.activity.append(f"~ {summary}")
        elif what == "text_end":
            content = str(inner.get("content") or "")
            if content.strip():
                out.text = content if content.endswith("\n") else content + "\n"
                out.activity.append(f"> {first_line(content)}")
        return out

    # ------------------------------------------------------------ claude

    def _claude(self, event: dict) -> Rendered:
        """claude's `--output-format stream-json`: whole messages, not deltas."""
        out = Rendered()
        kind = event.get("type")
        if kind != "assistant":
            return out
        message = event.get("message")
        if not isinstance(message, dict):
            return out
        reason = message.get("stop_reason")
        if reason:
            out.stop_reason = str(reason)
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                name = str(block.get("name") or "tool")
                args = block.get("input")
                detail = hint(args if isinstance(args, dict) else {})
                out.activity.append(f"· {name} {detail}".rstrip())
            elif block.get("type") == "text":
                content = str(block.get("text") or "")
                if content.strip():
                    out.text += content if content.endswith("\n") else content + "\n"
                    out.activity.append(f"> {first_line(content)}")
        return out


#: what a truncated turn reports instead of a finished one, per shape. A turn
#: that ends this way spent its whole output allowance before it could act, so
#: the agent exits having written nothing — the one silent failure that is not
#: about authentication or a missing model.
TRUNCATED = {"length", "max_tokens"}


def truncated(reasons: list[str]) -> bool:
    """Whether the last thing the agent did was run out of output budget."""
    return bool(reasons) and reasons[-1] in TRUNCATED
