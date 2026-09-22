"""Telling a broken machine from a rejected piece of work.

Every way a job could fail used to arrive at the same place: an `Outcome.error`
string, a task pushed back to the queue, and a rework attempt spent. That
flattened two unrelated things.

A reviewer rejecting an implementation is a *judgement*. It is the signal the
whole review loop is built to produce, and spending an attempt on it is exactly
right: the agent gets another go, and a task that cannot pass after N goes is a
task that needs a person.

A provider timing out, a subprocess failing to spawn, a state lock that could not
be taken — none of those are judgements about the work. Nobody looked at the
code. Charging them to the rework budget does two kinds of damage at once: the
task loses attempts it never used, and when it finally runs out, writ reports it
as having failed on technical merit. The record then says a reviewer rejected
work that no reviewer ever read.

So failures are classified, and the classification decides what happens:

    INFRASTRUCTURE   retry on its own budget, with backoff. Not the task's fault.
    UNAVAILABLE      infrastructure, but retrying cannot help — a missing agent
                     binary is missing on the next attempt too.
    TASK             the work itself did not come out right. Rework budget.
    REJECTION        a reviewer said no. Rework budget, which is the point of it.
    BLOCKED          waiting for a person. Neither budget; nothing to retry.
    INTERNAL         a bug in writ. Not retried — a deterministic exception
                     raises deterministically — but always recorded.

`classify` is deliberately conservative. An exception it does not recognise is
INTERNAL, never retryable: spending a budget on an unknown failure mode is how a
crash loop gets mistaken for patience.
"""
from __future__ import annotations

import errno
import random
import subprocess
import traceback
from dataclasses import dataclass, field
from typing import Any

from .state import WritError

INFRASTRUCTURE = "infrastructure"
UNAVAILABLE = "unavailable"
TASK = "task"
REJECTION = "rejection"
BLOCKED = "blocked"
INTERNAL = "internal"

#: how many times one job may be retried for infrastructure reasons, before the
#: task is reported as blocked on infrastructure rather than tried forever.
DEFAULT_MAX_INFRA_RETRIES = 2

#: the backoff schedule: first retry after 2s, then 6s, growing threefold. Short
#: enough that a transient provider hiccup costs a pause rather than a session,
#: long enough that a rate limit has a chance to lift.
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_FACTOR = 3.0
BACKOFF_CAP_SECONDS = 120.0

#: proportion of the delay to scatter randomly. Two workers that fail against the
#: same rate-limited provider in the same second must not come back in the same
#: second, or they reproduce the failure together.
BACKOFF_JITTER = 0.25


@dataclass(frozen=True)
class Failure:
    """One classified failure, in the form the record and the log both need."""

    category: str
    reason: str
    #: whether another attempt could plausibly succeed *without anything
    #: changing*. The only field the scheduler reads.
    retryable: bool = False
    #: exception type name, when the failure came from one
    exception: str = ""
    #: where it was raised — the last few frames, not the whole stack. Enough to
    #: find the line, short enough to live in `state.json`.
    where: list[str] = field(default_factory=list)

    @property
    def infrastructure(self) -> bool:
        return self.category in (INFRASTRUCTURE, UNAVAILABLE)

    @property
    def on_merit(self) -> bool:
        """Whether this failure is a statement about the work.

        The negation is what needs saying out loud: everything else failed
        *around* the task, and reporting it as a rejected implementation is the
        misinformation this module exists to prevent.
        """
        return self.category in (TASK, REJECTION)

    def to_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "category": self.category,
            "reason": self.reason,
            "retryable": self.retryable,
        }
        if self.exception:
            record["exception"] = self.exception
        if self.where:
            record["where"] = self.where
        return record

    @property
    def described(self) -> str:
        """One line, for the session log and the terminal.

        Leads with the category because the first thing a reader needs is whether
        to look at their code or their network.
        """
        if self.category == INFRASTRUCTURE:
            return f"infrastructure failure (not the task's fault): {self.reason}"
        if self.category == UNAVAILABLE:
            return f"agent unavailable: {self.reason}"
        if self.category == INTERNAL:
            return f"internal error in writ: {self.reason}"
        return self.reason


def classify(exc: BaseException) -> Failure:
    """What kind of failure this exception is.

    Only exceptions that escaped a worker reach here. The agent itself runs as a
    subprocess, so a provider's own errors arrive as an exit code and a
    transcript, not as a traceback — which is why this reads as a list of
    operating-system failures rather than a list of API failures.
    """
    where = _frames(exc)
    name = type(exc).__name__

    if isinstance(exc, WritError):
        return _classify_writ_error(exc, name, where)

    if isinstance(exc, subprocess.TimeoutExpired):
        # The agent was killed for running too long. Nothing was judged, and a
        # hang is frequently transient — a wedged provider connection, a machine
        # under load — so it is worth one more go on the infrastructure budget
        # rather than a rework attempt the task never used.
        return Failure(
            category=INFRASTRUCTURE,
            reason=f"the agent exceeded its timeout ({exc.timeout}s) and was killed",
            retryable=True,
            exception=name,
            where=where,
        )

    if isinstance(exc, FileNotFoundError):
        return Failure(
            category=UNAVAILABLE,
            reason=f"the agent command could not be found: {exc}",
            exception=name,
            where=where,
        )

    if isinstance(exc, PermissionError):
        return Failure(
            category=UNAVAILABLE,
            reason=f"permission denied running the agent: {exc}",
            exception=name,
            where=where,
        )

    if isinstance(exc, OSError):
        return Failure(
            category=INFRASTRUCTURE,
            reason=f"operating system failure: {exc}",
            # A retryable subset, named rather than assumed: out of memory, out
            # of file descriptors, a full disk, a dropped connection. Each is a
            # resource that another process may hand back.
            retryable=exc.errno in _TRANSIENT_ERRNOS,
            exception=name,
            where=where,
        )

    return Failure(
        category=INTERNAL,
        reason=f"{name}: {exc}",
        exception=name,
        where=where,
    )


