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


def test_no_style_attribute_survives_the_content_security_policy():
    """A style *attribute* is inline style, and the server's CSP forbids it.

    This is the bug this test exists for: `setAttribute('style', 'width:33%')`
    is dropped silently by the browser — no error, no console warning, the
    element simply renders unstyled. Every progress meter on the page rendered
    full regardless of actual progress, which is worse than rendering nothing,
    because a full bar is a plausible reading.

    Assigning through the CSSOM (`el.style.cssText = ...`) is not inline style
    as far as CSP is concerned, so the fix keeps the policy rather than relaxing
    it to 'unsafe-inline'. Enforced against the compiled bundle so a new view
    cannot reintroduce it.
    """
    body = (STATIC / "app.js").read_text()
    offenders = re.findall(r"setAttribute\(\s*['\"]style['\"]", body)
    assert offenders == [], (
        "setAttribute('style', ...) is dropped by our own CSP; "
        "assign el.style.cssText instead"
    )
    # The one permitted route, still present: the meters depend on it.
    assert "style.cssText" in body


def test_the_policy_the_bundle_is_written_against_is_the_one_served():
    """The two halves of that bug have to stay in agreement.

    If the CSP ever gains 'unsafe-inline', the test above becomes pointless
    ceremony; if style-src is dropped entirely, the stylesheet stops loading.
    Either change should land deliberately, not as a side effect.
    """
    from writ import server

    policy = server.PAGE  # the page itself carries no inline style either
    assert "style=" not in policy

    source = (Path(server.__file__)).read_text()
    assert "style-src 'self'" in source
    assert "unsafe-inline" not in source


def _luminance(hex_colour: str) -> float:
    """Relative luminance per WCAG 2.1."""
    value = hex_colour.lstrip("#")
    parts = [int(value[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    channels = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in parts]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast(a: str, b: str) -> float:
    first, second = _luminance(a), _luminance(b)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def _palette(block: str) -> dict[str, str]:
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{3,8});", block))


def _palettes() -> dict[str, dict[str, str]]:
    """The light and dark custom-property blocks, as name -> colour."""
    css = (STATIC / "style.css").read_text()
    dark_at = css.index("prefers-color-scheme: dark")
    return {"light": _palette(css[:dark_at]), "dark": _palette(css[dark_at:])}


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_text_tones_are_legible_on_the_surfaces_they_sit_on(scheme):
    """Text colours meet WCAG AA against panel and sunken, in both schemes.

    Measured rather than eyeballed, because eyeballing got this wrong twice: the
    dark palette carried `--faint: #75757040`, an 8-digit hex among 6-digit ones,
    which made the smallest labels 25% opaque and effectively invisible; and the
    light `--faint` was picked to look right on a white card and came out at
    2.8:1. These tones carry the labels that say what a number means, so they
    are the last thing that should be hard to read.
    """
    palette = _palettes()[scheme]
    for surface in ("--panel", "--sunken"):
        for tone in ("--ink", "--dim", "--faint"):
            ratio = _contrast(palette[tone], palette[surface])
            assert ratio >= 4.5, f"{scheme}: {tone} on {surface} is {ratio:.2f}:1"


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_status_colours_are_legible_on_their_own_washes(scheme):
    """Each status ink against its matching background wash.

    A pill pairs `--i-failed` with `--s-failed`, which is a different question
    from either against the panel, and the review pair was the one that failed.
    """
    palette = _palettes()[scheme]
    for status in ("running", "review", "completed", "failed"):
        ratio = _contrast(palette[f"--i-{status}"], palette[f"--s-{status}"])
        assert ratio >= 4.5, f"{scheme}: --i-{status} on --s-{status} is {ratio:.2f}:1"


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_every_palette_colour_is_opaque(scheme):
    """No alpha channel in the palette.

    The dark `--faint` regression was a single stray byte on the end of a hex
    literal, which no amount of reading catches but this does.
    """
    for name, colour in _palettes()[scheme].items():
        assert len(colour.lstrip("#")) in (3, 6), f"{scheme}: {name} = {colour}"
