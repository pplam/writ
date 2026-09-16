"""Dispatching work to coding agents and tracking the resulting runs."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, IO, Iterable

from . import agents, planner, state, verdict
from .model import (
    add_evidence,
    blocking_dependencies,
    get_task,
    refresh_milestones,
)
from .state import WritError, utcnow

ACTIVE_RUN_STATUSES = ("starting", "running")

GUARDRAILS = """\
Working rules (non-negotiable):
1. Inspect the repository before editing; state contradictions and assumptions first.
2. Write the failing test first and run it so the failure is visible.
3. Implement the minimum change that satisfies the acceptance criteria.
4. Refactor only while tests are green.
5. Run the project's full verification (build, tests, vet/lint) before reporting.
6. Do not weaken an invariant, add a dependency, or use live network data to pass a test.
7. Do not modify components outside the allowed list.
"""


def build_prompt(
    data: dict[str, Any],
    task: dict[str, Any],
    root: Path,
    *,
    verdict_path: Path | None = None,
) -> str:
    """Compose the agent prompt from the task, its gates, and the design doc."""
    lines: list[str] = []
    lines.append("You are implementing ONE bounded task in this project.")
    lines.append("")
    docs = list(data.get("design_docs", []))
    if task.get("design_doc") and task["design_doc"] not in docs:
        docs.append(task["design_doc"])
    if docs:
        lines.append("Authoritative documents (read before editing):")
        lines.extend(f"- {doc}" for doc in docs)
        lines.append("")
    lines.append(f"Task {task['id']}: {task['title']}")
    if task.get("milestone"):
        milestone = data["milestones"].get(task["milestone"], {})
        lines.append(f"Milestone: {task['milestone']} — {milestone.get('title', '')}")
    lines.append("")
    lines.append("Acceptance criteria (each must be demonstrably met):")
    for index, item in enumerate(task.get("acceptances", []), start=1):
        marker = "x" if item["status"] == "passed" else " "
        lines.append(f"  {index}. [{marker}] {item['text']}")
    lines.append("")
    if task.get("depends_on"):
        lines.append(f"Completed prerequisites: {', '.join(task['depends_on'])}")
        lines.append("")
    if task.get("allowed"):
        lines.append("Allowed files/packages:")
        lines.extend(f"- {item}" for item in task["allowed"])
        lines.append("")
    if task.get("forbidden"):
        lines.append("Forbidden (do not modify):")
        lines.extend(f"- {item}" for item in task["forbidden"])
        lines.append("")
    excerpt = _design_excerpt(task, root)
    if excerpt:
        lines.append("Relevant design section:")
        lines.append("---")
        lines.append(excerpt)
        lines.append("---")
        lines.append("")
    if task.get("evidence"):
        recent = task["evidence"][-4:]
        lines.append("Previous attempts on this task recorded:")
        for entry in recent:
            actor = entry.get("actor", "operator")
            lines.append(f"- [{actor}] {entry['text']}")
        lines.append("")
    lines.append(GUARDRAILS)
    lines.append("")
    lines.append(_verdict_instructions(task, verdict_path))
    return "\n".join(lines)


def _verdict_instructions(task: dict[str, Any], verdict_path: Path | None) -> str:
    """Tell the agent to report a machine-readable verdict, and how.

    Writ records the task's status from this file. Without it the run leaves the
    task untouched, so the instruction is explicit about the consequence rather
    than trusting the agent to infer that reporting matters.
    """
    path = verdict_path or Path(verdict.VERDICT_FILENAME)
    total = len(task.get("acceptances", []))
    lines = [
        "When you are done, report your verdict as JSON to this exact path:",
        f"  {path}",
        "",
        "The file must contain JSON only — no prose, no code fence.",
        "",
        "Schema:",
        verdict.SCHEMA,
        "",
        verdict.RULES,
        "",
        f"This task has {total} acceptance criteria, numbered 1 to {total}.",
        "",
        "Writ sets this task's status from that file, and an independent reviewer "
        "re-checks whatever you claim. If you do not write it, the task stays "
        "where it was and your work is not recorded.",
        "",
        "If you cannot write the file, print the same JSON to stdout inside a "
        "single ```json fenced block instead.",
    ]
    return "\n".join(lines)


def build_review_prompt(
    data: dict[str, Any],
    task: dict[str, Any],
    root: Path,
    *,
    verdict_path: Path | None = None,
) -> str:
    """Compose the prompt for an agent reviewing someone else's work.

    The reviewer is told what was claimed and asked to verify it independently.
    It gets the claim because a review that cannot see the claim cannot tell a
    misleading one from an honest one; it is told not to trust it for the same
    reason.
    """
    lines: list[str] = []
    lines.append(
        "You are reviewing ONE completed task in this project. You did not write "
        "this code. Do not fix it — judge it."
    )
    lines.append("")
    docs = list(data.get("design_docs", []))
    if task.get("design_doc") and task["design_doc"] not in docs:
        docs.append(task["design_doc"])
    if docs:
        lines.append("Authoritative documents:")
        lines.extend(f"- {doc}" for doc in docs)
        lines.append("")
    lines.append(f"Task {task['id']}: {task['title']}")
    if task.get("milestone"):
        milestone = data["milestones"].get(task["milestone"], {})
        lines.append(f"Milestone: {task['milestone']} — {milestone.get('title', '')}")
    lines.append("")
    lines.append("Acceptance criteria to verify:")
    for index, item in enumerate(task.get("acceptances", []), start=1):
        lines.append(f"  {index}. {item['text']}")
        claimed = item.get("status", "pending")
        if item.get("evidence"):
            lines.append(f"     implementer claimed {claimed}: {item['evidence']}")
        else:
            lines.append(f"     implementer left this {claimed}")
    lines.append("")
    last = task.get("last_verdict") or {}
    if last.get("summary"):
        lines.append("The implementer summarised its work as:")
        lines.append(f"  {last['summary']}")
        lines.append("")
    if task.get("allowed"):
        lines.append("The task was scoped to these files/packages:")
        lines.extend(f"- {item}" for item in task["allowed"])
        lines.append("")
    if task.get("forbidden"):
        lines.append("It was forbidden from modifying:")
        lines.extend(f"- {item}" for item in task["forbidden"])
        lines.append("")
    excerpt = _design_excerpt(task, root)
    if excerpt:
        lines.append("Relevant design section:")
        lines.append("---")
        lines.append(excerpt)
        lines.append("---")
        lines.append("")
    lines.append(
        "Verify by running the project's tests yourself and reading the diff. "
        "Treat the implementer's claims as claims."
    )
    lines.append("")
    lines.append(verdict.REVIEW_RULES)
    lines.append("")
    path = verdict_path or Path(verdict.VERDICT_FILENAME)
    lines.append("Write your review as JSON to this exact path:")
    lines.append(f"  {path}")
    lines.append("")
    lines.append("The file must contain JSON only — no prose, no code fence.")
    lines.append("")
    lines.append("Schema:")
    lines.append(verdict.REVIEW_SCHEMA)
    lines.append("")
    lines.append(
        "Writ completes or fails the task from your decision, so it is the last "
        "word. If you do not write the file, the task stays awaiting review."
    )
    lines.append("")
    lines.append(
        "If you cannot write the file, print the same JSON to stdout inside a "
        "single ```json fenced block instead."
    )
    return "\n".join(lines)


def _design_excerpt(task: dict[str, Any], root: Path, limit: int = 4000) -> str:
    doc = task.get("design_doc")
    section = task.get("design_section")
    if not doc or not section:
        return ""
    path = Path(doc)
    if not path.is_absolute():
        path = root / path
    text = planner.section_text(path, section)
    return text[:limit]


def new_run_id(task_id: str, taken: Iterable[str] = ()) -> str:
    """A unique run id for this task.

    Ids are timestamped to the second and two runs of the same task can easily
    start within one second — dispatch then review, or a quick retry — so a
    collision is disambiguated with a suffix rather than silently overwriting
    the earlier run's record.
    """
    base = f"{task_id}-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}"
    existing = set(taken)
    if base not in existing:
        return base
    for suffix in range(2, 100):
        candidate = f"{base}-{suffix}"
        if candidate not in existing:
            return candidate
    raise WritError(f"too many runs of {task_id} in one second")


TIMEOUT_NOTE = "writ: agent exceeded its timeout and was terminated"


def _tee(
    source: IO[str], sink: IO[str], mirror: IO[str] | None, prefix: str = ""
) -> None:
    """Copy a stream to a file and optionally to the terminal, line by line.

    Read in small chunks rather than by line: an agent that draws progress with
    carriage returns and no newline would otherwise appear frozen.
    """
    at_line_start = True
    while True:
        chunk = source.read(1)
        if not chunk:
            break
        sink.write(chunk)
        sink.flush()
        if mirror is None:
            continue
        if prefix and at_line_start:
            mirror.write(prefix)
        mirror.write(chunk)
        mirror.flush()
        at_line_start = chunk in ("\n", "\r")
    if mirror is not None and not at_line_start:
        mirror.write("\n")
        mirror.flush()


def _feed(process: subprocess.Popen, prompt: str) -> None:
    """Deliver the prompt on stdin, tolerating an agent that never reads it."""
    if process.stdin is None:
        return
    try:
        process.stdin.write(prompt)
    except BrokenPipeError:
        # an agent that ignores stdin is the caller's problem to diagnose,
        # not a reason to fail the run here
        pass
    finally:
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass


def run_agent(
    command: list[str],
    prompt: str,
    directory: Path,
    cwd: str | Path,
    timeout: int | None,
    *,
    stream: bool = False,
    prefix: str = "",
) -> int:
    """Run a coding agent to completion, leaving a full transcript on disk.

    Shared by task dispatch and by `writ plan`: prompt on stdin, streams to
    files, timeout enforced by killing the whole process group. Returns the exit
    code; 124 means it was killed for exceeding its timeout.

    With `stream`, output is mirrored to this terminal as it arrives, so a long
    agent run is visibly working instead of looking hung. The transcript on disk
    is written either way and is the same bytes.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "prompt.txt").write_text(prompt, encoding="utf-8")
    with (directory / "stdout.log").open("w", encoding="utf-8") as out, (
        directory / "stderr.log"
    ).open("w", encoding="utf-8") as err:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE if stream else out,
            stderr=subprocess.PIPE if stream else err,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        if not stream:
            try:
                process.communicate(input=prompt, timeout=timeout)
                return process.returncode
            except subprocess.TimeoutExpired:
                return _timed_out(process, directory, out, err)

        # tee in threads: the agent may write a lot to either stream, and a
        # full pipe buffer would deadlock a single-threaded reader
        pumps = [
            threading.Thread(
                target=_tee, args=(process.stdout, out, sys.stdout, prefix), daemon=True
            ),
            threading.Thread(
                target=_tee, args=(process.stderr, err, sys.stderr, prefix), daemon=True
            ),
        ]
        for pump in pumps:
            pump.start()
        try:
            _feed(process, prompt)
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            for pump in pumps:
                pump.join(timeout=1)
            return _timed_out(process, directory, out, err)
        for pump in pumps:
            pump.join(timeout=5)
        return process.returncode


