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

from . import agents, gates, plans, repair, runner, state
from .model import acceptance_summary, effective_status, rework_attempts
from .state import WritError, utcnow

#: how long a worker may hold the scheduler open after a stop is requested
GRACE_SECONDS = 5.0

#: selection orders for the ready set. `id` follows the plan's own numbering, so
#: work proceeds roughly in the order the design document laid it out and two
#: runs over the same graph pick the same tasks in the same sequence. `depth`
#: prefers the task with the longest chain of work behind it, which shortens the
#: critical path once the pool is wide enough for depth rather than total work to
#: be the limit. Below that it changes almost nothing, so `id` stays the default.
ORDERS = ("id", "depth", "unlocks")
DEFAULT_ORDER = "id"


@dataclass
class Job:
    """One agent invocation the scheduler decided to make."""

    task_id: str
    role: str  # "agent" | "reviewer" | "gate" | "repair"
    #: how many times this task had been sent back for rework when the job was
    #: chosen. Part of the ledger key, not decoration: see `key`.
    attempt: int = 0
    #: for a repair job, the request it is planning against. Part of the key for
    #: the same reason `attempt` is: a second request on the same gate is a
    #: different job, a retry of the same one is not.
    request: str = ""

    @property
    def verb(self) -> str:
        if self.role == "reviewer":
            return "review"
        if self.role == "gate":
            return "gate"
        if self.role == "repair":
            return "repair"
        # A re-dispatch after a rejection is the same invocation with a different
        # prompt, but calling it "dispatch" in the preview and the log reads as
        # work that had not started yet.
        return "rework" if self.attempt else "dispatch"

    @property
    def key(self) -> str:
        """How the session remembers this job was tried.

        A session refuses to attempt the same task twice in the same role, which
        is what stops a reviewer that writes no verdict from being re-selected
        forever. A rejection has to get past that guard without weakening it: the
        task genuinely should be dispatched again, but only because something
        changed. The rework count is that something, so it goes in the key — the
        second attempt is a different job from the first, while a repeat of the
        *same* attempt is still refused.

        Bare task id at attempt 0, so a ledger from anywhere else still matches.
        """
        if self.role == "repair":
            # The attempt here counts patches writ refused, not rework rounds. A
            # refused patch leaves the request open in the state that selected
            # it, so without the count the scheduler would either re-plan it
            # forever or — since the key would be identical — never again. The
            # bound lives in `repair.patches_left`.
            suffix = f"#{self.attempt}" if self.attempt else ""
            return f"{self.task_id}~{self.request or 'repair'}{suffix}"
        return self.task_id if not self.attempt else f"{self.task_id}#{self.attempt}"


