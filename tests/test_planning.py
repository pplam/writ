import json
import shlex
import sys
from pathlib import Path

import pytest

from writ import planning, state
from writ.state import WritError

PLAN = {
    "milestones": [
        {
            "id": "M01",
            "title": "Storage foundations",
            "notes": "Nothing exists yet; start with the log.",
            "tasks": [
                {
                    "id": "T-log",
                    "title": "Add append-only event log writer",
                    "notes": "internal/store is empty today.",
                    "design_section": "Milestone 1 — Storage / Event log",
                    "acceptances": ["appends are atomic", "replay is byte-identical"],
                    "allowed": ["internal/store/"],
                    "forbidden": ["api/"],
                },
                {
                    "id": "T-proj",
                    "title": "Project the log inline",
                    "acceptances": ["projection matches replay"],
                    "depends_on": ["T-log"],
                },
            ],
        },
        {
            "id": "M02",
            "title": "Interface",
            "tasks": [
                {
                    "id": "T-cli",
                    "title": "Expose the CLI surface",
                    "acceptances": ["`writ status` prints progress"],
                    "depends_on": ["T-log"],
                }
            ],
        },
    ]
}


def agent_writing(payload, *, exit_code=0, to_stdout=False):
    """A fake planning agent: reads the prompt, emits a plan, exits."""
    body = json.dumps(payload) if not isinstance(payload, str) else payload
    script = (
        "import re,sys;"
        "p=sys.stdin.read();"
        f"body={body!r};"
        "m=re.search(r'^  (\\S+plan\\.json)$', p, re.M);"
        + (
            "sys.stdout.write('here is the plan\\n```json\\n'+body+'\\n```\\n');"
            if to_stdout
            else "open(m.group(1),'w').write(body);"
            "sys.stdout.write('wrote the plan\\n');"
        )
        + f"sys.exit({exit_code})"
    )
    return f"{sys.executable} -c {shlex.quote(script)}"


# --------------------------------------------------------------------------
# plan validation


def _work(data):
    """Just the implementation tasks. Gates are in `tasks` too, by design.

    A plan's size is the work it describes; the plan-level checks over that work
    are writ's, not the planner's, and counting them would make every one of these
    assertions a statement about how many gates writ installs.
    """
    return {
        task_id: task
        for task_id, task in data["tasks"].items()
        if task.get("kind", "task") == "task"
    }



def test_load_plan_maps_every_field():
    milestones = planning.load_plan(json.dumps(PLAN))
    assert [m.title for m in milestones] == ["Storage foundations", "Interface"]
    log = milestones[0].tasks[0]
    assert log.ref == "T-log"
    assert log.acceptances == ["appends are atomic", "replay is byte-identical"]
    assert log.allowed == ["internal/store/"] and log.forbidden == ["api/"]
    assert log.notes == "internal/store is empty today."
    assert milestones[0].tasks[1].depends_on == ["T-log"]


def test_load_plan_accepts_common_key_aliases():
    milestones = planning.load_plan(
        json.dumps(
            {
                "milestones": [
                    {
                        "name": "Alt keys",
                        "items": [
                            {
                                "task": "Do the thing",
                                "acceptance_criteria": ["it is done"],
                                "deps": [],
                                "allow": "pkg/",
                            }
                        ],
                    }
                ]
            }
        )
    )
    task = milestones[0].tasks[0]
    assert task.title == "Do the thing"
    assert task.acceptances == ["it is done"]
    assert task.allowed == ["pkg/"]


def test_load_plan_accepts_criteria_as_objects():
    milestones = planning.load_plan(
        json.dumps(
            {
                "milestones": [
                    {
                        "title": "M",
                        "tasks": [
                            {
                                "title": "T",
                                "acceptances": [{"text": "measured, not assumed"}],
                            }
                        ],
                    }
                ]
            }
        )
    )
    assert milestones[0].tasks[0].acceptances == ["measured, not assumed"]


