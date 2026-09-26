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


def test_the_store_is_one_file_and_the_dump_is_the_document(project):
    state.initialize(project)
    assert sorted(p.name for p in state.store_dir(project).iterdir() if p.is_file()) == [
        state.DB_FILENAME
    ]
    raw = state.dump(project)
    assert raw.endswith("\n")
    assert json.loads(raw)["schema_version"] == state.SCHEMA_VERSION


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


def _legacy(project, **extra):
    """A project as writ used to keep it: one state.json."""
    document = state.empty_state()
    document["schema_version"] = state.LEGACY_SCHEMA_VERSION
    document.update(extra)
    store = state.store_dir(project)
    store.mkdir(parents=True)
    state.legacy_file(project).write_text(json.dumps(document), encoding="utf-8")
    return document


def test_an_old_project_is_refused_until_migrated(project):
    _legacy(project, tasks={"T-1": {"id": "T-1", "title": "kept", "status": "planned"}})
    with pytest.raises(WritError, match="writ migrate"):
        state.load(project)

    report = state.migrate(project)
    assert report["converted"] and report["pruned"] == []
    assert state.load(project)["tasks"]["T-1"]["title"] == "kept"
    assert state.legacy_file(project).exists()  # kept, and ignored

    again = state.migrate(project, prune=True)
    assert not again["converted"]
    assert again["pruned"] == [state.legacy_file(project)]
    assert state.load(project)["tasks"]["T-1"]["title"] == "kept"


def test_migrate_refuses_a_schema_it_does_not_know(project):
    _legacy(project, schema_version=7)
    with pytest.raises(WritError, match="schema 7"):
        state.migrate(project)
    assert not state.state_file(project).exists()


def test_a_revision_change_is_recorded_as_a_changeset(project):
    from writ import plans

    state.initialize(project)
    with state.transaction(project) as data:
        data["tasks"]["T-1"] = {"id": "T-1", "title": "old", "status": "planned"}
        plans.bump(data, by="planner")
    with state.transaction(project) as data:
        data["tasks"]["T-1"]["title"] = "new"
        data["tasks"]["T-1"]["status"] = "running"  # progress, not plan
        data["tasks"]["T-2"] = {"id": "T-2", "title": "added", "status": "planned"}
        plans.bump(data, by="GR-1 (gate repair)")
    revisions = state.load(project)["plan_revisions"]
    assert [entry["by"] for entry in revisions] == ["planner", "GR-1 (gate repair)"]
    last = revisions[-1]
    assert list(last["added"]) == ["T-2"] and last["added"]["T-2"]["title"] == "added"
    assert set(last["modified"]) == {"T-1"}
    assert "status" not in last["modified"]["T-1"]
    assert "revised_by" not in state.load(project)["plan"]


def test_a_plan_change_by_anyone_rewrites_the_plan_files(project):
    """A gate repair changes the plan outside planning; the files follow it."""
    from writ import planfiles, plans

    state.initialize(project)
    with state.transaction(project) as data:
        data["tasks"]["T-1"] = {"id": "T-1", "title": "old", "status": "planned"}
        plans.bump(data, by="planner")
        planfiles.export(project, data)
    with state.transaction(project) as data:
        data["tasks"]["T-1"]["title"] = "repaired"
        plans.bump(data, by="GR-1 (gate repair)")
    data = state.load(project)
    feature = planfiles.features_dir(project, data) / "T-1.json"
    assert json.loads(feature.read_text())["title"] == "repaired"
    assert planfiles.drift(project, data) == []

    feature.write_text("{}")
    assert planfiles.drift(project, data) == [feature]


def test_state_dump_prints_the_document(project):
    from conftest import run

    state.initialize(project)
    code, out, _ = run("--root", str(project), "state", "dump")
    assert code == 0
    assert json.loads(out)["schema_version"] == state.SCHEMA_VERSION


def test_writ_migrate_converts_and_prunes(project):
    from conftest import run

    _legacy(project)
    code, out, _ = run("--root", str(project), "migrate", "--prune")
    assert code == 0, out
    assert "converted state.json" in out and "removed .writ/state.json" in out
    assert not state.legacy_file(project).exists()
    assert state.load(project)["schema_version"] == state.SCHEMA_VERSION
