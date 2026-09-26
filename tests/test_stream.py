"""The event renderer: what a watching person sees, and what gets recorded.

The event shapes here are real ones, copied from `pi --mode json` and from
claude's `--output-format stream-json`. A renderer tested against invented shapes
would pass while showing nothing on the runs that matter.
"""
import json

from writ import stream


def render(shape: str, events: list[dict]) -> tuple[list[str], str, list[str]]:
    """Feed whole events through a renderer, collecting all three products."""
    renderer = stream.Renderer(shape)
    activity: list[str] = []
    text = ""
    reasons: list[str] = []
    for event in events:
        out = renderer.feed(json.dumps(event) + "\n")
        activity += out.activity
        text += out.text
        if out.stop_reason is not None:
            reasons.append(out.stop_reason)
    return activity, text, reasons


# --------------------------------------------------------------------------
# the bug this module exists for: a run that works in silence looks hung


def test_a_tool_call_is_shown_as_it_starts():
    activity, _, _ = render("pi", [
        {"type": "tool_execution_start", "toolCallId": "c1", "toolName": "read",
         "args": {"path": "/repo/docs/system-design.md"}},
    ])
    assert activity == ["· read /repo/docs/system-design.md"]


def test_a_bash_call_is_identified_by_its_command():
    activity, _, _ = render("pi", [
        {"type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash",
         "args": {"command": "ls -la && echo done"}},
    ])
    assert activity == ["· bash ls -la && echo done"]


def test_thinking_says_so_when_it_begins_not_when_it_ends():
    """The point is showing life during the minutes thinking can take."""
    activity, _, _ = render("pi", [
        {"type": "message_update",
         "assistantMessageEvent": {"type": "thinking_start", "contentIndex": 0}},
    ])
    assert activity == ["~ thinking…"]


def test_a_finished_thought_shows_its_first_line():
    activity, _, _ = render("pi", [
        {"type": "message_update", "assistantMessageEvent": {
            "type": "thinking_end", "contentIndex": 0,
            "content": "The repository is nearly empty.\nLet me check the structure.",
        }},
    ])
    assert activity == ["~ The repository is nearly empty."]


def test_a_failed_tool_call_is_reported_by_name():
    activity, _, _ = render("pi", [
        {"type": "tool_execution_start", "toolCallId": "c1", "toolName": "read",
         "args": {"path": "/nope"}},
        {"type": "tool_execution_end", "toolCallId": "c1", "toolName": "read",
         "isError": True, "result": {"content": []}},
    ])
    assert activity == ["· read /nope", "! read failed"]


def test_a_successful_tool_call_is_not_mentioned_twice():
    activity, _, _ = render("pi", [
        {"type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash",
         "args": {"command": "true"}},
        {"type": "tool_execution_end", "toolCallId": "c1", "toolName": "bash",
         "isError": False, "result": {"content": []}},
    ])
    assert activity == ["· bash true"]


# --------------------------------------------------------------------------
# the transcript stays what text mode would have written


def test_what_the_agent_said_becomes_the_transcript():
    _, text, _ = render("pi", [
        {"type": "message_update", "assistantMessageEvent": {
            "type": "text_end", "contentIndex": 0, "content": "Survey complete.",
        }},
    ])
    assert text == "Survey complete.\n"


def test_thinking_and_tool_calls_never_reach_the_transcript():
    """Everything downstream parses stdout.log; it must hold speech and nothing else."""
    _, text, _ = render("pi", [
        {"type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash",
         "args": {"command": "cat secrets"}},
        {"type": "message_update", "assistantMessageEvent": {
            "type": "thinking_end", "contentIndex": 0, "content": "deliberating",
        }},
        {"type": "message_update", "assistantMessageEvent": {
            "type": "text_end", "contentIndex": 0, "content": "done",
        }},
    ])
    assert text == "done\n"
    assert "bash" not in text and "deliberating" not in text


def test_a_json_artifact_printed_as_speech_survives_intact():
    """The recovery path in planning greps this text for a fenced block."""
    said = '```json\n{"milestones": []}\n```'
    _, text, _ = render("pi", [
        {"type": "message_update", "assistantMessageEvent": {
            "type": "text_end", "contentIndex": 0, "content": said,
        }},
    ])
    from writ.planning import extract_json

    assert extract_json(text) == '{"milestones": []}'


