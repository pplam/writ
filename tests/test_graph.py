"""The dependency view: a topology, not a list of edges.

The fixture DAG is a chain, so most of these build their own shapes — a fork, a
join, a diamond — because the interesting cases are the ones a flat listing hides.
"""
import json

import pytest

from writ import render


def build(writ, *specs):
    """Add tasks as (title, *deps) so a shape can be written in one line."""
    for title, *deps in specs:
        args = ["task", "--title", title, "--milestone", "M01", "--acceptance", "x"]
        for dep in deps:
            args.extend(["--depends", dep])
        code, out, err = writ(*args)
        assert code == 0, err


# --------------------------------------------------------------------------
# layout


def test_levels_group_what_can_run_together():
    tasks = {
        "a": {"depends_on": []},
        "b": {"depends_on": ["a"]},
        "c": {"depends_on": ["a"]},
        "d": {"depends_on": ["b", "c"]},
    }
    assert render.dag_levels(tasks) == [["a"], ["b", "c"], ["d"]]


def test_a_level_is_one_past_its_deepest_dependency():
    """A long path must not let a task float up beside its own blocker."""
    tasks = {
        "a": {"depends_on": []},
        "b": {"depends_on": ["a"]},
        "c": {"depends_on": ["b"]},
        "late": {"depends_on": ["a", "c"]},
    }
    levels = render.dag_levels(tasks)
    assert levels == [["a"], ["b"], ["c"], ["late"]]


def test_the_tree_follows_dependencies_forwards():
    tasks = {
        "a": {"depends_on": []},
        "b": {"depends_on": ["a"]},
        "c": {"depends_on": ["b"]},
    }
    assert render.dag_tree(tasks, label=lambda t: t) == ["a", "└─ b", "   └─ c"]


def test_a_fork_branches():
    tasks = {
        "a": {"depends_on": []},
        "b": {"depends_on": ["a"]},
        "c": {"depends_on": ["a"]},
    }
    lines = render.dag_tree(tasks, label=lambda t: t)
    assert lines == ["a", "├─ b", "└─ c"]


def test_a_joined_task_is_drawn_once():
    """Expanding it under every path would imply the work happens twice."""
    tasks = {
        "a": {"depends_on": []},
        "b": {"depends_on": ["a"]},
        "c": {"depends_on": ["a"]},
        "join": {"depends_on": ["b", "c"]},
    }
    lines = render.dag_tree(tasks, label=lambda t: t)
    expanded = [line for line in lines if line.rstrip().endswith("join") and "↩" not in line]
    referenced = [line for line in lines if "↩ join" in line]
    assert len(expanded) == 1
    assert len(referenced) == 1


def test_a_join_is_drawn_under_its_last_dependency():
    """Drawn under an early dependency, it would appear before its own blockers."""
    tasks = {
        "a": {"depends_on": []},
        "b": {"depends_on": ["a"]},
        "late": {"depends_on": ["b"]},
        "join": {"depends_on": ["a", "late"]},
    }
    lines = render.dag_tree(tasks, label=lambda t: t)
    home = next(i for i, line in enumerate(lines) if line.rstrip().endswith("join"))
    blocker = next(i for i, line in enumerate(lines) if line.rstrip().endswith("late"))
    assert home > blocker


def test_every_task_appears_somewhere():
    tasks = {
        "a": {"depends_on": []},
        "b": {"depends_on": ["a"]},
        "c": {"depends_on": ["a"]},
        "d": {"depends_on": ["b", "c"]},
        "e": {"depends_on": ["d"]},
    }
    lines = render.dag_tree(tasks, label=lambda t: t)
    text = "\n".join(lines)
    for task_id in tasks:
        assert task_id in text


def test_disconnected_roots_are_separate_trees():
    tasks = {
        "a": {"depends_on": []},
        "b": {"depends_on": ["a"]},
        "island": {"depends_on": []},
    }
    lines = render.dag_tree(tasks, label=lambda t: t)
    assert lines == ["a", "└─ b", "", "island"]


def test_dependencies_on_missing_tasks_are_ignored():
    """A dangling edge is `check_dag`'s problem; the drawing should still work."""
    tasks = {"a": {"depends_on": ["ghost"]}}
    assert render.dag_tree(tasks, label=lambda t: t) == ["a"]


def test_layout_is_stable_across_runs():
    tasks = {
        "b": {"depends_on": ["a"]},
        "a": {"depends_on": []},
        "c": {"depends_on": ["a"]},
    }
    once = render.dag_tree(tasks, label=lambda t: t)
    shuffled = {k: tasks[k] for k in ("c", "a", "b")}
    assert render.dag_tree(shuffled, label=lambda t: t) == once


# --------------------------------------------------------------------------
# the command


def test_graph_draws_a_tree(planned, writ):
    code, out, _ = writ("graph")
    assert code == 0
    assert "└─" in out
    assert "M01-001" in out and "M02-001" in out


def test_graph_does_not_print_an_adjacency_list(planned, writ):
    """The old view; a flat line per task is what this replaced."""
    _, out, _ = writ("graph")
    assert "<-" not in out


def test_graph_counts_depth_and_width(planned, writ):
    _, out, _ = writ("graph")
    assert "4 tasks, 4 deep" in out