@pytest.mark.parametrize(
    "payload, message",
    [
        ("", "empty"),
        ("not json at all", "not valid JSON"),
        ('{"milestones": []}', "non-empty list"),
        ('{"nope": 1}', "no `milestones` list"),
        ('{"milestones": [{"tasks": [{"title": "T"}]}]}', "title must be"),
        ('{"milestones": [{"title": "M", "tasks": []}]}', "tasks must be a non-empty"),
        ('{"milestones": [{"title": "M", "tasks": [{"title": "T"}]}]}', "acceptances is required"),
        (
            '{"milestones": [{"title": "M", "tasks": [{"title": "T", "acceptances": []}]}]}',
            "non-empty list of criteria",
        ),
        (
            '{"milestones": [{"title": "M", "tasks": [{"id": "A", "title": "T",'
            ' "acceptances": ["x"], "depends_on": ["A"]}]}]}',
            "depends on itself",
        ),
    ],
)
def test_load_plan_rejects_malformed_plans(payload, message):
    with pytest.raises(WritError, match=message):
        planning.load_plan(payload)


def test_load_plan_rejects_duplicate_task_ids():
    duplicate = {
        "milestones": [
            {
                "title": "M",
                "tasks": [
                    {"id": "X", "title": "A", "acceptances": ["a"]},
                    {"id": "X", "title": "B", "acceptances": ["b"]},
                ],
            }
        ]
    }
    with pytest.raises(WritError, match="duplicate task id"):
        planning.load_plan(json.dumps(duplicate))


def test_extract_json_recovers_a_fenced_block():
    text = 'I planned it.\n```json\n{"milestones": [1]}\n```\nDone.'
    assert planning.extract_json(text) == '{"milestones": [1]}'


def test_extract_json_recovers_an_unfenced_object_with_nested_braces():
    text = 'prose {not json} more\n{"a": {"b": "}"}}\ntail'
    assert json.loads(planning.extract_json(text)) == {"a": {"b": "}"}}


def test_extract_json_returns_none_when_there_is_none():
    assert planning.extract_json("no object here") is None


def test_unresolved_sections_flags_invented_headings(design):
    milestones = planning.load_plan(json.dumps(PLAN))
    # the plan's first task cites a real heading, the third cites none
    assert planning.unresolved_sections(milestones, design) == []
    milestones[0].tasks[0].section = "Milestone 9 — Invented"
    assert planning.unresolved_sections(milestones, design) == [
        "Milestone 9 — Invented"
    ]


# --------------------------------------------------------------------------
# prompt


def test_prompt_states_the_target_path_and_schema(tmp_path):
    prompt = planning.build_prompt(
        root=tmp_path,
        doc=tmp_path / "design.md",
        plan_path=tmp_path / ".writ/plans/p/plan.json",
        instructions="focus on the storage layer first",
        context={"design_docs": [], "milestone_offset": 0, "tasks": []},
    )
    assert "You are not implementing it" in prompt
    assert str(tmp_path / ".writ/plans/p/plan.json") in prompt
    assert '"acceptances"' in prompt
    assert "focus on the storage layer first" in prompt
    assert "One task is one bounded agent session" in prompt


def test_prompt_lists_existing_tasks_when_appending(planned, writ, project, design):
    context = planning.plan_context(state.load(project))
    prompt = planning.build_prompt(
        root=project, doc=design, plan_path=project / "plan.json", context=context
    )
    assert "This project already has a plan" in prompt
    assert "M01-001" in prompt
    assert "Number new milestones from M04 onward" in prompt


# --------------------------------------------------------------------------
# end to end through the CLI


