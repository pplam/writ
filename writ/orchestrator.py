"""Drive the whole DAG: dispatch, review, repeat, until the work runs out.

`writ dispatch` and `writ review` each move one task one step. This walks the
graph on its own, which needs three things the single-shot commands do not.

**Both phases, not just dispatch.** A task an agent has implemented sits at
`awaiting-review`, and a dependency only unblocks when it reaches `completed`.
Completion comes from a reviewer. So a loop that only dispatched would stall
after the first level of the graph, having done work that never counts.

**One selector.** Two threads that both ask "what is ready?" can pick the same
task. Selection and claiming happen on the scheduler thread alone, and the claim
is `runner.prepare` marking the task `running` — so by the time a worker starts,
the task is no longer selectable. Workers only run agents and record verdicts.
Between processes the same problem returns, so a session record with a live pid
refuses a second concurrent `writ run` rather than racing it.

**Resumability.** Nothing is held in memory that matters. The store already knows
every task's status and every run's outcome, so resuming is re-deriving: reap the
runs whose process died, then carry on. An interrupted agent costs its own work,
never the session's.
"""
from __future__ import annotations

import os
import signal
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from . import agents, runner, state
from .model import effective_status
from .state import WritError, utcnow

#: how long a worker may hold the scheduler open after a stop is requested
GRACE_SECONDS = 5.0


@dataclass
class Job:
    """One agent invocation the scheduler decided to make."""

    task_id: str
    role: str  # "agent" | "reviewer"

    @property
    def verb(self) -> str:
        return "review" if self.role == "reviewer" else "dispatch"


@dataclass
class Outcome:
    """What a job produced, as recorded in the store."""

    job: Job
    run_id: str | None = None
    exit_code: int | None = None
    status: str | None = None  # the task's status afterwards
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.exit_code == 0


@dataclass
class Session:
    """The tally for one `writ run`."""

    dispatched: list[str] = field(default_factory=list)
    reviewed: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    reaped: list[str] = field(default_factory=list)
    #: tasks whose review was attempted, whether or not an agent ran. Kept apart
    #: from `reviewed` so a review that could not start is not counted as one,
    #: while still being remembered as tried.
    review_attempts: list[str] = field(default_factory=list)
    stopped: bool = False
    aborted: bool = False
    started_at: str = field(default_factory=utcnow)
    finished_at: str | None = None

    @property
    def agent_runs(self) -> int:
        return len(self.dispatched) + len(self.reviewed)


class Stop(Exception):
    """Raised inside the scheduler when the operator asks it to stop."""


# --------------------------------------------------------------------------
# session ownership


def _session_path(root: Path) -> Path:
    return state.store_dir(root) / "run.session"


def claim_session(root: Path, *, force: bool = False) -> None:
    """Refuse to start while another `writ run` owns this project.

    Two schedulers would each believe they were the only selector, which is
    exactly the double-dispatch this design avoids within one process.
    """
    path = _session_path(root)
    if path.exists():
        try:
            existing = int(path.read_text(encoding="utf-8").split()[0])
        except (ValueError, IndexError, OSError):
            existing = None
        if existing and runner.process_alive(existing) and not force:
            raise WritError(
                f"another writ run is active (pid {existing}). Wait for it, stop "
                f"it, or pass --force if you know it is gone."
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()} {utcnow()}\n", encoding="utf-8")


def release_session(root: Path) -> None:
    try:
        _session_path(root).unlink()
    except FileNotFoundError:
        pass


def active_session(root: Path) -> int | None:
    """The pid of a live `writ run`, if there is one."""
    path = _session_path(root)
    if not path.exists():
        return None
    try:
        pid = int(path.read_text(encoding="utf-8").split()[0])
    except (ValueError, IndexError, OSError):
        return None
    return pid if runner.process_alive(pid) else None


# --------------------------------------------------------------------------
# what to do next


