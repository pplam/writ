"""The plan as a reviewed artifact: requirements, coverage, findings, approval.

The claim these tests are about is that a structurally valid plan is not
necessarily a good one. A plan can name every dependency, fence every path and
still leave an obligation from the design with nothing implementing it — so the
inventory, the coverage derived from it, and the approval step that reads both are
the parts worth pinning down.
"""
from __future__ import annotations

import json

import pytest

from writ import plancheck, plans, state
from writ.state import WritError


PLAN = {
    "requirements": [
        {
            "id": "REQ-001",
            "text": "Appends to the event log are atomic",
            "source": "Storage",
            "priority": "must",
            "status": "planned",
        },
        {
            "id": "REQ-002",
            "text": "A replay of the log reproduces the projection byte-for-byte",
            "source": "Projection",
            "priority": "must",
            "status": "planned",
        },
        {
            "id": "REQ-003",
            "text": "The operator can see queue depth",
            "source": "Operations",
            "priority": "should",
            "status": "planned",
        },
        {
            "id": "REQ-004",
            "text": "Authentication is already handled by the gateway",
            "source": "Security",
            "priority": "must",
            "status": "existing",
            "evidence": "gateway/auth_test.go covers it",
        },
        {
            "id": "REQ-005",
            "text": "Multi-region replication",
            "source": "Scale",
            "priority": "may",
            "status": "out-of-scope",
            "reason": "the design defers it to a later phase",
        },
    ],
    "milestones": [
        {
            "id": "M01",
            "title": "Storage",
            "tasks": [
                {
                    "id": "M01-001",
                    "title": "Add the append-only event log writer",
                    "design_section": "Storage",
                    "requirement_ids": ["REQ-001"],
                    "acceptances": [
                        "a failing test in store/log_test.go reproduces a torn append",
                        "`go test ./store` passes with appends fsync'd in order",
                    ],
                    "allowed": ["store/log.go", "store/log_test.go"],
                },
                {
                    "id": "M01-002",
                    "title": "Build the projection from the log",
                    "design_section": "Projection",
                    "requirement_ids": ["REQ-002"],
                    "depends_on": ["M01-001"],
                    "acceptances": [
                        "`go test ./store -run Replay` reproduces the projection",
                        "store/projection.go rebuilds from an empty state",
                    ],
                    "allowed": ["store/projection.go"],
                },
            ],
        }
    ],
}


@pytest.fixture
def inventoried(writ, project, design, tmp_path):
    """A committed plan that declares a requirement inventory."""
    artifact = tmp_path / "plan.json"
    artifact.write_text(json.dumps(PLAN), encoding="utf-8")
    writ("init")
    writ("plan", str(design), "--from-plan", str(artifact))
    return writ


# --------------------------------------------------------------------------
# the inventory


def test_the_inventory_is_committed_with_the_plan(inventoried, project):
    data = state.load(project)
    assert sorted(plans.requirements(data)) == [
        "REQ-001", "REQ-002", "REQ-003", "REQ-004", "REQ-005",
    ]


def test_a_task_records_which_requirements_it_covers(inventoried, project):
    data = state.load(project)
    assert data["tasks"]["M01-001"]["requirement_ids"] == ["REQ-001"]


# --------------------------------------------------------------------------
# coverage


def test_coverage_is_derived_from_the_graph_not_stored(inventoried, project):
    data = state.load(project)
    rows = {row["id"]: row for row in plans.coverage(data)}
    assert rows["REQ-001"]["tasks"] == ["M01-001"]
    assert rows["REQ-002"]["tasks"] == ["M01-002"]
    # Nothing is stored under `requirements` that repeats this.
    assert "tasks" not in plans.requirements(data)["REQ-001"]


def test_a_requirement_nothing_implements_is_uncovered(inventoried, project):
    data = state.load(project)
    rows = {row["id"]: row for row in plans.coverage(data)}
    assert rows["REQ-003"]["state"] == "uncovered"
    assert plans.uncovered(data) == ["REQ-003"]


