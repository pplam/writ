"""`writ run`: walking the whole DAG, in parallel, resumably.

The agents here are real subprocesses that write real verdict files, because the
things worth testing — that two agents do not claim one task, that a killed
session resumes — only exist once processes are actually running.
"""
import json
import os
import shlex
import sys
from pathlib import Path

import pytest

from writ import orchestrator, runner, state


# --------------------------------------------------------------------------
# fake agents


IMPLEMENTER = """
import json, os, re, sys, time
prompt = sys.stdin.read()
path = re.search(r'^  (\\S*verdict\\.json)$', prompt, re.M).group(1)
total = int(re.search(r'has (\\d+) acceptance criteri', prompt).group(1))
time.sleep(float(os.environ.get("WRIT_TEST_SLEEP", "0")))
open(path, "w").write(json.dumps({
    "outcome": "complete",
    "summary": "did the work",
    "criteria": [
        {"number": i, "status": "passed", "evidence": "ran: pytest -q -> ok"}
        for i in range(1, total + 1)
    ],
}))
print("implemented")
"""

REVIEWER = """
import json, os, re, sys, time
prompt = sys.stdin.read()
path = re.search(r'^  (\\S*verdict\\.json)$', prompt, re.M).group(1)
total = int(re.search(r'has (\\d+) acceptance criteri', prompt).group(1))
time.sleep(float(os.environ.get("WRIT_TEST_SLEEP", "0")))
open(path, "w").write(json.dumps({
    "decision": "accept",
    "summary": "verified independently",
    "criteria": [
        {"number": i, "status": "passed", "evidence": "re-ran: pytest -q -> ok"}
        for i in range(1, total + 1)
    ],
}))
print("reviewed")
"""

REJECTOR = """
import json, re, sys
prompt = sys.stdin.read()
path = re.search(r'^  (\\S*verdict\\.json)$', prompt, re.M).group(1)
total = int(re.search(r'has (\\d+) acceptance criteri', prompt).group(1))
open(path, "w").write(json.dumps({
    "decision": "reject",
    "summary": "not convinced",
    "criteria": [
        {"number": i, "status": "failed", "evidence": "the tests do not cover it"}
        for i in range(1, total + 1)
    ],
}))
print("rejected")
"""

#: an agent that records when it ran, so overlap can be measured
STAMPER = """
import json, os, re, sys, time
prompt = sys.stdin.read()
path = re.search(r'^  (\\S*verdict\\.json)$', prompt, re.M).group(1)
total = int(re.search(r'has (\\d+) acceptance criteri', prompt).group(1))
task = re.search(r'Task (M\\d+-\\d+)', prompt).group(1)
stamps = os.environ["WRIT_TEST_STAMPS"]
start = time.time()
time.sleep(float(os.environ.get("WRIT_TEST_SLEEP", "0.4")))
with open(stamps, "a") as handle:
    handle.write(json.dumps({"task": task, "start": start, "end": time.time()}) + "\\n")
open(path, "w").write(json.dumps({
    "outcome": "complete",
    "summary": "did the work",
    "criteria": [
        {"number": i, "status": "passed", "evidence": "ran: pytest -q"}
        for i in range(1, total + 1)
    ],
}))
"""

#: an implementer that also proposes decisions, to exercise the log's reporting
PROPOSER = """
import json, os, re, sys
prompt = sys.stdin.read()
path = re.search(r'^  (\\S*verdict\\.json)$', prompt, re.M).group(1)
total = int(re.search(r'has (\\d+) acceptance criteri', prompt).group(1))
open(path, "w").write(json.dumps({
    "outcome": "complete",
    "summary": "built it",
    "criteria": [
        {"number": i, "status": "passed", "evidence": "ran: pytest -q -> ok"}
        for i in range(1, total + 1)
    ],
    "decisions": [
        {"title": "Frames are length-prefixed",
         "context": "The design does not say how messages are delimited.",
         "decision": "A four-byte big-endian length precedes each payload.",
         "consequences": "A later reader must agree or it desynchronises."},
        {"title": "Timeouts are per-request",
         "context": "Only a total budget was specified.",
         "decision": "Each retry gets the full timeout, not a share of one budget.",
         "consequences": "Worst-case latency is retries times timeout."},
    ],
}))
"""

SLEEPER = """
import sys, time
sys.stdin.read()
print("working", flush=True)
time.sleep(60)
"""


