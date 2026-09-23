import json

import pytest

from writ import agents
from writ.state import WritError


# --------------------------------------------------------------------------
# the bug this module exists for: agents hang without a headless flag


def test_pi_gets_the_print_flag_so_it_does_not_open_a_tui():
    # bare `pi` opens an interactive session and ignores piped stdin forever
    assert agents.resolve("pi").command == ["pi", "-p"]


def test_claude_gets_the_print_flag():
    assert agents.resolve("claude").command == ["claude", "-p"]


def test_codex_reads_the_prompt_from_stdin_via_exec():
    assert agents.resolve("codex").command == ["codex", "exec", "-"]


def test_opencode_uses_its_run_subcommand():
    assert agents.resolve("opencode").command == ["opencode", "run"]


def test_gemini_needs_no_flag_because_a_pipe_is_enough():
    assert agents.resolve("gemini").command == ["gemini"]


def test_an_absolute_path_is_still_recognised():
    resolved = agents.resolve("/opt/homebrew/bin/pi")
    assert resolved.command == ["/opt/homebrew/bin/pi", "-p"]
    assert resolved.name == "pi"


# --------------------------------------------------------------------------
# explicit tokens win


def test_an_explicit_print_flag_is_not_duplicated():
    assert agents.resolve("pi -p").command == ["pi", "-p"]
    assert agents.resolve("claude --print").command == ["claude", "--print"]


def test_an_explicit_mode_flag_suppresses_the_default():
    # `pi --mode json` is already non-interactive
    assert agents.resolve("pi --mode json").command == ["pi", "--mode", "json"]


def test_an_explicit_subcommand_is_not_repeated():
    assert agents.resolve("codex exec").command == ["codex", "exec", "-"]
    assert agents.resolve("codex exec -").command == ["codex", "exec", "-"]


def test_an_optout_supplied_after_the_separator_also_counts():
    assert agents.resolve("pi", ["-p", "--thinking", "high"]).command == [
        "pi",
        "-p",
        "--thinking",
        "high",
    ]


# --------------------------------------------------------------------------
# model translation


def test_model_is_translated_to_each_agents_own_flag():
    assert agents.resolve("pi", model="sonnet").command == [
        "pi", "-p", "--model", "sonnet",
    ]
    assert agents.resolve("codex", model="gpt-5-codex").command == [
        "codex", "exec", "--model", "gpt-5-codex", "-",
    ]


def test_model_lands_before_the_stdin_marker():
    # `codex exec - --model x` would be read as a prompt argument
    command = agents.resolve("codex", model="x").command
    assert command[-1] == "-"


def test_model_given_twice_is_rejected():
    with pytest.raises(WritError, match="given twice"):
        agents.resolve("pi --model a", model="b")


def test_model_for_an_agent_without_a_known_flag_is_rejected():
    with pytest.raises(WritError, match="does not know a model flag"):
        agents.resolve("amp", model="x")


def test_model_for_an_unknown_agent_explains_the_alternative():
    with pytest.raises(WritError, match="does not know how to pass a model"):
        agents.resolve("my-agent.sh", model="x")


# --------------------------------------------------------------------------
# unknown agents pass through, with an honest warning


def test_an_unknown_agent_is_passed_through_untouched():
    resolved = agents.resolve("my-agent.sh --flag", ["extra"])
    assert resolved.command == ["my-agent.sh", "--flag", "extra"]
    assert resolved.profile is None
    assert "cannot confirm it runs without a terminal" in resolved.warning


def test_a_known_agent_carries_no_warning():
    assert agents.resolve("pi").warning is None


def test_an_empty_agent_command_is_rejected():
    with pytest.raises(WritError, match="agent command is empty"):
        agents.resolve("   ")


def test_hang_hint_points_at_the_interactive_default_for_unknown_agents():
    hint = agents.hang_hint(agents.resolve("mystery"))
    assert "interactive session" in hint and "-p" in hint


