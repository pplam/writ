"""The plan as a reviewed artifact: requirements, coverage, findings, approval.

The claim these tests are about is that a structurally valid plan is not
necessarily a good one. A plan can name every dependency, fence every path and
still leave an obligation from the design with nothing implementing it — so the
inventory, the coverage derived from it, and the approval step that reads both are
the parts worth pinning down.
"""
from __future__ import annotations

import json

import pytest

from writ import plancheck, plans, state
from writ.state import WritError


PLAN = {
    "requirements": [
        {
            "id": "REQ-001",
            "text": "Appends to the event log are atomic",
            "source": "Storage",
            "priority": "must",
            "status": "planned",
        },
        {
            "id": "REQ-002",
            "text": "A replay of the log reproduces the projection byte-for-byte",
            "source": "Projection",
            "priority": "must",
            "status": "planned",
        },
        {
            "id": "REQ-003",
            "text": "The operator can see queue depth",
            "source": "Operations",
            "priority": "should",
            "status": "planned",
        },
        {
            "id": "REQ-004",
            "text": "Authentication is already handled by the gateway",
            "source": "Security",
            "priority": "must",
            "status": "existing",
            "evidence": "gateway/auth_test.go covers it",
        },
        {
            "id": "REQ-005",
            "text": "Multi-region replication",
            "source": "Scale",
            "priority": "may",
            "status": "out-of-scope",
            "reason": "the design defers it to a later phase",
        },
    ],
    "milestones": [
        {
            "id": "M01",
            "title": "Storage",
            "tasks": [
                {
                    "id": "M01-001",
                    "title": "Add the append-only event log writer",
                    "design_section": "Storage",
                    "requirement_ids": ["REQ-001"],
                    "acceptances": [
                        "a failing test in store/log_test.go reproduces a torn append",
                        "`go test ./store` passes with appends fsync'd in order",
                    ],
                    "allowed": ["store/log.go", "store/log_test.go"],
                },
                {
                    "id": "M01-002",
                    "title": "Build the projection from the log",
                    "design_section": "Projection",
                    "requirement_ids": ["REQ-002"],
                    "depends_on": ["M01-001"],
                    "acceptances": [
                        "`go test ./store -run Replay` reproduces the projection",
                        "store/projection.go rebuilds from an empty state",
                    ],
                    "allowed": ["store/projection.go"],
                },
            ],
        }
    ],
}


@pytest.fixture
def inventoried(writ, project, design, tmp_path):
    """A committed plan that declares a requirement inventory."""
    artifact = tmp_path / "plan.json"
    artifact.write_text(json.dumps(PLAN), encoding="utf-8")
    writ("init")
    writ("plan", str(design), "--from-plan", str(artifact), "--auto-approve")
    return writ


# --------------------------------------------------------------------------
# the inventory


def test_the_inventory_is_committed_with_the_plan(inventoried, project):
    data = state.load(project)
    assert sorted(plans.requirements(data)) == [
        "REQ-001", "REQ-002", "REQ-003", "REQ-004", "REQ-005",
    ]


def test_a_task_records_which_requirements_it_covers(inventoried, project):
    data = state.load(project)
    assert data["tasks"]["M01-001"]["requirement_ids"] == ["REQ-001"]


# --------------------------------------------------------------------------
# coverage


def test_coverage_is_derived_from_the_graph_not_stored(inventoried, project):
    data = state.load(project)
    rows = {row["id"]: row for row in plans.coverage(data)}
    assert rows["REQ-001"]["tasks"] == ["M01-001"]
    assert rows["REQ-002"]["tasks"] == ["M01-002"]
    # Nothing is stored under `requirements` that repeats this.
    assert "tasks" not in plans.requirements(data)["REQ-001"]


def test_a_requirement_nothing_implements_is_uncovered(inventoried, project):
    data = state.load(project)
    rows = {row["id"]: row for row in plans.coverage(data)}
    assert rows["REQ-003"]["state"] == "uncovered"
    assert plans.uncovered(data) == ["REQ-003"]


