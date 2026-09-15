"""The decision log: append-only records, mirrored to markdown."""
from __future__ import annotations

from typing import Any

from . import state
from .state import WritError, utcnow

MARKDOWN_HEADER = "# Decision Log\n\nAppend-only. Superseding entries are added, never edited.\n"


def add(
    data: dict[str, Any],
    *,
    title: str,
    decision: str,
    context: str = "",
    consequences: str = "",
    supersedes: str | None = None,
    tasks: list[str] | None = None,
) -> dict[str, Any]:
    if not title.strip():
        raise WritError("a decision needs a title")
    if not decision.strip():
        raise WritError("a decision needs a --decision statement")
    counters = data.setdefault("counters", {})
    number = counters.get("decision", len(data["decisions"])) + 1
    counters["decision"] = number
    if supersedes and not any(item["id"] == supersedes for item in data["decisions"]):
        raise WritError(f"unknown decision to supersede: {supersedes}")
    for task_id in tasks or []:
        if task_id not in data["tasks"]:
            raise WritError(f"unknown task: {task_id}")
    record = {
        "id": f"D-{number:04d}",
        "date": utcnow(),
        "title": title.strip(),
        "context": context.strip(),
        "decision": decision.strip(),
        "consequences": consequences.strip(),
        "supersedes": supersedes,
        "superseded_by": None,
        "tasks": tasks or [],
        "status": "active",
    }
    data["decisions"].append(record)
    if supersedes:
        for item in data["decisions"]:
            if item["id"] == supersedes:
                item["superseded_by"] = record["id"]
                item["status"] = "superseded"
    return record


def get(data: dict[str, Any], decision_id: str) -> dict[str, Any]:
    for item in data["decisions"]:
        if item["id"] == decision_id:
            return item
    raise WritError(f"unknown decision: {decision_id}")


def render_markdown(data: dict[str, Any]) -> str:
    parts = [MARKDOWN_HEADER]
    for item in data["decisions"]:
        parts.append(f"\n## {item['id']} — {item['title']}\n")
        parts.append(f"**Date:** {item['date']}  ")
        parts.append(f"**Status:** {item['status']}  ")
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
    return "\n".join(parts).rstrip() + "\n"


def sync_markdown(root, data: dict[str, Any]) -> None:
    """Keep the human-readable mirror in step with the JSON record."""
    state.decisions_file(root).write_text(render_markdown(data), encoding="utf-8")
