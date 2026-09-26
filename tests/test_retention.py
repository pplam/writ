"""Retention: abandoned live logs get compacted, old compacted ones go."""
import gzip
import json
import os

from writ import retention, state, stream


def _log(directory, lines, *, age):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / stream.EVENTS_FILENAME
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    os.utime(path, (1_000_000 - age, 1_000_000 - age))
    return path


DELTA = {"type": "message_update", "assistantMessageEvent": {"type": "text_delta"}}
END = {"type": "turn_end", "message": {"stopReason": "stop"}}


def test_an_abandoned_live_log_is_compacted_and_a_live_one_is_left(tmp_path):
    state.initialize(tmp_path)
    store = state.store_dir(tmp_path)
    dead = _log(store / "plans" / "p" / "synthesis", [DELTA, END], age=7 * 3600)
    live = _log(store / "plans" / "p" / "rounds" / "r1" / "fidelity", [END], age=60)

    report = retention.collect(tmp_path, now=1_000_000)

    assert report.compacted == [dead]
    assert not dead.exists()
    with gzip.open(dead.parent / stream.COMPACT_EVENTS_FILENAME, "rt") as handle:
        assert [json.loads(line) for line in handle] == [END]  # shape sniffed: pi
    assert live.exists()


def test_old_compacted_logs_go_and_nothing_else_does(tmp_path):
    state.initialize(tmp_path)
    run = state.store_dir(tmp_path) / "runs" / "FT-001" / "01-implement"
    _log(run, [END], age=0)
    stream.compact(run, "pi")
    old = run / stream.COMPACT_EVENTS_FILENAME
    os.utime(old, (0, 0))
    (run / "prompt.txt").write_text("p")
    (run / "verdict.json").write_text("{}")

    dry = retention.collect(tmp_path, keep_days=14, dry_run=True, now=1_000_000_000)
    assert dry.removed == [old] and old.exists()

    retention.collect(tmp_path, keep_days=14, now=1_000_000_000)
    assert not old.exists()
    assert sorted(p.name for p in run.iterdir()) == ["prompt.txt", "verdict.json"]


def test_the_writer_is_recognised_from_its_first_event(tmp_path):
    claude = _log(tmp_path / "a", [{"type": "system"}, {"type": "assistant"}], age=0)
    pi = _log(tmp_path / "b", ["garbage", DELTA], age=0)
    unknown = _log(tmp_path / "c", [{"type": "whatever"}], age=0)
    assert retention.sniff_shape(claude) == "claude"
    assert retention.sniff_shape(pi) == "pi"
    assert retention.sniff_shape(unknown) == ""
