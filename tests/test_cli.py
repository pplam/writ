import json


def test_init_then_plan_builds_the_dag(writ, design):
    code, out, _ = writ("init")
    assert code == 0 and "initialized" in out
    code, out, _ = writ("plan", str(design), "--extract")
    assert code == 0
    assert "created 3 milestones and 4 tasks" in out

    code, out, _ = writ("--json", "list")
    tasks = json.loads(out)
    assert [t["id"] for t in tasks] == ["M01-001", "M02-001", "M02-002", "M03-001"]
    assert tasks[0]["depends_on"] == []
    assert tasks[1]["depends_on"] == ["M01-001"]


def test_plan_dry_run_writes_nothing(writ, design):
    writ("init")
    code, out, _ = writ("plan", str(design), "--extract", "--dry-run")
    assert code == 0
    assert "would create 3 milestones" in out
    _, listing, _ = writ("list")
    assert "(none)" in listing


def test_plan_refuses_to_overwrite_without_a_flag(writ, design):
    writ("init")
    writ("plan", str(design), "--extract")
    code, _, err = writ("plan", str(design), "--extract")
    assert code == 2 and "already has tasks" in err
    assert writ("plan", str(design), "--extract", "--force")[0] == 0


def test_plan_append_extends_numbering(writ, design):
    writ("init")
    writ("plan", str(design), "--extract")
    writ("plan", str(design), "--extract", "--append")
    _, out, _ = writ("--json", "list", "milestones")
    assert [m["id"] for m in json.loads(out)] == [
        "M01", "M02", "M03", "M04", "M05", "M06",
    ]


def test_plan_parallel_leaves_tasks_independent(writ, design):
    writ("init")
    writ("plan", str(design), "--extract", "--parallel")
    _, out, _ = writ("--json", "list")
    assert all(t["depends_on"] == [] for t in json.loads(out))


def test_status_reports_progress_and_readiness(planned, writ):
    code, out, _ = writ("--json", "status")
    payload = json.loads(out)
    assert code == 0
    assert payload["tasks"] == 4
    assert payload["tasks_completed"] == 0
    assert payload["ready"] == ["M01-001"]
    _, text, _ = writ("status")
    assert "tasks 0/4" in text


def test_show_task_lists_acceptances(planned, writ):
    code, out, _ = writ("show", "M01-001")
    assert code == 0
    assert "1. [ ] the project builds" in out
    assert "status: ready" in out


def test_show_milestone_lists_its_tasks(planned, writ):
    code, out, _ = writ("show", "M02")
    assert code == 0
    assert "M02-001" in out and "M02-002" in out


def test_show_unknown_id_is_an_error(planned, writ):
    code, _, err = writ("show", "nope")
    assert code == 2 and "unknown id" in err


def test_next_and_graph(planned, writ):
    _, out, _ = writ("list", "--ready")
    assert "M01-001" in out
    _, dot, _ = writ("graph", "--dot")
    assert "digraph writ" in dot and '"M01-001" -> "M02-001"' in dot


def test_lifecycle_with_acceptance_gate(planned, writ):
    assert writ("set", "M01-001", "running")[0] == 0
    code, _, err = writ("set", "M01-001", "completed")
    assert code == 2 and "unmet acceptance criteria" in err
    for index in (1, 2, 3):
        assert writ("accept", "M01-001", str(index), "passed")[0] == 0
    assert writ("set", "M01-001", "completed", "--evidence", "suite green")[0] == 0
    _, out, _ = writ("--json", "status")
    payload = json.loads(out)
    assert payload["tasks_completed"] == 1
    assert payload["ready"] == ["M02-001"]


def test_dependency_gate_blocks_out_of_order_start(planned, writ):
    code, _, err = writ("set", "M02-001", "running")
    assert code == 2 and "blocked by incomplete dependencies" in err
    assert writ("set", "M02-001", "running", "--force")[0] == 0


def test_filters(planned, writ):
    writ("set", "M01-001", "running")
    _, out, _ = writ("--json", "list", "--status", "running")
    assert [t["id"] for t in json.loads(out)] == ["M01-001"]
    _, out, _ = writ("--json", "list", "--milestone", "M02")
    assert [t["id"] for t in json.loads(out)] == ["M02-001", "M02-002"]


def test_task_creates_then_amends(planned, writ, project):
    code, out, _ = writ(
        "task",
        "--title",
        "Spike the store",
        "--milestone",
        "M01",
        "--acceptance",
        "measured, not assumed",
        "--allow",
        "internal/store",
        "--forbid",
        "api/",
    )
    assert code == 0 and "created M01-002" in out
    # the same command amends when given an existing id
    assert writ("task", "M01-002", "--depends", "M01-001")[0] == 0
    _, out, _ = writ("--json", "show", "M01-002")
    task = json.loads(out)
    assert task["depends_on"] == ["M01-001"]
    assert task["allowed"] == ["internal/store"]
    assert task["forbidden"] == ["api/"]


def test_task_needs_a_title_to_create(planned, writ):
    code, _, err = writ("task", "--acceptance", "orphaned")
    assert code == 2 and "needs --title" in err


def test_edit_task_rejects_unknown_dependency(planned, writ):
    code, _, err = writ("task", "M01-001", "--depends", "ghost")
    assert code == 2 and "unknown dependency" in err


def test_milestones_rollup(planned, writ):
    _, out, _ = writ("--json", "list", "milestones")
    rollup = {m["id"]: m for m in json.loads(out)}
    assert rollup["M02"]["tasks_total"] == 2
    assert rollup["M02"]["tasks_completed"] == 0


def test_commands_require_an_initialized_project(writ):
    code, _, err = writ("status")
    assert code == 2 and "no Writ project" in err
