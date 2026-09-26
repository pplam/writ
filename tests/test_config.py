"""Per-project defaults in `.writ/config.yaml`.

Two things are being tested here, and the second is the one that matters. The
first is that a config is read and honoured at all. The second is that a flag
still beats it and a typo in it is refused, because a config file that is quietly
half-applied is worse than no config file: the whole point of writing the reviewer
down is that you stop having to remember it, and a silently ignored `reviewr`
takes that away while leaving the file on disk claiming otherwise.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from writ import config, orchestrator, yamlish
from writ.cli import build_parser
from writ.model import DEFAULT_MAX_REWORK
from writ.server import DEFAULT_PORT
from writ.state import WritError


def _yaml(payload: dict, indent: int = 0) -> str:
    lines = []
    for key, value in payload.items():
        if isinstance(value, dict):
            lines.append(" " * indent + f"{key}:")
            lines.append(_yaml(value, indent + 2))
        else:
            lines.append(" " * indent + f"{key}: {yamlish.scalar(value)}")
    return "\n".join(line for line in lines if line)


def write_config(root: Path, payload: dict | str) -> Path:
    path = config.config_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else _yaml(payload) + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def resolve(root: Path, *argv: str):
    """Parse a command line the way `main` does, then apply the config to it."""
    args = build_parser().parse_args(["--root", str(root), *argv])
    decided = config.apply(args, config.load(root))
    return args, decided


# --------------------------------------------------------------------------
# the YAML subset


def test_yamlish_reads_nested_mappings_scalars_and_comments():
    text = """
# a comment
agents:
  planner:
    command: "claude -p"   # trailing comment
    model: opus
    timeout: 60
  reviewer:
    command: 'it''s'
    model: ~
plan:
  critics: true
  ratio: 0.5
  empty:
  tags: [a, "b, c", 3]
  items:
    - one
    - 2
