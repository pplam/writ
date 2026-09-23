"""The record of a planning attempt, written while it is still happening.

Everything else writ records is written after the fact. A run record is closed
when the agent exits, a pipeline record is written inside the commit that ends
planning, a finding exists because a check already ran. That is the right shape
for a record of what happened, and the wrong shape for watching.

The plan phase is where that hurts. `writ plan --critics --repair` runs four to
fourteen agents before a single task is dispatched, and until the commit at the
end **nothing at all is written to `state.json`** — the analyses and the synthesis
produce files on disk and no state. So `writ serve`, which pushes a snapshot when
`state.json` changes, has nothing to say for the entire time: the one stretch a
reader most wants to watch is the one stretch the dashboard is blank. The terminal
shows it, because the terminal is where the agent's output is being mirrored. A
second window cannot.

This is that record. One entry per planning attempt, holding one step per agent
writ intends to run, written as each step starts and finishes.

**Declared before it runs.** The step list is built up front, from the flags: which
stages were chosen, which critics, whether repair is on, whether approval is
automatic. So a step is visible as *pending* before anything has happened to it,
and the page can show what is about to occur rather than boxes appearing from
nowhere. The exceptions are the steps that genuinely cannot be known in advance —
a repair round only exists if the critics objected, and re-reviewing a patched plan
is a second pass of critics at a new revision — and those are appended as they
open, which is the honest representation of work that was not foreseen.

**Waves are columns.** A step's `wave` comes from `analysis.waves` and
`critics.waves`, the functions that already decide what may run at once. The graph
is therefore those rules drawn, not a second opinion about them that is free to
disagree.

**Bookkeeping never fails a plan.** Every mutation here is wrapped: a write that
cannot land is dropped with a note on stderr. Writ spent four agent runs getting
to that point, and losing them because a progress record could not be updated
would be the tail wagging the dog.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import procs, state
from .state import WritError, utcnow

#: how many attempts to keep. A phase carries a step per agent, so it is an order
#: of magnitude larger than a `plans` entry; keeping every attempt a long-lived
#: project ever made would grow `state.json` without bound, and the value of an
#: old one is in its transcripts on disk, which are not pruned with it.
KEEP = 10

#: what a step is, in the order the phase runs them. `commit` and `approval` are
#: writ's own work rather than an agent's, and are steps anyway: a plan held at
#: needs-approval is the single most common end state, and a reader looking for
#: why should find the approval step saying it declined rather than find nothing.
KINDS = ("stage", "synthesis", "commit", "critic", "repair", "approval")

#: `reused` is not a kind of success, it is the absence of a run: the artifact was
#: already on disk. `skipped` is a step that was declared and then not reached —
#: approval when the plan was never committed, repair when nothing was blocking.
STATUSES = ("pending", "running", "ok", "reused", "failed", "skipped", "abandoned")

#: statuses a step can still leave on its own.
LIVE = ("pending", "running")

#: what an attempt as a whole comes to. `stopped` is `--stage requirements` and
#: `--dry-run`: writ did what it was asked and deliberately went no further, which
#: is not a failure and not a finished plan either.
PHASE_STATUSES = ("running", "done", "failed", "stopped", "abandoned")


def records(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Every planning attempt this project has on record, oldest first.

    Installs the key when it is missing, because `begin` appends to what this
    returns and a list it made privately would be thrown away. `state.load`
    defaults it too, so in practice this only fires on a document assembled in a
    test — but `writ/api.py` reads through here as well, and a read model that
    mutates its input is the kind of thing that stays harmless right up until it
    is not. So the read path asks for `phases` directly.
    """
    found = data.get("phases")
    if not isinstance(found, list):
        found = []
        data["phases"] = found
    return found


