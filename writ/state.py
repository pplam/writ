"""State storage for Writ.

The store is deliberately boring: one JSON document per project, written
atomically, guarded by an advisory lock file so a detached supervisor and an
interactive CLI cannot clobber each other.

Layout under a project root:

    .writ/
        state.json        the whole project: milestones, tasks, runs, decisions
        state.lock        advisory lock; an OS-level `flock` where available, so
                          it is released by the kernel if a holder dies and can
                          never be taken from a holder that is merely slow
        decisions.md      human-readable, append-only mirror of the decision log
        runs/<run-id>/    prompt.txt, stdout.log, stderr.log, meta.json
        plans/<plan-id>/  prompt.txt, plan.json, stdout.log, stderr.log
"""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import procs

try:  # pragma: no cover - platform dependent
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

SCHEMA_VERSION = 1

STORE_DIRNAME = ".writ"
STATE_FILENAME = "state.json"
LOCK_FILENAME = "state.lock"
DECISIONS_FILENAME = "decisions.md"
RUNS_DIRNAME = "runs"
PLANS_DIRNAME = "plans"

LOCK_TIMEOUT_SECONDS = 10.0
LOCK_STALE_SECONDS = 60.0

#: how often the fallback lock refreshes its own mtime while held. The age
#: timeout below only means "abandoned" if a live holder keeps saying otherwise,
#: so the heartbeat is what makes a slow transaction different from a dead one.
LOCK_HEARTBEAT_SECONDS = 5.0

#: set WRIT_FSYNC=0 to skip the flush that makes a committed write survive power
#: loss. Every write then costs one fewer syscall pair and the store becomes only
#: as durable as the page cache — worth it for a throwaway project, never for one
#: whose plan you would mind re-deriving.
FSYNC_ENV = "WRIT_FSYNC"


class WritError(Exception):
    """Any expected, user-facing failure."""


