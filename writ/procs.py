"""Process identity: who owns a run, a state lock, or a run session.

A bare pid is not an identity. PIDs are small integers the kernel reuses as soon
as the table wraps, so `kill(pid, 0)` answers "is *something* alive at this
number", which is not the question any of writ's owners actually ask. The three
places that ask it are all load-bearing:

* `reap` decides a run is abandoned and returns its task to the queue. A reused
  pid makes a dead run look alive, so the task stays wedged.
* `cancel` sends SIGTERM to a process group. A reused pid means signalling
  something that was never ours.
* the state lock and the run session decide whether another writ process is
  still working. Wrong either way: a stolen lock is concurrent writes to
  `state.json`, a wrong claim is a confusing refusal to start.

So ownership is recorded as a composite: pid, the process's start time,
the hostname, and a token unique to the claim. A process is the recorded owner
only when the whole tuple matches. Where a start time cannot be read the
identity degrades to the pid alone — no worse than before, and said out loud
rather than assumed.

The probe is deliberately two-stage. `os.kill(pid, 0)` is a syscall; reading a
start time costs a `/proc` read or a `ps` fork. Every caller checks liveness
first, so the expensive half only runs for the handful of pids that are alive.
"""
from __future__ import annotations

import os
import platform
import re
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

#: how far apart two readings of the same process's start time may be and still
#: be the same process. `ps -o lstart` has one-second granularity, and the
#: /proc arithmetic rounds against a boot time that is itself a whole second, so
#: an exact comparison would report every process as a different one.
START_TOLERANCE_SECONDS = 1.5


@dataclass(frozen=True)
class Identity:
    """Who a claim belongs to.

    `start_time` is None when this platform would not say, which is the one case
    where matching falls back to the pid. `token` is what makes a claim
    releasable by its own owner and nobody else: two processes can share a pid
    across a reboot, but not a uuid.
    """

    pid: int
    start_time: float | None = None
    host: str = ""
    token: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "start_time": self.start_time,
            "host": self.host,
            "token": self.token,
        }

    @property
    def described(self) -> str:
        """For an error message a person has to act on."""
        where = f" on {self.host}" if self.host and self.host != hostname() else ""
        return f"pid {self.pid}{where}"


def hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:  # pragma: no cover - defensive
        return ""


def identify(pid: int | None = None, *, token: str | None = None) -> Identity:
    """The identity of this process, or of a child we just spawned.

    A freshly spawned child is probed rather than stamped with the current time:
    the difference is small but it is the difference between a recorded start
    time and a guess, and the guess is what a later comparison would trust.
    """
    target = os.getpid() if pid is None else pid
    return Identity(
        pid=target,
        start_time=start_time(target),
        host=hostname(),
        token=token or uuid.uuid4().hex,
    )


def normalize(value: Any) -> Identity | None:
    """Read an identity out of state, accepting what older writs wrote.

    A bare int is a pid recorded before identities existed. It is honoured, with
    no start time, so an in-flight project keeps working across the upgrade.
    """
    if value is None:
        return None
    if isinstance(value, Identity):
        return value
    if isinstance(value, bool):  # pragma: no cover - defensive
        return None
    if isinstance(value, int):
        return Identity(pid=value) if value > 0 else None
    if isinstance(value, str):
        try:
            pid = int(value.split()[0])
        except (ValueError, IndexError):
            return None
        return Identity(pid=pid) if pid > 0 else None
    if isinstance(value, dict):
        try:
            pid = int(value.get("pid") or 0)
        except (TypeError, ValueError):
            return None
        if pid <= 0:
            return None
        raw = value.get("start_time")
        try:
            started = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            started = None
        return Identity(
            pid=pid,
            start_time=started,
            host=str(value.get("host") or ""),
            token=str(value.get("token") or ""),
        )
    return None


