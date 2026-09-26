"""Independent adversarial review of a plan, before any of it runs.

Writ's own checks can prove a criterion names no command. They cannot tell that a
task is the wrong task. That needs a reader, and the argument these tests are about
is that the reader must not be the author — and must report findings rather than
rewrite the plan, for the same reason a gate does.
"""
from __future__ import annotations

import json
import shlex
import sys

import pytest

from writ import critics, plancheck, plans, state
from writ.state import WritError

from tests.test_plans import PLAN


CRITIC = """
import json, os, re, sys
prompt = sys.stdin.read()
path = re.search(r'Write your findings as JSON to this exact path:\\n  (\\S+)', prompt).group(1)
brief = re.search(r'Your brief, and nothing else: (.+?)\\.\\n', prompt).group(1)
payload = json.loads(os.environ.get("WRIT_TEST_REPORT", "{}"))
findings = payload.get("findings", [])
# Only the critic named in WRIT_TEST_CRITIC reports anything, so a test can say
# which review produced which finding.
want = os.environ.get("WRIT_TEST_CRITIC", "")
if want and want not in prompt.split("Your brief")[0] + brief:
    findings = []
open(path, "w").write(json.dumps({
    "findings": findings,
    "summary": payload.get("summary", "reviewed " + brief[:30]),
    "confidence": payload.get("confidence", "high"),
}))
"""

SILENT = """
import json, re, sys
prompt = sys.stdin.read()
path = re.search(r'Write your findings as JSON to this exact path:\\n  (\\S+)', prompt).group(1)
open(path, "w").write(json.dumps({"findings": [], "summary": "nothing to report"}))
"""

MUTE = """
import sys
sys.stdin.read()
print("I have decided not to write a report")
"""

BLOCKING_FINDING = {
    "severity": "blocking",
    "category": "uncovered-requirement",
    "where": "REQ-003",
    "message": "No task implements the queue depth view",
    "suggested_action": "add a task, or mark it out of scope with a reason",
    "requirement_ids": ["REQ-003"],
    "evidence": "searched the plan and the repository for queue depth",
}


