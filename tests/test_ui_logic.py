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
    // The bundle narrows with `instanceof` before touching focus, so the names have
    // to exist. Nothing in the stub DOM is an instance of either, which is the
    // honest answer for a document that is not a document.
    globalThis.HTMLElement = class {{}};
    globalThis.Node = class {{}};
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


# ------------------------------------------------------------ drawer dismissal


def test_a_click_off_the_drawer_dismisses_it():
    """The whole point: attention moved elsewhere, so the panel gets out of the way."""
    assert evaluate(
        "dismissesOnClick({openKey: 'task:M01-001', keyAtPress: 'task:M01-001', "
        "insideDrawer: false})"
    ) is True


def test_a_click_inside_the_drawer_keeps_it_open():
    """Reading a run row or selecting text in the panel is not leaving it."""
    assert evaluate(
        "dismissesOnClick({openKey: 'task:M01-001', keyAtPress: 'task:M01-001', "
        "insideDrawer: true})"
    ) is False


def test_opening_a_second_task_swaps_the_panel_rather_than_closing_it():
    """The click landed outside the drawer, and it opened something.

    Without this the drawer would close on the same click that reopens it: it
    empties, drops its 'open' class, then slides back with a "Loading…" for the
    new task. Comparing the open detail before and after the click is what tells
    the two apart, since both are clicks outside the panel.
    """
    assert evaluate(
        "dismissesOnClick({openKey: 'task:M01-002', keyAtPress: 'task:M01-001', "
        "insideDrawer: false})"
    ) is False


def test_clicking_the_row_that_is_already_open_toggles_it_shut():
    """Same key before and after, so nothing was opened: it is a dismissal.

    A click that visibly does nothing is the worse alternative.
    """
    assert evaluate(
        "dismissesOnClick({openKey: 'run:R1', keyAtPress: 'run:R1', insideDrawer: false})"
    ) is True


def test_a_click_with_the_drawer_shut_dismisses_nothing():
    assert evaluate(
        "dismissesOnClick({openKey: null, keyAtPress: null, insideDrawer: false})"
    ) is False


def test_focus_moving_out_of_the_drawer_dismisses_it():
    """Tabbing past the end of the panel is leaving it, the keyboard's version."""
    assert evaluate(
        "dismissesOnFocus({openKey: 'task:M01-001', movedTo: 'outside'})"
    ) is True


def test_focus_moving_within_the_drawer_keeps_it_open():
    assert evaluate(
        "dismissesOnFocus({openKey: 'task:M01-001', movedTo: 'inside'})"
    ) is False


def test_a_rerender_that_destroys_the_focused_element_does_not_dismiss():
    """The bug this guard exists for.

    The drawer re-fetches and replaces its contents on every snapshot. Anything
    focused inside it is destroyed, focus falls to the body, and focusout fires
    with no relatedTarget. Reading that as "focus left" would close the panel by
    itself every time an agent reported anything — while someone was watching it.
    """
    assert evaluate(
        "dismissesOnFocus({openKey: 'task:M01-001', movedTo: 'nowhere'})"
    ) is False


# --------------------------------------------------------------- scroll keeping


def scroll_case(before: str, after: str) -> list:
    """Run a repaint against fake panes and report where they end up scrolled.

    `before` and `after` are pane lists: the panes that existed when the offsets
    were noted, and the ones that exist after the view was rebuilt. Calls the
    shipped methods with a stand-in `body`, so this exercises the real lookup and
    not a restatement of it.
    """
    return evaluate(
        f"""(() => {{
          const pane = (key, left, top) => ({{
            dataset: {{ scrollKey: key }}, scrollLeft: left, scrollTop: top,
          }});
          const host = (panes) => ({{ body: {{ querySelectorAll: () => panes }} }});
          const old = {before};
          const fresh = {after};
          const saved = App.prototype.scrollOffsets.call(host(old));
          App.prototype.restoreScroll.call(host(fresh), saved);
          return fresh.map((p) => [p.dataset.scrollKey, p.scrollLeft, p.scrollTop]);
        }})()"""
    )


def test_a_repaint_keeps_the_graph_where_it_was_scrolled_to():
    """The bug: selecting a node sent the graph back to the far left.

    Repainting builds a fresh holder and swaps it in, and scroll position lives on
    the element being discarded. Clicking a node repaints, so the graph jumped to
    the left at the one moment you were certainly looking at something off to the
    right — and every arriving snapshot did it again.
    """
    kept = scroll_case("[pane('graph', 900, 0)]", "[pane('graph', 0, 0)]")
    assert kept == [["graph", 900, 0]]


def test_leaving_the_view_and_coming_back_starts_at_the_beginning():
    """Keys are view-specific, so a different view finds no offset to restore.

    Returning to a view is not the same as never having left it: starting at the
    top is what a reader expects, and restoring a position from before would be
    the page remembering something they did not ask it to.
    """
    kept = scroll_case("[pane('graph', 900, 0)]", "[pane('runs', 0, 0)]")
    assert kept == [["runs", 0, 0]]


def test_both_axes_are_kept():
    """The graph scrolls sideways, but a tall one scrolls down too."""
    kept = scroll_case("[pane('graph', 640, 220)]", "[pane('graph', 0, 0)]")
    assert kept == [["graph", 640, 220]]