def test_an_already_satisfied_requirement_needs_its_evidence(inventoried, project):
    data = state.load(project)
    rows = {row["id"]: row for row in plans.coverage(data)}
    # Declared `existing` with evidence: satisfied without a task, which is the
    # point of the status. Without the evidence it would read `unevidenced`.
    assert rows["REQ-004"]["state"] == "satisfied"
    with state.transaction(project) as live:
        plans.requirements(live)["REQ-004"]["evidence"] = ""
    rows = {row["id"]: row for row in plans.coverage(state.load(project))}
    assert rows["REQ-004"]["state"] == "unevidenced"


def test_an_out_of_scope_requirement_is_not_a_hole(inventoried, project):
    data = state.load(project)
    rows = {row["id"]: row for row in plans.coverage(data)}
    assert rows["REQ-005"]["state"] == "out-of-scope"
    assert "REQ-005" not in plans.uncovered(data)


def test_a_requirement_is_not_satisfied_until_a_reviewer_accepts_it(
    inventoried, project
):
    with state.transaction(project) as data:
        data["tasks"]["M01-001"]["status"] = "awaiting-review"
    rows = {row["id"]: row for row in plans.coverage(state.load(project))}
    # The implementing agent has reported. Nothing independent has checked it, so
    # this is in-progress — the distinction the review split exists to keep.
    assert rows["REQ-001"]["state"] == "in-progress"

    with state.transaction(project) as data:
        data["tasks"]["M01-001"]["status"] = "completed"
    rows = {row["id"]: row for row in plans.coverage(state.load(project))}
    assert rows["REQ-001"]["state"] == "satisfied"


def test_an_uncovered_must_requirement_blocks_approval(inventoried, project, writ):
    # REQ-003 is a `should`, so it is a warning. Promote it and re-check.
    with state.transaction(project) as data:
        plans.requirements(data)["REQ-003"]["priority"] = "must"
        plans.run_check(data, root=project)
    data = state.load(project)
    assert plans.plan_status(data)["status"] == "needs-approval"
    assert not plans.runnable(data)
    code, out, err = writ("check")
    assert code == 1
    # A blocking finding goes to stderr, with the rest of its list.
    assert "REQ-003" in err
    assert "no task covers it" in err


def test_a_plan_with_no_inventory_has_nothing_to_trace(approved, project):
    data = state.load(project)
    assert plans.coverage(data) == []
    # And the coverage checks stay quiet rather than faulting every task.
    findings = plancheck.check(plancheck.from_state(data, root=project))
    assert not [f for f in findings if f.category.startswith("uncovered")]


# --------------------------------------------------------------------------
# the findings ledger


def test_a_finding_keeps_its_id_across_re_checks(inventoried, project):
    with state.transaction(project) as data:
        data["tasks"]["M01-001"]["acceptances"] = [
            {"text": "it works", "status": "pending"}
        ]
        first = plans.run_check(data, root=project)
        ids = {f.id for f in first if f.severity == "error"}
        assert ids
        second = plans.run_check(data, root=project)
    assert {f.id for f in second if f.severity == "error"} == ids


def _critic_finding(message="nothing covers the queue depth view"):
    return plancheck.Finding(
        severity="error",
        category="missing-coverage",
        message=message,
        where="REQ-003",
        source="critic:coverage",
    )


def test_an_agents_acceptance_does_not_survive_the_finding_coming_back(
    inventoried, project
):
    """A repair's claim to have closed a finding is not what settles it.

    Otherwise the pre-execution repair loop could launder a plan past its own
    critics: accept each objection, assert work that closes it, and the ledger would
    agree. The check that still reports it has to win.
    """
    with state.transaction(project) as data:
        written = plans.record_findings(
            data, [_critic_finding()], scope="critic:coverage"
        )
        plans.dispose(
            data, written[0].id, "accepted", actor="adjudicator", change="added a task"
        )
        assert plans.get_finding(data, written[0].id)["disposition"] == "accepted"
        plans.record_findings(data, [_critic_finding()], scope="critic:coverage")
        record = plans.get_finding(data, written[0].id)
    assert record["disposition"] == "open"
    assert record["reopened_at"]
    assert record["seen_count"] == 2


