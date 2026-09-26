"""Features with contracts (docs/planning-redesign.md §4 and §5).

A features plan has no milestones and no hand-written edges: each feature says
what it owns, provides and consumes, and writ derives the graph from that. These
tests pin the parts that only exist because of it — FT ids, derived edges, the
fence, the final gate's extra criterion, the dispatch prompt, and a plan repair
that edits contracts rather than edges.
"""
from __future__ import annotations

import json

import pytest

from writ import (
    adjudicate,
    config,
    contracts,
    gates,
    plancheck,
    planning,
    plans,
    runner,
    state,
)

from tests.test_adjudicate import ADJUDICATOR, agent, patched

DRAFT = {
    "requirements": [
        {
            "id": "REQ-001",
            "text": "Durable event store",
            "priority": "must",
            "source": "Milestone 1 — Storage",
            "details": ["an append survives a crash", "replay is byte-identical"],
        },
        {
            "id": "REQ-002",
            "text": "Inline projection",
            "priority": "must",
            "source": "Milestone 1 — Storage",
        },
        {
            "id": "REQ-003",
            "text": "Harness",
            "priority": "must",
            "source": "Milestone 0 — Foundations",
        },
        {
            "id": "REQ-004",
            "text": "Operator interface",
            "priority": "should",
            "source": "Milestone 2 — Interface",
        },
    ],
    "features": [
        {
            "id": "harness",
            "title": "Harness",
            "goal": "the project builds and its tests run",
            "design_section": "Milestone 0 — Foundations",
            "requirement_ids": ["REQ-003"],
            "owns": ["cmd/"],
            "provides": ["Config: typed settings"],
            "acceptance": [
                "the project builds from a clean checkout",
                "the test suite runs",
                "malformed input fails deterministically",
            ],
        },
        {
            "id": "store",
            "title": "Event store",
            "goal": "a durable, replayable log",
            "design_section": "Milestone 1 — Storage",
            "requirement_ids": ["REQ-001"],
            "owns": ["internal/store/"],
            "provides": ["EventLog: append(event) -> offset; read(from) -> events"],
            "consumes": ["Config: typed settings"],
            "acceptance": [
                "an append survives a crash",
                "replay returns events in append order",
                "a torn write is detected on open",
            ],
        },
        {
            "id": "projection",
            "title": "Projection",
            "goal": "an inline projection kept in step with the log",
            "design_section": "Milestone 1 — Storage",
            "requirement_ids": ["REQ-002"],
            "owns": ["internal/projection/"],
            "provides": ["View: current state by key"],
            "consumes": ["EventLog: append/read"],
            "acceptance": [
                "the projection matches a replay",
                "a missing key reads as absent",
                "rebuilding from scratch gives the same view",
            ],
        },
        {
            "id": "ui",
            "title": "Operator interface",
            "goal": "an operator can read the view",
            "design_section": "Milestone 2 — Interface",
            "requirement_ids": ["REQ-004"],
            "owns": ["internal/ui/"],
            "consumes": ["View", "Config"],
            "acceptance": [
                "the view is shown for a key",
                "an unknown key says so",
                "the page loads with an empty log",
            ],
        },
    ],
}


@pytest.fixture
def committed(writ, project, design, tmp_path):
    artifact = tmp_path / "draft.json"
    artifact.write_text(json.dumps(DRAFT), encoding="utf-8")
    writ("init")
    code, out, err = writ("plan", str(design), "--from-plan", str(artifact))
    assert code == 0, err
    assert "created 4 features" in out
    return state.load(project)


def _features(data):
    return {
        task_id: task
        for task_id, task in data["tasks"].items()
        if contracts.is_feature(task)
    }


# --------------------------------------------------------------------------
# contracts


def test_interface_names_are_the_text_before_the_colon():
    assert contracts.name("EventLog: append/read") == "eventlog"
    assert contracts.name("  Event   Log ") == "event log"
    assert contracts.names(["A: x", "a: y", "B"]) == ["a", "b"]


def test_edges_come_from_what_each_feature_consumes():
    features = {
        "a": {"provides": ["X: x"]},
        "b": {"provides": ["Y"], "consumes": ["X"]},
        "c": {"consumes": ["X", "Y", "Nobody"]},
    }
    assert contracts.edges(features) == {"a": [], "b": ["a"], "c": ["a", "b"]}


