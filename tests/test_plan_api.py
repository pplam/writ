"""The read model behind the dashboard's Plan tab.

The dashboard is TypeScript and has no test runner, so what can be held to a test
is the data it renders from. That is most of the risk anyway: a page that draws the
wrong thing is usually a page that was handed the wrong thing.
"""
from __future__ import annotations

import json

import pytest

from writ import api, gates, plans, repair, state
from writ.plancheck import Finding

from tests.test_plans import PLAN


@pytest.fixture
def snapshot(writ, project, design, tmp_path):
    """A plan with a blocking finding, a declined one, and a held gate."""
    artifact = tmp_path / "plan.json"
    artifact.write_text(json.dumps(PLAN), encoding="utf-8")
    writ("init")
    writ("plan", str(design), "--from-plan", str(artifact))
    with state.transaction(project) as data:
        plans.record_findings(
            data,
            [
                Finding(severity="error", category="missing-coverage",
                        message="REQ-003 has no task", where="REQ-003",
                        requirement_ids=["REQ-003"], source="critic:coverage"),
                Finding(severity="warning", category="unstated-edge",
                        message="M01-002 reads M01-001's types with no edge",
                        where="M01-002", source="critic:dependency"),
            ],
            scope="critic",
        )
        ids = [
            record["id"]
            for record in plans.finding_records(data)
            if record.get("source", "").startswith("critic:")
        ]
        plans.dispose(data, ids[1], "declined", actor="pplam", reason="separate reader")
        gate_id = next(task for task in data["tasks"] if task.startswith("G-"))
        request = repair.open_request(
            data, gate_id=gate_id, finding_ids=ids[:1],
            summary="Add a task exporting metrics.", actor=f"gate:{gate_id}",
        )
        request["refusals"] = [
            {"at": "2026-09-20T09:00:00+00:00", "run": "R-1",
             "reasons": [{"message": "patch weakens a criterion"}]}
        ]
        gate = data["tasks"][gate_id]
        gate["status"] = "blocked"
        gate["held"] = {"reason": "repair-refused", "at": "2026-09-20T09:00:00+00:00",
                        "request": request["id"]}
        gates.attempts(gate).append(
            {"at": "2026-09-20T08:40:00+00:00", "decision": "needs-repair",
             "actor": "reviewer:pi", "summary": "REQ-003 has no implementation",
             "findings": ids[:1], "revision": 1}
        )
    return {
        "data": state.load(project),
        "blocking": ids[0],
        "declined": ids[1],
        "gate": gate_id,
        "request": request["id"],
    }


def test_the_plan_row_carries_what_the_header_needs(snapshot):
    row = api.plan(snapshot["data"])
    assert row["revision"] >= 1
    assert row["blocking"] == 1
    # The counts are of open findings, so the declined one is not among them —
    # writ's own structural warnings still are.
    advisory = [
        f for f in api.findings(snapshot["data"])
        if f["severity"] == "warning" and f["disposition"] == "open"
    ]
    assert row["advisory"] == len(advisory)
    assert snapshot["declined"] not in [f["id"] for f in advisory]
    assert row["uncovered"] == ["REQ-003"]
    assert row["open_repairs"] == [snapshot["request"]]
    assert row["held_gates"] == [
        {"id": snapshot["gate"], "reason": "repair-refused"}
    ]


def test_a_gate_is_only_held_once_it_is_blocked(snapshot, project):
    # `held` alone is not a hold: a gate writ is about to ask again has the record
    # without being stopped, and the dashboard must not report it as parked.
    with state.transaction(project) as data:
        data["tasks"][snapshot["gate"]]["status"] = "planned"
    assert api.plan(state.load(project))["held_gates"] == []


def test_every_finding_carries_its_source_and_disposition(snapshot):
    rows = api.findings(snapshot["data"])
    by_id = {row["id"]: row for row in rows}
    assert by_id[snapshot["blocking"]]["source"] == "critic:coverage"
    assert by_id[snapshot["blocking"]]["disposition"] == "open"
    answered = by_id[snapshot["declined"]]
    assert answered["disposition"] == "declined"
    # The reason travels with it: a page that showed the disposition without it
    # would say the objection was overruled and not why.
    assert answered["reason"] == "separate reader"


def test_findings_come_back_worst_first(snapshot):
    severities = [row["severity"] for row in api.findings(snapshot["data"])]
    assert severities == sorted(
        severities, key=lambda s: ("error", "warning", "note").index(s)
    )


def test_coverage_rows_name_their_tasks_and_state(snapshot):
    rows = {row["id"]: row for row in api.coverage(snapshot["data"])}
    assert rows["REQ-003"]["state"] == "uncovered"
    assert rows["REQ-003"]["tasks"] == []
    assert rows["REQ-001"]["tasks"]
    assert rows["REQ-005"]["declared"] == "out-of-scope"
    # An `existing` requirement with evidence is satisfied without a task, which is
    # the one state a task-counting page would get wrong.
    assert rows["REQ-004"]["declared"] == "existing"
    assert rows["REQ-004"]["state"] == "satisfied"
    assert rows["REQ-004"]["tasks"] == []


def test_a_repair_row_counts_the_refusals(snapshot):
    row = api.repairs(snapshot["data"])[0]
    assert row["id"] == snapshot["request"]
    assert row["gate"] == snapshot["gate"]
    assert row["status"] == "open"
    # The count, not the reasons: the row says a patch was turned down, and
    # `writ show RR-0001` says what for.
    assert row["refusals"] == 1
    assert row["findings"] == [snapshot["blocking"]]


def test_a_gate_detail_carries_its_hold_and_its_reviews(snapshot):
    row = api.task(snapshot["data"], snapshot["gate"])
    assert row["kind"] == "gate"
    assert row["held"]["reason"] == "repair-refused"
    assert row["held"]["request"] == snapshot["request"]
    assert row["gate_attempts"][0]["decision"] == "needs-repair"


def test_an_ordinary_task_has_the_same_shape_with_nothing_in_it(snapshot):
    task_id = next(
        task_id
        for task_id, task in snapshot["data"]["tasks"].items()
        if task.get("kind", "task") == "task"
    )
    row = api.task(snapshot["data"], task_id)
    # One detail shape serves both, so the page needs no branch to ask.
    assert row["kind"] == "task"
    assert row["held"] is None
    assert row["gate_attempts"] == []


def test_the_snapshot_carries_all_three_collections(snapshot, project):
    everything = api.everything(project)
    assert everything["overview"]["plan"]["blocking"] == 1
    assert everything["findings"] and everything["coverage"] and everything["repairs"]