# --------------------------------------------------------------------------
# the stop reason, which is the thing no rendering may lose


def test_a_truncated_turn_is_recognisable():
    _, _, reasons = render("pi", [
        {"type": "turn_end", "message": {"role": "assistant", "stopReason": "length"}},
    ])
    assert reasons == ["length"]
    assert stream.truncated(reasons)


def test_a_finished_turn_is_not_called_truncated():
    _, _, reasons = render("pi", [
        {"type": "turn_end", "message": {"role": "assistant", "stopReason": "toolUse"}},
        {"type": "turn_end", "message": {"role": "assistant", "stopReason": "stop"}},
    ])
    assert not stream.truncated(reasons)


def test_only_the_last_turn_decides_whether_the_run_was_cut_off():
    """A run truncates mid-way and recovers; what matters is how it ended."""
    assert not stream.truncated(["length", "toolUse", "stop"])
    assert stream.truncated(["toolUse", "toolUse", "length"])


def test_no_turns_at_all_is_not_truncation():
    assert not stream.truncated([])


# --------------------------------------------------------------------------
# claude's shape, which packs whole messages rather than deltas


def test_claude_tool_calls_and_speech_are_both_rendered():
    activity, text, _ = render("claude", [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Reading the design doc."},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/repo/doc.md"}},
        ]}},
    ])
    assert activity == ["> Reading the design doc.", "· Read /repo/doc.md"]
    assert text == "Reading the design doc.\n"


def test_claude_reports_its_own_spelling_of_truncation():
    _, _, reasons = render("claude", [
        {"type": "assistant", "message": {"stop_reason": "max_tokens", "content": []}},
    ])
    assert stream.truncated(reasons)


# --------------------------------------------------------------------------
# a stream writ cannot read must degrade, never crash


def test_prose_from_an_unknown_shape_renders_as_nothing():
    renderer = stream.Renderer("")
    out = renderer.feed("just a line of prose\n")
    assert out.activity == [] and out.text == ""


def test_a_half_written_line_is_ignored():
    renderer = stream.Renderer("pi")
    assert renderer.feed('{"type": "tool_exec').activity == []


def test_a_json_value_that_is_not_an_object_is_ignored():
    renderer = stream.Renderer("pi")
    assert renderer.feed("[1, 2, 3]\n").activity == []


def test_an_unknown_event_type_is_ignored():
    renderer = stream.Renderer("pi")
    assert renderer.feed('{"type": "something_new"}\n').activity == []


def test_a_tool_call_with_no_recognisable_argument_still_shows_its_name():
    activity, _, _ = render("pi", [
        {"type": "tool_execution_start", "toolCallId": "c1", "toolName": "todowrite",
         "args": {"items": [1, 2]}},
    ])
    assert activity == ["· todowrite"]


def test_long_arguments_and_lines_are_cut_to_one_line():
    activity, _, _ = render("pi", [
        {"type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash",
         "args": {"command": "x" * 400}},
        {"type": "message_update", "assistantMessageEvent": {
            "type": "text_end", "contentIndex": 0, "content": "y" * 400,
        }},
    ])
    assert all(len(line) < 200 for line in activity)
    assert all("\n" not in line for line in activity)


# --------------------------------------------------------------------------
# end to end through the runner: a real subprocess emitting a real event stream


EVENT_AGENT = r"""
import json, sys, time
sys.stdin.read()
def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()
emit({"type": "message_update",
      "assistantMessageEvent": {"type": "thinking_start", "contentIndex": 0}})
emit({"type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash",
      "args": {"command": "ls docs"}})
emit({"type": "tool_execution_end", "toolCallId": "c1", "toolName": "bash",
      "isError": False, "result": {"content": []}})
emit({"type": "message_update",
      "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0,
                                "delta": "Survey"}})
emit({"type": "message_update",
      "assistantMessageEvent": {"type": "text_end", "contentIndex": 0,
                                "content": "Survey complete."}})
emit({"type": "turn_end", "message": {"role": "assistant", "stopReason": "stop"}})
"""

TRUNCATED_AGENT = r"""
import json, sys
sys.stdin.read()
def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()
emit({"type": "tool_execution_start", "toolCallId": "c1", "toolName": "read",
      "args": {"path": "/repo/doc.md"}})
emit({"type": "message_update",
      "assistantMessageEvent": {"type": "thinking_end", "contentIndex": 0,
                                "content": "Now let me assign every requirement."}})
emit({"type": "turn_end", "message": {"role": "assistant", "stopReason": "length"}})
"""


