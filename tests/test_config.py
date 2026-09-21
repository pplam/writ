"""Per-project defaults in `.writ/config.json`.

Two things are being tested here, and the second is the one that matters. The
first is that a config is read and honoured at all. The second is that a flag
still beats it and a typo in it is refused, because a config file that is quietly
half-applied is worse than no config file: the whole point of writing the reviewer
down is that you stop having to remember it, and a silently ignored `"reviewr"`
takes that away while leaving the file on disk claiming otherwise.
"""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from writ import config, orchestrator, state
from writ.cli import build_parser
from writ.config import AGENT_TIMEOUT as DEFAULT_AGENT_TIMEOUT
from writ.model import DEFAULT_MAX_REWORK
from writ.state import WritError


def write_config(root: Path, payload: dict) -> Path:
    path = config.config_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def resolve(root: Path, *argv: str):
    """Parse a command line the way `main` does, then apply the config to it."""
    args = build_parser().parse_args(["--root", str(root), *argv])
    decided = config.apply(args, config.load(root))
    return args, decided


# --------------------------------------------------------------------------
# reading it


def test_no_config_is_not_an_error(project):
    """A project that never needed one should not have to have one."""
    assert config.load(project) == {}


def test_a_malformed_config_names_the_file(project):
    path = config.config_file(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(WritError, match=r"config\.json: not valid JSON"):
        config.load(project)


def test_the_shipped_example_validates():
    """`config.example.json` is the documentation, so it has to be loadable.

    Including its comments: a reader copies the file, and one that had to be
    edited before it would load at all would be a poor first impression.
    """
    example = Path(__file__).resolve().parent.parent / "config.example.json"
    loaded = config.validate(json.loads(example.read_text()))
    assert sorted(loaded["agents"]) == sorted(config.ROLES)
    assert loaded["run"] == {"parallel": 3, "order": "id", "max_rework": 2}
    # a filled-in example, so it exercises the sections beyond agents and run too
    assert loaded["plan"] == {"stages": True, "gates": True, "critics": True}
    assert loaded["critique"]["critics"]


def test_init_writes_a_starter_config(writ, project):
    """`writ init` leaves a config behind, because an unknown default is unset."""
    code, out, _ = writ("init")
    assert code == 0
    assert "config.json" in out
    assert config.config_file(project).exists()


def test_the_starter_config_holds_writs_own_defaults(writ, project):
    """Every field is present, and every value is what writ would have done.

    Both halves matter. Present, so the file is a list of what can be set rather
    than a list of what someone already set. Unchanged, so `writ init` does not
    quietly decide this project's agents on the way past.
    """
    writ("init")
    loaded = config.load(project)
    assert loaded["agents"] == {
        "planner": {"command": "pi", "timeout": DEFAULT_AGENT_TIMEOUT},
        "critic": {"timeout": DEFAULT_AGENT_TIMEOUT},
        "implementer": {"command": "pi"},
        # the analysis stages: unset, so they run on the planner's own setting
        "stage": {},
        # the two that matter: unset, so review still falls back to the
        # implementing agent rather than being pinned to `pi` by a generated file
        "reviewer": {},
    }
    assert loaded["run"] == {
        "parallel": 1,
        "order": orchestrator.DEFAULT_ORDER,
        "max_rework": DEFAULT_MAX_REWORK,
    }


def test_the_starter_config_names_every_field_writ_accepts():
    """The file is the schema, so a field writ accepts has to appear in it.

    Otherwise the list is a subset nobody can tell is a subset, and the setting
    left out is the one a reader concludes does not exist.
    """
    raw = json.loads(config.default_document())
    for role in config.ROLES:
        assert sorted(raw["agents"][role]) == sorted(config.ROLE_KEYS)
    assert sorted(k for k in raw["run"] if not k.startswith("_")) == sorted(
        config.RUN_KEYS
    )


def test_the_comment_block_is_the_only_comment():
    """All of it in `_` at the top, and nothing scattered down the file.

    A note that drifted beside a field would put the explanation and the values
    in each other's way — whichever a reader came for ends up interleaved with
    the one they did not.
    """
    raw = json.loads(config.default_document())
    assert [key for key in raw if config.is_comment(key)] == ["_"]
    for section in ("agents", "run"):
        for key in raw[section]:
            assert not config.is_comment(key)
        for settings in raw[section].values():
            if isinstance(settings, dict):
                assert not any(config.is_comment(key) for key in settings)


def entry_names(lines: list[str]) -> list[str]:
    """The `name - what it does` entries of the comment block, by name.

    Parsed from the left of each line rather than matched as a substring, because
    the descriptions contain the names too: the line for `chain` opens "order tasks
    the plan left independent", which a substring check reads as a second entry for
    `order`.
    """
    names = []
    for line in lines:
        head, sep, _ = line.strip().partition(" - ")
        if sep and head and " " not in head.strip():
            names.append(head.strip())
    return names


def test_every_field_writ_accepts_has_a_line():
    """One line per setting, each named once, under a heading for its section.

    The agents section is documented per role rather than per field: command, model
    and timeout mean the same thing in all five, and repeating that fifteen times
    would bury the part that differs, which is what each role is for.
    """
    lines = json.loads(config.default_document())["_"]
    names = entry_names(lines)
    expected = list(config.ROLES) + [
        path.split(".", 1)[1]
        for path in config.FIELDS
        if not path.startswith("agents.")
    ]
    assert sorted(names) == sorted(expected)
    assert "Note:" in lines[0]
    for section in config.SECTIONS:
        assert any(line.startswith(f"{section} — ") for line in lines), section


def test_every_section_and_role_is_documented():
    """A section or role with no line would be a setting nobody is told about."""
    for section in config.SECTIONS:
        assert config.SECTION_DOCS.get(section), section
    for role in config.ROLES:
        assert config.ROLE_DOCS.get(role), role
    for path, entry in config.FIELDS.items():
        if path.startswith("agents."):
            continue  # documented per role
        assert entry.doc, path
        assert entry.flag, path


def test_no_documented_default_is_typed_by_hand():
    """Every default the comments mention is a token, filled from the code.

    This is the drift the generated file exists to prevent, and asserting that
    the rendered text contains writ's defaults cannot catch it — the text is
    rendered *from* those defaults, so it agrees with them by construction. What
    can be caught is the next writer typing `1800` into a line instead of
    `@AGENT_T@`, which reads identically today and is wrong the moment the
    builtin moves. So the check is on the source table, not the output.
    """
    written = [*config.ROLE_DOCS.values(), *config.RUN_DOCS.values(), *config.NOTE]
    # Values only. The order *names* are exempt: `@ORDERS@` renders the list, but
    # the line saying what "depth" actually prefers has to name it to say it, and
    # a renamed order would be caught by the parser refusing the config anyway.
    literals = [
        str(config.AGENT_TIMEOUT),
        str(config.DEFAULTS["plan"]["agent"].builtin),
    ]
    for line in written:
        for literal in literals:
            assert literal not in line, f"{literal!r} hardcoded in {line!r}"


def test_the_rendered_document_names_every_default_and_flag():
    """And once filled, the tokens have to have produced something."""
    doc = config.default_document()
    assert str(config.DEFAULTS["plan"]["agent"].builtin) in doc
    assert str(config.AGENT_TIMEOUT) in doc
    for flag in config.RUN_KEYS.values():
        assert flag in doc
    for order in orchestrator.ORDERS:
        assert order in doc


def test_the_starter_config_documents_the_reviewer_fallback():
    """The one default a reader has to be told, because it is the weakest.

    Unset, review runs on the agent that wrote the code. A file listing
    `"reviewer": {"command": null}` without saying what the null resolves to
    would be schema with the point left out.
    """
    lines = json.loads(config.default_document())["_"]
    start = next(i for i, line in enumerate(lines) if line.strip().startswith("reviewer"))
    # the entry plus its continuation lines, which is where the wrap put half of it
    about = " ".join(line.strip() for line in lines[start:] if line.strip())
    assert "null: the implementer" in about
    assert "wrote the code reviews it" in about


def test_a_null_field_means_the_default(project):
    """Writing null is the same as leaving the key out.

    Which is what lets the generated config name a field whose default is not a
    value at all: an unset reviewer timeout is *no* timeout, and there is no
    number that says so.
    """
    write_config(
        project,
        {
            "agents": {"reviewer": {"command": None, "model": None, "timeout": None}},
            "run": {"parallel": None, "order": None, "max_rework": None},
        },
    )
    assert config.load(project) == {"agents": {"reviewer": {}}, "run": {}}


def test_init_does_not_overwrite_a_config(writ, project):
    """This file is hand-edited and recoverable from nothing else in .writ."""
    writ("init")
    path = config.config_file(project)
    path.write_text('{"run": {"parallel": 7}}', encoding="utf-8")
    code, out, _ = writ("init", "--force")
    assert code == 0
    assert "kept your existing" in out
    assert config.load(project)["run"] == {"parallel": 7}


def test_comments_are_ignored_at_every_level():
    raw = {
        "_": "why this project chose these",
        "agents": {"_note": "...", "reviewer": {"command": "codex"}},
        "run": {"_": "...", "parallel": 2},
    }
    assert config.validate(raw) == {
        "agents": {"reviewer": {"command": "codex"}},
        "run": {"parallel": 2},
    }


# --------------------------------------------------------------------------
# refusing what it cannot honour


def test_a_misspelled_role_is_refused_with_the_name_it_meant():
    """The suggestion is the point. `reviewr` is three characters from right."""
    with pytest.raises(WritError, match=r"did you mean 'reviewer'"):
        config.validate({"agents": {"reviewr": {"command": "codex"}}})


def test_an_unknown_section_lists_the_known_ones():
    with pytest.raises(WritError, match=r"unknown section 'models'.*known sections"):
        config.validate({"models": {}})


def test_an_unknown_key_in_a_role_is_refused():
    with pytest.raises(WritError, match=r"agents\.reviewer: unknown key 'agent'"):
        config.validate({"agents": {"reviewer": {"agent": "codex"}}})


def test_a_role_that_is_not_an_object_says_what_one_looks_like():
    with pytest.raises(WritError, match=r'expected an object like \{"command"'):
        config.validate({"agents": {"reviewer": "codex"}})


def test_an_empty_command_is_refused():
    with pytest.raises(WritError, match="not an empty string"):
        config.validate({"agents": {"planner": {"command": "  "}}})


def test_a_model_that_is_not_a_string_names_the_type():
    with pytest.raises(WritError, match=r"agents\.planner\.model: expected a string"):
        config.validate({"agents": {"planner": {"model": 4}}})


def test_an_unknown_order_lists_the_real_ones():
    with pytest.raises(WritError, match=r"'sideways' is not an order"):
        config.validate({"run": {"order": "sideways"}})


def test_parallel_must_be_above_zero():
    with pytest.raises(WritError, match=r"run\.parallel: expected a whole number"):
        config.validate({"run": {"parallel": 0}})


def test_max_rework_of_zero_is_allowed_because_it_means_something():
    """`--max-rework 0` is a real setting: fail on the first rejection."""
    assert config.validate({"run": {"max_rework": 0}}) == {"run": {"max_rework": 0}}
    with pytest.raises(WritError, match="0 or more"):
        config.validate({"run": {"max_rework": -1}})


def test_a_boolean_is_not_a_number():
    """`True` is an int in Python and is not a parallelism."""
    with pytest.raises(WritError, match=r"run\.parallel"):
        config.validate({"run": {"parallel": True}})


def test_a_broken_config_fails_before_any_agent_runs(writ, project, design):
    writ("init")
    write_config(project, {"agents": {"reviewr": {"command": "codex"}}})
    code, _, err = writ("run")
    assert code == 2
    assert "did you mean 'reviewer'" in err


# --------------------------------------------------------------------------
# precedence


def test_the_config_supplies_what_no_flag_did(project):
    write_config(
        project,
        {
            "agents": {
                "implementer": {"command": "claude", "model": "sonnet"},
                "reviewer": {"command": "codex", "model": "gpt-5-codex"},
            },
            "run": {"parallel": 3, "order": "depth", "max_rework": 1},
        },
    )
    args, decided = resolve(project, "run")
    assert (args.agent, args.model) == ("claude", "sonnet")
    assert (args.reviewer, args.reviewer_model) == ("codex", "gpt-5-codex")
    assert (args.parallel, args.order, args.max_rework) == (3, "depth", 1)
    assert decided["agent"] == (config.FROM_CONFIG, "claude")


def test_a_flag_beats_the_config(project):
    write_config(project, {"agents": {"implementer": {"command": "claude"}}})
    args, decided = resolve(project, "run", "--agent", "codex")
    assert args.agent == "codex"
    assert decided["agent"] == (config.FROM_FLAG, "codex")


def test_a_flag_beats_the_config_even_when_it_matches_the_old_default(project):
    """The reason argparse no longer carries `default="pi"`.

    With the default in the parser, `--agent pi` and an absent `--agent` are the
    same value, so the config could not tell which had happened and would override
    an explicit choice.
    """
    write_config(project, {"agents": {"implementer": {"command": "claude"}}})
    args, decided = resolve(project, "run", "--agent", "pi")
    assert args.agent == "pi"
    assert decided["agent"] == (config.FROM_FLAG, "pi")


def test_the_builtin_stands_when_nothing_else_speaks(project):
    args, decided = resolve(project, "run")
    assert args.agent == "pi"
    assert args.parallel == 1
    assert args.order == "id"
    assert args.max_rework == 2
    assert decided["parallel"] == (config.FROM_BUILTIN, 1)


def test_one_flag_name_means_a_different_role_per_command(project):
    """`--agent` is four different roles depending on the verb."""
    write_config(
        project,
        {
            "agents": {
                "planner": {"command": "planner-cmd"},
                "critic": {"command": "critic-cmd"},
                "implementer": {"command": "impl-cmd"},
                "reviewer": {"command": "review-cmd"},
            }
        },
    )
    assert resolve(project, "plan", "d.md")[0].agent == "planner-cmd"
    assert resolve(project, "critique")[0].agent == "critic-cmd"
    assert resolve(project, "dispatch", "M01-001")[0].agent == "impl-cmd"
    assert resolve(project, "review")[0].agent == "review-cmd"
    assert resolve(project, "run")[0].agent == "impl-cmd"


def test_planning_keeps_its_timeout_and_implementing_does_not(project):
    """A bounded question gets a ceiling; "implement this" does not.

    An implementation timeout is a guess about work writ has not seen, and a wrong
    guess kills a task that was going fine. Planning and critique are each one
    question, so a ceiling there is a safety net.
    """
    assert resolve(project, "plan", "d.md")[0].timeout == config.AGENT_TIMEOUT
    assert resolve(project, "critique")[0].timeout == config.AGENT_TIMEOUT
    assert resolve(project, "review")[0].timeout == config.AGENT_TIMEOUT
    assert resolve(project, "run")[0].timeout is None
    assert resolve(project, "dispatch", "M01-001")[0].timeout is None


def test_a_configured_timeout_replaces_the_builtin_one(project):
    write_config(project, {"agents": {"planner": {"timeout": 60}}})
    assert resolve(project, "plan", "d.md")[0].timeout == 60


def test_the_reviewers_timeout_is_separable_from_the_implementers(project):
    write_config(
        project,
        {
            "agents": {
                "implementer": {"timeout": 100},
                "reviewer": {"timeout": 900},
            }
        },
    )
    args, _ = resolve(project, "run")
    assert (args.timeout, args.reviewer_timeout) == (100, 900)


def test_review_and_run_share_one_rework_budget(project):
    """It is the same setting, so a project should not have to say it twice."""
    write_config(project, {"run": {"max_rework": 0}})
    assert resolve(project, "run")[0].max_rework == 0
    assert resolve(project, "review")[0].max_rework == 0


def test_the_critic_falls_back_to_the_planning_agent_not_to_pi(project):
    """Unset critic means "whoever plans", which is the documented behaviour."""
    write_config(project, {"agents": {"planner": {"command": "claude"}}})
    args, _ = resolve(project, "plan", "d.md", "--critics")
    assert args.agent == "claude"
    assert args.critic_agent is None  # left for cmd_plan to fall back


# --------------------------------------------------------------------------
# end to end


AGENT = """
import json, os, re, sys
prompt = sys.stdin.read()
path = re.search(r'^  (\\S*verdict\\.json)$', prompt, re.M).group(1)
open(os.environ["WRIT_TEST_LOG"], "a").write(sys.argv[1] + "\\n")
if "MILESTONE GATE" in prompt or "FINAL GATE" in prompt:
    total = int(re.search(r'has (\\d+) criteria', prompt).group(1))
    head = {"decision": "pass"}
elif "You are reviewing ONE completed task" in prompt:
    total = int(re.search(r'has (\\d+) acceptance criteri', prompt).group(1))
    head = {"decision": "accept"}
else:
    total = int(re.search(r'has (\\d+) acceptance criteri', prompt).group(1))
    head = {"outcome": "complete"}
open(path, "w").write(json.dumps(head | {
    "summary": "did it",
    "criteria": [
        {"number": i, "status": "passed", "evidence": "ran: pytest -q"}
        for i in range(1, total + 1)
    ],
}))
"""


def test_a_configured_run_needs_no_flags_at_all(
    writ, project, design, tmp_path, monkeypatch
):
    """The whole point: the project's choices are made once, not per invocation."""
    log = tmp_path / "who.log"
    monkeypatch.setenv("WRIT_TEST_LOG", str(log))
    script = tmp_path / "agent.py"
    script.write_text(AGENT, encoding="utf-8")
    base = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
    writ("init")
    writ("plan", str(design), "--extract", "--auto-approve")
    write_config(
        project,
        {
            "agents": {
                "implementer": {"command": f"{base} implementer"},
                "reviewer": {"command": f"{base} reviewer"},
            }
        },
    )
    code, out, _ = writ("run")
    assert code == 0, out
    invoked = log.read_text().split()
    assert "implementer" in invoked and "reviewer" in invoked
    data = state.load(project)
    roles = {
        run.get("role"): Path(run["command"][-1]).name
        for run in data["runs"].values()
    }
    assert roles["agent"] == "implementer"
    assert roles["reviewer"] == "reviewer"


def test_writ_agents_reports_what_the_project_settled_on(writ, project):
    writ("init")
    write_config(project, {"agents": {"reviewer": {"command": "codex", "model": "o3"}}})
    code, out, _ = writ("agents")
    assert code == 0
    assert "codex" in out and "o3" in out
    assert "a flag overrides any of it" in out


def test_writ_agents_names_the_fallback_when_nothing_is_configured(writ, project):
    """The reviewer default is the one worth warning about."""
    writ("init")
    code, out, _ = writ("agents")
    assert code == 0
    assert "(the implementing agent)" in out
    assert "agrees with itself" in out


def test_resetting_the_project_does_not_discard_its_config(writ, project):
    """`init --force` resets the state, not your preferences.

    The config is not writ's to delete: it says how this project runs agents,
    which is still true of the next plan written in it.
    """
    writ("init")
    write_config(project, {"run": {"parallel": 4}})
    writ("init", "--force")
    assert config.load(project) == {"run": {"parallel": 4}}


# --------------------------------------------------------------------------
# covering every flag


def test_every_configurable_flag_is_read_by_some_command():
    """A field the file accepts but nothing resolves would be a silent lie.

    The same failure as an ignored typo, one level up: `writ init` would write the
    key, validation would accept it, a reader would set it, and no command would
    ever look. So the two tables have to correspond exactly in both directions.
    """
    referenced = {
        default.path
        for command in config.DEFAULTS.values()
        for default in command.values()
    }
    assert sorted(referenced) == sorted(config.FIELDS)


def test_no_written_default_is_guessed_where_commands_disagree():
    """A path two commands default differently has to say which writ writes.

    `agents.reviewer.command` is the case: `writ review` falls back to `pi`, and
    under `writ run` an unset reviewer is the implementing agent. Deriving one of
    those silently would put a value in the generated file that is right for one
    command and wrong for the other, so `_written_default` raises instead.
    """
    for path in config.FIELDS:
        config._written_default(path)  # the guard is in here; no path may trip it

    ambiguous = config.Field(kind="text", doc="x", flag="--x")
    saved = config.FIELDS.get("run.order")
    try:
        config.FIELDS["run.order"] = ambiguous
        with pytest.raises(AssertionError, match="commands disagree"):
            # two commands already name run.max_rework; point one at run.order
            original = config.DEFAULTS["review"]["max_rework"]
            config.DEFAULTS["review"]["max_rework"] = config.Default("run.order", "x")
            try:
                config._written_default("run.order")
            finally:
                config.DEFAULTS["review"]["max_rework"] = original
    finally:
        config.FIELDS["run.order"] = saved


def test_every_flag_writ_can_configure_parses_as_unset(project):
    """A configurable flag cannot carry its own argparse default.

    This is what makes "a flag always wins" true of a boolean. With
    `action="store_true"`, an absent `--quiet` and an explicit one are both
    `False`, and `apply` — which fills only what is None — could not tell a
    project's `quiet: true` from a user overriding it. So every attribute a config
    can set has to arrive as None when no flag was given.
    """
    parser = build_parser()
    action = next(a for a in parser._actions if a.dest == "command")
    for command, wanted in config.DEFAULTS.items():
        if command not in action.choices:
            continue
        # positionals only; every configurable flag is optional by construction
        required = {
            "plan": ["d.md"],
            "show": ["M01"],
            "logs": ["r1"],
            "dispatch": ["M01-001"],
        }.get(command, [])
        args = action.choices[command].parse_args(required)
        for attribute in wanted:
            if not hasattr(args, attribute):
                continue
            assert getattr(args, attribute) is None, f"{command} --{attribute}"


def test_a_configured_boolean_is_honoured_and_still_overridable(project):
    """`plan.stages: false` turns the pipeline off, and `--stages` turns it back on.

    Both halves, because a boolean that a flag cannot argue with is not a default,
    it is a policy — and `--no-stages` swapping the staged pipeline for the older
    single-shot planner is too consequential to be unsayable for one run.
    """
    write_config(project, {"plan": {"stages": False, "gates": False}})
    args, decided = resolve(project, "plan", "d.md")
    assert (args.stages, args.gates) == (False, False)
    assert decided["stages"] == (config.FROM_CONFIG, False)

    args, decided = resolve(project, "plan", "d.md", "--stages", "--gates")
    assert (args.stages, args.gates) == (True, True)
    assert decided["stages"] == (config.FROM_FLAG, True)


def test_a_negative_flag_is_configured_positively_and_beaten_both_ways(project):
    """`serve.open` reads the way a person would say it; `--no-open` is its negative.

    The config holds the setting, argparse holds `no_open`, and `--open` exists so
    a project that turned the browser off can still ask for it once. Without that
    counterpart the two negatively-spelled settings would be the only ones a flag
    could not argue with in both directions.
    """
    write_config(project, {"serve": {"open": False}, "status": {"clear": False}})
    args, decided = resolve(project, "serve")
    assert args.no_open is True
    # reported as the setting, not as the flag it reached argparse through
    assert decided["no_open"] == (config.FROM_CONFIG, False)
    assert resolve(project, "serve", "--open")[0].no_open is False
    assert resolve(project, "status")[0].no_clear is True
    assert resolve(project, "status", "--clear")[0].no_clear is False


def test_false_in_a_config_is_a_value_and_not_an_absence(project):
    """`quiet: false` has to survive the lookup that discards empty strings.

    `_lookup` treats `""` as unset — an empty agent command is not an answer — and
    a check that discarded anything falsy with it would make every boolean in the
    file unsettable in one direction, silently.
    """
    write_config(project, {"common": {"quiet": False}, "run": {"max_rework": 0}})
    args, decided = resolve(project, "run")
    assert args.quiet is False
    assert decided["quiet"] == (config.FROM_CONFIG, False)
    assert args.max_rework == 0
    assert decided["max_rework"] == (config.FROM_CONFIG, 0)


def test_the_critics_setting_carries_three_states(project):
    """Off, all, or exactly these — the same three the flag has.

    `plan.critics` says *whether* to run them, `critique.critics` says *which*, so
    a project that has settled on a subset states it once and both commands honour
    it. Absent runs nothing under `writ plan`, because each critic costs an agent
    run and writ does not spend those unasked.
    """
    from writ import commands, critics

    args, _ = resolve(project, "plan", "d.md")
    assert args.critics is False
    assert commands._critics_requested(args) is False

    write_config(project, {"plan": {"critics": True}})
    args, _ = resolve(project, "plan", "d.md")
    assert commands._critics_requested(args) is True
    assert commands._chosen_critics(args) == list(critics.CRITICS)

    write_config(
        project, {"plan": {"critics": True}, "critique": {"critics": ["scope"]}}
    )
    args, _ = resolve(project, "plan", "d.md")
    assert [c.name for c in commands._chosen_critics(args)] == ["scope"]
    # and a flag naming them outright still wins over the configured set
    args, _ = resolve(project, "plan", "d.md", "--critics", "coverage")
    assert [c.name for c in commands._chosen_critics(args)] == ["coverage"]


def test_an_unknown_critic_is_refused_with_the_name_it_meant():
    with pytest.raises(WritError, match=r"did you mean 'coverage'"):
        config.validate({"critique": {"critics": ["coverge"]}})


def test_a_critic_list_must_be_a_list():
    with pytest.raises(WritError, match="expected a list of names"):
        config.validate({"critique": {"critics": "coverage"}})


def test_an_empty_critic_list_is_none_rather_than_all(project):
    """`"critics": []` is a project saying none, which is not saying nothing."""
    assert config.validate({"critique": {"critics": []}}) == {"critique": {"critics": []}}


def test_a_port_outside_the_range_is_refused():
    with pytest.raises(WritError, match="expected a port from 1 to 65535"):
        config.validate({"serve": {"port": 70000}})


def test_an_interval_must_be_a_positive_number():
    assert config.validate({"status": {"interval": 0.5}}) == {"status": {"interval": 0.5}}
    with pytest.raises(WritError, match="number of seconds above 0"):
        config.validate({"status": {"interval": 0}})


def test_a_flag_setting_refuses_a_non_boolean():
    with pytest.raises(WritError, match="expected true or false"):
        config.validate({"plan": {"stages": "yes"}})


def test_the_analysis_stages_have_their_own_role(project):
    """Reading a document is cheaper work than synthesising a plan from it.

    So `agents.stage` can point somewhere cheaper than the planner. Unset it is
    the planner, which is what writ did before the role existed.
    """
    write_config(project, {"agents": {"stage": {"command": "codex", "timeout": 600}}})
    args, decided = resolve(project, "plan", "d.md")
    assert args.stage_agent == "codex"
    assert args.stage_timeout == 600
    assert decided["stage_agent"] == (config.FROM_CONFIG, "codex")

    args, _ = resolve(project, "plan", "d.md")
    assert args.agent == "pi"  # the planner is untouched by the stage setting


#: the positional each command needs before its flags can be parsed at all
POSITIONALS = {
    "plan": ["d.md"],
    "show": ["M01"],
    "logs": ["r1"],
    "dispatch": ["M01-001"],
}


def test_a_generated_config_resolves_exactly_like_no_config(writ, project, tmp_path):
    """The whole promise of the generated file, checked per command per argument.

    `writ init` writes every field at writ's own default, so a project that has one
    and a project that has none must reach identical arguments — otherwise `writ
    init` quietly decides something on the way past, which is the one thing the file
    is not allowed to do.

    This caught a real bug. `status.clear` and `serve.open` are stated positively
    and reach argparse as `no_clear` and `no_open`; inverting the config value but
    not the builtin made a generated config resolve to the *opposite* of no config
    for both, and nothing else here would have noticed.
    """
    writ("init")
    bare = tmp_path / "no-config"
    bare.mkdir()
    for command, wanted in config.DEFAULTS.items():
        argv = [command, *POSITIONALS.get(command, [])]
        resolved = []
        for root in (project, bare):
            args, _ = resolve(root, *argv)
            resolved.append(
                {a: getattr(args, a) for a in wanted if hasattr(args, a)}
            )
        assert resolved[0] == resolved[1], command


def test_the_generated_file_holds_every_field_and_nothing_else(writ, project):
    """It is the schema, so it has to be the whole schema and no more than it."""
    writ("init")
    raw = json.loads(config.config_file(project).read_text())
    flat: dict[str, object] = {}

    def walk(node: dict, prefix: str = "") -> None:
        for key, value in node.items():
            if config.is_comment(key):
                continue
            path = f"{prefix}{key}"
            walk(value, f"{path}.") if isinstance(value, dict) else flat.update(
                {path: value}
            )

    walk(raw)
    assert sorted(flat) == sorted(config.FIELDS)
    assert flat == config.DEFAULT_VALUES
