"""Dispatching work to coding agents and tracking the resulting runs."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, IO

from . import agents, planner, state
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

Report back:
- files changed and why
- the test written first and its initial failure
- tests now passing, with the exact commands and results
- acceptance criteria met and not met
- assumptions, deviations, and remaining risks
"""


def build_prompt(data: dict[str, Any], task: dict[str, Any], root: Path) -> str:
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
    lines.append(GUARDRAILS)
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


def new_run_id(task_id: str) -> str:
    return f"{task_id}-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}"


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
) -> tuple[str, Path, str, agents.ResolvedAgent]:
    """Create the run directory and record the run as `starting`."""
    resolved = agents.resolve(agent, agent_args, model)
    with state.transaction(root) as data:
        task = get_task(data, task_id)
        if not force:
            blockers = blocking_dependencies(data, task)
            if blockers:
                raise WritError(
                    f"{task_id} is blocked by incomplete dependencies: "
                    f"{', '.join(blockers)} (use --force to override)"
                )
        run_id = new_run_id(task_id)
        directory = state.run_dir(root, run_id)
        directory.mkdir(parents=True, exist_ok=True)
        prompt = build_prompt(data, task, Path(root))
        (directory / "prompt.txt").write_text(prompt, encoding="utf-8")
        data["runs"][run_id] = {
            "id": run_id,
            "task": task_id,
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
        task["status"] = "running"
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
    with state.transaction(root) as data:
        run = data["runs"][run_id]
        run["status"] = "completed" if code == 0 else "failed"
        run["exit_code"] = code
        run["finished_at"] = utcnow()
        if note:
            run["note"] = note
        task = data["tasks"].get(run["task"])
        if task is not None and task["status"] == "running":
            # An agent exit is evidence, not a verdict: success moves the task to
            # `planned` awaiting acceptance sign-off, failure is recorded as failed.
            task["status"] = "planned" if code == 0 else "failed"
            task["updated_at"] = utcnow()
            add_evidence(
                task,
                f"run {run_id} finished with exit code {code}"
                + (f" ({note})" if note else ""),
            )
        refresh_milestones(data)


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
