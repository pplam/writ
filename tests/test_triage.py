"""Triage: what happens to a task that is stuck.

A task that is blocked, or failed with its rework spent, holds up everything that
depends on it. Triage hands it to the plan's agent, which may move a criterion to
the task or gate that can meet it, send the task back with guidance, or ask. The
claims worth pinning are the ones writ enforces rather than trusts: a criterion
may move and never vanish, the bound holds, and an unstuck task's next attempt is
told what changed.
"""
from __future__ import annotations

import json
import shlex
import sys

import pytest

from writ import adjudicate, decisions, gates, orchestrator, runner, state, triage
from writ.state import WritError, utcnow

from tests.test_plans import PLAN
from tests.test_run import IMPLEMENTER, REVIEWER


#: a triage agent that does what WRIT_TEST_TRIAGE says: `revise` merges fields
#: into workspace feature files, and `response` is written as the response.
TRIAGER = """
import json, os, re, sys
from pathlib import Path
prompt = sys.stdin.read()
path = Path(re.search(r'Write your response as JSON to this exact path:\\n  (\\S+)', prompt).group(1))
spec = json.loads(os.environ["WRIT_TEST_TRIAGE"])
work = path.parent / "workspace"
for ref, fields in spec.get("revise", {}).items():
    file = work / (ref + ".json")
    entry = json.loads(file.read_text())
    entry.update(fields)
    file.write_text(json.dumps(entry))
path.write_text(json.dumps(spec["response"]))
"""

#: the criterion of M01-001 that the tests move away
MOVED = "`go test ./store` passes with appends fsync'd in order"
KEPT = "a failing test in store/log_test.go reproduces a torn append"


