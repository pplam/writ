"""The verdict protocol: agents judge their own work, reviewers confirm it.

The fake agents here are shell one-liners that write a verdict file, so the whole
loop is exercised without a live model.
"""
import json
import os
import shlex
import sys
import time

import pytest

from writ import state, verdict
from writ.state import WritError


# --------------------------------------------------------------------------
# fake agents


def agent_reporting(payload, *, exit_code=0, to_stdout=False):
    """A fake agent that writes `payload` as its verdict.

    It reads the prompt to find the exact path writ asked for, which is also a
    check that the prompt really names one.
    """
    script = f"""
import json, re, sys
prompt = sys.stdin.read()
payload = {payload!r}
match = re.search(r'^  (\\S*verdict\\.json)$', prompt, re.M)
if {to_stdout!r} or not match:
    sys.stdout.write("here is my report\\n```json\\n" + payload + "\\n```\\n")
else:
    open(match.group(1), "w").write(payload)
    sys.stdout.write("wrote verdict\\n")
sys.exit({exit_code})
"""
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


def agent_writing_elsewhere(payload, *, where=".reviews/report-verdict.json"):
    """A fake agent that reports correctly, to a path of its own invention.

    Modelled on a real reviewer: it did the whole job, wrote a valid verdict to
    `.reviews/<task>-verdict.json`, announced that in prose, and never touched the
    path it was given.
    """
    script = f"""
import os, sys
sys.stdin.read()
path = {where!r}
os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
open(path, "w").write({payload!r})
sys.stdout.write("Verdict written to `" + path + "` — **accept**.\\n")
"""
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


def passing(count=3, note="ran: pytest -q, 187 passed"):
    return json.dumps(
        {
            "outcome": "complete",
            "summary": "implemented the thing",
            "criteria": [
                {"number": n, "status": "passed", "evidence": note}
                for n in range(1, count + 1)
            ],
        }
    )


def partial(passed=1, total=3):
    criteria = [
        {"number": n, "status": "passed", "evidence": "ran: pytest -q"}
        for n in range(1, passed + 1)
    ]
    criteria += [
        {"number": n, "status": "failed", "evidence": "could not get this green"}
        for n in range(passed + 1, total + 1)
    ]
    return json.dumps(
        {"outcome": "incomplete", "summary": "got part way", "criteria": criteria}
    )


def review(decision, count=3):
    status = "passed" if decision == "accept" else "failed"
    return json.dumps(
        {
            "decision": decision,
            "summary": f"re-ran the suite and {decision}ed",
            "criteria": [
                {"number": n, "status": status, "evidence": "independently re-ran"}
                for n in range(1, count + 1)
            ],
        }
    )


# --------------------------------------------------------------------------
# the prompt asks for a verdict


def test_the_task_prompt_names_the_verdict_file_and_schema(planned, writ):
    _, out, _ = writ("dispatch", "M01-001", "--dry-run")
    assert "verdict.json" in out
    assert '"outcome"' in out
    assert "Writ sets this task's status from that file" in out


def test_the_prompt_states_how_many_criteria_there_are(planned, writ):
    _, out, _ = writ("dispatch", "M01-001", "--dry-run")
    assert "3 acceptance criteria, numbered 1 to 3" in out


def test_the_prompt_no_longer_asks_for_prose_only(planned, writ):
    """The old prompt asked for a freeform report, which writ could not act on."""
    _, out, _ = writ("dispatch", "M01-001", "--dry-run")
    assert "acceptance criteria met and not met" not in out


# --------------------------------------------------------------------------
# an agent's verdict drives the task


def test_a_passing_verdict_parks_the_task_for_review(planned, writ, project):
    code, out, _ = writ("dispatch", "M01-001", "--agent", agent_reporting(passing()))
    assert code == 0
    assert "M01-001 -> awaiting-review" in out
    assert "3/3 criteria passed" in out
    assert "next: writ review M01-001" in out

    task = state.load(project)["tasks"]["M01-001"]
    assert task["status"] == "awaiting-review"
    assert [a["status"] for a in task["acceptances"]] == ["passed"] * 3


def test_an_agent_cannot_complete_its_own_task(planned, writ, project):
    """Self-assessment is not evidence, so `complete` becomes awaiting-review."""
    writ("dispatch", "M01-001", "--agent", agent_reporting(passing()))
    task = state.load(project)["tasks"]["M01-001"]
    assert task["status"] != "completed"
    assert task["last_verdict"]["outcome"] == "complete"
    assert task["last_verdict"]["role"] == "agent"


