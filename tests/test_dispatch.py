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
    writ("task", "M01-001", "--allow", "internal/store", "--forbid", "api/")
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
    # the echo agent never writes a verdict, so exiting 0 proves nothing and the
    # task goes back to planned rather than being credited
    assert task["status"] == "planned"
    assert task["runs"] == [run_id]
    assert any("without a usable verdict" in item["text"] for item in task["evidence"])


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

    _, runs, _ = writ("--json", "list", "runs", "--active")
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
    code, out, _ = writ("show", run_id)
    assert code == 0 and run_id in out and "alive: no" in out
    assert "status: completed  exit: 0" in out
    _, filtered, _ = writ("--json", "list", "runs", "--task", "M01-001")
    assert [r["id"] for r in json.loads(filtered)] == [run_id]
    _, empty, _ = writ("--json", "list", "runs", "--task", "M03-001")
    assert json.loads(empty) == []


def test_reap_reconciles_a_dead_run(planned, writ, project):
    writ("dispatch", "M01-001", "--agent", ECHO)
    run_id = next(iter(state.load(project)["runs"]))
    with state.transaction(project) as data:
        data["runs"][run_id]["status"] = "running"
        data["runs"][run_id]["pid"] = 2 ** 22  # certainly not alive
        data["runs"][run_id].pop("supervisor_pid", None)
        data["tasks"]["M01-001"]["status"] = "running"

    code, out, _ = writ("cancel")
    assert code == 0 and run_id in out
    data = state.load(project)
    assert data["runs"][run_id]["status"] == "interrupted"
    assert data["tasks"]["M01-001"]["status"] == "planned"
    assert writ("cancel")[1].strip() == "no stale runs"


def test_reap_returns_an_interrupted_review_to_awaiting_review(planned, writ, project):
    """A review whose process died left the task in a status no queue looks at:
    not running, not awaiting review, invisible to every listing.
    """
    writ("dispatch", "M01-001", "--agent", ECHO)
    run_id = next(iter(state.load(project)["runs"]))
    with state.transaction(project) as data:
        data["runs"][run_id]["status"] = "running"
        data["runs"][run_id]["role"] = "reviewer"
        data["runs"][run_id]["pid"] = 2 ** 22
        data["runs"][run_id].pop("supervisor_pid", None)
        data["runs"][run_id].pop("owner_pid", None)
        data["tasks"]["M01-001"]["status"] = "reviewing"

    writ("cancel")
    task = state.load(project)["tasks"]["M01-001"]
    assert task["status"] == "awaiting-review", "the work was thrown away"


def test_a_reaped_task_says_where_it_went(planned, writ, project):
    writ("dispatch", "M01-001", "--agent", ECHO)
    run_id = next(iter(state.load(project)["runs"]))
    with state.transaction(project) as data:
        data["runs"][run_id]["status"] = "running"
        data["runs"][run_id]["pid"] = 2 ** 22
        data["runs"][run_id].pop("supervisor_pid", None)
        data["runs"][run_id].pop("owner_pid", None)
        data["tasks"]["M01-001"]["status"] = "running"
    writ("cancel")
    evidence = state.load(project)["tasks"]["M01-001"]["evidence"]
    assert any("returned to planned" in item["text"] for item in evidence)


def test_cancelling_a_run_is_not_recorded_as_a_failure(planned, writ, project):
    """A deliberate stop and a failing agent mean different things."""
    writ("dispatch", "M01-001", "--agent", SLEEP, "--detach")
    run_id = next(iter(state.load(project)["runs"]))
    assert wait_for(lambda: state.load(project)["runs"][run_id]["status"] == "running")

    code, _, _ = writ("cancel", run_id)
    assert code == 0
    data = state.load(project)
    assert data["runs"][run_id]["status"] == "cancelled"
    assert data["tasks"]["M01-001"]["status"] == "planned", "a stop became a failure"


