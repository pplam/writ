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

from writ import config, state
from writ.cli import build_parser
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
    assert sorted(loaded["agents"]) == ["critic", "implementer", "planner", "reviewer"]
    assert loaded["run"] == {"parallel": 3, "order": "id", "max_rework": 2}


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
    writ("plan", str(design), "--extract")
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