def agent(script: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


@pytest.fixture
def agents_pair():
    return ["--agent", agent(IMPLEMENTER), "--reviewer", agent(REVIEWER)]


def chain(writ, count, *, milestone="M01"):
    """A linear chain of `count` extra tasks after M01-001."""
    previous = "M01-001"
    for index in range(count):
        code, out, err = writ(
            "task",
            "--title",
            f"Step {index}",
            "--milestone",
            milestone,
            "--depends",
            previous,
            "--acceptance",
            "it works",
        )
        assert code == 0, err
        previous = out.strip().split()[-1]


def fan(writ, count, *, root="M01-001", milestone="M01"):
    """`count` independent tasks that all depend only on `root`."""
    created = []
    for index in range(count):
        code, out, err = writ(
            "task",
            "--title",
            f"Leaf {index}",
            "--milestone",
            milestone,
            "--depends",
            root,
            "--acceptance",
            "it works",
        )
        assert code == 0, err
        created.append(out.strip().split()[-1])
    return created


# --------------------------------------------------------------------------
# selection


def make(**tasks):
    """A state document from {id: [deps]}, all planned."""
    return {
        "tasks": {
            task_id: {
                "id": task_id,
                "status": "planned",
                "depends_on": list(deps),
                "acceptances": [],
            }
            for task_id, deps in tasks.items()
        },
        "milestones": {},
        "runs": {},
        "decisions": [],
    }


def test_the_first_job_is_the_only_ready_task():
    data = make(a=[], b=["a"])
    job = orchestrator.next_job(data, busy=[], budget=None, started=[])
    assert job.task_id == "a" and job.role == "agent"


def test_a_reported_task_is_reviewed_before_new_work_starts():
    """Only a completed task unblocks its dependents, and only review completes."""
    data = make(a=[], b=[])
    data["tasks"]["a"]["status"] = "awaiting-review"
    job = orchestrator.next_job(data, busy=[], budget=None, started=[])
    assert job.task_id == "a" and job.role == "reviewer"


def test_a_busy_task_is_not_selected_again():
    """The guard against two agents on one task."""
    data = make(a=[], b=[])
    job = orchestrator.next_job(data, busy=["a"], budget=None, started=["a"])
    assert job.task_id == "b"


def test_a_busy_review_is_not_selected_again():
    data = make(a=[])
    data["tasks"]["a"]["status"] = "awaiting-review"
    assert orchestrator.next_job(data, busy=["a"], budget=None, started=[]) is None


def test_a_blocked_task_is_never_selected():
    data = make(a=[], b=["a"])
    job = orchestrator.next_job(data, busy=["a"], budget=None, started=["a"])
    assert job is None


def test_nothing_is_selected_when_the_budget_is_spent():
    data = make(a=[], b=[])
    assert orchestrator.next_job(data, busy=[], budget=1, started=["a"]) is None


def test_the_budget_does_not_block_a_review():
    """Half-finished work is worse than none, so reviews ignore the budget."""
    data = make(a=[], b=[])
    data["tasks"]["a"]["status"] = "awaiting-review"
    job = orchestrator.next_job(data, busy=[], budget=1, started=["a"])
    assert job.task_id == "a" and job.role == "reviewer"


def test_a_failed_task_is_not_retried_on_its_own():
    data = make(a=[])
    data["tasks"]["a"]["status"] = "failed"
    assert orchestrator.next_job(data, busy=[], budget=None, started=[]) is None


def test_preview_projects_the_whole_walk():
    data = make(a=[], b=["a"])
    jobs = orchestrator.preview(data, budget=None)
    assert [(j.task_id, j.role) for j in jobs] == [
        ("a", "agent"),
        ("a", "reviewer"),
        ("b", "agent"),
        ("b", "reviewer"),
    ]


def test_preview_respects_the_budget():
    data = make(a=[], b=["a"], c=["b"])
    jobs = orchestrator.preview(data, budget=2)
    assert len({j.task_id for j in jobs}) == 2


def test_preview_terminates_on_a_graph_it_cannot_finish():
    """A guard against the projection looping when nothing can progress."""
    data = make(a=[], b=["a"])
    data["tasks"]["a"]["status"] = "failed"
    assert orchestrator.preview(data, budget=None) == []


# --------------------------------------------------------------------------
# walking the graph


def test_run_completes_a_chain(planned, writ, project):
    code, out, _ = writ("run", "--agent", agent(IMPLEMENTER), "--reviewer",
                        agent(REVIEWER))
    assert code == 0, out
    data = state.load(project)
    assert all(t["status"] == "completed" for t in data["tasks"].values())
    assert "4/4 tasks complete" in out


def test_run_dispatches_and_reviews_each_task(planned, writ, project):
    writ("run", "--agent", agent(IMPLEMENTER), "--reviewer", agent(REVIEWER))
    data = state.load(project)
    roles = [run["role"] for run in data["runs"].values()]
    assert roles.count("agent") == 4
    assert roles.count("reviewer") == 4


def test_the_reviewer_is_a_different_command_when_asked(planned, writ, project):
    writ("run", "--agent", agent(IMPLEMENTER), "--reviewer", agent(REVIEWER))
    data = state.load(project)
    implement = {
        " ".join(r["command"]) for r in data["runs"].values() if r["role"] == "agent"
    }
    review = {
        " ".join(r["command"]) for r in data["runs"].values() if r["role"] == "reviewer"
    }
    assert implement and review and implement != review


def test_the_reviewer_defaults_to_the_agent_command(planned, writ, project):
    writ("run", "--agent", agent(REVIEWER))
    data = state.load(project)
    commands = {" ".join(r["command"]) for r in data["runs"].values()}
    assert len(commands) == 1


def test_dependencies_are_respected(planned, writ, project):
    writ("run", "--agent", agent(IMPLEMENTER), "--reviewer", agent(REVIEWER))
    data = state.load(project)
    started = {}
    finished = {}
    for run in data["runs"].values():
        if run["role"] == "agent":
            started.setdefault(run["task"], run["created_at"])
        finished[run["task"]] = run["finished_at"]
    for task_id, task in data["tasks"].items():
        for dep in task.get("depends_on", []):
            assert started[task_id] >= finished[dep], f"{task_id} started before {dep}"


def test_a_rejected_task_does_not_stall_the_walk(planned, writ, project):
    """An independent task must still run after another one fails."""
    fan(writ, 1)
    code, out, _ = writ("run", "--agent", agent(IMPLEMENTER), "--reviewer",
                        agent(REJECTOR))
    assert code == 1
    data = state.load(project)
    assert data["tasks"]["M01-001"]["status"] == "failed"
    # the leaf depends on M01-001, so it cannot run; the summary must say so
    assert "blocked by failed work" in out


def test_failure_downstream_is_reported_transitively(planned, writ):
    _, out, _ = writ("run", "--agent", agent(IMPLEMENTER), "--reviewer",
                     agent(REJECTOR))
    # M01-001 failed, so M02-001 -> M02-002 -> M03-001 are all parked
    assert "M02-001" in out and "M02-002" in out and "M03-001" in out


def test_max_tasks_limits_what_starts(planned, writ, project):
    code, out, _ = writ("run", "--max-tasks", "2", "--agent", agent(IMPLEMENTER),
                        "--reviewer", agent(REVIEWER))
    assert code == 0
    data = state.load(project)
    completed = [t for t in data["tasks"].values() if t["status"] == "completed"]
    assert len(completed) == 2
    assert "ready to dispatch" in out


def test_max_tasks_still_reviews_what_it_started(planned, writ, project):
    """Leaving work at awaiting-review would be worse than not starting it."""
    writ("run", "--max-tasks", "1", "--agent", agent(IMPLEMENTER), "--reviewer",
         agent(REVIEWER))
    data = state.load(project)
    assert not [t for t in data["tasks"].values() if t["status"] == "awaiting-review"]


def test_run_reports_what_is_left(planned, writ):
    _, out, _ = writ("run", "--max-tasks", "1", "--agent", agent(IMPLEMENTER),
                     "--reviewer", agent(REVIEWER))
    assert "1/4 tasks complete" in out


def test_nothing_to_do_is_not_an_error(planned, writ):
    writ("run", "--agent", agent(IMPLEMENTER), "--reviewer", agent(REVIEWER))
    code, out, _ = writ("run", "--agent", agent(IMPLEMENTER))
    assert code == 0
    assert "every task is complete" in out


def test_an_empty_project_says_to_plan_first(project, writ):
    writ("init")
    code, out, _ = writ("run", "--agent", agent(IMPLEMENTER))
    assert code == 0 and "writ plan" in out


# --------------------------------------------------------------------------
# parallelism


def test_parallel_agents_actually_overlap(planned, writ, project, tmp_path, monkeypatch):
    """The point of --parallel: independent work runs at the same time."""
    stamps = tmp_path / "stamps.jsonl"
    monkeypatch.setenv("WRIT_TEST_STAMPS", str(stamps))
    monkeypatch.setenv("WRIT_TEST_SLEEP", "0.5")
    fan(writ, 4)
    # complete the root first so the four leaves are all ready together
    writ("run", "--max-tasks", "1", "--agent", agent(IMPLEMENTER), "--reviewer",
         agent(REVIEWER))
    stamps.write_text("", encoding="utf-8")
    writ("run", "--parallel", "4", "--agent", agent(STAMPER), "--reviewer",
         agent(REVIEWER))

    records = [json.loads(line) for line in stamps.read_text().splitlines() if line]
    assert len(records) >= 4
    peak = _peak_overlap(records)
    assert peak >= 2, f"expected overlap, saw {peak}"


def _peak_overlap(records):
    events = []
    for record in records:
        events.append((record["start"], 1))
        events.append((record["end"], -1))
    events.sort()
    current = peak = 0
    for _, delta in events:
        current += delta
        peak = max(peak, current)
    return peak


def test_parallel_never_runs_one_task_twice(planned, writ, project):
    fan(writ, 5)
    writ("run", "--parallel", "4", "--agent", agent(IMPLEMENTER), "--reviewer",
         agent(REVIEWER))
    data = state.load(project)
    for task_id, task in data["tasks"].items():
        implementations = [
            r for r in data["runs"].values()
            if r["task"] == task_id and r["role"] == "agent"
        ]
        assert len(implementations) == 1, f"{task_id} was implemented twice"


def test_parallel_respects_dependencies(planned, writ, project):
    chain(writ, 3)
    writ("run", "--parallel", "4", "--agent", agent(IMPLEMENTER), "--reviewer",
         agent(REVIEWER))
    data = state.load(project)
    assert all(t["status"] == "completed" for t in data["tasks"].values())


def test_parallel_of_one_never_overlaps(planned, writ, project, tmp_path, monkeypatch):
    """The default: one agent at a time, whatever the graph allows."""
    stamps = tmp_path / "serial.jsonl"
    monkeypatch.setenv("WRIT_TEST_STAMPS", str(stamps))
    monkeypatch.setenv("WRIT_TEST_SLEEP", "0.2")
    fan(writ, 3)
    writ("run", "--parallel", "1", "--agent", agent(STAMPER), "--reviewer",
         agent(REVIEWER))
    records = [json.loads(line) for line in stamps.read_text().splitlines() if line]
    assert len(records) >= 2
    assert _peak_overlap(records) == 1


def test_a_zero_or_negative_parallel_is_treated_as_one(planned, writ):
    code, _, _ = writ("run", "--parallel", "0", "--agent", agent(IMPLEMENTER),
                      "--reviewer", agent(REVIEWER))
    assert code == 0


# --------------------------------------------------------------------------
# resumability


def test_run_reaps_before_deciding_there_is_nothing_to_do(planned, writ, project):
    with state.transaction(project) as data:
        task = data["tasks"]["M01-001"]
        task["status"] = "running"
        data["runs"]["M01-001-stale"] = {
            "id": "M01-001-stale",
            "task": "M01-001",
            "role": "agent",
            "status": "running",
            "pid": 999999,
            "dir": str(state.run_dir(project, "M01-001-stale")),
            "command": ["true"],
            "created_at": state.utcnow(),
            "started_at": state.utcnow(),
            "finished_at": None,
            "exit_code": None,
        }
        task.setdefault("runs", []).append("M01-001-stale")
    code, out, _ = writ("run", "--agent", agent(IMPLEMENTER), "--reviewer",
                        agent(REVIEWER))
    assert code == 0
    assert "reconciled 1 interrupted run" in out
    assert state.load(project)["tasks"]["M01-001"]["status"] == "completed"


def test_a_resumed_run_does_not_redo_completed_work(planned, writ, project):
    writ("run", "--max-tasks", "2", "--agent", agent(IMPLEMENTER), "--reviewer",
         agent(REVIEWER))
    before = len(state.load(project)["runs"])
    done_first = {
        task_id
        for task_id, task in state.load(project)["tasks"].items()
        if task["status"] == "completed"
    }
    writ("run", "--agent", agent(IMPLEMENTER), "--reviewer", agent(REVIEWER))
    data = state.load(project)
    for task_id in done_first:
        runs = [r for r in data["runs"].values() if r["task"] == task_id]
        assert len(runs) == 2, f"{task_id} was re-run"
    assert len(data["runs"]) > before


def test_a_resume_re_reviews_rather_than_re_implements(planned, writ, project):
    """A killed review must not cost the implementation it was judging."""
    writ("run", "--max-tasks", "1", "--agent", agent(IMPLEMENTER),
         "--reviewer", agent(REVIEWER))
    # rewind M01-001 to a review killed mid-flight
    with state.transaction(project) as data:
        task = data["tasks"]["M01-001"]
        task["status"] = "reviewing"
        review_id = [
            run_id for run_id in task["runs"]
            if data["runs"][run_id]["role"] == "reviewer"
        ][-1]
        data["runs"][review_id]["status"] = "running"
        data["runs"][review_id]["pid"] = 999999
        data["runs"][review_id]["owner_pid"] = 999999
    before = [
        run_id
        for run_id, run in state.load(project)["runs"].items()
        if run["role"] == "agent" and run["task"] == "M01-001"
    ]

    # budget 0 new tasks: only the resumed review may run
    code, out, _ = writ("run", "--max-tasks", "0", "--agent", agent(IMPLEMENTER),
                        "--reviewer", agent(REVIEWER))
    assert code == 0
    data = state.load(project)
    assert data["tasks"]["M01-001"]["status"] == "completed"
    after = [
        run_id
        for run_id, run in data["runs"].items()
        if run["role"] == "agent" and run["task"] == "M01-001"
    ]
    assert after == before, "the implementation was redone"


# --------------------------------------------------------------------------
# session ownership


def test_a_second_run_refuses_while_one_is_active(planned, writ, project):
    orchestrator.claim_session(Path(project))
    try:
        code, _, err = writ("run", "--agent", agent(IMPLEMENTER))
        assert code == 2
        assert "another writ run is active" in err
    finally:
        orchestrator.release_session(Path(project))


def test_force_overrides_a_stale_session(planned, writ, project):
    path = state.store_dir(project) / "run.session"
    path.write_text("999999 2020-01-01T00:00:00+00:00\n", encoding="utf-8")
    code, _, _ = writ("run", "--force", "--agent", agent(IMPLEMENTER),
                      "--reviewer", agent(REVIEWER))
    assert code == 0


def test_a_dead_session_does_not_block_a_resume(planned, writ, project):
    """A killed run leaves its session file behind; that must not be fatal."""
    path = state.store_dir(project) / "run.session"
    path.write_text("999999 2020-01-01T00:00:00+00:00\n", encoding="utf-8")
    assert orchestrator.active_session(Path(project)) is None
    code, _, _ = writ("run", "--agent", agent(IMPLEMENTER), "--reviewer",
                      agent(REVIEWER))
    assert code == 0


def test_a_garbled_session_file_is_ignored(planned, writ, project):
    path = state.store_dir(project) / "run.session"
    path.write_text("not a pid\n", encoding="utf-8")
    assert orchestrator.active_session(Path(project)) is None


def test_the_session_file_is_released_on_success(planned, writ, project):
    writ("run", "--agent", agent(IMPLEMENTER), "--reviewer", agent(REVIEWER))
    assert not (state.store_dir(project) / "run.session").exists()


def test_the_session_file_is_released_on_failure(planned, writ, project):
    writ("run", "--agent", agent(IMPLEMENTER), "--reviewer", agent(REJECTOR))
    assert not (state.store_dir(project) / "run.session").exists()


# --------------------------------------------------------------------------
# preview and reporting


def test_dry_run_spends_nothing(planned, writ, project):
    code, out, _ = writ("run", "--dry-run")
    assert code == 0
    assert "would run 8 agent invocations" in out
    assert state.load(project)["runs"] == {}


def test_dry_run_lists_both_phases(planned, writ):
    _, out, _ = writ("run", "--dry-run")
    assert "dispatch M01-001" in out
    assert "review   M01-001" in out


def test_dry_run_admits_it_is_a_projection(planned, writ):
    _, out, _ = writ("run", "--dry-run")
    assert "not a promise" in out


def test_dry_run_honours_max_tasks(planned, writ):
    _, out, _ = writ("run", "--dry-run", "--max-tasks", "2")
    assert "would run 4 agent invocations" in out


def test_quiet_reports_outcomes_but_not_starts(planned, writ):
    _, out, _ = writ("run", "--quiet", "--agent", agent(IMPLEMENTER),
                     "--reviewer", agent(REVIEWER))
    assert "dispatch M01-001" not in out
    assert "completed" in out


def test_json_emits_machine_readable_events(planned, writ):
    _, out, _ = writ("--json", "run", "--max-tasks", "1", "--agent",
                     agent(IMPLEMENTER), "--reviewer", agent(REVIEWER))
    events = [json.loads(chunk) for chunk in _json_objects(out)]
    names = {event["event"] for event in events}
    assert "started" in names and "finished" in names


def test_json_mode_prints_no_prose(planned, writ):
    """A machine-readable stream with a banner in it is not machine-readable."""
    _, out, _ = writ("--json", "run", "--max-tasks", "1", "--agent",
                     agent(IMPLEMENTER), "--reviewer", agent(REVIEWER))
    for chunk in _json_objects(out):
        json.loads(chunk)  # raises if any prose leaked in
    assert "running up to" not in out


def test_json_summary_lists_what_is_left(planned, writ):
    _, out, _ = writ("--json", "run", "--max-tasks", "1", "--agent",
                     agent(IMPLEMENTER), "--reviewer", agent(REVIEWER))
    summary = [
        json.loads(chunk)
        for chunk in _json_objects(out)
        if json.loads(chunk)["event"] == "summary"
    ][0]
    assert summary["completed"] == ["M01-001"]
    assert "M02-001" in summary["remaining"]


def test_json_dry_run_is_json(planned, writ):
    _, out, _ = writ("--json", "run", "--dry-run")
    payload = json.loads(out)
    assert payload["event"] == "preview"
    assert len(payload["invocations"]) == 8


def test_json_idle_is_json(planned, writ):
    writ("run", "--agent", agent(IMPLEMENTER), "--reviewer", agent(REVIEWER))
    _, out, _ = writ("--json", "run")
    assert json.loads(out)["event"] == "idle"


def _json_objects(text):
    """Split concatenated pretty-printed JSON objects."""
    depth = 0
    current = []
    for line in text.splitlines():
        if not line.strip():
            continue
        current.append(line)
        depth += line.count("{") - line.count("}")
        if depth == 0 and current:
            yield "\n".join(current)
            current = []


def test_the_summary_counts_agents_and_tasks(planned, writ):
    _, out, _ = writ("run", "--max-tasks", "1", "--agent", agent(IMPLEMENTER),
                     "--reviewer", agent(REVIEWER))
    assert "ran 2 agents over 1 tasks" in out


def test_an_agent_that_cannot_start_is_reported_not_fatal(planned, writ, project):
    code, _, err = writ("run", "--agent", "definitely-not-a-real-command-xyz")
    assert code == 1
    assert "error" in err.lower()


def test_a_review_that_cannot_start_is_not_retried_forever(planned, writ, project):
    """A failed review leaves the task at awaiting-review: the state that
    selected it. Without a per-role ledger the scheduler re-selects it forever.
    """
    # `--model` against a command writ cannot pass a model to fails in prepare,
    # before any agent runs — the review never even starts.
    code, out, err = writ("run", "--max-tasks", "1", "--agent", agent(IMPLEMENTER),
                          "--reviewer", "definitely-not-an-agent",
                          "--reviewer-model", "whatever")
    assert code == 1
    data = state.load(project)
    assert data["tasks"]["M01-001"]["status"] == "awaiting-review"
    assert len([r for r in data["runs"].values() if r["role"] == "reviewer"]) == 0


def test_a_silent_review_returns_the_task_to_the_queue(planned, writ, project):
    """An agent that exits 0 with no verdict judged nothing, so nothing moves."""
    code, _, _ = writ("run", "--max-tasks", "1", "--agent", agent(IMPLEMENTER),
                      "--reviewer", "true")
    assert code == 0
    data = state.load(project)
    assert data["tasks"]["M01-001"]["status"] == "planned"
    assert len([r for r in data["runs"].values() if r["role"] == "reviewer"]) == 1


def test_an_invalid_review_verdict_is_an_error_not_a_loop(planned, writ, project):
    """An implementer-shaped verdict from a reviewer is rejected, once."""
    code, out, err = writ("run", "--max-tasks", "1", "--agent", agent(IMPLEMENTER),
                          "--reviewer", agent(IMPLEMENTER))
    assert code == 1
    assert "decision must be one of" in err
    data = state.load(project)
    assert len([r for r in data["runs"].values() if r["role"] == "reviewer"]) == 1


def test_a_later_session_can_still_review_it(planned, writ, project):
    writ("run", "--max-tasks", "1", "--agent", agent(IMPLEMENTER),
         "--reviewer", agent(IMPLEMENTER))
    code, _, _ = writ("run", "--max-tasks", "1", "--agent", agent(IMPLEMENTER),
                      "--reviewer", agent(REVIEWER))
    assert code == 0
    assert state.load(project)["tasks"]["M01-001"]["status"] == "completed"


# --------------------------------------------------------------------------
# the scheduling invariants, stated directly


def test_the_claim_makes_a_task_unselectable(planned, writ, project):
    """The guard against double dispatch: prepare marks it running."""
    runner.prepare(
        Path(project), "M01-001", "true", [], timeout=None, cwd=None, force=False
    )
    data = state.load(project)
    assert orchestrator.next_job(data, busy=[], budget=None, started=[]) is None


def test_a_task_running_elsewhere_is_not_selected(planned, writ, project):
    """A detached `writ dispatch` and a `writ run` must not collide."""
    writ("dispatch", "M01-001", "--agent", "true", "--detach")
    data = state.load(project)
    job = orchestrator.next_job(data, busy=[], budget=None, started=[])
    assert job is None or job.task_id != "M01-001"


def test_a_completed_dependency_is_what_opens_the_next_task(planned, writ, project):
    """awaiting-review is not enough: only completed unblocks a dependent."""
    writ("dispatch", "M01-001", "--agent", agent(IMPLEMENTER))
    data = state.load(project)
    assert data["tasks"]["M01-001"]["status"] == "awaiting-review"
    job = orchestrator.next_job(data, busy=[], budget=None, started=[])
    assert job.task_id == "M01-001" and job.role == "reviewer"


def test_every_run_records_its_role(planned, writ, project):
    """`writ logs` and the reviewer prompt both depend on this."""
    writ("run", "--max-tasks", "1", "--agent", agent(IMPLEMENTER),
         "--reviewer", agent(REVIEWER))
    data = state.load(project)
    assert {r["role"] for r in data["runs"].values()} == {"agent", "reviewer"}


def test_run_ids_do_not_collide_within_a_second(planned, writ, project):
    """A dispatch and its review can land in the same second."""
    writ("run", "--max-tasks", "1", "--agent", agent(IMPLEMENTER),
         "--reviewer", agent(REVIEWER))
    data = state.load(project)
    assert len(data["runs"]) == len(set(data["runs"]))
    assert len(data["tasks"]["M01-001"]["runs"]) == 2


def test_the_verdict_is_written_per_run_not_per_task(planned, writ, project):
    """Two runs on one task must not overwrite each other's verdict."""
    writ("run", "--max-tasks", "1", "--agent", agent(IMPLEMENTER),
         "--reviewer", agent(REVIEWER))
    data = state.load(project)
    paths = set()
    for run_id in data["tasks"]["M01-001"]["runs"]:
        path = Path(data["runs"][run_id]["dir"]) / "verdict.json"
        assert path.exists(), f"{run_id} wrote no verdict"
        paths.add(path)
    assert len(paths) == 2


def test_a_timeout_does_not_take_down_the_session(planned, writ, project):
    """One hung agent must cost its own task, not the whole walk."""
    fan(writ, 1)
    code, out, _ = writ("run", "--timeout", "1", "--agent", agent(SLEEPER),
                        "--reviewer", agent(REVIEWER))
    data = state.load(project)
    run = [r for r in data["runs"].values() if r["role"] == "agent"][0]
    assert run["exit_code"] == 124
    # the task did not silently pass, and the session reported rather than hung
    assert data["tasks"]["M01-001"]["status"] != "completed"
    assert "0/" in out or "tasks complete" in out


def test_a_timed_out_task_is_reported_not_completed(planned, writ, project):
    code, _, _ = writ("run", "--max-tasks", "1", "--timeout", "1",
                      "--agent", agent(SLEEPER), "--reviewer", agent(REVIEWER))
    acceptances = state.load(project)["tasks"]["M01-001"]["acceptances"]
    assert all(item["status"] == "pending" for item in acceptances)


def test_max_tasks_zero_means_reviews_only(planned, writ, project):
    """Useful on its own: clear the review queue without starting new work."""
    writ("run", "--max-tasks", "1", "--agent", agent(IMPLEMENTER),
         "--reviewer", "true")  # leaves M01-001 implemented but unjudged
    with state.transaction(project) as data:
        data["tasks"]["M01-001"]["status"] = "awaiting-review"

    code, _, _ = writ("run", "--max-tasks", "0", "--agent", agent(IMPLEMENTER),
                      "--reviewer", agent(REVIEWER))
    assert code == 0
    data = state.load(project)
    assert data["tasks"]["M01-001"]["status"] == "completed"
    assert data["tasks"]["M02-001"]["status"] == "planned", "new work was started"


# --------------------------------------------------------------------------
# selection order


def hub_and_chain(writ):
    """A shallow hub that unblocks three, beside a four-deep chain.

    Shaped so the three orders visibly disagree: by id the hub comes first, by
    depth the chain does. Returns (hub, chain_head) since the fixture already
    holds tasks and the new ids depend on what is there.
    """
    def add(title, dep):
        code, out, err = writ("task", "--title", title, "--milestone", "M01",
                              "--depends", dep, "--acceptance", "a")
        assert code == 0, err
        return out.strip().split()[-1]

    root = "M01-001"
    hub = add("hub", root)
    for _ in range(3):
        add("leaf", hub)
    head = add("deep head", root)
    previous = head
    for step in range(3):
        previous = add(f"deep {step}", previous)
    assert hub < head, "the hub must be numbered first for these tests to bite"
    return hub, head


def dispatch_order(writ, *args):
    _, out, _ = writ("run", "--dry-run", *args)
    return [
        line.split()[-1]
        for line in out.splitlines()
        if "dispatch" in line
    ]


def test_the_default_order_follows_the_plan(planned, writ):
    hub_and_chain(writ)
    order = dispatch_order(writ)
    assert order == sorted(order), "ids were not walked in order"


def test_depth_prefers_the_longest_remaining_chain(planned, writ):
    hub, head = hub_and_chain(writ)
    order = dispatch_order(writ, "--order", "depth")
    # the chain head must come before the hub, though the hub is numbered first
    assert order.index(head) < order.index(hub)


def test_unlocks_prefers_the_task_most_others_wait_on(planned, writ):
    hub, head = hub_and_chain(writ)
    order = dispatch_order(writ, "--order", "unlocks")
    assert order.index(hub) < order.index(head)


def test_no_order_violates_a_dependency(planned, writ, project):
    """An order may only choose among ready tasks, never widen the ready set."""
    hub_and_chain(writ)
    data = state.load(project)
    for name in ("id", "depth", "unlocks"):
        order = dispatch_order(writ, "--order", name)
        seen = set()
        for task_id in order:
            for dep in data["tasks"][task_id].get("depends_on", []):
                assert dep in seen, f"{name}: {task_id} ran before {dep}"
            seen.add(task_id)


def test_every_order_runs_every_task(planned, writ):
    hub_and_chain(writ)
    expected = len(dispatch_order(writ))
    for name in ("depth", "unlocks"):
        assert len(dispatch_order(writ, "--order", name)) == expected


def test_an_order_is_deterministic(planned, writ):
    hub_and_chain(writ)
    for name in ("id", "depth", "unlocks"):
        first = dispatch_order(writ, "--order", name)
        assert dispatch_order(writ, "--order", name) == first


def test_an_unknown_order_is_a_usage_error(planned, writ):
    code, _, err = writ("run", "--order", "sideways", "--dry-run")
    assert code == 2
    assert "invalid choice" in err


def test_depth_ignores_completed_work(planned, writ, project):
    """Depth measures work still to do, not the chain's original length."""
    hub, head = hub_and_chain(writ)
    data = state.load(project)
    deep = orchestrator._depths(data)[head]
    chain = [
        task_id
        for task_id, task in data["tasks"].items()
        if task["title"].startswith("deep ")
    ]
    with state.transaction(project) as live:
        for task_id in chain:
            live["tasks"][task_id]["status"] = "completed"
    shallower = orchestrator._depths(state.load(project))[head]
    assert shallower < deep


def test_a_non_default_order_is_stated_in_the_banner(planned, writ):
    hub_and_chain(writ)
    _, out, _ = writ("run", "--max-tasks", "1", "--order", "depth",
                     "--agent", agent(IMPLEMENTER), "--reviewer", agent(REVIEWER))
    assert "deepest work first" in out


def test_the_default_order_says_nothing_extra(planned, writ):
    _, out, _ = writ("run", "--max-tasks", "1", "--agent", agent(IMPLEMENTER),
                     "--reviewer", agent(REVIEWER))
    assert "deepest work first" not in out
    assert "most-unblocking first" not in out


def test_the_order_reaches_the_json_preview(planned, writ):
    _, out, _ = writ("--json", "run", "--dry-run", "--order", "depth")
    assert json.loads(out)["order"] == "depth"


def test_a_chosen_order_actually_runs(planned, writ, project):
    hub_and_chain(writ)
    code, _, err = writ("run", "--parallel", "2", "--order", "depth",
                        "--agent", agent(IMPLEMENTER), "--reviewer",
                        agent(REVIEWER))
    assert code == 0, err
    data = state.load(project)
    assert all(t["status"] == "completed" for t in data["tasks"].values())


# --------------------------------------------------------------------------
# the progress log


def transitions(out):
    """Just the indented status lines, without the `-> <command>` echoes.

    The fake agents here are `python -c '<source>'`, so their source appears in
    the started line and would match almost any assertion about content.
    """
    return [
        line for line in out.splitlines()
        if line.startswith("         ") or line.startswith("           ")
    ]


def test_the_log_reports_each_transition(planned, writ):
    _, out, _ = writ("run", "--max-tasks", "1", "--agent", agent(IMPLEMENTER),
                     "--reviewer", agent(REVIEWER))
    assert "? M01-001  awaiting-review" in out
    assert "+ M01-001  completed" in out


def test_the_log_names_the_agent_it_started(planned, writ):
    _, out, _ = writ("run", "--max-tasks", "1", "--agent", agent(IMPLEMENTER),
                     "--reviewer", agent(REVIEWER))
    assert "dispatch M01-001  ->" in out
    assert "review   M01-001  ->" in out


def test_the_log_counts_criteria_as_they_pass(planned, writ):
    """M01-001 has three bars; a bare status would not show that."""
    _, out, _ = writ("run", "--max-tasks", "1", "--agent", agent(IMPLEMENTER),
                     "--reviewer", agent(REVIEWER))
    assert "3/3" in out


def test_a_failure_says_why_in_the_log(planned, writ):
    """Otherwise the one line a reader sees sends them to `writ show`."""
    _, out, _ = writ("run", "--max-tasks", "1", "--agent", agent(IMPLEMENTER),
                     "--reviewer", agent(REJECTOR))
    assert "x M01-001  failed" in out
    assert "unmet 1, 2, 3" in out
    assert "not convinced" in out, "the reviewer's own reason was dropped"


def test_a_pass_does_not_repeat_the_summary(planned, writ):
    """The reason matters when something went wrong; otherwise it is noise."""
    _, out, _ = writ("run", "--max-tasks", "1", "--agent", agent(IMPLEMENTER),
                     "--reviewer", agent(REVIEWER))
    assert "verified independently" not in "\n".join(transitions(out))


def test_proposed_decisions_appear_as_they_happen(planned, writ):
    _, out, _ = writ("run", "--max-tasks", "1", "--agent",
                     agent(PROPOSER), "--reviewer", agent(REVIEWER))
    assert "proposed 2 decisions" in out
    assert "Frames are length-prefixed" in out


def test_the_summary_points_at_proposals_left_unresolved(planned, writ):
    """They are inert until a human rules, and no later run will pick them up."""
    _, out, _ = writ("run", "--max-tasks", "1", "--agent", agent(PROPOSER),
                     "--reviewer", agent(REVIEWER))
    assert "decisions proposed: D-0001, D-0002" in out
    assert "writ list decisions --proposed" in out


def test_a_run_without_proposals_says_nothing_about_them(planned, writ):
    _, out, _ = writ("run", "--max-tasks", "1", "--agent", agent(IMPLEMENTER),
                     "--reviewer", agent(REVIEWER))
    assert "decisions proposed" not in out


def test_a_timeout_is_reported_with_its_exit_code(planned, writ):
    _, out, _ = writ("run", "--max-tasks", "1", "--timeout", "1",
                     "--agent", agent(SLEEPER), "--reviewer", agent(REVIEWER))
    assert "(exit 124)" in out


def test_a_crashing_agent_is_reported_with_its_exit_code(planned, writ):
    crash = f"{shlex.quote(sys.executable)} -c 'import sys; sys.exit(3)'"
    _, out, _ = writ("run", "--max-tasks", "1", "--agent", crash)
    assert "(exit 3)" in out


def test_an_unusable_verdict_is_reported_on_the_line(planned, writ):
    _, out, _ = writ("run", "--max-tasks", "1", "--agent", agent(IMPLEMENTER),
                     "--reviewer", agent(IMPLEMENTER))
    assert "decision must be one of" in out


def test_quiet_keeps_the_transitions_and_drops_the_starts(planned, writ):
    _, out, _ = writ("run", "--quiet", "--max-tasks", "1", "--agent",
                     agent(IMPLEMENTER), "--reviewer", agent(REVIEWER))
    assert "dispatch M01-001" not in out
    assert "+ M01-001  completed" in out


def test_a_long_summary_is_trimmed_to_one_line(planned, writ):
    """The log is one line per event; a paragraph would break that."""
    wordy = REJECTOR.replace(
        '"summary": "not convinced"',
        '"summary": "' + "a very long explanation " * 20 + '"',
    )
    _, out, _ = writ("run", "--max-tasks", "1", "--agent", agent(IMPLEMENTER),
                     "--reviewer", agent(wordy))
    reason = [line for line in transitions(out) if "a very long" in line]
    assert len(reason) == 1, reason
    assert len(reason[0]) < 120
    assert reason[0].rstrip().endswith("…")


def test_the_json_stream_carries_the_same_facts(planned, writ):
    _, out, _ = writ("--json", "run", "--max-tasks", "1", "--agent",
                     agent(PROPOSER), "--reviewer", agent(REJECTOR))
    events = [json.loads(chunk) for chunk in _json_objects(out)]
    finished = [e for e in events if e["event"] == "finished"]
    assert any(e["decisions"] for e in finished)
    assert any(e["summary"] for e in finished)
    assert any(e["unmet"] for e in finished)
    assert any(e["criteria"] for e in finished)