def current(data: dict[str, Any]) -> dict[str, Any] | None:
    """The attempt a reader is asking about: the most recent one.

    Read-only, deliberately: `writ/api.py` calls this on every snapshot it serves,
    and `writ serve` must not be able to touch the document it is reading.
    """
    found = data.get("phases")
    if not isinstance(found, list) or not found:
        return None
    return found[-1]


def step(phase: dict[str, Any], step_id: str) -> dict[str, Any] | None:
    for entry in phase.get("steps", []):
        if entry.get("id") == step_id:
            return entry
    return None


# --------------------------------------------------------------------------
# declaring the shape of the attempt


def declare(
    *,
    stages: Iterable[Any] = (),
    parallel_stages: bool = False,
    synthesis: bool = True,
    critics: Sequence[Any] = (),
    parallel_critics: bool = False,
    repair: bool = False,
    auto_approve: bool = False,
) -> list[dict[str, Any]]:
    """The steps this attempt intends to run, in order, with their waves.

    Takes the same objects the pipeline takes — `analysis.Stage`s and
    `critics.Critic`s — and asks them, through `waves`, what may run together.
    Imported here rather than at module scope because `analysis` imports plenty
    and this module is imported by `api`, which the server loads on every request.
    """
    from . import analysis, critics as critic_module

    steps: list[dict[str, Any]] = []
    wave = 0
    previous: list[str] = []

    stages = list(stages)
    if stages:
        grouped = (
            analysis.waves(stages) if parallel_stages else [[s] for s in stages]
        )
        for group in grouped:
            landed = []
            for stage in group:
                landed.append(
                    _step(
                        steps,
                        id=f"stage:{stage.name}",
                        kind="stage",
                        name=stage.name,
                        summary=stage.summary,
                        wave=wave,
                        depends_on=previous,
                    )
                )
            previous = landed
            wave += 1

    if synthesis:
        previous = [
            _step(
                steps,
                id="synthesis",
                kind="synthesis",
                name="synthesis",
                summary=(
                    "decompose the work into milestones, tasks and acceptance "
                    "criteria"
                    if stages
                    else "plan the work from the design document in one pass"
                ),
                wave=wave,
                depends_on=previous,
            )
        ]
        wave += 1

    previous = [
        _step(
            steps,
            id="commit",
            kind="commit",
            name="commit",
            summary="validate the plan and write it into project state",
            wave=wave,
            depends_on=previous,
        )
    ]
    wave += 1

    chosen = list(critics)
    if chosen:
        grouped = (
            critic_module.waves(chosen) if parallel_critics else [[c] for c in chosen]
        )
        for group in grouped:
            landed = []
            for critic in group:
                landed.append(
                    _step(
                        steps,
                        id=f"critic:{critic.name}",
                        kind="critic",
                        name=critic.name,
                        summary=critic.brief,
                        wave=wave,
                        depends_on=previous,
                    )
                )
            previous = landed
            wave += 1

    if repair:
        # One placeholder, because whether *any* repair happens depends on what the
        # critics find, and how many rounds depends on what each patch fixes. The
        # rounds that do happen are appended by `add` as they open; this is the
        # declaration that repair is armed, which is worth showing before it is
        # known whether it will be needed.
        previous = [
            _step(
                steps,
                id="repair",
                kind="repair",
                name="repair",
                summary="answer the plan's blocking findings, if any stand",
                wave=wave,
                depends_on=previous,
            )
        ]
        wave += 1

    if auto_approve:
        _step(
            steps,
            id="approval",
            kind="approval",
            name="approval",
            summary="approve the plan if nothing blocking stands against it",
            wave=wave,
            depends_on=previous,
        )
    return steps


