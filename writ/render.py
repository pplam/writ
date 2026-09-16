"""Terminal rendering: plain tables, no dependencies, JSON when asked."""
from __future__ import annotations

import json
from typing import Any, Iterable, Sequence

STATUS_MARKS = {
    "planned": "·",
    "ready": ">",
    "running": "*",
    "reviewing": "*",
    "awaiting-review": "?",
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


def acceptance_detail(index: int, item: dict[str, Any]) -> list[str]:
    """An acceptance criterion with the evidence and the judge behind it.

    A bare checkmark is not much use when an agent set it: the value is in who
    claimed it and what they ran, so both are shown under the line.
    """
    lines = [acceptance_line(index, item)]
    judge = item.get("judged_by")
    if judge:
        lines.append(f"        judged by {judge}")
    evidence = item.get("evidence")
    if evidence:
        for number, chunk in enumerate(str(evidence).splitlines()):
            prefix = "        evidence: " if number == 0 else "                  "
            lines.append(f"{prefix}{chunk}")
    return lines


# --------------------------------------------------------------------------
# dependency graph


def dag_levels(tasks: dict[str, Any]) -> list[list[str]]:
    """Group tasks into dependency levels.

    A task's level is one past the deepest level among its dependencies, so
    everything in a level can run at once given the levels before it. This is the
    parallelism the DAG allows, which an adjacency list does not show.
    """
    level: dict[str, int] = {}

    def depth(task_id: str) -> int:
        if task_id in level:
            return level[task_id]
        deps = [d for d in tasks[task_id].get("depends_on", []) if d in tasks]
        level[task_id] = 1 + max((depth(d) for d in deps), default=-1)
        return level[task_id]

    for task_id in tasks:
        depth(task_id)
    grouped: dict[int, list[str]] = {}
    for task_id, value in level.items():
        grouped.setdefault(value, []).append(task_id)
    return [sorted(grouped[key]) for key in sorted(grouped)]


def dag_tree(
    tasks: dict[str, Any],
    *,
    label: Any,
    roots: Sequence[str] | None = None,
) -> list[str]:
    """Render a DAG as a tree, following dependencies forwards.

    A DAG is not a tree: a task can be reached by several paths, and printing its
    subtree under each one would multiply the graph and imply work happens more
    than once. So each task is expanded exactly once, beneath the dependency that
    comes last in topological order — the one that actually gates it — and every
    other appearance is a reference back to it.
    """
    dependents: dict[str, list[str]] = {task_id: [] for task_id in tasks}
    for task_id, task in tasks.items():
        for dep in task.get("depends_on", []):
            if dep in dependents:
                dependents[dep].append(task_id)

    order = {task_id: i for i, task_id in enumerate(_topological(tasks))}

    # Where each task gets expanded: under its latest-ordered dependency, so it
    # appears after everything it waits on rather than before some of it.
    home: dict[str, str] = {}
    for task_id, task in tasks.items():
        deps = [d for d in task.get("depends_on", []) if d in tasks]
        if deps:
            home[task_id] = max(deps, key=lambda d: (order.get(d, 0), d))

    if roots is None:
        roots = sorted(t for t in tasks if not [
            d for d in tasks[t].get("depends_on", []) if d in tasks
        ])

    lines: list[str] = []

    def children(task_id: str) -> list[str]:
        return sorted(
            child for child in dependents[task_id] if home.get(child) == task_id
        )

    def references(task_id: str) -> list[str]:
        """Dependents gated elsewhere: named here, expanded there."""
        return sorted(
            child for child in dependents[task_id] if home.get(child) != task_id
        )

    def walk(task_id: str, prefix: str, connector: str) -> None:
        lines.append(f"{prefix}{connector}{label(task_id)}")
        deeper = prefix + ("   " if connector and connector[0] == "└" else
                           "│  " if connector else "")
        kids = children(task_id)
        refs = references(task_id)
        entries: list[tuple[str, bool]] = [(k, False) for k in kids]
        entries += [(r, True) for r in refs]
        for index, (child, is_reference) in enumerate(entries):
            last = index == len(entries) - 1
            branch = "└─ " if last else "├─ "
            if is_reference:
                lines.append(f"{deeper}{branch}↩ {child}")
            else:
                walk(child, deeper, branch)

    for index, root in enumerate(roots):
        if index:
            lines.append("")
        walk(root, "", "")
    return lines


def _topological(tasks: dict[str, Any]) -> list[str]:
    """Dependency order, stable across runs so the drawing does not shuffle."""
    seen: set[str] = set()
    out: list[str] = []

    def visit(task_id: str) -> None:
        if task_id in seen:
            return
        seen.add(task_id)
        for dep in sorted(tasks[task_id].get("depends_on", [])):
            if dep in tasks:
                visit(dep)
        out.append(task_id)

    for task_id in sorted(tasks):
        visit(task_id)
    return out
