"""Plan-level gates, and the repair loop a failing gate starts.

The point of a gate is that it judges something no single task can: whether work
that each passed its own review adds up to what was asked for. So the tests here
are mostly about what happens *after* a gate says no — that the findings become
work, that the work goes in front of the gate rather than behind it, that the
graph stays acyclic across rounds, and that writ rather than an agent decides what
may be applied.

The agents are real subprocesses writing real files, as in test_run.py: the parts
worth testing are the ones that only exist once a scheduler is running.
"""
from __future__ import annotations

import json
import shlex
import sys

import pytest

from writ import gates, orchestrator, plancheck, plans, repair, state
from writ.state import WritError


# --------------------------------------------------------------------------
# fake agents

IMPLEMENTER = """
import json, re, sys
prompt = sys.stdin.read()
path = re.search(r'^  (\\S*verdict\\.json)$', prompt, re.M).group(1)
total = int(re.search(r'has (\\d+) acceptance criteri', prompt).group(1))
open(path, "w").write(json.dumps({
    "outcome": "complete",
    "summary": "did the work",
    "criteria": [
        {"number": i, "status": "passed", "evidence": "ran: pytest -q -> ok"}
        for i in range(1, total + 1)
    ],
}))
"""

REVIEWER = """
import json, re, sys
prompt = sys.stdin.read()
path = re.search(r'^  (\\S*verdict\\.json)$', prompt, re.M).group(1)
total = int(re.search(r'has (\\d+) acceptance criteri', prompt).group(1))
open(path, "w").write(json.dumps({
    "decision": "accept",
    "summary": "verified independently",
    "criteria": [
        {"number": i, "status": "passed", "evidence": "re-ran: pytest -q -> ok"}
        for i in range(1, total + 1)
    ],
}))
"""

#: an agent that plays whichever role the prompt says it is
MULTI = """
import json, os, re, sys
prompt = sys.stdin.read()
state_dir = os.environ["WRIT_TEST_DIR"]

def verdict_path():
    return re.search(r'^  (\\S*verdict\\.json)$', prompt, re.M).group(1)

def criteria_count():
    return int(re.search(r'has (\\d+) criteria', prompt).group(1))

# Each role's prompt opens by saying what it is. Matching those exact
# openings rather than a keyword: "independent" and "review" both appear
# in the middle of the implementer's prompt, so a looser test makes the
# implementer write a reviewer's verdict and nothing after that makes sense.
IS_REPAIR = "You are planning a REPAIR" in prompt
IS_GATE = "MILESTONE GATE" in prompt or "FINAL GATE" in prompt
IS_REVIEW = "You are reviewing ONE completed task" in prompt

def bump(name):
    path = os.path.join(state_dir, name)
    seen = int(open(path).read()) if os.path.exists(path) else 0
    open(path, "w").write(str(seen + 1))
    return seen + 1

if IS_REPAIR:
    patch = os.path.join(
        re.search(r'Write the patch as JSON to this exact path:\\n  (\\S+)', prompt).group(1)
    )
    revision = int(re.search(r'Set `base_revision` to (\\d+)', prompt).group(1))
    finding = re.search(r'(F-\\d+)', prompt).group(1)
    round_no = bump("repair")
    open(patch, "w").write(json.dumps(json.loads(os.environ["WRIT_TEST_PATCH"]) | {
        "base_revision": revision,
        "add_tasks": [
            dict(task, id=task["id"] + "-" + str(round_no),
                 resolves_findings=[finding])
            for task in json.loads(os.environ["WRIT_TEST_PATCH"])["add_tasks"]
        ],
        "dispositions": [
            {"finding_id": finding, "resolution": "accepted",
             "change": "added the missing work"}
        ],
    }))
elif IS_GATE:
    total = criteria_count()
    attempt = bump("gate")
    fail = int(os.environ.get("WRIT_TEST_GATE_FAILS", "1"))
    if attempt <= fail:
        open(verdict_path(), "w").write(json.dumps({
            "decision": "needs-repair",
            "summary": "the pieces do not meet",
            "criteria": [
                {"number": i, "status": "failed", "evidence": "the seam is broken"}
                for i in range(1, total + 1)
            ],
            "findings": [
                {"severity": "blocking",
                 "summary": "nothing writes the projection the log implies",
                 "where": "internal/store/projection.go",
                 "suggested_action": "add the writer"},
            ],
        }))
    else:
        open(verdict_path(), "w").write(json.dumps({
            "decision": "pass",
            "summary": "the integrated work matches the design",
            "criteria": [
                {"number": i, "status": "passed", "evidence": "ran the suite"}
                for i in range(1, total + 1)
            ],
        }))
elif IS_REVIEW:
    total = int(re.search(r'has (\\d+) acceptance criteri', prompt).group(1))
    open(verdict_path(), "w").write(json.dumps({
        "decision": "accept",
        "summary": "verified independently",
        "criteria": [
            {"number": i, "status": "passed", "evidence": "re-ran the suite"}
            for i in range(1, total + 1)
        ],
    }))
else:
    total = int(re.search(r'has (\\d+) acceptance criteri', prompt).group(1))
    open(verdict_path(), "w").write(json.dumps({
        "outcome": "complete",
        "summary": "did the work",
        "criteria": [
            {"number": i, "status": "passed", "evidence": "ran: pytest -q -> ok"}
            for i in range(1, total + 1)
        ],
    }))
"""


