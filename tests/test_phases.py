"""The pre-execution record: the plan phase, written while it is still running.

What these tests are really about is timing. Every other record writ keeps is
written after the thing it describes finished, and could therefore be tested by
running the thing and reading the record. This one has to be true *during* — so
several tests below read `state.json` from inside a stub agent, which is the only
way to assert that a step was visible as running while it was running.
"""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from tests.test_analysis import PLAN, agent, staged, staged_agent
from writ import api, phases, state
from writ.state import WritError


def record(root: Path) -> dict:
    described = phases.describe(phases.current(state.load(root)) or {})
    return described


def steps(root: Path) -> dict[str, dict]:
    return {entry["id"]: entry for entry in record(root)["steps"]}


# --------------------------------------------------------------------------
# declaring the attempt before it happens


def test_a_declared_step_is_pending_before_anything_runs():
    from writ import analysis, critics

    declared = phases.declare(
        stages=list(analysis.STAGES),
        critics=critics.CRITICS,
        repair=True,
        auto_approve=True,
    )
    assert every_key_present(declared)
    assert {entry["status"] for entry in declared} == {"pending"}
    ids = [entry["id"] for entry in declared]
    assert ids[:2] == ["stage:requirements", "stage:inventory"]
    assert "synthesis" in ids and "commit" in ids
    assert "repair" in ids and "approval" in ids
    assert sum(1 for entry in declared if entry["kind"] == "critic") == len(
        critics.CRITICS
    )


def every_key_present(declared: list[dict]) -> bool:
    """Every step carries every field, so the dashboard reads no `undefined`."""
    expected = set(phases.make_step(id="x", kind="commit", name="x"))
    return all(set(entry) == expected for entry in declared)


def test_waves_are_the_columns_the_pipeline_actually_runs():
    """The graph's columns come from the concurrency rules, not from a guess.

    Requirements and inventory share one, because that is what `analysis.waves`
    says. A picture that disagreed with that function would show parallelism writ
    will not use, or hide parallelism it does.
    """
    from writ import analysis

    together = phases.declare(stages=list(analysis.STAGES))
    assert [len(w) for w in analysis.waves(analysis.STAGES)] == [2]
    by_wave = {e["id"]: e["wave"] for e in together if e["kind"] == "stage"}
    assert by_wave["stage:requirements"] == by_wave["stage:inventory"]


def test_the_critics_are_declared_as_one_column():
    """Both boxes in one wave, the command-runner among them.

    The phase graph is drawn from `critics.waves`, so a critic held back into its
    own wave would be drawn as a second column of one. None is.
    """
    from writ import critics

    declared = phases.declare(
        stages=(), synthesis=False, critics=critics.CRITICS
    )
    waves: dict[int, list[str]] = {}
    for entry in declared:
        if entry["kind"] == "critic":
            waves.setdefault(entry["wave"], []).append(entry["name"])
    assert len(waves) == 1, f"the critics should share one wave: {waves}"
    assert sorted(next(iter(waves.values()))) == sorted(
        critic.name for critic in critics.CRITICS
    )


def test_nothing_is_declared_for_work_that_was_not_asked_for():
    declared = phases.declare(stages=(), synthesis=True)
    kinds = {entry["kind"] for entry in declared}
    assert kinds == {"synthesis", "commit"}


# --------------------------------------------------------------------------
# writing it as the phase runs


def test_a_plan_records_every_step_it_ran(writ, design, project):
    writ("init")
    code, out, err = writ("plan", str(design), *staged())
    assert code == 0, err
    found = record(project)
    assert found["status"] == "done"
    assert found["plan_id"]
    marks = steps(project)
    assert [marks[f"stage:{name}"]["status"] for name in
            ("requirements", "inventory")] == ["ok", "ok"]
    assert marks["synthesis"]["status"] == "ok"
    assert marks["commit"]["status"] == "ok"


