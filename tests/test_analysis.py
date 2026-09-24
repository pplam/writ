"""The staged planning pipeline: two analyses, then a synthesis held to them."""
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
            "details": ["a crash mid-append leaves no partial record"],
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
    plan=PLAN,
) -> str:
    """An agent that writes whatever artifact the prompt asked it for.

    The pipeline tells each stage its exact output path, so one script serves all
    three calls: it reads the path out of the prompt and picks the payload by
    filename. That is also a check on the prompts — a stage that failed to state
    its path would get nothing written and the run would fail.
    """
    payloads = {
        "requirements.json": json.dumps(requirements),
        "inventory.json": json.dumps(inventory),
        "draft.json": json.dumps(plan),
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
    assert analysis.STAGE_NAMES == ("requirements", "inventory")
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
    assert [s.name for s in analysis.upto("inventory")] == list(analysis.STAGE_NAMES)
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


def test_plan_runs_both_analyses_then_synthesises(writ, project, design):
    writ("init")
    code, out, _ = writ("plan", str(design), *staged())
    assert code == 0
    assert "analysing" in out and "2 stage(s)" in out
    for stage in analysis.STAGE_NAMES:
        assert f"{stage}: wrote" in out
    assert "(staged)" in out
    assert _work(state.load(project)) == ["M01-001", "M01-002", "M02-001"]


def test_each_stage_header_names_the_command_it_ran(writ, project, design):
    """The header says how the stage was invoked, not just which agent.

    A stage that hangs or writes nothing is diagnosed by running its own
    invocation by hand, and each stage may resolve its own model and event
    flags — so the agent name alone is not enough to reproduce it.
    """
    writ("init")
    code, out, _ = writ("plan", str(design), *staged())
    assert code == 0
    headers = [line for line in out.splitlines() if "running:" in line]
    assert len(headers) == len(analysis.STAGE_NAMES) + 1  # stages, then synthesis


def test_the_waves_put_requirements_beside_inventory():
    """What may run at once is derived from what each stage says it needs.

    Neither stage declares a need, so both share the one wave.
    """
    grouped = analysis.waves(analysis.STAGES)
    assert [[stage.name for stage in wave] for wave in grouped] == [
        ["requirements", "inventory"],
    ]
    # A partition, not a filter: every chosen stage appears exactly once.
    assert [stage for wave in grouped for stage in wave] == list(analysis.STAGES)


def test_a_wave_of_one_stage_is_still_a_wave():
    assert analysis.waves([analysis.STAGES[0]]) == [[analysis.STAGES[0]]]
    assert analysis.waves([]) == []


def test_parallel_stages_produce_the_same_plan(writ, project, design):
    """Concurrency changes the wall-clock and nothing on disk.

    The artifacts, the tasks and the order they are reported in are what a
    sequential run produces, because a run's record should not depend on which
    agent happened to finish first.
    """
    writ("init")
    code, out, _ = writ("plan", str(design), "--parallel-stages", *staged())
    assert code == 0
    assert _work(state.load(project)) == ["M01-001", "M01-002", "M02-001"]
    reported = [
        line.split(":")[0].strip()
        for line in out.splitlines()
        if ": wrote " in line
    ]
    assert reported[: len(analysis.STAGE_NAMES)] == list(analysis.STAGE_NAMES)


def test_parallel_stages_say_what_the_inventory_gives_up(writ, project, design):
    """The cost is printed, because afterwards it is invisible.

    An inventory that ran without the requirement ids writes a well-formed
    artifact with one field empty. Nothing downstream objects to that, so the only
    place the trade can be seen is the run that made it.
    """
    writ("init")
    code, out, _ = writ("plan", str(design), "--parallel-stages", *staged())
    assert code == 0
    assert "at once: requirements, inventory" in out
    assert "claim no existing coverage" in out


def test_an_inventory_running_blind_is_told_to_claim_no_coverage(tmp_path):
    """The instruction is in the prompt, not only in the flag's help.

    A stage that has no requirement ids and is asked for coverage anyway invents
    them, so the prompt has to say which field to leave alone.
    """
    prompt = analysis.build_prompt(
        analysis.STAGES[1],
        root=tmp_path,
        doc=tmp_path / "design.md",
        artifact_path=tmp_path / "inventory.json",
        artifacts=analysis.Artifacts(),
    )
    assert "leave `existing_coverage` empty" in prompt
    # And when it does have them, that instruction is gone and the ids are there.
    with_ids = analysis.build_prompt(
        analysis.STAGES[1],
        root=tmp_path,
        doc=tmp_path / "design.md",
        artifact_path=tmp_path / "inventory.json",
        artifacts=analysis.Artifacts(
            requirements=analysis.load_requirements(json.dumps(REQUIREMENTS))
        ),
    )
    assert "leave `existing_coverage` empty" not in with_ids
    # the inventory is pointed at, not pasted
    assert "requirements.json" in with_ids
    assert "REQ-001" not in with_ids


def test_a_blind_inventory_citing_an_id_it_invented_fails_the_stage(
    writ, project, design
):
    """The validation is deferred, not dropped.

    Running without the ids is why the claim could not be checked when it was
    written. It is still checked, once the requirements land — a hallucinated
    obligation marked already-satisfied is exactly what that check exists for.
    """
    writ("init")
    invented = dict(INVENTORY)
    invented["existing_coverage"] = [
        {"requirement_id": "REQ-099", "status": "full", "evidence": "tests/test_x.py"}
    ]
    code, out, err = writ(
        "plan", str(design), "--parallel-stages", *staged(inventory=invented)
    )
    assert code == 1
    assert "REQ-099" in out + err
    assert "not in the requirement inventory" in out + err


def test_a_sequential_inventory_is_validated_when_it_is_written():
    """Nothing about the deferred check loosens the ordinary one."""
    invented = dict(INVENTORY)
    invented["existing_coverage"] = [
        {"requirement_id": "REQ-099", "status": "full", "evidence": "tests/test_x.py"}
    ]
    with pytest.raises(WritError, match="REQ-099"):
        analysis.load_inventory(json.dumps(invented), known=["REQ-001"])


def test_every_stage_leaves_its_artifact_on_disk(writ, project, design):
    writ("init")
    writ("plan", str(design), *staged())
    record = plans.plan_status(state.load(project))["pipeline"]
    directory = Path(record["directory"])
    for stage in analysis.STAGES:
        assert (directory / stage.artifact).exists(), stage.name
    assert (directory / "draft.json").exists()
    # the committed plan: a small index, and one file per feature
    index = json.loads((directory / "plan.json").read_text())
    assert [row["id"] for row in index["requirements"]] == ["REQ-001", "REQ-002", "REQ-003"]
    for row in index["features"]:
        assert (project / row["file"]).exists()
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


def test_the_synthesis_prompt_points_at_both_analyses(writ, project, design):
    writ("init")
    writ("plan", str(design), *staged())
    directory = Path(plans.plan_status(state.load(project))["pipeline"]["directory"])
    prompt = (directory / "synthesis" / "prompt.txt").read_text(encoding="utf-8")
    # each analysis is named by its path, with what it is binding for
    relative = directory.resolve().relative_to(project.resolve()).as_posix()
    assert f"{relative}/requirements.json — REQUIREMENTS, the fixed inventory" in prompt
    assert f"{relative}/inventory.json — REPOSITORY" in prompt
    assert "verification.json" not in prompt
    # and not pasted: the ids live in the file
    assert "REQ-002" not in prompt
    # the rules that make those binding still travel with the prompt
    assert "may not drop an entry" in " ".join(prompt.split())


def test_a_stage_prompt_does_not_ask_for_a_plan(writ, project, design):
    """Each analysis is fenced off from the decomposition, in its own prompt."""
    writ("init")
    writ("plan", str(design), "--stage", "requirements", *staged())
    directory = next(state.plans_dir(project).iterdir())
    prompt = (directory / "requirements" / "prompt.txt").read_text(encoding="utf-8")
    assert "Do not propose features, tasks, ordering" in prompt
    assert '"acceptance"' not in prompt


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
    # the verification stage is gone, so nothing writes its artifact
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


def test_a_reconcile_finding_closes_once_the_graph_no_longer_earns_it(
    writ, project, design
):
    """A staged finding is a fact about the graph, so a later check must retire it.

    These were produced once, at plan time, and passed into `run_check` as `extra`.
    Every later check omitted them, and because they carry `source="stage:synthesis"`
    no reporter claimed the authority to close them — so a finding the very next
    patch answered stayed open for the life of the plan and counted toward the
    repeat-finding bound that ends the repair loop. On a real plan, 32 of them sat
    open at revision 1 while the graph had moved to revision 4.
    """
    writ("init")
    done = json.loads(json.dumps(INVENTORY))
    done["existing_coverage"] = [
        {"requirement_id": "REQ-003", "status": "full", "evidence": "tests/test_views.py"}
    ]
    writ("plan", str(design), *staged(inventory=done))

    def replanned(data):
        return [
            finding
            for finding in plans.findings(data, open_only=True)
            if finding.category == "replanned-requirement"
        ]

    assert len(replanned(state.load(project))) == 1

    # Mark it existing with the inventory's evidence, as a repair would.
    with state.transaction(project) as data:
        requirement = data["requirements"]["REQ-003"]
        requirement["status"] = "existing"
        requirement["evidence"] = "tests/test_views.py"
        plans.bump(data)

    writ("check")
    data = state.load(project)
    assert replanned(data) == []
    records = [
        payload
        for payload in plans.finding_records(data)
        if payload.get("category") == "replanned-requirement"
    ]
    assert records and records[0]["disposition"] == "resolved"
    assert records[0]["resolved_revision"] == plans.revision(data)


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


def test_the_api_reports_every_stage_in_pipeline_order(writ, project, design):
    """The dashboard draws the pipeline, so it needs the sequence, not a set.

    `stages` is a sorted set of names: `inventory, requirements`,
    which is neither the order they run in nor a statement about what is missing.
    """
    from writ import api

    writ("init")
    writ("plan", str(design), *staged())
    rows = api.plan(state.load(project))["pipeline"]["stage_rows"]
    assert [row["name"] for row in rows] == list(analysis.STAGE_NAMES)
    assert [row["state"] for row in rows] == ["ok", "ok"]
    assert [row["artifact"] for row in rows] == [
        stage.artifact for stage in analysis.STAGES
    ]


def test_a_stage_with_no_entry_reports_pending_rather_than_vanishing():
    """A stage the record does not mention is named, and marked as not run.

    Not reachable from the CLI today: the record is only written after a synthesis
    that succeeded, and a synthesis only runs once every stage has. It is in the read
    model because the alternative is a payload that shrinks — a pipeline recorded by
    a writ with three stages, read by a writ with four, would silently describe the
    fourth as though it were never part of the pipeline at all, and "this plan rests
    on an analysis nobody ran" is not a thing to infer from an absent key.
    """
    from writ import api

    payload = api._pipeline(
        {
            "pipeline": {
                "plan_id": "design-20250101T000000",
                "stages": {
                    "requirements": {
                        "artifact": "requirements.json",
                        "exit_code": 0,
                        "error": "",
                    }
                },
            }
        }
    )
    states = {row["name"]: row["state"] for row in payload["stage_rows"]}
    assert states == {"requirements": "ok", "inventory": "pending"}


def test_a_failed_stage_is_distinguished_from_one_that_did_not_run():
    """Both leave no artifact, and they are opposite things to go and look at."""
    from writ import api

    payload = api._pipeline(
        {
            "pipeline": {
                "plan_id": "design-20250101T000000",
                "stages": {
                    "requirements": {
                        "artifact": "requirements.json",
                        "error": "wrote no artifact (exit 1)",
                        "exit_code": 1,
                    },
                },
            }
        }
    )
    rows = {row["name"]: row for row in payload["stage_rows"]}
    assert rows["requirements"]["state"] == "failed"
    assert rows["requirements"]["error"] == "wrote no artifact (exit 1)"
    assert rows["inventory"]["state"] == "pending"
    assert rows["inventory"]["error"] == ""


def test_a_reused_stage_says_so_rather_than_reading_as_a_fresh_run(
    writ, project, design
):
    """Resuming a pipeline costs nothing, and the dashboard should not claim it did.

    The stage count is the cost of a `writ plan`, so a dashboard that showed three
    fresh analyses for a resume that paid for none would misreport what the plan cost
    and, worse, how recently each part of it was established.
    """
    from writ import api

    writ("init")
    writ("plan", str(design), "--stage", "requirements", *staged())
    plan_id = next(state.plans_dir(project).iterdir()).name
    writ("plan", str(design), "--plan-id", plan_id, *staged())
    rows = {
        row["name"]: row for row in api.plan(state.load(project))["pipeline"]["stage_rows"]
    }
    assert rows["requirements"]["state"] == "reused"
    assert rows["inventory"]["state"] == "ok"


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
