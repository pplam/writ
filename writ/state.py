"""State storage for Writ.

To every caller the project is one JSON document: `load` returns a dict,
`transaction` hands one over and commits whatever it looks like afterwards. On
disk it is a SQLite file of small records — one per task, run, milestone and
requirement, one per entry of each log, one per remaining top-level field —
so a commit writes the records that changed rather than the whole project.
An advisory lock file still serializes writers, so a detached supervisor and an
interactive CLI cannot clobber each other.

Layout under a project root:

    .writ/
        store.db          the whole project, as records (see `_shards`)
        state.lock        advisory lock; an OS-level `flock` where available, so
                          it is released by the kernel if a holder dies and can
                          never be taken from a holder that is merely slow
        decisions.md      human-readable, append-only mirror of the decision log
        runs/<task>/<nn>-<role>/
                          prompt.txt, stdout.log, stderr.log, meta.json, and the
                          run's output; see runner.new_run_id
        plans/<plan-id>/  one plan: analysis artifacts, draft.json, the committed
                          index (plan.json) and features/, rounds/;
                          see planfiles.py

A store from before the records (`state.json`, schema 1) is converted by
`writ migrate`; `writ state dump` prints the document either way.
"""
from __future__ import annotations

import json
import os
import sqlite3
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

SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1

STORE_DIRNAME = ".writ"
DB_FILENAME = "store.db"
#: the single-document store of schema 1, read only by `migrate`
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
    """The file that holds the project. Its mtime moves on every commit."""
    return store_dir(root) / DB_FILENAME


def legacy_file(root: str | os.PathLike[str]) -> Path:
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
        # the pre-execution record: one entry per planning attempt, written while
        # it happens rather than after. Everything else here is a record of work
        # that finished; this is the only one a reader can watch.
        "phases": [],
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
    return state_file(root).exists() or legacy_file(root).exists()


def initialize(root: str | os.PathLike[str], force: bool = False) -> Path:
    """Create the store. Refuses to overwrite unless `force`."""
    if is_initialized(root) and not force:
        raise WritError(
            f"already initialized at {store_dir(root)} (use --force to reset)"
        )
    runs_dir(root).mkdir(parents=True, exist_ok=True)
    plans_dir(root).mkdir(parents=True, exist_ok=True)
    target = state_file(root)
    target.unlink(missing_ok=True)
    _commit(target, empty_state(), {})
    sweep_temporaries(root)
    return store_dir(root)


def load(root: str | os.PathLike[str]) -> dict[str, Any]:
    """Read the project document. Raises if the project is not initialized."""
    return _load(root)[0]


def _load(root: str | os.PathLike[str]) -> tuple[dict[str, Any], dict[Key, Row]]:
    """The document, and the records it was read from, for `transaction` to diff."""
    target = state_file(root)
    if not target.exists():
        if legacy_file(root).exists():
            raise WritError(
                f"the store at {store_dir(root)} is from an older Writ "
                f"({STATE_FILENAME}, schema {LEGACY_SCHEMA_VERSION}); "
                "run `writ migrate` to convert it"
            )
        raise WritError(
            f"no Writ project at {Path(root).expanduser()} (run `writ init` first)"
        )
    rows = _read_rows(target)
    data = _assemble(rows)
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise WritError(
            f"state schema {version!r} is not supported by this Writ build "
            f"(expected {SCHEMA_VERSION})"
        )
    return _defaults(data), rows


def _defaults(data: dict[str, Any]) -> dict[str, Any]:
    # Defaults rather than a schema bump: every one of these is additive, and a
    # project planned by an older Writ stays readable and runnable without a
    # migration step that could fail halfway.
    data.setdefault("plans", [])
    data.setdefault("phases", [])
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
    data.setdefault("plan_revisions", [])
    return data


# ------------------------------------------------------------------ records
#
# A record is (section, item) -> (kind, position, body). `kind` says how the
# section is put back together: `value` is a top-level field stored whole,
# `map` and `list` are a section stored one entry per record plus an empty
# container record, so an empty section survives and a one-entry change writes
# one record. Bodies are canonical JSON (sorted keys), so an unchanged entry
# serializes to the same bytes and is not written.