def agent(script: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


@pytest.fixture
def planned_with_requirements(writ, project, design, tmp_path):
    artifact = tmp_path / "plan.json"
    artifact.write_text(json.dumps(PLAN), encoding="utf-8")
    writ("init")
    writ("plan", str(design), "--from-plan", str(artifact), "--auto-approve")
    return writ


# --------------------------------------------------------------------------
# the briefs


def test_each_critic_gets_one_question():
    briefs = [critic.brief for critic in critics.CRITICS]
    assert len(set(briefs)) == len(briefs)
    # Every critic states what it is not looking at, so two reviews are two
    # reviews rather than two copies of the same one.
    assert all(critic.out_of_scope for critic in critics.CRITICS)
    assert all(critic.checks for critic in critics.CRITICS)


def test_an_unknown_critic_is_named_against_the_ones_that_exist():
    with pytest.raises(WritError, match="fidelity"):
        critics.by_name(["vibes"])
    # And a bad name among good ones fails rather than quietly running the rest.
    with pytest.raises(WritError, match="'vibes'"):
        critics.by_name(["fidelity,vibes"])


def test_critics_can_be_named_with_commas_or_spaces():
    assert [c.name for c in critics.by_name(["fidelity,feasibility"])] == [
        "fidelity",
        "feasibility",
    ]
    # Declaration order, not argument order, so two runs of a set read alike.
    assert [c.name for c in critics.by_name(["feasibility", "fidelity"])] == [
        "fidelity",
        "feasibility",
    ]
    assert [c.name for c in critics.by_name([" fidelity , fidelity "])] == ["fidelity"]


def test_the_names_of_the_five_old_critics_still_resolve():
    """A config written when there were five critics names who asks that now."""
    assert [c.name for c in critics.by_name(["coverage,scope"])] == ["fidelity"]
    assert [c.name for c in critics.by_name(["acceptance", "feasibility"])] == [
        "fidelity",
        "feasibility",
    ]


def _files(root, *, inventory=False):
    folder = root / ".writ/plans/p"
    return critics.PlanFiles(
        index=folder / "plan.json",
        features=folder / "features",
        inventory=folder / "inventory.json" if inventory else None,
    )


def test_the_prompt_points_at_the_plan_and_forbids_rewriting(tmp_path):
    prompt = critics.build_prompt(
        critics.CRITICS[0],
        root=tmp_path,
        doc=tmp_path / "design.md",
        plan=_files(tmp_path),
        report_path=tmp_path / ".writ/plans/p/rounds/r1/fidelity/findings.json",
    )
    assert "you are not fixing it" in prompt
    assert "Do not rewrite the plan" in prompt
    # the plan is referenced by repo-relative path, not pasted
    assert "  - .writ/plans/p/plan.json — the plan's index" in prompt
    assert "  - .writ/plans/p/features — one file per feature" in prompt
    assert "\n  .writ/plans/p/rounds/r1/fidelity/findings.json\n" in prompt
    # and a path the plan proposes is not a defect for not existing yet
    assert "Files that don't exist yet are expected" in prompt
    # the categories a blocker may use, and the cap, travel with the prompt
    assert "uncovered-requirement" in prompt and "needs-decision" in prompt
    assert f"At most {critics.MAX_BLOCKING} blocking" in prompt
    # It is told its own brief and told what to leave alone.
    assert critics.CRITICS[0].brief in prompt
    assert critics.CRITICS[0].out_of_scope in prompt


def test_the_prompt_points_at_what_writ_already_found(tmp_path):
    known = critics.write_known(
        tmp_path / "known-findings.json",
        [
            plancheck.Finding(
                severity="error",
                category="vague-acceptance",
                message="criterion 1 is not checkable",
                where="M01-001",
            ),
            plancheck.Finding(
                severity="note", category="info", message="just saying", where="plan"
            ),
        ],
    )
    prompt = critics.build_prompt(
        critics.CRITICS[0],
        root=tmp_path,
        doc=None,
        plan=_files(tmp_path),
        report_path=tmp_path / "findings.json",
        known_findings=known,
    )
    assert "known-findings.json — what writ's own" in prompt
    assert "do not re-report them" in prompt
    # pre-filtered: the file holds only what a critic should read
    written = json.loads(known.read_text())
    assert [entry["message"] for entry in written] == ["criterion 1 is not checkable"]


def test_nothing_known_writes_no_file(tmp_path):
    assert critics.write_known(tmp_path / "k.json", []) is None
    assert not (tmp_path / "k.json").exists()


# --------------------------------------------------------------------------
# parsing


def test_a_report_becomes_ledger_findings():
    report = critics.parse(
        json.dumps({"findings": [BLOCKING_FINDING], "summary": "one hole",
                    "confidence": "high"}),
        critics.CRITICS[0],
    )
    assert report.blocking == 1
    finding = report.findings[0]
    # The same shape writ's own checks produce, tagged with who said it.
    assert finding.severity == "error"
    assert finding.where == "REQ-003"
    assert finding.requirement_ids == ["REQ-003"]
    assert finding.source == "critic:fidelity"
    assert "searched the plan" in finding.message


def test_a_blocker_outside_the_closed_categories_is_only_advisory():
    report = critics.parse(
        json.dumps({"findings": [{**BLOCKING_FINDING, "category": "file-naming"}]}),
        critics.CRITICS[0],
    )
    assert report.blocking == 0
    assert report.findings[0].severity == "warning"


def test_a_report_is_cut_to_its_caps():
    many = [
        {**BLOCKING_FINDING, "message": f"hole {n}"} for n in range(8)
    ] + [
        {**BLOCKING_FINDING, "severity": "advisory", "message": f"nit {n}"}
        for n in range(8)
    ]
    report = critics.parse(json.dumps({"findings": many}), critics.CRITICS[0])
    assert report.blocking == critics.MAX_BLOCKING
    assert len(report.findings) == critics.MAX_BLOCKING + critics.MAX_ADVISORY


def test_an_advisory_finding_does_not_block():
    report = critics.parse(
        json.dumps({"findings": [{**BLOCKING_FINDING, "severity": "advisory"}]}),
        critics.CRITICS[0],
    )
    assert report.blocking == 0
    assert report.findings[0].severity == "warning"


def test_a_finding_that_names_nowhere_is_refused():
    with pytest.raises(WritError, match="names no feature"):
        critics.parse(
            json.dumps({"findings": [{**BLOCKING_FINDING, "where": ""}]}),
            critics.CRITICS[0],
        )


def test_a_finding_that_says_nothing_is_refused():
    with pytest.raises(WritError, match="says nothing"):
        critics.parse(
            json.dumps({"findings": [{**BLOCKING_FINDING, "message": ""}]}),
            critics.CRITICS[0],
        )


def test_an_unknown_severity_is_refused():
    with pytest.raises(WritError, match="severity"):
        critics.parse(
            json.dumps({"findings": [{**BLOCKING_FINDING, "severity": "bad"}]}),
            critics.CRITICS[0],
        )


def test_finding_nothing_is_a_legitimate_report():
    report = critics.parse(
        json.dumps({"findings": [], "summary": "checked every edge"}),
        critics.CRITICS[1],
    )
    assert report.ok and report.findings == [] and report.summary


def test_a_report_printed_as_chatter_is_still_read():
    report = critics.parse(
        "Here is what I found:\n```json\n"
        + json.dumps({"findings": [BLOCKING_FINDING]})
        + "\n```\n",
        critics.CRITICS[0],
    )
    assert report.blocking == 1


# --------------------------------------------------------------------------
# through the command


def test_critique_records_findings_in_the_same_ledger(
    planned_with_requirements, project, writ, monkeypatch
):
    monkeypatch.setenv(
        "WRIT_TEST_REPORT", json.dumps({"findings": [BLOCKING_FINDING]})
    )
    monkeypatch.setenv("WRIT_TEST_CRITIC", "builds what the design")
    code, out, err = writ("critique", "--agent", agent(CRITIC), "--quiet")
    # Blocking findings stand against the plan, so this is a non-zero exit.
    assert code == 1
    records = [
        record
        for record in plans.finding_records(state.load(project))
        if str(record.get("source", "")).startswith("critic:")
    ]
    assert len(records) == 1
    assert records[0]["source"] == "critic:fidelity"
    assert records[0]["disposition"] == "open"


def test_a_blocking_critic_finding_holds_the_plan(
    planned_with_requirements, project, writ, monkeypatch
):
    assert plans.runnable(state.load(project))
    monkeypatch.setenv(
        "WRIT_TEST_REPORT", json.dumps({"findings": [BLOCKING_FINDING]})
    )
    writ("critique", "--agent", agent(CRITIC), "--quiet")
    data = state.load(project)
    # No new vocabulary: a critic's objection holds the plan exactly as a
    # structural one does, and the same approve overrules it.
    assert plans.plan_status(data)["status"] == "needs-approval"
    assert not plans.runnable(data)
    assert writ("run", "--agent", "false")[0] == 2


def test_a_clean_critique_leaves_the_plan_runnable(
    planned_with_requirements, project, writ
):
    code, out, _ = writ("critique", "--agent", agent(SILENT), "--quiet")
    assert code == 0
    assert "found nothing to report" in out
    assert plans.runnable(state.load(project))


def test_one_critic_failing_does_not_lose_the_others(
    planned_with_requirements, project, writ, monkeypatch
):
    # An agent that writes no report at all.
    code, out, err = writ("critique", "--agent", agent(MUTE), "--quiet")
    assert code == 1
    assert "wrote no report" in err
    records = critics.reviews(state.load(project))
    # Every failure is on the record by name, rather than the review silently
    # reducing to whoever happened to succeed.
    assert len(records) == len(critics.CRITICS)
    assert all(record.get("error") for record in records)


def test_only_the_named_critics_run(planned_with_requirements, project, writ):
    writ("critique", "--agent", agent(SILENT), "--quiet", "--critics", "feasibility")
    names = [record["critic"] for record in critics.reviews(state.load(project))]
    assert names == ["feasibility"]


# --------------------------------------------------------------------------
# running them at once


def test_only_the_critic_that_runs_commands_says_so():
    """The distinction that decides the waves is declared, not inferred by name.

    Reading a repository concurrently is harmless; running its test suite twice in
    one working tree is what produced findings about a broken build. A critic added
    later is placed by what it says it does.
    """
    runs = [critic.name for critic in critics.CRITICS if critic.runs_commands]
    assert runs == ["feasibility"]
    # And it is the one whose brief actually tells it to run them.
    feasibility = critics.by_name(["feasibility"])[0]
    assert any("test command works" in check.lower() for check in feasibility.checks)


def test_only_the_baseline_critic_is_allowed_to_run_the_suite():
    """What makes one wave safe, since the schedule no longer does.

    Two agents in one working tree is fine while only one of them runs the tests.
    The prompt is the only thing enforcing that, so it is worth a test.
    """
    from pathlib import Path

    prompts = {
        critic.name: critics.build_prompt(
            critic,
            root=Path("/repo"),
            doc=None,
            plan=_files(Path("/repo")),
            report_path=Path("/repo/findings.json"),
        )
        for critic in critics.CRITICS
    }
    for critic in critics.CRITICS:
        prompt = prompts[critic.name]
        if critic.runs_commands:
            assert "at the same time as you" in prompt
        else:
            assert "Do not run the project's build, test or lint suite" in prompt


def test_the_critics_all_go_in_one_wave():
    """Including the one that runs commands.

    Holding it back bought nothing: the collision worth avoiding is two critics
    running the suite at once, and only one of them is asked to run it at all.
    """
    grouped = critics.waves(critics.CRITICS)
    assert [[critic.name for critic in wave] for wave in grouped] == [
        ["fidelity", "feasibility"],
    ]
    # A partition: every critic runs exactly once, whatever the grouping.
    assert [critic for wave in grouped for critic in wave] == list(critics.CRITICS)


def test_a_wave_is_not_invented_for_critics_that_were_not_chosen():
    chosen = critics.by_name(["feasibility"])
    assert critics.waves(chosen) == [chosen]
    assert critics.waves([]) == []


def test_parallel_critics_all_report(planned_with_requirements, project, writ):
    """Concurrency must not reduce the review to whoever finished first."""
    code, out, _ = writ("critique", "--agent", agent(SILENT), "--quiet")
    assert code == 0
    names = [record["critic"] for record in critics.reviews(state.load(project))]
    assert names == [critic.name for critic in critics.CRITICS]
    assert "critics at once:" in out


def test_a_parallel_critic_report_names_whose_it_is(
    planned_with_requirements, project, writ
):
    """Every header prints before any reports, so a bare count has no owner.

    Run at once, the counts do not follow the header that named the critic, and
    an unattributed `0 blocking, 0 advisory` is unreadable.
    """
    _, out, _ = writ("critique", "--agent", agent(SILENT), "--quiet")
    for critic in critics.CRITICS:
        assert f"{critic.name}: 0 blocking" in out


def test_the_old_parallel_flags_still_parse(planned_with_requirements, writ):
    """Concurrency is no longer a choice, but scripts that asked for it still run."""
    code, _, _ = writ(
        "critique", "--agent", agent(SILENT), "--quiet", "--parallel-critics"
    )
    assert code == 0


def test_parallel_critics_report_in_the_order_they_were_asked_for(
    planned_with_requirements, project, writ
):
    """Two identical reviews are recorded in the same order.

    Which agent finishes first is a timing accident, and a record that ordered
    itself by that would make two identical reviews look different.
    """
    writ("critique", "--agent", agent(SILENT), "--quiet")
    parallel = [record["critic"] for record in critics.reviews(state.load(project))]
    with state.transaction(project) as data:
        data["reviews"] = []
    writ("critique", "--agent", agent(SILENT), "--quiet")
    assert [
        record["critic"] for record in critics.reviews(state.load(project))
    ] == parallel


def test_one_parallel_critic_failing_does_not_lose_the_others(
    planned_with_requirements, project, writ
):
    code, out, err = writ("critique", "--agent", agent(MUTE), "--quiet")
    assert code == 1
    records = critics.reviews(state.load(project))
    assert len(records) == len(critics.CRITICS)
    assert all(record.get("error") for record in records)


def test_parallel_critics_findings_reach_the_same_ledger(
    planned_with_requirements, project, writ, monkeypatch
):
    monkeypatch.setenv(
        "WRIT_TEST_REPORT", json.dumps({"findings": [BLOCKING_FINDING]})
    )
    monkeypatch.setenv("WRIT_TEST_CRITIC", "builds what the design")
    code, _, _ = writ("critique", "--agent", agent(CRITIC), "--quiet")
    assert code == 1
    records = [
        record
        for record in plans.finding_records(state.load(project))
        if str(record.get("source", "")).startswith("critic:")
    ]
    assert [record["source"] for record in records] == ["critic:fidelity"]
    assert not plans.runnable(state.load(project))


def test_a_review_is_stale_once_the_plan_moves_on(
    planned_with_requirements, project, writ
):
    writ("critique", "--agent", agent(SILENT), "--quiet")
    assert critics.unreviewed(state.load(project)) == []
    with state.transaction(project) as data:
        plans.bump(data)
    # A critic that passed the plan two revisions ago reviewed something else.
    assert critics.unreviewed(state.load(project)) == [
        critic.name for critic in critics.CRITICS
    ]


def test_critique_needs_a_plan(writ, project):
    writ("init")
    code, _, err = writ("critique", "--agent", agent(SILENT))
    assert code == 2 and "no plan" in err


def test_plan_without_the_flag_runs_no_critics(writ, project, design, tmp_path):
    artifact = tmp_path / "plan.json"
    artifact.write_text(json.dumps(PLAN), encoding="utf-8")
    writ("init")
    writ("plan", str(design), "--from-plan", str(artifact), "--quiet")
    # Each critic is an agent run, so they are opt-in.
    assert critics.reviews(state.load(project)) == []


def test_a_bare_critics_flag_runs_all_of_them(writ, project, design, tmp_path):
    artifact = tmp_path / "plan.json"
    artifact.write_text(json.dumps(PLAN), encoding="utf-8")
    writ("init")
    writ(
        "plan", str(design), "--from-plan", str(artifact),
        "--critics", "--critic-agent", agent(SILENT), "--quiet",
    )
    # `--critics` with no names is a request for all of them, not an empty one.
    assert [r["critic"] for r in critics.reviews(state.load(project))] == [
        critic.name for critic in critics.CRITICS
    ]


def test_plan_can_run_the_critics_in_one_pass(writ, project, design, tmp_path):
    artifact = tmp_path / "plan.json"
    artifact.write_text(json.dumps(PLAN), encoding="utf-8")
    writ("init")
    code, out, _ = writ(
        "plan", str(design), "--from-plan", str(artifact),
        "--critics", "fidelity", "--critic-agent", agent(SILENT), "--quiet",
    )
    assert code == 0
    assert [r["critic"] for r in critics.reviews(state.load(project))] == ["fidelity"]


def test_auto_approve_waits_for_the_critics(writ, project, design, tmp_path, monkeypatch):
    """A critic's blocking finding must reach `--auto-approve` before it decides.

    Auto-approve used to run inside the commit that writes the tasks, which is
    before the critics have read anything. So a plan whose structural checks were
    clean was approved, the critics then reported something blocking, and the
    re-check demoted the plan to `needs-approval` — leaving an approval record that
    said nothing blocking stood against a plan with blocking findings against it.
    While that window was open the plan really was `approved`, so a concurrent
    `writ run` would have started executing it.
    """
    artifact = tmp_path / "plan.json"
    artifact.write_text(json.dumps(PLAN), encoding="utf-8")
    writ("init")
    # No WRIT_TEST_CRITIC: only the fidelity critic runs, so it is the one that
    # reports, and the selector in the stub matches on brief text rather than name.
    monkeypatch.setenv("WRIT_TEST_REPORT", json.dumps({"findings": [BLOCKING_FINDING]}))
    code, out, _ = writ(
        "plan", str(design), "--from-plan", str(artifact),
        "--critics", "fidelity", "--critic-agent", agent(CRITIC),
        "--auto-approve", "--quiet",
    )
    assert code == 0
    data = state.load(project)
    record = plans.plan_status(data)
    assert record["status"] == "needs-approval"
    # Never approved at all, rather than approved and then demoted: the record
    # must not carry an approval the findings contradict.
    assert record["approved_by"] is None
    assert record["approved_at"] is None
    assert "plan held at needs-approval" in out


def test_auto_approve_still_approves_a_plan_the_critics_pass(
    writ, project, design, tmp_path
):
    """The other half: silence from the critics still reaches approval."""
    artifact = tmp_path / "plan.json"
    artifact.write_text(json.dumps(PLAN), encoding="utf-8")
    writ("init")
    code, out, _ = writ(
        "plan", str(design), "--from-plan", str(artifact),
        "--critics", "fidelity", "--critic-agent", agent(SILENT),
        "--auto-approve", "--quiet",
    )
    assert code == 0
    record = plans.plan_status(state.load(project))
    assert record["status"] == "approved"
    assert record["approved_by"] == "writ --auto-approve"


# --------------------------------------------------------------------------
# what `writ check` says about the review


def test_check_says_when_no_critic_has_read_the_plan(
    planned_with_requirements, writ
):
    code, out, _ = writ("check")
    assert "no critic has read this plan" in out
    # Not a finding: writ cannot tell whether an unread plan is wrong.
    assert "0 blocking" in out


def test_check_names_the_critics_that_have_not_read_it(
    planned_with_requirements, writ
):
    writ("critique", "--agent", agent(SILENT), "--quiet", "--critics", "fidelity")
    code, out, _ = writ("check")
    assert "not reviewed at this revision: feasibility" in out


def test_check_is_quiet_once_every_critic_has_read_it(
    planned_with_requirements, writ
):
    writ("critique", "--agent", agent(SILENT), "--quiet")
    code, out, _ = writ("check")
    assert "critic" not in out
    assert "not reviewed" not in out


def test_check_reports_staleness_as_json(planned_with_requirements, writ):
    writ("critique", "--agent", agent(SILENT), "--quiet", "--critics", "fidelity")
    code, out, _ = writ("--json", "check")
    assert json.loads(out)["unreviewed"] == ["feasibility"]


def test_the_repo_summary_is_offered_when_there_is_one(tmp_path):
    prompt = critics.build_prompt(
        critics.CRITICS[1],
        root=tmp_path,
        doc=None,
        plan=_files(tmp_path, inventory=True),
        report_path=tmp_path / "findings.json",
    )
    assert "inventory.json — the short repo summary" in prompt
    assert "verification.json" not in prompt


# --------------------------------------------------------------------------
# verify mode: the second round re-checks, it does not re-review


def _open_blocker(project, writ, monkeypatch):
    monkeypatch.setenv("WRIT_TEST_REPORT", json.dumps({"findings": [BLOCKING_FINDING]}))
    monkeypatch.setenv("WRIT_TEST_CRITIC", "builds what the design")
    writ("critique", "--agent", agent(CRITIC), "--quiet", "--critics", "fidelity")
    monkeypatch.delenv("WRIT_TEST_REPORT")
    monkeypatch.delenv("WRIT_TEST_CRITIC")
    (record,) = [
        r
        for r in plans.finding_records(state.load(project))
        if r.get("source") == "critic:fidelity"
    ]
    return record


def test_a_critic_that_never_reviewed_has_nothing_to_verify(
    planned_with_requirements, project
):
    assert critics.verify_context(state.load(project), critics.CRITICS[0]) is None


def test_verify_mode_hands_back_its_own_blockers_and_what_changed(
    planned_with_requirements, project, writ, monkeypatch
):
    record = _open_blocker(project, writ, monkeypatch)
    with state.transaction(project) as data:
        data["tasks"]["M01-001"]["title"] = "Renamed by a repair"
        plans.bump(data)
    context = critics.verify_context(state.load(project), critics.CRITICS[0])
    assert context.open_ids == [record["id"]]
    assert context.changed == ["M01-001"]
    prompt = critics.build_prompt(
        critics.CRITICS[0],
        root=project,
        doc=None,
        plan=_files(project),
        report_path=project / "findings.json",
        verify=context,
        verify_path=project / "verify.json",
    )
    assert "This is a VERIFY pass" in prompt
    assert "still_open" in prompt


def test_a_blocker_the_verifier_still_names_stays_open(
    planned_with_requirements, project, writ, monkeypatch
):
    record = _open_blocker(project, writ, monkeypatch)
    context = critics.verify_context(state.load(project), critics.CRITICS[0])
    report = critics.parse(
        json.dumps({"still_open": [record["id"]]}), critics.CRITICS[0], verify=context
    )
    assert report.still_open == [record["id"]]
    assert report.blocking == 1
    # and one it leaves out is not re-emitted, so the ledger closes it
    silent = critics.parse(
        json.dumps({"still_open": []}), critics.CRITICS[0], verify=context
    )
    assert silent.blocking == 0


def test_a_new_blocker_on_an_unchanged_feature_is_only_advisory(
    planned_with_requirements, project, writ, monkeypatch
):
    _open_blocker(project, writ, monkeypatch)
    context = critics.verify_context(state.load(project), critics.CRITICS[0])
    fresh = {**BLOCKING_FINDING, "where": "M01-002", "requirement_ids": []}
    report = critics.parse(
        json.dumps({"findings": [fresh]}), critics.CRITICS[0], verify=context
    )
    assert report.blocking == 0