def test_an_already_satisfied_requirement_needs_its_evidence(inventoried, project):
    data = state.load(project)
    rows = {row["id"]: row for row in plans.coverage(data)}
    # Declared `existing` with evidence: satisfied without a task, which is the
    # point of the status. Without the evidence it would read `unevidenced`.
    assert rows["REQ-004"]["state"] == "satisfied"
    with state.transaction(project) as live:
        plans.requirements(live)["REQ-004"]["evidence"] = ""
    rows = {row["id"]: row for row in plans.coverage(state.load(project))}
    assert rows["REQ-004"]["state"] == "unevidenced"


def test_an_out_of_scope_requirement_is_not_a_hole(inventoried, project):
    data = state.load(project)
    rows = {row["id"]: row for row in plans.coverage(data)}
    assert rows["REQ-005"]["state"] == "out-of-scope"
    assert "REQ-005" not in plans.uncovered(data)


def test_a_requirement_is_not_satisfied_until_a_reviewer_accepts_it(
    inventoried, project
):
    with state.transaction(project) as data:
        data["tasks"]["M01-001"]["status"] = "awaiting-review"
    rows = {row["id"]: row for row in plans.coverage(state.load(project))}
    # The implementing agent has reported. Nothing independent has checked it, so
    # this is in-progress — the distinction the review split exists to keep.
    assert rows["REQ-001"]["state"] == "in-progress"

    with state.transaction(project) as data:
        data["tasks"]["M01-001"]["status"] = "completed"
    rows = {row["id"]: row for row in plans.coverage(state.load(project))}
    assert rows["REQ-001"]["state"] == "satisfied"


def test_an_uncovered_must_requirement_blocks_approval(inventoried, project, writ):
    # REQ-003 is a `should`, so it is a warning. Promote it and re-check.
    with state.transaction(project) as data:
        plans.requirements(data)["REQ-003"]["priority"] = "must"
        plans.run_check(data, root=project)
    data = state.load(project)
    assert plans.plan_status(data)["status"] == "needs-approval"
    assert not plans.runnable(data)
    code, out, err = writ("check")
    assert code == 1
    # A blocking finding goes to stderr, with the rest of its list.
    assert "REQ-003" in err
    assert "no task covers it" in err


def test_a_plan_with_no_inventory_has_nothing_to_trace(approved, project):
    data = state.load(project)
    assert plans.coverage(data) == []
    # And the coverage checks stay quiet rather than faulting every task.
    findings = plancheck.check(plancheck.from_state(data, root=project))
    assert not [f for f in findings if f.category.startswith("uncovered")]


# --------------------------------------------------------------------------
# the findings ledger


def test_a_finding_keeps_its_id_across_re_checks(inventoried, project):
    with state.transaction(project) as data:
        data["tasks"]["M01-001"]["acceptances"] = [
            {"text": "it works", "status": "pending"}
        ]
        first = plans.run_check(data, root=project)
        ids = {f.id for f in first if f.severity == "error"}
        assert ids
        second = plans.run_check(data, root=project)
    assert {f.id for f in second if f.severity == "error"} == ids


def test_a_finding_that_goes_away_is_resolved_not_deleted(inventoried, project):
    with state.transaction(project) as data:
        data["tasks"]["M01-001"]["acceptances"] = [
            {"text": "it works", "status": "pending"}
        ]
        plans.run_check(data, root=project)
        vague = [
            record["id"]
            for record in plans.finding_records(data)
            if record["category"] == "vague-acceptance"
        ]
        assert vague
        # Fix the plan and check again.
        data["tasks"]["M01-001"]["acceptances"] = [
            {"text": "`go test ./store` passes with appends fsync'd", "status": "pending"},
            {"text": "a failing test in store/log_test.go reproduces a torn append",
             "status": "pending"},
        ]
        plans.run_check(data, root=project)
        records = {record["id"]: record for record in plans.finding_records(data)}
    for finding_id in vague:
        # Still readable, marked resolved: the objection was real and the history
        # of it is part of the plan's record.
        assert records[finding_id]["disposition"] == "resolved"