def next_job(
    data: dict[str, Any],
    *,
    busy: Iterable[str],
    budget: int | None,
    started: Iterable[str],
    reviewed: Iterable[str] = (),
) -> Job | None:
    """Choose the next agent invocation, or None when there is nothing to do.

    Reviews come first. A review is the only thing that can complete a task, and
    a completed task is the only thing that unblocks its dependents, so finishing
    work in flight opens more of the graph than starting more work does.

    Each task is attempted once per session in each role. A reviewer that writes
    no verdict leaves its task at `awaiting-review` — the same state that
    selected it — so without this the scheduler would re-review it forever.
    """
    busy = set(busy)
    started = set(started)
    reviewed = set(reviewed)

    for task in sorted(data["tasks"].values(), key=lambda item: item["id"]):
        if task["id"] in busy or task["id"] in reviewed:
            continue
        if task["status"] == "awaiting-review":
            return Job(task_id=task["id"], role="reviewer")

    if budget is not None and len(started) >= budget:
        return None

    for task in sorted(data["tasks"].values(), key=lambda item: item["id"]):
        if task["id"] in busy or task["id"] in started:
            continue
        if effective_status(data, task) != "ready":
            continue
        return Job(task_id=task["id"], role="agent")
    return None


def preview(data: dict[str, Any], *, budget: int | None) -> list[Job]:
    """The jobs a session would run, assuming everything passes.

    A projection, not a promise: a rejected verdict changes what comes next. It
    exists so `--dry-run` can show the intended walk before any tokens are spent.
    """
    simulated = {
        task_id: dict(task, status=task["status"])
        for task_id, task in data["tasks"].items()
    }
    shadow = dict(data, tasks=simulated)
    jobs: list[Job] = []
    started: list[str] = []
    guard = 0
    limit = 4 * len(simulated) + 8
    while guard < limit:
        guard += 1
        job = next_job(shadow, busy=[], budget=budget, started=started)
        if job is None:
            break
        jobs.append(job)
        task = simulated[job.task_id]
        if job.role == "agent":
            started.append(job.task_id)
            task["status"] = "awaiting-review"
        else:
            task["status"] = "completed"
    return jobs


# --------------------------------------------------------------------------
# the scheduler