def test_a_persons_acceptance_stands_when_the_finding_comes_back(
    inventoried, project
):
    """A human accepting a known objection has made a judgement, not a claim.

    `writ check` still lists it, so nothing is hidden — but a re-check does not
    overturn a decision somebody signed.
    """
    with state.transaction(project) as data:
        written = plans.record_findings(
            data, [_critic_finding()], scope="critic:coverage"
        )
        plans.dispose(
            data,
            written[0].id,
            "accepted",
            actor="tim",
            reason="shipping without the depth view on purpose",
        )
        plans.record_findings(data, [_critic_finding()], scope="critic:coverage")
        record = plans.get_finding(data, written[0].id)
    assert record["disposition"] == "accepted"
    assert record["disposed_by"] == "tim"
    assert "reopened_at" not in record


def test_a_finding_that_goes_away_is_resolved_not_deleted(inventoried, project):
    with state.transaction(project) as data:
        data["tasks"]["M01-001"]["acceptances"] = [
            {"text": "it works", "status": "pending"}
        ]
        plans.run_check(data, root=project)
        vague = [
            record["id"]
            for record in plans.finding_records(data)
            if record["category"] == "vague-acceptance"
        ]
        assert vague
        # Fix the plan and check again.
        data["tasks"]["M01-001"]["acceptances"] = [
            {"text": "`go test ./store` passes with appends fsync'd", "status": "pending"},
            {"text": "a failing test in store/log_test.go reproduces a torn append",
             "status": "pending"},
        ]
        plans.run_check(data, root=project)
        records = {record["id"]: record for record in plans.finding_records(data)}
    for finding_id in vague:
        # Still readable, marked resolved: the objection was real and the history
        # of it is part of the plan's record.
        assert records[finding_id]["disposition"] == "resolved"


def test_a_critic_that_stops_objecting_closes_its_own_finding(inventoried, project):
    """The critic read the patched plan and no longer objects. That closes it.

    Nothing could close a critic's finding before this. Auto-closing was restricted
    to findings writ itself had raised, on the sound reasoning that a deterministic
    re-check cannot disprove an objection it never looked for — but the restriction
    was written as "source is writ" rather than "whoever is reporting", so the one
    party whose silence *is* evidence about the finding could not close it either. A
    repaired plan went on carrying every objection its repair had answered.
    """
    with state.transaction(project) as data:
        written = plans.record_findings(
            data,
            [_critic_finding()],
            scope="critic:coverage",
            reporter="critic:coverage",
        )
        # The same critic, reading the plan again, with nothing to say about it.
        plans.record_findings(
            data, [], scope="critic:coverage", reporter="critic:coverage"
        )
        record = plans.get_finding(data, written[0].id)
    assert record["disposition"] == "resolved"
    assert record["resolved_by"] == "critic:coverage"
    assert record["resolved_revision"]


def test_one_reporters_silence_does_not_close_anothers_finding(inventoried, project):
    """Only the party that raised it may stop reporting it.

    A structural re-check never looks for what a critic objected to, so its passing
    says nothing about that finding. The same in reverse: the acceptance critic's
    pass is not evidence about what the coverage critic found.
    """
    with state.transaction(project) as data:
        written = plans.record_findings(
            data,
            [_critic_finding()],
            scope="critic:coverage",
            reporter="critic:coverage",
        )
        # Writ's own deterministic pass over the same scope, which did not look.
        plans.record_findings(data, [], scope="critic:coverage")
        record = plans.get_finding(data, written[0].id)
    assert record["disposition"] == "open"


def test_an_agents_acceptance_is_resolved_once_the_check_agrees(inventoried, project):
    """`accepted` by an agent is a claim awaiting evidence. Silence is the evidence.

    The mirror of the reopening rule: if a finding coming back overturns an agent's
    acceptance, a finding that does not come back settles it. Leaving it `accepted`
    forever made a repaired plan read as one whose objections had merely been
    asserted away.
    """
    with state.transaction(project) as data:
        written = plans.record_findings(
            data,
            [_critic_finding()],
            scope="critic:coverage",
            reporter="critic:coverage",
        )
        plans.dispose(
            data, written[0].id, "accepted", actor="adjudicator", change="added a task"
        )
        plans.record_findings(
            data, [], scope="critic:coverage", reporter="critic:coverage"
        )
        record = plans.get_finding(data, written[0].id)
    assert record["disposition"] == "resolved"