def test_panes_without_a_key_are_left_alone():
    """Opting in matters: a pane with no key is one nothing claimed to manage."""
    kept = evaluate(
        """(() => {
          const panes = [{ dataset: {}, scrollLeft: 0, scrollTop: 0 }];
          const host = { body: { querySelectorAll: () => panes } };
          const saved = App.prototype.scrollOffsets.call(host);
          return [saved.size, panes[0].scrollLeft];
        })()"""
    )
    assert kept == [0, 0]


def test_a_render_is_what_keeps_the_scroll_position():
    """That `render` calls the restore, not just that the restore works.

    Written after noticing the tests above pass with the call to `restoreScroll`
    deleted from `render`: they reach the methods directly, so they pin the lookup
    and say nothing about the wiring — and the wiring was the bug. This drives
    `render` with a stand-in pane and asserts the offset survives the repaint.
    """
    kept = evaluate(
        """(() => {
          const pane = { dataset: { scrollKey: 'graph' }, scrollLeft: 900, scrollTop: 0 };
          const app = Object.create(App.prototype);
          // Repainting is what loses the offset in the browser, because the holder
          // is replaced. Standing in for that: the pane is zeroed mid-render, the
          // same damage, so only a restore afterwards can put it back.
          const zero = () => { pane.scrollLeft = 0; pane.scrollTop = 0; };
          Object.assign(app, {
            store: { current: { graph: {}, overview: { counts: {} } } },
            route: { view: 'tasks' },
            nav: { querySelectorAll: () => [] },
            body: { querySelectorAll: () => [pane] },
            paintCounts: zero, paintView: zero, paintDrawer: zero,
          });
          App.prototype.render.call(app);
          return pane.scrollLeft;
        })()"""
    )
    assert kept == 900


# ------------------------------------------------------- what became of the task


def test_a_lost_review_is_not_described_as_returning_to_the_queue():
    """The wording bug. A reviewer that writes no verdict moves nothing.

    The implementation is still there with its criteria still passed, and the task
    is still in the review queue. Telling that reader the task "was returned to the
    queue rather than judged" — as this said for every no-verdict run — sends them
    to re-dispatch work that is already done.
    """
    sentence = evaluate("noVerdictOutcome({resulting_status: 'awaiting-review'})")
    assert sentence == 'the task is still awaiting review, with the implementation intact'


def test_a_lost_implementation_is_the_case_that_did_return_to_the_queue():
    assert evaluate("noVerdictOutcome({resulting_status: 'planned'})") == (
        'the task was returned to the queue rather than judged'
    )


def test_an_unrecorded_outcome_names_the_gap_rather_than_filling_it():
    """Runs recorded before the status was kept have nothing to report here.

    Naming the gap is honest; picking the likelier answer would be the same bug
    again, quieter.
    """
    assert evaluate("noVerdictOutcome({resulting_status: ''})") == (
        'the task was left unjudged'
    )


def test_a_failed_task_is_reported_as_what_it_became():
    assert evaluate("noVerdictOutcome({resulting_status: 'failed'})") == (
        'the task became failed rather than judged'
    )


# ------------------------------------------------------------------ blocked tasks


def test_a_blocked_task_gets_a_section_and_an_unblocked_one_does_not():
    """The decision this makes. A blocked task's dependencies all read as satisfied,
    because what stopped it was its own report rather than an unmet dependency, so
    the status pill was the only sign anything was wrong.

    Asserted as present-or-absent rather than by reading the rendered text: the stub
    DOM here builds no real tree to query, and the heading is a literal in the
    source. What is worth pinning is that an empty reason renders nothing at all,
    because the field is empty for every task that is not blocked and a bare
    "Blocked on" heading over nothing would be worse than silence.
    """
    assert evaluate("blockedSection({blocked_on: 'needs a decision first'}) !== null") is True
    assert evaluate("blockedSection({blocked_on: ''}) === null") is True


# ------------------------------------------------------------------- the route


def test_a_planning_step_is_linkable():
    """A step's route survives a reload, like a task's and a run's.

    Worth pinning because a step id carries a colon — `stage:requirements`,
    `critic:feasibility@r2` — so it has to round-trip through the encoding. A hash
    that decoded to a different id would open the drawer on nothing.
    """
    for step in ("stage:requirements", "critic:feasibility@r2"):
        hashed = evaluate(f"toHash({{view: 'plan', step: {step!r}}})")
        assert ":" not in hashed.split("/step/")[1], hashed
        assert evaluate(f"parseHash({hashed!r})") == {"view": "plan", "step": step}


def test_a_step_route_keeps_its_view():
    assert evaluate("parseHash('#/plan/step/synthesis')") == {
        "view": "plan",
        "step": "synthesis",
    }
    # An unknown view falls back rather than routing nowhere.
    assert evaluate("parseHash('#/nope/step/synthesis')")["view"] == "overview"


def test_an_open_step_is_a_dismissable_detail():
    """The drawer's dismissal logic has to know about steps too.

    Otherwise a click elsewhere would leave the step panel open forever, since
    `detailKey` returning null means "nothing is open".
    """
    assert evaluate(
        "dismissesOnClick({openKey: 'step:stage:requirements', "
        "keyAtPress: 'step:stage:requirements', insideDrawer: false})"
    ) is True
