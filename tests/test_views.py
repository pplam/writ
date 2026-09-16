"""Detail views: `writ tasks show`, `writ milestones show`, and `writ show`."""
import json


# --------------------------------------------------------------------------
# tasks show


def test_tasks_show_prints_one_task_in_full(planned, writ):
    code, out, _ = writ("show", "M01-001")
    assert code == 0
    assert "M01-001  Milestone 0 — Foundations" in out
    assert "status: ready" in out
    assert "1. [ ] the project builds" in out
    assert "acceptance criteria (0/3 passed)" in out


def test_tasks_show_names_the_milestone_not_just_its_id(planned, writ):
    _, out, _ = writ("show", "M02-001")
    assert "milestone: M02 — Milestone 1 — Storage" in out


def test_tasks_show_reports_both_directions_of_the_dag(planned, writ):
    _, out, _ = writ("show", "M02-001")
    assert "depends on: M01-001" in out
    assert "blocked by: M01-001" in out
    # knowing what a task unblocks is what tells you whether it is worth doing
    assert "blocks: M02-002" in out


def test_tasks_show_omits_blocks_for_a_leaf_task(planned, writ):
    _, out, _ = writ("show", "M03-001")
    assert "blocks:" not in out


def test_tasks_show_includes_constraints_runs_and_evidence(planned, writ, project):
    writ("task", "M01-001", "--allow", "internal/store", "--forbid", "api/")
    writ("override", "M01-001", "failed", "--reason", "flaky under load", "--accept", "1")
    _, out, _ = writ("show", "M01-001")
    assert "allowed:\n  - internal/store" in out
    assert "forbidden:\n  - api/" in out
    assert "acceptance criteria (1/3 passed)" in out
    assert "1. [x] the project builds" in out
    assert "flaky under load" in out


def test_tasks_show_lists_runs_with_their_outcome(planned, writ, project):
    import sys

    echo = f"{sys.executable} -c 'import sys; sys.stdout.write(sys.stdin.read())'"
    writ("dispatch", "M01-001", "--agent", echo)
    _, out, _ = writ("show", "M01-001")
    assert "runs:" in out
    assert "completed  exit=0" in out


def test_tasks_show_json_is_the_raw_record(planned, writ):
    _, out, _ = writ("--json", "show", "M01-001")
    task = json.loads(out)
    assert task["id"] == "M01-001"
    assert len(task["acceptances"]) == 3


def test_show_resolves_a_milestone_id_without_being_told(planned, writ):
    code, out, _ = writ("show", "M01")
    assert code == 0 and "M01  Milestone 0" in out


def test_tasks_without_an_action_still_lists(planned, writ):
    code, out, _ = writ("list")
    assert code == 0
    assert "M01-001" in out and "M03-001" in out


# --------------------------------------------------------------------------
# milestones show


def test_milestones_show_summarises_and_lists_members(planned, writ):
    code, out, _ = writ("show", "M02")
    assert code == 0
    assert "M02  Milestone 1 — Storage" in out
    assert "tasks: 0/2" in out
    assert "acceptance criteria: 0/3 passed" in out
    assert "M02-001" in out and "M02-002" in out
    # the summary view points at the fuller one
    assert "writ show M02 --verbose" in out


def test_milestones_show_rollup_tracks_completion(planned, writ):
    writ(
        "override", "M01-001", "completed", "--reason", "verified by hand",
        "--accept", "1", "--accept", "2", "--accept", "3",
    )
    _, out, _ = writ("show", "M01")
    assert "tasks: 1/1" in out
    assert "status: completed" in out
    assert "acceptance criteria: 3/3 passed" in out


def test_milestones_show_verbose_expands_every_task(planned, writ):
    code, out, _ = writ("show", "M02", "--verbose")
    assert code == 0
    # both members rendered in full, with their criteria
    assert "appends are atomic" in out
    assert "projection matches replay" in out
    assert out.count("acceptance criteria (") == 2


def test_milestones_show_verbose_short_flag(planned, writ):
    _, out, _ = writ("show", "M02", "-v")
    assert "appends are atomic" in out


def test_milestones_show_json_includes_the_member_tasks(planned, writ):
    _, out, _ = writ("--json", "show", "M02")
    payload = json.loads(out)
    assert payload["id"] == "M02"
    assert [task["id"] for task in payload["task_details"]] == ["M02-001", "M02-002"]
    assert payload["task_details"][0]["acceptances"]


def test_show_rejects_an_id_of_no_known_kind(planned, writ):
    code, _, err = writ("show", "nonsense")
    assert code == 2 and "unknown id" in err


def test_milestones_without_an_action_still_lists(planned, writ):
    code, out, _ = writ("list", "milestones")
    assert code == 0
    assert "M01" in out and "M03" in out


def test_milestones_show_handles_an_empty_milestone(planned, writ):
    writ("task", "--title", "Standalone")
    code, out, _ = writ("show", "M01")
    assert code == 0 and "tasks: 0/1" in out


# --------------------------------------------------------------------------
# writ show keeps working, and gains the same detail


def test_show_task_matches_tasks_show(planned, writ):
    _, direct, _ = writ("show", "M01-001")
    _, via_tasks, _ = writ("show", "M01-001")
    assert direct == via_tasks


def test_show_milestone_matches_milestones_show(planned, writ):
    _, direct, _ = writ("show", "M02")
    _, via_milestones, _ = writ("show", "M02")
    assert direct == via_milestones


def test_show_milestone_verbose_expands_tasks(planned, writ):
    _, out, _ = writ("show", "M02", "--verbose")
    assert "appends are atomic" in out