def test_each_step_records_the_command_that_ran_it(writ, design, project):
    """The resolved invocation, not the agent's name.

    This is the first thing anyone does with a step that hung or wrote nothing:
    run it by hand. A stage may resolve its own model and event flags, so the
    agent name alone is not something you can paste.
    """
    writ("init")
    writ("plan", str(design), *staged())
    entry = steps(project)["stage:requirements"]
    assert entry["command"], entry
    assert entry["display"]
    assert entry["directory"].endswith("requirements")
    assert entry["artifact"].endswith("requirements.json")
    assert entry["event_shape"] or entry["event_shape"] == ""


def test_a_reused_stage_is_recorded_without_ever_having_started(writ, design, project):
    """The common case on a re-run, and the one that never calls `on_start`.

    `run_stage` returns before the launch hook when the artifact is already on
    disk. So `finish_step` has to tolerate a step whose first news is that it
    finished — and must not invent a `started_at` for a run that did not happen.
    """
    writ("init")
    code, out, _ = writ("plan", str(design), *staged())
    assert code == 0
    plan_id = record(project)["plan_id"]
    writ("plan", str(design), "--plan-id", plan_id, "--force", *staged())
    entry = steps(project)["stage:requirements"]
    assert entry["status"] == "reused"
    assert entry["started_at"] is None
    assert entry["finished_at"]


def test_a_failed_stage_stops_the_phase_and_says_which(writ, design, project):
    """A stage that writes nothing: the record names it, the rest is skipped."""
    writ("init")
    silent = agent("import sys; sys.exit(7)")
    code, out, err = writ("plan", str(design), "--agent", silent, "--quiet")
    assert code == 1
    found = record(project)
    assert found["status"] == "failed"
    marks = {entry["id"]: entry for entry in found["steps"]}
    assert marks["stage:requirements"]["status"] == "failed"
    assert marks["stage:requirements"]["error"]
    assert marks["stage:requirements"]["exit_code"] == 7
    # Declared and never reached is `skipped`, which is a different claim from
    # `failed` and from a step that is still running.
    assert marks["synthesis"]["status"] == "skipped"
    assert marks["commit"]["status"] == "skipped"


def test_stopping_at_a_named_stage_is_not_a_failure(writ, design, project):
    writ("init")
    code, _, err = writ(
        "plan", str(design), "--stage", "requirements", *staged()
    )
    assert code == 0, err
    found = record(project)
    assert found["status"] == "stopped"
    assert "requirements" in found["note"]
    assert steps(project)["stage:requirements"]["status"] == "ok"


def test_a_step_is_visible_as_running_while_it_is_running(writ, design, project):
    """The whole point, asserted from inside the agent.

    The stub reads `state.json` while it is the running step and writes what it
    saw beside its artifact. Nothing else can prove this: by the time the command
    returns, every step has finished, and a record that only became correct at
    the end would pass every other test here.
    """
    writ("init")
    seen = project / "seen.json"
    script = staged_agent() + f"""
import json as _json, pathlib
_root = pathlib.Path({str(project)!r})
_data = _json.loads((_root / '.writ' / 'state.json').read_text())
_phase = _data['phases'][-1]
_live = [s['id'] for s in _phase['steps'] if s['status'] == 'running']
_out = pathlib.Path({str(seen)!r})
_prev = _json.loads(_out.read_text()) if _out.exists() else []
_out.write_text(_json.dumps(_prev + [{{'phase': _phase['status'], 'live': _live}}]))
"""
    code, _, err = writ("plan", str(design), "--agent", agent(script), "--quiet")
    assert code == 0, err
    observed = json.loads(seen.read_text())
    assert observed, "the agent never read the record"
    assert all(entry["phase"] == "running" for entry in observed)
    # Each agent saw its own step running, and nothing outside its wave: the two
    # analyses run together, so either may see the other, but synthesis runs
    # alone once both have finished.
    analyses = {"stage:requirements", "stage:inventory"}
    stages, last = observed[:-1], observed[-1]
    assert len(stages) == 2
    assert all(entry["live"] and set(entry["live"]) <= analyses for entry in stages)
    assert last["live"] == ["synthesis"]


