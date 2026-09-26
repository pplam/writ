"""The decision log: append-only records, mirrored to markdown.

Records are written by the agents that make the decisions, not by hand. An agent
that picks a data format or defines a behaviour the design left open has decided
something the next agent inherits, so it proposes a record as part of its verdict.

Proposals are not commitments. An agent may not silently commit the project to an
architectural position, so a proposed record is inert until a human confirms it —
the same split as acceptance criteria, where the agent claims and something else
decides.
"""
from __future__ import annotations

from typing import Any

from . import state
from .state import WritError, utcnow

MARKDOWN_HEADER = (
    "# Decision Log\n\n"
    "Append-only. Superseding entries are added, never edited.\n"
    "Proposed entries were recorded by an agent and are awaiting confirmation.\n"
)

STATUSES = ("proposed", "active", "superseded", "rejected")

#: what a human may set a decision to; `superseded` is a consequence of
#: confirming a replacement, not something you assert directly
SETTABLE_DECISION_STATUSES = ("active", "rejected")

#: the statement a decision carries while it waits for a person to rule. A
#: decision raised from a `needs-decision` finding is a question, not a proposal:
#: confirming this text as written would make "undecided" binding.
UNDECIDED = "Undecided: a critic found the plan needs a ruling here."

#: how every question's placeholder opens, whoever raised it: a critic, the
#: adjudicator, a gate or a repair planner
UNDECIDED_PREFIX = "Undecided:"

#: who confirms a decision writ made without a person (`decisions.autonomous`)
AUTONOMOUS = "autonomous"


def _next_id(data: dict[str, Any]) -> str:
    counters = data.setdefault("counters", {})
    number = counters.get("decision", len(data["decisions"])) + 1
    counters["decision"] = number
    return f"D-{number:04d}"


def propose(
    data: dict[str, Any],
    *,
    title: str,
    decision: str,
    context: str = "",
    consequences: str = "",
    proposed_by: str,
    tasks: list[str] | None = None,
) -> dict[str, Any]:
    """Record an agent's decision as `proposed`, pending human confirmation.

    Deliberately permissive about the task list: a verdict names the task it came
    from, and rejecting the whole proposal because that task was renamed would
    lose the content for no gain.
    """
    if not title.strip():
        raise WritError("a decision needs a title")
    if not decision.strip():
        raise WritError("a decision needs a statement")
    record = {
        "id": _next_id(data),
        "date": utcnow(),
        "title": title.strip(),
        "context": context.strip(),
        "decision": decision.strip(),
        "consequences": consequences.strip(),
        "supersedes": None,
        "superseded_by": None,
        "tasks": [t for t in (tasks or []) if t in data["tasks"]],
        "status": "proposed",
        "proposed_by": proposed_by,
        "confirmed_by": None,
        "confirmed_at": None,
    }
    data["decisions"].append(record)
    return record


def confirm(
    data: dict[str, Any],
    decision_id: str,
    *,
    supersedes: str | None = None,
    actor: str = "operator",
) -> dict[str, Any]:
    """Accept a proposed decision, making it binding on the project."""
    record = get(data, decision_id)
    if record["status"] != "proposed":
        raise WritError(
            f"{decision_id} is {record['status']}, not proposed "
            "(only a proposed decision can be confirmed)"
        )
    if supersedes:
        superseded = get(data, supersedes)
        if superseded["id"] == decision_id:
            raise WritError(f"{decision_id} cannot supersede itself")
        record["supersedes"] = supersedes
        superseded["superseded_by"] = decision_id
        superseded["status"] = "superseded"
    record["status"] = "active"
    record["confirmed_by"] = actor
    record["confirmed_at"] = utcnow()
    return record


def reject(
    data: dict[str, Any], decision_id: str, *, reason: str, actor: str = "operator"
) -> dict[str, Any]:
    """Turn down a proposed decision, keeping the record and the reason.

    The record stays: that an agent proposed something and it was turned down is
    itself worth knowing, and deleting it would invite the same proposal again.
    """
    record = get(data, decision_id)
    if record["status"] not in ("proposed", "active"):
        raise WritError(f"{decision_id} is already {record['status']}")
    record["status"] = "rejected"
    record["rejected_reason"] = reason.strip()
    record["confirmed_by"] = actor
    record["confirmed_at"] = utcnow()
    return record


