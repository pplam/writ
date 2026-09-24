"""Feature contracts: named interfaces, and the edges they imply.

A feature says what it `provides` and what it `consumes`, one interface per
line, as `Name: what it is`. The dependency graph is derived from those lines
rather than written by hand: a feature depends on whoever provides what it
consumes. That turns "is an edge missing?" from a judgement a critic has to make
into a lookup writ can do, and one check replaces two — every consumed interface
has exactly one provider.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping


def name(line: str) -> str:
    """The interface a contract line names: the text before the first colon.

    Compared case-insensitively and with whitespace collapsed, so `EventLog`
    and `eventlog ` are the same interface and a line with no colon names
    itself.
    """
    head = str(line).split(":", 1)[0]
    return " ".join(head.split()).lower()


def names(lines: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for line in lines or ():
        key = name(line)
        if key and key not in seen:
            seen.append(key)
    return seen


def providers(features: Mapping[str, Mapping[str, Any]]) -> dict[str, list[str]]:
    """Every interface name, mapped to the features that provide it."""
    found: dict[str, list[str]] = {}
    for feature_id in sorted(features):
        for key in names(features[feature_id].get("provides") or ()):
            found.setdefault(key, []).append(feature_id)
    return found


def edges(features: Mapping[str, Mapping[str, Any]]) -> dict[str, list[str]]:
    """What each feature depends on, from its contracts alone.

    An interface nobody provides, or one that several provide, yields no edge;
    `plancheck.check_contracts` reports both. A feature consuming something it
    provides itself is not an edge either.
    """
    by_name = providers(features)
    derived: dict[str, list[str]] = {}
    for feature_id in sorted(features):
        deps: list[str] = []
        for key in names(features[feature_id].get("consumes") or ()):
            owners = by_name.get(key, [])
            if len(owners) != 1 or owners[0] == feature_id:
                continue
            if owners[0] not in deps:
                deps.append(owners[0])
        derived[feature_id] = deps
    return derived


def is_feature(task: Mapping[str, Any]) -> bool:
    """Whether a task was planned as a feature, with contracts and a component."""
    return any(key in task for key in ("owns", "provides", "consumes", "goal"))


def fence(owns: Iterable[str], test_dirs: Iterable[str]) -> list[str]:
    """What a feature may touch: the component it owns, and where tests go."""
    fenced: list[str] = []
    for path in list(owns or ()) + list(test_dirs or ()):
        path = str(path).strip()
        if path and path not in fenced:
            fenced.append(path)
    return fenced