def test_the_stages_are_both_recorded_running(writ, design, project):
    """Two stages at once, and the record says so while it is true.

    The reason the recording hooks are outside `mirror_lock`: a state write there
    would hold the terminal lock across an flock and two fsyncs. This asserts the
    observable half — that both stages appear live together.
    """
    writ("init")
    seen = project / "seen.json"
    script = staged_agent() + f"""
import json as _json, pathlib, time
_root = pathlib.Path({str(project)!r})
time.sleep(0.4)
_data = _json.loads((_root / '.writ' / 'state.json').read_text())
_phase = _data['phases'][-1]
_live = sorted(s['id'] for s in _phase['steps'] if s['status'] == 'running')
_out = pathlib.Path({str(seen)!r})
_prev = _json.loads(_out.read_text()) if _out.exists() else []
_out.write_text(_json.dumps(_prev + [_live]))
"""
    code, out, err = writ(
        "plan", str(design), "--agent", agent(script), "--quiet"
    )
    assert code == 0, err
    observed = json.loads(seen.read_text())
    both = ["stage:inventory", "stage:requirements"]
    assert both in observed, observed


def test_an_approval_that_declined_says_so(writ, design, project):
    """A plan held at needs-approval is the most common unattended ending.

    Recorded as a step rather than as nothing, because "why was this not
    approved" is the question a reader arrives with, and an absent step reads as
    one that never ran for reasons unknown.
    """
    writ("init")
    writ("plan", str(design), "--auto-approve", *staged())
    entry = steps(project)["approval"]
    assert entry["status"] in ("ok", "skipped")
    assert entry["note"]


# --------------------------------------------------------------------------
# what a record survives


def test_the_record_is_capped(writ, design, project):
    writ("init")
    with state.transaction(project) as data:
        data["phases"] = [
            {"id": f"ph-{n}", "steps": [], "status": "done"}
            for n in range(phases.KEEP + 4)
        ]
    phases.begin(project, doc="d.md", plan_id="p", steps=[])
    found = state.load(project)["phases"]
    assert len(found) == phases.KEEP
    # The newest survive: trimming takes from the front.
    assert found[-1]["id"] == "ph-p"


def test_a_dead_owner_turns_a_running_step_into_an_abandoned_one(writ, project):
    """A `writ plan` that was killed must not read as one still working.

    Derived at read time from the owner's liveness rather than written down,
    because the process that would have written it is the one that died.
    """
    writ("init")
    phase_id = phases.begin(
        project,
        doc="d.md",
        plan_id="p",
        steps=phases.declare(stages=(), synthesis=True),
    )
    phases.start_step(project, phase_id, "synthesis")
    assert steps(project)["synthesis"]["status"] == "running"

    with state.transaction(project) as data:
        # A pid that cannot be this process, with this host's name on it, which is
        # what `confirmed_dead` needs before it will say so.
        owner = data["phases"][-1]["owner"]
        owner["pid"] = 999_999
        owner["start_time"] = "0"
    found = record(project)
    assert found["status"] == "abandoned"
    assert found["running"] is False
    assert steps(project)["synthesis"]["status"] == "abandoned"


def test_finishing_settles_a_step_nobody_will_write_to_again(writ, project):
    writ("init")
    phase_id = phases.begin(
        project, doc="d.md", plan_id="p", steps=phases.declare(stages=(), synthesis=True)
    )
    phases.start_step(project, phase_id, "synthesis")
    phases.finish(project, phase_id, status="failed", note="interrupted")
    marks = steps(project)
    # Running when the phase ended means the process died inside it.
    assert marks["synthesis"]["status"] == "abandoned"
    assert marks["synthesis"]["finished_at"]
    # Pending when it ended means it was declared and never reached.
    assert marks["commit"]["status"] == "skipped"


def test_a_mutation_against_a_record_that_is_gone_does_nothing(writ, project):
    """Trimmed, or never written: the mutation finds nothing and says nothing."""
    writ("init")
    phases.start_step(project, "ph-nope", "synthesis")
    phases.finish_step(project, "ph-nope", "synthesis", status="ok")
    phases.finish(project, None, status="done")
    assert phases.current(state.load(project)) is None


