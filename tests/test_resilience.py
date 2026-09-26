"""Failure injection for the four durability fixes.

Each test breaks one thing on purpose and then asserts on *both* halves of the
recovery: the persisted run record and the resulting task state. Checking only
one lets the exact bug these fixes address slip through — a run marked failed
while its task is still `running` reads as consistent from either side alone.

Four fixes, in the order of the review that asked for them:

1. a worker that raises still leaves its prepared run reconciled;
2. a committed state write is flushed, and a crash mid-write leaves no debris;
3. ownership is a composite identity, so a recycled pid is not mistaken for the
   process that was recorded — across locks, sessions, reaping and cancellation;
4. infrastructure failures are separated from task failures, retried on their own
   budget, and never reported as a rejection of the work.
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from writ import failures, orchestrator, procs, runner, state
from writ.state import WritError

from test_run import IMPLEMENTER, REVIEWER, REJECTOR, agent


# --------------------------------------------------------------------------
# helpers


def only_run(project, **match):
    """The single run in the store, or the single one matching `match`."""
    runs = [
        run
        for run in state.load(project)["runs"].values()
        if all(run.get(key) == value for key, value in match.items())
    ]
    assert len(runs) == 1, [r["id"] for r in runs]
    return runs[0]


def prepared(project, task_id="M01-001", role="agent"):
    """A claimed run, exactly as the scheduler leaves one before a worker starts."""
    run_id, _, _, _ = runner.prepare(
        Path(project),
        task_id,
        f"{sys.executable} -c pass",
        [],
        model=None,
        timeout=None,
        cwd=None,
        force=True,
        role=role,
    )
    return run_id


# --------------------------------------------------------------------------
# 1. a worker that raises leaves nothing stranded


def test_a_worker_exception_reconciles_the_prepared_run(planned, project, monkeypatch):
    """The bug: an exception left the task claimed by a process that was gone.

    `prepare` marks the task `running`. If the worker then raises, the old code
    returned an error string and touched nothing else: the run still said
    `running`, so the scheduler would not reselect the task, and the pid was
    gone, so nothing would ever finish it.
    """
    run_id = prepared(project)
    assert state.load(project)["tasks"]["M01-001"]["status"] == "running"

    def explode(*args, **kwargs):
        raise RuntimeError("the provider fell over")

    monkeypatch.setattr(runner, "execute", explode)
    job = orchestrator.Job(task_id="M01-001", role="agent")
    outcome = orchestrator._execute(Path(project), job, run_id)

    assert "the provider fell over" in (outcome.error or "")
    data = state.load(project)
    run = data["runs"][run_id]
    assert run["status"] == "failed", "the run still looks active"
    assert run["finished_at"]
    assert run["failure"]["category"] == failures.INTERNAL
    assert run["failure"]["exception"] == "RuntimeError"
    assert run["failure"]["where"], "no traceback location was kept"
    assert data["tasks"]["M01-001"]["status"] == "planned", "the task is stranded"


def test_a_worker_exception_on_a_review_returns_it_to_awaiting_review(
    planned, writ, project, monkeypatch
):
    """The implementation still stands; only the judgement was lost."""
    writ("dispatch", "M01-001", "--agent", agent(IMPLEMENTER))
    with state.transaction(project) as data:
        data["tasks"]["M01-001"]["status"] = "awaiting-review"
    run_id = prepared(project, role="reviewer")
    assert state.load(project)["tasks"]["M01-001"]["status"] == "reviewing"

    monkeypatch.setattr(
        runner, "execute", lambda *a, **k: (_ for _ in ()).throw(OSError("i/o"))
    )
    outcome = orchestrator._execute(
        Path(project), orchestrator.Job(task_id="M01-001", role="reviewer"), run_id
    )

    data = state.load(project)
    assert data["runs"][run_id]["status"] in ("failed", "interrupted")
    assert data["tasks"]["M01-001"]["status"] == "awaiting-review"
    assert outcome.category == failures.INFRASTRUCTURE


def test_reconciling_an_already_finished_run_keeps_its_recorded_outcome(
    planned, writ, project
):
    """A worker can raise *after* the run was recorded. That record is the true one."""
    writ("dispatch", "M01-001", "--agent", agent(IMPLEMENTER))
    run = only_run(project)
    settlement = runner.reconcile(
        Path(project),
        run["id"],
        failures.Failure(category=failures.INTERNAL, reason="raised on the way out"),
    )
    assert settlement.settled is False
    after = state.load(project)["runs"][run["id"]]
    assert after["status"] == "completed", "a recorded outcome was overwritten"
    assert after["failure"]["reason"] == "raised on the way out", "and not recorded"


def test_a_worker_exception_reopens_the_repair_request(approved, writ, project):
    """A repair planner holds no task, but it does move its request to `planning`."""
    from writ import repair

    with state.transaction(project) as data:
        gate_id = next(
            task_id
            for task_id, task in data["tasks"].items()
            if task.get("kind") == "gate"
        )
        request = repair.open_request(
            data,
            gate_id=gate_id,
            finding_ids=[],
            summary="the acceptance criterion is untestable",
            actor="gate(test)",
        )
        request_id = request["id"]
        data["tasks"][gate_id]["status"] = "blocked"
        data["tasks"][gate_id]["held"] = {
            "reason": "awaiting-repair",
            "request": request_id,
        }
    run_id = prepared(project, task_id=gate_id, role="repair")
    assert repair.request_for_gate(state.load(project), gate_id)["status"] == "planning"

    runner.reconcile(
        Path(project),
        run_id,
        failures.Failure(
            category=failures.INFRASTRUCTURE,
            reason="provider timed out",
            retryable=True,
        ),
    )
    request = repair.request_for_gate(state.load(project), gate_id)
    assert request["status"] == "open", "the request is stuck in planning"


def test_a_spawn_failure_settles_the_run_and_the_task(planned, writ, project):
    """The whole lifecycle, through the CLI, with an agent that cannot be spawned."""
    code, _, err = writ("dispatch", "M01-001", "--agent", "writ-no-such-agent-binary")
    assert code == 2 and "not found" in err
    run = only_run(project)
    assert run["status"] == "failed" and run["exit_code"] == 127
    # nothing ran, so nothing failed on its merits: the task waits, unspent
    task = state.load(project)["tasks"]["M01-001"]
    assert task["status"] == "planned"
    assert run["failure"]["category"] == "unavailable"
    assert not task.get("rework")


def test_a_single_shot_dispatch_settles_its_run_when_the_agent_raises(
    planned, writ, project, monkeypatch
):
    """`writ dispatch` claims the task too, so it needs the same guarantee.

    Without it the task is claimed by a process that is about to exit, invisible
    to every queue until somebody happens to run writ again.
    """
    real = runner.execute

    def explode(root, run_id, **kwargs):
        raise OSError(12, "cannot allocate memory")

    monkeypatch.setattr(runner, "execute", explode)
    # The exception still escapes to the caller — how writ reports an unexpected
    # error is unchanged. What the guard adds is that the store is settled first.
    with pytest.raises(OSError):
        writ("dispatch", "M01-001", "--agent", agent(IMPLEMENTER))

    monkeypatch.setattr(runner, "execute", real)
    data = state.load(project)
    run = only_run(project)
    assert run["status"] == "interrupted", "the run still looks active"
    assert run["failure"]["category"] == failures.INFRASTRUCTURE
    assert data["tasks"]["M01-001"]["status"] == "planned", "the task is stranded"


def test_the_fallback_lock_heartbeats_while_it_is_held(project, monkeypatch):
    """Age has to measure abandonment, not duration.

    The heartbeat is what makes the fallback's last-resort timeout safe: a slow
    transaction keeps saying so, and only a lock nothing has touched for the whole
    timeout is treated as debris.
    """
    state.initialize(project)
    monkeypatch.setattr(state, "fcntl", None)
    monkeypatch.setattr(state, "LOCK_HEARTBEAT_SECONDS", 0.05)
    path = state.store_dir(project) / state.LOCK_FILENAME
    with state.transaction(project):
        first = path.stat().st_mtime
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if path.stat().st_mtime > first:
                break
            time.sleep(0.05)
        assert path.stat().st_mtime > first, "the lock never refreshed itself"


def test_a_stranded_run_is_still_recoverable_when_reconciling_cannot_write(
    planned, project, monkeypatch
):
    """If the store is what broke, the failure is reported rather than raised.

    Losing the other agents in flight to a store that is briefly unavailable
    would turn one stranded run into a lost session. `reap` remains the backstop.
    """
    run_id = prepared(project)
    monkeypatch.setattr(
        runner, "execute", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    monkeypatch.setattr(
        runner,
        "reconcile",
        lambda *a, **k: (_ for _ in ()).throw(WritError("the lock is gone")),
    )
    outcome = orchestrator._execute(
        Path(project), orchestrator.Job(task_id="M01-001", role="agent"), run_id
    )
    assert "boom" in (outcome.error or "")
    assert "could not record it" in (outcome.error or "")

    # Nothing was recorded, so the run is exactly as stranded as before the fix:
    # still `starting`, its task still `running`. That is what `reap` is for, and
    # the guarantee is that the run is recoverable rather than invisible. Here the
    # claiming process is this very test, so the owner is stubbed out to stand for
    # a scheduler that has since exited.
    data = state.load(project)
    assert data["runs"][run_id]["status"] in runner.ACTIVE_RUN_STATUSES
    with state.transaction(project) as data:
        data["runs"][run_id]["owner"] = {"pid": 2 ** 22, "host": procs.hostname()}
        data["runs"][run_id].pop("owner_pid", None)
    assert runner.reap(Path(project)) == [run_id]
    assert state.load(project)["tasks"]["M01-001"]["status"] == "planned"


# --------------------------------------------------------------------------
# 2. state writes are durable


def test_a_committed_write_is_flushed(project, monkeypatch):
    synced: list[str] = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(fd) or real(fd))
    state.initialize(project)
    with state.transaction(project) as data:
        data["counters"]["decision"] = 1
    assert len(synced) >= 2, "the file and its directory must both be flushed"


def test_fsync_can_be_turned_off_explicitly(project, monkeypatch):
    monkeypatch.setenv(state.FSYNC_ENV, "0")
    synced: list[int] = []
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(fd))
    state.initialize(project)
    with state.transaction(project) as data:
        data["counters"]["decision"] = 1
    assert synced == []
    assert state.load(project)["counters"]["decision"] == 1


def test_a_write_that_fails_before_the_replace_keeps_the_old_document(
    project, monkeypatch
):
    """Better an old state than a truncated one."""
    state.initialize(project)
    with state.transaction(project) as data:
        data["counters"]["decision"] = 5

    def fail(self, target):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError):
        with state.transaction(project) as data:
            data["counters"]["decision"] = 6
    assert state.load(project)["counters"]["decision"] == 5
    assert list(state.store_dir(project).glob("state.json.tmp.*")) == [], (
        "the failed write left its temporary file behind"
    )


def test_a_write_that_fails_after_the_replace_is_committed(project, monkeypatch):
    """The replace *is* the commit point; a failure after it changes nothing."""
    state.initialize(project)
    monkeypatch.setattr(state, "_fsync_dir", lambda directory: None)
    with state.transaction(project) as data:
        data["counters"]["decision"] = 9
    assert state.load(project)["counters"]["decision"] == 9


def test_orphaned_temporary_files_are_swept_at_startup(project):
    state.initialize(project)
    debris = state.store_dir(project) / f"{state.STATE_FILENAME}.tmp.999999.1"
    debris.write_text("{half written", encoding="utf-8")
    mine = state.store_dir(project) / f"{state.STATE_FILENAME}.tmp.{os.getpid()}.1"
    mine.write_text("{in progress", encoding="utf-8")

    removed = state.sweep_temporaries(project)

    assert debris in removed and not debris.exists()
    assert mine.exists(), "a live writer's work in progress was deleted"
    mine.unlink()


def test_reap_sweeps_temporaries_too(planned, project):
    debris = state.store_dir(project) / f"{state.STATE_FILENAME}.tmp.999998.1"
    debris.write_text("{", encoding="utf-8")
    runner.reap(Path(project))
    assert not debris.exists()


# --------------------------------------------------------------------------
# 3. process identity


def test_a_recycled_pid_is_not_the_recorded_owner():
    """The core of the fix: same pid, different process, different identity."""
    mine = procs.identify()
    assert procs.alive(mine) and not procs.confirmed_dead(mine)

    imposter = procs.Identity(
        pid=mine.pid,
        start_time=(mine.start_time or 0.0) - 3600,
        host=mine.host,
        token="someone-else",
    )
    if mine.start_time is None:  # pragma: no cover - platform without start times
        pytest.skip("this platform does not report process start times")
    assert not procs.alive(imposter), "a recycled pid passed as the owner"
    assert procs.confirmed_dead(imposter)
    assert procs.safe_to_signal(imposter) is None, "writ would have killed a stranger"


def test_a_bare_pid_from_an_older_writ_still_works():
    """An in-flight project must keep running across the upgrade."""
    assert procs.alive(os.getpid())
    assert not procs.alive(2 ** 22)
    assert procs.confirmed_dead(2 ** 22)
    assert procs.normalize(f"{os.getpid()} 2026-01-01T00:00:00+00:00").pid == os.getpid()
    assert procs.normalize(None) is None
    assert procs.normalize("not a pid") is None


def test_an_owner_on_another_host_is_never_reaped():
    """This machine has no standing to declare a remote process dead."""
    elsewhere = {"pid": os.getpid(), "start_time": 1.0, "host": "some-other-box"}
    assert procs.alive(elsewhere), "a remote run was treated as dead"
    assert not procs.confirmed_dead(elsewhere)
    assert procs.safe_to_signal(elsewhere) is None


def test_reap_leaves_a_run_whose_pid_was_recycled_alone(planned, writ, project):
    """A live process at a recorded pid is only the owner if its identity matches.

    The mirror of the test below: here the pid *is* alive, and the question is
    whether writ checks anything else before believing it.
    """
    writ("dispatch", "M01-001", "--agent", agent(IMPLEMENTER))
    run_id = only_run(project)["id"]
    with state.transaction(project) as data:
        run = data["runs"][run_id]
        run["status"] = "running"
        run.pop("supervisor", None)
        run.pop("supervisor_pid", None)
        # alive, but started an hour before the recorded identity says
        run["identity"] = {
            "pid": os.getpid(),
            "start_time": (procs.start_time(os.getpid()) or 0.0) - 3600,
            "host": procs.hostname(),
            "token": "gone",
        }
        data["tasks"]["M01-001"]["status"] = "running"
    if procs.start_time(os.getpid()) is None:  # pragma: no cover
        pytest.skip("this platform does not report process start times")

    assert runner.reap(Path(project)) == [run_id], "a recycled pid blocked the reap"
    data = state.load(project)
    assert data["runs"][run_id]["status"] == "interrupted"
    assert data["tasks"]["M01-001"]["status"] == "planned"


def test_reap_leaves_a_live_run_alone(planned, writ, project):
    """The other direction: a genuinely live owner must survive a reap."""
    writ("dispatch", "M01-001", "--agent", agent(IMPLEMENTER))
    run_id = only_run(project)["id"]
    with state.transaction(project) as data:
        run = data["runs"][run_id]
        run["status"] = "running"
        run["identity"] = procs.identify().to_dict()
        data["tasks"]["M01-001"]["status"] = "running"
    assert runner.reap(Path(project)) == []
    assert state.load(project)["tasks"]["M01-001"]["status"] == "running"


def test_cancel_will_not_signal_a_recycled_pid(planned, writ, project, monkeypatch):
    writ("dispatch", "M01-001", "--agent", agent(IMPLEMENTER))
    run_id = only_run(project)["id"]
    with state.transaction(project) as data:
        run = data["runs"][run_id]
        run["status"] = "running"
        run.pop("supervisor", None)
        run.pop("supervisor_pid", None)
        run["identity"] = {
            "pid": os.getpid(),
            "start_time": (procs.start_time(os.getpid()) or 0.0) - 3600,
            "host": procs.hostname(),
            "token": "gone",
        }
        data["tasks"]["M01-001"]["status"] = "running"
    if procs.start_time(os.getpid()) is None:  # pragma: no cover
        pytest.skip("this platform does not report process start times")
    killed: list[int] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: killed.append(pgid))

    runner.cancel(Path(project), run_id)

    assert killed == [], "writ signalled a process group that was not its own"
    # the run is still settled, which is the half that must happen either way
    data = state.load(project)
    assert data["runs"][run_id]["status"] == "cancelled"
    assert data["tasks"]["M01-001"]["status"] == "planned"


def test_a_live_lock_is_never_stolen_however_slow_its_holder(project, monkeypatch):
    """The data-loss bug: a transaction slower than the timeout lost its lock.

    Under `flock` there is no age at which a held lock can be taken, which is
    what this asserts. The old code unlinked it after 60 seconds and let a second
    writer in while the first was still inside the critical section.
    """
    state.initialize(project)
    monkeypatch.setattr(state, "LOCK_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(state, "LOCK_STALE_SECONDS", -1.0)  # "always stale"
    with state.transaction(project) as data:
        data["counters"]["decision"] = 1
        with pytest.raises(WritError, match="timed out waiting for the state lock"):
            with state.transaction(project) as inner:  # pragma: no cover - must raise
                inner["counters"]["decision"] = 99
    assert state.load(project)["counters"]["decision"] == 1


def test_a_lock_names_its_holder(project):
    state.initialize(project)
    path = state.store_dir(project) / state.LOCK_FILENAME
    with state.transaction(project):
        owner = state.lock_owner(path)
        assert owner is not None and owner.pid == os.getpid()


def test_a_lock_left_by_a_dead_process_is_broken_at_once(project, monkeypatch):
    """Proof of death beats waiting out a timeout — for the fallback path."""
    state.initialize(project)
    monkeypatch.setattr(state, "fcntl", None)
    path = state.store_dir(project) / state.LOCK_FILENAME
    path.write_text(
        json.dumps({"pid": 2 ** 22, "host": procs.hostname(), "token": "x"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(state, "LOCK_TIMEOUT_SECONDS", 0.2)
    with state.transaction(project) as data:
        data["counters"]["decision"] = 3
    assert state.load(project)["counters"]["decision"] == 3


def test_the_fallback_lock_is_not_released_by_a_process_that_lost_it(
    project, monkeypatch
):
    """Our lock was broken and another process holds it. We must not unlink it."""
    state.initialize(project)
    monkeypatch.setattr(state, "fcntl", None)
    path = state.store_dir(project) / state.LOCK_FILENAME
    mine = procs.identify()
    path.write_text(json.dumps({**mine.to_dict()}) + "\n", encoding="utf-8")
    successor = procs.identify()
    path.write_text(json.dumps({**successor.to_dict()}) + "\n", encoding="utf-8")

    state._release(path, mine)

    assert path.exists(), "a lock belonging to another holder was released"


def test_concurrent_session_claims_leave_exactly_one_winner(planned, project):
    """The TOCTOU: both processes read a dead pid, both wrote themselves in.

    Threads rather than processes, because the race is on the file and threads
    make it reproducible. `claim_session` creates the file exclusively, so one
    thread has to lose.
    """
    root = Path(project)
    orchestrator.release_session(root)
    ready = threading.Barrier(8)
    wins: list[int] = []
    losses: list[str] = []
    lock = threading.Lock()

    def claim(index: int) -> None:
        ready.wait()
        try:
            orchestrator.claim_session(root)
        except WritError as exc:
            with lock:
                losses.append(str(exc))
        else:
            with lock:
                wins.append(index)

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(wins) == 1, f"{len(wins)} schedulers both believed they owned the project"
    assert len(losses) == 7
    assert all("another writ run is active" in message for message in losses)
    orchestrator.release_session(root)


def test_release_does_not_delete_another_process_claim(planned, project):
    """After a `--force` start, the loser's release must not remove the winner's."""
    root = Path(project)
    orchestrator.release_session(root)
    orchestrator.claim_session(root)
    stolen = orchestrator._session_claim
    # somebody else forces their way in
    path = state.store_dir(root) / "run.session"
    path.write_text(
        json.dumps(procs.identify().to_dict()) + "\n", encoding="utf-8"
    )

    orchestrator._session_claim = stolen
    orchestrator.release_session(root)

    assert path.exists(), "released a live session's claim"
    path.unlink()


def test_a_session_claimed_by_a_recycled_pid_does_not_block_a_start(planned, project):
    root = Path(project)
    path = state.store_dir(root) / "run.session"
    path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "start_time": (procs.start_time(os.getpid()) or 0.0) - 3600,
                "host": procs.hostname(),
                "token": "gone",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    if procs.start_time(os.getpid()) is None:  # pragma: no cover
        pytest.skip("this platform does not report process start times")
    assert orchestrator.active_session(root) is None
    orchestrator.claim_session(root)  # must not raise
    orchestrator.release_session(root)


def test_a_live_session_still_blocks_a_second_run(planned, project):
    root = Path(project)
    orchestrator.claim_session(root)
    try:
        with pytest.raises(WritError, match="another writ run is active"):
            orchestrator.claim_session(root)
    finally:
        orchestrator.release_session(root)


# --------------------------------------------------------------------------
# 4. infrastructure failures are not task failures


def test_a_provider_timeout_is_classified_as_retryable_infrastructure():
    failure = failures.classify(subprocess.TimeoutExpired(cmd=["agent"], timeout=30))
    assert failure.category == failures.INFRASTRUCTURE
    assert failure.retryable
    assert not failure.on_merit, "a timeout would be reported as a rejection"


def test_a_missing_agent_is_infrastructure_but_not_retryable():
    failure = failures.classify(FileNotFoundError("no such file: pi"))
    assert failure.category == failures.UNAVAILABLE
    assert not failure.retryable, "retrying cannot conjure a binary"
    assert not failure.on_merit


def test_a_state_lock_timeout_is_retryable():
    failure = failures.classify(
        WritError("timed out waiting for the state lock at /x; held by pid 12")
    )
    assert failure.retryable and failure.category == failures.INFRASTRUCTURE


def test_an_ordinary_writ_error_is_a_task_failure():
    failure = failures.classify(WritError("M01-002 is blocked by incomplete deps"))
    assert failure.category == failures.TASK
    assert not failure.retryable
    assert failure.on_merit


def test_an_unknown_exception_is_internal_and_not_retried():
    failure = failures.classify(ValueError("a bug in writ"))
    assert failure.category == failures.INTERNAL
    assert not failure.retryable, "a deterministic bug would be retried forever"


def test_transient_os_errors_are_retryable_and_others_are_not():
    import errno

    transient = OSError(errno.ENOMEM, "cannot allocate memory")
    assert failures.classify(transient).retryable
    permanent = OSError(errno.ENOTDIR, "not a directory")
    assert not failures.classify(permanent).retryable


def test_backoff_grows_and_is_jittered_within_the_cap():
    first = [failures.backoff(1) for _ in range(40)]
    second = [failures.backoff(2) for _ in range(40)]
    assert all(1.4 <= value <= 2.0 for value in first), (min(first), max(first))
    assert all(4.4 <= value <= 6.0 for value in second), (min(second), max(second))
    assert len(set(round(value, 6) for value in first)) > 1, "no jitter at all"
    assert failures.backoff(50) <= failures.BACKOFF_CAP_SECONDS


def test_an_infrastructure_failure_does_not_spend_a_rework_attempt(
    planned, project, monkeypatch
):
    """The signal corruption the review names: rework is for judged work only."""
    run_id = prepared(project)
    monkeypatch.setattr(
        runner,
        "execute",
        lambda *a, **k: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd=["agent"], timeout=1)
        ),
    )
    orchestrator._execute(
        Path(project), orchestrator.Job(task_id="M01-001", role="agent"), run_id
    )
    task = state.load(project)["tasks"]["M01-001"]
    assert not task.get("rework"), "an outage spent the task's rework budget"
    assert runner.infrastructure_attempts(task) == 1
    attempt = task["infrastructure"]["attempts"][0]
    assert attempt["category"] == failures.INFRASTRUCTURE
    assert attempt["run"] == run_id
    assert attempt["key"], "no idempotency key was recorded"


def test_the_infrastructure_budget_is_durable(planned, project, monkeypatch):
    """Counted in the store, so a resumed session does not hand out a fresh set."""
    monkeypatch.setattr(
        runner,
        "execute",
        lambda *a, **k: (_ for _ in ()).throw(OSError(12, "cannot allocate memory")),
    )
    job = orchestrator.Job(task_id="M01-001", role="agent")
    seen = []
    for _ in range(3):
        run_id = prepared(project)
        seen.append(orchestrator._execute(Path(project), job, run_id).infra_attempt)
    assert seen == [1, 2, 3]


def test_a_retry_after_an_infrastructure_failure_completes_the_task(
    planned, project, monkeypatch
):
    """End to end through the scheduler: the first attempt raises, the retry works.

    The failure is injected as an exception because that is what this fix governs.
    An agent that *exits* non-zero has run and reported for itself, and writ still
    reads its transcript and its verdict rather than second-guessing the exit code —
    the classification here is for the ways a job dies without ever reporting.
    """
    real = runner.execute
    calls: list[str] = []

    def flaky(root, run_id, **kwargs):
        calls.append(run_id)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd=["agent"], timeout=1)
        return real(root, run_id, **kwargs)

    monkeypatch.setattr(runner, "execute", flaky)
    monkeypatch.setattr(failures, "backoff", lambda attempt: 0.01)

    session = orchestrator.run(
        Path(project),
        agent=agent(IMPLEMENTER),
        model=None,
        reviewer=agent(REVIEWER),
        max_tasks=1,
    )

    assert session.infra_retries == ["M01-001"]
    assert session.infra_blocked == [], "a successful retry was reported as blocked"
    assert session.completed == ["M01-001"]
    data = state.load(project)
    assert data["tasks"]["M01-001"]["status"] == "completed"
    assert len(calls) >= 2, "the job was never retried"
    # both halves: the abandoned run is settled, and it is settled as an
    # interruption rather than as a failure, because it may yet have succeeded.
    first = data["runs"][calls[0]]
    assert first["status"] == "interrupted"
    assert first["failure"]["category"] == failures.INFRASTRUCTURE
    assert first["failure"]["retryable"] is True


def test_a_task_out_of_infrastructure_retries_is_not_reported_as_failed(
    planned, project, monkeypatch
):
    """A broken provider must not read as an implementation that was rejected."""
    monkeypatch.setattr(
        runner,
        "execute",
        lambda *a, **k: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd=["agent"], timeout=1)
        ),
    )
    monkeypatch.setattr(failures, "backoff", lambda attempt: 0.01)
    session = orchestrator.run(
        Path(project),
        agent=f"{sys.executable} -c pass",
        model=None,
        max_tasks=1,
        max_infra_retries=1,
    )
    assert session.infra_retries == ["M01-001"]
    assert session.infra_blocked == ["M01-001"]
    assert session.failed == [], "an outage was reported as failed work"
    assert session.completed == []
    task = state.load(project)["tasks"]["M01-001"]
    assert task["infrastructure"]["exhausted"] is True
    assert task["status"] == "planned", "the task is still to be attempted"
    assert any(
        "not rejected on technical merit" in item["text"]
        for item in task["evidence"]
    )
    lines = orchestrator.summary(state.load(project), session)
    assert any("out of infrastructure retries" in line for line in lines)


def test_retries_can_be_turned_off(planned, project, monkeypatch):
    monkeypatch.setattr(
        runner,
        "execute",
        lambda *a, **k: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd=["agent"], timeout=1)
        ),
    )
    session = orchestrator.run(
        Path(project),
        agent=f"{sys.executable} -c pass",
        model=None,
        max_tasks=1,
        max_infra_retries=0,
    )
    assert session.infra_retries == []
    assert session.infra_blocked == ["M01-001"]


def test_a_reviewer_rejection_still_spends_rework(planned, writ, project):
    """The control: the budget that should be spent still is."""
    writ(
        "run",
        "--agent",
        agent(IMPLEMENTER),
        "--reviewer",
        agent(REJECTOR),
        "--max-tasks",
        "1",
    )
    task = state.load(project)["tasks"]["M01-001"]
    assert task.get("rework", {}).get("attempt"), "a rejection spent no rework"
    assert runner.infrastructure_attempts(task) == 0


# --------------------------------------------------------------------------
# 4b. the two infrastructure failures that never raise
#
# `classify` only ever sees an exception, and the two most common infrastructure
# failures do not produce one: writ kills a hung agent itself and calls it exit
# 124, and an agent that cannot reach a model exits 0 having printed nothing.
# Both used to arrive as an ordinary unjudged run and land the task at `failed` —
# a status that says a reviewer read this work and rejected it.


def test_a_timeout_is_classified_from_the_finished_run():
    failure = failures.from_run({"exit_code": 124, "timeout": 30})
    assert failure is not None
    assert failure.category == failures.INFRASTRUCTURE
    assert failure.retryable is True
    assert "30s" in failure.reason


def test_an_agent_that_never_reached_a_model_is_unavailable_not_retried():
    failure = failures.from_run({"exit_code": 0, "no_output": True})
    assert failure is not None
    assert failure.category == failures.UNAVAILABLE
    assert failure.retryable is False, "another attempt cannot fix a wrong model id"


def test_a_run_that_reported_is_never_reclassified():
    """The guard. An agent that judged the work has spoken for itself."""
    assert failures.from_run({"exit_code": 124, "verdict": {"outcome": "pass"}}) is None
    assert failures.from_run({"exit_code": 3}) is None
    assert failures.from_run({"exit_code": 0}) is None


def test_a_timed_out_agent_returns_its_task_to_the_queue(planned, writ, project):
    """The fix, through the real path: writ's own kill, not an injected exception.

    Both halves. The run says it failed and why; the task says it is still to be
    attempted — not `failed`, which is the word for work a reviewer rejected.
    """
    sleeper = agent("import sys, time\nsys.stdin.read()\nprint('working')\ntime.sleep(60)\n")
    code, _, _ = writ("dispatch", "M01-001", "--agent", sleeper, "--timeout", "1")
    assert code == 124
    data = state.load(project)
    run = only_run(project)
    assert run["exit_code"] == 124 and run["status"] == "failed"
    assert run["failure"]["category"] == failures.INFRASTRUCTURE
    assert run["failure"]["retryable"] is True
    assert run["resulting_status"] == "planned"
    task = data["tasks"]["M01-001"]
    assert task["status"] == "planned", "a killed hang was reported as failed work"
    assert task.get("rework", {}).get("attempt") is None, "a hang spent rework"
    assert runner.infrastructure_attempts(task) == 1


def test_a_timed_out_review_holds_at_awaiting_review(planned, writ, project):
    """A lost review costs the judgement, not the implementation."""
    writ("run", "--max-tasks", "1", "--agent", agent(IMPLEMENTER), "--reviewer", "true")
    with state.transaction(Path(project)) as data:
        data["tasks"]["M01-001"]["status"] = "awaiting-review"
    sleeper = agent("import sys, time\nsys.stdin.read()\nprint('reading')\ntime.sleep(60)\n")
    writ("review", "M01-001", "--agent", sleeper, "--timeout", "1")
    runs = [
        r
        for r in state.load(project)["runs"].values()
        if r["role"] == "reviewer" and r.get("exit_code") == 124
    ]
    assert len(runs) == 1
    run = runs[0]
    assert run["failure"]["category"] == failures.INFRASTRUCTURE
    assert run["resulting_status"] == "awaiting-review"
    assert state.load(project)["tasks"]["M01-001"]["status"] == "awaiting-review"


def test_an_agent_that_printed_nothing_leaves_its_task_selectable(
    planned, writ, project
):
    """A wrong model id or a dead credential, which is what this looks like.

    Not retried — the next attempt finds the same wrong model id — but not
    reported as failed work either, and left in the status it was claimed from so
    that fixing the configuration is the only thing the operator has to do.
    """
    writ("dispatch", "M01-001", "--agent", "true")
    run = only_run(project)
    assert run["no_output"] is True
    assert run["failure"]["category"] == failures.UNAVAILABLE
    assert run["failure"]["retryable"] is False
    task = state.load(project)["tasks"]["M01-001"]
    assert task["status"] == "planned"
    assert runner.infrastructure_attempts(task) == 0, "a dead credential took a retry"


def test_a_timeout_under_the_scheduler_spends_the_infrastructure_budget(
    planned, project
):
    """End to end: the scheduler retries a hang rather than rejecting the task."""
    sleeper = agent("import sys, time\nsys.stdin.read()\ntime.sleep(60)\n")
    session = orchestrator.run(
        Path(project),
        agent=sleeper,
        model=None,
        max_tasks=1,
        timeout=1,
        max_infra_retries=1,
    )
    assert session.infra_retries == ["M01-001"]
    assert session.failed == [], "a hang was reported as failed work"
    task = state.load(project)["tasks"]["M01-001"]
    assert task["status"] == "planned"
    assert task.get("rework", {}).get("attempt") is None
    # two runs, both classified, and the budget spent rather than the rework one
    runs = [r for r in state.load(project)["runs"].values() if r["role"] == "agent"]
    assert len(runs) == 2
    assert all(r["failure"]["category"] == failures.INFRASTRUCTURE for r in runs)
    assert task["infrastructure"]["exhausted"] is True
    lines = orchestrator.summary(state.load(project), session)
    assert any("infrastructure" in line for line in lines)