def test_criteria_record_who_judged_them_and_on_what_evidence(planned, writ, project):
    writ("dispatch", "M01-001", "--agent", agent_reporting(passing()))
    task = state.load(project)["tasks"]["M01-001"]
    first = task["acceptances"][0]
    assert first["evidence"] == "ran: pytest -q, 187 passed"
    assert first["judged_by"].startswith("agent(")


def test_a_partial_verdict_fails_the_task_and_keeps_the_detail(planned, writ, project):
    code, out, _ = writ("dispatch", "M01-001", "--agent", agent_reporting(partial()))
    assert code == 0  # the process succeeded; the work did not
    assert "M01-001 -> failed" in out
    task = state.load(project)["tasks"]["M01-001"]
    assert task["status"] == "failed"
    assert [a["status"] for a in task["acceptances"]] == ["passed", "failed", "failed"]


def test_a_blocked_verdict_records_what_stopped_it(planned, writ, project):
    payload = json.dumps(
        {
            "outcome": "blocked",
            "summary": "cannot proceed",
            "blocked_on": "the storage format is undecided",
            "criteria": [{"number": 1, "status": "pending", "evidence": ""}],
        }
    )
    writ("dispatch", "M01-001", "--agent", agent_reporting(payload))
    task = state.load(project)["tasks"]["M01-001"]
    assert task["status"] == "blocked"
    assert any("storage format is undecided" in e["text"] for e in task["evidence"])


def test_an_exit_code_alone_changes_nothing(planned, writ, project):
    """Exit 0 with no verdict is not a claim, so no criterion moves."""
    silent = f"{shlex.quote(sys.executable)} -c 'import sys; sys.stdin.read()'"
    code, out, err = writ("dispatch", "M01-001", "--agent", silent)
    assert code == 0
    assert "exited without a usable verdict" in err
    task = state.load(project)["tasks"]["M01-001"]
    assert task["status"] == "planned"
    assert all(a["status"] == "pending" for a in task["acceptances"])


def test_the_run_record_says_why_nothing_happened(planned, writ, project):
    """Exit 0, status completed, task unmoved: the run has to explain itself.

    `dispatch` says this on stderr at the time, but that scrolls away. Anything
    reading the run later — `writ show`, or a dashboard — sees a clean exit and no
    reason, which is the most confusing state writ can leave a record in.
    """
    silent = f"{shlex.quote(sys.executable)} -c 'import sys; sys.stdin.read()'"
    writ("dispatch", "M01-001", "--agent", silent)
    run = next(iter(state.load(project)["runs"].values()))
    assert run["exit_code"] == 0
    assert "without writing a usable verdict" in run["no_verdict"]
    # Distinct from verdict_error, which means a verdict was written and rejected.
    assert not run.get("verdict_error")


def test_an_agent_that_printed_nothing_is_not_blamed_for_a_missing_report(
    planned, writ, project
):
    """Empty transcript, exit 0: the invocation failed, it did not skip its report.

    This is what a wrong model id or an unauthenticated provider looks like from
    writ's side — several agent CLIs report both as exit 0 with no output. Read
    as "no verdict" alone it sends the operator to a transcript that says
    nothing, so the record has to distinguish the two.
    """
    code, _, err = writ("dispatch", "M01-001", "--agent", "true")
    assert code == 0
    run = next(iter(state.load(project)["runs"].values()))
    assert run["no_output"] is True
    assert "never reached a model" in run["no_verdict"]
    # and at the time, where the invocation is still on screen
    assert "never ran" in err


def test_an_agent_that_spoke_but_did_not_report_is_not_called_silent(
    planned, writ, project
):
    """The other half of the distinction: output, but no verdict."""
    talker = f"{shlex.quote(sys.executable)} -c 'import sys; sys.stdin.read(); print(\"done\")'"
    writ("dispatch", "M01-001", "--agent", talker)
    run = next(iter(state.load(project)["runs"].values()))
    assert not run.get("no_output")
    assert "never reached a model" not in run["no_verdict"]