def test_a_persons_disposition_is_not_overwritten_by_silence(inventoried, project):
    """A judgement stands in both directions.

    A person who accepted an objection knowingly, or declined it with evidence, has
    made a ruling. A check going quiet is not a reason to rewrite whose decision the
    record says it was.
    """
    with state.transaction(project) as data:
        written = plans.record_findings(
            data,
            [_critic_finding()],
            scope="critic:coverage",
            reporter="critic:coverage",
        )
        plans.dispose(
            data, written[0].id, "accepted", actor="tim", reason="shipping without it"
        )
        plans.record_findings(
            data, [], scope="critic:coverage", reporter="critic:coverage"
        )
        record = plans.get_finding(data, written[0].id)
    assert record["disposition"] == "accepted"
    assert record["disposed_by"] == "tim"


def test_only_a_blocking_finding_holds_the_plan(inventoried, project):
    data = state.load(project)
    # The committed plan has warnings (the design's generic criteria) and
    # `--auto-approve` still approved it, because advisory findings are not a veto.
    assert plans.plan_status(data)["status"] == "approved"
    assert plans.runnable(data)


def test_a_clean_check_does_not_approve_the_plan(writ, project, design, tmp_path):
    """A check says nothing is provably wrong. Approval says somebody decided.

    These were one status until the planning review pointed out that they are
    different claims, and that the weaker one was silently standing in for the
    stronger: every defect a clean check cannot see — an omitted requirement, a
    legal-but-wrong edge, a criterion nothing can demonstrate — passed straight
    through to execution as an approved plan.
    """
    artifact = tmp_path / "plan.json"
    artifact.write_text(json.dumps(PLAN), encoding="utf-8")
    writ("init")
    writ("plan", str(design), "--from-plan", str(artifact))
    data = state.load(project)
    assert not plancheck.blocking(plans.findings(data, open_only=True))
    assert plans.plan_status(data)["status"] == "needs-approval"
    assert not plans.runnable(data)
    # Re-checking it does not change that, however many times it is run.
    assert writ("check")[0] == 0
    assert not plans.runnable(state.load(project))


def test_auto_approve_signs_off_a_clean_plan_and_says_who(
    writ, project, design, tmp_path
):
    artifact = tmp_path / "plan.json"
    artifact.write_text(json.dumps(PLAN), encoding="utf-8")
    writ("init")
    code, out, _ = writ("plan", str(design), "--from-plan", str(artifact), "--auto-approve")
    assert code == 0
    record = plans.plan_status(state.load(project))
    assert record["status"] == "approved"
    assert record["approved_by"] == "writ --auto-approve"
    assert record["forced"] is False
    assert "approved by writ --auto-approve" in out


def test_auto_approve_will_not_override_a_blocking_finding(
    writ, project, design, tmp_path
):
    """The one thing --auto-approve must not become: a silent --force.

    Automation needs to get from a document to a running graph unattended, which
    is what the flag is for. Letting it also overrule writ's own objections would
    make every blocking finding advisory for anyone in a hurry.
    """
    broken = json.loads(json.dumps(PLAN))
    # a task that depends on something no plan defines: a blocking finding
    broken["milestones"][0]["tasks"][0]["requirement_ids"] = ["REQ-404"]
    artifact = tmp_path / "plan.json"
    artifact.write_text(json.dumps(broken), encoding="utf-8")
    writ("init")
    code, out, _ = writ("plan", str(design), "--from-plan", str(artifact), "--auto-approve")
    assert code == 0
    data = state.load(project)
    assert plancheck.blocking(plans.findings(data, open_only=True))
    assert plans.plan_status(data)["status"] == "needs-approval"
    assert not plans.runnable(data)
    assert "writ approve" in out


# --------------------------------------------------------------------------
# approval


def test_run_refuses_a_plan_that_has_not_been_checked(inventoried, project, writ):
    with state.transaction(project) as data:
        plans.set_status(data, "draft")
    code, _, err = writ("run", "--agent", "false")
    assert code == 2
    assert "writ check" in err


def test_approving_records_who_and_bumps_the_revision(inventoried, project, writ):
    before = plans.revision(state.load(project))
    code, out, _ = writ("approve", "--by", "a-person", "--reason", "read it through")
    assert code == 0
    record = plans.plan_status(state.load(project))
    assert record["approved_by"] == "a-person"
    assert record["approval_note"] == "read it through"
    assert plans.revision(state.load(project)) > before