def make_step(
    *,
    id: str,
    kind: str,
    name: str,
    summary: str = "",
    wave: int = 0,
    note: str = "",
    depends_on: Sequence[str] = (),
) -> dict[str, Any]:
    """One step, with every key a reader expects present.

    Public because a step can also be born mid-phase — a repair round, a critic
    re-reading a patched plan — and one built by hand from a subset of these keys
    would reach the dashboard missing the fields it reads on every step. Every step
    on the record has the same shape whether it was foreseen or not.
    """
    return {
        "id": id,
        "kind": kind,
        "name": name,
        "summary": summary,
        "wave": wave,
        "status": "pending",
        "command": [],
        "display": "",
        "model": "",
        "directory": "",
        "artifact": "",
        "event_shape": "",
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "error": "",
        "note": note,
        "depends_on": list(depends_on),
    }


def _step(steps: list[dict[str, Any]], **kwargs: Any) -> str:
    """Append one declared step and return its id, for the next wave's edges."""
    entry = make_step(**kwargs)
    steps.append(entry)
    return str(entry["id"])


# --------------------------------------------------------------------------
# writing it as it happens


def begin(
    root: Path,
    *,
    doc: str,
    plan_id: str,
    steps: Sequence[dict[str, Any]],
    label: str = "",
) -> str | None:
    """Open a phase record and return its id.

    `owner` is this process's identity, not just its pid. It is what lets a reader
    tell a plan that is still working from one that was killed: a step left
    `running` by a process this machine can prove is gone is reported `abandoned`
    rather than shown as live forever. Same reasoning as a run record's owner, and
    the same `procs` comparison behind it.
    """
    try:
        with state.transaction(root) as data:
            found = records(data)
            phase_id = _mint(found, plan_id)
            found.append(
                {
                    "id": phase_id,
                    "plan_id": plan_id,
                    "doc": doc,
                    "label": label,
                    "status": "running",
                    "owner": procs.identify().to_dict(),
                    "started_at": utcnow(),
                    "finished_at": None,
                    "steps": [dict(entry) for entry in steps],
                }
            )
            # Oldest first, so trimming the front keeps the recent ones.
            del found[: max(0, len(found) - KEEP)]
    except WritError as exc:
        _complain(exc)
        return None
    return phase_id


def start_step(
    root: Path,
    phase_id: str | None,
    step_id: str,
    *,
    resolved: Any = None,
    directory: Path | str = "",
    artifact: Path | str = "",
) -> None:
    """Mark a step running, with the command that is running it.

    The command is the point. A stage that hung or wrote nothing is diagnosed by
    running its own invocation by hand, and each step may resolve its own agent,
    model and event flags — so the page shows `display`, exactly as the terminal
    header does, rather than an agent name nobody can paste.
    """
    def apply(entry: dict[str, Any]) -> None:
        entry["status"] = "running"
        entry["started_at"] = utcnow()
        if resolved is not None:
            entry["command"] = list(getattr(resolved, "command", []) or [])
            entry["display"] = str(getattr(resolved, "display", "") or "")
            entry["model"] = str(getattr(resolved, "model", "") or "")
            entry["event_shape"] = str(getattr(resolved, "event_shape", "") or "")
        if directory:
            entry["directory"] = str(directory)
        if artifact:
            entry["artifact"] = str(artifact)

    _edit(root, phase_id, step_id, apply)


def finish_step(
    root: Path,
    phase_id: str | None,
    step_id: str,
    *,
    status: str,
    exit_code: int | None = None,
    error: str = "",
    note: str = "",
    directory: Path | str = "",
    artifact: Path | str = "",
) -> None:
    """Record how a step ended.

    Tolerates a step that never started, because one case reaches here that way:
    a stage whose artifact was already on disk returns before `on_start` is
    reached, so its first and only news is that it was reused. Stamping a
    `started_at` here would invent a run that did not happen, so it is left unset
    and `reused` is what says why.
    """
    def apply(entry: dict[str, Any]) -> None:
        entry["status"] = status
        entry["finished_at"] = utcnow()
        if exit_code is not None:
            entry["exit_code"] = exit_code
        if error:
            entry["error"] = error
        if note:
            entry["note"] = note
        if directory:
            entry["directory"] = str(directory)
        if artifact:
            entry["artifact"] = str(artifact)

    _edit(root, phase_id, step_id, apply)


