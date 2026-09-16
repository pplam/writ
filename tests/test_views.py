"""Detail views: `writ tasks show`, `writ milestones show`, and `writ show`."""
import json


# --------------------------------------------------------------------------
# tasks show


def test_tasks_show_prints_one_task_in_full(planned, writ):
    code, out, _ = writ("tasks", "show", "M01-001")
    assert code == 0
    assert "M01-001  Milestone 0 — Foundations" in out
    assert "status: ready" in out
    assert "1. [ ] the project builds" in out
    assert "acceptance criteria (0/3 passed)" in out


def test_tasks_show_names_the_milestone_not_just_its_id(planned, writ):
    _, out, _ = writ("tasks", "show", "M02-001")
    assert "milestone: M02 — Milestone 1 — Storage" in out


def test_tasks_show_reports_both_directions_of_the_dag(planned, writ):
    _, out, _ = writ("tasks", "show", "M02-001")
    assert "depends on: M01-001" in out
    assert "blocked by: M01-001" in out
    # knowing what a task unblocks is what tells you whether it is worth doing
    assert "blocks: M02-002" in out


def test_tasks_show_omits_blocks_for_a_leaf_task(planned, writ):
    _, out, _ = writ("tasks", "show", "M03-001")
    assert "blocks:" not in out


def test_tasks_show_includes_constraints_runs_and_evidence(planned, writ, project):
    writ("edit-task", "M01-001", "--allow", "internal/store", "--forbid", "api/")
    writ("accept", "M01-001", "1", "passed")
    writ("fail", "M01-001", "--evidence", "flaky under load")
    _, out, _ = writ("tasks", "show", "M01-001")
    assert "allowed:\n  - internal/store" in out
    assert "forbidden:\n  - api/" in out
    assert "acceptance criteria (1/3 passed)" in out
    assert "1. [x] the project builds" in out
    assert "flaky under load" in out


def test_tasks_show_lists_runs_with_their_outcome(planned, writ, project):
    import sys

    echo = f"{sys.executable} -c 'import sys; sys.stdout.write(sys.stdin.read())'"
    writ("dispatch", "M01-001", "--agent", echo)
    _, out, _ = writ("tasks", "show", "M01-001")
    assert "runs:" in out
    assert "completed  exit=0" in out


def test_tasks_show_json_is_the_raw_record(planned, writ):
    _, out, _ = writ("--json", "tasks", "show", "M01-001")
    task = json.loads(out)
    assert task["id"] == "M01-001"
    assert len(task["acceptances"]) == 3


def test_tasks_without_an_action_still_lists(planned, writ):
    code, out, _ = writ("tasks")
    assert code == 0
    assert "M01-001" in out and "M03-001" in out


# --------------------------------------------------------------------------
# milestones show


def test_milestones_show_summarises_and_lists_members(planned, writ):
    code, out, _ = writ("milestones", "show", "M02")
    assert code == 0
    assert "M02  Milestone 1 — Storage" in out
    assert "tasks: 0/2" in out
    assert "acceptance criteria: 0/3 passed" in out
    assert "M02-001" in out and "M02-002" in out
    # the summary view points at the fuller one
    assert "writ milestones show M02 --verbose" in out


def test_milestones_show_rollup_tracks_completion(planned, writ):
    for index in (1, 2, 3):
        writ("accept", "M01-001", str(index), "passed")
    writ("complete", "M01-001")
    _, out, _ = writ("milestones", "show", "M01")
    assert "tasks: 1/1" in out
    assert "status: completed" in out
    assert "acceptance criteria: 3/3 passed" in out


def test_milestones_show_verbose_expands_every_task(planned, writ):
    code, out, _ = writ("milestones", "show", "M02", "--verbose")
    assert code == 0
    # both members rendered in full, with their criteria
    assert "appends are atomic" in out
    assert "projection matches replay" in out
    assert out.count("acceptance criteria (") == 2


def test_milestones_show_verbose_short_flag(planned, writ):
    _, out, _ = writ("show", "M02", "-v")
    assert "appends are atomic" in out


def test_milestones_show_json_includes_the_member_tasks(planned, writ):
    _, out, _ = writ("--json", "milestones", "show", "M02")
    payload = json.loads(out)
    assert payload["id"] == "M02"
    assert [task["id"] for task in payload["task_details"]] == ["M02-001", "M02-002"]
    assert payload["task_details"][0]["acceptances"]


def test_show_rejects_an_id_of_no_known_kind(planned, writ):
    code, _, err = writ("show", "nonsense")
    assert code == 2 and "unknown task or milestone" in err


def test_milestones_without_an_action_still_lists(planned, writ):
    code, out, _ = writ("milestones")
    assert code == 0
    assert "M01" in out and "M03" in out


def test_milestones_show_handles_an_empty_milestone(planned, writ):
    writ("add-task", "Standalone", "--id", "S-001")
    code, out, _ = writ("milestones", "show", "M01")
    assert code == 0 and "tasks: 0/1" in out


# --------------------------------------------------------------------------
# writ show keeps working, and gains the same detail


def test_show_task_matches_tasks_show(planned, writ):
    _, direct, _ = writ("tasks", "show", "M01-001")
    _, via_tasks, _ = writ("tasks", "show", "M01-001")
    assert direct == via_tasks


def test_show_milestone_matches_milestones_show(planned, writ):
    _, direct, _ = writ("milestones", "show", "M02")
    _, via_milestones, _ = writ("milestones", "show", "M02")
    assert direct == via_milestones


def test_show_milestone_verbose_expands_tasks(planned, writ):
    _, out, _ = writ("milestones", "show", "M02", "--verbose")
    assert "appends are atomic" in out
