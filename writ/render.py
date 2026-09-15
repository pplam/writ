"""Terminal rendering: plain tables, no dependencies, JSON when asked."""
from __future__ import annotations

import json
from typing import Any, Iterable, Sequence

STATUS_MARKS = {
    "planned": "·",
    "ready": ">",
    "running": "*",
    "blocked": "!",
    "completed": "+",
    "failed": "x",
    "cancelled": "-",
    "interrupted": "?",
    "in-progress": ">",
    "empty": "·",
    "starting": "*",
}


def emit_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))


def table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    materialized = [[str(cell) for cell in row] for row in rows]
    if not materialized:
        return "(none)"
    widths = [len(header) for header in headers]
    for row in materialized:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = ["  ".join(header.ljust(widths[i]) for i, header in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in materialized:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return "\n".join(lines)


def mark(status: str) -> str:
    return STATUS_MARKS.get(status, "?")


def bar(done: int, total: int, width: int = 24) -> str:
    if total <= 0:
        return "[" + " " * width + "] 0%"
    filled = round(width * done / total)
    percent = round(100 * done / total)
    return "[" + "#" * filled + " " * (width - filled) + f"] {percent}%"


def acceptance_line(index: int, item: dict[str, Any]) -> str:
    glyph = {"passed": "x", "failed": "!", "pending": " "}[item["status"]]
    return f"  {index}. [{glyph}] {item['text']}"