def utcnow() -> str:
    """Timestamp used for every recorded event."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def store_dir(root: str | os.PathLike[str]) -> Path:
    """The `.writ` directory for a project root."""
    return Path(root).expanduser() / STORE_DIRNAME


def state_file(root: str | os.PathLike[str]) -> Path:
    return store_dir(root) / STATE_FILENAME


def runs_dir(root: str | os.PathLike[str]) -> Path:
    return store_dir(root) / RUNS_DIRNAME


def plans_dir(root: str | os.PathLike[str]) -> Path:
    return store_dir(root) / PLANS_DIRNAME


def plan_dir(root: str | os.PathLike[str], plan_id: str) -> Path:
    return plans_dir(root) / plan_id


def decisions_file(root: str | os.PathLike[str]) -> Path:
    return store_dir(root) / DECISIONS_FILENAME


def run_dir(root: str | os.PathLike[str], run_id: str) -> Path:
    return runs_dir(root) / run_id


def empty_state() -> dict[str, Any]:
    """A fresh project document."""
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": utcnow(),
        "design_docs": [],
        "milestones": {},
        "tasks": {},
        "runs": {},
        "plans": [],
        # what the design asks for, keyed by requirement id, and the plan-level
        # record of whether the graph covering it has been reviewed. Tasks are
        # the work; these two are what the work is answerable to.
        "requirements": {},
        "plan": {
            "status": "draft",
            "revision": 0,
            "checked_at": None,
            "approved_at": None,
            "approved_by": None,
            "approval_note": None,
            "forced": False,
        },
        "findings": [],
        "repairs": [],
        "decisions": [],
        "counters": {"decision": 0, "finding": 0, "repair": 0, "gate": 0},
    }


def is_initialized(root: str | os.PathLike[str]) -> bool:
    return state_file(root).exists()


def initialize(root: str | os.PathLike[str], force: bool = False) -> Path:
    """Create the store. Refuses to overwrite unless `force`."""
    target = state_file(root)
    if target.exists() and not force:
        raise WritError(
            f"already initialized at {store_dir(root)} (use --force to reset)"
        )
    runs_dir(root).mkdir(parents=True, exist_ok=True)
    plans_dir(root).mkdir(parents=True, exist_ok=True)
    _write(target, empty_state())
    sweep_temporaries(root)
    return store_dir(root)


def load(root: str | os.PathLike[str]) -> dict[str, Any]:
    """Read the project document. Raises if the project is not initialized."""
    target = state_file(root)
    if not target.exists():
        raise WritError(
            f"no Writ project at {Path(root).expanduser()} (run `writ init` first)"
        )
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - corrupted store
        raise WritError(f"corrupt state file {target}: {exc}") from exc
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise WritError(
            f"state schema {version!r} is not supported by this Writ build "
            f"(expected {SCHEMA_VERSION})"
        )
    # Defaults rather than a schema bump: every one of these is additive, and a
    # project planned by an older Writ stays readable and runnable without a
    # migration step that could fail halfway.
    data.setdefault("plans", [])
    data.setdefault("requirements", {})
    data.setdefault("findings", [])
    data.setdefault("repairs", [])
    data.setdefault(
        "plan",
        {
            "status": "draft",
            "revision": 0,
            "checked_at": None,
            "approved_at": None,
            "approved_by": None,
            "approval_note": None,
            "forced": False,
        },
    )
    for task in data.get("tasks", {}).values():
        # Every task written before gates existed is implementation work. Defaulted
        # here rather than at each read so that `kind` is something callers can rely
        # on being present, which is what makes `task["kind"] == "gate"` safe to
        # write anywhere in the codebase.
        task.setdefault("kind", "task")
        task.setdefault("requirement_ids", [])
    return data


def fsync_enabled() -> bool:
    """Whether a committed write is flushed to the device before returning."""
    return os.environ.get(FSYNC_ENV, "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _write(target: Path, data: dict[str, Any]) -> None:
    """Replace the document durably: a commit survives a crash, or never happened.

    `os.replace` alone is atomic against a concurrent *reader* — nobody ever sees
    half a document — but it says nothing about power loss. Without the flushes
    below, a committed transaction can be in the page cache and nowhere else, so
    a machine that dies comes back having lost the most recent state while every
    run directory on disk says the work happened. Every other guarantee in writ
    is written down in this file, so this is the floor they all stand on.

    Four steps, in this order, and the order is the whole thing:

    1. write the replacement to a temporary file in the same directory;
    2. `fsync` it, so its *contents* are on the device before it has a name
       anyone will read;
    3. `os.replace`, which is the atomic commit point;
    4. `fsync` the directory, so the rename itself is on the device.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    tmp = target.with_name(f"{target.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    sync = fsync_enabled()
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            if sync:
                handle.flush()
                os.fsync(handle.fileno())
        tmp.replace(target)
    except OSError:
        # A failure before the replace leaves the previous document intact, which
        # is the outcome to preserve: better an old state than a truncated one.
        # The temporary file would otherwise accumulate, and `sweep_temporaries`
        # only runs at startup.
        try:
            tmp.unlink()
        except OSError:  # pragma: no cover - defensive
            pass
        raise
    if sync:
        _fsync_dir(target.parent)


def _fsync_dir(directory: Path) -> None:
    """Flush a directory entry, where the platform supports it.

    Not every filesystem allows opening a directory for this, and Windows does
    not at all. A platform that refuses leaves the rename as durable as it was
    before — the file's own contents are still flushed — so this is best effort
    by design rather than by omission.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:  # pragma: no cover - platform dependent
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - platform dependent
        pass
    finally:
        os.close(fd)


def sweep_temporaries(root: str | os.PathLike[str]) -> list[Path]:
    """Delete temporary state files orphaned by a crash. Returns what it removed.

    A write that dies between step 1 and step 3 above leaves a `state.json.tmp.*`
    behind. It is harmless — nothing reads it — but it accumulates, and a store
    littered with debris from previous crashes is a store nobody trusts.

    A temporary whose writing process is still alive is left alone: the pid is in
    the filename precisely so a concurrent writer's work-in-progress can be told
    from a dead one's leavings.
    """
    directory = store_dir(root)
    removed: list[Path] = []
    for path in sorted(directory.glob(f"{STATE_FILENAME}.tmp.*")):
        if procs.running(_temporary_pid(path)):
            continue
        try:
            path.unlink()
        except OSError:  # pragma: no cover - raced with another sweeper
            continue
        removed.append(path)
    return removed


def _temporary_pid(path: Path) -> int | None:
    """The pid embedded in a temporary file's name, if it is readable."""
    parts = path.name.split(".tmp.")
    if len(parts) != 2:  # pragma: no cover - glob guarantees the marker
        return None
    try:
        return int(parts[1].split(".")[0])
    except ValueError:
        return None


def save(root: str | os.PathLike[str], data: dict[str, Any]) -> None:
    _write(state_file(root), data)


@contextmanager
def _lock(root: str | os.PathLike[str]) -> Iterator[None]:
    """Advisory inter-process lock around a read-modify-write cycle.

    Two implementations, same contract. Where the OS has `flock` that is what is
    used, because it makes the hard half of this problem the kernel's: a lock
    held by a process that dies is released by the kernel when its file
    descriptors close, so there is no such thing as a stale lock to guess about,
    and no timeout that can steal one from a holder that is merely slow.

    The fallback, for a platform without `flock`, keeps the old create-exclusive
    file but no longer breaks a lock on age alone — see `_lock_fallback`.
    """
    path = store_dir(root) / LOCK_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    keeper = _lock_flock if fcntl is not None else _lock_fallback
    with keeper(path):
        yield


@contextmanager
def _lock_flock(path: Path) -> Iterator[None]:
    """The OS-level path: an advisory lock the kernel releases on death.

    The lock *file* is never unlinked. Unlinking it is what would reintroduce the
    race this removes: the lock lives on the open file, so a holder that deletes
    the path on release lets the next writer create a fresh file and lock that
    one instead — two processes, two inodes, one `state.json`. The file stays,
    and its contents say who holds it, for an error message that names a pid
    rather than shrugging.
    """
    identity = procs.identify()
    handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise WritError(_held_by(path, identity)) from None
                time.sleep(0.05)
        try:
            _stamp(handle, identity)
            yield
        finally:
            # Cleared before the unlock so the next holder never reads the
            # previous owner's identity as the current one.
            try:
                os.ftruncate(handle, 0)
            except OSError:  # pragma: no cover - defensive
                pass
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        os.close(handle)


@contextmanager
def _lock_fallback(path: Path) -> Iterator[None]:
    """Create-exclusive locking, for a platform without `flock`.

    The old version broke any lock older than 60 seconds. That is a data-loss
    bug rather than a recovery mechanism: a transaction slower than the timeout —
    a paused process, a slow filesystem, a big document — had its lock taken
    while it was still inside the critical section, and two writers then wrote
    `state.json` with nothing to detect it.

    Three changes make the timeout a last resort instead of the first rule:

    * a lock is broken immediately when its recorded owner is *proven* gone,
      which is the case that actually needs breaking and no longer needs to wait
      out a minute;
    * a holder refreshes the file's mtime while it works, so age measures
      abandonment rather than duration;
    * release unlinks only a lock this process still owns, so a holder whose lock
      was broken anyway cannot delete its successor's.
    """
    identity = procs.identify()
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    handle = None
    while handle is None:
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if _lock_is_stale(path):
                _break_lock(path)
                continue
            if time.monotonic() > deadline:
                raise WritError(_held_by(path, identity)) from None
            time.sleep(0.05)
    _stamp(handle, identity)
    os.close(handle)
    stop = threading.Event()
    beat = threading.Thread(
        target=_heartbeat, args=(path, identity, stop), daemon=True
    )
    beat.start()
    try:
        yield
    finally:
        stop.set()
        beat.join(timeout=1)
        _release(path, identity)


def _stamp(handle: int, identity: procs.Identity) -> None:
    """Record who holds the lock, in the lock."""
    payload = json.dumps({**identity.to_dict(), "at": utcnow()}) + "\n"
    try:
        os.lseek(handle, 0, os.SEEK_SET)
        os.write(handle, payload.encode())
    except OSError:  # pragma: no cover - defensive
        pass


def _heartbeat(path: Path, identity: procs.Identity, stop: threading.Event) -> None:
    """Keep saying "still working" for as long as this process holds the lock."""
    while not stop.wait(LOCK_HEARTBEAT_SECONDS):
        owner = lock_owner(path)
        if owner is not None and owner.token and owner.token != identity.token:
            return  # someone broke our lock; stop refreshing theirs
        try:
            os.utime(path, None)
        except OSError:
            return


def lock_owner(path: Path) -> procs.Identity | None:
    """The identity recorded in a lock file, if one is readable.

    Accepts the `<pid> <timestamp>` line older writs wrote, so a lock left behind
    by a previous build is still attributable rather than anonymous.
    """
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not text:
        return None
    try:
        return procs.normalize(json.loads(text))
    except json.JSONDecodeError:
        return procs.normalize(text)


def _held_by(path: Path, identity: procs.Identity) -> str:
    """The message for a lock this process could not get.

    It names the holder, because the action a person takes next depends entirely
    on whether that pid is a run they forgot about or a process that is wedged.
    """
    owner = lock_owner(path)
    if owner is None:
        who = "another Writ process may be writing"
    elif owner.token and owner.token == identity.token:  # pragma: no cover
        who = "this process already holds it (a nested transaction)"
    else:
        who = f"held by {owner.described}"
    return f"timed out waiting for the state lock at {path}; {who}"


def _lock_is_stale(path: Path) -> bool:
    """Whether a lock may be broken. Only for the no-`flock` fallback.

    Proof first: a lock whose owner this machine can show is gone is stale
    however new it is. Age is kept only as the last resort for the cases proof
    cannot reach — a lock written by a process on another host, or one whose
    identity predates this field — and with the heartbeat above, reaching that
    timeout now means nothing has touched the lock for a full minute.
    """
    owner = lock_owner(path)
    if owner is not None and procs.confirmed_dead(owner):
        return True
    try:
        age = time.time() - path.stat().st_mtime
    except (FileNotFoundError, OSError):
        return False
    return age > LOCK_STALE_SECONDS


def _break_lock(path: Path) -> None:
    """Remove a lock whose holder is gone. Unconditional, by contract."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _release(path: Path, identity: procs.Identity | None = None) -> None:
    """Drop a lock this process holds, and only one it holds.

    The owner check is what stops the pathological case: our lock was broken as
    stale, another process acquired it, and then we finish and unlink — releasing
    a lock that is not ours while its holder is still writing.
    """
    if identity is not None:
        owner = lock_owner(path)
        if owner is not None and owner.token and owner.token != identity.token:
            return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


@contextmanager
def transaction(root: str | os.PathLike[str]) -> Iterator[dict[str, Any]]:
    """Load, mutate, save — under a lock, so concurrent writers serialize."""
    with _lock(root):
        data = load(root)
        yield data
        save(root, data)