def run(script: str, directory, **kwargs) -> int:
    import sys as _sys

    from writ import runner

    return runner.run_agent(
        [_sys.executable, "-c", script],
        "prompt",
        directory,
        directory,
        None,
        event_shape="pi",
        **kwargs,
    )


def test_the_transcript_holds_only_speech_and_the_events_are_kept(tmp_path, capsys):
    reasons: list[str] = []
    assert run(EVENT_AGENT, tmp_path, stop_reasons=reasons) == 0
    transcript = (tmp_path / "stdout.log").read_text()
    assert transcript == "Survey complete.\n"
    # every event but the streaming fragment survives verbatim, beside the
    # transcript, and the live log is gone once the run has ended
    assert not (tmp_path / "events.jsonl").exists()
    raw = list(stream.event_lines(tmp_path))
    assert len(raw) == 5
    assert json.loads(raw[0])["assistantMessageEvent"]["type"] == "thinking_start"
    assert all("text_delta" not in line for line in raw)
    assert reasons == ["stop"]


def test_compacting_keeps_everything_a_reader_renders(tmp_path):
    """Fragments render as nothing, so dropping them changes no activity line."""
    events = [
        {"type": "message_update",
         "assistantMessageEvent": {"type": "thinking_start", "contentIndex": 0}},
        {"type": "message_update",
         "assistantMessageEvent": {"type": "thinking_delta", "delta": "Let"}},
        {"type": "message_update",
         "assistantMessageEvent": {"type": "thinking_end", "content": "Let me look."}},
        {"type": "message_update",
         "assistantMessageEvent": {"type": "text_delta", "delta": "Done"}},
        {"type": "message_update",
         "assistantMessageEvent": {"type": "text_end", "content": "Done."}},
        {"type": "turn_end", "message": {"stopReason": "stop"}},
    ]
    lines = [json.dumps(event) + "\n" for event in events] + ["not json\n"]
    (tmp_path / "events.jsonl").write_text("".join(lines))
    before = render("pi", events)

    stream.compact(tmp_path, "pi")

    assert not (tmp_path / "events.jsonl").exists()
    kept = list(stream.event_lines(tmp_path))
    assert len(kept) == 5  # two fragments gone; the line it cannot parse is kept
    assert "not json\n" in kept
    assert render("pi", [json.loads(line) for line in kept[:-1]]) == before
    assert stream.has_events(tmp_path)


def test_an_unknown_shape_is_compressed_but_never_filtered(tmp_path):
    line = json.dumps({"type": "message_update",
                       "assistantMessageEvent": {"type": "text_delta"}}) + "\n"
    (tmp_path / "events.jsonl").write_text(line)
    stream.compact(tmp_path, "someday")
    assert list(stream.event_lines(tmp_path)) == [line]


def test_claude_partial_messages_are_fragments():
    assert stream.fragment("claude", json.dumps({"type": "stream_event"}))
    assert not stream.fragment("claude", json.dumps({"type": "assistant"}))


def test_activity_is_mirrored_live_with_its_prefix(tmp_path, capsys):
    assert run(EVENT_AGENT, tmp_path, stream=True, prefix="  | ") == 0
    out = capsys.readouterr().out
    assert "  | ~ thinking…" in out
    assert "  | · bash ls docs" in out
    assert "  | > Survey complete." in out


def test_quiet_still_records_events_and_the_stop_reason(tmp_path, capsys):
    """The stop reason is how a silent run is diagnosed; --quiet must not cost it."""
    reasons: list[str] = []
    assert run(TRUNCATED_AGENT, tmp_path, stream=False, stop_reasons=reasons) == 0
    assert capsys.readouterr().out == ""
    assert stream.has_events(tmp_path)
    assert stream.truncated(reasons)


def test_a_truncated_run_is_not_reported_as_having_done_nothing(tmp_path):
    """It wrote no transcript, but it plainly ran — that distinction is the point."""
    from writ import runner

    assert run(TRUNCATED_AGENT, tmp_path) == 0
    assert (tmp_path / "stdout.log").read_text() == ""
    assert runner.produced_output(tmp_path)