def test_a_contradicted_claim_keeps_the_evidence_under_it(planned, writ, project):
    """The failure this replaced: honest per-criterion work thrown away.

    An agent that meets three of four bars, says so criterion by criterion with
    evidence, and then heads the report `complete` has made one field wrong and
    three right. Discarding the verdict lost all three, reset the task to
    `planned`, and left the next agent to rediscover the same work.
    """
    payload = json.dumps(
        {
            "outcome": "complete",
            "summary": "implemented the store",
            "criteria": [
                {"number": 1, "status": "passed", "evidence": "pytest tests/a.py"},
                {"number": 2, "status": "pending", "evidence": "a sibling task fails"},
            ],
        }
    )
    code, _, err = writ("dispatch", "M01-001", "--agent", agent_reporting(payload))
    assert code == 0
    task = state.load(project)["tasks"]["M01-001"]
    # the claim was lowered, so the task is failed rather than awaiting review
    assert task["status"] == "failed"
    # and the evidence for the bar that was met survived
    assert task["acceptances"][0]["status"] == "passed"
    assert task["acceptances"][0]["evidence"] == "pytest tests/a.py"
    assert task["acceptances"][1]["status"] == "pending"
    # the next agent on this task reads the task, so the mismatch is recorded there
    assert any("writ recorded 'incomplete'" in e["text"] for e in task["evidence"])
    assert "criteria 2 are not passed" in err
    run = next(iter(state.load(project)["runs"].values()))
    # applied, not rejected: this is not the unusable-verdict path
    assert not run.get("verdict_error")
    assert run["resulting_status"] == "failed"
    assert "writ recorded 'incomplete'" in run["verdict_downgraded"]


def test_an_unusable_verdict_is_recorded_differently_from_a_missing_one(
    planned, writ, project
):
    """Two different failures with two different remedies."""
    writ("dispatch", "M01-001", "--agent", agent_reporting("{not json at all"))
    run = next(iter(state.load(project)["runs"].values()))
    assert run["verdict_error"]
    assert not run.get("no_verdict")


def test_a_verdict_on_stdout_is_recovered(planned, writ, project):
    """An agent that cannot write files still gets its report read."""
    writ(
        "dispatch",
        "M01-001",
        "--agent",
        agent_reporting(passing(), to_stdout=True),
    )
    task = state.load(project)["tasks"]["M01-001"]
    assert task["status"] == "awaiting-review"


def test_a_nonzero_exit_with_a_verdict_still_uses_the_verdict(planned, writ, project):
    writ(
        "dispatch",
        "M01-001",
        "--agent",
        agent_reporting(partial(passed=2), exit_code=1),
    )
    task = state.load(project)["tasks"]["M01-001"]
    assert task["status"] == "failed"
    # the criteria it did meet are still credited
    assert [a["status"] for a in task["acceptances"]] == ["passed", "passed", "failed"]


# --------------------------------------------------------------------------
# review


def test_review_completes_a_task_the_agent_only_claimed(planned, writ, project):
    writ("dispatch", "M01-001", "--agent", agent_reporting(passing()))
    code, out, _ = writ("review", "M01-001", "--agent", agent_reporting(review("accept")))
    assert code == 0
    assert "M01-001 -> completed" in out
    task = state.load(project)["tasks"]["M01-001"]
    assert task["status"] == "completed"
    assert task["last_verdict"]["role"] == "reviewer"


def test_a_rejecting_review_sends_the_task_back_for_rework(planned, writ, project):
    """A rejection is a finding, not a dead end: it buys another attempt."""
    writ("dispatch", "M01-001", "--agent", agent_reporting(passing()))
    _, out, _ = writ("review", "M01-001", "--agent", agent_reporting(review("reject")))
    assert "sent back for rework (1 of 2)" in out
    assert "next: writ dispatch M01-001" in out
    task = state.load(project)["tasks"]["M01-001"]
    # planned, so it is back in the ready set and the next `writ run` picks it up
    assert task["status"] == "planned"
    assert [a["status"] for a in task["acceptances"]] == ["failed"] * 3
    assert task["rework"]["attempt"] == 1
    assert task["rework"]["reviewer"].startswith("reviewer")


def test_the_rejection_is_recorded_with_both_sides_of_it(planned, writ, project):
    """The next attempt needs the claim and the finding, not just the finding."""
    writ("dispatch", "M01-001", "--agent", agent_reporting(passing()))
    writ("review", "M01-001", "--agent", agent_reporting(review("reject")))
    record = state.load(project)["tasks"]["M01-001"]["rework"]
    assert len(record["findings"]) == 3
    assert all(f["status"] == "failed" for f in record["findings"])
    # what the implementer said before the review overwrote the criteria
    assert all(c["status"] == "passed" for c in record["claimed"])
    assert record["claimed"][0]["evidence"]


