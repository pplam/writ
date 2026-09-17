"""The committed bundle has to match the TypeScript it was built from.

`writ/static/` is checked in so that installing writ needs no node. That is worth
the tradeoff, but it introduces the one failure mode a build step normally rules
out: editing `ui/src` and shipping the previous bundle. A stale dashboard is
especially bad because it looks like it works.

So the build stamps its output with a hash of every source file, and this asserts
the stamp still matches. It needs no node itself — it hashes the same files the
same way — so contributors without a toolchain still get told.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "ui" / "src"
STATIC = ROOT / "writ" / "static"


def source_stamp() -> str:
    """Reimplements ui/build.mjs's `sourceHash` — same files, same order."""
    entries = []
    for path in sorted(SRC.rglob("*"), key=lambda p: str(p.relative_to(SRC))):
        if path.is_file() and path.suffix in (".ts", ".css"):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            entries.append(f"{path.relative_to(SRC).as_posix()}:{digest}")
    joined = "\n".join(entries)
    return hashlib.sha256(joined.encode()).hexdigest()[:12]


def test_the_committed_bundle_was_built_from_the_current_sources():
    stamped = re.match(r"/\* built from ui/src \((\w+)\) \*/", (STATIC / "app.js").read_text())
    assert stamped, "writ/static/app.js has no build stamp"
    assert stamped.group(1) == source_stamp(), (
        "writ/static is stale: rebuild it with `node ui/build.mjs`"
    )


def test_the_stylesheet_carries_the_same_stamp():
    script = (STATIC / "app.js").read_text().splitlines()[0]
    style = (STATIC / "style.css").read_text().splitlines()[0]
    assert script == style, "the two assets were built from different sources"


@pytest.mark.parametrize("name", ["app.js", "style.css"])
def test_the_assets_are_present_and_not_empty(name):
    """What `writ serve` refuses to start without."""
    assert (STATIC / name).stat().st_size > 1000


def test_the_bundle_is_a_single_scope_with_no_module_syntax():
    """Concatenation only works if nothing left expects a module loader."""
    body = (STATIC / "app.js").read_text()
    assert not re.search(r"^\s*import\s", body, re.M)
    assert not re.search(r"^\s*export\s", body, re.M)


def test_the_bundle_fetches_nothing_from_the_network():
    """Offline by design, and the CSP would block it anyway.

    Checked against fetch and element sources rather than the string "http",
    because the SVG namespace is a URL that is an identifier, not an address.
    """
    body = (STATIC / "app.js").read_text()
    remote = re.findall(r"""(?:fetch|EventSource|import|src\s*=|href\s*=)\s*\(?\s*['"`](https?://[^'"`]+)""", body)
    assert remote == [], remote
    assert "cdn" not in body.lower()
    # The only namespace URL that should appear, and it is never requested.
    assert set(re.findall(r"https?://[\w./-]+", body)) <= {"http://www.w3.org/2000/svg"}


def test_the_page_never_builds_dom_from_a_string():
    """XSS-safe by construction: a task title is text, never markup.

    Titles, summaries and evidence all come from an agent's output, so the one
    rule worth enforcing mechanically is that none of it is ever parsed as HTML.
    """
    body = (STATIC / "app.js").read_text()
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert forbidden not in body, forbidden