def test_a_doubly_provided_interface_yields_no_edge():
    features = {"a": {"provides": ["X"]}, "b": {"provides": ["X"]}, "c": {"consumes": ["X"]}}
    assert contracts.edges(features)["c"] == []


def test_the_fence_is_what_a_feature_owns_plus_the_test_directories():
    assert contracts.fence(["pkg/a/"], ["tests/", "pkg/a/"]) == ["pkg/a/", "tests/"]


# --------------------------------------------------------------------------
# loading and committing


def test_a_features_draft_loads_as_one_loose_group():
    document = planning.load_document(json.dumps(DRAFT))
    assert document.features
    (group,) = document.milestones
    assert group.loose and len(group.tasks) == 4
    store = group.tasks[1]
    assert store.feature and store.owns == ["internal/store/"]
    # derived from the contracts, not written by the synthesizer
    assert store.depends_on == ["harness"]


def test_a_features_plan_commits_with_ft_ids_and_no_milestones(committed):
    features = _features(committed)
    assert sorted(features) == ["FT-001", "FT-002", "FT-003", "FT-004"]
    assert committed["milestones"] == {}
    assert all(task["milestone"] is None for task in features.values())


def test_committed_edges_are_derived_from_contracts(committed):
    tasks = committed["tasks"]
    assert tasks["FT-001"]["depends_on"] == []
    assert tasks["FT-002"]["depends_on"] == ["FT-001"]
    assert tasks["FT-003"]["depends_on"] == ["FT-002"]
    assert sorted(tasks["FT-004"]["depends_on"]) == ["FT-001", "FT-003"]


def test_a_feature_keeps_its_contracts_on_the_task(committed):
    store = committed["tasks"]["FT-002"]
    assert store["goal"] == "a durable, replayable log"
    assert store["owns"] == ["internal/store/"]
    assert store["allowed"] == ["internal/store/"]
    assert store["provides"][0].startswith("EventLog")


def test_the_final_gate_waits_for_every_feature(committed):
    final = committed["tasks"][gates.FINAL_GATE_ID]
    assert sorted(final["depends_on"]) == ["FT-001", "FT-002", "FT-003", "FT-004"]


def test_the_final_gate_checks_details_and_contracts(committed):
    texts = [item["text"] for item in committed["tasks"][gates.FINAL_GATE_ID]["acceptances"]]
    assert any("`details`" in text for text in texts)
    assert any("interface a feature provides" in text for text in texts)


def test_a_features_plan_is_not_blocked_by_its_own_shape(committed):
    blocking = [f for f in plans.findings(committed, open_only=True) if f.severity == "error"]
    assert blocking == []


def test_the_dry_run_shows_contracts(writ, design, tmp_path):
    artifact = tmp_path / "draft.json"
    artifact.write_text(json.dumps(DRAFT), encoding="utf-8")
    writ("init")
    code, out, _ = writ("plan", str(design), "--from-plan", str(artifact), "--dry-run")
    assert code == 0
    assert "would create 4 features" in out
    assert "owns: internal/store/" in out
    assert "provides: EventLog" in out


def test_an_unprovided_interface_is_a_contract_gap():
    draft = json.loads(json.dumps(DRAFT))
    draft["features"][3]["consumes"].append("Metrics: counters")
    document = planning.load_document(json.dumps(draft))
    found = plancheck.check(plancheck.from_plan(document.milestones, document.requirements))
    gaps = [f for f in found if f.category == "contract-gap"]
    assert gaps and gaps[0].severity == "error" and "metrics" in gaps[0].message.lower()


def test_contracts_that_form_a_cycle_are_reported():
    draft = json.loads(json.dumps(DRAFT))
    draft["features"][0]["consumes"] = ["View"]
    document = planning.load_document(json.dumps(draft))
    found = plancheck.check(plancheck.from_plan(document.milestones, document.requirements))
    assert any(f.category == "cycle" and f.severity == "error" for f in found)


# --------------------------------------------------------------------------
# dispatch


def test_the_dispatch_prompt_hands_over_goal_contracts_and_upstream(committed, project):
    data = committed
    data["tasks"]["FT-001"]["status"] = "done"
    data["tasks"]["FT-001"]["last_verdict"] = {"summary": "settings load from writ.toml"}
    prompt = runner.build_prompt(data, data["tasks"]["FT-002"], project)
    assert "Goal: a durable, replayable log" in prompt
    assert "an append survives a crash" in prompt  # a requirement detail
    assert "You consume:" in prompt and "Config: typed settings" in prompt
    assert "FT-001 [done] Harness" in prompt
    assert "settings load from writ.toml" in prompt
    assert "Plan your own steps" in prompt
    assert "Fence" in prompt


