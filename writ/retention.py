"""What the store keeps, and for how long.

Everything a run leaves is small except its raw event stream, which is kept for
one reason: it is where a stop reason, or the shape of a run nobody watched,
survives. `stream.compact` already shrinks a finished run's log to a small gzip,
so what is left for retention is two things:

- a live `events.jsonl` nothing will ever finish. Runs from before compaction
  existed, and runs whose writ process died mid-stream, never got compacted.
  One untouched for `STALE_LIVE_SECONDS` is not being tailed by anyone.
- compacted logs past their usefulness. They go after `--older-than` days;
  prompts, transcripts and every output a run wrote are never touched.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import state, stream

#: a live log untouched for this long has no writer. The age is since the last
#: event, not since the run began: a running agent appends one per tool call and
#: per message, and the roles that stream events (planner, critic, adjudicator)
#: are killed after `config.AGENT_TIMEOUT`, half an hour, unless someone says
#: otherwise.
STALE_LIVE_SECONDS = 6 * 60 * 60

#: how long a compacted log is kept by default
DEFAULT_KEEP_DAYS = 14


@dataclass
class Report:
    compacted: list[Path] = field(default_factory=list)
    removed: list[Path] = field(default_factory=list)
    freed: int = 0


def sniff_shape(path: Path) -> str:
    """Which agent wrote an event log, read from its first event.

    A run's shape is recorded on its phase step, but not every log has a step,
    and a log with no known shape is still worth compressing: `compact` keeps
    every line of a shape it does not know.
    """
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for _ in range(50):
                line = handle.readline()
                if not line:
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get("type")
                if kind in ("message_update", "turn_end", "agent_start", "tool_execution_start"):
                    return "pi"
                if kind in ("assistant", "system", "stream_event", "user", "result"):
                    return "claude"
    except OSError:
        pass
    return ""


def collect(
    root: str | os.PathLike[str],
    *,
    keep_days: float = DEFAULT_KEEP_DAYS,
    dry_run: bool = False,
    now: float | None = None,
) -> Report:
    """Compact abandoned live logs and remove compacted ones past `keep_days`."""
    report = Report()
    now = time.time() if now is None else now
    store = state.store_dir(root)
    for live in sorted(store.rglob(stream.EVENTS_FILENAME)):
        age = now - live.stat().st_mtime
        if age < STALE_LIVE_SECONDS:
            continue
        before = live.stat().st_size
        report.compacted.append(live)
        if dry_run:
            continue
        compacted = stream.compact(live.parent, sniff_shape(live))
        if compacted is not None:
            report.freed += before - compacted.stat().st_size
    cutoff = now - keep_days * 24 * 60 * 60
    for old in sorted(store.rglob(stream.COMPACT_EVENTS_FILENAME)):
        if old.stat().st_mtime > cutoff:
            continue
        report.removed.append(old)
        report.freed += old.stat().st_size
        if not dry_run:
            old.unlink()
    return report
