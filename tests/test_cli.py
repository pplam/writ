import json


def test_init_then_plan_builds_the_dag(forge, design):
    code, out, _ = forge("init")
    assert code == 0 and "initialized" in out
    code, out, _ = forge("plan", str(design))
    assert code == 0
    assert "created 3 milestones and 4 tasks" in out

    code, out, _ = forge("--json", "tasks")
    tasks = json.loads(out)
    assert [t["id"] for t in tasks] == ["M01-001", "M02-001", "M02-002", "M03-001"]
    assert tasks[0]["depends_on"] == []
    assert tasks[1]["depends_on"] == ["M01-001"]


def test_plan_dry_run_writes_nothing(forge, design):
    forge("init")
    code, out, _ = forge("plan", str(design), "--dry-run")
    assert code == 0
    assert "would create 3 milestones" in out
    _, listing, _ = forge("tasks")
    assert "(none)" in listing


def test_plan_refuses_to_overwrite_without_a_flag(forge, design):
    forge("init")
    forge("plan", str(design))
    code, _, err = forge("plan", str(design))
    assert code == 2 and "already has tasks" in err
    assert forge("plan", str(design), "--force")[0] == 0


def test_plan_append_extends_numbering(forge, design):
    forge("init")
    forge("plan", str(design))
    forge("plan", str(design), "--append")
    _, out, _ = forge("--json", "milestones")
    assert [m["id"] for m in json.loads(out)] == [
        "M01", "M02", "M03", "M04", "M05", "M06",
    ]


def test_plan_parallel_leaves_tasks_independent(forge, design):
    forge("init")
    forge("plan", str(design), "--parallel")
    _, out, _ = forge("--json", "tasks")
    assert all(t["depends_on"] == [] for t in json.loads(out))


def test_status_reports_progress_and_readiness(planned, forge):
    code, out, _ = forge("--json", "status")
    payload = json.loads(out)
    assert code == 0
    assert payload["tasks"] == 4
    assert payload["tasks_completed"] == 0
    assert payload["ready"] == ["M01-001"]
    _, text, _ = forge("status")
    assert "tasks 0/4" in text


def test_show_task_lists_acceptances(planned, forge):
    code, out, _ = forge("show", "M01-001")
    assert code == 0
    assert "1. [ ] the project builds" in out
    assert "status: ready" in out


def test_show_milestone_lists_its_tasks(planned, forge):
    code, out, _ = forge("show", "M02")
    assert code == 0
    assert "M02-001" in out and "M02-002" in out


def test_show_unknown_id_is_an_error(planned, forge):
    code, _, err = forge("show", "nope")
    assert code == 2 and "unknown task or milestone" in err


def test_next_and_graph(planned, forge):
    _, out, _ = forge("next")
    assert "M01-001" in out
    _, dot, _ = forge("graph", "--dot")
    assert "digraph forge" in dot and '"M01-001" -> "M02-001"' in dot


def test_lifecycle_with_acceptance_gate(planned, forge):
    assert forge("start", "M01-001")[0] == 0
    code, _, err = forge("complete", "M01-001")
    assert code == 2 and "unmet acceptance criteria" in err
    for index in (1, 2, 3):
        assert forge("accept", "M01-001", str(index), "passed")[0] == 0
    assert forge("complete", "M01-001", "--evidence", "suite green")[0] == 0
    _, out, _ = forge("--json", "status")
    payload = json.loads(out)
    assert payload["tasks_completed"] == 1
    assert payload["ready"] == ["M02-001"]


def test_dependency_gate_blocks_out_of_order_start(planned, forge):
    code, _, err = forge("start", "M02-001")
    assert code == 2 and "blocked by incomplete dependencies" in err
    assert forge("start", "M02-001", "--force")[0] == 0


def test_filters(planned, forge):
    forge("start", "M01-001")
    _, out, _ = forge("--json", "tasks", "--status", "running")
    assert [t["id"] for t in json.loads(out)] == ["M01-001"]
    _, out, _ = forge("--json", "tasks", "--milestone", "M02")
    assert [t["id"] for t in json.loads(out)] == ["M02-001", "M02-002"]


def test_add_and_edit_task(planned, forge, project):
    code, out, _ = forge(
        "add-task",
        "Spike the store",
        "--id",
        "S-001",
        "--acceptance",
        "measured, not assumed",
        "--allow",
        "internal/store",
        "--forbid",
        "api/",
    )
    assert code == 0 and "created S-001" in out
    assert forge("edit-task", "S-001", "--depends", "M01-001")[0] == 0
    _, out, _ = forge("--json", "show", "S-001")
    task = json.loads(out)
    assert task["depends_on"] == ["M01-001"]
    assert task["allowed"] == ["internal/store"]
    assert task["forbidden"] == ["api/"]


def test_edit_task_rejects_unknown_dependency(planned, forge):
    code, _, err = forge("edit-task", "M01-001", "--depends", "ghost")
    assert code == 2 and "unknown dependency" in err


def test_milestones_rollup(planned, forge):
    _, out, _ = forge("--json", "milestones")
    rollup = {m["id"]: m for m in json.loads(out)}
    assert rollup["M02"]["tasks_total"] == 2
    assert rollup["M02"]["tasks_completed"] == 0


def test_commands_require_an_initialized_project(forge):
    code, _, err = forge("status")
    assert code == 2 and "no Forge project" in err
