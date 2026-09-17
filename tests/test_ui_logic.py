"""The browser's own decisions, checked by running them.

Most of the UI is DOM assembly that a test would only restate. Two parts are not:
the formatting helpers, which decide what a duration or a stale timestamp reads
as, and the filter predicates, which decide what "live" or "blocked" means. Those
are pure functions over data, and they are worth pinning because they have to
agree with the terminal's vocabulary.

Run through node against the compiled bundle rather than the sources, so this
tests what actually ships. Skipped where node is absent — the suite must pass
without a JavaScript toolchain.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from writ import render

ROOT = Path(__file__).resolve().parent.parent
BUNDLE = ROOT / "writ" / "static" / "app.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed"
)


def evaluate(expression: str):
    """Evaluate an expression with the bundle's helpers in scope.

    The bundle is an IIFE that starts an app, so it cannot simply be imported.
    Its inner body is spliced into a scope with a stub DOM instead: enough for
    the pure helpers, and nothing runs the app itself.
    """
    body = BUNDLE.read_text()
    inner = body[body.index("(() => {") + len("(() => {") : body.rindex("})();")]
    # Drop the bootstrap line so no app starts and no fetch is attempted.
    inner = inner.replace("void new App().start();", "")
    harness = f"""
    const noop = () => {{}};
    const stub = () => ({{
      append: noop, replaceChildren: noop, addEventListener: noop, prepend: noop,
      classList: {{ add: noop, remove: noop, toggle: noop, contains: () => false }},
      dataset: {{}}, style: {{}}, querySelector: () => null, querySelectorAll: () => [],
      setAttribute: noop, getAttribute: () => null, textContent: '', appendChild: noop,
    }});
    globalThis.document = {{
      createElement: stub, createElementNS: stub, createTextNode: stub,
      addEventListener: noop, body: stub(), hidden: false, visibilityState: 'visible',
      querySelector: () => null, querySelectorAll: () => [],
    }};
    globalThis.window = {{ addEventListener: noop, setTimeout: noop, clearTimeout: noop }};
    globalThis.location = {{ hash: '' }};
    globalThis.EventSource = class {{ constructor() {{}} addEventListener() {{}} close() {{}} }};
    globalThis.fetch = () => Promise.reject(new Error('no network in tests'));
    {inner}
    console.log(JSON.stringify({expression}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", harness],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


# ------------------------------------------------------------ shared vocabulary


def test_the_status_marks_match_the_terminal():
    """A status should look the same on a page as in `writ list`.

    The marks are duplicated rather than served, because they are presentation
    and the page should render before any request completes. Duplication is fine;
    silent divergence is not.
    """
    marks = evaluate("MARKS")
    # A subset: the terminal also marks milestone rollups ("in-progress",
    # "empty") that no page element uses. Every mark the page does use must match.
    assert marks
    for status, glyph in marks.items():
        assert render.STATUS_MARKS[status] == glyph, status


def test_live_means_the_same_thing_as_it_does_in_the_scheduler():
    assert evaluate("['running', 'reviewing', 'starting'].map(isLive)") == [True] * 3
    assert evaluate("['planned', 'ready', 'completed', 'failed'].map(isLive)") == [False] * 4


# ------------------------------------------------------------------- durations


def test_a_duration_reads_in_units_a_reader_expects():
    """Sub-second runs read as "<1s": the exact figure is process startup, not work."""
    assert evaluate("[0.4, 12, 95, 3700, null].map(duration)") == [
        "<1s",
        "12s",
        "1m 35s",
        "1h 1m",
        "",
    ]


def test_relative_time_prefers_the_recent_past():
    now = "2026-09-17T12:00:00Z"
    cases = evaluate(
        f"""[
      ago('2026-09-17T11:59:55Z', new Date('{now}')),
      ago('2026-09-17T11:56:00Z', new Date('{now}')),
      ago('2026-09-17T09:00:00Z', new Date('{now}')),
      ago('2026-09-14T12:00:00Z', new Date('{now}')),
      ago('', new Date('{now}')),
    ]"""
    )
    assert cases == ["just now", "4m ago", "3h ago", "3d ago", ""]


def test_a_ratio_of_nothing_is_not_a_division_by_zero():
    """A task with no acceptance criteria is legal, so 0/0 must not render "0/0".

    An empty string rather than "0/0", because a criterion count of zero is the
    absence of a gate, not a gate nobody has passed.
    """
    assert evaluate("[ratio(0, 0), ratio(2, 3), percent(0, 0), percent(1, 4)]") == [
        "",
        "2/3",
        0,
        25,
    ]


def test_a_long_title_is_clipped_with_an_ellipsis():
    assert evaluate("clip('a'.repeat(40), 10)") == "aaaaaaaaa…"
    assert evaluate("clip('short', 10)") == "short"


def test_plural_does_not_say_one_tasks():
    assert evaluate("[plural(1, 'task'), plural(2, 'task'), plural(0, 'run')]") == [
        "1 task",
        "2 tasks",
        "0 runs",
    ]


# --------------------------------------------------------------- what filters mean


def test_the_task_filters_select_what_their_names_claim():
    rows = json.dumps(
        [
            {"id": "a", "status": "running", "blocked_by": [], "passed": 0, "total": 1},
            {"id": "b", "status": "ready", "blocked_by": [], "passed": 0, "total": 1},
            {"id": "c", "status": "planned", "blocked_by": ["a"], "passed": 0, "total": 1},
            {"id": "d", "status": "failed", "blocked_by": [], "passed": 0, "total": 1},
            {"id": "e", "status": "completed", "blocked_by": [], "passed": 1, "total": 1},
            {"id": "f", "status": "awaiting-review", "blocked_by": [], "passed": 1, "total": 1},
        ]
    )
    picked = evaluate(
        f"Object.fromEntries(Object.entries(FILTERS).map(([k, f]) => "
        f"[k, {rows}.filter(f).map(r => r.id)]))"
    )
    assert picked["all"] == ["a", "b", "c", "d", "e", "f"]
    assert picked["live"] == ["a"]
    assert picked["ready"] == ["b"]
    assert picked["blocked"] == ["c"]
    assert picked["failed"] == ["d"]
    assert picked["done"] == ["e"]
    assert picked["awaiting review"] == ["f"]


def test_the_run_filters_pick_out_the_runs_worth_finding():
    rows = json.dumps(
        [
            {"id": "1", "status": "running", "role": "agent", "exit_code": None, "decision": ""},
            {"id": "2", "status": "completed", "role": "reviewer", "exit_code": 0, "decision": "accept"},
            {"id": "3", "status": "completed", "role": "reviewer", "exit_code": 0, "decision": "reject"},
            {"id": "4", "status": "failed", "role": "agent", "exit_code": 1, "decision": ""},
        ]
    )
    picked = evaluate(
        f"Object.fromEntries(Object.entries(RUN_FILTERS).map(([k, f]) => "
        f"[k, {rows}.filter(f).map(r => r.id)]))"
    )
    assert picked["live"] == ["1"]
    assert picked["reviews"] == ["2", "3"]
    assert picked["rejected"] == ["3"]
    assert picked["failed"] == ["4"]


def test_status_weight_puts_work_needing_attention_first():
    """Sorting a task list by status should surface what to act on."""
    order = evaluate(
        "['completed','planned','running','failed','awaiting-review','ready']"
        ".sort((a,b) => statusWeight(a) - statusWeight(b))"
    )
    assert order[0] in ("running", "awaiting-review", "failed")
    assert order[-1] == "completed"