def add(
    root: Path,
    phase_id: str | None,
    steps: Sequence[dict[str, Any]],
    *,
    after: str = "",
) -> None:
    """Append steps nobody could have declared, and place them.

    Two things reach here, both consequences of what earlier steps found: a repair
    round, which exists only because the critics objected, and a re-review, which
    is the critics reading a plan that a patch has changed. Neither is foreseeable
    from the flags, and both are real agent runs that a reader watching the page
    must be able to see.

    `after` is the step they follow, which fixes their column: one past it, and
    everything already at or beyond that column shifts right to make room. A
    dynamically added step is therefore drawn where it happened in sequence rather
    than appended to the end of the graph.
    """
    if not steps:
        return

    def apply(phase: dict[str, Any]) -> None:
        existing = phase.setdefault("steps", [])
        known = {entry.get("id") for entry in existing}
        fresh = [dict(entry) for entry in steps if entry.get("id") not in known]
        if not fresh:
            return
        anchor = next(
            (entry for entry in existing if entry.get("id") == after), None
        )
        base = int(anchor.get("wave", 0)) + 1 if anchor else _next_wave(existing)
        span = 1 + max(int(entry.get("wave", 0)) for entry in fresh)
        for entry in existing:
            if int(entry.get("wave", 0)) >= base:
                entry["wave"] = int(entry["wave"]) + span
        for entry in fresh:
            entry["wave"] = base + int(entry.get("wave", 0))
        existing.extend(fresh)

    _edit_phase(root, phase_id, apply)


def resume(root: Path, phase_id: str | None) -> None:
    """Reopen a finished phase, because more of it is about to happen.

    `writ adjudicate` continues a planning attempt that has already closed: the loop
    stopped with findings open, a human settled them, and the rounds that follow
    belong to the same attempt. Adding a `running` step to a phase still marked
    `done` would describe something that cannot be true, and `describe` only
    corrects a dead owner on a phase that claims to be running — so a done phase
    with a live step in it would have shown that step spinning forever.

    The owner is retaken, since this process is now the one doing the work and is
    the one a reader should be able to prove alive or dead.
    """
    def apply(phase: dict[str, Any]) -> None:
        if phase.get("status") == "running":
            return
        phase["status"] = "running"
        phase["finished_at"] = None
        phase["owner"] = procs.identify().to_dict()

    _edit_phase(root, phase_id, apply)


def finish(
    root: Path,
    phase_id: str | None,
    *,
    status: str,
    note: str = "",
) -> None:
    """Close the phase, and settle whatever steps it never reached.

    Called from a `finally`, because every way out of `writ plan` has to reach it.
    A phase left `running` by an early return — `--stage requirements` stopping
    deliberately, a failed stage, an exception — would have the dashboard report a
    plan in flight minutes after the process exited, which is worse than showing
    nothing: nothing is honest about not knowing.

    A step still `pending` when the phase ends was declared and not reached, which
    is `skipped`. A step still `running` is a stronger claim and gets a different
    word: the phase is over and that step never reported, so something died inside
    it, which is `abandoned`. Both are settled here because a step nobody will ever
    write to again must not be left looking live — the page would spin a progress
    marker and poll its output forever.
    """
    def apply(phase: dict[str, Any]) -> None:
        phase["status"] = status
        phase["finished_at"] = utcnow()
        if note:
            phase["note"] = note
        for entry in phase.get("steps", []):
            if entry.get("status") == "pending":
                entry["status"] = "skipped"
            elif entry.get("status") == "running":
                entry["status"] = "abandoned"
                entry["finished_at"] = utcnow()

    _edit_phase(root, phase_id, apply)


# --------------------------------------------------------------------------
# reading it back


