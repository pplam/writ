"""The decision log: agents propose, humans confirm.

There is no command for writing a decision by hand. An agent that resolves a
question the design left open reports it in its verdict, so these tests drive the
log the way the tool actually fills it — through a dispatch.
"""
import json
import shlex
import sys

import pytest

from writ import decisions, state
from writ.state import WritError


def agent_proposing(*proposals, criteria=3):
    """A fake agent that passes its criteria and proposes decisions."""
    payload = json.dumps(
        {
            "outcome": "complete",
            "summary": "did the work",
            "criteria": [
                {"number": n, "status": "passed", "evidence": "ran: pytest -q"}
                for n in range(1, criteria + 1)
            ],
            "decisions": list(proposals),
        }
    )
    script = f"""
import re, sys
prompt = sys.stdin.read()
match = re.search(r'^  (\\S*verdict\\.json)$', prompt, re.M)
open(match.group(1), "w").write({payload!r})
"""
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


FIXTURES = {
    "title": "Fixture-only tests",
    "decision": "Automated tests never touch a live platform.",
    "context": "Quotas are small and debugging consumes them.",
    "consequences": "Recorded fixtures are the only substrate.",
}


def propose(writ, *proposals, task="M01-001", criteria=3):
    """Dispatch a proposing agent at `task`.

    `--force` because only the first task is ungated and these tests care about
    the decision log, not the dependency DAG.
    """
    return writ(
        "dispatch",
        task,
        "--agent",
        agent_proposing(*proposals, criteria=criteria),
        "-q",
        "--force",
    )


# --------------------------------------------------------------------------
# agents record decisions


def test_an_agents_verdict_records_its_decisions(planned, writ, project):
    code, out, _ = propose(writ, FIXTURES)
    assert code == 0

    _, listing, _ = writ("--json", "list", "decisions")
    records = json.loads(listing)
    assert len(records) == 1
    assert records[0]["id"] == "D-0001"
    assert records[0]["title"] == "Fixture-only tests"
    assert records[0]["tasks"] == ["M01-001"]


def test_a_proposal_is_not_yet_binding(planned, writ):
    propose(writ, FIXTURES)
    _, listing, _ = writ("--json", "list", "decisions")
    record = json.loads(listing)[0]
    assert record["status"] == "proposed"
    assert record["confirmed_by"] is None


def test_the_proposing_agent_is_named(planned, writ):
    propose(writ, FIXTURES)
    _, listing, _ = writ("--json", "list", "decisions")
    assert json.loads(listing)[0]["proposed_by"] != "operator"


def test_several_decisions_from_one_verdict(planned, writ):
    propose(
        writ,
        {"title": "One", "decision": "the first choice we made"},
        {"title": "Two", "decision": "the second choice we made"},
    )
    _, out, _ = writ("--json", "list", "decisions")
    assert [r["id"] for r in json.loads(out)] == ["D-0001", "D-0002"]


def test_a_verdict_with_no_decisions_records_none(planned, writ):
    code, _, _ = propose(writ)
    assert code == 0
    _, out, _ = writ("--json", "list", "decisions")
    assert json.loads(out) == []


def test_the_markdown_mirror_shows_proposals_as_proposed(planned, writ, project):
    propose(writ, FIXTURES)
    mirror = state.decisions_file(project).read_text(encoding="utf-8")
    assert "D-0001 — Fixture-only tests (proposed)" in mirror
    assert "Automated tests never touch a live platform." in mirror
    assert "**Status:** proposed" in mirror


# --------------------------------------------------------------------------
# humans rule on them


def test_confirming_makes_a_decision_binding(planned, writ, project):
    propose(writ, FIXTURES)
    code, out, _ = writ("set", "D-0001", "active")
    assert code == 0 and "D-0001 active" in out

    record = decisions.get(state.load(project), "D-0001")
    assert record["status"] == "active"
    assert record["confirmed_by"] == "operator"
    assert record["confirmed_at"]


def test_rejecting_keeps_the_record_and_the_reason(planned, writ, project):
    propose(writ, FIXTURES)
    code, out, _ = writ(
        "set", "D-0001", "rejected", "--reason", "we do want live smoke tests"
    )
    assert code == 0 and "rejected" in out

    record = decisions.get(state.load(project), "D-0001")
    assert record["status"] == "rejected"
    assert record["rejected_reason"] == "we do want live smoke tests"


def test_rejection_demands_a_reason(planned, writ):
    propose(writ, FIXTURES)
    code, _, err = writ("set", "D-0001", "rejected")
    assert code == 2 and "--reason" in err


def test_confirming_can_supersede_an_earlier_decision(planned, writ, project):
    propose(writ, {"title": "Original", "decision": "the first way of doing it"})
    writ("set", "D-0001", "active")
    propose(
        writ,
        {"title": "Replacement", "decision": "the better way of doing it"},
        task="M02-001",
        criteria=2,
    )
    code, out, _ = writ("set", "D-0002", "active", "--supersedes", "D-0001")
    assert code == 0 and "supersedes: D-0001" in out

    records = {r["id"]: r for r in state.load(project)["decisions"]}
    assert records["D-0001"]["status"] == "superseded"
    assert records["D-0001"]["superseded_by"] == "D-0002"
    assert records["D-0002"]["supersedes"] == "D-0001"


