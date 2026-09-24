"""One shape for every agent prompt: say what exists and where, not what it says.

Agents can read files. A prompt that pastes the plan, the findings and three
analyses in full costs the agent its attention on material it did not need,
and got to 240KB on one real plan. So a prompt names the repository root once,
lists the files to read (first, and as needed) with one line on what each is
for, states the brief, and names the exact path to write.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .planfiles import rel
from .planner import DesignDocs, doc_list


@dataclass(frozen=True)
class Ref:
    """A file or directory the agent may read, and why."""

    path: str | os.PathLike[str]
    purpose: str


def root_line(root: str | os.PathLike[str]) -> str:
    return (
        f"Repository root: {Path(root).resolve()} — every path below is relative "
        "to it."
    )


def references(
    root: str | os.PathLike[str],
    *,
    first: Iterable[Ref] = (),
    as_needed: Iterable[Ref] = (),
) -> list[str]:
    """The "Read first" / "Read as needed" lists, paths relative to `root`."""
    lines: list[str] = []
    for heading, refs in (
        ("Read first:", list(first)),
        ("Read as needed:", list(as_needed)),
    ):
        if not refs:
            continue
        lines.append(heading)
        lines.extend(f"  - {rel(root, ref.path)} — {ref.purpose}" for ref in refs)
        lines.append("")
    return lines


def output(
    root: str | os.PathLike[str], what: str, path: str | os.PathLike[str]
) -> list[str]:
    """The write instruction. Its wording is what test fakes and agents key on."""
    return [f"Write your {what} as JSON to this exact path:", f"  {rel(root, path)}"]


def design_refs(doc: DesignDocs, purpose: str) -> list[Ref]:
    """One `Ref` per design document, numbered when the design is split."""
    docs = doc_list(doc)
    if len(docs) <= 1:
        return [Ref(path, purpose) for path in docs]
    return [
        Ref(path, f"{purpose}, part {index} of {len(docs)}")
        for index, path in enumerate(docs, start=1)
    ]


def design_note(doc: DesignDocs) -> list[str]:
    """What an agent needs told when the design spans several documents."""
    docs = doc_list(doc)
    if len(docs) <= 1:
        return []
    return [
        f"The design is split across {len(docs)} documents. Read every one: "
        "together they are one design, and one requirement may draw on more than "
        "one of them. Cite a heading as it appears; where two documents share a "
        "heading, cite it as `<file name> / <heading>`.",
        "",
    ]


def other_docs(registered: Iterable[str], doc: DesignDocs) -> list[str]:
    """Registered design documents that are not part of this design."""
    ours = {str(path.resolve()) for path in doc_list(doc)}
    return [path for path in registered if str(Path(path).resolve()) not in ours]