def test_plan_runs_the_agent_and_commits_its_plan(writ, project, design):
    writ("init")
    code, out, _ = writ("plan", str(design), "--no-stages", "--agent", agent_writing(PLAN))
    assert code == 0
    assert "created 2 milestones and 3 tasks" in out
    assert "(agent)" in out

    data = state.load(project)
    assert sorted(_work(data)) == ["M01-001", "M01-002", "M02-001"]
    log = data["tasks"]["M01-001"]
    assert log["title"] == "Add append-only event log writer"
    assert [a["text"] for a in log["acceptances"]] == [
        "appends are atomic",
        "replay is byte-identical",
    ]
    assert log["allowed"] == ["internal/store/"]
    assert log["forbidden"] == ["api/"]
    assert any("plan:" in item["text"] for item in log["evidence"])
    # the plan's own ids are translated onto the ids Writ assigned
    assert data["tasks"]["M01-002"]["depends_on"] == ["M01-001"]
    assert data["tasks"]["M02-001"]["depends_on"] == ["M01-001"]


def test_plan_keeps_the_agent_transcript_and_artifact(writ, project, design):
    writ("init")
    writ("plan", str(design), "--no-stages", "--agent", agent_writing(PLAN))
    record = state.load(project)["plans"][-1]
    artifact = Path(record["artifact"])
    assert json.loads(artifact.read_text())["milestones"][0]["id"] == "M01"
    directory = artifact.parent
    assert "You are planning" in (directory / "prompt.txt").read_text()
    assert "wrote the plan" in (directory / "stdout.log").read_text()
    assert record["milestones"] == ["M01", "M02"]


def test_plan_accepts_json_printed_on_stdout(writ, project, design):
    writ("init")
    code, _, _ = writ(
        "plan", str(design), "--no-stages", "--agent", agent_writing(PLAN, to_stdout=True)
    )
    assert code == 0
    assert len(_work(state.load(project))) == 3


def test_plan_dry_run_prints_the_prompt_and_runs_no_agent(writ, project, design):
    writ("init")
    code, out, _ = writ("plan", str(design), "--no-stages", "--dry-run")
    assert code == 0
    assert "Schema:" in out and "plan.json" in out
    assert state.load(project)["tasks"] == {}
    assert not state.plans_dir(project).exists() or not list(
        state.plans_dir(project).iterdir()
    )


def test_plan_reports_a_planner_that_produced_nothing(writ, project, design):
    writ("init")
    silent = f"{sys.executable} -c 'import sys; sys.stdin.read(); print(\"thinking\")'"
    code, _, err = writ("plan", str(design), "--no-stages", "--agent", silent)
    assert code == 2
    assert "without producing a plan" in err
    assert state.load(project)["tasks"] == {}


def test_plan_reports_an_invalid_plan_with_the_artifact_path(writ, project, design):
    writ("init")
    bad = agent_writing('{"milestones": [{"title": "M", "tasks": []}]}')
    code, _, err = writ("plan", str(design), "--no-stages", "--agent", bad)
    assert code == 2
    assert "tasks must be a non-empty list" in err
    assert "plan artifact:" in err
    assert state.load(project)["tasks"] == {}


def test_plan_reports_a_missing_planning_agent(writ, project, design):
    writ("init")
    code, _, err = writ("plan", str(design), "--no-stages", "--agent", "definitely-not-an-agent")
    assert code == 2
    assert "planning agent not found" in err


def test_plan_rejects_a_dependency_on_nothing(writ, project, design):
    writ("init")
    dangling = {
        "milestones": [
            {
                "title": "M",
                "tasks": [
                    {
                        "id": "A",
                        "title": "T",
                        "acceptances": ["x"],
                        "depends_on": ["ghost"],
                    }
                ],
            }
        ]
    }
    code, _, err = writ("plan", str(design), "--no-stages", "--agent", agent_writing(dangling))
    assert code == 2
    assert "neither in this plan nor an existing task" in err
    assert state.load(project)["tasks"] == {}