Key = tuple[str, str]
Row = tuple[str, int, str]

#: sections stored one record per entry, keyed by id
MAP_SECTIONS = ("tasks", "runs", "milestones", "requirements")
#: sections stored one record per entry, in order
LIST_SECTIONS = ("findings", "repairs", "decisions", "phases", "plans", "plan_revisions")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS record (
    section  TEXT NOT NULL,
    item     TEXT NOT NULL,
    kind     TEXT NOT NULL,
    position INTEGER NOT NULL,
    body     TEXT NOT NULL,
    PRIMARY KEY (section, item)
)
"""


def _body(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _shards(data: dict[str, Any]) -> dict[Key, Row]:
    """The document as records."""
    rows: dict[Key, Row] = {}
    for section, value in data.items():
        if section in MAP_SECTIONS and isinstance(value, dict):
            rows[(section, "")] = ("map", 0, "{}")
            # Unordered, as `state.json` was: entries come back sorted by id, and
            # removing one does not renumber, and so rewrite, all the others.
            for item, entry in value.items():
                rows[(section, str(item))] = ("map", 0, _body(entry))
        elif section in LIST_SECTIONS and isinstance(value, list):
            rows[(section, "")] = ("list", 0, "[]")
            for position, entry in enumerate(value, start=1):
                rows[(section, f"{position:06d}")] = ("list", position, _body(entry))
        else:
            rows[(section, "")] = ("value", 0, _body(value))
    return rows


def _assemble(rows: dict[Key, Row]) -> dict[str, Any]:
    """Records back into the document `_shards` took apart."""
    data: dict[str, Any] = {}
    ordered = sorted(rows.items(), key=lambda pair: (pair[0][0], pair[1][1], pair[0][1]))
    for (section, item), (kind, _, body) in ordered:
        if not item:
            data[section] = json.loads(body)
        elif kind == "map":
            data.setdefault(section, {})[item] = json.loads(body)
        else:
            data.setdefault(section, []).append(json.loads(body))
    return data


def _connect(target: Path) -> sqlite3.Connection:
    """A connection in autocommit mode: every transaction here is explicit.

    The rollback journal, not WAL: a reader then never writes anything — no
    `-wal` or `-shm` files, no checkpoint on close — which is what lets
    `writ serve` read a live project without touching it, and the file's mtime
    still moves on every commit for the watcher.
    """
    connection = sqlite3.connect(target, timeout=LOCK_TIMEOUT_SECONDS, isolation_level=None)
    connection.execute(f"PRAGMA synchronous = {'FULL' if fsync_enabled() else 'OFF'}")
    return connection


def _read_rows(target: Path) -> dict[Key, Row]:
    try:
        connection = _connect(target)
        try:
            cursor = connection.execute(
                "SELECT section, item, kind, position, body FROM record"
            )
            return {
                (section, item): (kind, position, body)
                for section, item, kind, position, body in cursor
            }
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise WritError(f"corrupt state store {target}: {exc}") from exc


def _commit(
    target: Path, data: dict[str, Any], before: dict[Key, Row]
) -> dict[Key, Row]:
    """Write the records that differ from `before`, in one transaction.

    `BEGIN IMMEDIATE` takes the write lock up front and the commit is atomic and
    durable (with `synchronous = FULL`): a crash leaves the previous project or
    this one, never a mix of records from both.
    """
    rows = _shards(data)
    changed = [
        (section, item, *row)
        for (section, item), row in rows.items()
        if before.get((section, item)) != row
    ]
    gone = [key for key in before if key not in rows]
    if not changed and not gone and target.exists():
        return rows
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        connection = _connect(target)
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(_SCHEMA)
                connection.executemany(
                    "DELETE FROM record WHERE section = ? AND item = ?", gone
                )
                connection.executemany(
                    "INSERT OR REPLACE INTO record (section, item, kind, position, body) "
                    "VALUES (?, ?, ?, ?, ?)",
                    changed,
                )
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise WritError(f"could not write the state store {target}: {exc}") from exc
    return rows


def fsync_enabled() -> bool:
    """Whether a committed write is flushed to the device before returning."""
    return os.environ.get(FSYNC_ENV, "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def sweep_temporaries(root: str | os.PathLike[str]) -> list[Path]:
    """Delete temporary state files orphaned by a crash. Returns what it removed.

    The single-file store of schema 1 wrote through a temporary, and a write that
    died before its rename left a `state.json.tmp.*` behind. It is harmless — nothing reads it — but it accumulates, and a store
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
    """Replace the stored document with `data`, writing only what differs."""
    target = state_file(root)
    _commit(target, data, _read_rows(target) if target.exists() else {})


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
    """Load, mutate, save — under a lock, so concurrent writers serialize.

    Only the records the block changed are written. A change to the plan's
    revision is recorded as a changeset in the same commit, and a change to
    the plan refreshes its files afterwards, whoever made it.
    """
    with _lock(root):
        data, before = _load(root)
        yield data
        if data.get("plan", {}).get("revision") != _revision(before):
            _record_revision(data, before)
        after = _commit(state_file(root), data, before)
        if any(
            after.get(key) != before.get(key)
            for key in set(after) | set(before)
            if key[0] in VIEW_SECTIONS
        ):
            _refresh_views(root, data)