def test_a_store_that_cannot_be_written_does_not_fail_the_plan(
    writ, design, project, monkeypatch, capsys
):
    """Bookkeeping must never be the thing that loses four agent runs.

    The real failure is a contended lock: `state.transaction` waits ten seconds
    for another writ process and then raises. Writ has by then spent the agent
    runs, and abandoning them because a progress marker could not be updated would
    make the record more important than the work. So it is a note on stderr.
    """
    writ("init")
    real = state.transaction

    def contended(root, *args, **kwargs):
        raise WritError("state.lock is held by another writ (pid 4242)")

    monkeypatch.setattr(phases.state, "transaction", contended)
    assert phases.begin(project, doc="d.md", plan_id="p", steps=[]) is None
    phases.start_step(project, "ph-p", "synthesis")
    phases.finish(project, "ph-p", status="done")
    noted = capsys.readouterr().err
    assert "could not record planning progress" in noted
    assert "another writ" in noted

    monkeypatch.setattr(phases.state, "transaction", real)
    # And a plan still runs to completion with the record having been lost.
    monkeypatch.setattr(phases, "begin", lambda *a, **k: None)
    code, _, err = writ("plan", str(design), *staged())
    assert code == 0, err
    assert phases.current(state.load(project)) is None


def test_adding_a_step_shifts_the_columns_after_it(writ, project):
    """A repair round nobody declared is placed where it happened, not at the end."""
    writ("init")
    phase_id = phases.begin(
        project,
        doc="d.md",
        plan_id="p",
        steps=phases.declare(stages=(), synthesis=True, repair=True),
    )
    before = {e["id"]: e["wave"] for e in steps(project).values()}
    phases.add(
        project,
        phase_id,
        [phases.make_step(id="repair:round-2", kind="repair", name="round 2")],
        after="repair",
    )
    after = {e["id"]: e["wave"] for e in steps(project).values()}
    assert after["repair:round-2"] == before["repair"] + 1
    assert after["commit"] == before["commit"]
    assert after["repair"] == before["repair"]


# --------------------------------------------------------------------------
# what the dashboard is served


def test_the_phase_payload_is_laid_out_like_the_task_graph(writ, design, project):
    writ("init")
    writ("plan", str(design), *staged())
    payload = api.phase(state.load(project))
    assert payload["steps"] and payload["edges"]
    assert payload["width"] > api.NODE_WIDTH
    first = payload["steps"][0]
    assert first["x"] == api.MARGIN
    assert first["y"] == api.MARGIN
    # A step's column is its wave, and the geometry follows from it.
    for step in payload["steps"]:
        assert step["x"] == api.MARGIN + step["column"] * (
            api.NODE_WIDTH + api.COLUMN_GAP
        )


def test_an_edge_joins_a_step_to_what_it_waited_for(writ, design, project):
    writ("init")
    writ("plan", str(design), *staged())
    payload = api.phase(state.load(project))
    pairs = {(edge["from"], edge["to"]) for edge in payload["edges"]}
    assert ("stage:requirements", "synthesis") in pairs
    assert ("stage:inventory", "synthesis") in pairs
    assert all(edge["satisfied"] for edge in payload["edges"])


def test_a_project_with_no_phase_record_gets_an_empty_payload(writ, design, project):
    """Older plans, `--extract`, `--from-plan`: nothing ran, so there is nothing.

    The Plan page falls back to the pipeline's stage rows, which is what it showed
    before this record existed.
    """
    writ("init")
    writ("plan", str(design), "--extract")
    assert api.phase(state.load(project)) == {}


