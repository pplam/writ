import pytest

from forge import planner
from forge.state import ForgeError
from tests.conftest import DESIGN


def test_parses_milestones_and_subsection_tasks():
    milestones = planner.parse(DESIGN)
    titles = [m.title for m in milestones]
    assert titles == [
        "Milestone 0 — Foundations",
        "Milestone 1 — Storage",
        "Milestone 2 — Interface",
    ]
    storage = milestones[1]
    assert [t.title for t in storage.tasks] == ["Event log", "Projection"]


def test_extracts_stated_acceptance_bars():
    milestones = planner.parse(DESIGN)
    foundations = milestones[0].tasks[0]
    assert foundations.acceptances == [
        "the project builds",
        "tests run",
        "malformed input fails deterministically",
    ]
    event_log = milestones[1].tasks[0]
    assert event_log.acceptances == [
        "appends are atomic",
        "Replay is byte-identical",
    ]


def test_falls_back_to_generic_acceptances_when_none_stated():
    milestones = planner.parse(DESIGN)
    interface = milestones[2].tasks[0]
    assert interface.acceptances == list(planner.GENERIC_ACCEPTANCES)


def test_flat_mode_makes_one_task_per_milestone():
    milestones = planner.parse(DESIGN, split_subsections=False)
    assert all(len(m.tasks) == 1 for m in milestones)


def test_ids_are_stable_and_ordered():
    milestones = planner.parse(DESIGN)
    built = planner.build_ids(milestones)
    assert [mid for mid, _, _ in built] == ["M01", "M02", "M03"]
    assert [tid for _, _, tasks in built for tid, _ in tasks] == [
        "M01-001",
        "M02-001",
        "M02-002",
        "M03-001",
    ]


def test_offset_continues_numbering():
    milestones = planner.parse(DESIGN)
    built = planner.build_ids(milestones, offset=3)
    assert [mid for mid, _, _ in built] == ["M04", "M05", "M06"]


def test_document_without_headings_is_rejected():
    with pytest.raises(ForgeError):
        planner.parse("just prose, no headings at all")


def test_code_fences_do_not_create_milestones():
    text = "## Real\n\n```\n## Not a heading\n```\n"
    assert [m.title for m in planner.parse(text)] == ["Real"]


def test_section_text_round_trips(tmp_path):
    path = tmp_path / "d.md"
    path.write_text(DESIGN, encoding="utf-8")
    body = planner.section_text(path, "Milestone 1 — Storage / Event log")
    assert "Append-only writes." in body
    assert "Projection" not in body
