"""A design in several documents, and `writ build` from a design to finished work.

The planning agent and the implementers are faked the way the rest of the suite
fakes them: planning re-imports a plan artifact (`--from-plan`), and the run's
agents are real subprocesses that write real verdict files.
"""
import json
import shlex
import sys
from pathlib import Path

import pytest

from writ import analysis, commands, planner, planning, plans, prompts, state

API = """\
# API

## Endpoints

Serve the event log over HTTP.

**Gate:** `GET /events` returns every appended event.
"""

STORAGE = """\
# Storage

## Event log

Append-only writes.

**Gate:** appends are atomic.

## Endpoints

A heading the API document also has.
"""


@pytest.fixture
def docs(tmp_path: Path) -> list[Path]:
    api, storage = tmp_path / "api.md", tmp_path / "storage.md"
    api.write_text(API, encoding="utf-8")
    storage.write_text(STORAGE, encoding="utf-8")
    return [storage, api]


# --------------------------------------------------------------------------
# several design documents


def test_a_section_is_found_in_whichever_document_holds_it(docs):
    storage, api = docs
    assert planner.find_section(docs, "Event log")[0] == storage
    # both have it, so the first given wins unless the section names its file
    assert planner.find_section(docs, "Endpoints")[0] == storage
    found, text = planner.find_section(docs, "api.md / Endpoints")
    assert found == api and "Serve the event log" in text
    assert planner.find_section(docs, "Nowhere") == (None, "")


def test_one_document_is_still_a_design(docs):
    storage, _ = docs
    assert planner.doc_list(storage) == [storage]
    assert planner.doc_list(None) == []
    assert planner.doc_names(docs) == "storage.md, api.md"


def test_the_planning_prompt_names_every_document(docs, project):
    prompt = planning.build_prompt(
        root=project,
        doc=docs,
        plan_path=project / "draft.json",
        context={"design_docs": [str(docs[0].resolve()), "/elsewhere/old.md"]},
    )
    assert "storage.md — the design document, part 1 of 2" in prompt
    assert "api.md — the design document, part 2 of 2" in prompt
    assert "split across 2 documents" in prompt
    # a registered document that is part of this design is not listed twice
    assert "old.md — another document already registered" in prompt
    assert prompt.count("storage.md") == 1


def test_a_single_document_prompt_is_unchanged(docs, project):
    prompt = planning.build_prompt(
        root=project, doc=docs[0], plan_path=project / "draft.json", context={}
    )
    assert "storage.md — the design document\n" in prompt
    assert "split across" not in prompt


def test_the_analysis_stages_read_every_document(docs, project):
    stage = analysis.STAGES[0]
    prompt = analysis.build_prompt(
        stage,
        root=project,
        doc=docs,
        artifact_path=project / "requirements.json",
        artifacts=analysis.Artifacts(),
    )
    assert "part 1 of 2" in prompt and "part 2 of 2" in prompt


def test_other_docs_compares_resolved_paths(docs):
    registered = [str(docs[0].resolve()), str(docs[1])]
    assert prompts.other_docs(registered, docs) == []


def test_extract_reads_each_document_in_order(writ, project, docs):
    writ("init")
    code, out, err = writ(
        "plan", *map(str, docs), "--extract", "--no-gates", "--level", "1"
    )
    assert code == 0, err
    data = state.load(project)
    assert data["design_docs"] == [str(path.resolve()) for path in docs]
    titles = [data["milestones"][m]["title"] for m in sorted(data["milestones"])]
    assert titles == ["Storage", "API"]
    assert "storage.md, api.md (extracted)" in out
    assert data["plans"][-1]["design_docs"] == data["design_docs"]