def test_a_reworking_agent_is_given_the_rejection(planned, writ, project):
    """A re-dispatch that does not carry the review is the same prompt again."""
    writ("dispatch", "M01-001", "--agent", agent_reporting(passing()))
    writ("review", "M01-001", "--agent", agent_reporting(review("reject")))
    _, out, _ = writ("dispatch", "M01-001", "--dry-run")
    assert "THIS TASK WAS ALREADY IMPLEMENTED AND THE REVIEW REJECTED IT" in out
    assert "You are attempt 2" in out
    assert "reviewer marked this failed" in out
    assert "the previous attempt claimed this passed" in out


def test_a_re_review_is_told_what_the_last_one_objected_to(planned, writ, project):
    writ("dispatch", "M01-001", "--agent", agent_reporting(passing()))
    writ("review", "M01-001", "--agent", agent_reporting(review("reject")))
    writ("dispatch", "M01-001", "--agent", agent_reporting(passing()))
    _, out, _ = writ("review", "M01-001", "--dry-run")
    assert "rejected 1 time already" in out
    assert "an unaddressed one is a rejection" in out


def test_the_rework_budget_is_finite(planned, writ, project):
    """An agent that cannot satisfy a reviewer in N tries needs a human."""
    for _ in range(3):
        writ("dispatch", "M01-001", "--agent", agent_reporting(passing()))
        _, out, _ = writ(
            "review", "M01-001", "--agent", agent_reporting(review("reject"))
        )
    assert "M01-001 -> failed" in out
    assert "rework budget of 2 attempts is spent" in out
    task = state.load(project)["tasks"]["M01-001"]
    assert task["status"] == "failed"
    assert task["rework"]["exhausted"] is True


def test_max_rework_zero_fails_on_the_first_rejection(planned, writ, project):
    writ("dispatch", "M01-001", "--agent", agent_reporting(passing()))
    _, out, _ = writ(
        "review", "M01-001", "--agent", agent_reporting(review("reject")),
        "--max-rework", "0",
    )
    assert "M01-001 -> failed" in out
    assert state.load(project)["tasks"]["M01-001"]["status"] == "failed"


def test_requeueing_an_exhausted_task_grants_more_attempts(planned, writ, project):
    """`writ set ... planned` on a spent task is a human asking for another try."""
    for _ in range(3):
        writ("dispatch", "M01-001", "--agent", agent_reporting(passing()))
        writ("review", "M01-001", "--agent", agent_reporting(review("reject")))
    assert state.load(project)["tasks"]["M01-001"]["rework"]["exhausted"] is True

    writ("set", "M01-001", "planned")
    writ("dispatch", "M01-001", "--agent", agent_reporting(passing()))
    _, out, _ = writ("review", "M01-001", "--agent", agent_reporting(review("reject")))
    # not failed again on the first rejection after the reset
    assert "sent back for rework (4 of 4)" in out
    record = state.load(project)["tasks"]["M01-001"]["rework"]
    # the count keeps climbing rather than lying about the history
    assert record["attempt"] == 4
    assert record["allowance"] == 2
    assert record["exhausted"] is False


def test_an_accepted_rework_closes_the_record(planned, writ, project):
    """A task that was sent back and then passed is not still awaiting rework."""
    writ("dispatch", "M01-001", "--agent", agent_reporting(passing()))
    writ("review", "M01-001", "--agent", agent_reporting(review("reject")))
    writ("dispatch", "M01-001", "--agent", agent_reporting(passing()))
    writ("review", "M01-001", "--agent", agent_reporting(review("accept")))
    task = state.load(project)["tasks"]["M01-001"]
    assert task["status"] == "completed"
    assert task["rework"]["resolved_at"]
    # and the next prompt for this task no longer argues a settled rejection
    _, out, _ = writ("dispatch", "M01-001", "--dry-run", "--force")
    assert "THE REVIEW REJECTED IT" not in out


def test_the_review_prompt_shows_the_claim_but_says_not_to_trust_it(planned, writ):
    writ("dispatch", "M01-001", "--agent", agent_reporting(passing()))
    _, out, _ = writ("review", "M01-001", "--dry-run")
    assert "implementer claimed passed" in out
    assert "Treat the implementer's claims as claims" in out
    assert "You did not write this code" in out
    assert "Do not modify the repository" in out


def test_review_refuses_a_task_that_was_never_reported(planned, writ):
    code, _, err = writ("review", "M01-001", "--agent", "true")
    assert code == 2
    assert "not awaiting review" in err