def test_a_decision_cannot_be_confirmed_twice(planned, writ):
    propose(writ, FIXTURES)
    writ("set", "D-0001", "active")
    code, _, err = writ("set", "D-0001", "active")
    assert code == 2 and "not proposed" in err


def test_task_statuses_are_not_decision_statuses(planned, writ):
    propose(writ, FIXTURES)
    code, _, err = writ("set", "D-0001", "running")
    assert code == 2 and "not a decision status" in err


def test_decision_statuses_are_not_task_statuses(planned, writ):
    code, _, err = writ("set", "M01-001", "rejected")
    assert code == 2 and "not a task status" in err


# --------------------------------------------------------------------------
# reading


def test_show_explains_how_to_rule_on_a_proposal(planned, writ):
    propose(writ, FIXTURES)
    code, out, _ = writ("show", "D-0001")
    assert code == 0
    assert "D-0001 — Fixture-only tests" in out
    assert "Automated tests never touch a live platform." in out
    assert "Quotas are small" in out
    assert "not yet confirmed" in out
    assert "writ set D-0001 active" in out


def test_confirmed_decisions_do_not_nag(planned, writ):
    propose(writ, FIXTURES)
    writ("set", "D-0001", "active")
    _, out, _ = writ("show", "D-0001")
    assert "not yet confirmed" not in out
    assert "ruled by: operator" in out


def test_the_proposed_filter_finds_the_queue(planned, writ):
    propose(writ, {"title": "One", "decision": "the first choice we made"})
    writ("set", "D-0001", "active")
    propose(
        writ,
        {"title": "Two", "decision": "the second choice we made"},
        task="M02-001",
        criteria=2,
    )
    _, out, _ = writ("--json", "list", "decisions", "--proposed")
    assert [r["id"] for r in json.loads(out)] == ["D-0002"]


def test_status_surfaces_the_proposal_queue(planned, writ):
    propose(writ, FIXTURES)
    _, out, _ = writ("status")
    assert "decisions proposed: D-0001" in out


def test_active_filter_hides_proposals(planned, writ):
    propose(writ, FIXTURES)
    _, out, _ = writ("--json", "list", "decisions", "--status", "active")
    assert json.loads(out) == []


def test_task_filter(planned, writ):
    propose(writ, FIXTURES)
    _, out, _ = writ("--json", "list", "decisions", "--task", "M01-001")
    assert [r["id"] for r in json.loads(out)] == ["D-0001"]
    _, out, _ = writ("--json", "list", "decisions", "--task", "M02-001")
    assert json.loads(out) == []


def test_unknown_decision_ids_are_rejected(planned, writ):
    code, _, err = writ("show", "D-9999")
    assert code == 2 and "unknown id" in err
    code, _, err = writ("set", "D-9999", "active")
    assert code == 2 and "unknown id" in err


def test_the_log_survives_across_invocations(planned, writ, project):
    propose(writ, {"title": "First", "decision": "the earlier choice made"})
    propose(
        writ,
        {"title": "Second", "decision": "the later choice made"},
        task="M02-001",
        criteria=2,
    )
    data = state.load(project)
    assert [d["title"] for d in data["decisions"]] == ["First", "Second"]
    assert data["counters"]["decision"] == 2


def test_writing_a_decision_by_hand_is_not_a_command(planned, writ):
    code, _, err = writ("decide", "By hand", "--decision", "because I say so")
    assert code == 2
    assert "invalid choice" in err or "decide" in err


# --------------------------------------------------------------------------
# the log module directly


def test_a_proposal_needs_a_title_and_a_statement():
    data = {"decisions": [], "tasks": {}, "counters": {}}
    with pytest.raises(WritError, match="needs a title"):
        decisions.propose(data, title=" ", decision="x", proposed_by="agent")
    with pytest.raises(WritError, match="needs a statement"):
        decisions.propose(data, title="t", decision=" ", proposed_by="agent")


def test_a_proposal_drops_task_ids_that_no_longer_exist():
    """A renamed task should not cost us the decision's content."""
    data = {"decisions": [], "tasks": {"M01-001": {}}, "counters": {}}
    record = decisions.propose(
        data,
        title="t",
        decision="a real decision here",
        proposed_by="agent",
        tasks=["M01-001", "ghost"],
    )
    assert record["tasks"] == ["M01-001"]


def test_a_decision_cannot_supersede_itself():
    data = {"decisions": [], "tasks": {}, "counters": {}}
    decisions.propose(data, title="t", decision="a real one", proposed_by="a")
    with pytest.raises(WritError, match="cannot supersede itself"):
        decisions.confirm(data, "D-0001", supersedes="D-0001")
