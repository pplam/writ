"""The staged planning pipeline: three analyses, then a synthesis held to them."""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from writ import analysis, plancheck, planning, plans, state
from writ.state import WritError

REQUIREMENTS = {
    "requirements": [
        {
            "id": "REQ-001",
            "text": "Appends to the event log are atomic.",
            "source": "Milestone 1 — Storage / Event log",
            "priority": "must",
            "status": "planned",
            "verification": ["a crash mid-append leaves no partial record"],
        },
        {
            "id": "REQ-002",
            "text": "Replay of the log is byte-identical.",
            "source": "Milestone 1 — Storage / Event log",
            "priority": "must",
            "status": "planned",
        },
        {
            "id": "REQ-003",
            "text": "The CLI reports progress.",
            "source": "Milestone 2 — Interface",
            "priority": "should",
            "status": "planned",
        },
    ],
    "ambiguities": [
        {
            "id": "AMB-001",
            "question": "Is replay ordered by write time or by sequence number?",
            "requirement_ids": ["REQ-002"],
            "readings": ["write time", "sequence number"],
            "assumed": "sequence number",
        }
    ],
}

INVENTORY = {
    "components": [
        {
            "name": "event store",
            "paths": ["internal/store/"],
            "existing_behavior": "empty package, nothing implemented",
            "test_locations": [],
            "extension_points": ["internal/store/log.go"],
            "risks": ["no tests exist yet"],
        }
    ],
    "existing_coverage": [
        {"requirement_id": "REQ-003", "status": "none", "evidence": ""}
    ],
    "conventions": ["table-driven tests", "errors wrapped with context"],
    "baseline_commands": ["pytest -q"],
    "baseline_result": {"status": "pass", "summary": "41 passed", "known_failures": []},
}

VERIFICATION = {
    "verification": [
        {
            "requirement_id": "REQ-001",
            "methods": [
                {
                    "kind": "test",
                    "location": "tests/test_store.py",
                    "command": "pytest -q tests/test_store.py",
                    "observable": "a killed writer leaves no partial record",
                    "exists": False,
                    "needs": "the test file itself",
                }
            ],
            "confidence": "high",
        },
        {
            "requirement_id": "REQ-002",
            "methods": [
                {
                    "kind": "test",
                    "location": "tests/test_replay.py",
                    "command": "pytest -q tests/test_replay.py",
                    "observable": "replay output hashes equal the log",
                    "exists": False,
                }
            ],
            "confidence": "high",
        },
        {
            "requirement_id": "REQ-003",
            "methods": [
                {
                    "kind": "command",
                    "command": "writ status",
                    "observable": "prints counts per milestone",
                    "exists": True,
                }
            ],
            "confidence": "medium",
        },
    ],
    "missing_infrastructure": [
        {
            "need": "there is no store test module",
            "blocks": ["REQ-001", "REQ-002"],
            "suggestion": "add tests/test_store.py first",
        }
    ],
    "undemonstrable": [],
}

PLAN = {
    "requirements": REQUIREMENTS["requirements"],
    "milestones": [
        {
            "id": "M01",
            "title": "Storage foundations",
            "tasks": [
                {
                    "id": "T-log",
                    "title": "Add append-only event log writer",
                    "design_section": "Milestone 1 — Storage / Event log",
                    "requirement_ids": ["REQ-001"],
                    "acceptances": [
                        "pytest -q tests/test_store.py passes",
                        "a killed writer leaves no partial record",
                    ],
                    "allowed": ["internal/store/"],
                },
                {
                    "id": "T-replay",
                    "title": "Replay the log byte-identically",
                    "design_section": "Milestone 1 — Storage / Event log",
                    "requirement_ids": ["REQ-002"],
                    "acceptances": [
                        "pytest -q tests/test_replay.py passes",
                        "replay output hashes equal the log",
                    ],
                    "depends_on": ["T-log"],
                    "allowed": ["internal/replay/"],
                },
            ],
        },
        {
            "id": "M02",
            "title": "Interface",
            "tasks": [
                {
                    "id": "T-cli",
                    "title": "Report progress from the CLI",
                    "design_section": "Milestone 2 — Interface",
                    "requirement_ids": ["REQ-003"],
                    "acceptances": ["`writ status` prints counts per milestone"],
                    "depends_on": ["T-replay"],
                    "allowed": ["cmd/"],
                }
            ],
        },
    ],
}