def test_a_review_written_to_the_wrong_path_is_still_used(planned, writ, project):
    """A complete review, written where the agent felt like writing it.

    The real case: a reviewer re-ran the tests, judged all four criteria, wrote a
    valid verdict to `.reviews/<task>-verdict.json`, and said so in prose. Writ
    looked at one path, found nothing, and threw the whole review away — then sent
    the task back to the queue to be reviewed again from scratch.
    """
    writ("dispatch", "M01-001", "--agent", agent_reporting(passing()))
    code, _, err = writ(
        "review", "M01-001", "--agent", agent_writing_elsewhere(review("accept"))
    )
    assert code == 0
    task = state.load(project)["tasks"]["M01-001"]
    assert task["status"] == "completed"
    # used, and not quietly: the habit is worth fixing in the prompt
    assert "rather than the path it was given" in err
    run = [r for r in state.load(project)["runs"].values() if r["role"] == "reviewer"][0]
    assert run["verdict_misplaced"].endswith("report-verdict.json")
    assert not run.get("no_verdict")
    assert any("instead of" in e["text"] for e in task["evidence"])


def test_a_verdict_from_an_earlier_run_is_not_adopted(planned, writ, project):
    """The search is bounded by the run's own start time.

    Without that it would find any verdict-shaped file left lying around and
    credit it to whatever ran last, which is worse than reporting nothing.
    """
    stale = project / "stale-verdict.json"
    stale.write_text(review("accept"), encoding="utf-8")
    old = time.time() - 3600
    os.utime(stale, (old, old))
    writ("dispatch", "M01-001", "--agent", agent_reporting(passing()))
    writ("review", "M01-001", "--agent", "true")
    data = state.load(project)
    run = [r for r in data["runs"].values() if r["role"] == "reviewer"][0]
    assert not run.get("verdict_misplaced")
    assert run["no_verdict"]
    # and the lost review leaves the task where it was, not back at planned
    assert data["tasks"]["M01-001"]["status"] == "awaiting-review"


def test_a_verdict_shaped_file_that_does_not_validate_is_not_adopted(
    planned, writ, project
):
    """Named like a verdict is not the same as being one."""
    writ("dispatch", "M01-001", "--agent", agent_reporting(passing()))
    writ(
        "review",
        "M01-001",
        "--agent",
        agent_writing_elsewhere('{"decision": "maybe"}'),
    )
    run = [
        r for r in state.load(project)["runs"].values() if r["role"] == "reviewer"
    ][0]
    assert not run.get("verdict_misplaced")
    assert run["no_verdict"]


def test_review_with_no_id_reviews_everything_awaiting(planned, writ, project):
    writ("dispatch", "M01-001", "--agent", agent_reporting(passing()))
    _, out, _ = writ("review", "--agent", agent_reporting(review("accept")))
    assert "reviewing 1 task(s): M01-001" in out
    assert state.load(project)["tasks"]["M01-001"]["status"] == "completed"


def test_review_with_nothing_awaiting_says_so(planned, writ):
    code, out, _ = writ("review", "--agent", "true")
    assert code == 0 and "nothing is awaiting review" in out


def test_a_review_run_is_recorded_as_a_reviewer(planned, writ, project):
    writ("dispatch", "M01-001", "--agent", agent_reporting(passing()))
    writ("review", "M01-001", "--agent", agent_reporting(review("accept")))
    runs = state.load(project)["runs"]
    roles = sorted(run.get("role") for run in runs.values())
    assert roles == ["agent", "reviewer"]


def test_completing_a_task_by_review_unblocks_its_dependents(planned, writ, project):
    writ("dispatch", "M01-001", "--agent", agent_reporting(passing()))
    writ("review", "M01-001", "--agent", agent_reporting(review("accept")))
    _, out, _ = writ("--json", "status")
    assert "M02-001" in json.loads(out)["ready"]


# --------------------------------------------------------------------------
# validation: a verdict has to be honest to be accepted


def test_passing_a_criterion_requires_evidence():
    payload = json.dumps(
        {
            "outcome": "complete",
            "criteria": [{"number": 1, "status": "passed", "evidence": ""}],
        }
    )
    with pytest.raises(WritError, match="marked passed with no evidence"):
        verdict.parse(payload)


def test_claiming_complete_with_unmet_criteria_is_lowered_not_discarded():
    """The criteria win, because they are the part carrying evidence.

    Rejecting the whole verdict here used to throw away three honest
    per-criterion reports over one wrong summary field, leaving a task that was
    mostly done looking untouched.
    """
    payload = json.dumps(
        {
            "outcome": "complete",
            "criteria": [
                {"number": 1, "status": "passed", "evidence": "ran it"},
                {"number": 2, "status": "failed", "evidence": "nope"},
            ],
        }
    )
    parsed = verdict.parse(payload)
    assert parsed.outcome == "incomplete"
    assert parsed.downgraded and "criteria 2 are not passed" in parsed.downgraded
    # nothing the agent did not itself mark passed is credited
    assert parsed.passed == [1]
    assert parsed.unmet == [2]


