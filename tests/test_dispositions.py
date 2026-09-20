"""Answering a finding one at a time, and reading the records writ points at.

Two things meet here. Writ tells people to go and look at ids — a held gate's
evidence says `writ show RR-0001`, a blocking finding is reported as `F-0004` — so
those ids have to resolve. And a finding has to be answerable individually: the
wholesale lever (`writ approve --force`) accepts every open finding under one
reason, which is the wrong instrument for disagreeing with one of them.
"""
from __future__ import annotations

import json

import pytest

from writ import model, plans, repair, state
from writ.plancheck import Finding
from writ.state import WritError

from tests.test_plans import PLAN


def blocker(**over) -> Finding:
    payload = {
        "severity": "error",
        "category": "missing-coverage",
        "message": "No task implements the queue depth view",
        "where": "REQ-001",
        "suggested_action": "add a task, or mark it out of scope",
        "requirement_ids": ["REQ-001"],
        "source": "critic:coverage",
    }
    payload.update(over)
    return Finding(**payload)


@pytest.fixture
def held(writ, project, design, tmp_path):
    """A plan with two blocking findings, one of them with a refused repair."""
    artifact = tmp_path / "plan.json"
    artifact.write_text(json.dumps(PLAN), encoding="utf-8")
    writ("init")
    writ("plan", str(design), "--from-plan", str(artifact))
    with state.transaction(project) as data:
        plans.record_findings(
            data,
            [blocker(), blocker(
                category="unordered-writers",
                message="M01-001 and M01-002 both own store/queue.go",
                where="M01-001",
                source="critic:scope",
            )],
            scope="critic",
        )
        plans.set_status(data, "needs-approval")
        ids = [
            record["id"]
            for record in plans.finding_records(data)
            if record["severity"] == "error"
        ]
        gate_id = next(task for task in data["tasks"] if task.startswith("G-"))
        request = repair.open_request(
            data,
            gate_id=gate_id,
            finding_ids=ids[:1],
            summary="Add a task that builds the depth view.",
            actor=f"gate:{gate_id}",
        )
        request["refusals"] = [
            {
                "at": "2026-09-20T09:00:00+00:00",
                "run": "R-1",
                "reasons": [{"message": "patch targets revision 1; the plan is at 2"}],
            }
        ]
        data["tasks"][gate_id]["held"] = {
            "reason": "repair-refused",
            "at": "2026-09-20T09:00:00+00:00",
            "request": request["id"],
        }
    return {"findings": ids, "request": request["id"], "gate": gate_id}


# --------------------------------------------------------------------------
# the ids writ sends people to


def test_a_finding_id_resolves(held, project):
    kind, record = model.find(state.load(project), held["findings"][0])
    assert kind == "finding"
    assert record["id"] == held["findings"][0]


def test_a_repair_id_resolves(held, project):
    kind, record = model.find(state.load(project), held["request"])
    assert kind == "repair"
    assert record["gate"] == held["gate"]


def test_an_unknown_id_names_every_kind_it_could_have_been(held, project):
    with pytest.raises(WritError, match="finding, or repair request"):
        model.find(state.load(project), "RR-9999")


def test_show_a_finding_says_what_to_do_about_it(held, writ):
    code, out, _ = writ("show", held["findings"][0])
    assert code == 0
    assert "severity: error" in out
    assert "raised by: critic:coverage" in out
    assert "No task implements the queue depth view" in out
    # The reader is here to answer it, so both answers are on the page.
    assert f"writ set {held['findings'][0]} accepted" in out
    assert f"writ set {held['findings'][0]} declined" in out
    # And the repair already asked for against it.
    assert held["request"] in out


def test_show_a_disposed_finding_stops_offering_the_answers(held, writ):
    finding_id = held["findings"][0]
    writ("set", finding_id, "declined", "--reason", "served by /stats")
    code, out, _ = writ("show", finding_id)
    assert "disposition: declined" in out
    assert "served by /stats" in out
    assert "writ set" not in out


def test_show_a_repair_is_the_refusals(held, writ):
    code, out, _ = writ("show", held["request"])
    assert code == 0
    assert "status: open" in out
    assert held["findings"][0] in out
    assert "Add a task that builds the depth view." in out
    # Why writ would not apply what the planner proposed is the substance.
    assert "refused patch 1" in out
    assert "patch targets revision 1" in out
    assert "held (repair-refused)" in out


def test_show_a_finding_as_json(held, writ):
    code, out, _ = writ("--json", "show", held["findings"][0])
    assert json.loads(out)["id"] == held["findings"][0]


# --------------------------------------------------------------------------
# answering one finding


def test_declining_a_finding_records_who_and_why(held, writ, project):
    finding_id = held["findings"][0]
    code, out, _ = writ(
        "set", finding_id, "declined",
        "--reason", "REQ-001 is served by the existing /stats endpoint",
        "--by", "pplam",
    )
    assert code == 0
    record = plans.get_finding(state.load(project), finding_id)
    assert record["disposition"] == "declined"
    assert record["disposed_by"] == "pplam"
    assert "/stats" in record["reason"]
    # The other blocker is untouched: that is the whole difference from --force.
    other = plans.get_finding(state.load(project), held["findings"][1])
    assert other["disposition"] == "open"
    assert held["findings"][1] in out


def test_answering_the_last_blocker_points_at_the_check(held, writ):
    for finding_id in held["findings"]:
        code, out, _ = writ("set", finding_id, "accepted", "--reason", "known, shipping")
        assert code == 0
    # A disposition does not approve the plan; a check does.
    assert "next: writ check" in out


def test_a_plan_with_nothing_open_is_not_told_to_force(held, writ, project):
    for finding_id in held["findings"]:
        writ("set", finding_id, "accepted", "--reason", "known, shipping")
    code, _, err = writ("run", "--agent", "false")
    assert code == 2
    assert "nothing blocking is open any more" in err
    assert "--force" not in err
    # And the check it names does approve it.
    assert writ("check")[0] == 0
    assert plans.runnable(state.load(project))


def test_a_finding_cannot_be_resolved_by_hand(held, writ, project):
    # `resolved` is not in the parser's choices, so argparse refuses it first.
    code, _, err = writ("set", held["findings"][0], "resolved", "--reason", "done")
    assert code == 2
    assert "invalid choice" in err
    # And the layer underneath refuses it too, for callers that reach past the CLI.
    with state.transaction(project) as data:
        with pytest.raises(WritError, match="cannot be set resolved by hand"):
            from writ.commands import _set_finding
            _set_finding(
                type("A", (), {"id": held["findings"][0], "status": "resolved",
                               "reason": "done", "by": None, "evidence": None})(),
                data,
            )


def test_either_answer_needs_a_reason(held, writ, project):
    for disposition in ("accepted", "declined"):
        code, _, err = writ("set", held["findings"][0], disposition)
        assert code == 2
        assert "--reason is required" in err
        assert disposition[:6] in err
    assert plans.get_finding(state.load(project), held["findings"][0])[
        "disposition"
    ] == "open"


def test_a_task_status_is_not_a_finding_disposition(held, writ):
    code, _, err = writ("set", held["findings"][0], "blocked", "--reason", "x")
    assert code == 2
    assert "not a finding disposition" in err


def test_a_finding_disposition_is_not_a_task_status(held, writ, project):
    task_id = next(
        task_id
        for task_id, task in state.load(project)["tasks"].items()
        if task.get("kind", "task") == "task"
    )
    code, _, err = writ("set", task_id, "declined", "--reason", "x")
    assert code == 2
    assert "not a task status" in err