@dataclass
class Outcome:
    """What a job produced, as recorded in the store."""

    job: Job
    run_id: str | None = None
    exit_code: int | None = None
    status: str | None = None  # the task's status afterwards
    error: str | None = None
    #: the agent's own one-line account of what it did or refused to accept.
    #: Carried through so a failure explains itself in the log rather than
    #: sending the reader to `writ show` to find out why.
    summary: str = ""
    unmet: list[int] = field(default_factory=list)
    criteria: dict[str, int] | None = None
    decisions: list[str] = field(default_factory=list)
    #: (attempt, budget) when a reviewer sent this task back rather than failing
    #: it, so the log can say "rework 1 of 2" instead of a bare `planned` that
    #: reads as though the task had never run.
    rework: tuple[int, int] | None = None
    #: for a gate: why it is held, if it is (`awaiting-repair`,
    #: `needs-decision`, `repair-exhausted`)
    held: str = ""
    #: for a gate: the repair request it opened
    request: str = ""
    #: for a repair job: the task ids its patch added, if writ accepted it
    repaired: list[str] = field(default_factory=list)
    #: for a repair job: why writ refused the patch
    refused: str = ""
    #: gate findings recorded by this run
    findings: list[str] = field(default_factory=list)

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
    #: tasks a reviewer rejected and sent back for another attempt. Not failures
    #: — the walk carried on with them — but not free either, so a session that
    #: spent half its agents on second attempts says so.
    reworked: list[str] = field(default_factory=list)
    #: gate reviews run, and the gates that asked for the plan to be repaired
    gated: list[str] = field(default_factory=list)
    repaired: list[str] = field(default_factory=list)
    #: gates that stopped for a human: an unanswerable question, or a spent
    #: repair budget. The run is not finished when one of these is outstanding —
    #: it is waiting, which is a different thing and has to read differently.
    held: list[str] = field(default_factory=list)
    stopped: bool = False
    aborted: bool = False
    started_at: str = field(default_factory=utcnow)
    finished_at: str | None = None

    @property
    def agent_runs(self) -> int:
        return len(self.dispatched) + len(self.reviewed) + len(self.gated)


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
    order: str = DEFAULT_ORDER,
) -> Job | None:
    """Choose the next agent invocation, or None when there is nothing to do.

    Reviews come first. A review is the only thing that can complete a task, and
    a completed task is the only thing that unblocks its dependents, so finishing
    work in flight opens more of the graph than starting more work does.

    Each task is attempted once per session in each role *per rework round*. A
    reviewer that writes no verdict leaves its task at `awaiting-review` — the
    same state that selected it — so without the ledger the scheduler would
    re-review it forever. A reviewer that rejects, on the other hand, has changed
    something: the task goes back to the queue carrying the rejection, and its
    next attempt is a different job under a different key (see `Job.key`), so it
    is dispatched again within this same session rather than waiting for the next
    one.

    `order` breaks ties among ready tasks. It cannot affect *which* tasks are
    eligible, only which eligible one goes first, so no order can produce a run
    the dependency rules would not allow.
    """
    busy = set(busy)
    started = set(started)
    reviewed = set(reviewed)

    for task in sorted(data["tasks"].values(), key=lambda item: item["id"]):
        if gates.is_gate(task):
            continue
        job = Job(
            task_id=task["id"], role="reviewer", attempt=rework_attempts(task)
        )
        if task["id"] in busy or job.key in reviewed:
            continue
        if task["status"] == "awaiting-review":
            return job

    # Repair planning outranks new implementation for the same reason review does:
    # it is the only thing that can un-hold a gate, and a held gate is holding
    # everything behind it. It is cheap to choose — one agent, no code written —
    # and leaving it until the ready set empties would stall the graph behind work
    # that is merely available.
    for request in repair.open_requests(data):
        if request.get("status") not in ("open", "planning"):
            continue
        if not repair.patches_left(request):
            # Writ has refused everything this planner proposed. The gate is held
            # for a human; re-planning it would spend agents on the same refusal.
            continue
        job = Job(
            task_id=request["gate"],
            role="repair",
            request=request["id"],
            attempt=repair.refusals(request),
        )
        if request["gate"] in busy or job.key in started:
            continue
        return job

    ready = [
        task
        for task in data["tasks"].values()
        if task["id"] not in busy
        and _job_for(task).key not in started
        and effective_status(data, task) == "ready"
    ]
    # The budget counts tasks, not attempts. `--max-tasks N` caps how much of the
    # graph a session takes on, and reworking a task it already started is not
    # taking on more of it — so a spent budget stops *new* tasks and still lets a
    # task already in the ledger have its next attempt. Refusing that would leave
    # a rejected task failed for want of a slot it had already spent, which is the
    # same reason a review ignores the budget: half-finished work is worse than
    # none.
    if budget is not None:
        taken = {_base(key) for key in started}
        if len(taken) >= budget:
            # Gates are exempt. `--max-tasks N` caps how much implementation work
            # a session takes on, and a gate is not implementation: refusing to
            # run the gate over the tasks the budget just allowed would stop the
            # session exactly where its work is least verified.
            ready = [
                task
                for task in ready
                if task["id"] in taken or gates.is_gate(task)
            ]
    if not ready:
        return None
    task_id = _first(data, _prefer_tasks(ready), order)
    return _job_for(data["tasks"][task_id])


def _job_for(task: dict[str, Any]) -> Job:
    """The job that would run this ready node, gate or task.

    The attempt number differs by kind, and that is the point of routing both
    through one function. A task's attempt counts rework rounds; a gate's counts
    repair rounds, because a gate whose repair has landed is genuinely due again
    — the code it is reviewing has changed — and keying it at attempt 0 would
    leave the session believing it had already been run.
    """
    if gates.is_gate(task):
        return Job(task_id=task["id"], role="gate", attempt=gates.rounds(task))
    return Job(task_id=task["id"], role="agent", attempt=rework_attempts(task))


def _base(key: str) -> str:
    """The task id inside a ledger key, whichever attempt it names."""
    return key.split("#", 1)[0].split("~", 1)[0]


def _first(data: dict[str, Any], ready: list[dict[str, Any]], order: str) -> str:
    """The id of the ready task to start next, under `order`.

    Every order falls back to the id, so selection stays deterministic: two runs
    over the same graph make the same choices, which matters more for reading a
    transcript than the few percent a cleverer order buys.
    """
    if order == "depth":
        depths = _depths(data)
        return min(ready, key=lambda t: (-depths[t["id"]], t["id"]))["id"]
    if order == "unlocks":
        counts = _dependents(data)
        return min(ready, key=lambda t: (-len(counts[t["id"]]), t["id"]))["id"]
    return min(task["id"] for task in ready)


