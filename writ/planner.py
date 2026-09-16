"""Structural extraction of a milestone/task DAG from a design document.

This is the deterministic fallback behind `writ plan --extract`. The parser is
intentionally simple and predictable: headings become milestones, optional
sub-headings become tasks, and acceptance criteria are lifted from explicit gate
markers (`**Pass:**`, `**Gate:**`, `Acceptance:`) or from bullet lists under an
acceptance heading. Anything it cannot find, it says so — it never invents a bar
that the document did not state.

The richer, judged plan comes from `planning.py`, which asks a coding agent to
produce the same shapes. Both paths share `PlannedMilestone`/`PlannedTask` and
`build_ids`, so everything downstream is identical.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .state import WritError

GATE_PATTERN = re.compile(
    r"\*{0,2}(?:pass|gate|acceptance|acceptance criteria|stage\s*\d*\s*gate)\*{0,2}\s*:\*{0,2}\s*(.+)",
    re.IGNORECASE,
)
BULLET_PATTERN = re.compile(r"^\s*[-*+]\s+(.*\S)\s*$")
EMPHASIS_PATTERN = re.compile(r"[*_`]+")
LINK_PATTERN = re.compile(r"\[([^\]]+)\]\([^)]*\)")

GENERIC_ACCEPTANCES = (
    "Behavior specified by this section is implemented",
    "A failing test preceded the implementation and now passes",
    "Project build, tests, and lint pass",
    "Agent reported assumptions, deviations, and remaining risks",
)


@dataclass
class PlannedTask:
    title: str
    acceptances: list[str]
    section: str
    body: str = ""
    #: id the plan's author used, for resolving intra-plan dependencies
    ref: str | None = None
    notes: str = ""
    depends_on: list[str] = field(default_factory=list)
    allowed: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    #: whether `section` was claimed by the author or synthesized from titles
    stated_section: bool = False


@dataclass
class PlannedMilestone:
    title: str
    section: str
    tasks: list[PlannedTask] = field(default_factory=list)
    ref: str | None = None
    notes: str = ""


def clean(text: str) -> str:
    """Strip markdown decoration from a heading or bullet."""
    text = LINK_PATTERN.sub(r"\1", text)
    text = EMPHASIS_PATTERN.sub("", text)
    return text.strip().rstrip(":").strip()


def split_sentences(text: str) -> list[str]:
    """Split a gate line into individual, independently checkable bars."""
    parts = re.split(r";\s+|\.\s+(?=[A-Z0-9])|\.\s*$", text)
    return [clean(part) for part in parts if clean(part)]


def _headings(lines: list[str], level: int) -> list[tuple[int, str]]:
    prefix = "#" * level + " "
    found = []
    fenced = False
    for index, line in enumerate(lines):
        if line.startswith("```"):
            fenced = not fenced
        if fenced:
            continue
        if line.startswith(prefix):
            found.append((index, clean(line[len(prefix):])))
    return found


def _section_bounds(
    lines: list[str], start: int, level: int
) -> int:
    """Return the line index where a section of the given level ends."""
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.startswith("#"):
            hashes = len(line) - len(line.lstrip("#"))
            if hashes <= level and line[hashes : hashes + 1] == " ":
                return index
    return len(lines)


def extract_acceptances(body: str) -> list[str]:
    """Pull explicit acceptance bars out of a section body."""
    found: list[str] = []
    lines = body.splitlines()
    for index, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        match = GATE_PATTERN.match(line.lstrip("-*+ ").strip())
        if not match:
            continue
        trailing = match.group(1).strip()
        if trailing:
            found.extend(split_sentences(trailing))
        else:
            # gate stated as a heading followed by bullets
            for following in lines[index + 1 :]:
                bullet = BULLET_PATTERN.match(following)
                if bullet:
                    found.append(clean(bullet.group(1)))
                elif following.strip():
                    break
    # de-duplicate while preserving order
    seen: set[str] = set()
    unique = []
    for item in found:
        key = item.lower()
        if key not in seen and len(item) > 3:
            seen.add(key)
            unique.append(item)
    return unique


def parse(
    text: str,
    *,
    milestone_level: int = 2,
    split_subsections: bool = True,
) -> list[PlannedMilestone]:
    """Parse a markdown design document into milestones and tasks."""
    lines = text.splitlines()
    heads = _headings(lines, milestone_level)
    if not heads:
        raise WritError(
            f"no level-{milestone_level} headings found; "
            "try --level 1 or point at a document with `##` sections"
        )
    milestones: list[PlannedMilestone] = []
    for position, (line_index, title) in enumerate(heads):
        end = (
            heads[position + 1][0]
            if position + 1 < len(heads)
            else len(lines)
        )
        body_lines = lines[line_index + 1 : end]
        body = "\n".join(body_lines)
        milestone = PlannedMilestone(title=title, section=title)
        subheads = _headings(body_lines, milestone_level + 1) if split_subsections else []
        if subheads:
            for sub_position, (sub_index, sub_title) in enumerate(subheads):
                sub_end = (
                    subheads[sub_position + 1][0]
                    if sub_position + 1 < len(subheads)
                    else len(body_lines)
                )
                sub_body = "\n".join(body_lines[sub_index + 1 : sub_end])
                milestone.tasks.append(
                    PlannedTask(
                        title=sub_title,
                        acceptances=extract_acceptances(sub_body)
                        or list(GENERIC_ACCEPTANCES),
                        section=f"{title} / {sub_title}",
                        body=sub_body,
                        stated_section=True,
                    )
                )
        else:
            milestone.tasks.append(
                PlannedTask(
                    title=title,
                    acceptances=extract_acceptances(body) or list(GENERIC_ACCEPTANCES),
                    section=title,
                    body=body,
                    stated_section=True,
                )
            )
        milestones.append(milestone)
    return milestones


def section_text(doc: Path, section: str) -> str:
    """Re-read one section of the design doc, for prompt construction."""
    if not doc.exists():
        return ""
    wanted = section.split(" / ")[-1]
    lines = doc.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("#"):
            continue
        level = len(line) - len(line.lstrip("#"))
        if clean(line[level:]) == wanted:
            end = _section_bounds(lines, index, level)
            return "\n".join(lines[index:end]).strip()
    return ""


def build_ids(
    milestones: list[PlannedMilestone], offset: int = 0
) -> list[tuple[str, PlannedMilestone, list[tuple[str, PlannedTask]]]]:
    """Assign stable `M01` / `M01-001` identifiers.

    Writ owns identity, not the plan's author: whatever ids a generated plan
    proposed are kept only as a translation table (see `ref_map`) so its stated
    dependencies can be rewritten onto the ids we actually assign.
    """
    result = []
    for position, milestone in enumerate(milestones, start=offset + 1):
        milestone_id = f"M{position:02d}"
        tasks = [
            (f"{milestone_id}-{index:03d}", task)
            for index, task in enumerate(milestone.tasks, start=1)
        ]
        result.append((milestone_id, milestone, tasks))
    return result


def ref_map(
    built: list[tuple[str, PlannedMilestone, list[tuple[str, PlannedTask]]]]
) -> dict[str, str]:
    """Map the plan author's task ids onto the ids Writ assigned."""
    return {
        task.ref: task_id
        for _, _, tasks in built
        for task_id, task in tasks
        if task.ref
    }


def summarize(milestones: list[PlannedMilestone]) -> dict[str, Any]:
    return {
        "milestones": len(milestones),
        "tasks": sum(len(milestone.tasks) for milestone in milestones),
        "acceptances": sum(
            len(task.acceptances)
            for milestone in milestones
            for task in milestone.tasks
        ),
    }