def run(
    root: Path,
    *,
    agent: str,
    model: str | None,
    reviewer: str | None = None,
    reviewer_model: str | None = None,
    parallel: int = 1,
    max_tasks: int | None = None,
    timeout: int | None = None,
    cwd: str | None = None,
    agent_args: list[str] | None = None,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> Session:
    """Walk the DAG until it runs out of work, an error stops it, or you do."""
    session = Session()
    emit = on_event or (lambda name, payload: None)

    # Idempotent: the CLI reaps first so it can report the resume, and reaping
    # again here finds nothing. Kept so a direct caller of `run` still recovers
    # a killed session rather than tripping over its leftovers.
    session.reaped = runner.reap(root)
    if session.reaped:
        emit("reaped", {"runs": session.reaped})

    stop = threading.Event()
    abort = threading.Event()
    _install_signals(stop, abort, emit)

    in_flight: dict[Future[Outcome], Job] = {}
    started: list[str] = []
    pool = ThreadPoolExecutor(max_workers=max(1, parallel))
    try:
        while True:
            while not stop.is_set() and len(in_flight) < max(1, parallel):
                data = state.load(root)
                job = next_job(
                    data,
                    busy=[j.task_id for j in in_flight.values()],
                    budget=max_tasks,
                    started=started,
                    reviewed=session.review_attempts,
                )
                if job is None:
                    break
                try:
                    prepared = _prepare(
                        root,
                        job,
                        agent=agent,
                        model=model,
                        reviewer=reviewer,
                        reviewer_model=reviewer_model,
                        timeout=timeout,
                        cwd=cwd,
                        agent_args=agent_args or [],
                    )
                except WritError as exc:
                    # A job that cannot even be started is recorded and skipped,
                    # not allowed to stall the whole walk. It must be marked
                    # attempted in the ledger for its own role: a failed review
                    # leaves the task at `awaiting-review`, which is the state
                    # that selected it, so anything else re-selects it forever.
                    session.errors.append(f"{job.task_id}: {exc}")
                    emit("error", {"task": job.task_id, "message": str(exc)})
                    if job.role == "reviewer":
                        session.review_attempts.append(job.task_id)
                    else:
                        started.append(job.task_id)
                    continue
                run_id, resolved = prepared
                if job.role == "agent":
                    started.append(job.task_id)
                    session.dispatched.append(job.task_id)
                else:
                    session.reviewed.append(job.task_id)
                    session.review_attempts.append(job.task_id)
                emit(
                    "started",
                    {
                        "task": job.task_id,
                        "role": job.role,
                        "run": run_id,
                        "command": resolved.display,
                        "in_flight": len(in_flight) + 1,
                    },
                )
                future = pool.submit(_execute, root, job, run_id)
                in_flight[future] = job

            if not in_flight:
                break

            done = _wait_for_one(in_flight, abort)
            for future in done:
                job = in_flight.pop(future)
                outcome = future.result()
                _record(session, outcome)
                emit("finished", _finished_payload(outcome))

            if abort.is_set():
                session.aborted = True
                break
    finally:
        if abort.is_set():
            _abort_in_flight(root, in_flight, session, emit)
        pool.shutdown(wait=not abort.is_set())
        _restore_signals()
        session.stopped = stop.is_set()
        session.finished_at = utcnow()
    return session


def _prepare(
    root: Path,
    job: Job,
    *,
    agent: str,
    model: str | None,
    reviewer: str | None,
    reviewer_model: str | None,
    timeout: int | None,
    cwd: str | None,
    agent_args: list[str],
) -> tuple[str, agents.ResolvedAgent]:
    """Claim the task by marking it running, and write its prompt.

    `--reviewer-model` applies whether or not `--reviewer` was given, so that
    review can use a different model of the same agent. Without a reviewer model
    the review falls back to the implementation model.
    """
    if job.role == "reviewer":
        command = reviewer or agent
        chosen_model = reviewer_model or model
    else:
        command = agent
        chosen_model = model
    run_id, _, _, resolved = runner.prepare(
        root,
        job.task_id,
        command,
        agent_args if job.role == "agent" else [],
        model=chosen_model,
        timeout=timeout,
        cwd=cwd,
        force=False,
        role=job.role,
    )
    return run_id, resolved


def _execute(root: Path, job: Job, run_id: str) -> Outcome:
    """Run one agent to completion. Errors become outcomes, never exceptions.

    A worker that raised would take the scheduler down with it and lose the
    other agents' work, so everything is reported back as data.
    """
    try:
        code = runner.execute(root, run_id, stream=False)
    except WritError as exc:
        return Outcome(job=job, run_id=run_id, error=str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        return Outcome(job=job, run_id=run_id, error=f"{type(exc).__name__}: {exc}")
    data = state.load(root)
    task = data["tasks"].get(job.task_id, {})
    run = data["runs"].get(run_id, {})
    return Outcome(
        job=job,
        run_id=run_id,
        exit_code=code,
        status=task.get("status"),
        error=run.get("verdict_error"),
    )


def _wait_for_one(
    in_flight: dict[Future[Outcome], Job], abort: threading.Event
) -> list[Future[Outcome]]:
    """Block until at least one job finishes, staying responsive to ^C.

    `concurrent.futures.wait` with no timeout swallows signals on some
    platforms, so this polls on a short interval instead.
    """
    while True:
        done = [future for future in in_flight if future.done()]
        if done:
            return done
        if abort.is_set():
            return []
        time.sleep(0.05)


def _record(session: Session, outcome: Outcome) -> None:
    if outcome.error:
        session.errors.append(f"{outcome.job.task_id}: {outcome.error}")
    status = outcome.status
    if status == "completed":
        session.completed.append(outcome.job.task_id)
    elif status in ("failed", "blocked"):
        session.failed.append(outcome.job.task_id)


def _finished_payload(outcome: Outcome) -> dict[str, Any]:
    return {
        "task": outcome.job.task_id,
        "role": outcome.job.role,
        "run": outcome.run_id,
        "exit_code": outcome.exit_code,
        "status": outcome.status,
        "error": outcome.error,
    }


def _abort_in_flight(
    root: Path,
    in_flight: dict[Future[Outcome], Job],
    session: Session,
    emit: Callable[[str, dict[str, Any]], None],
) -> None:
    """Kill the agents still running and leave their tasks resumable.

    The worker threads are still inside `runner.execute`, so they will try to
    record an outcome for a run this just cancelled. `_finish` declines to
    overwrite a cancelled run for that reason; here we wait briefly for those
    threads to notice, so the store settles before the summary is printed.
    """
    data = state.load(root)
    for job in in_flight.values():
        run_id = runner.latest_run_for(data, job.task_id)
        if run_id is None:
            continue
        try:
            runner.cancel(root, run_id)
        except WritError:
            continue
        emit("cancelled", {"task": job.task_id, "run": run_id})
    deadline = time.monotonic() + GRACE_SECONDS
    for future in in_flight:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            future.result(timeout=remaining)
        except Exception:  # pragma: no cover - the run was killed on purpose
            pass


# --------------------------------------------------------------------------
# signals


_PREVIOUS: dict[int, Any] = {}


def _install_signals(
    stop: threading.Event,
    abort: threading.Event,
    emit: Callable[[str, dict[str, Any]], None],
) -> None:
    """First ^C stops scheduling; a second one kills the agents in flight.

    Killing on the first would throw away an agent that is nearly done, and
    waiting forever on the second would leave the operator no way out.
    """

    def handler(signum, frame):  # pragma: no cover - exercised interactively
        if stop.is_set():
            abort.set()
            emit("abort", {})
            return
        stop.set()
        emit("stopping", {})

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            _PREVIOUS[signum] = signal.signal(signum, handler)
        except ValueError:  # pragma: no cover - not the main thread
            pass


def _restore_signals() -> None:
    for signum, previous in list(_PREVIOUS.items()):
        try:
            signal.signal(signum, previous)
        except (ValueError, TypeError):  # pragma: no cover
            pass
        _PREVIOUS.pop(signum, None)


# --------------------------------------------------------------------------
# reporting


def summary(data: dict[str, Any], session: Session) -> list[str]:
    """What the session did, and what is left."""
    tasks = data["tasks"]
    total = len(tasks)
    complete = sum(1 for task in tasks.values() if task["status"] == "completed")
    lines = [
        f"ran {session.agent_runs} agents over {len(set(session.dispatched))} tasks"
        f" in {_elapsed(session)}",
        f"completed {len(session.completed)}, failed {len(session.failed)}"
        + (f", errors {len(session.errors)}" if session.errors else ""),
        f"project {complete}/{total} tasks complete",
    ]
    remaining = [
        task["id"]
        for task in sorted(tasks.values(), key=lambda item: item["id"])
        if effective_status(data, task) == "ready"
    ]
    awaiting = [
        task["id"]
        for task in sorted(tasks.values(), key=lambda item: item["id"])
        if task["status"] == "awaiting-review"
    ]
    if awaiting:
        lines.append(f"awaiting review: {', '.join(awaiting)}")
    if remaining:
        lines.append(f"ready to dispatch: {', '.join(remaining)}")
    blocked = _stalled(data)
    if blocked:
        lines.append(
            f"blocked by failed work: {', '.join(blocked)}"
        )
    return lines


def _stalled(data: dict[str, Any]) -> list[str]:
    """Tasks that cannot start until something failed is dealt with.

    Transitive on purpose: if A failed and C waits on B waits on A, then C is
    just as stuck as B, and reporting only the frontier would understate how
    much of the graph one failure has parked.
    """
    tasks = data["tasks"]
    poisoned = {
        task_id
        for task_id, task in tasks.items()
        if task["status"] in ("failed", "blocked")
    }
    if not poisoned:
        return []
    # Walk forwards until the set stops growing: a task is stuck if any of its
    # dependencies is stuck.
    changed = True
    while changed:
        changed = False
        for task_id, task in tasks.items():
            if task_id in poisoned or task["status"] == "completed":
                continue
            if any(dep in poisoned for dep in task.get("depends_on", [])):
                poisoned.add(task_id)
                changed = True
    # Report only the tasks that are waiting. The failed ones are named by the
    # `failed` count above, and repeating them here would read as if they were
    # blocked by something else.
    return sorted(
        task_id
        for task_id in poisoned
        if tasks[task_id]["status"] == "planned"
    )


def _elapsed(session: Session) -> str:
    if not session.finished_at:
        return "0s"
    start = datetime.fromisoformat(session.started_at)
    end = datetime.fromisoformat(session.finished_at)
    seconds = int((end - start).total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"