def test_hang_hint_for_a_known_agent_blames_something_else():
    hint = agents.hang_hint(agents.resolve("pi"))
    assert "should be non-interactive" in hint
    assert "pi -p" in hint


def test_a_silent_exit_is_blamed_on_the_invocation_not_the_report():
    """An agent CLI that cannot reach its model exits 0 and says nothing.

    Writ's own reading of that run is "no verdict", which sends the operator to
    the transcript. The transcript is empty, so the hint has to redirect them to
    the model and the credentials instead.
    """
    hint = agents.silent_exit_hint(agents.resolve("pi", model="vendor/some-model"), 0)
    assert "never ran" in hint
    assert "pi -p --model vendor/some-model" in hint
    assert "authenticated" in hint


def test_the_silent_exit_hint_omits_model_advice_for_an_agent_without_one():
    """No model flag means writ cannot suggest checking one."""
    hint = agents.silent_exit_hint(agents.resolve("amp"), 0)
    assert "by hand" in hint
    assert "authenticated" not in hint


# --------------------------------------------------------------------------
# the agents command


def test_agents_command_lists_headless_invocations(writ):
    code, out, _ = writ("agents")
    assert code == 0
    assert "pi -p" in out
    assert "codex exec -" in out
    assert "--model" in out


def test_agents_command_previews_one_command(writ):
    code, out, _ = writ("agents", "--agent", "codex", "--model", "gpt-5-codex")
    assert code == 0
    assert out.strip().startswith("codex exec --model gpt-5-codex -")


def test_agents_command_json_reports_whether_it_is_known(writ):
    _, out, _ = writ("--json", "agents", "--agent", "pi")
    payload = json.loads(out)
    assert payload["command"] == ["pi", "-p"]
    assert payload["known"] is True

    _, out, err = writ("--json", "agents", "--agent", "mystery-agent")
    payload = json.loads(out)
    assert payload["known"] is False
    assert payload["warning"]


def test_agents_command_needs_no_project(tmp_path):
    from tests.conftest import run

    code, out, _ = run("--root", str(tmp_path), "agents")
    assert code == 0 and "pi" in out


# --------------------------------------------------------------------------
# asking for the event stream, so a long run can be watched while it runs


def test_events_are_not_asked_for_unless_wanted():
    assert agents.resolve("pi", model="x").command == ["pi", "-p", "--model", "x"]
    assert agents.resolve("pi").event_shape == ""


def test_pi_asks_for_json_events_and_reports_its_shape():
    resolved = agents.resolve("pi", events=True)
    assert resolved.command == ["pi", "-p", "--mode", "json"]
    assert resolved.event_shape == "pi"


def test_claude_asks_for_its_own_stream_format():
    resolved = agents.resolve("claude", events=True)
    assert resolved.command == ["claude", "-p", "--output-format", "stream-json", "--verbose"]
    assert resolved.event_shape == "claude"


def test_an_agent_with_no_adapter_is_left_alone():
    # codex has no event shape writ knows how to read, so nothing is added and
    # nothing claims to be parseable
    resolved = agents.resolve("codex", events=True)
    assert resolved.command == ["codex", "exec", "-"]
    assert resolved.event_shape == ""


def test_an_operator_who_set_the_mode_themselves_keeps_it():
    """Their flag decides the format, so writ must not claim a shape it may not get."""
    resolved = agents.resolve("pi --mode rpc", events=True)
    assert resolved.command == ["pi", "--mode", "rpc"]
    assert resolved.event_shape == ""


def test_the_event_flag_lands_before_a_stdin_suffix():
    """A trailing `-` must stay trailing, or the agent reads the flag as the prompt."""
    profile = agents.AgentProfile(
        prefix=("exec",), suffix=("-",), event_args=("--json",), event_shape="pi"
    )
    agents.PROFILES["fake-agent"] = profile
    try:
        assert agents.resolve("fake-agent", events=True).command == [
            "fake-agent", "exec", "--json", "-"
        ]
    finally:
        del agents.PROFILES["fake-agent"]
