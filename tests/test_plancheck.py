"""Deterministic plan checks: the bad plans that used to pass validation."""
from pathlib import Path

import pytest

from writ import plancheck
from writ.plancheck import Finding, Item, Requirement, Snapshot
from writ.planner import PlannedMilestone, PlannedTask


def snapshot(*items: Item, requirements=(), root=None, committed=True) -> Snapshot:
    return Snapshot(
        items=list(items),
        requirements=list(requirements),
        root=root,
        committed=committed,
    )


def task(task_id: str, **kwargs) -> Item:
    kwargs.setdefault("title", f"Task {task_id}")
    kwargs.setdefault(
        "acceptances", ["`pytest -q tests/test_a.py` passes", "it rejects bad input"]
    )
    return Item(id=task_id, **kwargs)


def categories(findings) -> list[str]:
    return [finding.category for finding in findings]


def find(findings, category: str) -> Finding:
    matches = [finding for finding in findings if finding.category == category]
    assert matches, f"no {category} finding in {categories(findings)}"
    return matches[0]


# --------------------------------------------------------------------------
# the findings shape


def test_finding_rejects_an_unknown_severity():
    with pytest.raises(ValueError):
        Finding(severity="critical", category="x", message="y")


def test_only_errors_block():
    assert Finding(severity="error", category="x", message="y").blocking
    assert not Finding(severity="warning", category="x", message="y").blocking
    assert not Finding(severity="note", category="x", message="y").blocking


def test_findings_round_trip_through_a_dict():
    finding = Finding(
        severity="error",
        category="missing-coverage",
        message="no task covers it",
        where="REQ-002",
        suggested_action="add a task",
        requirement_ids=["REQ-002"],
        source="coverage-critic",
        id="F-001",
    )
    assert Finding.from_dict(finding.to_dict()) == finding


def test_findings_sort_worst_first():
    findings = plancheck.sort_findings(
        [
            Finding(severity="note", category="a", message="n"),
            Finding(severity="error", category="z", message="e"),
            Finding(severity="warning", category="a", message="w"),
        ]
    )
    assert [f.severity for f in findings] == ["error", "warning", "note"]


def test_tally_counts_by_severity():
    counts = plancheck.tally(
        [
            Finding(severity="error", category="a", message="1"),
            Finding(severity="error", category="b", message="2"),
            Finding(severity="note", category="c", message="3"),
        ]
    )
    assert counts == {"error": 2, "warning": 0, "note": 1, "total": 3}


# --------------------------------------------------------------------------
# acceptance criteria


def test_duplicate_criteria_are_an_error():
    findings = plancheck.check(
        snapshot(
            task(
                "M01-001",
                acceptances=["`pytest -q` passes", "`PYTEST -q`   passes"],
            )
        )
    )
    assert find(findings, "duplicate-acceptance").blocking


def test_one_criterion_is_too_thin_to_judge():
    findings = plancheck.check(
        snapshot(task("M01-001", acceptances=["`pytest -q tests/test_a.py` passes"]))
    )
    assert find(findings, "thin-acceptance").severity == "warning"


def test_too_many_criteria_reads_as_two_tasks():
    findings = plancheck.check(
        snapshot(task("M01-001", acceptances=[f"it returns {n}" for n in range(8)]))
    )
    assert find(findings, "wide-acceptance").severity == "warning"


# --------------------------------------------------------------------------
# dependencies


def test_a_self_dependency_is_an_error():
    findings = plancheck.check(snapshot(task("M01-001", depends_on=["M01-001"])))
    assert find(findings, "self-dependency").blocking


def test_a_repeated_edge_is_a_warning():
    findings = plancheck.check(
        snapshot(task("M01-001"), task("M01-002", depends_on=["M01-001", "M01-001"]))
    )
    assert find(findings, "duplicate-dependency").severity == "warning"


def test_an_edge_to_nothing_is_an_error_once_committed():
    findings = plancheck.check(snapshot(task("M01-002", depends_on=["M01-001"])))
    assert find(findings, "unknown-dependency").blocking