def test_accepting_a_review_with_unmet_criteria_becomes_a_rejection():
    """Lowering an accept to a reject is the conservative direction."""
    payload = json.dumps(
        {
            "decision": "accept",
            "criteria": [{"number": 1, "status": "pending", "evidence": "unsure"}],
        }
    )
    parsed = verdict.parse(payload, role="reviewer")
    assert parsed.decision == "reject"
    assert parsed.outcome == "incomplete"
    assert "criteria 1 are not passed" in parsed.downgraded


def test_a_claim_that_matches_its_criteria_is_not_downgraded():
    payload = json.dumps(
        {
            "outcome": "complete",
            "criteria": [{"number": 1, "status": "passed", "evidence": "ran it"}],
        }
    )
    parsed = verdict.parse(payload)
    assert parsed.outcome == "complete"
    assert parsed.downgraded is None


def test_blocked_requires_saying_what_blocked_it():
    payload = json.dumps({"outcome": "blocked", "criteria": []})
    with pytest.raises(WritError, match="blocked_on is empty"):
        verdict.parse(payload)


def test_an_unknown_outcome_names_the_field():
    payload = json.dumps({"outcome": "mostly done", "criteria": []})
    with pytest.raises(WritError, match="outcome must be one of"):
        verdict.parse(payload)


def test_a_duplicated_criterion_is_rejected():
    payload = json.dumps(
        {
            "outcome": "incomplete",
            "criteria": [
                {"number": 1, "status": "failed"},
                {"number": 1, "status": "passed", "evidence": "ran it"},
            ],
        }
    )
    with pytest.raises(WritError, match="criterion 1 reported twice"):
        verdict.parse(payload)


def test_a_criterion_this_task_does_not_have_is_rejected(planned, writ, project):
    payload = json.dumps(
        {
            "outcome": "incomplete",
            "criteria": [{"number": 9, "status": "failed", "evidence": "x"}],
        }
    )
    _, _, err = writ("dispatch", "M01-001", "--agent", agent_reporting(payload))
    assert "reports criteria 9 but M01-001 has 3" in err
    task = state.load(project)["tasks"]["M01-001"]
    assert all(a["status"] == "pending" for a in task["acceptances"])


def test_malformed_json_leaves_the_task_alone(planned, writ, project):
    _, _, err = writ(
        "dispatch", "M01-001", "--agent", agent_reporting("{not json at all")
    )
    assert "not valid JSON" in err
    task = state.load(project)["tasks"]["M01-001"]
    assert all(a["status"] == "pending" for a in task["acceptances"])


def test_criteria_may_be_reported_out_of_order():
    payload = json.dumps(
        {
            "outcome": "incomplete",
            "criteria": [
                {"number": 3, "status": "failed"},
                {"number": 1, "status": "passed", "evidence": "ran it"},
            ],
        }
    )
    parsed = verdict.parse(payload)
    assert [c.number for c in parsed.criteria] == [1, 3]


def test_a_string_number_is_tolerated():
    """Models emit "1" for 1 often enough that rejecting it is pedantry."""
    payload = json.dumps(
        {
            "outcome": "incomplete",
            "criteria": [{"number": "2", "status": "failed"}],
        }
    )
    assert verdict.parse(payload).criteria[0].number == 2


# --------------------------------------------------------------------------
# the operator escape hatch


def test_override_records_that_a_human_decided(planned, writ, project):
    writ(
        "override",
        "M01-001",
        "completed",
        "--reason",
        "checked on staging by hand",
        "--accept",
        "1",
        "--accept",
        "2",
        "--accept",
        "3",
    )
    task = state.load(project)["tasks"]["M01-001"]
    assert task["status"] == "completed"
    assert task["last_verdict"]["role"] == "operator"
    assert all(a["judged_by"] == "operator" for a in task["acceptances"])
    assert any(
        "operator override" in e["text"] and e["actor"] == "operator"
        for e in task["evidence"]
    )


def test_override_demands_a_reason(planned, writ):
    code, _, err = writ("override", "M01-001", "completed")
    assert code == 2 and "--reason" in err


def test_override_can_mark_one_criterion_failed(planned, writ, project):
    writ(
        "override",
        "M01-001",
        "failed",
        "--reason",
        "criterion 2 regressed in staging",
        "--accept",
        "2=failed",
    )
    task = state.load(project)["tasks"]["M01-001"]
    assert task["acceptances"][1]["status"] == "failed"


