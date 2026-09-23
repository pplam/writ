"""The bounded repair loop that answers a plan's findings before it executes.

Writ could already produce findings against a plan and resolve them once it was
running. Between those sat the gap these tests are about: a blocking finding before
execution had no repair path, only a hand disposition or `approve --force`. So the
claims worth pinning down are that an adjudicator's patch is validated rather than
trusted, that the bar cannot be lowered by one, that a finding closes on a re-check
rather than on the patch's word, and that the loop stops.

The adjudicator is a real subprocess writing a real patch file, as in test_gates.py.
The parts worth testing only exist once something has actually proposed a change.
"""
from __future__ import annotations

import json
import shlex
import sys

import pytest

from writ import adjudicate, phases, plancheck, plans, repair, state
from writ.state import WritError

from tests.test_plans import PLAN


#: an adjudicator that writes whatever patch the test hands it, with the revision
#: and finding id filled in from the prompt it was given.
ADJUDICATOR = """
import json, os, re, sys
prompt = sys.stdin.read()
path = re.search(r'Write your patch as JSON to this exact path:\\n  (\\S+)', prompt).group(1)
revision = int(re.search(r'Plan revision: (\\d+)', prompt).group(1))
findings = re.findall(r'(F-\\d+)', prompt)
round_no = int(re.search(r'Adjudication round: (\\d+)', prompt).group(1))
patch = json.loads(os.environ["WRIT_TEST_PATCH"])
if isinstance(patch, list):
    # A list means one patch per round, so a test can refuse then succeed.
    patch = patch[min(round_no, len(patch)) - 1]
patch.setdefault("base_revision", revision)
if patch.pop("_auto_dispositions", True) and findings:
    patch.setdefault("dispositions", [
        {"finding_id": f, "resolution": "accepted", "change": "fixed it"}
        for f in dict.fromkeys(findings)
    ])
    for entry in patch.get("add_tasks", []) + patch.get("revise_tasks", []):
        entry.setdefault("resolves_findings", list(dict.fromkeys(findings)))
open(path, "w").write(json.dumps(patch))
"""

MUTE = """
import sys
sys.stdin.read()
print("I would rather not")
"""

#: a critic that reports one blocking finding the first time it reads the plan and
#: nothing afterwards — a repair that actually worked, from the critic's side. The
#: marker file is how it remembers across processes.
CRITIC_ONCE = """
import json, os, re, sys
prompt = sys.stdin.read()
path = re.search(r'Write your findings as JSON to this exact path:\\n  (\\S+)', prompt).group(1)
marker = os.environ["WRIT_TEST_ONCE"]
first = not os.path.exists(marker)
open(marker, "a").write("x")
findings = [] if not first else [{
    "severity": "blocking",
    "category": "missing-coverage",
    "where": "REQ-003",
    "message": "No task implements the queue depth view",
    "suggested_action": "add a task, or mark it out of scope with a reason",
    "requirement_ids": ["REQ-003"],
    "evidence": "searched the plan and the repository for queue depth",
}]
open(path, "w").write(json.dumps({
    "findings": findings,
    "summary": "read it" if not first else "one hole",
    "confidence": "high",
}))
"""


#: a critic that objects to something *different* each time it reads.
#:
#: The production case the seventy-note bug was hiding: a patch closes what was
#: raised, and the re-read of the patched plan finds a new hole in the work the patch
#: just added. That is the loop's whole purpose — and it needs two rounds, so a stub
#: that reports the same thing forever (which escalates) cannot express it.
CRITIC_MOVES_ON = """
import json, os, re, sys
prompt = sys.stdin.read()
path = re.search(r'Write your findings as JSON to this exact path:\\n  (\\S+)', prompt).group(1)
marker = os.environ["WRIT_TEST_READS"]
reads = len(open(marker).read()) if os.path.exists(marker) else 0
open(marker, "a").write("x")
# A new requirement each read, so each finding is its own objection rather than the
# same one coming back. Two of them, then satisfied.
holes = ["REQ-003", "REQ-004"]
findings = []
if reads < len(holes):
    findings = [{
        "severity": "blocking",
        "category": "missing-coverage",
        "where": holes[reads],
        "message": f"No task implements {holes[reads]}",
        "suggested_action": "add a task, or mark it out of scope with a reason",
        "requirement_ids": [holes[reads]],
        "evidence": "searched the plan and the repository",
    }]
open(path, "w").write(json.dumps({
    "findings": findings,
    "summary": "another hole" if findings else "clean",
    "confidence": "high",
}))
"""