def test_only_a_blocking_finding_holds_the_plan(inventoried, project):
    data = state.load(project)
    # The committed plan has warnings (the design's generic criteria) and still
    # approved itself, because advisory findings are not a veto.
    assert plans.plan_status(data)["status"] == "approved"
    assert plans.runnable(data)


# --------------------------------------------------------------------------
# approval


def test_run_refuses_a_plan_that_has_not_been_checked(inventoried, project, writ):
    with state.transaction(project) as data:
        plans.set_status(data, "draft")
    code, _, err = writ("run", "--agent", "false")
    assert code == 2
    assert "writ check" in err


def test_approving_records_who_and_bumps_the_revision(inventoried, project, writ):
    before = plans.revision(state.load(project))
    code, out, _ = writ("approve", "--by", "a-person", "--reason", "read it through")
    assert code == 0
    record = plans.plan_status(state.load(project))
    assert record["approved_by"] == "a-person"
    assert record["approval_note"] == "read it through"
    assert plans.revision(state.load(project)) > before


def test_a_complete_plan_cannot_be_approved_again(inventoried, project):
    with state.transaction(project) as data:
        plans.set_status(data, "complete")
        with pytest.raises(WritError, match="already complete"):
            plans.approve(data)


def test_force_leaves_the_findings_readable_rather_than_deleting_them(
    inventoried, project, writ
):
    with state.transaction(project) as data:
        data["tasks"]["M01-001"]["acceptances"] = [
            {"text": "it works", "status": "pending"}
        ]
        plans.run_check(data, root=project)
    writ("approve", "--force", "--reason", "known gap, shipping anyway")
    records = plans.finding_records(state.load(project))
    accepted = [r for r in records if r["disposition"] == "accepted"]
    assert accepted
    assert all(r["reason"] == "known gap, shipping anyway" for r in accepted)
    # Nothing was removed from the ledger.
    assert all(r.get("message") for r in records)


# --------------------------------------------------------------------------
# the commands


def test_check_exits_nonzero_only_when_something_blocks(inventoried, writ, project):
    assert writ("check")[0] == 0
    with state.transaction(project) as data:
        data["tasks"]["M01-001"]["acceptances"] = [
            {"text": "it works", "status": "pending"}
        ]
    assert writ("check")[0] == 1


def test_coverage_prints_a_row_per_requirement(inventoried, writ):
    code, out, _ = writ("coverage")
    assert code == 0
    for req in ("REQ-001", "REQ-002", "REQ-003", "REQ-004", "REQ-005"):
        assert req in out


def test_coverage_uncovered_shows_only_the_holes(inventoried, writ):
    code, out, _ = writ("coverage", "--uncovered")
    assert code == 0
    assert "REQ-003" in out
    assert "REQ-001" not in out


def test_list_requirements_names_what_covers_each_one(inventoried, writ):
    code, out, _ = writ("--json", "list", "requirements")
    rows = {row["id"]: row for row in json.loads(out)}
    assert rows["REQ-001"]["tasks"] == ["M01-001"]
    assert rows["REQ-003"]["state"] == "uncovered"


def test_list_findings_shows_the_ledger(inventoried, writ, project):
    with state.transaction(project) as data:
        data["tasks"]["M01-001"]["acceptances"] = [
            {"text": "it works", "status": "pending"}
        ]
        plans.run_check(data, root=project)
    code, out, _ = writ("--json", "list", "findings")
    records = json.loads(out)
    assert any(r["category"] == "vague-acceptance" for r in records)


def test_a_gate_is_not_held_to_a_requirement_the_plan_disowns(inventoried, project):
    from writ import gates

    data = state.load(project)
    claimed = data["tasks"]["G-FINAL"]["requirement_ids"]
    # In the inventory so a reader can see it was considered and left out. Not
    # something a gate can be asked to judge.
    assert "REQ-005" not in claimed
    assert "REQ-003" in claimed
    assert gates.answerable_requirements(data) == claimed