def test_set_explains_where_completion_comes_from(planned, writ, project):
    """A refusal is only useful if it says what to do instead."""
    from writ import model

    data = state.load(project)
    with pytest.raises(WritError) as caught:
        model.set_status(data, "M01-001", "completed")
    message = str(caught.value)
    assert "writ dispatch" in message
    assert "writ review" in message
    assert "writ override" in message


# --------------------------------------------------------------------------
# evidence log attribution


def test_the_evidence_log_distinguishes_agent_from_reviewer(planned, writ, project):
    writ("dispatch", "M01-001", "--agent", agent_reporting(passing()))
    writ("review", "M01-001", "--agent", agent_reporting(review("accept")))
    task = state.load(project)["tasks"]["M01-001"]
    actors = {entry.get("actor", "") for entry in task["evidence"]}
    assert any(actor.startswith("agent(") for actor in actors)
    assert any(actor.startswith("reviewer(") for actor in actors)


def test_show_surfaces_the_evidence_and_the_judge(planned, writ):
    writ("dispatch", "M01-001", "--agent", agent_reporting(passing()))
    _, out, _ = writ("show", "M01-001")
    assert "judged by agent(" in out
    assert "evidence: ran: pytest -q, 187 passed" in out
    assert "awaiting review: writ review M01-001" in out


def test_status_lists_what_is_awaiting_review(planned, writ):
    writ("dispatch", "M01-001", "--agent", agent_reporting(passing()))
    _, out, _ = writ("status")
    assert "awaiting review: M01-001" in out


def test_list_filters_to_what_awaits_review(planned, writ):
    writ("dispatch", "M01-001", "--agent", agent_reporting(passing()))
    _, out, _ = writ("--json", "list", "--awaiting-review")
    assert [t["id"] for t in json.loads(out)] == ["M01-001"]


def test_two_runs_in_the_same_second_do_not_collide(planned, writ, project):
    """Run ids are second-resolution, and dispatch+review is faster than that."""
    writ("dispatch", "M01-001", "--agent", agent_reporting(passing()))
    writ("review", "M01-001", "--agent", agent_reporting(review("accept")))
    data = state.load(project)
    assert len(data["runs"]) == 2
    assert data["tasks"]["M01-001"]["runs"] == sorted(data["runs"])


# --------------------------------------------------------------------------
# proposed decisions


def with_decisions(*proposals, count=3):
    return json.dumps(
        {
            "outcome": "complete",
            "summary": "implemented the thing",
            "criteria": [
                {"number": n, "status": "passed", "evidence": "ran: pytest -q"}
                for n in range(1, count + 1)
            ],
            "decisions": list(proposals),
        }
    )


GOOD = {
    "title": "Length-prefixed frames",
    "decision": "Records are length-prefixed rather than newline-delimited.",
    "context": "The design did not say how records are framed.",
    "consequences": "Payloads may contain newlines; readers must buffer.",
}


def test_a_verdict_can_propose_a_decision(planned, writ, project):
    writ("dispatch", "M01-001", "--agent", agent_reporting(with_decisions(GOOD)))
    records = state.load(project)["decisions"]
    assert len(records) == 1
    assert records[0]["title"] == "Length-prefixed frames"
    assert records[0]["context"] == "The design did not say how records are framed."
    assert records[0]["consequences"].startswith("Payloads may contain")


def test_a_proposed_decision_is_linked_to_its_task(planned, writ, project):
    writ("dispatch", "M01-001", "--agent", agent_reporting(with_decisions(GOOD)))
    assert state.load(project)["decisions"][0]["tasks"] == ["M01-001"]


def test_a_proposal_does_not_block_the_verdict(planned, writ, project):
    """The criteria still apply; a decision rides along, it does not gate."""
    writ("dispatch", "M01-001", "--agent", agent_reporting(with_decisions(GOOD)))
    assert state.load(project)["tasks"]["M01-001"]["status"] == "awaiting-review"


def test_a_reviewer_can_propose_a_decision_too(planned, writ, project):
    """A choice the implementer made silently is exactly what review catches."""
    writ("dispatch", "M01-001", "--agent", agent_reporting(passing()))
    payload = json.dumps(
        {
            "decision": "accept",
            "summary": "verified",
            "criteria": [
                {"number": n, "status": "passed", "evidence": "re-ran: pytest -q"}
                for n in (1, 2, 3)
            ],
            "decisions": [
                {
                    "title": "ASCII-only slugs",
                    "decision": "Non-ASCII input is dropped rather than transliterated.",
                }
            ],
        }
    )
    writ("review", "M01-001", "--agent", agent_reporting(payload))
    records = state.load(project)["decisions"]
    assert [r["title"] for r in records] == ["ASCII-only slugs"]
    assert records[0]["proposed_by"].startswith("reviewer(")