def agent(script: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


def patched(monkeypatch, patch) -> None:
    monkeypatch.setenv("WRIT_TEST_PATCH", json.dumps(patch))


@pytest.fixture
def objected(writ, project, design, tmp_path):
    """A committed plan with one blocking finding standing against it.

    The finding is recorded directly rather than produced by a critic: what these
    tests are about is what happens *after* a finding exists, and a fake critic in
    the middle would only add a second thing that could break.
    """
    artifact = tmp_path / "plan.json"
    artifact.write_text(json.dumps(PLAN), encoding="utf-8")
    writ("init")
    writ("plan", str(design), "--from-plan", str(artifact))
    with state.transaction(project) as data:
        plans.record_findings(
            data,
            [
                plancheck.Finding(
                    severity="error",
                    category="missing-coverage",
                    message="No task implements the queue depth view",
                    where="REQ-003",
                    suggested_action="add a task, or mark it out of scope",
                    requirement_ids=["REQ-003"],
                    source="critic:coverage",
                )
            ],
            scope="critic:coverage",
        )
    return writ


#: a patch that closes the fixture's finding by adding the missing work
ADDS_THE_TASK = {
    "analysis": "nothing in the plan reads queue depth",
    "add_tasks": [
        {
            "id": "proposed-queue-depth",
            "title": "Expose queue depth to the operator",
            "milestone": "M01",
            "requirement_ids": ["REQ-003"],
            "acceptances": [
                "a failing test in ops/depth_test.go reproduces the missing view",
                "`go test ./ops` reports queue depth",
            ],
            "allowed": ["ops/depth.go", "ops/depth_test.go"],
        }
    ],
}


# --------------------------------------------------------------------------
# the prompt


def test_the_prompt_carries_the_findings_and_the_bar(tmp_path):
    prompt = adjudicate.build_prompt(
        root=tmp_path,
        doc=tmp_path / "design.md",
        plan_text='{"tasks": []}',
        patch_path=tmp_path / "patch.json",
        findings=[
            plancheck.Finding(
                severity="error",
                category="missing-coverage",
                message="nothing covers REQ-003",
                where="REQ-003",
                id="F-0001",
            )
        ],
        base_revision=4,
        round_number=1,
    )
    assert "has not been executed yet" in prompt
    assert "F-0001" in prompt
    assert "Plan revision: 4" in prompt
    # It is told the two things writ will refuse it for, in the rules it is given.
    assert "drops a bar" in prompt or "lowers the bar" in prompt
    assert "revise_tasks" in prompt


def test_a_refused_patch_tells_the_next_round_exactly_why(tmp_path):
    """A retry is only bounded if the next attempt knows more than the last."""
    prompt = adjudicate.build_prompt(
        root=tmp_path,
        doc=None,
        plan_text="{}",
        patch_path=tmp_path / "patch.json",
        findings=[],
        prior_refusals=[
            plancheck.Finding(
                severity="error",
                category="weakened-acceptance",
                message="revision of M01-001 states 1 criteria where it had 2",
                where="RR-0001.revise_tasks[0]",
            )
        ],
        round_number=2,
    )
    assert "REFUSED" in prompt
    assert "states 1 criteria where it had 2" in prompt


# --------------------------------------------------------------------------
# a patch closes a finding


def test_an_adjudicated_plan_gains_the_work_the_finding_asked_for(
    objected, project, monkeypatch
):
    patched(monkeypatch, ADDS_THE_TASK)
    code, out, _ = objected(
        "adjudicate", "--agent", agent(ADJUDICATOR), "--no-critics"
    )
    data = state.load(project)
    added = [
        task
        for task in data["tasks"].values()
        if task.get("repair", {}).get("scope") == "plan"
    ]
    assert added, [t["id"] for t in data["tasks"].values()]
    assert added[0]["requirement_ids"] == ["REQ-003"]
    assert code == 0, out


def test_the_finding_is_disposed_with_the_adjudicator_named(
    objected, project, monkeypatch
):
    patched(monkeypatch, ADDS_THE_TASK)
    objected("adjudicate", "--agent", agent(ADJUDICATOR), "--no-critics")
    record = [
        item
        for item in plans.finding_records(state.load(project))
        if item["source"] == "critic:coverage"
    ][0]
    assert record["disposition"] == "accepted"
    assert record["disposed_by"] == "adjudicator"


def test_a_repair_bumps_the_plan_revision(objected, project, monkeypatch):
    before = plans.revision(state.load(project))
    patched(monkeypatch, ADDS_THE_TASK)
    objected("adjudicate", "--agent", agent(ADJUDICATOR), "--no-critics")
    assert plans.revision(state.load(project)) > before


def test_the_request_records_what_landed(objected, project, monkeypatch):
    patched(monkeypatch, ADDS_THE_TASK)
    objected("adjudicate", "--agent", agent(ADJUDICATOR), "--no-critics")
    data = state.load(project)
    request = repair.requests(data)[0]
    assert request["status"] == "applied"
    assert request["applied_tasks"]
    # Plan-scoped, so it names no gate — which is what keeps the scheduler from
    # ever trying to dispatch it.
    assert repair.is_plan_request(request)
    assert repair.scope_of(request) == "plan"


# --------------------------------------------------------------------------
# revising a task, which only this occasion allows


REVISES_THE_TASK = {
    "analysis": "the criterion names no command",
    "revise_tasks": [
        {
            "id": "M01-001",
            "acceptances": [
                "a failing test in store/log_test.go reproduces a torn append",
                "`go test ./store` passes with appends fsync'd in order",
                "`go test ./store -run Torn` proves the torn append is rejected",
            ],
        }
    ],
}


def test_a_task_can_be_rewritten_before_it_runs(objected, project, monkeypatch):
    patched(monkeypatch, REVISES_THE_TASK)
    objected("adjudicate", "--agent", agent(ADJUDICATOR), "--no-critics")
    task = state.load(project)["tasks"]["M01-001"]
    assert len(task["acceptances"]) == 3
    # The revision is on the record, with the finding it was for.
    assert task["revisions"][0]["fields"] == ["acceptances"]


def test_a_revision_leaves_unnamed_fields_alone(objected, project, monkeypatch):
    before = state.load(project)["tasks"]["M01-001"]
    fence, requirements = before["allowed"], before["requirement_ids"]
    patched(monkeypatch, REVISES_THE_TASK)
    objected("adjudicate", "--agent", agent(ADJUDICATOR), "--no-critics")
    after = state.load(project)["tasks"]["M01-001"]
    assert after["allowed"] == fence
    assert after["requirement_ids"] == requirements


#: the commonest plan repair there is: the work a task needs does not exist, so the
#: patch adds it and points the task at it. Both halves in one patch, which is the
#: only way to write it — a patch is applied atomically, so there is no earlier one
#: for the new task to have landed in.
ADDS_AND_DEPENDS_ON_IT = {
    "analysis": "M01-002 needs work that is not in the plan",
    "add_tasks": [
        {
            "id": "proposed-queue-depth",
            "title": "Expose queue depth to the operator",
            "milestone": "M01",
            "requirement_ids": ["REQ-003"],
            "acceptances": [
                "a failing test in ops/depth_test.go reproduces the missing view",
                "`go test ./ops` reports queue depth",
            ],
            "allowed": ["ops/depth.go", "ops/depth_test.go"],
        }
    ],
    "revise_tasks": [
        {
            "id": "M01-002",
            "depends_on": ["M01-001", "proposed-queue-depth"],
        }
    ],
}


def test_a_revision_may_depend_on_a_task_the_same_patch_adds(
    objected, project, monkeypatch
):
    """The patch is one transaction, so the new task's id is only the patch's word.

    This was refused. `unknown-dependency` was judged against the committed graph
    alone, which cannot contain a task the patch is introducing — so the one shape a
    missing-dependency finding actually calls for was the one shape writ would not
    accept, and the adjudicator's only way past the refusal was to drop the edge and
    leave the finding unrepaired.
    """
    patched(monkeypatch, ADDS_AND_DEPENDS_ON_IT)
    code, _, _ = objected("adjudicate", "--agent", agent(ADJUDICATOR), "--no-critics")
    data = state.load(project)
    request = repair.requests(data)[-1]
    assert not request.get("refusals"), request.get("refusals")
    assert request["status"] == "applied"
    # And the edge points at the id writ minted, not the one the patch made up.
    added = request["applied_tasks"][0]
    assert added in data["tasks"]["M01-002"]["depends_on"]
    assert "proposed-queue-depth" not in data["tasks"]["M01-002"]["depends_on"]
    assert code == 0


def test_a_revision_still_may_not_depend_on_nothing(objected, project, monkeypatch):
    """The rule it relaxes, still enforced: a task the patch does not add either."""
    patched(
        monkeypatch,
        {
            "analysis": "points at thin air",
            "revise_tasks": [{"id": "M01-002", "depends_on": ["M09-999"]}],
        },
    )
    objected("adjudicate", "--agent", agent(ADJUDICATOR), "--no-critics")
    data = state.load(project)
    reasons = [
        reason["category"]
        for refusal in repair.requests(data)[-1].get("refusals") or []
        for reason in refusal["reasons"]
    ]
    assert "unknown-dependency" in reasons


def test_a_revision_may_not_drop_a_criterion(objected, project, monkeypatch):
    """The failure the review warned about: closing a finding by lowering the bar."""
    patched(
        monkeypatch,
        {
            "revise_tasks": [
                {"id": "M01-001", "acceptances": ["it works"]},
            ]
        },
    )
    code, out, _ = objected(
        "adjudicate", "--agent", agent(ADJUDICATOR), "--no-critics"
    )
    data = state.load(project)
    assert len(data["tasks"]["M01-001"]["acceptances"]) == 2
    assert code == 1
    assert "refused" in out


def test_a_revision_may_not_drop_a_requirement(objected, project, monkeypatch):
    patched(
        monkeypatch,
        {"revise_tasks": [{"id": "M01-001", "requirement_ids": []}]},
    )
    objected("adjudicate", "--agent", agent(ADJUDICATOR), "--no-critics")
    task = state.load(project)["tasks"]["M01-001"]
    assert task["requirement_ids"] == ["REQ-001"]


def test_a_revision_may_not_invent_a_requirement(objected, project, monkeypatch):
    patched(
        monkeypatch,
        {
            "revise_tasks": [
                {"id": "M01-001", "requirement_ids": ["REQ-001", "REQ-999"]}
            ]
        },
    )
    objected("adjudicate", "--agent", agent(ADJUDICATOR), "--no-critics")
    task = state.load(project)["tasks"]["M01-001"]
    assert task["requirement_ids"] == ["REQ-001"]


def test_a_gate_is_not_an_adjudicators_to_rewrite(
    writ, project, design, tmp_path, monkeypatch
):
    """A gate's criteria are the plan's own bar, not a task contract."""
    artifact = tmp_path / "plan.json"
    artifact.write_text(json.dumps(PLAN), encoding="utf-8")
    writ("init")
    writ("plan", str(design), "--from-plan", str(artifact))
    data = state.load(project)
    gate_id = next(
        task["id"] for task in data["tasks"].values() if task.get("kind") == "gate"
    )
    before = list(data["tasks"][gate_id]["acceptances"])
    with state.transaction(live := project) as data:
        plans.record_findings(
            data,
            [
                plancheck.Finding(
                    severity="error",
                    category="weak-acceptance",
                    message="the gate says nothing checkable",
                    where=gate_id,
                    source="critic:acceptance",
                )
            ],
            scope="critic:acceptance",
        )
    patched(
        monkeypatch,
        {"revise_tasks": [{"id": gate_id, "acceptances": ["it all works", "b", "c"]}]},
    )
    writ("adjudicate", "--agent", agent(ADJUDICATOR), "--no-critics")
    assert state.load(project)["tasks"][gate_id]["acceptances"] == before


def test_a_started_task_is_not_revisable(objected, project, monkeypatch):
    with state.transaction(project) as data:
        data["tasks"]["M01-001"]["status"] = "running"
    patched(monkeypatch, REVISES_THE_TASK)
    objected("adjudicate", "--agent", agent(ADJUDICATOR), "--no-critics")
    assert len(state.load(project)["tasks"]["M01-001"]["acceptances"]) == 2


def test_a_revision_is_refused_in_a_gate_repair():
    """`revise_tasks` exists for the pre-execution occasion and no other."""
    data = {
        "tasks": {
            "M01-001": {
                "id": "M01-001",
                "status": "planned",
                "acceptances": ["a", "b"],
                "requirement_ids": [],
                "depends_on": [],
            }
        },
        "requirements": {},
        "findings": [],
        "plan": {"revision": 1},
    }
    patch = repair.Patch(base_revision=1, revise_tasks=[{"id": "M01-001"}])
    request = {"id": "RR-0001", "gate": "G-M01", "findings": []}
    found = repair.validate(data, patch, request)
    assert any(f.category == "revision-after-start" for f in found)


# --------------------------------------------------------------------------
# writ owns what may be applied


def test_a_stale_patch_is_refused(objected, project, monkeypatch):
    patched(monkeypatch, dict(ADDS_THE_TASK, base_revision=99))
    code, out, _ = objected(
        "adjudicate", "--agent", agent(ADJUDICATOR), "--no-critics"
    )
    assert code == 1
    assert "refused" in out
    assert not [
        task
        for task in state.load(project)["tasks"].values()
        if task.get("repair", {}).get("scope") == "plan"
    ]


def test_an_empty_patch_is_refused(objected, project, monkeypatch):
    patched(monkeypatch, {"analysis": "I looked and it seems fine"})
    code, out, _ = objected(
        "adjudicate", "--agent", agent(ADJUDICATOR), "--no-critics"
    )
    assert code == 1
    assert "refused" in out


def test_an_accepted_finding_needs_work_that_closes_it(
    objected, project, monkeypatch
):
    patched(
        monkeypatch,
        dict(
            ADDS_THE_TASK,
            _auto_dispositions=False,
            add_tasks=[
                dict(ADDS_THE_TASK["add_tasks"][0], resolves_findings=[]),
            ],
            dispositions=[
                {
                    "finding_id": "F-0001",
                    "resolution": "accepted",
                    "change": "trust me",
                }
            ],
        ),
    )
    code, out, _ = objected(
        "adjudicate", "--agent", agent(ADJUDICATOR), "--no-critics"
    )
    assert code == 1
    assert "refused" in out


def test_a_refused_patch_leaves_the_request_open_for_the_next_round(
    objected, project, monkeypatch
):
    patched(monkeypatch, dict(ADDS_THE_TASK, base_revision=99))
    objected("adjudicate", "--agent", agent(ADJUDICATOR), "--no-critics")
    request = repair.requests(state.load(project))[0]
    assert request["status"] == "open"
    assert repair.refusals(request) >= 1


def test_a_refusal_is_followed_by_a_better_patch(objected, project, monkeypatch):
    """The loop's actual value: round two is told why round one was refused."""
    monkeypatch.setenv(
        "WRIT_TEST_PATCH",
        json.dumps([dict(ADDS_THE_TASK, base_revision=99), ADDS_THE_TASK]),
    )
    code, out, _ = objected(
        "adjudicate", "--agent", agent(ADJUDICATOR), "--no-critics"
    )
    data = state.load(project)
    assert [
        task
        for task in data["tasks"].values()
        if task.get("repair", {}).get("scope") == "plan"
    ]
    assert code == 0, out


# --------------------------------------------------------------------------
# the loop is bounded


def test_the_loop_stops_at_its_budget(objected, project, monkeypatch):
    """A patch that never closes the finding must not be tried forever."""
    patched(
        monkeypatch,
        {
            "add_tasks": [
                {
                    "id": "proposed-noop",
                    "title": "Something unrelated",
                    "milestone": "M01",
                    "acceptances": ["`go test ./x` passes", "it is covered"],
                    "allowed": ["x/x.go"],
                }
            ],
            "_auto_dispositions": False,
        },
    )
    code, out, _ = objected(
        "adjudicate",
        "--agent",
        agent(ADJUDICATOR),
        "--no-critics",
        "--max-rounds",
        "2",
    )
    data = state.load(project)
    applied = [r for r in repair.requests(data) if r["status"] == "applied"]
    assert len(applied) <= 2, [r["id"] for r in applied]
    assert "stopped:" in out
    assert code == 1


def test_a_refused_patch_does_not_spend_a_round(objected, project, monkeypatch):
    """A refusal is information for the next attempt, not a repair that happened.

    Counting agent runs against the budget instead of landed patches meant two
    refusals — the one thing writ hands straight back with the reason — exhausted a
    plan's whole repair allowance. The loop then reported the plan as adjudicated
    twice when it had not been adjudicated once, and the critics never re-read
    anything because nothing had changed for them to read.
    """
    patched(
        monkeypatch,
        [
            # Refused: the edge points at a task that does not exist and is not
            # being added either.
            {
                "analysis": "first try",
                "revise_tasks": [{"id": "M01-002", "depends_on": ["M09-999"]}],
            },
            # Then the patch that answers the finding.
            dict(ADDS_THE_TASK),
        ],
    )
    code, out, _ = objected(
        "adjudicate",
        "--agent",
        agent(ADJUDICATOR),
        "--no-critics",
        "--max-rounds",
        "1",
    )
    data = state.load(project)
    applied = [r for r in repair.requests(data) if r["status"] == "applied"]
    assert applied, out
    # The refusal is on the record, and the patch after it still landed inside a
    # budget of one. `MAX_PATCH_ATTEMPTS` is what bounds refusals.
    assert applied[-1].get("refusals"), applied[-1]
    assert repair.plan_rounds(data) == 1
    assert code == 0


def test_zero_rounds_adjudicates_nothing(objected, project, monkeypatch):
    patched(monkeypatch, ADDS_THE_TASK)
    code, out, _ = objected(
        "adjudicate",
        "--agent",
        agent(ADJUDICATOR),
        "--no-critics",
        "--max-rounds",
        "0",
    )
    assert repair.requests(state.load(project)) == []
    # Says what happened rather than reporting a budget spent: nothing was tried,
    # which is not the same fact as a plan that has been repaired to its limit.
    assert "no repair was allowed" in out
    assert code == 1


def test_an_adjudicator_that_writes_nothing_stops_the_loop(
    objected, project, monkeypatch
):
    code, out, err = objected("adjudicate", "--agent", agent(MUTE), "--no-critics")
    assert code == 1
    assert "wrote no patch" in out + err
    # And it did not leave the request claiming to be mid-planning.
    request = repair.requests(state.load(project))[0]
    assert request["status"] == "open"


def test_a_question_stops_the_loop_and_reaches_the_decision_log(
    objected, project, monkeypatch
):
    patched(
        monkeypatch,
        {
            "_auto_dispositions": False,
            "questions": [
                {
                    "id": "Q-001",
                    "question": "Is queue depth per shard or per cluster?",
                }
            ],
        },
    )
    code, out, _ = objected(
        "adjudicate", "--agent", agent(ADJUDICATOR), "--no-critics"
    )
    data = state.load(project)
    assert data["decisions"], data
    assert "per shard" in data["decisions"][0]["title"]
    assert code == 1


def test_a_finding_that_survives_its_repair_is_escalated(objected, project):
    """The worse bound: the same objection coming back after a patch closed it."""
    with state.transaction(project) as data:
        record = [
            item
            for item in plans.finding_records(data)
            if item["source"] == "critic:coverage"
        ][0]
        record["seen_count"] = repair.REPEAT_FINDING_LIMIT + 1
        record["reopened_at"] = "2026-01-01T00:00:00Z"
        repair.open_request(
            data,
            gate_id=None,
            finding_ids=[record["id"]],
            summary="still open",
            actor="adjudicator",
        )["status"] = "applied"
    assert repair.plan_repeat_findings(state.load(project))
    assert "survived" in repair.plan_exhausted(state.load(project), max_rounds=9)


def test_repeated_advisories_do_not_escalate_the_plan(objected, project):
    """The bound is about findings that were repaired, so advisories are not it.

    A critic re-reports every note it still believes each time it re-reads, so a
    plan with seventy notes crosses any seen-count limit the first time the critics
    run twice — on a plan whose blocking findings were being fixed exactly as
    intended. Counting those stopped the loop after one round and called a plan
    beyond repair over objections no patch had ever been asked to answer.
    """
    with state.transaction(project) as data:
        plans.record_findings(
            data,
            [
                plancheck.Finding(
                    severity=severity,
                    category="unjustified-task",
                    message="nothing in the design asks for it",
                    where="M01-001",
                    suggested_action="name the requirement it serves",
                    source="critic:scope",
                )
                for severity in ("note", "warning")
            ],
            scope="critic:scope",
        )
        for record in plans.finding_records(data):
            if record["severity"] in ("note", "warning"):
                record["seen_count"] = repair.REPEAT_FINDING_LIMIT + 2
                record["reopened_at"] = "2026-01-01T00:00:00Z"
        repair.open_request(
            data,
            gate_id=None,
            finding_ids=[],
            summary="one round landed",
            actor="adjudicator",
        )["status"] = "applied"
    data = state.load(project)
    assert repair.plan_repeat_findings(data) == []
    assert repair.plan_exhausted(data, max_rounds=9) == ""


def test_a_blocking_finding_reopened_once_escalates(objected, project):
    """`reopened_at` alone is the signal: a re-check disagreed with a patch.

    Separate from the seen-count path because it needs no repetition to be
    conclusive — the patch said it closed the finding and the critic that raised it
    found it again on the patched plan.
    """
    with state.transaction(project) as data:
        record = [
            item
            for item in plans.finding_records(data)
            if item["source"] == "critic:coverage"
        ][0]
        record["seen_count"] = 1
        record["reopened_at"] = "2026-01-01T00:00:00Z"
        repair.open_request(
            data,
            gate_id=None,
            finding_ids=[record["id"]],
            summary="still open",
            actor="adjudicator",
        )["status"] = "applied"
    data = state.load(project)
    assert repair.plan_repeat_findings(data) == [record["id"]]
    assert "survived" in repair.plan_exhausted(data, max_rounds=9)


def test_the_plan_bound_counts_only_applied_repairs(objected, project):
    with state.transaction(project) as data:
        repair.open_request(
            data,
            gate_id=None,
            finding_ids=[],
            summary="refused twice",
            actor="adjudicator",
        )
    # An open request is not a round: nothing landed, so nothing was spent.
    assert repair.plan_rounds(state.load(project)) == 0


# --------------------------------------------------------------------------
# a finding closes on a re-check, not on the patch's word


#: a critic that keeps objecting no matter what the patch did.
#:
#: Which critic it is comes from the report path, not the brief: writ writes each
#: critic's report under a directory named for it, and no critic's brief happens to
#: contain its own name.
STUBBORN_CRITIC = """
import json, re, sys
prompt = sys.stdin.read()
path = re.search(r'Write your findings as JSON to this exact path:\\n  (\\S+)', prompt).group(1)
findings = []
if "/coverage/" in path:
    findings = [{
        "severity": "blocking",
        "category": "missing-coverage",
        "where": "REQ-003",
        "message": "No task implements the queue depth view",
        "suggested_action": "add a task, or mark it out of scope",
    }]
open(path, "w").write(json.dumps({"findings": findings, "summary": "still not covered"}))
"""

#: a critic satisfied by whatever the patch did
SATISFIED_CRITIC = """
import json, re, sys
prompt = sys.stdin.read()
path = re.search(r'Write your findings as JSON to this exact path:\\n  (\\S+)', prompt).group(1)
open(path, "w").write(json.dumps({"findings": [], "summary": "the repair covers it"}))
"""


def test_the_critics_re_read_the_patched_plan(objected, project, monkeypatch):
    """A critic that passed revision 2 has not reviewed revision 3."""
    patched(monkeypatch, ADDS_THE_TASK)
    objected(
        "adjudicate",
        "--agent",
        agent(ADJUDICATOR),
        "--critic-agent",
        agent(SATISFIED_CRITIC),
    )
    data = state.load(project)
    revision = plans.revision(data)
    reviewed = {
        entry["critic"]
        for entry in data.get("reviews", [])
        if entry.get("revision") == revision
    }
    assert reviewed, data.get("reviews")


def test_a_finding_a_critic_still_reports_does_not_close(
    objected, project, monkeypatch
):
    """The invariant: the patch's claim does not settle it, the re-review does.

    The adjudicator accepts the finding and adds work. The coverage critic reads the
    patched plan and says the requirement is still uncovered. The finding has to be
    open at the end — a repair that closed its own objection would be the loop
    laundering a plan past its critics.
    """
    patched(monkeypatch, ADDS_THE_TASK)
    code, out, _ = objected(
        "adjudicate",
        "--agent",
        agent(ADJUDICATOR),
        "--critic-agent",
        agent(STUBBORN_CRITIC),
        "--max-rounds",
        "1",
    )
    data = state.load(project)
    reopened = [
        item
        for item in plans.finding_records(data)
        if item["category"] == "missing-coverage"
        and item["disposition"] == "open"
    ]
    assert reopened, plans.finding_records(data)
    assert code == 1


# --------------------------------------------------------------------------
# what it refuses to do at all


def test_adjudicating_a_clean_plan_does_nothing(writ, project, design, tmp_path):
    artifact = tmp_path / "plan.json"
    artifact.write_text(json.dumps(PLAN), encoding="utf-8")
    writ("init")
    writ("plan", str(design), "--from-plan", str(artifact), "--auto-approve")
    code, out, _ = writ("adjudicate", "--no-critics")
    assert code == 0
    assert "nothing blocking" in out
    assert repair.requests(state.load(project)) == []


def test_an_executing_plan_is_repaired_by_its_gates(objected, project):
    with state.transaction(project) as data:
        plans.set_status(data, "executing")
    code, _, err = objected("adjudicate", "--no-critics")
    assert code == 2
    assert "executing" in err


def test_there_is_nothing_to_adjudicate_without_a_plan(writ):
    writ("init")
    code, _, err = writ("adjudicate", "--no-critics")
    assert code == 2
    assert "no plan" in err


def test_a_plan_request_is_never_dispatched_as_a_gate_repair(objected, project):
    """The scheduler's half of the separation: a plan request has no gate."""
    from writ import orchestrator

    with state.transaction(project) as data:
        plans.set_status(data, "approved")
        repair.open_request(
            data,
            gate_id=None,
            finding_ids=[],
            summary="open, pre-execution",
            actor="adjudicator",
        )
    data = state.load(project)
    job = orchestrator.next_job(data, busy=[], budget=None, started=[])
    assert job is None or job.role != "repair"
    # And `gate_requests` is the view the scheduler side should be reading.
    assert repair.gate_requests(data) == []


# --------------------------------------------------------------------------
# reached from `writ plan`


def test_plan_repair_answers_what_the_critics_found_before_approval(
    writ, project, design, tmp_path, monkeypatch
):
    """One command from document to runnable graph, findings answered on the way.

    The loop existed but nothing in `writ plan` reached it, so an unattended run
    that asked for the critics and `--auto-approve` stopped at `needs-approval`
    with nobody whose job was to answer what they found. `--repair` is that path,
    and approval still comes last: it approves the plan as repair left it.
    """
    artifact = tmp_path / "plan.json"
    artifact.write_text(json.dumps(PLAN), encoding="utf-8")
    writ("init")
    # Reports on its first read and is satisfied on the re-read, which is what a
    # repair that worked looks like from a critic's side. A stub that reported the
    # same finding forever would be testing that a finding survives its own repair,
    # which is a different claim and already covered above.
    monkeypatch.setenv("WRIT_TEST_ONCE", str(tmp_path / "reported"))
    patched(monkeypatch, ADDS_THE_TASK)
    code, out, _ = writ(
        "plan", str(design), "--from-plan", str(artifact),
        "--critics", "coverage", "--critic-agent", agent(CRITIC_ONCE),
        "--repair", "--adjudicator-agent", agent(ADJUDICATOR),
        "--auto-approve", "--quiet",
    )
    assert code == 0
    assert "repairing the plan" in out
    data = state.load(project)
    # The patch landed, the finding closed on the re-check, and approval followed.
    assert repair.requests(data)
    record = plans.plan_status(data)
    assert record["status"] == "approved"
    assert record["approved_by"] == "writ --auto-approve"


def test_check_stops_listing_what_the_repair_answered(
    writ, project, design, tmp_path, monkeypatch
):
    """The end of the loop, from the reader's side.

    Three things had to be true for this to work and none of them was: the patch
    had to be accepted, the critic's re-read had to be able to close its own
    finding, and the adjudicator's `accepted` had to become `resolved` once nothing
    reported it. Until then `writ check` listed every objection the repair had just
    answered, which is what a person reads to decide whether to approve.
    """
    artifact = tmp_path / "plan.json"
    artifact.write_text(json.dumps(PLAN), encoding="utf-8")
    writ("init")
    monkeypatch.setenv("WRIT_TEST_ONCE", str(tmp_path / "reported"))
    patched(monkeypatch, ADDS_THE_TASK)
    writ(
        "plan", str(design), "--from-plan", str(artifact),
        "--critics", "coverage", "--critic-agent", agent(CRITIC_ONCE),
        "--repair", "--adjudicator-agent", agent(ADJUDICATOR),
        "--quiet",
    )
    data = state.load(project)
    answered = [
        record["id"]
        for record in plans.finding_records(data)
        if record["category"] == "missing-coverage"
    ]
    assert answered
    for finding_id in answered:
        assert plans.get_finding(data, finding_id)["disposition"] == "resolved"
    code, out, _ = writ("check")
    # Not listed, because it is not open — and the plan is no longer held by it.
    for finding_id in answered:
        assert finding_id not in out
    assert code == 0


def test_plan_without_repair_leaves_the_findings_standing(
    writ, project, design, tmp_path, monkeypatch
):
    """Opt-in, like the critics: the loop spends agent runs, so it waits to be asked."""
    artifact = tmp_path / "plan.json"
    artifact.write_text(json.dumps(PLAN), encoding="utf-8")
    writ("init")
    monkeypatch.setenv("WRIT_TEST_ONCE", str(tmp_path / "reported"))
    patched(monkeypatch, ADDS_THE_TASK)
    code, out, _ = writ(
        "plan", str(design), "--from-plan", str(artifact),
        "--critics", "coverage", "--critic-agent", agent(CRITIC_ONCE),
        "--auto-approve", "--quiet",
    )
    assert code == 0
    assert "repairing the plan" not in out
    data = state.load(project)
    assert repair.requests(data) == []
    assert plans.plan_status(data)["status"] == "needs-approval"


def test_a_resumed_round_reaches_the_phase_record(objected, project, monkeypatch):
    """`writ adjudicate` draws its rounds into the attempt they belong to.

    The command is how a stopped loop is resumed: the loop gives up with findings
    open, a human settles them, and this carries on. It wrote nothing to the phase
    record, so the dashboard kept showing the planning run that stopped — the same
    critics, the one repair box it had already drawn — while the rounds that
    followed were on disk and in `state.json`. The loop had run and the only view of
    it said it had not.
    """
    patched(monkeypatch, ADDS_THE_TASK)
    # A phase for the plan under test, as `writ plan` would have left it: closed,
    # with the steps that ran. The fixture commits from `--from-plan`, which records
    # no pipeline, so the id the two are matched on is set here too.
    plan_id = "design-20260101T000000"
    with state.transaction(project) as data:
        plans.plan_status(data)["pipeline"] = {"plan_id": plan_id}
    phase_id = phases.begin(
        project,
        doc="design.md",
        plan_id=plan_id,
        steps=[phases.make_step(id="commit", kind="commit", name="commit", wave=0)],
    )
    phases.finish(project, phase_id, status="done")

    code, out, _ = objected(
        "adjudicate", "--agent", agent(ADJUDICATOR), "--no-critics"
    )
    assert code == 0, out
    record = phases.current(state.load(project)) or {}
    rounds = [
        entry for entry in record.get("steps", []) if entry.get("kind") == "repair"
    ]
    assert rounds, [entry.get("id") for entry in record.get("steps", [])]
    assert rounds[0]["status"] == "ok", rounds[0]
    assert "applied" in rounds[0].get("note", ""), rounds[0]
    # And the phase it reopened is closed again, so nothing looks in flight.
    assert record.get("status") == "done", record.get("status")
    assert record.get("finished_at")


def test_a_round_is_not_drawn_into_another_plans_attempt(objected, project, monkeypatch):
    """A phase for a different plan is left alone.

    Adjudication is about one committed plan. Attaching its rounds to whatever
    attempt happened to be newest would draw them into a graph they were no part of,
    so a phase whose `plan_id` does not match is declined and the rounds go
    unrecorded rather than recorded in the wrong place.
    """
    patched(monkeypatch, ADDS_THE_TASK)
    phase_id = phases.begin(
        project,
        doc="other.md",
        plan_id="some-other-plan",
        steps=[phases.make_step(id="commit", kind="commit", name="commit", wave=0)],
    )
    phases.finish(project, phase_id, status="done")

    code, out, _ = objected(
        "adjudicate", "--agent", agent(ADJUDICATOR), "--no-critics"
    )
    assert code == 0, out
    record = phases.current(state.load(project)) or {}
    assert record.get("plan_id") == "some-other-plan"
    assert [e for e in record.get("steps", []) if e.get("kind") == "repair"] == []


def test_a_new_blocking_finding_after_a_patch_opens_another_round(
    writ, project, design, tmp_path, monkeypatch
):
    """The loop keeps going while each round is answering something new.

    This is the case the production bug broke, and nothing covered it: round 1 lands,
    the critics re-read the patched plan, and they object to the work the patch just
    added. That must open round 2 — a plan is not beyond repair because repairing it
    revealed the next problem.

    It broke because the repeat-finding bound counted advisories. A real plan carries
    dozens of notes the critics re-report on every read, so the first re-review pushed
    all of them past the limit at once and the loop escalated, leaving the blocking
    findings it had just been handed unanswered.
    """
    artifact = tmp_path / "plan.json"
    artifact.write_text(json.dumps(PLAN), encoding="utf-8")
    writ("init")
    monkeypatch.setenv("WRIT_TEST_READS", str(tmp_path / "reads"))
    # Advisories alongside the blocking ones, at the volume a real plan has, so the
    # test fails if they are ever counted towards escalation again.
    patched(monkeypatch, ADDS_THE_TASK)
    with state.transaction(project) as data:
        plans.record_findings(
            data,
            [
                plancheck.Finding(
                    severity="note",
                    category="unknown-path",
                    message=f"allowed path 'src/thing{n}.py' does not exist yet",
                    where="M01-001",
                    suggested_action="confirm the path",
                    source="critic:scope",
                )
                for n in range(40)
            ],
            scope="critic:scope",
        )
        for record in plans.finding_records(data):
            if record["severity"] == "note":
                record["seen_count"] = repair.REPEAT_FINDING_LIMIT + 3

    code, out, _ = writ(
        "plan", str(design), "--from-plan", str(artifact),
        "--critics", "coverage", "--critic-agent", agent(CRITIC_MOVES_ON),
        "--repair", "--adjudicator-agent", agent(ADJUDICATOR),
        "--max-rounds", "4", "--quiet",
    )
    data = state.load(project)
    applied = [r for r in repair.requests(data) if r.get("status") == "applied"]
    assert len(applied) >= 2, (
        f"the loop stopped after {len(applied)} round(s); out:\n{out}"
    )
    # Both objections were answered, and nothing blocking is left standing.
    # Both of the critic's objections were answered. Deliberately not "nothing
    # blocking is left": the stub patch adds a task owning a path an earlier one
    # owns, so writ's own shared-ownership check objects to the fixture — which is
    # that check doing its job, and not what this test is about.
    raised = [
        record
        for record in plans.finding_records(data)
        if record["scope"] == "critic:coverage"
    ]
    assert len(raised) == 2, [r["id"] for r in raised]
    for record in raised:
        assert record["disposition"] in ("resolved", "accepted"), record
    # Each round answered a different objection, which is what distinguishes this
    # from a finding surviving its repair.
    assert {record["where"] for record in raised} == {"REQ-003", "REQ-004"}


def test_each_resumed_round_gets_its_own_step(objected, project, monkeypatch):
    """Two rounds, two boxes. The second is appended when it opens.

    How many rounds there will be is not knowable when the loop starts — it depends
    on what each patch fixed — so the record has to grow as the loop does. A single
    step reused by every round would show one repair where three happened.
    """
    monkeypatch.setenv("WRIT_TEST_READS", str(project / "reads"))
    patched(monkeypatch, ADDS_THE_TASK)
    plan_id = "design-20260101T000000"
    with state.transaction(project) as data:
        plans.plan_status(data)["pipeline"] = {"plan_id": plan_id}
    phase_id = phases.begin(
        project,
        doc="design.md",
        plan_id=plan_id,
        steps=[phases.make_step(id="commit", kind="commit", name="commit", wave=0)],
    )
    phases.finish(project, phase_id, status="done")

    code, out, _ = objected(
        "adjudicate",
        "--agent", agent(ADJUDICATOR),
        "--critics", "coverage",
        "--critic-agent", agent(CRITIC_MOVES_ON),
        "--max-rounds", "4",
    )
    record = phases.current(state.load(project)) or {}
    rounds = [e for e in record.get("steps", []) if e.get("kind") == "repair"]
    assert len(rounds) >= 2, (
        f"{len(rounds)} repair step(s) for a loop that ran more than one round; "
        f"steps: {[e.get('id') for e in record.get('steps', [])]}\n{out}"
    )
    # Distinct ids, each pointing at its own transcript directory.
    assert len({e["id"] for e in rounds}) == len(rounds), rounds
    assert len({e.get("directory") for e in rounds}) == len(rounds), rounds
    # And the re-reviews landed on the same phase.
    assert [e for e in record.get("steps", []) if e.get("kind") == "critic"], record
