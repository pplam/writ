import pytest

from writ import model, state
from writ.state import WritError


def base_state():
    data = state.empty_state()
    model.add_milestone(data, milestone_id="M01", title="First")
    model.add_task(
        data,
        task_id="M01-001",
        title="A",
        milestone="M01",
        acceptances=["bar one"],
    )
    model.add_task(
        data,
        task_id="M01-002",
        title="B",
        milestone="M01",
        depends_on=["M01-001"],
        acceptances=["bar two"],
    )
    return data


def test_ready_requires_completed_dependencies():
    data = base_state()
    assert model.effective_status(data, data["tasks"]["M01-001"]) == "ready"
    assert model.effective_status(data, data["tasks"]["M01-002"]) == "planned"
    assert [t["id"] for t in model.ready_tasks(data)] == ["M01-001"]


def test_start_is_refused_while_blocked():
    data = base_state()
    with pytest.raises(WritError, match="blocked by incomplete dependencies"):
        model.set_status(data, "M01-002", "running")


def test_force_overrides_dependency_gate():
    data = base_state()
    model.set_status(data, "M01-002", "running", force=True)
    assert data["tasks"]["M01-002"]["status"] == "running"


def test_complete_requires_acceptances_passed():
    data = base_state()
    with pytest.raises(WritError, match="unmet acceptance criteria"):
        model.set_status(data, "M01-001", "completed", allow_judged=True)
    model.set_acceptance(data, "M01-001", 1, "passed")
    model.set_status(data, "M01-001", "completed", allow_judged=True)
    assert data["tasks"]["M01-001"]["status"] == "completed"


def test_completing_a_dependency_unblocks_the_next_task():
    data = base_state()
    model.set_acceptance(data, "M01-001", 1, "passed")
    model.set_status(data, "M01-001", "completed", allow_judged=True)
    assert model.effective_status(data, data["tasks"]["M01-002"]) == "ready"


def test_milestone_status_is_derived():
    data = base_state()
    assert data["milestones"]["M01"]["status"] == "ready"
    model.set_acceptance(data, "M01-001", 1, "passed")
    model.set_status(data, "M01-001", "completed", allow_judged=True)
    assert data["milestones"]["M01"]["status"] == "in-progress"
    model.set_acceptance(data, "M01-002", 1, "passed")
    model.set_status(data, "M01-002", "completed", allow_judged=True)
    assert data["milestones"]["M01"]["status"] == "completed"


def test_failed_task_surfaces_in_milestone_status():
    data = base_state()
    model.set_status(data, "M01-001", "failed")
    assert data["milestones"]["M01"]["status"] == "failed"


def test_evidence_is_appended_not_replaced():
    data = base_state()
    model.set_status(data, "M01-001", "running", evidence="first")
    model.set_status(data, "M01-001", "planned", evidence="second")
    texts = [item["text"] for item in data["tasks"]["M01-001"]["evidence"]]
    assert texts == ["first", "second"]


def test_acceptance_index_is_validated():
    data = base_state()
    with pytest.raises(WritError, match="out of range"):
        model.set_acceptance(data, "M01-001", 5, "passed")


def test_unknown_ids_are_rejected():
    data = base_state()
    with pytest.raises(WritError, match="unknown task"):
        model.get_task(data, "nope")
    with pytest.raises(WritError, match="unknown id"):
        model.find(data, "nope")


def test_duplicate_task_is_rejected():
    data = base_state()
    with pytest.raises(WritError, match="already exists"):
        model.add_task(data, task_id="M01-001", title="dup", milestone="M01")


def test_dependency_cycle_is_detected():
    data = base_state()
    data["tasks"]["M01-001"]["depends_on"] = ["M01-002"]
    with pytest.raises(WritError, match="dependency cycle"):
        model.check_dag(data)


def test_dangling_dependency_is_detected():
    data = base_state()
    data["tasks"]["M01-001"]["depends_on"] = ["ghost"]
    with pytest.raises(WritError, match="unknown task"):
        model.check_dag(data)