def test_graph_marks_status(planned, writ):
    _, out, _ = writ("graph")
    assert "> M01-001" in out  # ready
    assert "· M02-001" in out  # planned


def test_verbose_adds_status_and_acceptance_counts(planned, writ):
    _, out, _ = writ("graph", "--verbose")
    assert "[ready, 0/1]" in out or "[ready" in out
    assert "0/3" in out or "0/2" in out


def test_levels_view_groups_by_depth(planned, writ):
    _, out, _ = writ("graph", "--levels")
    assert "level 1  (1 task)" in out
    assert "after M01-001" in out


def test_a_fork_widens_the_graph(planned, writ):
    """The fixture is a pure chain, so width only appears once work forks."""
    _, before, _ = writ("graph")
    assert "in parallel" not in before
    build(writ, ("Left", "M01-001"), ("Right", "M01-001"))
    _, after, _ = writ("graph")
    assert "up to 3 in parallel" in after


def test_a_join_explains_its_marker(planned, writ):
    build(writ, ("Left", "M01-001"), ("Right", "M01-001"))
    build(writ, ("Both", "M01-002", "M01-003"))
    _, out, _ = writ("graph")
    assert "↩" in out
    assert "joins a task drawn under its last dependency" in out


def test_a_graph_with_no_joins_omits_the_marker_note(planned, writ):
    _, out, _ = writ("graph")
    assert "↩" not in out


def test_an_empty_project_says_so(project, writ):
    writ("init")
    code, out, _ = writ("graph")
    assert code == 0 and "no tasks" in out


def test_a_single_task_is_not_pluralised(project, writ):
    writ("init")
    writ("task", "--title", "Only", "--milestone", "M01", "--acceptance", "x")
    _, out, _ = writ("graph")
    assert "1 task, 1 deep" in out
    assert "in parallel" not in out


def test_json_gives_levels_and_both_edge_directions(planned, writ):
    _, out, _ = writ("--json", "graph")
    payload = json.loads(out)
    assert payload["levels"][0] == ["M01-001"]
    assert payload["tasks"]["M01-001"]["blocks"] == ["M02-001"]
    assert payload["tasks"]["M02-001"]["depends_on"] == ["M01-001"]


def test_dot_still_emits_graphviz(planned, writ):
    _, out, _ = writ("graph", "--dot")
    assert "digraph writ" in out
    assert '"M01-001" -> "M02-001"' in out


def test_dot_groups_tasks_by_milestone(planned, writ):
    _, out, _ = writ("graph", "--dot")
    assert "subgraph cluster_M01" in out
    assert 'label="M01' in out


def test_a_cycle_is_reported_not_drawn(planned, writ, project):
    from writ import state

    with state.transaction(project) as data:
        data["tasks"]["M01-001"]["depends_on"] = ["M03-001"]
    code, _, err = writ("graph")
    assert code == 2 and "dependency cycle" in err


# --------------------------------------------------------------------------
# graphviz output


def test_dot_carries_status_and_progress(planned, writ):
    """The rendered graph is where progress is most legible; do not drop it."""
    _, out, _ = writ("graph", "--dot")
    assert "ready" in out
    assert "0/1" in out or "0/3" in out
    assert "fillcolor=" in out


def test_dot_colours_by_status(planned, writ, project):
    from writ import render

    _, before, _ = writ("graph", "--dot")
    assert 'fillcolor="#f7f7f7"' in before  # planned
    writ("override", "M01-001", "completed", "--reason", "done by hand", "--accept", "1")
    _, after, _ = writ("graph", "--dot")
    assert 'fillcolor="#d8ece0"' in after  # completed


def test_dot_marks_the_ready_frontier(planned, writ):
    """What can start now is the question a rendered graph is opened to answer."""
    _, out, _ = writ("graph", "--dot")
    ready = [line for line in out.splitlines() if "M01-001" in line and "label" in line]
    assert "penwidth=2" in ready[0]


def test_dot_does_not_repeat_an_id_as_its_own_title(planned, writ):
    """`writ task --milestone M09` titles the new milestone `M09`."""
    writ("task", "--title", "Late", "--milestone", "M09", "--acceptance", "x")
    _, out, _ = writ("graph", "--dot")
    assert 'label="M09"' in out
    assert 'label="M09  M09"' not in out


def test_dot_quotes_are_escaped(planned, writ):
    writ("task", "--title", 'Handle "quoted" input', "--milestone", "M01",
         "--acceptance", "x")
    _, out, _ = writ("graph", "--dot")
    assert '\\"' not in out.replace('\\n', '')  # no raw escapes breaking the label
    assert "Handle 'quoted' input" in out


def test_dot_includes_tasks_with_no_milestone(planned, writ, project):
    """A task outside every cluster must still reach the drawing."""
    from writ import state

    with state.transaction(project) as data:
        data["tasks"]["M01-001"]["milestone"] = None
    _, out, _ = writ("graph", "--dot")
    assert '"M01-001" [label=' in out


def test_dot_is_valid_graphviz(planned, writ):
    """Render it if graphviz is here; a malformed graph is worse than no graph."""
    import shutil
    import subprocess

    if not shutil.which("dot"):
        pytest.skip("graphviz not installed")
    _, out, _ = writ("graph", "--dot")
    result = subprocess.run(
        ["dot", "-Tsvg", "-o", "/dev/null"],
        input=out,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stderr.strip(), result.stderr
