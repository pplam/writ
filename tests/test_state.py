import json

import pytest

from writ import state
from writ.state import WritError


def test_initialize_creates_store(project):
    location = state.initialize(project)
    assert location == project / ".writ"
    assert state.state_file(project).exists()
    assert state.runs_dir(project).exists()


def test_initialize_refuses_to_clobber(project):
    state.initialize(project)
    with pytest.raises(WritError, match="already initialized"):
        state.initialize(project)
    state.initialize(project, force=True)


def test_load_without_init_is_an_error(project):
    with pytest.raises(WritError, match="no Writ project"):
        state.load(project)


def test_unsupported_schema_is_refused(project):
    state.initialize(project)
    data = state.load(project)
    data["schema_version"] = 999
    state.save(project, data)
    with pytest.raises(WritError, match="schema"):
        state.load(project)


def test_corrupt_state_is_reported(project):
    state.initialize(project)
    state.state_file(project).write_text("{not json", encoding="utf-8")
    with pytest.raises(WritError, match="corrupt"):
        state.load(project)


def test_transaction_persists_changes(project):
    state.initialize(project)
    with state.transaction(project) as data:
        data["milestones"]["M01"] = {"id": "M01", "title": "x", "status": "planned", "tasks": []}
    assert "M01" in state.load(project)["milestones"]


def test_transaction_releases_the_lock(project):
    """Released means "the next writer can have it", not "the file is gone".

    Under `flock` the lock lives on the open file, so the path is deliberately
    permanent: a holder that unlinked it on release would let the next writer
    create a fresh file and lock *that*, which is two processes holding two
    inodes and one `state.json`.
    """
    state.initialize(project)
    with state.transaction(project):
        pass
    # a second and third transaction must not block
    with state.transaction(project):
        pass
    with state.transaction(project) as data:
        data["counters"]["decision"] = 7
    assert state.load(project)["counters"]["decision"] == 7
    # and the released lock names nobody
    lock = state.store_dir(project) / state.LOCK_FILENAME
    assert state.lock_owner(lock) is None


def test_write_is_atomic_and_sorted(project):
    state.initialize(project)
    raw = state.state_file(project).read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert json.loads(raw)["schema_version"] == state.SCHEMA_VERSION
    leftovers = list(state.store_dir(project).glob("state.json.tmp*"))
    assert leftovers == []


def test_stale_lock_is_broken(project, monkeypatch):
    """The no-`flock` fallback, which is the only path that breaks a lock at all."""
    state.initialize(project)
    monkeypatch.setattr(state, "fcntl", None)
    lock = state.store_dir(project) / state.LOCK_FILENAME
    lock.write_text("999999 old\n", encoding="utf-8")
    monkeypatch.setattr(state, "_lock_is_stale", lambda path: True)
    with state.transaction(project) as data:
        data["counters"]["decision"] = 1
    assert state.load(project)["counters"]["decision"] == 1