def test_plan_commits_an_edge_onto_a_task_declared_later(writ, project, design):
    """A dependency pointing forward in the plan is an edge, not an error.

    Tasks are numbered in the order the plan lists them, but nothing says work in
    an early milestone cannot need something from a late one — that is a backward
    edge in the graph, and the graph is a DAG either way. Writ used to insert
    tasks one at a time and demand each dependency already exist, so an ordering
    writ imposed decided whether a valid plan committed at all: a 44-task plan was
    refused for one such edge, after four agent runs had produced it.
    """
    writ("init")
    forward = {
        "milestones": [
            {
                "title": "Controls",
                "tasks": [
                    {
                        "id": "A",
                        "title": "User memory controls",
                        "acceptances": ["controls work"],
                        "depends_on": ["B"],
                    }
                ],
            },
            {
                "title": "Instrumentation",
                "tasks": [
                    {
                        "id": "B",
                        "title": "Retrieval logging",
                        "acceptances": ["scores are logged"],
                    }
                ],
            },
        ]
    }
    code, out, _ = writ(
        "plan", str(design), "--no-stages", "--agent", agent_writing(forward)
    )
    assert code == 0 and "created 2 milestones and 2 tasks" in out
    data = state.load(project)
    assert data["tasks"]["M01-001"]["depends_on"] == ["M02-001"]
    assert data["tasks"]["M02-001"]["depends_on"] == []


def test_plan_still_rejects_a_cycle_between_milestones(writ, project, design):
    """Deferring the edges must not defer the DAG check that reads them."""
    writ("init")
    circular = {
        "milestones": [
            {
                "title": "One",
                "tasks": [
                    {
                        "id": "A",
                        "title": "First",
                        "acceptances": ["a"],
                        "depends_on": ["B"],
                    }
                ],
            },
            {
                "title": "Two",
                "tasks": [
                    {
                        "id": "B",
                        "title": "Second",
                        "acceptances": ["b"],
                        "depends_on": ["A"],
                    }
                ],
            },
        ]
    }
    code, _, err = writ(
        "plan", str(design), "--no-stages", "--agent", agent_writing(circular)
    )
    assert code == 2
    assert "dependency cycle" in err
    assert state.load(project)["tasks"] == {}


def test_plan_append_can_depend_on_existing_tasks(planned, writ, project, design):
    followup = {
        "milestones": [
            {
                "title": "Follow-up",
                "tasks": [
                    {
                        "id": "N1",
                        "title": "Harden the store",
                        "acceptances": ["fuzzing finds no panics"],
                        "depends_on": ["M01-001"],
                    }
                ],
            }
        ]
    }
    code, out, _ = writ(
        "plan", str(design), "--append", "--no-stages", "--agent", agent_writing(followup)
    )
    assert code == 0 and "created 1 milestones and 1 tasks" in out
    data = state.load(project)
    assert data["tasks"]["M04-001"]["depends_on"] == ["M01-001"]


def test_plan_leaves_independent_tasks_independent(writ, project, design):
    independent = {
        "milestones": [
            {
                "title": "Parallel work",
                "tasks": [
                    {"id": "A", "title": "One", "acceptances": ["a"]},
                    {"id": "B", "title": "Two", "acceptances": ["b"]},
                ],
            }
        ]
    }
    writ("init")
    writ("plan", str(design), "--parallel", "--no-stages", "--agent", agent_writing(independent))
    data = state.load(project)
    assert data["tasks"]["M01-001"]["depends_on"] == []
    assert data["tasks"]["M01-002"]["depends_on"] == []


def test_plan_warns_about_sections_the_document_does_not_have(writ, project, design):
    invented = {
        "milestones": [
            {
                "title": "M",
                "tasks": [
                    {
                        "title": "T",
                        "acceptances": ["x"],
                        "design_section": "Milestone 9 — Invented",
                    }
                ],
            }
        ]
    }
    writ("init")
    code, _, err = writ("plan", str(design), "--no-stages", "--agent", agent_writing(invented))
    assert code == 0
    assert "no section titled 'Milestone 9 — Invented'" in err


