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


def _blocks(css: str) -> list[tuple[str, str]]:
    """(selector list, declarations) for each rule, with comments stripped.

    Comments are removed first because several carry braces in prose, which is
    enough to make a naive selector pattern swallow the rule that follows.
    """
    bare = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [(m.group(1).strip(), m.group(2)) for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", bare)]


def _rule(selector: str) -> dict[str, str]:
    """The declarations of the first rule whose selector list contains one exactly."""
    css = (STATIC / "style.css").read_text()
    for block in _blocks(css):
        selectors = [s.strip() for s in block[0].split(",")]
        if selector in selectors:
            return dict(
                (k.strip(), v.strip())
                for k, _, v in (d.partition(":") for d in block[1].split(";"))
                if k.strip()
            )
    raise AssertionError(f"no rule for {selector!r}")


def test_a_subsection_heading_is_smaller_than_the_section_it_sits_in():
    """The heading scale runs downward.

    It did not: a detail section was 11.5px and its own subgroups were 12px, so
    "may touch" was set larger than the "Guardrails" heading above it. Nothing
    about reading the stylesheet makes that obvious, since the two rules sit on
    adjacent lines and neither number looks wrong on its own.
    """
    section = float(_rule(".detail-section h3")["font-size"].rstrip("px"))
    subsection = float(_rule(".detail-section h4")["font-size"].rstrip("px"))
    assert subsection < section, f"h4 is {subsection}px inside an h3 of {section}px"


def test_the_three_heading_tiers_are_told_apart_by_more_than_size():
    """A panel title, a section title and a subgroup label differ on several axes.

    All three were within half a pixel of each other, uppercase, and the same
    grey, so a task panel showed five interchangeable labels and no sense of
    where one section ended. Size alone is not enough separation at these sizes.
    """
    section = _rule(".detail-section h3")
    subsection = _rule(".detail-section h4")

    # The section title is the darker of the two, and is not shouted in caps.
    assert section["color"] == "var(--ink)"
    assert "text-transform" not in section
    # The subgroup is quiet, small and uppercase: clearly subordinate.
    assert subsection["color"] == "var(--faint)"
    assert subsection["text-transform"] == "uppercase"
    # And a section is separated by a rule, not just by space.
    assert "border-bottom" in section


def test_a_count_beside_a_section_title_is_not_part_of_the_name():
    """Counts are a separate quieter element.

    "Acceptance · 3/3 passed" as one string makes the tally compete with the
    word that says what the section is, and it inherits the title's weight.
    """
    assert ".h3-note" in (STATIC / "style.css").read_text()
    bundle = (STATIC / "app.js").read_text()
    assert "'h3-note'" in bundle or '"h3-note"' in bundle
    note = _rule(".detail-section h3 .h3-note")
    section = _rule(".detail-section h3")
    assert float(note["font-size"].rstrip("px")) < float(section["font-size"].rstrip("px"))
    assert note["color"] == "var(--faint)"


def test_the_two_kinds_of_name_agree_with_each_other():
    """A milestone name and a decision name are the same tier, so same treatment.

    They had drifted to the same size at different weights.
    """
    milestone = _rule(".milestone-card h2")
    decision = _rule(".decision h3")
    assert milestone["font-size"] == decision["font-size"]
    assert milestone["font-weight"] == decision["font-weight"]


def test_the_subordinate_label_is_defined_once():
    """One selector list, not a copy per container.

    Three containers grew their own copy of this label and had already drifted
    apart by a few tenths of a pixel before they were merged.
    """
    css = (STATIC / "style.css").read_text()
    defining = [
        selectors
        for selectors, body in _blocks(css)
        if "text-transform: uppercase" in body and "font-size: 10.5px" in body
    ]
    assert len(defining) == 1, f"defined in {len(defining)} places: {defining}"
    assert ".decision .field h4" in defining[0]
    assert ".decision .ruling h4" in defining[0]


def test_a_detail_header_spaces_its_own_children():
    """The header row is a flex row with a gap.

    The run drawer puts the status mark, id and pill straight into the header
    while the task drawer nests them a level down. Without a gap here the run
    header rendered as "+M02-002-20260917T103559completed".
    """
    head = _rule(".detail-head")
    assert head["display"] == "flex"
    assert head["gap"] != "0"


def test_every_source_module_is_in_the_bundle():
    """A file under `ui/src` that `ORDER` omits is compiled and then dropped.

    The stamp cannot catch this: it hashes the sources, so adding a module changes
    it, a rebuild makes it match again, and the bundle is still missing the module.
    Everything passes and a whole view renders nothing. `ui/build.mjs` fails on it
    now; this asserts the same thing without needing node, because the failure is
    invisible from the output alone.
    """
    order = re.search(r"const ORDER = \[(.*?)\];", (ROOT / "ui" / "build.mjs").read_text(), re.S)
    assert order, "ui/build.mjs has no ORDER list"
    listed = set(re.findall(r"'([^']+)'", order.group(1)))
    modules = {
        f"{path.relative_to(SRC).as_posix()[:-3]}.js"
        for path in SRC.rglob("*.ts")
    }
    missing = sorted(modules - listed)
    assert not missing, (
        f"these modules are under ui/src but not in ui/build.mjs's ORDER, so their "
        f"code is not in the bundle: {', '.join(missing)}"
    )


def test_each_view_reaches_the_bundle():
    """The compiled output contains something from every view module.

    A cheaper version of the check above, from the other end: `ORDER` naming a file
    and the bundle containing its code are two different claims.
    """
    body = (STATIC / "app.js").read_text()
    for path in sorted((SRC / "views").glob("*.ts")):
        assert f"---- views/{path.stem}.js ----" in body, (
            f"views/{path.stem}.js contributed nothing to writ/static/app.js"
        )


def test_a_grid_card_may_not_grow_past_its_column():
    """A wide card has to let its own scroller scroll.

    A grid item's `min-width` defaults to `auto` — "at least as wide as my
    content" — so a card holding something wider than its column grows to fit it
    rather than clipping. The phase graph is that card: fourteen steps make it
    ~2900px, the card grew to match, the `overflow: auto` holder inside it had
    nothing left to scroll, and the page scrolled sideways instead. Every step past
    the first critics — the repair round, the re-review, the approval — was off the
    right edge with nothing on screen to suggest it was there, so a repair that had
    run looked like one that never happened.
    """
    assert _rule(".grid > *").get("min-width") == "0", (
        "`.grid > * { min-width: 0 }` is missing, so a card wider than its column "
        "will push the page sideways instead of scrolling inside itself"
    )
