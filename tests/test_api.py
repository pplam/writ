"""The read model behind `writ serve`.

Two things are worth testing here beyond "it returns something". First, that it
never writes: it is the one part of writ a reader can point at a live project
while agents are working, and a read model that mutates would be the worst
possible bug to find that way. Second, that its field names match `ui/src/types.ts`
— the browser has no type checker at runtime, so a rename on the Python side
would otherwise surface as a blank panel rather than a failure.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.conftest import DESIGN, LEGACY_PLAN
from writ import api, state

UI = Path(__file__).resolve().parent.parent / "ui" / "src"


@pytest.fixture()
def worked(writ, design, project):
    """A project with a finished task, a rejected one, and a live-looking run."""
    writ("init")
    writ("plan", str(design), *LEGACY_PLAN)
    return project


# --------------------------------------------------------------- the contract


def declared_fields(interface: str) -> set[str]:
    """Field names of one interface in ui/src/types.ts, following `extends`.

    Parsed rather than imported because the point is to compare two independent
    statements of the same contract. Generating one from the other would make
    them agree by construction and prove nothing.
    """
    source = (UI / "types.ts").read_text()
    match = re.search(
        rf"export interface {interface}(?: extends (\w+))? \{{(.*?)\n\}}",
        source,
        re.S,
    )
    assert match, f"no interface {interface} in types.ts"
    fields = set(re.findall(r"^\s{2}(\w+)\??:", match.group(2), re.M))
    if match.group(1):
        fields |= declared_fields(match.group(1))
    return fields


def test_overview_matches_the_typescript_interface(worked):
    data = state.load(worked)
    assert set(api.overview(data)) == declared_fields("Overview")


def test_task_row_matches_the_typescript_interface(worked):
    data = state.load(worked)
    assert set(api.tasks(data)[0]) == declared_fields("TaskRow")


def test_task_detail_matches_the_typescript_interface(worked):
    data = state.load(worked)
    assert set(api.task(data, "M01-001")) == declared_fields("Task")


def test_graph_matches_the_typescript_interface(worked):
    data = state.load(worked)
    graph = api.graph(data)
    assert set(graph) == declared_fields("Graph")
    assert set(graph["nodes"][0]) == declared_fields("GraphNode")
    assert set(graph["edges"][0]) == declared_fields("GraphEdge")


def test_snapshot_matches_the_typescript_interface(worked):
    assert set(api.everything(worked)) == declared_fields("Snapshot")


def test_plan_matches_the_typescript_interface(worked):
    data = state.load(worked)
    assert set(api.plan(data)) == declared_fields("Plan")


def test_pipeline_matches_the_typescript_interface():
    """The pipeline payload, and each stage row in it.

    Built from a record rather than by planning a project, because the fields under
    test are the ones a plan *without* a full pipeline still has to carry: the
    contract is that a dashboard can render the shape either way.
    """
    payload = api._pipeline(
        {
            "pipeline": {
                "plan_id": "design-20250101T000000",
                "directory": ".writ/plans/design-20250101T000000",
                "at": "2025-01-01T00:00:00+00:00",
                "stages": {
                    "requirements": {
                        "artifact": ".writ/plans/x/requirements.json",
                        "reused": False,
                        "exit_code": 0,
                        "error": "",
                        "at": "2025-01-01T00:00:00+00:00",
                    }
                },
                "requirement_ids": ["REQ-001"],
                "baseline": {"status": "pass", "commands": ["pytest -q"]},
            }
        }
    )
    assert set(payload) == declared_fields("Pipeline")
    assert set(payload["baseline"]) == declared_fields("PipelineBaseline")
    for row in payload["stage_rows"]:
        assert set(row) == declared_fields("PipelineStage")


def test_phase_matches_the_typescript_interfaces(writ, design, project):
    """The phase payload, each step in it, and each edge.

    Built by planning rather than from a literal, unlike the pipeline test above,
    because the fields under test include the ones only a real run produces — the
    resolved command, the transcript directory, the geometry.
    """
    from writ import phases

    writ("init")
    writ("plan", str(design), "--extract", "--auto-approve")
    # `--extract` runs no agents, so there is no phase to describe; declare one
    # directly to get a record with every field on it.
    phase_id = phases.begin(
        project,
        doc=str(design),
        plan_id="design-20250101T000000",
        steps=phases.declare(stages=(), synthesis=True),
    )
    phases.start_step(project, phase_id, "synthesis")
    phases.finish_step(project, phase_id, "synthesis", status="ok", exit_code=0)
    payload = api.phase(state.load(project))
    assert set(payload) == declared_fields("Phase")
    assert set(payload["steps"][0]) == declared_fields("PhaseStep")
    assert set(payload["edges"][0]) == declared_fields("PhaseEdge")
    output = api.step_output(state.load(project), project, "synthesis")
    assert set(output) == declared_fields("StepOutput")
    assert set(output["text"]) == declared_fields("LogTail")


def test_run_row_and_detail_match_the_typescript_interfaces(writ, design, project):
    writ("init")
    writ("plan", str(design), *LEGACY_PLAN)
    writ("dispatch", "M01-001", "--agent", "true")
    data = state.load(project)
    assert set(api.runs(data)[0]) == declared_fields("RunRow")
    run_id = next(iter(data["runs"]))
    assert set(api.run(data, project, run_id)) == declared_fields("Run")


def test_decision_matches_the_typescript_interface(writ, design, project):
    writ("init")
    writ("plan", str(design), *LEGACY_PLAN)
    data = state.load(project)
    data["decisions"] = [
        {
            "id": "D-0001",
            "title": "Frames are length-prefixed",
            "status": "proposed",
            "context": "c",
            "decision": "d",
            "consequences": "q",
            "at": state.utcnow(),
            "by": "agent",
            "task": "M01-001",
        }
    ]
    state.save(project, data)
    assert set(api.decisions(state.load(project))[0]) == declared_fields("Decision")


# ------------------------------------------------------------------ read-only


def test_reading_everything_does_not_touch_the_store(worked):
    """The whole point of a read model: pointing it at a live project is safe."""
    path = state.state_file(worked)
    before = (path.read_bytes(), path.stat().st_mtime)
    for _ in range(3):
        api.everything(worked)
    assert (path.read_bytes(), path.stat().st_mtime) == before


def test_reading_a_run_does_not_touch_its_directory(writ, design, project):
    writ("init")
    writ("plan", str(design), *LEGACY_PLAN)
    writ("dispatch", "M01-001", "--agent", "true")
    data = state.load(project)
    run_id = next(iter(data["runs"]))
    run_dir = state.run_dir(project, run_id)
    before = {p.name: p.stat().st_mtime for p in run_dir.iterdir()}
    api.run(data, project, run_id)
    assert {p.name: p.stat().st_mtime for p in run_dir.iterdir()} == before


def test_every_payload_is_json_serialisable(worked):
    """Whatever the store holds, the wire form must survive `json.dumps`."""
    json.dumps(api.everything(worked))


# ---------------------------------------------------------------- the numbers


def test_overview_counts_agree_with_the_store(worked):
    data = state.load(worked)
    over = api.overview(data)
    assert over["tasks"] == len(data["tasks"])
    assert sum(over["counts"].values()) == len(data["tasks"])


def test_progress_counts_completed_not_started(writ, design, project):
    writ("init")
    writ("plan", str(design), *LEGACY_PLAN)
    over = api.overview(state.load(project))
    assert over["completed"] == 0
    assert over["live"] == 0


def test_a_task_row_carries_its_acceptance_ratio(worked):
    rows = {row["id"]: row for row in api.tasks(state.load(worked))}
    assert rows["M01-001"]["total"] == 3
    assert rows["M01-001"]["passed"] == 0


def test_graph_columns_are_dependency_depth(worked):
    """DESIGN is a chain, so every task is its own level."""
    graph = api.graph(state.load(worked))
    columns = sorted({node["column"] for node in graph["nodes"]})
    assert columns == [0, 1, 2, 3]
    assert graph["levels"] == 4


def test_graph_marks_which_edges_are_already_satisfied(writ, design, project):
    writ("init")
    writ("plan", str(design), *LEGACY_PLAN)
    writ("override", "M01-001", "completed", "--reason", "done by hand")
    edges = api.graph(state.load(project))["edges"]
    satisfied = {(e["from"], e["to"]): e["satisfied"] for e in edges}
    assert satisfied[("M01-001", "M02-001")] is True
    assert satisfied[("M02-001", "M02-002")] is False


def test_a_dangling_dependency_does_not_take_the_whole_page_down(writ, design, project):
    """One bad edge should cost that edge, not every view.

    `blocking_dependencies` raises on a dangling id, which is right for a command
    that should refuse to act on a corrupt store. Here the reader is trying to
    find out what is wrong, so the graph drops the edge it cannot place and the
    rest still renders.
    """
    writ("init")
    writ("plan", str(design), *LEGACY_PLAN)
    data = state.load(project)
    data["tasks"]["M02-001"]["depends_on"] = ["M99-999"]
    state.save(project, data)
    graph = api.graph(state.load(project))
    assert len(graph["nodes"]) == 4
    assert "M99-999" not in {edge["from"] for edge in graph["edges"]}
    assert api.task(state.load(project), "M02-001")["blocked_by"] == []


# -------------------------------------------------------------------- details


def test_task_detail_carries_the_evidence_an_agent_reported(writ, design, project):
    writ("init")
    writ("plan", str(design), *LEGACY_PLAN)
    data = state.load(project)
    task = data["tasks"]["M01-001"]
    task["acceptances"][0] = {
        "text": task["acceptances"][0]["text"],
        "status": "passed",
        "evidence": "ran: pytest -q -> 12 passed",
        "by": "agent",
        "at": state.utcnow(),
    }
    state.save(project, data)
    detail = api.task(state.load(project), "M01-001")
    assert detail["acceptances"][0]["evidence"] == "ran: pytest -q -> 12 passed"
    assert detail["passed"] == 1


def test_task_detail_names_what_is_blocking_it(worked):
    detail = api.task(state.load(worked), "M02-001")
    assert detail["depends_on"] == ["M01-001"]
    assert detail["blocked_by"] == ["M01-001"]
    assert detail["blocks"] == ["M02-002"]


def test_run_detail_includes_the_prompt_the_agent_was_given(writ, design, project):
    writ("init")
    writ("plan", str(design), *LEGACY_PLAN)
    writ("dispatch", "M01-001", "--agent", "true")
    data = state.load(project)
    run_id = next(iter(data["runs"]))
    detail = api.run(data, project, run_id)
    assert "M01-001" in detail["prompt"]
    assert "acceptance criteri" in detail["prompt"]


def test_a_log_tail_reports_that_it_is_a_tail(writ, design, project, monkeypatch):
    writ("init")
    writ("plan", str(design), *LEGACY_PLAN)
    writ("dispatch", "M01-001", "--agent", "true")
    data = state.load(project)
    run_id = next(iter(data["runs"]))
    (state.run_dir(project, run_id) / "stdout.log").write_text("x" * 200_000)
    tail = api.run(data, project, run_id)["stdout"]
    assert tail["truncated"] is True
    assert tail["bytes"] == 200_000
    assert len(tail["text"]) <= api.LOG_TAIL_BYTES


def test_a_missing_log_is_empty_not_an_error(writ, design, project):
    writ("init")
    writ("plan", str(design), *LEGACY_PLAN)
    writ("dispatch", "M01-001", "--agent", "true")
    data = state.load(project)
    run_id = next(iter(data["runs"]))
    (state.run_dir(project, run_id) / "stdout.log").unlink()
    tail = api.run(data, project, run_id)["stdout"]
    assert tail == {"text": "", "bytes": 0, "truncated": False}


def test_an_unknown_id_raises_rather_than_returning_a_blank(worked):
    data = state.load(worked)
    with pytest.raises(KeyError):
        api.task(data, "M99-999")
    with pytest.raises(KeyError):
        api.run(data, worked, "nope")


# ------------------------------------------------------------------- activity


def test_activity_is_newest_first(writ, design, project):
    writ("init")
    writ("plan", str(design), *LEGACY_PLAN)
    writ("dispatch", "M01-001", "--agent", "true")
    events = api.activity(state.load(project))
    stamps = [event["at"] for event in events]
    assert stamps == sorted(stamps, reverse=True)


def test_activity_merges_runs_and_decisions(writ, design, project):
    writ("init")
    writ("plan", str(design), *LEGACY_PLAN)
    writ("dispatch", "M01-001", "--agent", "true")
    data = state.load(project)
    data["decisions"] = [
        {
            "id": "D-0001",
            "title": "Frames are length-prefixed",
            "status": "proposed",
            "at": state.utcnow(),
            "context": "c",
            "decision": "d",
            "consequences": "q",
        }
    ]
    state.save(project, data)
    kinds = {event["kind"] for event in api.activity(state.load(project))}
    assert "decision" in kinds
    assert "run-started" in kinds
    assert "run-finished" in kinds


def test_a_task_row_names_dependencies_that_do_not_exist(writ, design, project):
    """A silent omission would leave a task looking merely slow."""
    writ("init")
    writ("plan", str(design), *LEGACY_PLAN)
    data = state.load(project)
    data["tasks"]["M02-001"]["depends_on"] = ["M01-001", "M99-999"]
    state.save(project, data)
    rows = {row["id"]: row for row in api.tasks(state.load(project))}
    assert rows["M02-001"]["unknown_deps"] == ["M99-999"]
    assert rows["M02-001"]["depends_on"] == ["M01-001"]
    assert rows["M01-001"]["unknown_deps"] == []


def test_a_run_that_wrote_no_verdict_says_so_on_the_record(writ, design, project):
    """The most confusing failure writ has, so the record must explain it.

    The agent exits 0, the run reads "completed", and the task did not move.
    `dispatch` explains that at the time; anything reading the run later — the
    dashboard, or `writ show` tomorrow — needs it stored.
    """
    writ("init")
    writ("plan", str(design), *LEGACY_PLAN)
    writ("dispatch", "M01-001", "--agent", "true")
    data = state.load(project)
    run_id = next(iter(data["runs"]))
    detail = api.run(data, project, run_id)
    assert detail["exit_code"] == 0
    assert "without writing a usable verdict" in detail["no_verdict"]
    # `true` says nothing, which is a failed invocation rather than an agent that
    # worked and skipped its report. The page needs the two apart.
    assert detail["no_output"] is True
    # Distinct from a verdict that was written and rejected.
    assert detail["verdict_error"] == ""
    assert state.load(project)["tasks"]["M01-001"]["status"] == "planned"


def test_a_blocked_task_carries_its_reason_to_the_page(writ, design, project):
    """Without this the dashboard can only show the word "blocked".

    A task blocked by its own report has no unsatisfied dependency, so the page
    listed every dependency as satisfied and nothing said what the obstacle was. The
    reason was in the store the whole time, as one history line below four other
    sections.
    """
    writ("init")
    writ("plan", str(design), *LEGACY_PLAN)
    data = state.load(project)
    task = data["tasks"]["M01-001"]
    task["status"] = "blocked"
    task["last_verdict"] = {"outcome": "blocked", "blocked_on": "needs a decision first"}
    state.save(project, data)
    detail = api.task(state.load(project), "M01-001")
    assert detail["blocked_on"] == "needs a decision first"
    # and the thing it is not: no dependency is holding this up
    assert detail["blocked_by"] == []


def test_a_task_that_is_not_blocked_carries_no_reason(writ, design, project):
    """`last_verdict` outlives the status it was written under."""
    writ("init")
    writ("plan", str(design), *LEGACY_PLAN)
    data = state.load(project)
    task = data["tasks"]["M01-001"]
    task["status"] = "completed"
    task["last_verdict"] = {"outcome": "blocked", "blocked_on": "a stale reason"}
    state.save(project, data)
    assert api.task(state.load(project), "M01-001")["blocked_on"] == ""