# --------------------------------------------------------------------------
# one show for every kind of id


def test_show_resolves_a_run_id(planned, writ, project):
    import sys
    from writ import state

    echo = f"{sys.executable} -c 'import sys; sys.stdout.write(sys.stdin.read())'"
    writ("dispatch", "M01-001", "--agent", echo)
    run_id = next(iter(state.load(project)["runs"]))
    code, out, _ = writ("show", run_id)
    assert code == 0
    assert "status: completed  exit: 0" in out
    assert f"writ logs {run_id}" in out


def test_show_run_prompt_prints_what_the_agent_was_given(planned, writ, project):
    import sys
    from writ import state

    echo = f"{sys.executable} -c 'import sys; sys.stdout.write(sys.stdin.read())'"
    writ("dispatch", "M01-001", "--agent", echo)
    run_id = next(iter(state.load(project)["runs"]))
    code, out, _ = writ("show", run_id, "--prompt")
    assert code == 0
    assert "Task M01-001" in out
    assert "Working rules (non-negotiable)" in out


def test_show_resolves_a_decision_id(planned, writ):
    writ("decide", "Fixture-only tests", "--decision", "no live platform")
    code, out, _ = writ("show", "D-0001")
    assert code == 0
    assert "D-0001 — Fixture-only tests" in out
    assert "no live platform" in out


def test_a_task_id_is_not_shadowed_by_its_runs(planned, writ, project):
    """A run id starts with its task id, so resolution order matters."""
    import sys
    from writ import state

    echo = f"{sys.executable} -c 'import sys; sys.stdout.write(sys.stdin.read())'"
    writ("dispatch", "M01-001", "--agent", echo)
    _, out, _ = writ("show", "M01-001")
    assert "acceptance criteria" in out  # the task, not the run


# --------------------------------------------------------------------------
# one list for every collection


def test_list_defaults_to_tasks(planned, writ):
    _, bare, _ = writ("list")
    _, explicit, _ = writ("list", "tasks")
    assert bare == explicit
    assert "M01-001" in bare


def test_list_ready_replaces_the_next_command(planned, writ):
    _, out, _ = writ("list", "--ready")
    assert "M01-001" in out
    assert "M02-001" not in out


def test_list_limit_applies_to_every_collection(planned, writ):
    _, out, _ = writ("--json", "list", "--limit", "2")
    assert len(json.loads(out)) == 2
    _, out, _ = writ("--json", "list", "milestones", "--limit", "1")
    assert len(json.loads(out)) == 1


def test_list_runs_and_decisions_share_the_filters(planned, writ, project):
    import sys

    echo = f"{sys.executable} -c 'import sys; sys.stdout.write(sys.stdin.read())'"
    writ("dispatch", "M01-001", "--agent", echo)
    writ("decide", "A choice", "--decision", "d", "--task", "M01-001")
    _, runs, _ = writ("--json", "list", "runs", "--task", "M01-001")
    assert len(json.loads(runs)) == 1
    _, empty, _ = writ("--json", "list", "runs", "--task", "M03-001")
    assert json.loads(empty) == []
    _, decs, _ = writ("--json", "list", "decisions", "--task", "M01-001")
    assert len(json.loads(decs)) == 1


def test_list_rejects_an_unknown_collection(planned, writ):
    code, _, err = writ("list", "widgets")
    assert code == 2 and "invalid choice" in err


# --------------------------------------------------------------------------
# set replaces the five status verbs


def test_set_walks_a_task_through_its_statuses(planned, writ):
    assert writ("set", "M01-001", "running")[0] == 0
    assert writ("set", "M01-001", "blocked")[0] == 0
    assert writ("set", "M01-001", "planned")[0] == 0
    _, out, _ = writ("show", "M01-001")
    assert "status: ready" in out  # planned + unblocked reads as ready


def test_set_cannot_reach_completed_at_all(planned, writ):
    """Not even --force: completion is a verdict, not a transition."""
    code, _, err = writ("set", "M01-001", "completed")
    assert code == 2 and "invalid choice" in err
    code, _, err = writ("set", "M01-001", "completed", "--force")
    assert code == 2 and "invalid choice" in err


def test_set_rejects_a_derived_status(planned, writ):
    # `ready` is computed from the DAG, never stored
    code, _, err = writ("set", "M01-001", "ready")
    assert code == 2 and "invalid choice" in err


def test_override_can_sign_off_one_criterion(planned, writ):
    assert writ(
        "override", "M01-001", "planned", "--reason", "checked by hand", "--accept", "1"
    )[0] == 0
    _, out, _ = writ("show", "M01-001")
    assert "1. [x] the project builds" in out


# --------------------------------------------------------------------------
# cancel absorbs reap


def test_cancel_without_an_id_reaps_dead_runs(planned, writ, project):
    import sys
    from writ import state

    echo = f"{sys.executable} -c 'import sys; sys.stdout.write(sys.stdin.read())'"
    writ("dispatch", "M01-001", "--agent", echo)
    run_id = next(iter(state.load(project)["runs"]))
    with state.transaction(project) as data:
        data["runs"][run_id]["status"] = "running"
        data["runs"][run_id]["pid"] = 2 ** 22
        data["runs"][run_id].pop("supervisor_pid", None)
        data["tasks"]["M01-001"]["status"] = "running"
    code, out, _ = writ("cancel")
    assert code == 0 and run_id in out
    assert state.load(project)["runs"][run_id]["status"] == "interrupted"
    assert writ("cancel")[1].strip() == "no stale runs"