def describe(phase: dict[str, Any]) -> dict[str, Any]:
    """The phase as a reader should see it, with a dead owner accounted for.

    The stored status says what the last process to write said. This says what is
    true now, which differs in exactly one case and it is the case that matters:
    `writ plan` killed mid-step leaves `running` on the record, and a page that
    believed it would show a live agent, spin a progress marker and poll its output
    forever. `procs.confirmed_dead` is the same proof-first test `runner.reconcile`
    uses before reaping a run — an owner on another host is not declared dead by a
    machine with no standing to say so.
    """
    stored = str(phase.get("status", ""))
    live = stored in ("running",)
    dead = live and procs.confirmed_dead(phase.get("owner"))
    steps = []
    for entry in phase.get("steps", []):
        shown = dict(entry)
        if dead and shown.get("status") in LIVE:
            shown["status"] = (
                "abandoned" if shown.get("status") == "running" else "skipped"
            )
        steps.append(shown)
    return {
        "id": str(phase.get("id", "")),
        "plan_id": str(phase.get("plan_id", "")),
        "doc": str(phase.get("doc", "")),
        "label": str(phase.get("label", "")),
        "status": "abandoned" if dead else stored,
        "running": live and not dead,
        "started_at": str(phase.get("started_at") or ""),
        "finished_at": str(phase.get("finished_at") or ""),
        "note": str(phase.get("note", "")),
        "steps": steps,
    }


# --------------------------------------------------------------------------
# the plumbing


def _mint(found: list[dict[str, Any]], plan_id: str) -> str:
    """A fresh id, even when the plan id is one this project has seen before.

    `writ plan <doc> --plan-id <existing>` is the documented way to resume a
    pipeline, so one plan id can have several attempts against it: the first that
    failed, and the one that reused its artifacts. Naming them both `ph-<plan_id>`
    made the second attempt's writes land on the first attempt's record — every
    step of the live run stayed `pending` while a finished record beside it grew a
    second set of results.
    """
    taken = {str(entry.get("id", "")) for entry in found}
    base = f"ph-{plan_id}"
    if base not in taken:
        return base
    attempt = 2
    while f"{base}-{attempt}" in taken:
        attempt += 1
    return f"{base}-{attempt}"


def _next_wave(steps: Iterable[dict[str, Any]]) -> int:
    waves = [int(entry.get("wave", 0)) for entry in steps]
    return max(waves) + 1 if waves else 0


def _edit(
    root: Path, phase_id: str | None, step_id: str, apply: Any
) -> None:
    def on_phase(phase: dict[str, Any]) -> None:
        entry = step(phase, step_id)
        if entry is not None:
            apply(entry)

    _edit_phase(root, phase_id, on_phase)


def _edit_phase(root: Path, phase_id: str | None, apply: Any) -> None:
    """One transaction per change, and never inside somebody else's.

    Deliberately not folded into the caller's transaction, for two reasons. The
    state lock is not re-entrant — `_lock_flock` opens a fresh descriptor per
    call, so a nested acquisition spins for ten seconds and then raises — and a
    transaction that raises discards everything done inside it, which would lose
    the progress record exactly when a plan failed and someone wants to read it.
    """
    if not phase_id:
        return
    try:
        with state.transaction(root) as data:
            # Newest first. Ids are unique (see `_mint`), so this is only about
            # which end of the list the common case is at: the record being written
            # to is almost always the last one.
            for phase in reversed(records(data)):
                if phase.get("id") == phase_id:
                    apply(phase)
                    return
    except WritError as exc:
        _complain(exc)


def _complain(exc: Exception) -> None:
    """A record that could not be written is a note, not a failure.

    Reached when the state lock is held by another writ process for longer than
    it waits. The planning run continues: it has already spent the agent runs, and
    abandoning them because a progress marker could not be updated would make the
    bookkeeping more important than the work.
    """
    print(f"note: could not record planning progress: {exc}", file=sys.stderr)