def _timed_out(
    process: subprocess.Popen, directory: Path, out: IO[str], err: IO[str]
) -> int:
    """Kill a run that overran, recording whether it had said anything."""
    _terminate(process.pid)
    process.wait(timeout=10)
    out.flush()
    err.flush()
    # measured before writing our own note, so callers can still tell a silent
    # hang from an agent that produced output and then stalled
    if err.tell() == 0 and out.tell() == 0:
        (directory / "silent").write_text("", encoding="utf-8")
    err.write(f"\n{TIMEOUT_NOTE}\n")
    return 124


def produced_output(directory: Path) -> bool:
    """Whether the agent itself wrote anything, ignoring writ's own notes."""
    if (directory / "silent").exists():
        return False
    for name in ("stdout.log", "stderr.log"):
        path = directory / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace").replace(
            TIMEOUT_NOTE, ""
        )
        if text.strip():
            return True
    return False


def prepare(
    root: Path,
    task_id: str,
    agent: str,
    agent_args: list[str],
    *,
    model: str | None = None,
    timeout: int | None,
    cwd: str | None,
    force: bool,
    role: str = "agent",
) -> tuple[str, Path, str, agents.ResolvedAgent]:
    """Create the run directory and record the run as `starting`.

    `role` selects the prompt and how the resulting verdict is applied: an
    implementing agent parks the task at `awaiting-review`, a reviewer completes
    or fails it.
    """
    resolved = agents.resolve(agent, agent_args, model)
    with state.transaction(root) as data:
        task = get_task(data, task_id)
        if role == "reviewer":
            if task["status"] not in ("awaiting-review", "reviewing") and not force:
                raise WritError(
                    f"{task_id} is {task['status']}, not awaiting review "
                    "(use --force to review it anyway)"
                )
        elif not force:
            blockers = blocking_dependencies(data, task)
            if blockers:
                raise WritError(
                    f"{task_id} is blocked by incomplete dependencies: "
                    f"{', '.join(blockers)} (use --force to override)"
                )
        run_id = new_run_id(task_id, data["runs"])
        directory = state.run_dir(root, run_id)
        directory.mkdir(parents=True, exist_ok=True)
        verdict_path = directory / verdict.VERDICT_FILENAME
        builder = build_review_prompt if role == "reviewer" else build_prompt
        prompt = builder(data, task, Path(root), verdict_path=verdict_path)
        (directory / "prompt.txt").write_text(prompt, encoding="utf-8")
        data["runs"][run_id] = {
            "id": run_id,
            "task": task_id,
            "role": role,
            "status": "starting",
            "command": resolved.command,
            "model": model,
            "cwd": str(Path(cwd or root).expanduser().resolve()),
            "timeout": timeout,
            "created_at": utcnow(),
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
            "pid": None,
            "dir": str(directory),
        }
        task.setdefault("runs", []).append(run_id)
        task["status"] = "reviewing" if role == "reviewer" else "running"
        task["updated_at"] = utcnow()
        refresh_milestones(data)
    return run_id, directory, prompt, resolved


