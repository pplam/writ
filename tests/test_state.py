import json

import pytest

from forge import state
from forge.state import ForgeError


def test_initialize_creates_store(project):
    location = state.initialize(project)
    assert location == project / ".forge"
    assert state.state_file(project).exists()
    assert state.runs_dir(project).exists()


def test_initialize_refuses_to_clobber(project):
    state.initialize(project)
    with pytest.raises(ForgeError, match="already initialized"):
        state.initialize(project)
    state.initialize(project, force=True)


def test_load_without_init_is_an_error(project):
    with pytest.raises(ForgeError, match="no Forge project"):
        state.load(project)


def test_unsupported_schema_is_refused(project):
    state.initialize(project)
    data = state.load(project)
    data["schema_version"] = 999
    state.save(project, data)
    with pytest.raises(ForgeError, match="schema"):
        state.load(project)


def test_corrupt_state_is_reported(project):
    state.initialize(project)
    state.state_file(project).write_text("{not json", encoding="utf-8")
    with pytest.raises(ForgeError, match="corrupt"):
        state.load(project)


def test_transaction_persists_changes(project):
    state.initialize(project)
    with state.transaction(project) as data:
        data["milestones"]["M01"] = {"id": "M01", "title": "x", "status": "planned", "tasks": []}
    assert "M01" in state.load(project)["milestones"]


def test_transaction_releases_the_lock(project):
    state.initialize(project)
    with state.transaction(project):
        pass
    assert not (state.store_dir(project) / state.LOCK_FILENAME).exists()
    # a second transaction must not block
    with state.transaction(project):
        pass


def test_write_is_atomic_and_sorted(project):
    state.initialize(project)
    raw = state.state_file(project).read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert json.loads(raw)["schema_version"] == state.SCHEMA_VERSION
    leftovers = list(state.store_dir(project).glob("state.json.tmp*"))
    assert leftovers == []


def test_stale_lock_is_broken(project, monkeypatch):
    state.initialize(project)
    lock = state.store_dir(project) / state.LOCK_FILENAME
    lock.write_text("999999 old\n", encoding="utf-8")
    monkeypatch.setattr(state, "_lock_is_stale", lambda path: True)
    with state.transaction(project) as data:
        data["counters"]["decision"] = 1
    assert state.load(project)["counters"]["decision"] == 1