def test_a_step_output_is_rendered_by_writs_own_renderer(writ, design, project):
    """The page's `· tool` lines come from `writ/stream.py`, not from TypeScript.

    Asserted by handing a step an events file in pi's shape and checking the
    payload reads like the terminal does.
    """
    writ("init")
    writ("plan", str(design), *staged())
    data = state.load(project)
    entry = phases.step(phases.current(data), "stage:requirements")
    directory = Path(entry["directory"])
    (directory / "events.jsonl").write_text(
        json.dumps(
            {
                "type": "tool_execution_start",
                "toolName": "read_file",
                "toolCallId": "1",
                "args": {"path": "design.md"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with state.transaction(project) as live:
        phases.step(phases.current(live), "stage:requirements")["event_shape"] = "pi"
    payload = api.step_output(state.load(project), project, "stage:requirements")
    assert payload["activity"] == ["· read_file design.md"]
    assert payload["status"] == "ok"


def test_an_unknown_step_is_not_found_rather_than_a_path(writ, design, project):
    """The path comes from the record; the request only selects a step.

    A crafted id therefore names nothing — it cannot reach a file — and the server
    turns the `KeyError` into a 404.
    """
    writ("init")
    writ("plan", str(design), *staged())
    data = state.load(project)
    with pytest.raises(KeyError):
        api.step_output(data, project, "../../etc/passwd")
    with pytest.raises(KeyError):
        api.step_output(data, project, "stage:nope")


def test_a_step_writ_ran_itself_offers_no_transcript(writ, design, project):
    """`commit` and `approval` are writ's own work: no directory, no output pane."""
    writ("init")
    writ("plan", str(design), *staged())
    payload = api.phase(state.load(project))
    by_id = {step["id"]: step for step in payload["steps"]}
    assert by_id["commit"]["has_output"] is False
    assert by_id["stage:requirements"]["has_output"] is True


# --------------------------------------------------------------------------
# the steps nobody could declare


def test_a_repair_round_and_its_re_review_are_appended_as_they_happen(
    writ, design, project, tmp_path, monkeypatch
):
    """Two kinds of step that cannot be foreseen from the flags.

    A repair round exists only because the critics objected; a re-review is the
    critics reading a plan a patch has changed, at a bumped revision and in a new
    `reviews/rN` directory. Giving that second pass the declared step's id would
    overwrite the first pass's result and point its transcript at the wrong place,
    so it gets an id of its own.
    """
    from tests.test_adjudicate import ADDS_THE_TASK, ADJUDICATOR, CRITIC_ONCE
    from tests.test_adjudicate import agent as sub_agent

    writ("init")
    monkeypatch.setenv("WRIT_TEST_ONCE", str(tmp_path / "reported"))
    monkeypatch.setenv("WRIT_TEST_EDIT", json.dumps(ADDS_THE_TASK))
    code, out, err = writ(
        "plan", str(design), *staged(),
        "--critics", "fidelity", "--critic-agent", sub_agent(CRITIC_ONCE),
        "--repair", "--adjudicator-agent", sub_agent(ADJUDICATOR),
    )
    assert code == 0, err
    assert "repairing the plan" in out
    marks = steps(project)
    # The declared critic step is the first pass, and it stands.
    assert marks["critic:fidelity"]["status"] == "ok"
    # Round 1 is the declared repair step, which actually ran.
    assert marks["repair"]["status"] == "ok"
    assert marks["repair"]["note"]
    # The re-review is a step of its own, at the patched plan's revision.
    rereads = [
        entry for entry in marks.values()
        if entry["kind"] == "critic" and entry["id"] != "critic:fidelity"
    ]
    assert rereads, sorted(marks)
    assert rereads[0]["id"].startswith("critic:fidelity@r")
    assert rereads[0]["status"] == "ok"
    # And it is placed after the repair that caused it, not at the end.
    assert rereads[0]["wave"] > marks["repair"]["wave"]


def test_repair_that_was_armed_and_not_needed_says_so(writ, design, project):
    """`--repair` with nothing blocking: the step is skipped, with the reason."""
    writ("init")
    code, _, err = writ("plan", str(design), "--repair", *staged())
    assert code == 0, err
    entry = steps(project)["repair"]
    assert entry["status"] == "skipped"
    assert "nothing blocking" in entry["note"]


def test_a_very_long_event_log_is_read_from_a_bounded_tail(writ, design, project, monkeypatch):
    """Polled once a second, so the read has to stay affordable.

    A long agent turn can put megabytes in one `events.jsonl`, and rendering all of
    it every second to show the last screenful is a cost that grows with the run.
    Past the cap the oldest events are dropped, and the seek lands mid-line — which
    the renderer has to survive, since half a JSON object is not an event.
    """
    writ("init")
    writ("plan", str(design), *staged())
    data = state.load(project)
    entry = phases.step(phases.current(data), "stage:requirements")
    monkeypatch.setattr(api, "STEP_EVENT_BYTES", 400)
    lines = [
        json.dumps(
            {
                "type": "tool_execution_start",
                "toolName": f"tool_{n}",
                "toolCallId": str(n),
                "args": {"path": f"file-{n}.md"},
            }
        )
        for n in range(40)
    ]
    Path(entry["directory"], "events.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    with state.transaction(project) as live:
        phases.step(phases.current(live), "stage:requirements")["event_shape"] = "pi"
    payload = api.step_output(state.load(project), project, "stage:requirements")
    assert payload["activity"], "the tail rendered nothing"
    # The newest survive, the oldest are dropped, and nothing is garbled.
    assert payload["activity"][-1] == "· tool_39 file-39.md"
    assert "· tool_0 file-0.md" not in payload["activity"]
    assert all(line.startswith("· tool_") for line in payload["activity"])


def test_a_keyboard_interrupt_closes_the_record(writ, design, project, monkeypatch):
    """A `^C` mid-plan is the likeliest early ending, and the one worth recording.

    Left open, the record would have the dashboard reporting a plan in flight long
    after the terminal came back. The exception still propagates — the CLI's own
    handler owns the exit code — so this is about what is on the record afterwards.
    """
    from writ import commands

    writ("init")
    real = commands._run_stages

    def interrupted(*args, **kwargs):
        result = real(*args, **kwargs)
        raise KeyboardInterrupt()

    monkeypatch.setattr(commands, "_run_stages", interrupted)
    with pytest.raises(KeyboardInterrupt):
        writ("plan", str(design), *staged())
    found = record(project)
    assert found["status"] == "failed"
    assert found["finished_at"]
    assert not any(entry["status"] == "running" for entry in found["steps"])


def test_reading_the_record_does_not_touch_the_document(writ, design, project):
    """`writ serve` computes the phase payload on every snapshot it serves.

    A read model that mutated its input would be the worst bug to find on a live
    project, so this is asserted about the document rather than about the payload.
    `tests/test_serve.py` makes the same claim about the file on disk.
    """
    writ("init")
    writ("plan", str(design), *staged())
    data = state.load(project)
    before = json.dumps(data, sort_keys=True)
    api.phase(data)
    api.step_output(data, project, "commit")
    api.everything(project)
    assert json.dumps(data, sort_keys=True) == before

    # And on a document that has no `phases` key at all, which is what an older
    # project's state looks like before `state.load` defaults it.
    bare = {"tasks": {}, "runs": {}}
    assert phases.current(bare) is None
    assert bare == {"tasks": {}, "runs": {}}


def test_an_unknown_model_leaves_a_closed_record_naming_the_reason(
    writ, design, project
):
    """The record opens before the stages resolve, so a resolve error has to close it.

    `--model nonsense` raises from `agents.resolve` before any agent runs, which is
    deliberate — writ refuses before creating a pipeline directory. The phase has
    already been declared by then, so the wrapper's `finally` is what keeps the
    dashboard from reporting a plan in flight that never started.
    """
    writ("init")
    code, _, err = writ(
        "plan", str(design), *staged(), "--model", "nonsense-9000"
    )
    assert code != 0
    found = record(project)
    assert found["status"] == "failed"
    assert found["finished_at"]
    assert found["note"]
    assert {entry["status"] for entry in found["steps"]} == {"skipped"}