def test_a_titleless_proposal_is_rejected(planned, writ, project):
    bad = with_decisions({"decision": "we chose the other thing"})
    writ("dispatch", "M01-001", "--agent", agent_reporting(bad))
    data = state.load(project)
    assert data["decisions"] == []
    assert "no title" in data["runs"][next(iter(data["runs"]))]["verdict_error"]


def test_a_proposal_with_no_statement_is_rejected(planned, writ, project):
    bad = with_decisions({"title": "Framing"})
    writ("dispatch", "M01-001", "--agent", agent_reporting(bad))
    data = state.load(project)
    assert data["decisions"] == []
    error = data["runs"][next(iter(data["runs"]))]["verdict_error"]
    assert "does not say what was decided" in error


def test_a_one_word_decision_is_rejected(planned, writ, project):
    """A title with a shrug attached is not a decision record."""
    bad = with_decisions({"title": "Framing", "decision": "yes"})
    writ("dispatch", "M01-001", "--agent", agent_reporting(bad))
    data = state.load(project)
    assert data["decisions"] == []
    assert "too thin" in data["runs"][next(iter(data["runs"]))]["verdict_error"]


def test_a_bad_proposal_takes_the_whole_verdict_with_it(planned, writ, project):
    """Partial application would leave criteria passed against a rejected report."""
    bad = with_decisions({"title": "Framing"})
    writ("dispatch", "M01-001", "--agent", agent_reporting(bad))
    task = state.load(project)["tasks"]["M01-001"]
    assert task["status"] != "awaiting-review"
    assert all(a["status"] != "passed" for a in task["acceptances"])


def test_near_miss_field_names_still_land():
    """Models reach for `name`/`why`; the content matters more than the key."""
    parsed = verdict.parse(
        json.dumps(
            {
                "outcome": "incomplete",
                "criteria": [],
                "decisions": [
                    {
                        "name": "Framing",
                        "choice": "Length-prefixed records, not newline-delimited.",
                        "why": "the design was silent",
                    }
                ],
            }
        ),
        where="test",
    )
    assert parsed.decisions[0].title == "Framing"
    assert parsed.decisions[0].context == "the design was silent"


def test_decisions_must_be_a_list():
    with pytest.raises(WritError, match="must be a list"):
        verdict.parse(
            json.dumps(
                {"outcome": "incomplete", "criteria": [], "decisions": "one thing"}
            ),
            where="test",
        )


def test_the_prompt_asks_for_decisions(planned, writ):
    _, out, _ = writ("dispatch", "M01-001", "--dry-run")
    assert "decisions" in out
    assert "design document did not make for you" in out


def test_the_review_prompt_asks_for_them_too(planned, writ):
    writ("dispatch", "M01-001", "--agent", agent_reporting(passing()))
    _, out, _ = writ("review", "M01-001", "--dry-run")
    assert "design document did not make for you" in out


def test_the_reviewer_sees_decisions_already_recorded(planned, writ):
    writ("dispatch", "M01-001", "--agent", agent_reporting(with_decisions(GOOD)))
    _, out, _ = writ("review", "M01-001", "--dry-run")
    assert "Decisions already recorded against this task:" in out
    assert "[proposed] Length-prefixed frames" in out


def test_the_reviewer_is_told_not_to_repeat_them(planned, writ):
    """Live runs showed reviewers restating the implementer's proposals."""
    writ("dispatch", "M01-001", "--agent", agent_reporting(with_decisions(GOOD)))
    _, out, _ = writ("review", "M01-001", "--dry-run")
    assert "Do not propose these again" in out


def test_a_task_with_no_decisions_gets_no_such_instruction(planned, writ):
    writ("dispatch", "M01-001", "--agent", agent_reporting(passing()))
    _, out, _ = writ("review", "M01-001", "--dry-run")
    assert "Do not propose these again" not in out


def test_the_review_prompt_states_the_criterion_count(planned, writ):
    """A reviewer that does not know how many bars there are can miss one."""
    writ("dispatch", "M01-001", "--agent", agent_reporting(passing()))
    _, out, _ = writ("review", "M01-001", "--dry-run")
    assert "This task has 3 acceptance criteria, numbered 1 to 3." in out