def test_a_complete_plan_cannot_be_approved_again(inventoried, project):
    with state.transaction(project) as data:
        plans.set_status(data, "complete")
        with pytest.raises(WritError, match="already complete"):
            plans.approve(data)


def test_force_leaves_the_findings_readable_rather_than_deleting_them(
    inventoried, project, writ
):
    with state.transaction(project) as data:
        data["tasks"]["M01-001"]["acceptances"] = [
            {"text": "it works", "status": "pending"}
        ]
        plans.run_check(data, root=project)
    writ("approve", "--force", "--reason", "known gap, shipping anyway")
    records = plans.finding_records(state.load(project))
    accepted = [r for r in records if r["disposition"] == "accepted"]
    assert accepted
    assert all(r["reason"] == "known gap, shipping anyway" for r in accepted)
    # Nothing was removed from the ledger.
    assert all(r.get("message") for r in records)


# --------------------------------------------------------------------------
# the commands


def test_check_exits_nonzero_only_when_something_blocks(inventoried, writ, project):
    assert writ("check")[0] == 0
    with state.transaction(project) as data:
        data["tasks"]["M01-001"]["acceptances"] = [
            {"text": "it works", "status": "pending"}
        ]
    assert writ("check")[0] == 1


def test_coverage_prints_a_row_per_requirement(inventoried, writ):
    code, out, _ = writ("coverage")
    assert code == 0
    for req in ("REQ-001", "REQ-002", "REQ-003", "REQ-004", "REQ-005"):
        assert req in out


def test_coverage_uncovered_shows_only_the_holes(inventoried, writ):
    code, out, _ = writ("coverage", "--uncovered")
    assert code == 0
    assert "REQ-003" in out
    assert "REQ-001" not in out


def test_list_requirements_names_what_covers_each_one(inventoried, writ):
    code, out, _ = writ("--json", "list", "requirements")
    rows = {row["id"]: row for row in json.loads(out)}
    assert rows["REQ-001"]["tasks"] == ["M01-001"]
    assert rows["REQ-003"]["state"] == "uncovered"


def test_list_findings_shows_the_ledger(inventoried, writ, project):
    with state.transaction(project) as data:
        data["tasks"]["M01-001"]["acceptances"] = [
            {"text": "it works", "status": "pending"}
        ]
        plans.run_check(data, root=project)
    code, out, _ = writ("--json", "list", "findings")
    records = json.loads(out)
    assert any(r["category"] == "vague-acceptance" for r in records)


def test_a_gate_is_not_held_to_a_requirement_the_plan_disowns(inventoried, project):
    from writ import gates

    data = state.load(project)
    claimed = data["tasks"]["G-FINAL"]["requirement_ids"]
    # In the inventory so a reader can see it was considered and left out. Not
    # something a gate can be asked to judge.
    assert "REQ-005" not in claimed
    assert "REQ-003" in claimed
    assert gates.answerable_requirements(data) == claimed


def _wordy_finding(where="REQ-003", head="nothing covers the queue depth view"):
    """A finding whose message is longer than a compacted one is allowed to be."""
    return plancheck.Finding(
        severity="error",
        category="missing-coverage",
        message=(
            f"{head}: the design calls for an operator to see how far behind the "
            "consumer is, and no task in the graph reads the offset, exposes it, or "
            "asserts on it. Nothing downstream would fail if it were never built."
        ),
        where=where,
        source="critic:coverage",
    )


def _resolve_then_advance(data, project, finding):
    """Raise a finding, let it go away, then move the plan on a revision."""
    plans.record_findings(data, [finding], scope="critic:coverage",
                          reporter="critic:coverage")
    record = plans.finding_records(data)[-1]
    plans.bump(data)
    plans.record_findings(data, [], scope="critic:coverage",
                          reporter="critic:coverage")
    plans.bump(data)
    plans.record_findings(data, [], scope="critic:coverage",
                          reporter="critic:coverage")
    return record


