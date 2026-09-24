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

from writ import adjudicate, contracts, gates, plancheck, plans, planning, runner, state

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


def test_the_adjudicator_is_told_edges_come_from_contracts(tmp_path):
    prompt = adjudicate.build_prompt(
        root=tmp_path, doc=None, directory=tmp_path / "round-1", blocking=1, features=True
    )
    assert "writ derives it from the contracts" in " ".join(prompt.split())
    assert '"owns"' in prompt