# --------------------------------------------------------------------------
# plan repair over features


@pytest.fixture
def contract_gap(committed, writ, project):
    with state.transaction(project) as data:
        plans.record_findings(
            data,
            [
                plancheck.Finding(
                    severity="error",
                    category="contract-gap",
                    message="nothing provides metrics the operator view needs",
                    where="FT-004",
                    source="critic:fidelity",
                )
            ],
            scope="critic:fidelity",
        )
    return writ


def _adjudicate(writ):
    return writ("adjudicate", "--agent", agent(ADJUDICATOR), "--no-critics")


def test_a_new_feature_is_minted_an_ft_id_and_wired_by_its_contracts(
    contract_gap, project, monkeypatch
):
    patched(
        monkeypatch,
        {
            "add": {
                "metrics": {
                    "title": "Metrics",
                    "goal": "counters the operator view reads",
                    "requirement_ids": ["REQ-004"],
                    "owns": ["internal/metrics/"],
                    "provides": ["Metrics: counters"],
                    "consumes": ["EventLog"],
                    "acceptances": ["a", "b", "c"],
                }
            },
            "revise": {"FT-004": {"consumes": ["View", "Config", "Metrics"]}},
        },
    )
    code, _, err = _adjudicate(contract_gap)
    assert code == 0, err
    tasks = state.load(project)["tasks"]
    assert tasks["FT-005"]["milestone"] is None
    assert tasks["FT-005"]["depends_on"] == ["FT-002"]
    assert "FT-005" in tasks["FT-004"]["depends_on"]
    assert "FT-005" in tasks[gates.FINAL_GATE_ID]["depends_on"]


def test_a_removed_contract_takes_its_edge_with_it(contract_gap, project, monkeypatch):
    patched(monkeypatch, {"revise": {"FT-004": {"consumes": ["View"]}}})
    code, _, err = _adjudicate(contract_gap)
    assert code == 0, err
    assert state.load(project)["tasks"]["FT-004"]["depends_on"] == ["FT-003"]


def test_the_fence_follows_what_a_feature_owns(contract_gap, project, monkeypatch):
    patched(monkeypatch, {"revise": {"FT-004": {"owns": ["internal/web/"]}}})
    code, _, err = _adjudicate(contract_gap)
    assert code == 0, err
    assert state.load(project)["tasks"]["FT-004"]["allowed"] == ["internal/web/"]


def test_contracts_that_would_cycle_are_refused(contract_gap, project, monkeypatch):
    patched(monkeypatch, {"revise": {"FT-001": {"consumes": ["View"]}}})
    _adjudicate(contract_gap)
    data = state.load(project)
    assert data["tasks"]["FT-001"]["consumes"] == []
    reasons = [
        reason["category"]
        for request in data.get("repairs", [])
        for refusal in request.get("refusals") or []
        for reason in refusal["reasons"]
    ]
    assert "dependency-cycle" in reasons


def test_a_needs_decision_finding_goes_to_a_human_not_the_adjudicator(
    committed, writ, project, monkeypatch
):
    with state.transaction(project) as data:
        plans.record_findings(
            data,
            [
                plancheck.Finding(
                    severity="error",
                    category="needs-decision",
                    message="the design allows two retention policies",
                    where="REQ-001",
                    source="critic:fidelity",
                )
            ],
            scope="critic:fidelity",
        )
    patched(monkeypatch, {"revise": {"FT-004": {"title": "never runs"}}})
    code, out, _ = _adjudicate(writ)
    data = state.load(project)
    assert data["tasks"]["FT-004"]["title"] == "Operator interface"
    raised = [item for item in data["decisions"] if item.get("finding")]
    assert len(raised) == 1 and "retention" in raised[0]["title"]
    # routed once, however often the loop is asked
    _adjudicate(writ)
    assert len([i for i in state.load(project)["decisions"] if i.get("finding")]) == 1