def test_an_unresolved_ref_is_not_judged_before_commit():
    findings = plancheck.check(
        snapshot(task("T-b", depends_on=["T-a"]), committed=False)
    )
    assert "unknown-dependency" not in categories(findings)


# --------------------------------------------------------------------------
# requirement coverage


def requirement(req_id: str, **kwargs) -> Requirement:
    kwargs.setdefault("text", f"The system must do {req_id}.")
    return Requirement(id=req_id, **kwargs)


def test_coverage_is_not_judged_when_the_plan_states_no_requirements():
    findings = plancheck.check(snapshot(task("M01-001")))
    assert "missing-coverage" not in categories(findings)
    assert "unjustified-task" not in categories(findings)


def test_an_uncovered_must_requirement_is_an_error():
    findings = plancheck.check(
        snapshot(
            task("M01-001", requirement_ids=["REQ-001"]),
            requirements=[requirement("REQ-001"), requirement("REQ-002")],
        )
    )
    finding = find(findings, "missing-coverage")
    assert finding.blocking
    assert finding.requirement_ids == ["REQ-002"]


def test_an_uncovered_should_requirement_is_only_a_warning():
    findings = plancheck.check(
        snapshot(
            task("M01-001", requirement_ids=["REQ-001"]),
            requirements=[requirement("REQ-001"), requirement("REQ-002", priority="should")],
        )
    )
    assert find(findings, "missing-coverage").severity == "warning"


def test_a_task_covering_nothing_is_a_warning():
    findings = plancheck.check(
        snapshot(
            task("M01-001", requirement_ids=["REQ-001"]),
            task("M01-002"),
            requirements=[requirement("REQ-001")],
        )
    )
    assert find(findings, "unjustified-task").where == "M01-002"


def test_a_reference_to_a_requirement_that_does_not_exist_is_an_error():
    findings = plancheck.check(
        snapshot(
            task("M01-001", requirement_ids=["REQ-009"]),
            requirements=[requirement("REQ-001")],
        )
    )
    assert find(findings, "unknown-requirement").blocking


def test_already_implemented_needs_evidence():
    findings = plancheck.check(
        snapshot(
            task("M01-001", requirement_ids=["REQ-001"]),
            requirements=[requirement("REQ-001"), requirement("REQ-002", status="existing")],
        )
    )
    assert find(findings, "unevidenced-requirement").blocking


def test_already_implemented_with_evidence_needs_no_task():
    findings = plancheck.check(
        snapshot(
            task("M01-001", requirement_ids=["REQ-001"]),
            requirements=[
                requirement("REQ-001"),
                requirement(
                    "REQ-002", status="existing", evidence="tests/test_state.py::test_replay"
                ),
            ],
        )
    )
    assert "missing-coverage" not in categories(findings)
    assert "unevidenced-requirement" not in categories(findings)


def test_out_of_scope_needs_a_reason():
    findings = plancheck.check(
        snapshot(
            task("M01-001", requirement_ids=["REQ-001"]),
            requirements=[requirement("REQ-001"), requirement("REQ-002", status="out-of-scope")],
        )
    )
    assert find(findings, "undeclared-exclusion").blocking


def test_out_of_scope_with_a_reason_is_accepted():
    findings = plancheck.check(
        snapshot(
            task("M01-001", requirement_ids=["REQ-001"]),
            requirements=[
                requirement("REQ-001"),
                requirement("REQ-002", status="out-of-scope", reason="shipping v2 first"),
            ],
        )
    )
    assert not plancheck.blocking(findings)


def test_an_unknown_priority_is_flagged():
    findings = plancheck.check(
        snapshot(
            task("M01-001", requirement_ids=["REQ-001"]),
            requirements=[requirement("REQ-001", priority="critical")],
        )
    )
    assert find(findings, "requirement-shape").severity == "warning"


# --------------------------------------------------------------------------
# features and contracts


def feature(feature_id: str, **kwargs) -> Item:
    kwargs.setdefault("title", f"Feature {feature_id}")
    kwargs.setdefault("acceptances", ["it stores", "it replays", "it rejects a torn write"])
    kwargs.setdefault("owns", [f"pkg/{feature_id.lower()}/"])
    return Item(id=feature_id, feature=True, **kwargs)