_TRANSIENT_ERRNOS = frozenset(
    code
    for code in (
        getattr(errno, name, None)
        for name in (
            "EAGAIN",
            "EBUSY",
            "ECONNABORTED",
            "ECONNRESET",
            "EDEADLK",
            "EINTR",
            "EMFILE",
            "ENFILE",
            "ENOBUFS",
            "ENOLCK",
            "ENOMEM",
            "ENOSPC",
            "EPIPE",
            "ETIMEDOUT",
        )
    )
    if code is not None
)


#: phrases in a WritError that mean writ's own machinery could not proceed,
#: rather than that the work was unacceptable. Matched on the message because
#: WritError is one type carrying every user-facing failure; the alternative is a
#: subclass per case, which is a larger change than this fix should make.
_RETRYABLE_MARKERS = (
    "timed out waiting for the state lock",
)

_UNAVAILABLE_MARKERS = (
    "agent command not found",
    "no agent command",
)


def _classify_writ_error(
    exc: WritError, name: str, where: list[str]
) -> Failure:
    message = str(exc)
    lowered = message.lower()
    if any(marker in lowered for marker in _RETRYABLE_MARKERS):
        return Failure(
            category=INFRASTRUCTURE,
            reason=message,
            retryable=True,
            exception=name,
            where=where,
        )
    if any(marker in lowered for marker in _UNAVAILABLE_MARKERS):
        return Failure(
            category=UNAVAILABLE,
            reason=message,
            exception=name,
            where=where,
        )
    # Everything else a WritError says is about this task: a dependency that is
    # not complete, a status that does not allow the transition, a verdict writ
    # refused. Those do not get better by being retried.
    return Failure(category=TASK, reason=message, exception=name, where=where)


def _frames(exc: BaseException, limit: int = 3) -> list[str]:
    """The innermost frames, as `file:line in function`.

    The traceback is where the failure happened, and it is the one thing a
    stranded run cannot reconstruct later. Kept to three frames: enough to name
    the line, short enough that a project's state file does not become a log.
    """
    tb = exc.__traceback__
    if tb is None:
        return []
    frames = traceback.extract_tb(tb)[-limit:]
    return [
        f"{frame.filename}:{frame.lineno} in {frame.name}" for frame in frames
    ]


def backoff(attempt: int) -> float:
    """How long to wait before infrastructure retry `attempt` (1-based).

    Exponential with jitter, capped. The jitter is applied as a *reduction* from
    the nominal delay so the schedule stays predictable enough to test: attempt 1
    waits between 1.5s and 2s, never longer than the cap.
    """
    nominal = min(
        BACKOFF_BASE_SECONDS * (BACKOFF_FACTOR ** max(0, attempt - 1)),
        BACKOFF_CAP_SECONDS,
    )
    return nominal * (1.0 - random.random() * BACKOFF_JITTER)


def idempotency_key(task_id: str, role: str, attempt: int, infra_attempt: int) -> str:
    """A stable name for one logical attempt at one job.

    An infrastructure retry re-runs work that may have partly happened — a lock
    that timed out *after* the agent finished leaves a completed run nobody
    recorded. The key is what lets the record say "these two runs are the same
    attempt", so a reader counting attempts is not misled and a later
    reconciliation has something to join on.
    """
    return f"{task_id}/{role}/{attempt}/{infra_attempt}"


#: the exit code writ assigns a run it killed for exceeding its timeout. Set by
#: `runner._timed_out`, and the one code that is writ's own statement rather than
#: the agent's.
TIMEOUT_EXIT_CODE = 124


def describe(record: dict[str, Any] | None) -> str:
    """One line for a failure read back out of the store.

    The record is the durable half of a `Failure`; this is how a reader turns it
    back into the sentence the log would have printed at the time.
    """
    if not record:
        return ""
    return Failure(
        category=str(record.get("category", "")),
        reason=str(record.get("reason", "")),
    ).described


def from_run(run: dict[str, Any]) -> Failure | None:
    """Classify a run that *finished* but produced no usable verdict.

    `classify` only sees exceptions, and the two most common infrastructure
    failures never raise one: a hung agent is killed by writ itself and returns
    124, and an agent that cannot reach a model exits 0 having printed nothing.
    Both arrived here as an ordinary unjudged run, which spent a rework attempt
    and eventually reported the task as having failed on technical merit — when
    no reviewer had read a line of it.

    None for everything else. An agent that ran, spoke and exited non-zero has
    reported for itself, and second-guessing that would be writ inventing a
    judgement it has no grounds for.
    """
    if run.get("verdict"):
        return None
    if run.get("exit_code") == TIMEOUT_EXIT_CODE:
        limit = run.get("timeout")
        bound = f" ({limit}s)" if limit else ""
        return Failure(
            category=INFRASTRUCTURE,
            reason=(
                f"the agent exceeded its timeout{bound} and was killed before it "
                "reported"
            ),
            retryable=True,
        )
    if run.get("no_output"):
        # Exit 0 and an empty transcript. Retrying cannot fix an unknown model
        # id or an expired credential, so this is UNAVAILABLE rather than
        # INFRASTRUCTURE — but it is still not a statement about the work, and
        # that is the distinction that was missing.
        return Failure(
            category=UNAVAILABLE,
            reason=(
                "the agent printed nothing at all, so it most likely never "
                "reached a model (unknown model id, missing provider "
                "credentials, or exhausted quota)"
            ),
        )
    return None