def undecided(record: dict[str, Any]) -> bool:
    """Whether this decision is a question still waiting for its answer."""
    return record.get("decision", "").startswith(UNDECIDED_PREFIX)


def autonomous(data: dict[str, Any]) -> bool:
    """Whether writ makes decisions itself rather than stopping for a person.

    Stored in the state by the command that resolved it (`--autonomous`, else
    `decisions.autonomous` in the config), because what applies a verdict is
    several calls away from the arguments that said so.
    """
    return bool(data.get("autonomous"))


def answer(
    data: dict[str, Any], decision_id: str, ruling: str, *, actor: str = AUTONOMOUS
) -> dict[str, Any]:
    """Settle a question with its answer, keeping the question on the record."""
    record = get(data, decision_id)
    if not ruling.strip():
        raise WritError(f"{decision_id} needs an answer, not an empty one")
    if record["status"] != "proposed":
        raise WritError(f"{decision_id} is {record['status']}; its text is settled")
    record["decision"] = ruling.strip()
    return confirm(data, decision_id, actor=actor)


def decide(
    data: dict[str, Any],
    *,
    title: str,
    decision: str,
    context: str = "",
    consequences: str = "",
    proposed_by: str,
    tasks: list[str] | None = None,
) -> dict[str, Any]:
    """Record a decision writ made on its own: proposed and confirmed at once.

    Nothing is skipped on the way. It is the same record a person would have
    confirmed, marked `confirmed_by: autonomous`, so the log still says who
    proposed what, and `writ set D-NNNN rejected` still overturns it.
    """
    record = propose(
        data,
        title=title,
        decision=decision,
        context=context,
        consequences=consequences,
        proposed_by=proposed_by,
        tasks=tasks,
    )
    return confirm(data, record["id"], actor=AUTONOMOUS)


def ruling(data: dict[str, Any], finding_id: str) -> dict[str, Any] | None:
    """The active decision a person gave in answer to this finding, if any."""
    for item in data["decisions"]:
        if (
            item.get("finding") == finding_id
            and item["status"] == "active"
            and not undecided(item)
        ):
            return item
    return None


def asked(data: dict[str, Any], finding_id: str) -> dict[str, Any] | None:
    """The decision raised for this finding that nobody has answered yet."""
    for item in data["decisions"]:
        if item.get("finding") == finding_id and item["status"] == "proposed":
            return item
    return None


def proposed(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Decisions an agent recorded that nobody has confirmed yet."""
    return [item for item in data["decisions"] if item["status"] == "proposed"]


def get(data: dict[str, Any], decision_id: str) -> dict[str, Any]:
    for item in data["decisions"]:
        if item["id"] == decision_id:
            return item
    raise WritError(f"unknown decision: {decision_id}")


def render_markdown(data: dict[str, Any]) -> str:
    parts = [MARKDOWN_HEADER]
    for item in data["decisions"]:
        marker = " (proposed)" if item["status"] == "proposed" else ""
        parts.append(f"\n## {item['id']} — {item['title']}{marker}\n")
        parts.append(f"**Date:** {item['date']}  ")
        parts.append(f"**Status:** {item['status']}  ")
        if item.get("proposed_by"):
            parts.append(f"**Proposed by:** {item['proposed_by']}  ")
        if item.get("confirmed_by") and item["status"] == "active":
            parts.append(f"**Confirmed by:** {item['confirmed_by']}  ")
        if item.get("supersedes"):
            parts.append(f"**Supersedes:** {item['supersedes']}  ")
        if item.get("superseded_by"):
            parts.append(f"**Superseded by:** {item['superseded_by']}  ")
        if item.get("tasks"):
            parts.append(f"**Tasks:** {', '.join(item['tasks'])}  ")
        parts.append("")
        if item.get("context"):
            parts.append(f"**Context:** {item['context']}\n")
        parts.append(f"**Decision:** {item['decision']}\n")
        if item.get("consequences"):
            parts.append(f"**Consequences:** {item['consequences']}\n")
        if item.get("rejected_reason"):
            parts.append(f"**Rejected:** {item['rejected_reason']}\n")
    return "\n".join(parts).rstrip() + "\n"


def sync_markdown(root, data: dict[str, Any]) -> None:
    """Keep the human-readable mirror in step with the JSON record."""
    state.decisions_file(root).write_text(render_markdown(data), encoding="utf-8")