def test_a_consumed_interface_nobody_provides_is_a_contract_gap():
    findings = plancheck.check(
        snapshot(feature("FT-001", provides=["Log: append"]), feature("FT-002", consumes=["Clock"]))
    )
    gap = find(findings, "contract-gap")
    assert gap.blocking and gap.where == "FT-002" and "clock" in gap.message


def test_an_interface_provided_twice_is_a_contract_gap():
    findings = plancheck.check(
        snapshot(feature("FT-001", provides=["Log"]), feature("FT-002", provides=["log: again"]))
    )
    assert find(findings, "contract-gap").where == "FT-002"


def test_closed_contracts_raise_nothing():
    findings = plancheck.check(
        snapshot(
            feature("FT-001", provides=["Log: append"]),
            feature("FT-002", consumes=["LOG"], depends_on=["FT-001"]),
        )
    )
    assert "contract-gap" not in categories(findings)


def test_a_feature_with_too_few_behaviours_is_thin():
    findings = plancheck.check(snapshot(feature("FT-001", acceptances=["a", "b"])))
    assert find(findings, "thin-acceptance").severity == "warning"


def test_a_feature_carrying_too_much_is_oversized():
    findings = plancheck.check(
        snapshot(feature("FT-001", requirement_ids=[f"REQ-00{n}" for n in range(1, 8)]))
    )
    assert find(findings, "oversized-feature").severity == "warning"


def test_a_feature_that_owns_nothing_is_flagged():
    findings = plancheck.check(snapshot(feature("FT-001", owns=[])))
    assert find(findings, "unowned-feature").where == "FT-001"


def test_too_few_features_is_advisory():
    findings = plancheck.check(snapshot(feature("FT-001"), feature("FT-002")))
    assert find(findings, "feature-count").severity == "warning"


def test_file_paths_are_not_judged_before_the_code_exists(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    findings = plancheck.check(
        snapshot(task("M01-001", allowed=["pkg/nothing_yet.py"]), root=tmp_path)
    )
    assert not {"unknown-path", "suspect-path"} & set(categories(findings))


# --------------------------------------------------------------------------
# adapters


def test_from_plan_reads_the_author_s_own_ids():
    milestone = PlannedMilestone(title="Storage", section="Storage", ref="M01")
    milestone.tasks.append(
        PlannedTask(
            title="Add the log",
            acceptances=["appends are atomic"],
            section="Storage / log",
            ref="T-log",
            allowed=["store/"],
        )
    )
    milestone.tasks.append(
        PlannedTask(title="Unnamed", acceptances=["it returns 0"], section="Storage")
    )
    shot = plancheck.from_plan([milestone])
    assert [item.id for item in shot.items] == ["T-log", "M01#2"]
    assert shot.items[0].allowed == ["store/"]
    assert not shot.committed


def test_from_state_reads_the_committed_graph():
    data = {
        "tasks": {
            "M01-001": {
                "id": "M01-001",
                "title": "Add the log",
                "acceptances": [{"text": "appends are atomic", "status": "pending"}],
                "depends_on": [],
                "allowed": ["store/"],
                "forbidden": [],
                "requirement_ids": ["REQ-001"],
                "milestone": "M01",
                "status": "planned",
            }
        },
        "requirements": {
            "REQ-001": {"id": "REQ-001", "text": "Appends are atomic.", "priority": "must"}
        },
    }
    shot = plancheck.from_state(data)
    assert shot.committed
    assert shot.items[0].acceptances == ["appends are atomic"]
    assert shot.items[0].requirement_ids == ["REQ-001"]
    assert shot.requirements[0].id == "REQ-001"


def test_a_cycle_does_not_crash_the_checker():
    findings = plancheck.check(
        snapshot(
            task("M01-001", depends_on=["M01-002"], allowed=["a.py"]),
            task("M01-002", depends_on=["M01-001"], allowed=["a.py"]),
        )
    )
    assert isinstance(findings, list)