#: MULTI, except the gate claims a pass while reporting on no criteria at all
SILENT_GATE = MULTI.replace(
    """        open(verdict_path(), "w").write(json.dumps({
            "decision": "pass",
            "summary": "the integrated work matches the design",
            "criteria": [
                {"number": i, "status": "passed", "evidence": "ran the suite"}
                for i in range(1, total + 1)
            ],
        }))""",
    """        open(verdict_path(), "w").write(json.dumps({
            "decision": "pass",
            "summary": "the integrated work matches the design",
            "criteria": [],
        }))""",
)

REPAIR_PATCH = {
    "analysis": "the projection writer was never planned",
    "add_tasks": [
        {
            "id": "proposed-projection-writer",
            "title": "Write the projection the log implies",
            "allowed": ["internal/store/projection_writer.go"],
            "acceptances": [
                "a failing test in internal/store/writer_test.go reproduces the gap",
                "`go test ./internal/store` passes with the writer in place",
            ],
        }
    ],
}


def agent(script: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


@pytest.fixture
def looping(writ, design, project, tmp_path, monkeypatch):
    """A project whose gate fails once, then passes after a repair lands."""
    monkeypatch.setenv("WRIT_TEST_DIR", str(tmp_path / "counters"))
    (tmp_path / "counters").mkdir()
    monkeypatch.setenv("WRIT_TEST_PATCH", json.dumps(REPAIR_PATCH))
    writ("init")
    writ("plan", str(design), "--extract", "--auto-approve")
    return writ


# --------------------------------------------------------------------------
# installation


def test_a_plan_gets_a_gate_per_milestone_and_one_at_the_end(approved, project):
    data = state.load(project)
    assert [gate["id"] for gate in gates.gates(data)] == [
        "G-FINAL", "G-M01", "G-M02", "G-M03",
    ]
    final = gates.final_gate(data)
    # The final gate waits on every milestone gate, which is what makes it the
    # last thing in the graph rather than merely another node in it.
    assert set(final["depends_on"]) >= {"G-M01", "G-M02", "G-M03"}


def test_a_milestone_gate_waits_for_its_own_tasks(approved, project):
    data = state.load(project)
    gate = data["tasks"]["G-M02"]
    assert set(gate["depends_on"]) == {"M02-001", "M02-002"}


def test_installing_twice_does_not_duplicate_a_gate(approved, project):
    with state.transaction(project) as data:
        gates.install(data)
        gates.install(data)
    data = state.load(project)
    assert len(gates.gates(data)) == 4


def test_a_gate_is_a_task_so_the_graph_walks_it(approved, project):
    data = state.load(project)
    gate = data["tasks"]["G-M01"]
    assert gate["kind"] == "gate"
    assert gate["status"] == "planned"
    # Not a special case anywhere: the ordinary readiness rule applies to it.
    assert orchestrator._stalled(data) == []


# --------------------------------------------------------------------------
# approval


def _block(project):
    """A blocking critic finding, the kind a plan cannot run past."""
    with state.transaction(project) as data:
        plans.record_findings(
            data,
            [
                plancheck.Finding(
                    severity="error",
                    category="uncovered-requirement",
                    message="nothing builds the operator view",
                    where="M01-001",
                    source="critic:fidelity",
                )
            ],
            scope="critic:fidelity",
        )
        plans.run_check(data, root=project)


def test_a_plan_with_a_blocking_finding_will_not_run(writ, project, design):
    writ("init")
    writ("plan", str(design), "--extract", "--auto-approve")
    _block(project)
    data = state.load(project)
    assert plans.plan_status(data)["status"] == "needs-approval"
    code, _, err = writ("run", "--agent", agent(IMPLEMENTER))
    assert code == 2
    assert "needs-approval" in err and "writ check" in err


def test_forced_approval_needs_a_reason_and_records_it(writ, project, design):
    writ("init")
    writ("plan", str(design), "--extract", "--auto-approve")
    _block(project)
    assert writ("approve", "--force")[0] == 2
    code, out, _ = writ("approve", "--force", "--reason", "shipping the spike")
    assert code == 0
    record = plans.plan_status(state.load(project))
    assert record["status"] == "approved" and record["forced"] is True
    assert record["approval_note"] == "shipping the spike"


# --------------------------------------------------------------------------
# the loop


def _repaired_gate(data):
    """The gate that asked for repair. Whichever ran first, the story is the same."""
    for gate in gates.gates(data):
        decisions = [a["decision"] for a in gates.attempts(gate)]
        if "needs-repair" in decisions:
            return gate
    raise AssertionError(
        "no gate asked for repair: "
        + repr({g["id"]: [a["decision"] for a in gates.attempts(g)] for g in gates.gates(data)})
    )


def test_a_failing_gate_turns_its_findings_into_work_in_front_of_itself(
    looping, project
):
    code, out, _ = looping(
        "run", "--agent", agent(MULTI), "--reviewer", agent(MULTI)
    )
    assert code == 0, out
    data = state.load(project)
    gate = _repaired_gate(data)

    # The repair task exists, and it is a dependency *of* the gate. This is the
    # whole cycle-avoidance argument: repair work never waits on the gate that
    # asked for it, the gate waits on the repair.
    added = [
        task_id
        for task_id, task in data["tasks"].items()
        if "projection the log implies" in task["title"]
    ]
    assert added, sorted(data["tasks"])
    for task_id in added:
        assert task_id in gate["depends_on"]
        assert gate["id"] not in data["tasks"][task_id]["depends_on"]

    # And the graph is still a DAG, which is the invariant the direction buys.
    from writ.model import check_dag

    check_dag(data)


def test_the_gate_runs_again_after_the_repair_and_can_pass(looping, project):
    looping("run", "--agent", agent(MULTI), "--reviewer", agent(MULTI))
    data = state.load(project)
    gate = _repaired_gate(data)
    decisions = [a["decision"] for a in gates.attempts(gate)]
    assert decisions == ["needs-repair", "pass"], decisions
    assert gate["status"] == "completed"


def test_a_repair_bumps_the_plan_revision(looping, project):
    before = plans.revision(state.load(project))
    looping("run", "--agent", agent(MULTI), "--reviewer", agent(MULTI))
    assert plans.revision(state.load(project)) > before


def test_the_finding_closes_only_when_the_gate_passes(looping, project):
    looping("run", "--agent", agent(MULTI), "--reviewer", agent(MULTI))
    data = state.load(project)
    # A gate's findings are scoped to the gate that made them, so they are
    # distinguishable from writ's own checks in the one shared ledger.
    gate_findings = [
        record
        for record in plans.finding_records(data)
        if str(record.get("source", "")).startswith("gate:")
    ]
    assert gate_findings, plans.finding_records(data)
    # The finding closed because the gate passed on the repaired code — not
    # because the repair task reported completion.
    assert all(record["disposition"] == "resolved" for record in gate_findings)


def test_a_gate_that_keeps_failing_stops_instead_of_looping(
    looping, project, monkeypatch
):
    monkeypatch.setenv("WRIT_TEST_GATE_FAILS", "99")
    code, out, _ = looping("run", "--agent", agent(MULTI), "--reviewer", agent(MULTI))
    assert code == 0
    data = state.load(project)
    gate = data["tasks"]["G-M02"]
    assert gate["status"] == "failed" or gates.rounds(gate) <= repair.DEFAULT_MAX_REPAIR_ROUNDS + 1
    # Whatever it ends as, it must not have run unboundedly.
    assert len(gates.attempts(gate)) <= repair.DEFAULT_MAX_REPAIR_ROUNDS + 2


# --------------------------------------------------------------------------
# writ owns what may be applied


def _request(project, gate_id="G-M02"):
    with state.transaction(project) as data:
        finding = plans.record_findings(
            data,
            [
                __import__("writ.plancheck", fromlist=["Finding"]).Finding(
                    severity="error",
                    category="integration",
                    message="the seam is broken",
                    where=gate_id,
                    source="gate",
                )
            ],
            scope="gate",
        )[0]
        return repair.open_request(
            data,
            gate_id=gate_id,
            finding_ids=[finding.id],
            summary="the seam is broken",
            actor="gate",
        )["id"], finding.id


def test_a_patch_against_a_stale_revision_is_refused(approved, project):
    request_id, finding_id = _request(project)
    with state.transaction(project) as data:
        plans.bump(data)
        request = repair.get_request(data, request_id)
        patch = repair.load_patch(json.dumps({
            "base_revision": 0,
            "add_tasks": [],
            "dispositions": [
                {"finding_id": finding_id, "resolution": "declined",
                 "reason": "the seam is fine"}
            ],
        }))
        findings = repair.validate(data, patch, request)
    assert any(f.category == "stale-patch" for f in findings)


def test_a_patch_that_would_make_a_cycle_is_refused(approved, project):
    request_id, finding_id = _request(project)
    with state.transaction(project) as data:
        request = repair.get_request(data, request_id)
        patch = repair.load_patch(json.dumps({
            "base_revision": plans.revision(data),
            "add_tasks": [
                {
                    "id": "fix-it",
                    "title": "Fix the seam between the log and the projection",
                    "milestone": "M02",
                    # Depending on the gate it repairs is exactly the cycle.
                    "depends_on": ["G-M02"],
                    "allowed": ["internal/store/seam.go"],
                    "acceptances": [
                        "a failing test in internal/store/seam_test.go reproduces it",
                        "`go test ./internal/store` passes",
                    ],
                }
            ],
            "dispositions": [
                {"finding_id": finding_id, "resolution": "accepted",
                 "change": "added fix-it"}
            ],
        }))
        findings = repair.validate(data, patch, request)
    assert any(f.category == "gate-cycle" for f in findings)


def test_a_patch_that_leaves_a_blocking_finding_undisposed_is_refused(
    approved, project
):
    request_id, _ = _request(project)
    with state.transaction(project) as data:
        request = repair.get_request(data, request_id)
        patch = repair.load_patch(json.dumps({
            "base_revision": plans.revision(data),
            "add_tasks": [
                {
                    "id": "fix-it",
                    "title": "Fix the seam between the log and the projection",
                    "milestone": "M02",
                    "allowed": ["internal/store/seam.go"],
                    "acceptances": [
                        "a failing test in internal/store/seam_test.go reproduces it",
                        "`go test ./internal/store` passes",
                    ],
                }
            ],
            "dispositions": [],
        }))
        findings = repair.validate(data, patch, request)
    assert any(f.category == "undisposed-finding" for f in findings)


def test_a_patch_may_not_touch_a_task_an_agent_is_working_on(approved, project):
    request_id, finding_id = _request(project)
    with state.transaction(project) as data:
        data["tasks"]["M02-001"]["status"] = "running"
        request = repair.get_request(data, request_id)
        patch = repair.load_patch(json.dumps({
            "base_revision": plans.revision(data),
            "add_tasks": [],
            "add_dependencies": [
                {"from": "M02-001", "to": "M01-001", "reason": "needs the log"}
            ],
            "dispositions": [
                {"finding_id": finding_id, "resolution": "accepted",
                 "change": "ordered them"}
            ],
        }))
        findings = repair.validate(data, patch, request)
    assert any(f.category == "contract-in-flight" for f in findings)


def test_writ_refuses_a_patch_and_the_request_stays_open(approved, project):
    request_id, finding_id = _request(project)
    with state.transaction(project) as data:
        request = repair.get_request(data, request_id)
        patch = repair.load_patch(json.dumps({
            "base_revision": plans.revision(data),
            "add_tasks": [],
            "dispositions": [],
        }))
        findings = repair.validate(data, patch, request)
        assert any(f.blocking for f in findings)
        # Nothing was applied, because validation is what decides that.
        assert "fix-it" not in data["tasks"]
        assert repair.get_request(data, request_id)["status"] == "open"


def test_a_valid_patch_is_applied_and_closes_the_request(approved, project):
    request_id, finding_id = _request(project)
    with state.transaction(project) as data:
        before = plans.revision(data)
        request = repair.get_request(data, request_id)
        patch = repair.load_patch(json.dumps({
            "base_revision": before,
            "add_tasks": [
                {
                    "id": "fix-it",
                    "title": "Fix the seam between the log and the projection",
                    "milestone": "M02",
                    "resolves_findings": [finding_id],
                    "allowed": ["internal/store/seam.go"],
                    "acceptances": [
                        "a failing test in internal/store/seam_test.go reproduces it",
                        "`go test ./internal/store` passes",
                    ],
                }
            ],
            "dispositions": [
                {"finding_id": finding_id, "resolution": "accepted",
                 "change": "added fix-it"}
            ],
        }))
        assert [f.line() for f in repair.validate(data, patch, request) if f.blocking] == []
        applied = repair.apply_patch(data, patch, request, actor="planner")
        assert applied["tasks"]
        new_id = applied["tasks"][0]
        assert new_id in data["tasks"]["G-M02"]["depends_on"]
        assert repair.get_request(data, request_id)["status"] == "applied"
        assert plans.revision(data) > before


def test_two_tasks_added_to_one_milestone_get_two_ids(approved, project):
    """The collision that rolled back a real round-3 patch.

    `_next_repair_id` reads the ids already in the plan, and the loop that mints
    them used to run to completion before the loop that inserts them. So two tasks
    added to one milestone both saw the same graph and minted the same id: the
    first insert succeeded, the second raised `task M06-006 already exists`, and
    the whole patch rolled back naming an id that was nowhere in the plan.
    """
    request_id, finding_id = _request(project)
    with state.transaction(project) as data:
        request = repair.get_request(data, request_id)
        patch = repair.load_patch(json.dumps({
            "base_revision": plans.revision(data),
            "add_tasks": [
                {
                    "id": "first",
                    "title": "Reinforce the seam on a confirmed read",
                    "milestone": "M02",
                    "resolves_findings": [finding_id],
                    "acceptances": ["`go test ./internal/store` passes"],
                },
                {
                    "id": "second",
                    "title": "Forget what the policy says to forget",
                    "milestone": "M02",
                    # The second task orders itself after the first, which is only
                    # expressible if the two have distinct ids.
                    "depends_on": ["first"],
                    "acceptances": ["`go test ./internal/policy` passes"],
                },
            ],
            "dispositions": [
                {"finding_id": finding_id, "resolution": "accepted",
                 "change": "added both"}
            ],
        }))
        assert [f.line() for f in repair.validate(data, patch, request) if f.blocking] == []
        applied = repair.apply_patch(data, patch, request, actor="planner")
        minted = applied["tasks"]
        assert len(minted) == 2, minted
        assert len(set(minted)) == 2, f"both tasks minted {minted}"
        for task_id in minted:
            assert task_id in data["tasks"]
        # And the edge between them survived the translation from proposed ids.
        assert minted[0] in data["tasks"][minted[1]]["depends_on"]


def test_a_patch_may_not_use_one_id_for_two_tasks(approved, project):
    """`translate` is keyed on the proposed id, so the second would erase the first.

    A patch like this used to apply cleanly: both tasks were created, but every
    edge naming the shared id pointed at whichever task registered last, and the
    other was left unreachable — an ordering silently dropped from a patch written
    to add it.
    """
    request_id, finding_id = _request(project)
    with state.transaction(project) as data:
        request = repair.get_request(data, request_id)
        patch = repair.load_patch(json.dumps({
            "base_revision": plans.revision(data),
            "add_tasks": [
                {
                    "id": "same",
                    "title": "One piece of work",
                    "milestone": "M02",
                    "resolves_findings": [finding_id],
                    "acceptances": ["`go test ./internal/store` passes"],
                },
                {
                    "id": "same",
                    "title": "A different piece of work",
                    "milestone": "M02",
                    "acceptances": ["`go test ./internal/policy` passes"],
                },
            ],
            "dispositions": [
                {"finding_id": finding_id, "resolution": "accepted",
                 "change": "added both"}
            ],
        }))
        findings = repair.validate(data, patch, request)
        collisions = [
            f for f in findings
            if f.blocking and f.category == "task-collision"
        ]
        assert collisions, [f.line() for f in findings]
        assert "two tasks under the id same" in collisions[0].message


# --------------------------------------------------------------------------
# reporting


def test_a_held_gate_is_reported_as_waiting_not_as_failed(approved, project):
    with state.transaction(project) as data:
        data["tasks"]["G-M02"]["status"] = "blocked"
        data["tasks"]["G-M02"]["held"] = {
            "reason": "needs-decision", "request": "RR-0001"
        }
    data = state.load(project)
    assert orchestrator.held_gates(data) == {"G-M02": "needs-decision"}
    # The work behind it is waiting on a decision, not stalled by a failure.
    assert "G-M02" not in orchestrator._stalled(data)
    session = orchestrator.Session()
    lines = orchestrator.summary(data, session)
    assert any("G-M02 held: waiting on a decision" in line for line in lines)
    assert not any("blocked by failed work" in line for line in lines)


def test_run_says_what_a_held_gate_is_waiting_for(approved, writ, project):
    with state.transaction(project) as data:
        for task_id in ("M01-001", "M02-001", "M02-002", "M03-001"):
            data["tasks"][task_id]["status"] = "completed"
        for gate_id in ("G-M01", "G-M03"):
            data["tasks"][gate_id]["status"] = "completed"
        data["tasks"]["G-M02"]["status"] = "blocked"
        data["tasks"]["G-M02"]["held"] = {
            "reason": "repair-exhausted", "request": "RR-0001"
        }
    code, out, _ = writ("run", "--agent", agent(IMPLEMENTER))
    assert code == 0
    assert "G-M02 (repair-exhausted)" in out
    assert "writ list gates" in out


# --------------------------------------------------------------------------
# the scheduler
#
# "No ready tasks" used to be enough to conclude a run was finished. With gates
# and repair it is not: the graph can be entirely blocked on a gate that has not
# been reviewed yet, or on a repair that has not been planned.


def test_a_gate_is_scheduled_once_its_tasks_complete(approved, project):
    with state.transaction(project) as data:
        for task_id in ("M02-001", "M02-002"):
            data["tasks"][task_id]["status"] = "completed"
    data = state.load(project)
    # With the other milestones' work taken, the gate over the finished milestone
    # is what is left — and nothing had to teach the scheduler what a gate is.
    job = orchestrator.next_job(
        data, busy=[], budget=None, started=["M01-001", "M03-001"]
    )
    assert job is not None and job.task_id == "G-M02" and job.role == "gate"


def test_implementation_work_goes_before_a_ready_gate(approved, project):
    with state.transaction(project) as data:
        data["tasks"]["M01-001"]["status"] = "completed"
    data = state.load(project)
    # G-M01 is ready now, and so is M02-001. A worker spent on the gate is a
    # worker not spent on work that might be the only thing to do.
    job = orchestrator.next_job(data, busy=[], budget=None, started=[])
    assert job.role == "agent"
    # Not starved: once the tasks are taken, the gate is what is left.
    job = orchestrator.next_job(
        data, busy=[], budget=None, started=["M02-001", "M02-002", "M03-001"]
    )
    assert job.task_id == "G-M01" and job.role == "gate"


def test_repair_planning_outranks_new_implementation(approved, project):
    with state.transaction(project) as data:
        finding = plans.record_findings(
            data,
            [
                __import__("writ.plancheck", fromlist=["Finding"]).Finding(
                    severity="error", category="integration",
                    message="the seam is broken", where="G-M01", source="gate:G-M01",
                )
            ],
            scope="gate:G-M01",
        )[0]
        repair.open_request(
            data, gate_id="G-M01", finding_ids=[finding.id],
            summary="the seam is broken", actor="gate",
        )
    data = state.load(project)
    job = orchestrator.next_job(data, busy=[], budget=None, started=[])
    # A held gate holds everything behind it, and planning the repair is the only
    # thing that can release it.
    assert job.role == "repair" and job.task_id == "G-M01"


def test_a_gate_is_exempt_from_the_task_budget(approved, project):
    with state.transaction(project) as data:
        data["tasks"]["M01-001"]["status"] = "completed"
    data = state.load(project)
    # The budget is spent on M01-001. Refusing to run the gate over it would stop
    # the session exactly where its work is least verified.
    job = orchestrator.next_job(data, busy=[], budget=1, started=["M01-001"])
    assert job is not None and job.task_id == "G-M01"


def test_a_gate_due_again_after_a_repair_is_not_treated_as_already_run(
    approved, project
):
    with state.transaction(project) as data:
        gate = data["tasks"]["G-M01"]
        data["tasks"]["M01-001"]["status"] = "completed"
        gates.record_attempt(
            gate, decision="needs-repair", actor="gate",
            summary="the seam is broken", findings=["F-0001"], revision=1,
        )
    data = state.load(project)
    # The session ledger holds the gate's first run. The code it reviews has since
    # changed, so this is a different job — keyed by repair round, not by task id.
    job = orchestrator.next_job(
        data,
        busy=[],
        budget=None,
        started=["G-M01", "M02-001", "M02-002", "M03-001"],
    )
    assert job is not None and job.task_id == "G-M01" and job.role == "gate"
    assert job.attempt == 1


def test_writ_stops_re_planning_a_repair_it_keeps_refusing(approved, project):
    with state.transaction(project) as data:
        finding = plans.record_findings(
            data,
            [
                __import__("writ.plancheck", fromlist=["Finding"]).Finding(
                    severity="error", category="integration",
                    message="the seam is broken", where="G-M01", source="gate:G-M01",
                )
            ],
            scope="gate:G-M01",
        )[0]
        request = repair.open_request(
            data, gate_id="G-M01", finding_ids=[finding.id],
            summary="the seam is broken", actor="gate",
        )
        request["refusals"] = [{"reasons": []}] * repair.MAX_PATCH_ATTEMPTS
    data = state.load(project)
    assert not repair.patches_left(repair.get_request(data, request["id"]))
    job = orchestrator.next_job(data, busy=[], budget=None, started=[])
    # Whatever it picks, it is not another round of the same refusal.
    assert job is None or job.role != "repair"


def test_a_gate_claiming_a_pass_while_checking_nothing_is_not_believed(
    looping, project, monkeypatch
):
    """The one place a silent report was worth the most and cost the least.

    A gate that reports on no criteria has no unmet ones, so the arithmetic in
    `parse` had nothing to object to and the pass stood — the final check on a
    plan signing it off without confirming a single bar. Writ now reads silence
    as the `pending` the prompt asks for, which turns the claim into a repair
    request naming each criterion nobody checked.
    """
    monkeypatch.setenv("WRIT_TEST_GATE_FAILS", "0")  # every attempt claims a pass
    code, out, _ = looping(
        "run", "--agent", agent(SILENT_GATE), "--reviewer", agent(SILENT_GATE)
    )
    assert code == 0, out
    data = state.load(project)
    ran = [gate for gate in gates.gates(data) if gates.attempts(gate)]
    assert ran, {g["id"]: g["status"] for g in gates.gates(data)}
    for gate in ran:
        decisions = [attempt["decision"] for attempt in gates.attempts(gate)]
        assert "pass" not in decisions, (gate["id"], decisions)
    # No gate passed, so nothing downstream of one did either, and the plan is
    # not complete — which is the outcome that was being handed out for free.
    assert all(gate["status"] != "completed" for gate in gates.gates(data))
    assert plans.plan_status(data)["status"] != "complete"

    # And the reason is on the record, in the words of the criteria it skipped.
    findings = [
        record
        for record in plans.finding_records(data)
        if record.get("category") == "unchecked-gate-criterion"
    ]
    assert findings, plans.finding_records(data)
    assert "nothing confirmed it" in findings[0]["message"]