@pytest.fixture
def asked(committed, writ, project, monkeypatch):
    """A plan held on a `needs-decision` finding the loop has put to a person."""
    with state.transaction(project) as data:
        plans.record_findings(
            data,
            [
                plancheck.Finding(
                    severity="error",
                    category="needs-decision",
                    message="the design allows two retention policies",
                    where="FT-002",
                    source="critic:fidelity",
                )
            ],
            scope="critic:fidelity",
        )
    patched(monkeypatch, {"revise": {"FT-002": {"notes": "retention: keep 30 days"}}})
    code, out, _ = _adjudicate(writ)
    assert code == 1
    assert "need your ruling" in out and "D-0001 for F-" in out
    assert "next: writ set D-NNNN active --decision" in out
    return writ


def _to_fix(project):
    folder = state.load(project)["plan"]["id"]
    rounds = sorted((project / ".writ" / "plans").glob(f"{folder}*/rounds/*/round-*"))
    return json.loads((rounds[-1] / "to-fix.json").read_text())


def test_a_held_plan_names_the_decision_to_answer(asked, project):
    message = plans.not_runnable_message(state.load(project))
    assert "D-0001 (for F-" in message and "--decision" in message


def test_a_question_cannot_be_confirmed_without_its_answer(asked):
    code, _, err = asked("set", "D-0001", "active")
    assert code != 0 and "--decision" in err


def test_a_ruling_is_repaired_into_the_plan(asked, project):
    code, out, err = asked(
        "set", "D-0001", "active", "--decision", "Keep 30 days of events."
    )
    assert code == 0, err
    record = state.load(project)["decisions"][0]
    assert record["status"] == "active"
    assert record["decision"] == "Keep 30 days of events."
    assert "next: writ build" in out
    code, _, err = _adjudicate(asked)
    assert code == 0, err
    assert state.load(project)["tasks"]["FT-002"]["notes"] == "retention: keep 30 days"
    handed = _to_fix(project)
    assert handed[0]["category"] == "needs-decision"
    assert "Keep 30 days of events." in handed[0]["suggested_action"]


def test_building_again_applies_the_ruling_and_approves(asked, project):
    asked("set", "D-0001", "active", "--decision", "Keep 30 days of events.")
    code, out, err = asked(
        "build", "--critic", agent(ADJUDICATOR), "--repair", "--max-tasks", "0"
    )
    assert "repairing the plan to follow the ruling on F-" in out, err
    data = state.load(project)
    assert data["tasks"]["FT-002"]["notes"] == "retention: keep 30 days"
    assert plans.runnable(data)


def _retention_question(project):
    with state.transaction(project) as data:
        plans.record_findings(
            data,
            [
                plancheck.Finding(
                    severity="error",
                    category="needs-decision",
                    message="the design allows two retention policies",
                    where="FT-002",
                    suggested_action="Keep 30 days of events.",
                    source="critic:fidelity",
                )
            ],
            scope="critic:fidelity",
        )


def test_autonomous_mode_has_the_adjudicator_decide_and_logs_it(
    committed, writ, project, monkeypatch
):
    _retention_question(project)
    patched(
        monkeypatch,
        {
            "revise": {"FT-002": {"notes": "retention: keep 30 days"}},
            "_decision": "Keep 30 days of events.",
        },
    )
    code, out, err = writ(
        "adjudicate", "--agent", agent(ADJUDICATOR), "--no-critics", "--autonomous"
    )
    assert code == 0, out + err
    data = state.load(project)
    assert data["tasks"]["FT-002"]["notes"] == "retention: keep 30 days"
    [record] = [item for item in data["decisions"] if item.get("finding")]
    assert record["status"] == "active"
    assert record["decision"] == "Keep 30 days of events."
    assert record["confirmed_by"] == "autonomous"
    handed = _to_fix(project)
    assert "decide it yourself" in handed[0]["suggested_action"]
    assert "Keep 30 days" in handed[0]["suggested_action"]
    # the log says who ruled, and can be narrowed to what writ decided alone
    code, out, _ = writ("list", "decisions", "--autonomous")
    assert code == 0 and record["id"] in out and "autonomous" in out


def test_autonomous_mode_can_be_set_once_in_the_config(
    committed, writ, project, monkeypatch
):
    _retention_question(project)
    config.config_file(project).write_text("decisions:\n  autonomous: true\n")
    patched(
        monkeypatch,
        {
            "revise": {"FT-002": {"notes": "retention: keep 30 days"}},
            "_decision": "Keep 30 days of events.",
        },
    )
    code, out, err = _adjudicate(writ)
    assert code == 0, out + err
    [record] = [i for i in state.load(project)["decisions"] if i.get("finding")]
    assert record["confirmed_by"] == "autonomous"
    # and a flag still wins over it
    _retention_question(project)
    code, _, _ = writ(
        "adjudicate", "--agent", agent(ADJUDICATOR), "--no-critics", "--no-autonomous"
    )
    assert code == 1