#: the sections the plan's files are generated from
VIEW_SECTIONS = ("tasks", "plan", "requirements", "milestones", "design_docs")


def _revision(rows: dict[Key, Row]) -> Any:
    row = rows.get(("plan", ""))
    return json.loads(row[2]).get("revision") if row else None


def _record_revision(data: dict[str, Any], before: dict[Key, Row]) -> None:
    """Append what the new revision changed, field by field, to `plan_revisions`."""
    from . import planfiles  # the plan's shape lives there; it imports this module

    previous = {
        item: json.loads(body)
        for (section, item), (_, _, body) in before.items()
        if section == "tasks" and item
    }
    plan = data.get("plan") or {}
    changes = planfiles.changeset(previous, data.get("tasks", {}))
    data.setdefault("plan_revisions", []).append(
        {
            "revision": plan.get("revision"),
            "base_revision": _revision(before),
            "by": plan.pop("revised_by", "") or "",
            "at": utcnow(),
            **changes,
        }
    )


def _refresh_views(root: str | os.PathLike[str], data: dict[str, Any]) -> None:
    """Regenerate the plan's files. Views: a failure here loses nothing."""
    from . import planfiles

    try:
        planfiles.refresh(root, data)
    except (OSError, WritError):
        pass


# ------------------------------------------------------------------ migration


def migrate(root: str | os.PathLike[str], *, prune: bool = False) -> dict[str, Any]:
    """Convert a schema 1 `state.json` into the record store.

    The old file is left where it was, and ignored from then on, until `prune`
    removes it: a conversion that turns out wrong is then one `rm store.db`
    away from undone.
    """
    legacy = legacy_file(root)
    target = state_file(root)
    report: dict[str, Any] = {"converted": False, "pruned": [], "store": target}
    with _lock(root):
        if not target.exists():
            if not legacy.exists():
                raise WritError(
                    f"no Writ project at {Path(root).expanduser()} (run `writ init` first)"
                )
            try:
                data = json.loads(legacy.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise WritError(f"corrupt state file {legacy}: {exc}") from exc
            version = data.get("schema_version")
            if version != LEGACY_SCHEMA_VERSION:
                raise WritError(
                    f"{legacy} has schema {version!r}; `writ migrate` converts "
                    f"schema {LEGACY_SCHEMA_VERSION}"
                )
            data["schema_version"] = SCHEMA_VERSION
            _commit(target, _defaults(data), {})
            report["converted"] = True
        if prune:
            for path in [legacy, *sorted(store_dir(root).glob(f"{STATE_FILENAME}.tmp.*"))]:
                if path.exists():
                    path.unlink()
                    report["pruned"].append(path)
    return report


def dump(root: str | os.PathLike[str]) -> str:
    """The whole project as one JSON document, the way `state.json` held it."""
    return json.dumps(load(root), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