def staged_agent(
    *,
    requirements=REQUIREMENTS,
    inventory=INVENTORY,
    verification=VERIFICATION,
    plan=PLAN,
) -> str:
    """An agent that writes whatever artifact the prompt asked it for.

    The pipeline tells each stage its exact output path, so one script serves all
    four calls: it reads the path out of the prompt and picks the payload by
    filename. That is also a check on the prompts — a stage that failed to state
    its path would get nothing written and the run would fail.
    """
    payloads = {
        "requirements.json": json.dumps(requirements),
        "inventory.json": json.dumps(inventory),
        "verification.json": json.dumps(verification),
        "plan.json": json.dumps(plan),
    }
    return f"""\
import re, sys
payloads = {payloads!r}
prompt = sys.stdin.read()
match = re.search(r'^  (\\S+\\.json)$', prompt, re.M)
if match is None:
    sys.stderr.write('no artifact path in the prompt' + chr(10))
    sys.exit(3)
path = match.group(1)
name = path.rsplit('/', 1)[-1]
open(path, 'w').write(payloads[name])
sys.stdout.write('wrote ' + name + chr(10))
"""


def agent(script: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


def staged(*extra: str, **payloads) -> tuple[str, ...]:
    return ("--agent", agent(staged_agent(**payloads)), "--quiet", *extra)


def _work(data) -> list[str]:
    return sorted(
        task_id
        for task_id, task in data["tasks"].items()
        if task.get("kind", "task") != "gate"
    )


# --------------------------------------------------------------------------
# the stages themselves


def test_the_pipeline_has_no_candidate_plan_stage():
    """Stage 4 of the review — competing candidate plans — is deliberately absent.

    With the requirement inventory fixed first, the useful disagreement about a
    plan is about coverage of a known list, which the critics produce by reading
    the one plan. Two plans with no shared vocabulary would need a third agent to
    choose between them, and that agent is the unreviewed author again.
    """
    assert analysis.STAGE_NAMES == ("requirements", "inventory", "verification")
    assert not any("candidate" in stage.name for stage in analysis.STAGES)


def test_each_stage_states_what_it_is_not_for():
    for stage in analysis.STAGES:
        assert stage.out_of_scope, stage.name
        assert stage.schema.strip().startswith("{")
        assert stage.rules.startswith("Rules:")
    # No two stages answer the same question.
    assert len({stage.brief for stage in analysis.STAGES}) == len(analysis.STAGES)


def test_upto_takes_every_stage_a_named_one_depends_on():
    assert [s.name for s in analysis.upto("requirements")] == ["requirements"]
    assert [s.name for s in analysis.upto("verification")] == list(
        analysis.STAGE_NAMES
    )
    with pytest.raises(WritError, match="requirements"):
        analysis.upto("vibes")


# --------------------------------------------------------------------------
# artifact validation


def test_a_requirements_artifact_with_no_obligations_is_rejected():
    with pytest.raises(WritError, match="no obligations"):
        analysis.load_requirements(json.dumps({"requirements": []}))


def test_an_ambiguity_cannot_cite_a_requirement_that_does_not_exist():
    payload = {
        "requirements": REQUIREMENTS["requirements"],
        "ambiguities": [{"id": "AMB-001", "question": "?", "requirement_ids": ["REQ-404"]}],
    }
    with pytest.raises(WritError, match="REQ-404"):
        analysis.load_requirements(json.dumps(payload))


def test_the_inventory_may_not_invent_a_requirement_id():
    """The point of running after the requirements stage is attaching to real ids.

    A coverage claim about an id nobody recorded is the analyst filling in a gap
    it imagined — and accepting it would let a hallucinated obligation be marked
    already-satisfied, which deletes real work from the plan.
    """
    payload = json.loads(json.dumps(INVENTORY))
    payload["existing_coverage"].append(
        {"requirement_id": "REQ-999", "status": "full", "evidence": "tests/test_x.py"}
    )
    with pytest.raises(WritError, match="REQ-999"):
        analysis.load_inventory(json.dumps(payload), known=["REQ-001", "REQ-003"])


def test_the_baseline_status_must_be_one_of_three_things():
    payload = json.loads(json.dumps(INVENTORY))
    payload["baseline_result"]["status"] = "probably fine"
    with pytest.raises(WritError, match="pass, fail or unknown"):
        analysis.load_inventory(json.dumps(payload), known=["REQ-003"])


def test_an_absent_baseline_reads_as_unknown_not_as_passing():
    artifact = analysis.load_inventory(
        json.dumps({"components": [], "baseline_result": {}})
    )
    assert artifact.baseline_status == "unknown"


def test_a_verification_method_that_states_nothing_checkable_is_rejected():
    payload = {
        "verification": [
            {"requirement_id": "REQ-001", "methods": [{"kind": "test", "exists": True}]}
        ]
    }
    with pytest.raises(WritError, match="verifies nothing"):
        analysis.load_verification(json.dumps(payload), known=["REQ-001"])


def test_one_verification_entry_per_requirement():
    payload = {
        "verification": [
            {"requirement_id": "REQ-001", "methods": [{"command": "a"}]},
            {"requirement_id": "REQ-001", "methods": [{"command": "b"}]},
        ]
    }
    with pytest.raises(WritError, match="second entry"):
        analysis.load_verification(json.dumps(payload), known=["REQ-001"])


def test_an_artifact_can_arrive_as_fenced_json_among_prose():
    text = "Here is what I found:\n```json\n" + json.dumps(REQUIREMENTS) + "\n```\n"
    artifact = analysis.load_requirements(text)
    assert artifact.ids == ["REQ-001", "REQ-002", "REQ-003"]


# --------------------------------------------------------------------------
# reconcile: holding the synthesizer to the analyses


def _artifacts() -> analysis.Artifacts:
    return analysis.Artifacts(
        requirements=analysis.load_requirements(json.dumps(REQUIREMENTS)),
        inventory=analysis.load_inventory(
            json.dumps(INVENTORY), known=["REQ-001", "REQ-002", "REQ-003"]
        ),
        verification=analysis.load_verification(
            json.dumps(VERIFICATION), known=["REQ-001", "REQ-002", "REQ-003"]
        ),
    )


def test_a_faithful_plan_reconciles_clean():
    document = planning.load_document(json.dumps(PLAN))
    assert analysis.reconcile(document, _artifacts()) == []


def test_a_dropped_must_requirement_is_a_blocking_finding():
    """The defect staging exists to catch, and a single-shot planner cannot.

    One agent writing the inventory and the tasks together never records an
    obligation it was not going to cover, so the omission leaves no trace. Fixing
    the inventory first is what makes it findable.
    """
    plan = json.loads(json.dumps(PLAN))
    plan["requirements"] = [
        req for req in plan["requirements"] if req["id"] != "REQ-002"
    ]
    plan["milestones"][0]["tasks"] = [plan["milestones"][0]["tasks"][0]]
    plan["milestones"][1]["tasks"][0]["depends_on"] = ["T-log"]
    document = planning.load_document(json.dumps(plan))
    findings = analysis.reconcile(document, _artifacts())
    dropped = [f for f in findings if f.category == "dropped-requirement"]
    assert len(dropped) == 1
    assert dropped[0].severity == "error"
    assert dropped[0].requirement_ids == ["REQ-002"]


def test_a_dropped_should_requirement_is_advisory_not_blocking():
    plan = json.loads(json.dumps(PLAN))
    plan["requirements"] = [
        req for req in plan["requirements"] if req["id"] != "REQ-003"
    ]
    plan["milestones"] = plan["milestones"][:1]
    document = planning.load_document(json.dumps(plan))
    dropped = [
        f
        for f in analysis.reconcile(document, _artifacts())
        if f.category == "dropped-requirement"
    ]
    assert [f.severity for f in dropped] == ["warning"]


def test_an_invented_requirement_is_a_blocking_finding():
    plan = json.loads(json.dumps(PLAN))
    plan["requirements"].append(
        {"id": "REQ-900", "text": "Everything is fast.", "priority": "must"}
    )
    document = planning.load_document(json.dumps(plan))
    invented = [
        f
        for f in analysis.reconcile(document, _artifacts())
        if f.category == "invented-requirement"
    ]
    assert len(invented) == 1 and invented[0].severity == "error"
    assert "REQ-900" in invented[0].message


def test_marking_a_requirement_out_of_scope_is_not_dropping_it():
    """A stated disposition is the whole point; only silence is a defect."""
    plan = json.loads(json.dumps(PLAN))
    for req in plan["requirements"]:
        if req["id"] == "REQ-003":
            req["status"] = "out-of-scope"
            req["reason"] = "the CLI is a later milestone"
    plan["milestones"] = plan["milestones"][:1]
    document = planning.load_document(json.dumps(plan))
    assert [
        f
        for f in analysis.reconcile(document, _artifacts())
        if f.category == "dropped-requirement"
    ] == []


def test_ignoring_the_verification_that_was_worked_out_is_flagged():
    plan = json.loads(json.dumps(PLAN))
    plan["milestones"][0]["tasks"][0]["acceptances"] = [
        "the writer behaves correctly under load",
        "the code is reviewed",
    ]
    document = planning.load_document(json.dumps(plan))
    unused = [
        f
        for f in analysis.reconcile(document, _artifacts())
        if f.category == "unused-verification"
    ]
    assert len(unused) == 1
    assert unused[0].severity == "warning"
    assert "REQ-001" in unused[0].message


def test_replanning_work_the_repository_already_does_is_flagged():
    artifacts = _artifacts()
    artifacts.inventory.existing_coverage = [
        {
            "requirement_id": "REQ-003",
            "status": "full",
            "evidence": "tests/test_views.py::test_status",
        }
    ]
    document = planning.load_document(json.dumps(PLAN))
    replanned = [
        f
        for f in analysis.reconcile(document, artifacts)
        if f.category == "replanned-requirement"
    ]
    assert len(replanned) == 1 and replanned[0].requirement_ids == ["REQ-003"]


def test_reconcile_says_nothing_without_a_requirements_artifact():
    document = planning.load_document(json.dumps(PLAN))
    assert analysis.reconcile(document, analysis.Artifacts()) == []


# --------------------------------------------------------------------------
# end to end through the CLI


def test_plan_runs_three_analyses_then_synthesises(writ, project, design):
    writ("init")
    code, out, _ = writ("plan", str(design), *staged())
    assert code == 0
    assert "analysing" in out and "3 stage(s)" in out
    for stage in analysis.STAGE_NAMES:
        assert f"{stage}: wrote" in out
    assert "(staged)" in out
    assert _work(state.load(project)) == ["M01-001", "M01-002", "M02-001"]


def test_every_stage_leaves_its_artifact_on_disk(writ, project, design):
    writ("init")
    writ("plan", str(design), *staged())
    record = plans.plan_status(state.load(project))["pipeline"]
    directory = Path(record["directory"])
    for stage in analysis.STAGES:
        assert (directory / stage.artifact).exists(), stage.name
    assert (directory / "plan.json").exists()
    # And each stage's own transcript, so a bad artifact can be traced to a run.
    for stage in analysis.STAGES:
        assert (directory / stage.name / "prompt.txt").exists()


def test_the_pipeline_is_recorded_on_the_plan(writ, project, design):
    writ("init")
    writ("plan", str(design), *staged())
    record = plans.plan_status(state.load(project))["pipeline"]
    assert record["requirement_ids"] == ["REQ-001", "REQ-002", "REQ-003"]
    assert record["baseline"]["status"] == "pass"
    assert record["baseline"]["commands"] == ["pytest -q"]
    assert set(record["stages"]) == set(analysis.STAGE_NAMES)


def test_the_requirements_reach_state_and_the_coverage_matrix(writ, project, design):
    writ("init")
    writ("plan", str(design), *staged())
    data = state.load(project)
    assert sorted(plans.requirements(data)) == ["REQ-001", "REQ-002", "REQ-003"]
    covered = {row["id"]: row for row in plans.coverage(data)}
    assert covered["REQ-001"]["tasks"] == ["M01-001"]
    assert plans.uncovered(data) == []


def test_the_synthesis_prompt_carries_all_three_analyses(writ, project, design):
    writ("init")
    writ("plan", str(design), *staged())
    directory = Path(plans.plan_status(state.load(project))["pipeline"]["directory"])
    prompt = (directory / "prompt.txt").read_text(encoding="utf-8")
    # the fixed inventory, verbatim and id-first
    assert "REQUIREMENTS — the fixed inventory" in prompt
    assert "REQ-002" in prompt
    # what the repository already is, including the baseline
    assert "REPOSITORY — what is already here" in prompt
    assert "pytest -q" in prompt
    # how each requirement can be shown, including what does not exist yet
    assert "VERIFICATION" in prompt
    assert "DOES NOT EXIST YET" in prompt
    assert "MISSING INFRASTRUCTURE" in prompt
    # and the rules that make those binding
    assert "may not drop an entry" in prompt


def test_a_stage_prompt_does_not_ask_for_a_plan(writ, project, design):
    """Each analysis is fenced off from the decomposition, in its own prompt."""
    writ("init")
    writ("plan", str(design), "--stage", "requirements", *staged())
    directory = next(state.plans_dir(project).iterdir())
    prompt = (directory / "requirements" / "prompt.txt").read_text(encoding="utf-8")
    assert "Do not propose milestones, tasks, ordering" in prompt
    assert '"acceptances"' not in prompt


def test_stage_runs_the_analyses_and_commits_nothing(writ, project, design):
    writ("init")
    code, out, _ = writ("plan", str(design), "--stage", "inventory", *staged())
    assert code == 0
    assert "stages complete: inventory" in out
    assert "Nothing has been committed" in out
    data = state.load(project)
    assert data["tasks"] == {}
    directory = next(state.plans_dir(project).iterdir())
    assert (directory / "requirements.json").exists()
    assert (directory / "inventory.json").exists()
    # verification comes after inventory, so it was not run
    assert not (directory / "verification.json").exists()


def test_a_pipeline_resumes_instead_of_paying_for_its_analyses_again(
    writ, project, design
):
    writ("init")
    writ("plan", str(design), "--stage", "requirements", *staged())
    plan_id = next(state.plans_dir(project).iterdir()).name
    code, out, _ = writ("plan", str(design), "--plan-id", plan_id, *staged())
    assert code == 0
    assert "requirements: reusing requirements.json" in out
    assert "inventory: wrote" in out
    record = plans.plan_status(state.load(project))["pipeline"]
    assert record["stages"]["requirements"]["reused"] is True
    assert record["stages"]["inventory"]["reused"] is False


def test_refresh_re_runs_a_stage_that_already_has_an_artifact(writ, project, design):
    writ("init")
    writ("plan", str(design), "--stage", "requirements", *staged())
    plan_id = next(state.plans_dir(project).iterdir()).name
    code, out, _ = writ(
        "plan", str(design), "--plan-id", plan_id, "--refresh", *staged()
    )
    assert code == 0
    assert "requirements: wrote" in out
    assert "reusing" not in out


def test_a_failed_stage_stops_before_synthesis(writ, project, design):
    """Synthesising from a partial set would cost a run to learn nothing."""
    writ("init")
    silent = f"{shlex.quote(sys.executable)} -c {shlex.quote('pass')}"
    code, out, err = writ("plan", str(design), "--agent", silent, "--quiet")
    assert code == 1
    assert "the requirements stage produced no usable artifact" in err
    assert "wrote no artifact" in err
    assert state.load(project)["tasks"] == {}
    # and it names the resume path rather than making the operator reconstruct it
    assert "--plan-id" in err


def test_a_malformed_artifact_names_the_stage_that_wrote_it(writ, project, design):
    writ("init")
    bad = f"""\
import re, sys
p = sys.stdin.read()
m = re.search(r'^  (\\S+\\.json)$', p, re.M)
open(m.group(1), 'w').write('{{"requirements": "not a list"}}')
"""
    code, _, err = writ("plan", str(design), "--agent", agent(bad), "--quiet")
    assert code == 1
    assert "requirements stage" in err


def test_a_dropped_requirement_holds_the_staged_plan(writ, project, design):
    """The end-to-end version: reconcile's findings reach the approval gate."""
    writ("init")
    forgetful = json.loads(json.dumps(PLAN))
    forgetful["requirements"] = [
        req for req in forgetful["requirements"] if req["id"] != "REQ-002"
    ]
    forgetful["milestones"][0]["tasks"] = [forgetful["milestones"][0]["tasks"][0]]
    forgetful["milestones"][1]["tasks"][0]["depends_on"] = ["T-log"]
    code, out, _ = writ(
        "plan", str(design), *staged("--auto-approve", plan=forgetful)
    )
    assert code == 0
    data = state.load(project)
    blocking = [
        finding
        for finding in plans.findings(data, open_only=True)
        if finding.category == "dropped-requirement"
    ]
    assert len(blocking) == 1
    # --auto-approve does not override it
    assert not plans.runnable(data)
    assert "writ check" in out


def test_no_stages_is_the_older_single_shot_planner(writ, project, design):
    writ("init")
    code, out, _ = writ(
        "plan", str(design), "--no-stages", "--agent", agent(staged_agent()), "--quiet"
    )
    assert code == 0
    assert "analysing" not in out
    assert "(agent)" in out
    directory = next(state.plans_dir(project).iterdir())
    assert not (directory / "requirements.json").exists()


def test_the_baseline_is_reported_before_any_work_starts(writ, project, design):
    """A run that starts on a failing suite must say so, or every failure is ambiguous."""
    writ("init")
    broken = json.loads(json.dumps(INVENTORY))
    broken["baseline_result"] = {
        "status": "fail",
        "summary": "2 failed",
        "known_failures": ["tests/test_old.py::test_a"],
    }
    code, out, err = writ("plan", str(design), *staged(inventory=broken))
    assert code == 0
    assert "baseline: pytest -q → fail" in out
    assert "verification already fails" in err
    assert "tests/test_old.py::test_a" in err
    assert plans.plan_status(state.load(project))["pipeline"]["baseline"][
        "known_failures"
    ] == ["tests/test_old.py::test_a"]


def test_an_unresolved_ambiguity_is_warned_about(writ, project, design):
    writ("init")
    open_question = json.loads(json.dumps(REQUIREMENTS))
    open_question["ambiguities"][0]["assumed"] = ""
    code, _, err = writ("plan", str(design), *staged(requirements=open_question))
    assert code == 0
    assert "1 ambiguity(ies) with no assumed reading" in err


def test_an_undemonstrable_requirement_is_warned_about(writ, project, design):
    writ("init")
    payload = json.loads(json.dumps(VERIFICATION))
    payload["undemonstrable"] = [
        {"requirement_id": "REQ-003", "why": "no observable output", "closest": "logs"}
    ]
    code, _, err = writ("plan", str(design), *staged(verification=payload))
    assert code == 0
    assert "no way found to demonstrate REQ-003" in err


def test_staged_planning_still_installs_the_gates(writ, project, design):
    """10–12 sit on top of the new front half unchanged."""
    writ("init")
    writ("plan", str(design), *staged())
    data = state.load(project)
    gate_ids = sorted(
        task_id for task_id, task in data["tasks"].items() if task.get("kind") == "gate"
    )
    assert gate_ids == ["G-FINAL", "G-M01", "G-M02"]
    final = data["tasks"]["G-FINAL"]
    # the final gate is held to the inventory the requirements stage established
    assert final["requirement_ids"] == ["REQ-001", "REQ-002", "REQ-003"]
    assert set(final["depends_on"]) == {"G-M01", "G-M02"}


def test_the_api_reports_the_baseline_the_plan_was_built_on(writ, project, design):
    """A dashboard has to be able to say the suite was already failing."""
    from writ import api

    writ("init")
    broken = json.loads(json.dumps(INVENTORY))
    broken["baseline_result"] = {
        "status": "fail",
        "summary": "2 failed",
        "known_failures": ["tests/test_old.py::test_a"],
    }
    writ("plan", str(design), *staged(inventory=broken))
    payload = api.plan(state.load(project))["pipeline"]
    assert payload["stages"] == sorted(analysis.STAGE_NAMES)
    assert payload["requirements"] == 3
    assert payload["baseline"]["status"] == "fail"
    assert payload["baseline"]["known_failures"] == ["tests/test_old.py::test_a"]


def test_a_plan_without_a_pipeline_reports_none(approved, project):
    from writ import api

    assert api.plan(state.load(project))["pipeline"] == {}


def test_a_requirement_citing_a_heading_that_does_not_exist_is_flagged(
    writ, project, design
):
    """The cheapest evidence that a requirement was read rather than assumed."""
    invented = json.loads(json.dumps(REQUIREMENTS))
    invented["requirements"][0]["source"] = "Milestone 9 — Telemetry"
    writ("init")
    code, _, _ = writ("plan", str(design), *staged(requirements=invented))
    assert code == 0
    untraceable = [
        finding
        for finding in plans.findings(state.load(project), open_only=True)
        if finding.category == "untraceable-requirement"
    ]
    assert len(untraceable) == 1
    assert untraceable[0].requirement_ids == ["REQ-001"]
    assert "Milestone 9 — Telemetry" in untraceable[0].message
    # Advisory: a heading cited loosely is a citation problem, not a fabrication.
    assert untraceable[0].severity == "warning"


def test_requirements_that_trace_cleanly_are_not_flagged(writ, project, design):
    writ("init")
    writ("plan", str(design), *staged())
    assert not [
        finding
        for finding in plans.findings(state.load(project), open_only=True)
        if finding.category == "untraceable-requirement"
    ]