def test_building_autonomously_repairs_and_approves(
    committed, writ, project, monkeypatch
):
    _retention_question(project)
    patched(
        monkeypatch,
        {
            "revise": {"FT-002": {"notes": "retention: keep 30 days"}},
            "_decision": "Keep 30 days of events.",
        },
    )
    code, out, err = writ(
        "build", "--critic", agent(ADJUDICATOR), "--autonomous", "--max-tasks", "0"
    )
    assert "(autonomous)" in out, out + err
    data = state.load(project)
    assert data["tasks"]["FT-002"]["notes"] == "retention: keep 30 days"
    assert plans.runnable(data)
    assert data["autonomous"] is True


def test_the_adjudicator_is_told_edges_come_from_contracts(tmp_path):
    prompt = adjudicate.build_prompt(
        root=tmp_path, doc=None, directory=tmp_path / "round-1", blocking=1, features=True
    )
    assert "writ derives it from the contracts" in " ".join(prompt.split())
    assert '"owns"' in prompt


# --------------------------------------------------------------------------
# what execution prompts carry: decisions and the verify command


def _decision(number, task_id, title, text, status="accepted"):
    return {
        "id": f"D-{number:03d}",
        "tasks": [task_id],
        "title": title,
        "decision": text,
        "status": status,
    }


def test_the_final_gate_sees_decisions_through_milestone_gates(committed, project):
    data = committed
    data["tasks"]["G-M1"] = {
        **data["tasks"]["G-FINAL"],
        "id": "G-M1",
        "depends_on": ["FT-001", "FT-002"],
    }
    data["tasks"]["G-FINAL"]["depends_on"] = ["G-M1", "FT-003", "FT-004"]
    data["decisions"] = [
        _decision(1, "FT-001", "Settings format", "TOML, one file per project."),
        _decision(2, "FT-002", "Log encoding", "x" * 1000),
        _decision(3, "FT-002", "Abandoned idea", "use sqlite", status="rejected"),
    ]
    prompt = runner.build_gate_prompt(data, data["tasks"]["G-FINAL"], project)
    assert "Settings format" in prompt  # reached only through G-M1
    assert "Log encoding" in prompt and "x" * 300 not in prompt  # abridged
    assert "Abandoned idea" not in prompt
    assert "abridged; the full text" in prompt


def test_the_implementer_sees_what_upstream_work_decided(committed, project):
    data = committed
    data["decisions"] = [
        _decision(1, "FT-001", "Settings format", "TOML, one file per project."),
        _decision(2, "FT-003", "Not upstream", "irrelevant to FT-002"),
    ]
    prompt = runner.build_prompt(data, data["tasks"]["FT-002"], project)
    assert "Decisions the work you build on already made" in prompt
    assert "Settings format: TOML, one file per project." in prompt
    assert "Not upstream" not in prompt


def test_every_execution_prompt_names_the_verify_command(committed, project):
    data = committed
    data.setdefault("plan", {}).setdefault("pipeline", {})["baseline"] = {
        "commands": ["make test"]
    }
    task, gate = data["tasks"]["FT-002"], data["tasks"]["G-FINAL"]
    for prompt in (
        runner.build_prompt(data, task, project),
        runner.build_review_prompt(data, task, project),
        runner.build_gate_prompt(data, gate, project),
    ):
        assert "  make test" in prompt
    overridden = runner.build_prompt(data, task, project, verify="uv run pytest")
    assert "  uv run pytest" in overridden and "make test" not in overridden


def test_a_greenfield_plan_names_no_verify_command(committed, project):
    prompt = runner.build_prompt(committed, committed["tasks"]["FT-002"], project)
    assert "Verify with (" not in prompt


def test_the_configured_verify_command_wins_over_planning(committed, project):
    data = committed
    data.setdefault("plan", {}).setdefault("pipeline", {})["baseline"] = {
        "commands": ["make test"]
    }
    config.config_file(project).write_text(
        "run:\n  verify: just check\n", encoding="utf-8"
    )
    assert runner.verify_commands(data, project) == ["just check"]
    assert runner.verify_commands(data, project, "tox") == ["tox"]
