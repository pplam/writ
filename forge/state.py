"""State storage for Forge.

The store is deliberately boring: one JSON document per project, written
atomically, guarded by an advisory lock file so a detached supervisor and an
interactive CLI cannot clobber each other.

Layout under a project root:

    .forge/
        state.json        the whole project: milestones, tasks, runs, decisions
        state.lock        advisory lock, held only for the duration of a write
        decisions.md      human-readable, append-only mirror of the decision log
        runs/<run-id>/    prompt.txt, stdout.log, stderr.log, meta.json
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1

STORE_DIRNAME = ".forge"
STATE_FILENAME = "state.json"
LOCK_FILENAME = "state.lock"
DECISIONS_FILENAME = "decisions.md"
RUNS_DIRNAME = "runs"

LOCK_TIMEOUT_SECONDS = 10.0
LOCK_STALE_SECONDS = 60.0


class ForgeError(Exception):
    """Any expected, user-facing failure."""


def utcnow() -> str:
    """Timestamp used for every recorded event."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def store_dir(root: str | os.PathLike[str]) -> Path:
    """The `.forge` directory for a project root."""
    return Path(root).expanduser() / STORE_DIRNAME


def state_file(root: str | os.PathLike[str]) -> Path:
    return store_dir(root) / STATE_FILENAME


def runs_dir(root: str | os.PathLike[str]) -> Path:
    return store_dir(root) / RUNS_DIRNAME


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
        "decisions": [],
        "counters": {"decision": 0},
    }


def is_initialized(root: str | os.PathLike[str]) -> bool:
    return state_file(root).exists()


def initialize(root: str | os.PathLike[str], force: bool = False) -> Path:
    """Create the store. Refuses to overwrite unless `force`."""
    target = state_file(root)
    if target.exists() and not force:
        raise ForgeError(
            f"already initialized at {store_dir(root)} (use --force to reset)"
        )
    runs_dir(root).mkdir(parents=True, exist_ok=True)
    _write(target, empty_state())
    return store_dir(root)


def load(root: str | os.PathLike[str]) -> dict[str, Any]:
    """Read the project document. Raises if the project is not initialized."""
    target = state_file(root)
    if not target.exists():
        raise ForgeError(
            f"no Forge project at {Path(root).expanduser()} (run `forge init` first)"
        )
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - corrupted store
        raise ForgeError(f"corrupt state file {target}: {exc}") from exc
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ForgeError(
            f"state schema {version!r} is not supported by this Forge build "
            f"(expected {SCHEMA_VERSION})"
        )
    return data


def _write(target: Path, data: dict[str, Any]) -> None:
    """Atomic replace so a reader never observes a half-written document."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + f".tmp.{os.getpid()}")
    tmp.write_text(
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(target)


def save(root: str | os.PathLike[str], data: dict[str, Any]) -> None:
    _write(state_file(root), data)


@contextmanager
def _lock(root: str | os.PathLike[str]) -> Iterator[None]:
    """Advisory inter-process lock around a read-modify-write cycle."""
    path = store_dir(root) / LOCK_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    handle = None
    while handle is None:
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _lock_is_stale(path):
                _release(path)
                continue
            if time.monotonic() > deadline:
                raise ForgeError(
                    f"timed out waiting for the state lock at {path}; "
                    "another Forge process may be writing"
                )
            time.sleep(0.05)
    try:
        os.write(handle, f"{os.getpid()} {utcnow()}\n".encode())
        os.close(handle)
        yield
    finally:
        _release(path)


def _lock_is_stale(path: Path) -> bool:
    try:
        age = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return False
    return age > LOCK_STALE_SECONDS


def _release(path: Path) -> None:
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