def agent(script: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


def told(monkeypatch, spec) -> None:
    monkeypatch.setenv("WRIT_TEST_TRIAGE", json.dumps(spec))


@pytest.fixture
def stuck(writ, project, design, tmp_path):
    """An approved plan whose first task blocked on something it cannot do."""
    artifact = tmp_path / "plan.json"
    artifact.write_text(json.dumps(PLAN), encoding="utf-8")
    writ("init")
    code, _, err = writ("plan", str(design), "--from-plan", str(artifact), "--auto-approve")
    assert code == 0, err
    block(project, "M01-001")
    return writ


def block(project, task_id, *, failed=False):
    with state.transaction(project) as data:
        task = data["tasks"][task_id]
        task["last_verdict"] = {
            "at": utcnow(),
            "outcome": "failed" if failed else "blocked",
            "summary": "fsync ordering needs the projection, which is not built yet",
        }
        if failed:
            task["status"] = "failed"
            task["rework"] = {"count": 2, "max": 2, "exhausted": True}
        else:
            task["status"] = "blocked"


def unstick(writ, *extra):
    return writ("unstick", "M01-001", "--agent", agent(TRIAGER), "--quiet", *extra)


def final_gate(data):
    return next(
        task_id
        for task_id, task in sorted(data["tasks"].items())
        if gates.is_gate(task) and not task.get("milestone_gate", False)
        and task_id.endswith("FINAL")
    )


def texts(task):
    return [item["text"] for item in task["acceptances"]]


# --------------------------------------------------------------------------
# which tasks are stuck


def test_a_blocked_task_is_stuck(stuck, project):
    assert triage.stuck(state.load(project)) == ["M01-001"]


def test_a_failed_task_is_stuck_only_once_its_rework_is_spent(stuck, project):
    with state.transaction(project) as data:
        data["tasks"]["M01-001"]["status"] = "failed"
        data["tasks"]["M01-001"]["rework"] = {"count": 1, "max": 2}
    assert triage.stuck(state.load(project)) == []
    block(project, "M01-001", failed=True)
    assert triage.stuck(state.load(project)) == ["M01-001"]


def test_a_task_that_is_not_stuck_is_refused(stuck, project):
    with pytest.raises(WritError, match="only a blocked task"):
        triage.run(project, "M01-002", agent=agent(TRIAGER), model=None, timeout=None)


# --------------------------------------------------------------------------
# revise


def test_a_criterion_moves_to_the_task_that_can_meet_it(stuck, project, monkeypatch):
    told(
        monkeypatch,
        {
            "revise": {
                "M01-001": {"acceptances": [KEPT]},
                "M01-002": {
                    "acceptances": [
                        "`go test ./store -run Replay` reproduces the projection",
                        "store/projection.go rebuilds from an empty state",
                        MOVED,
                    ]
                },
            },
            "response": {
                "action": "revise",
                "analysis": "criterion 2 needs the projection",
                "guidance": "meet criterion 1 only",
                "moved": [{"criterion": MOVED, "to": "M01-002"}],
            },
        },
    )
    code, out, err = unstick(stuck)
    assert code == 0, out + err
    data = state.load(project)
    assert data["tasks"]["M01-001"]["status"] == "planned"
    assert texts(data["tasks"]["M01-001"]) == [KEPT]
    assert MOVED in texts(data["tasks"]["M01-002"])
    record = data["tasks"]["M01-001"]["triage"][-1]
    assert record["unstuck"] and record["action"] == "revise"


def test_a_criterion_may_move_to_the_final_gate(stuck, project, monkeypatch):
    gate = final_gate(state.load(project))
    told(
        monkeypatch,
        {
            "revise": {"M01-001": {"acceptances": [KEPT]}},
            "response": {
                "action": "revise",
                "analysis": "only the integrated system can show this",
                "guidance": "meet criterion 1",
                "moved": [{"criterion": MOVED, "to": gate}],
            },
        },
    )
    code, out, err = unstick(stuck)
    assert code == 0, out + err
    data = state.load(project)
    carried = [
        item for item in data["tasks"][gate]["acceptances"] if item["text"] == MOVED
    ]
    assert carried and carried[0]["from"] == "M01-001"


def test_a_criterion_may_not_simply_vanish(stuck, project, monkeypatch):
    told(
        monkeypatch,
        {
            "revise": {"M01-001": {"acceptances": [KEPT]}},
            "response": {
                "action": "revise",
                "analysis": "too hard",
                "guidance": "meet criterion 1",
            },
        },
    )
    code, out, _ = unstick(stuck)
    assert code == 1
    data = state.load(project)
    assert data["tasks"]["M01-001"]["status"] == "blocked"
    assert texts(data["tasks"]["M01-001"]) == [KEPT, MOVED]
    assert data["tasks"]["M01-001"]["triage"][-1]["refused"]


def test_a_move_must_land_where_it_says(stuck, project, monkeypatch):
    told(
        monkeypatch,
        {
            "revise": {"M01-001": {"acceptances": [KEPT]}},
            "response": {
                "action": "revise",
                "analysis": "belongs downstream",
                "guidance": "meet criterion 1",
                "moved": [{"criterion": MOVED, "to": "M01-002"}],
            },
        },
    )
    code, _, _ = unstick(stuck)
    assert code == 1
    refused = state.load(project)["tasks"]["M01-001"]["triage"][-1]["refused"]
    assert any(item["category"] == "bad-move" for item in refused)


# --------------------------------------------------------------------------
# retry


def test_a_retry_gives_a_failed_task_a_fresh_rework_budget(stuck, project, monkeypatch):
    block(project, "M01-001", failed=True)
    told(
        monkeypatch,
        {
            "response": {
                "action": "retry",
                "analysis": "the reviewer wanted a test the brief never named",
                "guidance": "add store/log_test.go before touching log.go",
            }
        },
    )
    code, out, err = unstick(stuck)
    assert code == 0, out + err
    task = state.load(project)["tasks"]["M01-001"]
    assert task["status"] == "planned"
    assert task["rework"]["exhausted"] is False
    assert task["rework"]["allowance"] == 2


def test_a_retry_needs_guidance(stuck, project, monkeypatch):
    told(monkeypatch, {"response": {"action": "retry", "analysis": "try again"}})
    code, _, _ = unstick(stuck)
    assert code == 1
    assert state.load(project)["tasks"]["M01-001"]["status"] == "blocked"


def test_the_next_attempt_is_told_what_triage_decided(stuck, project, monkeypatch):
    told(
        monkeypatch,
        {
            "response": {
                "action": "retry",
                "analysis": "the fsync order is testable with a fake file",
                "guidance": "use a fake file to observe the fsync order",
            }
        },
    )
    unstick(stuck)
    task = state.load(project)["tasks"]["M01-001"]
    section = runner._triage_section(task)
    assert "use a fake file to observe the fsync order" in section
    # Once the task has run again, the guidance has been heard.
    task["last_verdict"] = {"at": "9999-01-01T00:00:00Z"}
    assert runner._triage_section(task) == ""


def test_the_proposed_change_waits_for_a_person_when_attended(
    stuck, project, monkeypatch, attended
):
    told(
        monkeypatch,
        {"response": {"action": "retry", "analysis": "x", "guidance": "do y"}},
    )
    unstick(stuck)
    logged = [
        item
        for item in state.load(project)["decisions"]
        if item.get("proposed_by") == "triage"
    ]
    assert logged and logged[-1]["status"] == "proposed"


# --------------------------------------------------------------------------
# question


QUESTION = {
    "response": {
        "action": "question",
        "analysis": "the design does not say whether appends may batch",
        "question": {
            "question": "May appends be batched before fsync?",
            "recommendation": "No: fsync every append",
        },
    }
}


def test_a_question_waits_for_a_person_when_attended(
    stuck, project, monkeypatch, attended
):
    told(monkeypatch, QUESTION)
    code, _, _ = unstick(stuck, "--no-autonomous")
    assert code == 1
    task = state.load(project)["tasks"]["M01-001"]
    assert task["status"] == "blocked"
    assert task["triage_question"]


def test_a_question_is_answered_with_its_recommendation_when_autonomous(
    stuck, project, monkeypatch
):
    told(monkeypatch, QUESTION)
    code, out, err = unstick(stuck, "--autonomous")
    assert code == 0, out + err
    task = state.load(project)["tasks"]["M01-001"]
    assert task["status"] == "planned"
    assert "No: fsync every append" in task["triage"][-1]["guidance"]


# --------------------------------------------------------------------------
# the bound


def test_a_task_is_triaged_at_most_twice(stuck, project, monkeypatch):
    told(monkeypatch, {"response": {"action": "retry", "analysis": "no guidance"}})
    for _ in range(triage.MAX_TRIAGES):
        assert unstick(stuck)[0] == 1
    assert triage.stuck(state.load(project)) == []
    with pytest.raises(WritError, match="most writ gives one task"):
        triage.run(project, "M01-001", agent=agent(TRIAGER), model=None, timeout=None)


def test_an_agent_that_writes_nothing_still_counts(stuck, project):
    code, _, _ = writ_unstick_mute(stuck)
    assert code == 1
    record = state.load(project)["tasks"]["M01-001"]["triage"][-1]
    assert record["error"]


def writ_unstick_mute(writ):
    return writ("unstick", "M01-001", "--agent", agent("import sys; sys.stdin.read()"), "--quiet")


# --------------------------------------------------------------------------
# gates keep what was carried to them


def test_a_recomputed_gate_keeps_a_criterion_carried_to_it(stuck, project):
    with state.transaction(project) as data:
        gate = next(
            task for task in data["tasks"].values()
            if gates.is_gate(task) and task.get("milestone") == "M01"
        )
        gate["acceptances"].append(
            {"text": "carried here", "status": "pending", "from": "M01-001"}
        )
        adjudicate._recompute_gates(data)
        assert "carried here" in texts(gate)


# --------------------------------------------------------------------------
# writ run


def test_the_scheduler_triages_a_stuck_task_only_when_autonomous():
    data = {
        "tasks": {
            "M01-001": {
                "id": "M01-001",
                "status": "blocked",
                "depends_on": [],
                "acceptances": [],
            }
        }
    }
    assert orchestrator.next_job(data, busy=[], budget=None, started=[]) is None
    job = orchestrator.next_job(data, busy=[], budget=None, started=[], triage=True)
    assert job is not None and job.role == "triage"
    again = orchestrator.next_job(
        data, busy=[], budget=None, started=[job.key], triage=True
    )
    assert again is None


def test_a_triaged_task_is_a_new_job_for_the_session():
    task = {"id": "M01-001", "status": "planned", "depends_on": [], "acceptances": []}
    before = orchestrator._job_for(task).key
    task["triage"] = [{"unstuck": True}]
    after = orchestrator._job_for(task).key
    assert before != after
    assert orchestrator._base(after) == "M01-001"


def test_run_triages_and_then_finishes_the_stuck_task(stuck, project, monkeypatch):
    told(
        monkeypatch,
        {
            "response": {
                "action": "retry",
                "analysis": "it can be done with a fake file",
                "guidance": "use a fake file",
            }
        },
    )
    code, out, err = stuck(
        "run",
        "--autonomous",
        "--no-stream",
        "--agent",
        agent(IMPLEMENTER),
        "--reviewer",
        agent(REVIEWER),
        "--critic",
        agent(TRIAGER),
    )
    assert "triage" in out, out + err
    data = state.load(project)
    assert data["tasks"]["M01-001"]["status"] == "completed", out + err
    assert "unstuck by triage: M01-001" in out
    assert decisions.autonomous(data)


def test_an_attended_run_says_how_to_unstick(stuck, project, attended):
    code, out, err = stuck(
        "run",
        "--no-autonomous",
        "--no-stream",
        "--agent",
        agent(IMPLEMENTER),
        "--reviewer",
        agent(REVIEWER),
    )
    assert "writ unstick" in out + err