def _prefer_tasks(ready: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Implementation work before gates, when both are ready.

    A gate is ready as soon as its dependencies complete, and it will still be
    ready in ten minutes. An implementation task that is ready now may be the one
    thing a parallel pool has to work on, so spending a worker on a gate while
    tasks wait narrows the graph for no gain. Gates are not starved: once the
    ready tasks run out, they are all that is left.
    """
    tasks = [task for task in ready if not gates.is_gate(task)]
    return tasks or ready


def _dependents(data: dict[str, Any]) -> dict[str, set[str]]:
    """Reverse edges: task id -> the tasks that wait on it."""
    out: dict[str, set[str]] = {task_id: set() for task_id in data["tasks"]}
    for task_id, task in data["tasks"].items():
        for dep in task.get("depends_on", []):
            if dep in out:
                out[dep].add(task_id)
    return out


def _depths(data: dict[str, Any]) -> dict[str, int]:
    """Longest chain of remaining work from each task to a leaf.

    Completed tasks contribute nothing, so the measure is of work still to do:
    finishing the deepest remaining chain first is what keeps the critical path
    from becoming the thing everything else waits on.
    """
    dependents = _dependents(data)
    tasks = data["tasks"]
    depths: dict[str, int] = {}

    def depth(task_id: str, seen: frozenset[str] = frozenset()) -> int:
        if task_id in depths:
            return depths[task_id]
        if task_id in seen:  # pragma: no cover - check_dag rejects cycles
            return 0
        onward = [
            depth(child, seen | {task_id})
            for child in dependents.get(task_id, ())
            if tasks[child]["status"] != "completed"
        ]
        depths[task_id] = 1 + max(onward, default=0)
        return depths[task_id]

    for task_id in tasks:
        depth(task_id)
    return depths


def preview(
    data: dict[str, Any], *, budget: int | None, order: str = DEFAULT_ORDER
) -> list[Job]:
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
        job = next_job(
            shadow, busy=[], budget=budget, started=started, order=order
        )
        if job is None:
            break
        jobs.append(job)
        task = simulated[job.task_id]
        if job.role == "agent":
            started.append(job.key)
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
    reviewer_timeout: int | None = None,
    parallel: int = 1,
    max_tasks: int | None = None,
    order: str = DEFAULT_ORDER,
    timeout: int | None = None,
    cwd: str | None = None,
    agent_args: list[str] | None = None,
    max_rework: int | None = None,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> Session:
    """Walk the DAG until it runs out of work, an error stops it, or you do."""
    session = Session()
    emit = on_event or (lambda name, payload: None)

    # The approval gate. Checked here rather than only in the CLI so that any
    # caller of `run` — the supervisor, the server, a test — is held to it: an
    # unreviewed plan is not executable, and a second entry point that skipped
    # the check would make the gate advisory.
    with state.transaction(root) as data:
        if not plans.runnable(data):
            raise WritError(plans.not_runnable_message(data))
        plans.mark_executing(data)

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
                    order=order,
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
                        reviewer_timeout=reviewer_timeout,
                        timeout=timeout,
                        cwd=cwd,
                        agent_args=agent_args or [],
                        max_rework=max_rework,
                    )
                except WritError as exc:
                    # A job that cannot even be started is recorded and skipped,
                    # not allowed to stall the whole walk. It must be marked
                    # attempted in the ledger for its own role: a failed review
                    # leaves the task at `awaiting-review`, which is the state
                    # that selected it, so anything else re-selects it forever.
                    # The same is true of a repair job, whose request stays open.
                    session.errors.append(f"{job.task_id}: {exc}")
                    emit("error", {"task": job.task_id, "message": str(exc)})
                    if job.role == "reviewer":
                        session.review_attempts.append(job.key)
                    else:
                        started.append(job.key)
                    continue
                run_id, resolved = prepared
                if job.role == "agent":
                    started.append(job.key)
                    session.dispatched.append(job.task_id)
                elif job.role == "gate":
                    started.append(job.key)
                    session.gated.append(job.task_id)
                elif job.role == "repair":
                    started.append(job.key)
                else:
                    session.reviewed.append(job.task_id)
                    session.review_attempts.append(job.key)
                emit(
                    "started",
                    {
                        "task": job.task_id,
                        "role": job.role,
                        "run": run_id,
                        "command": resolved.display,
                        "attempt": job.attempt,
                        "in_flight": len(in_flight) + 1,
                    },
                )
                future = pool.submit(_execute, root, job, run_id)
                in_flight[future] = job

            if not in_flight:
                # Nothing running and nothing selectable: the walk is over. That is
                # a weaker claim than it used to be, and it holds only because
                # everything a finished job can create — a gate becoming ready, a
                # repair request, the tasks a repair adds — is committed inside that
                # job's own transaction before it returns. So by the time this line
                # is reached, `next_job` has already had the chance to see it. The
                # run ends with work outstanding only when that work needs a person,
                # and `summary` says which of those it is rather than reporting the
                # project as done.
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
    reviewer_timeout: int | None,
    timeout: int | None,
    cwd: str | None,
    agent_args: list[str],
    max_rework: int | None = None,
) -> tuple[str, agents.ResolvedAgent]:
    """Claim the task by marking it running, and write its prompt.

    The reviewer's command, model and timeout each apply whether or not the others
    were given, so review can be a different model of the same agent, or the same
    agent on a longer leash. Each one falls back to the implementation setting,
    which is the convenient default and the weaker one — a model checking its own
    work agrees with itself more than it should.
    """
    if job.role == "reviewer":
        command = reviewer or agent
        chosen_model = reviewer_model or model
        chosen_timeout = reviewer_timeout if reviewer_timeout is not None else timeout
    else:
        command = agent
        chosen_model = model
        chosen_timeout = timeout
    run_id, _, _, resolved = runner.prepare(
        root,
        job.task_id,
        command,
        agent_args if job.role == "agent" else [],
        model=chosen_model,
        timeout=chosen_timeout,
        cwd=cwd,
        force=False,
        role=job.role,
        max_rework=max_rework,
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
    reported = run.get("verdict") or {}
    held = task.get("held") or {}
    applied = run.get("patch_applied") or {}
    attempts = task.get("gate_attempts") or []
    return Outcome(
        job=job,
        run_id=run_id,
        exit_code=code,
        status=task.get("status"),
        error=run.get("verdict_error"),
        summary=reported.get("summary", "") or run.get("patch_error", ""),
        unmet=list(reported.get("unmet", [])),
        criteria=acceptance_summary(task) if task else None,
        decisions=list(reported.get("decisions", [])),
        rework=_rework_round(job, task, reported),
        held=str(held.get("reason", "")) if task.get("status") == "blocked" else "",
        request=str(held.get("request", "")),
        repaired=list(applied.get("tasks", [])),
        refused=str(run.get("patch_error", "")),
        findings=list(attempts[-1].get("findings", [])) if attempts else [],
    )


def _rework_round(
    job: Job, task: dict[str, Any], reported: dict[str, Any]
) -> tuple[int, int] | None:
    """(attempt, budget) if this reviewer's rejection bought another attempt.

    Read from the task rather than inferred from its status: `planned` after a
    reviewer run could also be a reap of a lost implementation, and the two need
    different lines in the log.
    """
    if job.role != "reviewer" or reported.get("decision") != "reject":
        return None
    record = task.get("rework") or {}
    if record.get("exhausted") or not record.get("attempt"):
        return None
    return record["attempt"], record.get("budget", record.get("max", 0))


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
    if outcome.rework:
        session.reworked.append(outcome.job.task_id)
    status = outcome.status
    if outcome.job.role == "repair":
        if outcome.repaired:
            session.repaired.append(outcome.job.task_id)
        return
    if status == "completed":
        session.completed.append(outcome.job.task_id)
    elif status == "blocked" and outcome.job.role == "gate":
        # A gate that asked for repair is not a failure: the plan is being fixed
        # and the run carries on. A gate held for a human is not a failure either,
        # but it does need saying, because nothing else will move it.
        if outcome.held in ("needs-decision", "repair-exhausted"):
            session.held.append(outcome.job.task_id)
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
        "summary": outcome.summary,
        "unmet": outcome.unmet,
        "criteria": outcome.criteria,
        "decisions": outcome.decisions,
        "rework": list(outcome.rework) if outcome.rework else None,
        "held": outcome.held,
        "request": outcome.request,
        "repaired": outcome.repaired,
        "refused": outcome.refused,
        "findings": outcome.findings,
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
        + (
            f", sent back for rework {len(session.reworked)}"
            if session.reworked
            else ""
        )
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
        # Name what failed, not only what is waiting. "blocked by failed work:
        # G-M01" reads as if G-M01 failed, when G-M01 is the casualty and the
        # reader needs the id of the task to go and look at.
        roots = _stall_roots(data)
        lines.append(
            f"blocked by failed work: {', '.join(blocked)}"
            + (f"   (failed: {', '.join(roots)})" if roots else "")
        )
    lines.extend(_gate_lines(data, session))
    # Proposals are inert until a human rules on them, so a run that produced
    # some has left work that no later `writ run` will pick up. Say so, or the
    # records sit unread.
    proposed = [
        item["id"]
        for item in data.get("decisions", [])
        if item["status"] == "proposed"
    ]
    if proposed:
        lines.append(
            f"decisions proposed: {', '.join(proposed)}"
            "   (writ list decisions --proposed)"
        )
    return lines


#: reasons a gate is blocked that are not failures. See `held_gates`.
HELD_REASONS = ("awaiting-repair", "needs-decision", "repair-exhausted", "repair-refused")


def held_gates(data: dict[str, Any]) -> dict[str, str]:
    """Gates stopped short of a verdict, and why — id -> reason.

    A gate blocked this way has not failed. `awaiting-repair` means the plan is
    being changed and the gate will be asked again; the rest mean a person has to
    look. Both are distinct from a task that failed, and the distinction has to
    survive into the summary: a run that ends with a gate awaiting a decision has
    not finished the project, and saying "blocked by failed work" would send the
    reader looking for a failure that is not there.
    """
    found = {}
    for task_id, task in data["tasks"].items():
        if not gates.is_gate(task) or task.get("status") != "blocked":
            continue
        reason = str((task.get("held") or {}).get("reason", ""))
        if reason in HELD_REASONS:
            found[task_id] = reason
    return found


def _stalled(data: dict[str, Any]) -> list[str]:
    """Tasks that cannot start until something failed is dealt with.

    Transitive on purpose: if A failed and C waits on B waits on A, then C is
    just as stuck as B, and reporting only the frontier would understate how
    much of the graph one failure has parked.

    Held gates are excluded, along with the work behind them. They are reported
    separately because they are a different situation with a different next step
    — see `held_gates`.
    """
    tasks = data["tasks"]
    held = held_gates(data)
    poisoned = {
        task_id
        for task_id, task in tasks.items()
        if task["status"] in ("failed", "blocked") and task_id not in held
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


def _stall_roots(data: dict[str, Any]) -> list[str]:
    """The failed or blocked tasks that everything in `_stalled` is waiting on.

    The frontier, not the whole poisoned set: a task that failed because its own
    dependency failed is not where the reader should start. Held gates are excluded
    for the same reason they are in `_stalled` — they are reported separately.
    """
    tasks = data["tasks"]
    held = held_gates(data)
    return sorted(
        task_id
        for task_id, task in tasks.items()
        if task["status"] in ("failed", "blocked")
        and task_id not in held
        and not any(
            tasks[dep]["status"] in ("failed", "blocked")
            for dep in task.get("depends_on", [])
            if dep in tasks
        )
    )


#: what a held gate means for the reader, and what moves it
_HELD_ADVICE = {
    "awaiting-repair": ("plan repair pending", "writ list repairs"),
    "needs-decision": ("waiting on a decision", "writ list decisions --proposed"),
    "repair-exhausted": ("out of repair rounds", "writ show {gate}"),
    "repair-refused": ("no acceptable repair patch", "writ show {gate}"),
}


def _gate_lines(data: dict[str, Any], session: Session) -> list[str]:
    """What the gates did this session, and which of them are still waiting.

    Reported apart from the task counts because a gate is not a unit of work: it
    produced no code, and folding its outcome into "completed 4, failed 1" would
    make a plan that needs repairing look like an implementation that broke.
    """
    lines = []
    if session.repaired:
        lines.append(
            f"plan repaired: {', '.join(sorted(set(session.repaired)))}"
            f"   (revision {plans.revision(data)})"
        )
    held = held_gates(data)
    for gate_id, reason in sorted(held.items()):
        label, hint = _HELD_ADVICE.get(reason, (reason, "writ show {gate}"))
        lines.append(f"{gate_id} held: {label}   ({hint.format(gate=gate_id)})")
    if held:
        waiting = sorted(_behind(data, set(held)))
        if waiting:
            lines.append(f"waiting on gates: {', '.join(waiting)}")
    return lines


def _behind(data: dict[str, Any], stoppers: set[str]) -> set[str]:
    """Unstarted tasks that transitively depend on any of `stoppers`."""
    tasks = data["tasks"]
    reached = set(stoppers)
    changed = True
    while changed:
        changed = False
        for task_id, task in tasks.items():
            if task_id in reached or task["status"] == "completed":
                continue
            if any(dep in reached for dep in task.get("depends_on", [])):
                reached.add(task_id)
                changed = True
    return {
        task_id
        for task_id in reached - stoppers
        if tasks[task_id]["status"] == "planned"
    }


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