def test_each_task_points_at_the_document_that_holds_its_section(
    writ, project, docs, tmp_path
):
    storage, api = docs
    artifact = tmp_path / "plan.json"
    artifact.write_text(
        json.dumps(
            {
                "milestones": [
                    {
                        "id": "M01",
                        "title": "Everything",
                        "tasks": [
                            {"id": "a", "title": "Write the log",
                             "design_section": "Event log",
                             "acceptances": ["appends are atomic"]},
                            {"id": "b", "title": "Serve it",
                             "design_section": "api.md / Endpoints",
                             "acceptances": ["GET /events returns them"]},
                            {"id": "c", "title": "Unfiled",
                             "design_section": "Nowhere",
                             "acceptances": ["it is done"]},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    writ("init")
    code, _, err = writ(
        "plan", str(storage), str(api), "--from-plan", str(artifact), "--no-gates"
    )
    assert code == 0, err
    tasks = state.load(project)["tasks"]
    assert tasks["M01-001"]["design_doc"] == str(storage.resolve())
    assert tasks["M01-002"]["design_doc"] == str(api.resolve())
    # no heading matched, so the first document stands in
    assert tasks["M01-003"]["design_doc"] == str(storage.resolve())
    assert "no section titled 'Nowhere' in storage.md, api.md" in err


def test_a_missing_document_is_named(writ, project, docs, tmp_path):
    writ("init")
    code, _, err = writ("plan", str(docs[0]), str(tmp_path / "gone.md"), "--extract")
    assert code == 2
    assert "design document not found" in err and "gone.md" in err


def test_the_same_document_twice_is_read_once(writ, project, docs):
    writ("init")
    writ("plan", str(docs[0]), str(docs[0]), "--extract", "--no-gates")
    assert state.load(project)["design_docs"] == [str(docs[0].resolve())]


# --------------------------------------------------------------------------
# writ build


PLAN = {
    "milestones": [
        {
            "id": "M01",
            "title": "Storage",
            "tasks": [
                {"id": "log", "title": "Write the event log",
                 "design_section": "Event log",
                 "acceptances": ["a test in store/log_test.go shows atomic appends"],
                 "allowed": ["store/log.go"]},
            ],
        }
    ]
}

VERDICT = """
import json, re, sys
prompt = sys.stdin.read()
path = re.search(r'^  (\\S*verdict\\.json)$', prompt, re.M).group(1)
total = int(re.search(r'has (\\d+) acceptance criteri', prompt).group(1))
open(path, "w").write(json.dumps({
    %s,
    "summary": "done",
    "criteria": [
        {"number": i, "status": "passed", "evidence": "ran: pytest -q -> ok"}
        for i in range(1, total + 1)
    ],
}))
"""


def _agent(field: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(VERDICT % field)}"


AGENTS = (
    "--agent", _agent('"outcome": "complete"'),
    "--reviewer", _agent('"decision": "accept"'),
)


@pytest.fixture
def planned_from(monkeypatch, tmp_path):
    """Make build's planning step re-import a plan instead of running an agent."""
    artifact = tmp_path / "plan.json"
    artifact.write_text(json.dumps(PLAN), encoding="utf-8")
    real = commands._step_args
    seen: list = []

    def step_args(args, command, positional, *, index):
        step = real(args, command, positional, index=index)
        if command == "plan":
            step.from_plan = str(artifact)
            step.gates = False
        seen.append(step)
        return step

    monkeypatch.setattr(commands, "_step_args", step_args)
    return seen


def test_build_plans_approves_and_runs(writ, project, docs, planned_from):
    writ("init")
    code, out, err = writ("build", *map(str, docs), *AGENTS)
    assert code == 0, err
    data = state.load(project)
    assert plans.plan_status(data)["status"] in ("executing", "complete")
    assert data["tasks"]["M01-001"]["status"] == "completed"
    assert data["design_docs"] == [str(path.resolve()) for path in docs]
    plan_step, run_step = planned_from
    assert plan_step.auto_approve is True
    assert run_step.agent == AGENTS[1] and run_step.reviewer == AGENTS[3]


def test_build_stops_for_a_human_when_asked(writ, project, docs, planned_from):
    writ("init")
    code, out, _ = writ("build", str(docs[0]), "--no-auto-approve", *AGENTS)
    assert code == 1
    assert "writ approve" in out and "writ build" in out
    data = state.load(project)
    assert not plans.runnable(data)
    assert data["tasks"]["M01-001"]["status"] != "completed"
    # approved by hand, the next build runs what is there without replanning
    assert writ("approve", "--reason", "read it")[0] == 0
    code, out, err = writ("build", *AGENTS)
    assert code == 0, err
    assert "the plan is in place" in out
    assert len(planned_from) == 2  # one plan step, then only the run
    assert state.load(project)["tasks"]["M01-001"]["status"] == "completed"


def test_build_resumes_when_named_documents_are_already_planned(
    writ, project, docs, planned_from
):
    writ("init")
    writ("build", str(docs[0]), "--no-auto-approve", *AGENTS)
    writ("approve", "--reason", "fine")
    code, out, _ = writ("build", str(docs[0]), "--dry-run")
    assert code == 0
    assert "the plan is in place" in out


def test_build_refuses_a_new_document_without_append(
    writ, project, docs, planned_from
):
    writ("init")
    writ("build", str(docs[0]), "--no-auto-approve", *AGENTS)
    code, _, err = writ("build", str(docs[1]), *AGENTS)
    assert code == 2
    assert "api.md is not part of it" in err and "--append" in err


def test_build_appends_a_new_document_when_asked(writ, project, docs, planned_from):
    writ("init")
    writ("build", str(docs[0]), "--no-auto-approve", *AGENTS)
    code, _, err = writ("build", *map(str, docs), "--append", "--dry-run")
    assert code == 0, err
    plan_step = planned_from[-1]
    # only the document the plan does not cover yet is planned
    assert plan_step.design == [str(docs[1])]
    assert plan_step.append is True


def test_build_with_nothing_to_build_says_how(writ, project):
    writ("init")
    code, _, err = writ("build")
    assert code == 2
    assert "writ build design.md" in err


def test_build_flags_reach_the_right_step(writ, project, docs, planned_from):
    writ("init")
    writ(
        "build", str(docs[0]), "--dry-run",
        "--planner", "plan-cmd", "--critics", "coverage", "--no-repair",
        "--parallel", "4", "--order", "depth",
    )
    (plan_step,) = planned_from
    assert plan_step.agent == "plan-cmd"
    assert plan_step.critics == ["coverage"]
    assert plan_step.repair is False
    assert not hasattr(plan_step, "order")


def test_build_leaves_unset_flags_to_the_config(writ, project, docs, planned_from):
    writ("init")
    config_file = project / ".writ" / "config.yaml"
    config_file.write_text(
        "agents:\n  planner:\n    command: from-config\nrun:\n  parallel: 5\n",
        encoding="utf-8",
    )
    writ("build", str(docs[0]), "--no-auto-approve", *AGENTS)
    plan_step, *_ = planned_from
    assert plan_step.agent == "from-config"
    writ("approve", "--reason", "fine")
    writ("build", *AGENTS)
    assert planned_from[-1].parallel == 5