def execute(root: Path, run_id: str, *, stream: bool = False, prefix: str = "") -> int:
    """Run the agent synchronously and record the outcome.

    Unlike `run_agent`, this records the pid in project state so another
    terminal can watch or cancel the run, and it converts the exit code into
    task status. With `stream`, output is also mirrored to this terminal.
    """
    data = state.load(root)
    run = data["runs"].get(run_id)
    if run is None:
        raise WritError(f"unknown run: {run_id}")
    directory = Path(run["dir"])
    prompt = (directory / "prompt.txt").read_text(encoding="utf-8")
    stdout_path = directory / "stdout.log"
    stderr_path = directory / "stderr.log"
    try:
        with stdout_path.open("w", encoding="utf-8") as out, stderr_path.open(
            "w", encoding="utf-8"
        ) as err:
            process = subprocess.Popen(
                run["command"],
                cwd=run["cwd"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE if stream else out,
                stderr=subprocess.PIPE if stream else err,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            _mark_running(root, run_id, process.pid)
            pumps: list[threading.Thread] = []
            if stream:
                pumps = [
                    threading.Thread(
                        target=_tee,
                        args=(process.stdout, out, sys.stdout, prefix),
                        daemon=True,
                    ),
                    threading.Thread(
                        target=_tee,
                        args=(process.stderr, err, sys.stderr, prefix),
                        daemon=True,
                    ),
                ]
                for pump in pumps:
                    pump.start()
            try:
                if stream:
                    _feed(process, prompt)
                    process.wait(timeout=run.get("timeout"))
                else:
                    process.communicate(input=prompt, timeout=run.get("timeout"))
                code = process.returncode
            except subprocess.TimeoutExpired:
                for pump in pumps:
                    pump.join(timeout=1)
                code = _timed_out(process, directory, out, err)
            else:
                for pump in pumps:
                    pump.join(timeout=5)
    except FileNotFoundError as exc:
        _finish(root, run_id, 127, note=f"agent not found: {exc}")
        raise WritError(f"agent command not found: {run['command'][0]}") from exc
    _finish(root, run_id, code)
    return code


def _mark_running(root: Path, run_id: str, pid: int) -> None:
    with state.transaction(root) as data:
        run = data["runs"][run_id]
        run["status"] = "running"
        run["pid"] = pid
        run["started_at"] = utcnow()


def _finish(root: Path, run_id: str, code: int, note: str | None = None) -> None:
    """Record the run's outcome, and apply the agent's verdict to its task.

    An exit code is not a judgement. A process can exit 0 having done nothing and
    exit non-zero after finishing the work, so the task's status comes from the
    verdict the agent wrote, not from `code`. A missing or invalid verdict leaves
    the task's criteria untouched and says so — silently guessing is what this
    whole mechanism exists to avoid.
    """
    with state.transaction(root) as data:
        run = data["runs"][run_id]
        run["status"] = "completed" if code == 0 else "failed"
        run["exit_code"] = code
        run["finished_at"] = utcnow()
        if note:
            run["note"] = note
        role = run.get("role", "agent")
        task = data["tasks"].get(run["task"])
        if task is None:
            refresh_milestones(data)
            return
        directory = Path(run["dir"])
        actor = _actor(run)
        try:
            reported = verdict.read(directory, role=role)
        except WritError as exc:
            reported = None
            run["verdict_error"] = str(exc)
            add_evidence(task, f"unusable verdict from {actor}: {exc}", actor="writ")

        if reported is not None:
            try:
                verdict.check_scope(reported, task, str(directory))
            except WritError as exc:
                run["verdict_error"] = str(exc)
                add_evidence(task, f"unusable verdict from {actor}: {exc}", actor="writ")
            else:
                run["verdict"] = {
                    "outcome": reported.outcome,
                    "decision": reported.decision,
                    "passed": reported.passed,
                    "unmet": reported.unmet,
                }
                status = verdict.apply(data, task, reported, actor=actor)
                run["resulting_status"] = status
                refresh_milestones(data)
                return

        # No usable verdict. Record what happened without inventing a judgement:
        # the task falls back to failed if the process itself failed, and
        # otherwise returns to planned so it can be picked up again.
        if task["status"] in ("running", "reviewing"):
            task["status"] = "failed" if code != 0 else "planned"
            task["updated_at"] = utcnow()
        add_evidence(
            task,
            f"run {run_id} exited {code} without a usable verdict"
            + (f" ({note})" if note else ""),
            actor="writ",
        )
        refresh_milestones(data)


def _actor(run: dict[str, Any]) -> str:
    """A short name for who produced a verdict, for the evidence log."""
    role = run.get("role", "agent")
    command = run.get("command") or []
    name = Path(command[0]).name if command else "agent"
    if run.get("model"):
        name = f"{name}:{run['model']}"
    return f"{role}({name})"


def verdict_summary(root: Path, run_id: str) -> tuple[str | None, str | None]:
    """The status a run produced and any verdict error, for the CLI to report."""
    data = state.load(root)
    run = data["runs"].get(run_id) or {}
    return run.get("resulting_status"), run.get("verdict_error")


def detach(root: Path, run_id: str) -> int:
    """Spawn a supervisor process that owns the run after we exit."""
    log = state.run_dir(root, run_id) / "supervisor.log"
    with log.open("w", encoding="utf-8") as handle:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "writ",
                "--root",
                str(root),
                "supervise",
                run_id,
            ],
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    with state.transaction(root) as data:
        data["runs"][run_id]["supervisor_pid"] = process.pid
        data["runs"][run_id]["detached"] = True
    return process.pid


def _terminate(pid: int) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.2)
        if not process_alive(pid):
            return