def test_plan_from_artifact_reuses_a_previous_plan(writ, project, design, tmp_path):
    writ("init")
    artifact = tmp_path / "plan.json"
    artifact.write_text(json.dumps(PLAN), encoding="utf-8")
    code, out, _ = writ("plan", str(design), "--from-plan", str(artifact))
    assert code == 0 and "created 2 milestones and 3 tasks" in out
    assert len(_work(state.load(project))) == 3
    # no agent was run
    assert not list(state.plans_dir(project).iterdir())


def test_plan_from_missing_artifact_is_an_error(writ, design):
    writ("init")
    code, _, err = writ("plan", str(design), "--from-plan", "/nope/plan.json")
    assert code == 2 and "plan file not found" in err


def test_plan_timeout_kills_a_stuck_planner(writ, project, design):
    writ("init")
    stuck = f"{sys.executable} -c 'import sys,time; sys.stdin.read(); time.sleep(30)'"
    code, _, err = writ("plan", str(design), "--no-stages", "--agent", stuck, "--timeout", "1")
    assert code == 2
    assert "exceeding its timeout" in err
    # a planner that hung silently gets told why that usually happens
    assert "interactive session" in err


def test_plan_extract_still_works_without_an_agent(writ, project, design):
    writ("init")
    code, out, _ = writ("plan", str(design), "--extract")
    assert code == 0 and "(extracted)" in out
    assert len(_work(state.load(project))) == 4


def test_plan_dry_run_previews_an_imported_plan(writ, design, tmp_path, project):
    writ("init")
    artifact = tmp_path / "plan.json"
    artifact.write_text(json.dumps(PLAN), encoding="utf-8")
    code, out, _ = writ(
        "plan", str(design), "--from-plan", str(artifact), "--dry-run"
    )
    assert code == 0
    assert "would create 2 milestones, 3 tasks" in out
    assert "appends are atomic" in out
    assert "allowed: internal/store/" in out
    assert state.load(project)["tasks"] == {}


def test_extra_args_after_separator_reach_the_planning_agent(writ, project, design):
    writ("init")
    printer = f"{sys.executable} -c 'import sys; sys.stdin.read(); print(sys.argv[1:])'"
    writ("plan", str(design), "--no-stages", "--agent", printer, "--", "--model", "sonnet")
    directory = next(state.plans_dir(project).iterdir())
    assert "'--model', 'sonnet'" in (directory / "stdout.log").read_text()


def test_plan_checks_the_overwrite_gate_before_running_an_agent(planned, writ, project, design):
    before = list(state.plans_dir(project).iterdir())
    code, _, err = writ("plan", str(design), "--no-stages", "--agent", agent_writing(PLAN))
    assert code == 2 and "already has tasks" in err
    # the gate fired without spending an agent run
    assert list(state.plans_dir(project).iterdir()) == before


def test_plan_reports_the_exact_command_it_runs(writ, project, design):
    writ("init")
    code, out, _ = writ("plan", str(design), "--no-stages", "--agent", agent_writing(PLAN))
    assert code == 0
    assert "running:" in out
    assert "transcript:" in out


def test_plan_model_flag_reaches_a_known_agent(writ, project, design, monkeypatch):
    """`--model` is translated to the agent's own flag, not passed literally."""
    seen = {}

    def fake_run_agent(command, prompt, directory, cwd, timeout, **kwargs):
        seen["command"] = command
        (directory / "stdout.log").write_text("", encoding="utf-8")
        (directory / "plan.json").write_text(json.dumps(PLAN), encoding="utf-8")
        return 0

    from writ import planning as planning_module

    monkeypatch.setattr(planning_module.runner, "run_agent", fake_run_agent)
    writ("init")
    code, _, _ = writ("plan", str(design), "--no-stages", "--agent", "pi", "--model", "sonnet")
    assert code == 0
    # `--mode json` rides along because planning asks for the event stream: the
    # model flag is still translated to pi's own spelling, which is what this is about
    assert seen["command"] == ["pi", "-p", "--model", "sonnet", "--mode", "json"]