def running(pid: int | None) -> bool:
    """Is *something* alive at this pid. The cheap half of the probe."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Alive and owned by someone else. Still alive, which is the question.
        return True
    except OSError:  # pragma: no cover - defensive
        return False
    return True


def alive(record: Any) -> bool:
    """Is the process this record names still the process that was recorded.

    False for a dead pid, and false for a *reused* one — the case a bare pid
    cannot tell apart. A record from another host is reported alive, because
    this machine has no standing to say otherwise; `confirmed_dead` is the
    function that cares about the difference.
    """
    identity = normalize(record)
    if identity is None:
        return False
    if identity.host and identity.host != hostname():
        return True
    if not running(identity.pid):
        return False
    return not _start_time_differs(identity)


def confirmed_dead(record: Any) -> bool:
    """Can this machine *prove* the recorded owner is gone.

    The asymmetry with `alive` is deliberate and is the whole point of the
    function: breaking a lock, reaping a run and stealing a session are all
    destructive, so they need proof rather than the absence of evidence. A
    record with no pid is proof (nothing was ever claimed); a record from
    another host is not.
    """
    identity = normalize(record)
    if identity is None:
        return True
    if identity.host and identity.host != hostname():
        return False
    if not running(identity.pid):
        return True
    return _start_time_differs(identity)


def safe_to_signal(record: Any) -> int | None:
    """The pid to signal, or None when signalling it would hit a stranger.

    Guards the one irreversible thing writ does to something outside itself. A
    recorded start time that no longer matches means the pid was recycled, and a
    process group that is also *ours* means the pid resolves to writ itself —
    every agent is spawned with `start_new_session`, so a match there is never
    the agent.
    """
    identity = normalize(record)
    if identity is None:
        return None
    if identity.host and identity.host != hostname():
        return None
    if not running(identity.pid):
        return None
    if _start_time_differs(identity):
        return None
    try:
        if os.getpgid(identity.pid) == os.getpgid(0):
            return None
    except (ProcessLookupError, PermissionError, OSError):
        pass
    return identity.pid


def _start_time_differs(identity: Identity) -> bool:
    """True only when both start times are known and they disagree."""
    if identity.start_time is None:
        return False
    observed = start_time(identity.pid)
    if observed is None:
        return False
    return abs(observed - identity.start_time) > START_TOLERANCE_SECONDS


# --------------------------------------------------------------------------
# reading a process's start time


#: how long another process's start time is reused rather than re-probed. A
#: process's start time cannot change, so the only risk in caching it is a pid
#: that is recycled within the window — which needs the kernel's whole pid space
#: to wrap inside a second. Our own pid is cached without a window, since it is
#: not a guess about anyone else.
_START_CACHE_SECONDS = 1.0
_start_cache: dict[int, tuple[float, float | None]] = {}
_start_lock = threading.Lock()


def start_time(pid: int | None) -> float | None:
    """When this process started, as a unix timestamp, or None if unknown.

    Unknown is a real answer and is handled everywhere it is returned: an
    identity without a start time still carries a pid, a host and a token, which
    is strictly more than writ recorded before.

    Cached, because the uncached call forks `ps` on anything without `/proc` and
    this is now on the path of every state transaction — a few milliseconds each
    is not much until a scheduler is taking thousands of them.
    """
    if not pid or pid <= 0:
        return None
    now = time.monotonic()
    mine = pid == os.getpid()
    with _start_lock:
        hit = _start_cache.get(pid)
        if hit is not None and (mine or now - hit[0] < _START_CACHE_SECONDS):
            return hit[1]
    found = _probe_start_time(pid)
    with _start_lock:
        if len(_start_cache) > 512:  # pragma: no cover - long-lived process
            _start_cache.clear()
        _start_cache[pid] = (now, found)
    return found


def _probe_start_time(pid: int) -> float | None:
    if platform.system() == "Linux":
        found = _start_time_proc(pid)
        if found is not None:
            return found
    return _start_time_ps(pid)


def _start_time_proc(pid: int) -> float | None:
    """From /proc, where it is exact and costs no fork."""
    try:
        stat = open(f"/proc/{pid}/stat", "rb").read().decode("utf-8", "replace")
    except OSError:
        return None
    # field 2 is the executable name in parentheses and may itself contain
    # spaces and parentheses, so the fields after it are counted from the last
    # closing paren rather than from a naive split.
    tail = stat.rpartition(")")[2].split()
    if len(tail) < 20:  # pragma: no cover - malformed /proc
        return None
    try:
        ticks = float(tail[19])
    except ValueError:  # pragma: no cover - malformed /proc
        return None
    boot = _boot_time()
    if boot is None:  # pragma: no cover - /proc/stat without btime
        return None
    hertz = os.sysconf("SC_CLK_TCK") or 100
    return boot + ticks / hertz


def _boot_time() -> float | None:
    try:
        with open("/proc/stat", "rb") as handle:
            for line in handle:
                if line.startswith(b"btime "):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):  # pragma: no cover - defensive
        return None
    return None  # pragma: no cover - /proc/stat always has btime


#: `ps -o lstart=` under LC_ALL=C, e.g. "Mon Sep 22 10:11:12 2026".
_LSTART = "%a %b %d %H:%M:%S %Y"


def _start_time_ps(pid: int) -> float | None:
    """From `ps`, which every POSIX platform has and macOS needs.

    Forced to the C locale: the month and weekday names are parsed, and under a
    localized `ps` they would not be the ones `strptime` expects.
    """
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no ps
        return None
    line = result.stdout.strip()
    if result.returncode != 0 or not line:
        return None
    line = re.sub(r"\s+", " ", line)
    try:
        return datetime.strptime(line, _LSTART).timestamp()
    except ValueError:  # pragma: no cover - unexpected ps format
        return None