def process_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def cancel(root: Path, run_id: str) -> None:
    """Stop a running agent and mark the run cancelled."""
    with state.transaction(root) as data:
        run = data["runs"].get(run_id)
        if run is None:
            raise WritError(f"unknown run: {run_id}")
        if run["status"] not in ACTIVE_RUN_STATUSES:
            raise WritError(f"run {run_id} is not active (status: {run['status']})")
        pid = run.get("pid")
        supervisor = run.get("supervisor_pid")
    for candidate in (supervisor, pid):
        if candidate:
            _terminate(candidate)
    with state.transaction(root) as data:
        run = data["runs"][run_id]
        run["status"] = "cancelled"
        run["finished_at"] = utcnow()
        run["exit_code"] = run.get("exit_code")
        task = data["tasks"].get(run["task"])
        if task is not None and task["status"] == "running":
            task["status"] = "planned"
            task["updated_at"] = utcnow()
            add_evidence(task, f"run {run_id} cancelled")
        refresh_milestones(data)


def reap(root: Path) -> list[str]:
    """Reconcile runs whose owning process died without recording an outcome."""
    reaped: list[str] = []
    with state.transaction(root) as data:
        for run_id, run in data["runs"].items():
            if run["status"] not in ACTIVE_RUN_STATUSES:
                continue
            owner = run.get("supervisor_pid") or run.get("pid")
            if process_alive(owner):
                continue
            run["status"] = "interrupted"
            run["finished_at"] = utcnow()
            task = data["tasks"].get(run["task"])
            if task is not None and task["status"] == "running":
                task["status"] = "planned"
                task["updated_at"] = utcnow()
                add_evidence(task, f"run {run_id} was interrupted")
            reaped.append(run_id)
        refresh_milestones(data)
    return reaped


def log_path(root: Path, run_id: str, stream: str) -> Path:
    data = state.load(root)
    run = data["runs"].get(run_id)
    if run is None:
        raise WritError(f"unknown run: {run_id}")
    return Path(run["dir"]) / f"{stream}.log"


def resolve_run(data: dict[str, Any], run_id: str) -> dict[str, Any]:
    run = data["runs"].get(run_id)
    if run is None:
        raise WritError(f"unknown run: {run_id}")
    return run


def latest_run_for(data: dict[str, Any], task_id: str) -> str | None:
    runs = data["tasks"].get(task_id, {}).get("runs", [])
    return runs[-1] if runs else None