def test_a_cancelled_task_can_be_dispatched_again(planned, writ, project):
    writ("dispatch", "M01-001", "--agent", SLEEP, "--detach")
    run_id = next(iter(state.load(project)["runs"]))
    assert wait_for(lambda: state.load(project)["runs"][run_id]["status"] == "running")
    writ("cancel", run_id)
    code, _, err = writ("dispatch", "M01-001", "--agent", ECHO)
    assert code == 0, err


def test_a_second_agent_on_a_live_task_is_refused(planned, writ, project):
    """Two processes can both reach `prepare`; the store has to settle it."""
    writ("dispatch", "M01-001", "--agent", SLEEP, "--detach")
    run_id = next(iter(state.load(project)["runs"]))
    assert wait_for(lambda: state.load(project)["runs"][run_id]["status"] == "running")

    code, _, err = writ("dispatch", "M01-001", "--force", "--agent", ECHO)
    assert code == 2
    assert "already has a running agent" in err
    assert f"writ cancel {run_id}" in err
    writ("cancel", run_id)


def test_the_claim_records_the_claiming_process(planned, writ, project):
    """Closes the window between claiming a task and the agent having a pid."""
    writ("dispatch", "M01-001", "--agent", ECHO)
    run = next(iter(state.load(project)["runs"].values()))
    assert run["owner_pid"] > 0


def test_status_renders_progress(planned, writ):
    code, out, _ = writ("status")
    assert code == 0 and "tasks 0/4" in out


def test_dispatch_makes_a_known_agent_headless(planned, writ, project, monkeypatch):
    """Bare `pi` would open a TUI and hang; dispatch must add its print flag."""
    from writ import runner

    monkeypatch.setattr(runner, "execute", lambda root, run_id, **kwargs: 0)
    writ("dispatch", "M01-001", "--agent", "pi", "--model", "sonnet")
    run = next(iter(state.load(project)["runs"].values()))
    assert run["command"] == ["pi", "-p", "--model", "sonnet"]
    assert run["model"] == "sonnet"


def test_dispatch_warns_about_an_unrecognised_agent(planned, writ):
    _, out, err = writ("dispatch", "M01-001", "--agent", ECHO)
    assert "cannot confirm it runs without a terminal" in err
    assert "running:" in out


def test_dispatch_timeout_explains_a_likely_interactive_agent(planned, writ):
    code, _, err = writ("dispatch", "M01-001", "--agent", SLEEP, "--timeout", "1")
    assert code == 124
    assert "interactive session" in err


def test_dispatch_mirrors_agent_output_live(planned, writ):
    """A long implementation run must look alive, not hung."""
    chatty = (
        f"{sys.executable} -c 'import sys; sys.stdin.read(); "
        "print(\"editing store.go\"); sys.stdout.flush(); print(\"tests pass\")'"
    )
    code, out, _ = writ("dispatch", "M01-001", "--agent", chatty)
    assert code == 0
    assert "| editing store.go" in out
    assert "| tests pass" in out


def test_dispatch_quiet_keeps_output_in_the_log_only(planned, writ, project):
    code, out, _ = writ("dispatch", "M01-001", "--agent", ECHO, "--quiet")
    assert code == 0
    assert "| Task M01-001" not in out
    run_id = next(iter(state.load(project)["runs"]))
    assert "Task M01-001" in (state.run_dir(project, run_id) / "stdout.log").read_text()


def test_streamed_dispatch_records_the_same_transcript(planned, writ, project):
    writ("dispatch", "M01-001", "--agent", ECHO)
    run_id = next(iter(state.load(project)["runs"]))
    log = (state.run_dir(project, run_id) / "stdout.log").read_text()
    # the prompt came back through the echo agent, unprefixed on disk
    assert "Task M01-001" in log
    assert "| Task" not in log


def test_streamed_dispatch_still_records_failure(planned, writ, project):
    code, _, err = writ("dispatch", "M01-001", "--agent", FAIL)
    assert code == 3
    assert "boom" in err  # mirrored live
    data = state.load(project)
    run = next(iter(data["runs"].values()))
    assert run["status"] == "failed" and run["exit_code"] == 3
    assert data["tasks"]["M01-001"]["status"] == "failed"
    assert "boom" in (state.run_dir(project, run["id"]) / "stderr.log").read_text()