def test_plan_model_for_an_unknown_agent_is_rejected_before_running(writ, design, project):
    writ("init")
    code, _, err = writ(
        "plan", str(design), "--no-stages", "--agent", "mystery-agent", "--model", "x"
    )
    assert code == 2
    assert "does not know how to pass a model" in err
    assert not state.plans_dir(project).exists() or not list(
        state.plans_dir(project).iterdir()
    )


# --------------------------------------------------------------------------
# live output while the planner works


def chatty_agent(lines, *, stderr_lines=(), exit_code=0):
    """An agent that emits output progressively, like a real one."""
    script = (
        "import sys;"
        "p=sys.stdin.read();"
        "import re;"
        f"[ (sys.stdout.write(l+chr(10)), sys.stdout.flush()) for l in {list(lines)!r} ];"
        f"[ (sys.stderr.write(l+chr(10)), sys.stderr.flush()) for l in {list(stderr_lines)!r} ];"
        "m=re.search(r'^  (\\S+plan\\.json)$', p, re.M);"
        f"open(m.group(1),'w').write({json.dumps(PLAN)!r});"
        f"sys.exit({exit_code})"
    )
    return f"{sys.executable} -c {shlex.quote(script)}"


def test_plan_mirrors_agent_output_to_the_terminal(writ, project, design):
    writ("init")
    agent = chatty_agent(["reading the design doc", "inspecting src/"])
    code, out, _ = writ("plan", str(design), "--no-stages", "--agent", agent)
    assert code == 0
    assert "reading the design doc" in out
    assert "inspecting src/" in out


def test_streamed_output_is_prefixed_so_it_is_distinguishable(writ, project, design):
    writ("init")
    code, out, _ = writ("plan", str(design), "--no-stages", "--agent", chatty_agent(["thinking"]))
    assert code == 0
    assert "| thinking" in out


def test_agent_stderr_is_mirrored_too(writ, project, design):
    writ("init")
    agent = chatty_agent(["ok"], stderr_lines=["warning: slow model"])
    code, _, err = writ("plan", str(design), "--no-stages", "--agent", agent)
    assert code == 0
    assert "warning: slow model" in err


def test_streaming_still_writes_the_full_transcript(writ, project, design):
    writ("init")
    agent = chatty_agent(["line one", "line two"], stderr_lines=["a warning"])
    writ("plan", str(design), "--no-stages", "--agent", agent)
    directory = next(state.plans_dir(project).iterdir())
    stdout = (directory / "stdout.log").read_text()
    assert "line one" in stdout and "line two" in stdout
    # the mirror prefix is a display concern, never written to the transcript
    assert "|" not in stdout
    assert "a warning" in (directory / "stderr.log").read_text()


def test_quiet_suppresses_the_mirror_but_keeps_the_transcript(writ, project, design):
    writ("init")
    agent = chatty_agent(["chatter"])
    code, out, _ = writ("plan", str(design), "--no-stages", "--agent", agent, "--quiet")
    assert code == 0
    # the mirror prefix is the marker; the word itself appears in the echoed argv
    assert "| chatter" not in out
    directory = next(state.plans_dir(project).iterdir())
    assert "chatter" in (directory / "stdout.log").read_text()


def test_streamed_plan_still_commits_correctly(writ, project, design):
    writ("init")
    code, _, _ = writ("plan", str(design), "--no-stages", "--agent", chatty_agent(["working"]))
    assert code == 0
    assert len(_work(state.load(project))) == 3


def test_a_planner_that_writes_a_lot_does_not_deadlock(writ, project, design):
    """A full pipe buffer would hang a single-threaded reader."""
    writ("init")
    noisy = chatty_agent([f"line {i} " + "x" * 200 for i in range(400)])
    code, out, _ = writ("plan", str(design), "--no-stages", "--agent", noisy)
    assert code == 0
    assert "line 399" in out
    assert len(_work(state.load(project))) == 3
