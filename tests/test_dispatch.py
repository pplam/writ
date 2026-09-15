import json
import sys
import time

from writ import state

# Deterministic, offline stand-ins for a coding agent.
ECHO = f"{sys.executable} -c 'import sys; sys.stdout.write(sys.stdin.read())'"
FAIL = f"{sys.executable} -c 'import sys; sys.stderr.write(\"boom\"); sys.exit(3)'"
SLEEP = f"{sys.executable} -c 'import time; time.sleep(30)'"


def wait_for(predicate, timeout=15.0, interval=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_dry_run_prints_the_prompt_without_a_run(planned, writ, project):
    code, out, _ = writ("dispatch", "M01-001", "--dry-run")
    assert code == 0
    assert "Task M01-001: Milestone 0 — Foundations" in out
    assert "the project builds" in out
    assert "Working rules (non-negotiable)" in out
    assert "Write the failing test first" in out
    assert state.load(project)["runs"] == {}


def test_prompt_includes_design_excerpt_and_constraints(planned, writ):
    writ("edit-task", "M01-001", "--allow", "internal/store", "--forbid", "api/")
    _, out, _ = writ("dispatch", "M01-001", "--dry-run")
    assert "Relevant design section:" in out
    assert "Set up the harness." in out
    assert "internal/store" in out
    assert "api/" in out


def test_successful_dispatch_records_a_run(planned, writ, project):
    code, out, _ = writ("dispatch", "M01-001", "--agent", ECHO)
    assert code == 0
    assert "finished with exit code 0" in out

    data = state.load(project)
    run_id, run = next(iter(data["runs"].items()))
    assert run["task"] == "M01-001"
    assert run["status"] == "completed"
    assert run["exit_code"] == 0

    directory = state.run_dir(project, run_id)
    assert (directory / "prompt.txt").exists()
    # the echo agent writes the prompt back out, proving stdin delivery
    assert "Task M01-001" in (directory / "stdout.log").read_text()

    task = data["tasks"]["M01-001"]
    assert task["status"] == "planned"  # success awaits acceptance sign-off
    assert task["runs"] == [run_id]
    assert any("exit code 0" in item["text"] for item in task["evidence"])


def test_failing_agent_marks_the_task_failed(planned, writ, project):
    code, _, _ = writ("dispatch", "M01-001", "--agent", FAIL)
    assert code == 3
    data = state.load(project)
    run = next(iter(data["runs"].values()))
    assert run["status"] == "failed" and run["exit_code"] == 3
    assert data["tasks"]["M01-001"]["status"] == "failed"
    assert "boom" in (state.run_dir(project, run["id"]) / "stderr.log").read_text()


def test_missing_agent_is_reported_clearly(planned, writ, project):
    code, _, err = writ("dispatch", "M01-001", "--agent", "definitely-not-a-real-agent")
    assert code == 2
    assert "agent command not found" in err
    run = next(iter(state.load(project)["runs"].values()))
    assert run["status"] == "failed" and run["exit_code"] == 127


def test_dispatch_respects_the_dependency_gate(planned, writ):
    code, _, err = writ("dispatch", "M02-001", "--agent", ECHO)
    assert code == 2 and "blocked by incomplete dependencies" in err
    assert writ("dispatch", "M02-001", "--agent", ECHO, "--force")[0] == 0


def test_timeout_terminates_the_agent(planned, writ, project):
    code, _, _ = writ("dispatch", "M01-001", "--agent", SLEEP, "--timeout", "1")
    assert code == 124
    run = next(iter(state.load(project)["runs"].values()))
    assert run["exit_code"] == 124 and run["status"] == "failed"


def test_extra_args_after_separator_reach_the_agent(planned, writ, project):
    agent = f"{sys.executable} -c 'import sys; print(sys.argv[1:])'"
    writ("dispatch", "M01-001", "--agent", agent, "--", "--model", "sonnet")
    run = next(iter(state.load(project)["runs"].values()))
    assert run["command"][-2:] == ["--model", "sonnet"]
    log = (state.run_dir(project, run["id"]) / "stdout.log").read_text()
    assert "'--model', 'sonnet'" in log


def test_detached_run_is_observable_while_it_works(planned, writ, project):
    code, out, _ = writ("dispatch", "M01-001", "--agent", SLEEP, "--detach")
    assert code == 0 and "detached" in out

    assert wait_for(
        lambda: next(iter(state.load(project)["runs"].values())).get("pid") is not None
    )
    # a concurrent CLI invocation can see the live run
    _, status, _ = writ("--json", "status")
    payload = json.loads(status)
    assert payload["running"] == ["M01-001"]
    assert payload["active_runs"] and payload["active_runs"][0]["alive"] is True

    _, runs, _ = writ("--json", "runs", "--active")
    run_id = json.loads(runs)[0]["id"]

    assert writ("cancel", run_id)[0] == 0
    data = state.load(project)
    assert data["runs"][run_id]["status"] == "cancelled"
    assert data["tasks"]["M01-001"]["status"] == "planned"


def test_cancel_refuses_inactive_runs(planned, writ, project):
    writ("dispatch", "M01-001", "--agent", ECHO)
    run_id = next(iter(state.load(project)["runs"]))
    code, _, err = writ("cancel", run_id)
    assert code == 2 and "not active" in err


def test_logs_by_run_and_by_task(planned, writ, project):
    writ("dispatch", "M01-001", "--agent", ECHO)
    run_id = next(iter(state.load(project)["runs"]))
    code, out, _ = writ("logs", run_id)
    assert code == 0 and "Task M01-001" in out
    code, by_task, _ = writ("logs", "M01-001")
    assert code == 0 and by_task == out
    code, tailed, _ = writ("logs", run_id, "--tail", "2")
    assert code == 0 and len(tailed.strip().splitlines()) <= 2


def test_logs_for_task_without_runs(planned, writ):
    code, _, err = writ("logs", "M03-001")
    assert code == 2 and "no runs yet" in err


def test_run_show_and_runs_filter(planned, writ, project):
    writ("dispatch", "M01-001", "--agent", ECHO)
    run_id = next(iter(state.load(project)["runs"]))
    code, out, _ = writ("run", run_id)
    assert code == 0 and f"id: {run_id}" in out and "alive: no" in out
    _, filtered, _ = writ("--json", "runs", "--task", "M01-001")
    assert [r["id"] for r in json.loads(filtered)] == [run_id]
    _, empty, _ = writ("--json", "runs", "--task", "M03-001")
    assert json.loads(empty) == []


def test_reap_reconciles_a_dead_run(planned, writ, project):
    writ("dispatch", "M01-001", "--agent", ECHO)
    run_id = next(iter(state.load(project)["runs"]))
    with state.transaction(project) as data:
        data["runs"][run_id]["status"] = "running"
        data["runs"][run_id]["pid"] = 2 ** 22  # certainly not alive
        data["runs"][run_id].pop("supervisor_pid", None)
        data["tasks"]["M01-001"]["status"] = "running"

    code, out, _ = writ("reap")
    assert code == 0 and run_id in out
    data = state.load(project)
    assert data["runs"][run_id]["status"] == "interrupted"
    assert data["tasks"]["M01-001"]["status"] == "planned"
    assert writ("reap")[1].strip() == "no stale runs"


def test_watch_once_renders_a_frame(planned, writ):
    code, out, _ = writ("watch", "--once", "--no-clear")
    assert code == 0 and "writ watch" in out and "tasks 0/4" in out
