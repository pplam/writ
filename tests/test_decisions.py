import json

from forge import state


def test_add_and_list(planned, forge, project):
    code, out, _ = forge(
        "decision",
        "add",
        "--title",
        "Fixture-only tests",
        "--decision",
        "Automated tests never touch a live platform.",
        "--context",
        "Quotas are small and debugging consumes them.",
        "--consequences",
        "Recorded fixtures are the only substrate.",
        "--task",
        "M01-001",
    )
    assert code == 0 and "recorded D-0001" in out

    _, listing, _ = forge("--json", "decision", "list")
    records = json.loads(listing)
    assert len(records) == 1
    assert records[0]["id"] == "D-0001"
    assert records[0]["status"] == "active"
    assert records[0]["tasks"] == ["M01-001"]

    mirror = state.decisions_file(project).read_text(encoding="utf-8")
    assert "# Decision Log" in mirror
    assert "D-0001 — Fixture-only tests" in mirror
    assert "Automated tests never touch a live platform." in mirror


def add(forge, title, text="because", **extra):
    args = ["decision", "add", "--title", title, "--decision", text]
    for key, value in extra.items():
        args.extend([f"--{key.replace('_', '-')}", value])
    return forge(*args)


def test_ids_increment(planned, forge):
    add(forge, "One")
    add(forge, "Two")
    _, out, _ = forge("--json", "decision", "list")
    assert [r["id"] for r in json.loads(out)] == ["D-0001", "D-0002"]


def test_supersede_marks_the_old_record(planned, forge):
    add(forge, "Original")
    add(forge, "Replacement", supersedes="D-0001")
    _, out, _ = forge("--json", "decision", "list")
    records = {r["id"]: r for r in json.loads(out)}
    assert records["D-0001"]["status"] == "superseded"
    assert records["D-0001"]["superseded_by"] == "D-0002"
    assert records["D-0002"]["supersedes"] == "D-0001"


def test_active_filter_hides_superseded(planned, forge):
    add(forge, "Original")
    add(forge, "Replacement", supersedes="D-0001")
    _, out, _ = forge("--json", "decision", "list", "--active")
    assert [r["id"] for r in json.loads(out)] == ["D-0002"]


def test_task_filter(planned, forge):
    add(forge, "Scoped", task="M01-001")
    add(forge, "Unscoped")
    _, out, _ = forge("--json", "decision", "list", "--task", "M01-001")
    assert [r["id"] for r in json.loads(out)] == ["D-0001"]


def test_show(planned, forge):
    add(forge, "Visible", "the chosen path", context="the question")
    code, out, _ = forge("decision", "show", "D-0001")
    assert code == 0
    assert "D-0001 — Visible" in out
    assert "the chosen path" in out
    assert "the question" in out


def test_unknown_references_are_rejected(planned, forge):
    code, _, err = forge("decision", "show", "D-9999")
    assert code == 2 and "unknown decision" in err
    code, _, err = add(forge, "Bad supersede", supersedes="D-9999")
    assert code == 2 and "unknown decision to supersede" in err
    code, _, err = add(forge, "Bad task", task="ghost")
    assert code == 2 and "unknown task" in err


def test_export_to_stdout_and_file(planned, forge, tmp_path):
    add(forge, "Exported")
    _, out, _ = forge("decision", "export")
    assert "D-0001 — Exported" in out
    target = tmp_path / "log.md"
    code, message, _ = forge("decision", "export", "--out", str(target))
    assert code == 0 and str(target) in message
    assert "D-0001 — Exported" in target.read_text(encoding="utf-8")


def test_log_is_append_only_across_invocations(planned, forge, project):
    add(forge, "First")
    add(forge, "Second")
    data = state.load(project)
    assert [d["title"] for d in data["decisions"]] == ["First", "Second"]
    assert data["counters"]["decision"] == 2