"""
    assert yamlish.loads(text) == {
        "agents": {
            "planner": {"command": "claude -p", "model": "opus", "timeout": 60},
            "reviewer": {"command": "it's", "model": None},
        },
        "plan": {
            "critics": True,
            "ratio": 0.5,
            "empty": None,
            "tags": ["a", "b, c", 3],
            "items": ["one", 2],
        },
    }


def test_yamlish_keeps_a_hash_inside_a_value():
    assert yamlish.loads('a: "x # y"\nb: c#d') == {"a": "x # y", "b": "c#d"}


def test_an_empty_document_is_an_empty_mapping():
    assert yamlish.loads("# only a comment\n\n") == {}


@pytest.mark.parametrize(
    "text, message",
    [
        ("a: 1\na: 2", "appears twice"),
        ("a:\n\tb: 1", "tabs"),
        ("a: 1\n  b: 2", "unexpected indentation"),
        ("just words", "expected `key: value`"),
        ("a: &anchor x", "does not read"),
        ("a: {b: 1}", "does not read"),
        ('a: "open', "unterminated"),
        ("---\na: 1", "one document"),
    ],
)
def test_yamlish_refuses_what_it_does_not_read_and_names_the_line(text, message):
    with pytest.raises(WritError, match=r"line \d+: .*" + message):
        yamlish.loads(text)


@pytest.mark.parametrize(
    "value",
    [None, True, False, 0, 7, 1.5, "pi", "codex exec", "", "true", "12", "a: b",
     "# not a comment", 'say "hi"', "back\\slash", "-dash", ["x", 1]],
)
def test_a_written_scalar_reads_back_as_itself(value):
    assert yamlish.loads(f"k: {yamlish.scalar(value)}") == {"k": value}


# --------------------------------------------------------------------------
# reading it


def test_no_config_is_not_an_error(project):
    """A project that never needed one should not have to have one."""
    assert config.load(project) == {}


def test_a_malformed_config_names_the_file_and_line(project):
    write_config(project, "agents:\n  planner: [unclosed\n")
    with pytest.raises(WritError, match=r"config\.yaml: line 2"):
        config.load(project)


def test_a_legacy_json_config_alone_is_refused_with_the_fix(project):
    """Not read and not ignored: the settings in it would silently stop applying."""
    legacy = config.legacy_file(project)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("{}", encoding="utf-8")
    with pytest.raises(WritError, match="run `writ init`"):
        config.load(project)


def test_the_shipped_example_validates():
    """`config.example.yaml` is the documentation, so it has to be loadable."""
    example = Path(__file__).resolve().parent.parent / "config.example.yaml"
    loaded = config.validate(yamlish.loads(example.read_text()))
    assert sorted(loaded["agents"]) == sorted(config.ROLES)
    assert loaded["run"] == {"parallel": 3, "max_rework": 2}
    assert loaded["plan"]["critics"] is True


def test_init_writes_a_starter_config(writ, project):
    """`writ init` leaves a config behind, because an unknown default is unset."""
    code, out, _ = writ("init")
    assert code == 0
    assert "config.yaml" in out
    assert config.config_file(project).exists()


def test_the_starter_config_holds_writs_own_defaults(writ, project):
    """Every value is what writ would have done, so `init` decides nothing."""
    writ("init")
    loaded = config.load(project)
    assert loaded["agents"] == {
        "planner": {"command": "pi", "timeout": config.AGENT_TIMEOUT},
        "critic": {"timeout": config.AGENT_TIMEOUT},
        "implementer": {"command": "pi"},
        # unset, so review still falls back to the implementing agent rather
        # than being pinned to `pi` by a generated file
        "reviewer": {},
    }
    assert loaded["plan"] == {
        "critics": False,
        "repair": False,
        "max_rounds": config.MAX_REPAIR_ROUNDS,
        "auto_approve": False,
    }
    assert loaded["run"] == {"parallel": 1, "max_rework": DEFAULT_MAX_REWORK}
    assert loaded["serve"] == {"port": DEFAULT_PORT}


def test_the_starter_config_names_every_field_and_nothing_else(writ, project):
    writ("init")
    raw = yamlish.loads(config.config_file(project).read_text())
    flat = {
        f"{section}.{key}": value
        for section, body in raw.items()
        for key, value in body.items()
        if section != "agents"
    }
    flat.update(
        {
            f"agents.{role}.{key}": value
            for role, body in raw["agents"].items()
            for key, value in body.items()
        }
    )
    assert sorted(flat) == sorted(config.FIELDS)
    assert flat == config.DEFAULT_VALUES


def test_the_starter_config_comments_every_setting():
    doc = config.default_document()
    prose = " ".join(doc.replace("#", "").split())
    for path, entry in config.FIELDS.items():
        if not path.startswith("agents."):
            assert entry.doc and entry.doc in prose, path
    for section in config.SECTIONS:
        assert f"\n{section}:\n" in doc


def test_the_config_holds_only_the_core_settings():
    """The file is for the choices a project makes once, not a mirror of every flag."""
    assert config.SECTIONS == ("agents", "plan", "run", "decisions", "serve")
    assert "stage" not in config.ROLES
    assert len(config.FIELDS) == 4 * 3 + 5 + 3 + 1 + 1


def test_init_keeps_an_existing_config(writ, project):
    path = write_config(project, {"run": {"parallel": 4}})
    code, out, _ = writ("init", "--force")
    assert code == 0
    assert "kept your existing config.yaml" in out
    assert path.read_text() == "run:\n  parallel: 4\n"


def test_init_converts_a_legacy_json_config(writ, project):
    """The settings that still exist carry over; the rest are dropped."""
    legacy = config.legacy_file(project)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        json.dumps(
            {
                "_": ["a comment"],
                "agents": {
                    "reviewer": {"command": "codex", "model": "gpt-5-codex"},
                    "stage": {"command": "dropped"},
                },
                "run": {"parallel": 3, "order": "depth"},
                "plan": {"critics": True, "stages": False},
                "adjudicate": {"max_rounds": 4},
                "serve": {"host": "0.0.0.0"},
            }
        ),
        encoding="utf-8",
    )
    code, out, err = writ("init")
    assert code == 0, err
    assert "from your config.json" in out
    assert legacy.exists()
    loaded = config.load(project)
    assert loaded["agents"]["reviewer"] == {"command": "codex", "model": "gpt-5-codex"}
    assert loaded["run"]["parallel"] == 3
    assert loaded["plan"]["critics"] is True
    assert loaded["plan"]["max_rounds"] == 4


# --------------------------------------------------------------------------
# validation


def test_an_unknown_role_is_refused_with_a_suggestion(project):
    write_config(project, {"agents": {"reviewr": {"command": "codex"}}})
    with pytest.raises(WritError, match="'reviewr' .did you mean 'reviewer"):
        config.load(project)


def test_an_unknown_section_is_refused():
    with pytest.raises(WritError, match="unknown section 'models'"):
        config.validate({"models": {}})


def test_a_removed_setting_is_now_unknown():
    with pytest.raises(WritError, match="unknown key 'order'"):
        config.validate({"run": {"order": "depth"}})
    with pytest.raises(WritError, match="unknown role 'stage'"):
        config.validate({"agents": {"stage": {}}})


def test_an_unknown_role_key_is_refused():
    with pytest.raises(WritError, match="unknown key 'agent'"):
        config.validate({"agents": {"reviewer": {"agent": "codex"}}})


def test_a_role_must_be_a_mapping():
    with pytest.raises(WritError, match="expected a mapping"):
        config.validate({"agents": {"reviewer": "codex"}})


@pytest.mark.parametrize(
    "raw, message",
    [
        ({"agents": {"planner": {"command": "  "}}}, "not an empty string"),
        ({"agents": {"planner": {"model": 4}}}, "expected a string"),
        ({"run": {"parallel": 0}}, "above 0"),
        ({"run": {"parallel": True}}, "above 0"),
        ({"run": {"max_rework": -1}}, "0 or more"),
        ({"serve": {"port": 70000}}, "port from 1 to 65535"),
        ({"plan": {"critics": "yes"}}, "true or false"),
    ],
)
def test_a_bad_value_is_refused(raw, message):
    with pytest.raises(WritError, match=message):
        config.validate(raw)


def test_zero_rework_and_zero_rounds_are_meaningful():
    assert config.validate({"run": {"max_rework": 0}, "plan": {"max_rounds": 0}}) == {
        "run": {"max_rework": 0},
        "plan": {"max_rounds": 0},
    }


def test_null_is_the_same_as_absent():
    raw = {"agents": {"reviewer": {"command": None}}, "run": None}
    assert config.validate(raw) == {"agents": {"reviewer": {}}, "run": {}}


def test_a_bad_config_fails_before_any_command_runs(writ, project):
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
            "run": {"parallel": 3, "max_rework": 1},
        },
    )
    args, decided = resolve(project, "run")
    assert (args.agent, args.model) == ("claude", "sonnet")
    assert (args.reviewer, args.reviewer_model) == ("codex", "gpt-5-codex")
    assert (args.parallel, args.max_rework) == (3, 1)
    assert decided["agent"] == (config.FROM_CONFIG, "claude")


def test_a_flag_beats_the_config(project):
    write_config(project, {"agents": {"implementer": {"command": "claude"}}})
    args, decided = resolve(project, "run", "--agent", "codex")
    assert args.agent == "codex"
    assert decided["agent"] == (config.FROM_FLAG, "codex")


def test_a_flag_beats_the_config_even_when_it_matches_the_old_default(project):
    """The reason argparse does not carry `default="pi"`."""
    write_config(project, {"agents": {"implementer": {"command": "claude"}}})
    args, decided = resolve(project, "run", "--agent", "pi")
    assert args.agent == "pi"
    assert decided["agent"] == (config.FROM_FLAG, "pi")


def test_the_builtin_stands_when_nothing_else_speaks(project):
    args, decided = resolve(project, "run")
    assert args.agent == "pi"
    assert args.parallel == 1
    assert args.order == orchestrator.DEFAULT_ORDER
    assert args.max_rework == DEFAULT_MAX_REWORK
    assert decided["parallel"] == (config.FROM_BUILTIN, 1)


def test_flags_outside_the_config_keep_their_builtins(project):
    """Dropping a setting from the file must not drop its default."""
    args, _ = resolve(project, "plan", "d.md")
    assert (args.level, args.stages, args.gates, args.flat) == (
        config.DEFAULT_LEVEL, True, True, False
    )
    args, _ = resolve(project, "status")
    assert args.interval == config.DEFAULT_INTERVAL and args.no_clear is False
    args, _ = resolve(project, "serve")
    assert args.no_open is False and args.port == DEFAULT_PORT


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
    assert resolve(project, "adjudicate")[0].agent == "critic-cmd"
    assert resolve(project, "dispatch", "M01-001")[0].agent == "impl-cmd"
    assert resolve(project, "review")[0].agent == "review-cmd"
    assert resolve(project, "run")[0].agent == "impl-cmd"


def test_planning_keeps_its_timeout_and_implementing_does_not(project):
    """A bounded question gets a ceiling; "implement this" does not."""
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
        {"agents": {"implementer": {"timeout": 100}, "reviewer": {"timeout": 900}}},
    )
    args, _ = resolve(project, "run")
    assert (args.timeout, args.reviewer_timeout) == (100, 900)


def test_review_and_run_share_one_rework_budget(project):
    """It is the same setting, so a project should not have to say it twice."""
    write_config(project, {"run": {"max_rework": 0}})
    assert resolve(project, "run")[0].max_rework == 0
    assert resolve(project, "review")[0].max_rework == 0


def test_plan_and_adjudicate_share_one_repair_budget(project):
    write_config(project, {"plan": {"max_rounds": 5}})
    assert resolve(project, "plan", "d.md")[0].max_rounds == 5
    assert resolve(project, "adjudicate")[0].max_rounds == 5


def test_plan_switches_come_from_the_config_and_a_flag_still_wins(project):
    write_config(
        project,
        {"plan": {"critics": True, "repair": True, "auto_approve": True,
                  "instructions": "keep it small"}},
    )
    args, decided = resolve(project, "plan", "d.md")
    assert (args.critics, args.repair, args.auto_approve) == (True, True, True)
    assert args.instructions == "keep it small"
    assert decided["repair"] == (config.FROM_CONFIG, True)
    args, decided = resolve(project, "plan", "d.md", "--critics", "coverage")
    assert args.critics == ["coverage"]


def test_autonomous_mode_repairs_and_approves_unless_a_flag_says_not(project):
    args, decided = resolve(project, "plan", "d.md", "--autonomous")
    assert (args.autonomous, args.repair, args.auto_approve) == (True, True, True)
    assert decided["repair"] == (config.FROM_AUTONOMOUS, True)
    # what `writ build --autonomous --no-auto-approve` hands its planning step
    args = build_parser().parse_args(
        ["--root", str(project), "plan", "d.md", "--autonomous"]
    )
    args.auto_approve = False
    config.apply(args, config.load(project))
    assert (args.repair, args.auto_approve) == (True, False)


def test_autonomous_mode_can_be_the_projects_default(project):
    write_config(project, {"decisions": {"autonomous": True}})
    for argv in (("plan", "d.md"), ("adjudicate",), ("run",)):
        assert resolve(project, *argv)[0].autonomous is True, argv
    assert resolve(project, "run", "--no-autonomous")[0].autonomous is False
    assert resolve(project, "plan", "d.md")[0].auto_approve is True


def test_the_critic_follows_the_critic_role_under_plan(project):
    write_config(project, {"agents": {"critic": {"command": "codex", "model": "o"}}})
    args, _ = resolve(project, "plan", "d.md")
    assert (args.critic_agent, args.critic_model) == ("codex", "o")
    assert (args.adjudicator_agent, args.adjudicator_model) == ("codex", "o")


def test_the_serve_port_comes_from_the_config(project):
    write_config(project, {"serve": {"port": 9000}})
    assert resolve(project, "serve")[0].port == 9000
    assert resolve(project, "serve", "--port", "9001")[0].port == 9001


# --------------------------------------------------------------------------
# the tables agree


def test_every_field_is_used_by_some_command():
    referenced = {
        default.path
        for command in config.DEFAULTS.values()
        for default in command.values()
        if default.path
    }
    assert referenced == set(config.FIELDS)


def test_every_config_path_a_command_reads_is_a_field():
    for command, wanted in config.DEFAULTS.items():
        for attribute, default in wanted.items():
            known = default.path is None or default.path in config.FIELDS
            assert known, (command, attribute)


def test_agents_reports_the_configured_roles(writ, project):
    write_config(project, {"agents": {"reviewer": {"command": "codex"}}})
    code, out, _ = writ("agents")
    assert code == 0
    assert "reviewer" in out and "codex" in out
    assert "stage" not in out.split("ROLE", 1)[1]
    assert "config.yaml" in out