def test_a_long_resolved_finding_stops_carrying_its_argument(inventoried, project):
    """The prose is dropped once the objection is closed and a revision has passed.

    Findings are the bulk of a mature state file, and almost all of them are closed.
    Every agent that loads state pays to parse the paragraph explaining a defect that
    no longer exists.
    """
    with state.transaction(project) as data:
        record = _resolve_then_advance(data, project, _wordy_finding())
    assert record["disposition"] == "resolved"
    assert record["compacted"] is True
    assert "suggested_action" not in record
    # Still says what it was.
    assert record["message"].startswith("nothing covers the queue depth view")
    assert len(record["message"]) <= plans.RESOLVED_TEXT_CHARS + 1


def test_a_compacted_finding_is_still_the_same_finding(inventoried, project):
    """Shortening the message must not change what the finding *is*.

    Identity is category, place, source and the head of the message. Cut the message
    below that prefix and the same objection coming back would file as a new finding
    with a fresh `seen_count` — and the repeat bound that stops a repair loop from
    retrying one failed fix forever reads `seen_count`.
    """
    with state.transaction(project) as data:
        record = _resolve_then_advance(data, project, _wordy_finding())
        compacted_key = plans._finding_key(record)
        original_id = record["id"]
        before = len(plans.finding_records(data))

        # The critic reads the plan again and objects to the same thing.
        plans.record_findings(data, [_wordy_finding()], scope="critic:coverage",
                              reporter="critic:coverage")
        records = plans.finding_records(data)

    assert len(records) == before, "the same objection filed twice"
    came_back = next(r for r in records if r["id"] == original_id)
    assert plans._finding_key(came_back) == compacted_key
    assert came_back["disposition"] == "open"
    assert came_back["seen_count"] == 2
    assert came_back["reopened_at"]
    # Rewritten from the fresh report, so the full text is back.
    assert came_back["message"] == _wordy_finding().message


def test_a_finding_keeps_its_text_the_revision_it_is_resolved(inventoried, project):
    """A repair loop reads what it just closed. Compaction waits a revision."""
    with state.transaction(project) as data:
        finding = _wordy_finding()
        plans.record_findings(data, [finding], scope="critic:coverage",
                              reporter="critic:coverage")
        plans.bump(data)
        plans.record_findings(data, [], scope="critic:coverage",
                              reporter="critic:coverage")
        record = plans.finding_records(data)[-1]
    assert record["disposition"] == "resolved"
    assert not record.get("compacted")
    assert record["message"] == finding.message


def test_a_person_who_overruled_a_finding_keeps_every_word(inventoried, project):
    """A human judgement is the one thing here nobody may summarise.

    The objection's own text, sitting next to the reason someone overruled it, is the
    record of what they decided to ship. Shortening it would leave an approval whose
    subject had been paraphrased by a tool.
    """
    with state.transaction(project) as data:
        finding = _wordy_finding()
        plans.record_findings(data, [finding], scope="critic:coverage",
                              reporter="critic:coverage")
        record = plans.finding_records(data)[-1]
        plans.dispose(data, record["id"], "accepted", actor="tim",
                      reason="shipping without the depth view on purpose")
        plans.bump(data)
        plans.record_findings(data, [], scope="critic:coverage",
                              reporter="critic:coverage")
        plans.bump(data)
        plans.record_findings(data, [], scope="critic:coverage",
                              reporter="critic:coverage")
        record = next(r for r in plans.finding_records(data) if r["id"] == record["id"])
    assert record["disposition"] == "accepted"
    assert not record.get("compacted")
    assert record["message"] == finding.message


def test_a_shortened_message_still_contains_the_whole_identity_prefix():
    """The two constants are coupled, and only one of them is obvious.

    `_finding_key` hashes the head of the message, so a compacted message shorter
    than that prefix would change a finding's identity — silently, and only visibly
    much later as a repair loop that stopped noticing repeats. Lowering
    `RESOLVED_TEXT_CHARS` under `_KEY_PREFIX` should fail here rather than there.
    """
    assert plans.RESOLVED_TEXT_CHARS > plans._KEY_PREFIX
    text = "x" * 500
    assert plans._shorten(text, plans.RESOLVED_TEXT_CHARS)[: plans._KEY_PREFIX] == (
        text[: plans._KEY_PREFIX]
    )
    # Even a message that is all one word keeps the prefix intact.
    spaced = ("word " * 200)
    assert plans._shorten(spaced, plans.RESOLVED_TEXT_CHARS)[: plans._KEY_PREFIX] == (
        spaced[: plans._KEY_PREFIX]
    )
