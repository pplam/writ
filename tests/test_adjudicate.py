"""The bounded repair loop that answers a plan's findings before it executes.

Writ could already produce findings against a plan and resolve them once it was
running. Between those sat the gap these tests are about: a blocking finding before
execution had no repair path, only a hand disposition or `approve --force`. So the
claims worth pinning down are that an adjudicator's edit is validated rather than
trusted, that the bar cannot be lowered by one, that a finding closes on a re-check
rather than on the response's word, and that the loop stops.

The adjudicator is a real subprocess editing a real working copy, as in
test_gates.py. The parts worth testing only exist once something has actually
proposed a change.
"""
from __future__ import annotations

import json
import shlex
import sys

import pytest

from writ import adjudicate, phases, plancheck, planfiles, plans, repair, state
from writ.state import WritError

from tests.test_plans import PLAN


#: an adjudicator that edits the working copy the way the test tells it to.
#:
#: The edit is a JSON spec in WRIT_TEST_EDIT: `revise` merges fields into existing
#: feature files, `add` creates new ones, `remove` deletes them, and `analysis`,
#: `dispositions` and `questions` go into the response. Unless the spec says
#: `_auto_dispositions: false`, every finding in to-fix.json is accepted, with
#: `_decision` as the ruling when the spec gives one. A list
#: is one spec per round, so a test can be refused and then succeed.
ADJUDICATOR = """
import json, os, re, sys
from pathlib import Path
prompt = sys.stdin.read()
path = Path(re.search(r'Write your response as JSON to this exact path:\\n  (\\S+)', prompt).group(1))
round_no = int(re.search(r'Adjudication round: (\\d+)', prompt).group(1))
folder = path.parent
features = folder / "plan" / "features"
to_fix = [f["id"] for f in json.loads((folder / "to-fix.json").read_text())]
spec = json.loads(os.environ["WRIT_TEST_EDIT"])
if isinstance(spec, list):
    spec = spec[min(round_no, len(spec)) - 1]
for ref, fields in spec.get("revise", {}).items():
    file = features / (ref + ".json")
    entry = json.loads(file.read_text())
    entry.update(fields)
    file.write_text(json.dumps(entry))
for ref, fields in spec.get("add", {}).items():
    (features / (ref + ".json")).write_text(json.dumps(dict(fields, id=ref)))
for ref in spec.get("remove", []):
    (features / (ref + ".json")).unlink()
response = {k: spec[k] for k in ("analysis", "dispositions", "questions") if k in spec}
if spec.get("_auto_dispositions", True) and to_fix:
    response.setdefault("dispositions", [
        dict(
            {"finding_id": f, "disposition": "accepted", "change": "fixed it"},
            **({"decision": spec["_decision"]} if "_decision" in spec else {}),
        )
        for f in to_fix
    ])
path.write_text(json.dumps(response))
"""

MUTE = """
import sys
sys.stdin.read()
print("I would rather not")
"""

#: a critic that reports one blocking finding the first time it reads the plan and
#: nothing afterwards — a repair that actually worked, from the critic's side. The
#: marker file is how it remembers across processes.
CRITIC_ONCE = """
import json, os, re, sys
prompt = sys.stdin.read()
path = re.search(r'Write your findings as JSON to this exact path:\\n  (\\S+)', prompt).group(1)
marker = os.environ["WRIT_TEST_ONCE"]
first = not os.path.exists(marker)
open(marker, "a").write("x")
findings = [] if not first else [{
    "severity": "blocking",
    "category": "uncovered-requirement",
    "where": "REQ-003",
    "message": "No task implements the queue depth view",
    "suggested_action": "add a task, or mark it out of scope with a reason",
    "requirement_ids": ["REQ-003"],
    "evidence": "searched the plan and the repository for queue depth",
}]
open(path, "w").write(json.dumps({
    "findings": findings,
    "summary": "read it" if not first else "one hole",
    "confidence": "high",
}))
"""


#: a critic that objects to something *different* each time it reads.
#:
#: The production case the seventy-note bug was hiding: a repair closes what was
#: raised, and the re-read of the repaired plan finds a new hole in the work the
#: repair just added. That is the loop's whole purpose — and it needs two rounds, so
#: a stub that reports the same thing forever (which escalates) cannot express it.
CRITIC_MOVES_ON = """
import json, os, re, sys
prompt = sys.stdin.read()
path = re.search(r'Write your findings as JSON to this exact path:\\n  (\\S+)', prompt).group(1)
marker = os.environ["WRIT_TEST_READS"]
reads = len(open(marker).read()) if os.path.exists(marker) else 0
open(marker, "a").write("x")
# A new requirement each read, so each finding is its own objection rather than the
# same one coming back. Two of them, then satisfied. A verify pass may only raise a
# blocker on a feature the repair changed, so the second one is placed there.
holes = ["REQ-003", "REQ-004"]
verify = os.path.join(os.path.dirname(path), "verify.json")
changed = json.load(open(verify))["changed_features"] if os.path.exists(verify) else []
findings = []
if reads < len(holes):
    findings = [{
        "severity": "blocking",
        "category": "uncovered-requirement",
        "where": changed[0] if changed else holes[reads],
        "message": f"No task implements {holes[reads]}",
        "suggested_action": "add a task, or mark it out of scope with a reason",
        "requirement_ids": [holes[reads]],
        "evidence": "searched the plan and the repository",
    }]
open(path, "w").write(json.dumps({
    "findings": findings,
    "summary": "another hole" if findings else "clean",
    "confidence": "high",
}))
"""


def agent(script: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


def patched(monkeypatch, spec) -> None:
    """Hand the fake adjudicator the edit it should make."""
    monkeypatch.setenv("WRIT_TEST_EDIT", json.dumps(spec))


@pytest.fixture
def objected(writ, project, design, tmp_path):
    """A committed plan with one blocking finding standing against it.

    The finding is recorded directly rather than produced by a critic: what these
    tests are about is what happens *after* a finding exists, and a fake critic in
    the middle would only add a second thing that could break.
    """
    artifact = tmp_path / "plan.json"
    artifact.write_text(json.dumps(PLAN), encoding="utf-8")
    writ("init")
    writ("plan", str(design), "--from-plan", str(artifact))
    with state.transaction(project) as data:
        plans.record_findings(
            data,
            [
                plancheck.Finding(
                    severity="error",
                    category="uncovered-requirement",
                    message="No task implements the queue depth view",
                    where="REQ-003",
                    suggested_action="add a task, or mark it out of scope",
                    requirement_ids=["REQ-003"],
                    source="critic:fidelity",
                )
            ],
            scope="critic:fidelity",
        )
    return writ


#: the fixture's finding closed by adding the missing work
NEW_FEATURE = {
    "title": "Expose queue depth to the operator",
    "milestone": "M01",
    "requirement_ids": ["REQ-003"],
    "acceptances": [
        "a failing test in ops/depth_test.go reproduces the missing view",
        "`go test ./ops` reports queue depth",
    ],
    "allowed": ["ops/"],
}

ADDS_THE_TASK = {
    "analysis": "nothing in the plan reads queue depth",
    "add": {"new-queue-depth": NEW_FEATURE},
}


def _run(objected, *extra):
    return objected("adjudicate", "--agent", agent(ADJUDICATOR), "--no-critics", *extra)


def _added(data):
    return [
        task
        for task in data["tasks"].values()
        if task.get("repair", {}).get("scope") == "plan"
    ]


def _reasons(data):
    return [
        reason["category"]
        for request in repair.requests(data)
        for refusal in request.get("refusals") or []
        for reason in refusal["reasons"]
    ]


def _rounds(project):
    data = state.load(project)
    return planfiles.directory(project, data) / planfiles.ROUNDS_DIRNAME


# --------------------------------------------------------------------------
# the prompt


def test_the_prompt_names_files_instead_of_pasting_them(tmp_path):
    directory = tmp_path / ".writ" / "plans" / "p" / "rounds" / "r4" / "round-1"
    prompt = adjudicate.build_prompt(
        root=tmp_path,
        doc=tmp_path / "design.md",
        directory=directory,
        blocking=3,
        base_revision=4,
        round_number=1,
    )
    assert "has not been executed yet" in prompt
    assert "Plan revision: 4" in prompt
    assert f"Repository root: {tmp_path.resolve()}" in prompt
    # Relative paths, to the files the agent reads and the copy it edits.
    assert ".writ/plans/p/rounds/r4/round-1/to-fix.json" in prompt
    assert ".writ/plans/p/rounds/r4/round-1/plan/features" in prompt
    assert ".writ/plans/p/rounds/r4/round-1/plan/plan.json" in prompt
    assert "design.md" in prompt
    assert (
        "Write your response as JSON to this exact path:\n"
        "  .writ/plans/p/rounds/r4/round-1/response.json"
    ) in prompt
    # It is told the bar it will be held to.
    assert "never drop" in prompt
    assert "revise_tasks" not in prompt
    # No refusal to read on a first attempt.
    assert "REFUSED" not in prompt


def test_a_retry_is_pointed_at_why_the_last_attempt_was_refused(tmp_path):
    """A retry is only bounded if the next attempt knows more than the last."""
    previous = tmp_path / "rounds" / "r1" / "round-1" / "validation.json"
    prompt = adjudicate.build_prompt(
        root=tmp_path,
        doc=None,
        directory=tmp_path / "rounds" / "r1" / "round-2",
        blocking=1,
        previous=previous,
        round_number=2,
    )
    assert "REFUSED" in prompt
    assert "rounds/r1/round-1/validation.json" in prompt


# --------------------------------------------------------------------------
# an edit closes a finding


def test_an_adjudicated_plan_gains_the_work_the_finding_asked_for(
    objected, project, monkeypatch
):
    patched(monkeypatch, ADDS_THE_TASK)
    code, out, _ = _run(objected)
    data = state.load(project)
    added = _added(data)
    assert added, [t["id"] for t in data["tasks"].values()]
    assert added[0]["requirement_ids"] == ["REQ-003"]
    assert added[0]["repair"]["proposed_as"] == "new-queue-depth"
    assert code == 0, out


def test_the_finding_is_disposed_with_the_adjudicator_named(
    objected, project, monkeypatch
):
    patched(monkeypatch, ADDS_THE_TASK)
    _run(objected)
    record = [
        item
        for item in plans.finding_records(state.load(project))
        if item["source"] == "critic:fidelity"
    ][0]
    assert record["disposition"] == "accepted"
    assert record["disposed_by"] == "adjudicator"


def test_a_repair_bumps_the_plan_revision(objected, project, monkeypatch):
    before = plans.revision(state.load(project))
    patched(monkeypatch, ADDS_THE_TASK)
    _run(objected)
    assert plans.revision(state.load(project)) > before


def test_the_request_records_what_landed(objected, project, monkeypatch):
    patched(monkeypatch, ADDS_THE_TASK)
    _run(objected)
    data = state.load(project)
    request = repair.requests(data)[0]
    assert request["status"] == "applied"
    assert request["applied_tasks"]
    # Plan-scoped, so it names no gate — which is what keeps the scheduler from
    # ever trying to dispatch it.
    assert repair.is_plan_request(request)
    assert repair.scope_of(request) == "plan"


def test_the_plan_files_are_re_exported_at_the_new_revision(
    objected, project, monkeypatch
):
    patched(monkeypatch, ADDS_THE_TASK)
    _run(objected)
    data = state.load(project)
    index = json.loads(planfiles.index_path(project, data).read_text())
    assert index["revision"] == plans.revision(data)
    added = _added(data)[0]["id"]
    assert (planfiles.features_dir(project, data) / f"{added}.json").exists()


def test_a_round_leaves_its_working_copy_and_verdict_on_disk(
    objected, project, monkeypatch
):
    patched(monkeypatch, ADDS_THE_TASK)
    _run(objected)
    rounds = _rounds(project)
    [folder] = list(rounds.glob("r*/round-1"))
    for name in ("to-fix.json", "response.json", "validation.json", "prompt.txt"):
        assert (folder / name).exists(), name
    assert (folder / "plan" / "plan.json").exists()
    assert (folder / "plan" / "features" / "new-queue-depth.json").exists()
    verdict = json.loads((folder / "validation.json").read_text())
    assert verdict["accepted"] is True
    assert verdict["changes"]["added"] == ["new-queue-depth"]
    # Only blocking findings are handed over.
    to_fix = json.loads((folder / "to-fix.json").read_text())
    assert [item["severity"] for item in to_fix] == ["error"]


# --------------------------------------------------------------------------
# editing a feature, which only this occasion allows


REVISES_THE_TASK = {
    "analysis": "the criterion names no command",
    "revise": {
        "M01-001": {
            "acceptances": [
                "a failing test in store/log_test.go reproduces a torn append",
                "`go test ./store` passes with appends fsync'd in order",
                "`go test ./store -run Torn` proves the torn append is rejected",
            ],
        }
    },
}


def test_a_task_can_be_rewritten_before_it_runs(objected, project, monkeypatch):
    patched(monkeypatch, REVISES_THE_TASK)
    _run(objected)
    task = state.load(project)["tasks"]["M01-001"]
    assert len(task["acceptances"]) == 3
    # The revision is on the record.
    assert task["revisions"][0]["fields"] == ["acceptances"]


def test_a_revision_leaves_unedited_fields_alone(objected, project, monkeypatch):
    before = state.load(project)["tasks"]["M01-001"]
    patched(monkeypatch, REVISES_THE_TASK)
    _run(objected)
    after = state.load(project)["tasks"]["M01-001"]
    assert after["allowed"] == before["allowed"]
    assert after["requirement_ids"] == before["requirement_ids"]
    # A criterion whose wording did not change keeps its record.
    assert after["acceptances"][0] == before["acceptances"][0]


def test_a_feature_may_depend_on_one_the_same_edit_adds(
    objected, project, monkeypatch
):
    """The new feature's id is the copy's word, so writ has to translate the edge."""
    patched(
        monkeypatch,
        dict(
            ADDS_THE_TASK,
            revise={"M01-002": {"depends_on": ["M01-001", "new-queue-depth"]}},
        ),
    )
    code, _, _ = _run(objected)
    data = state.load(project)
    request = repair.requests(data)[-1]
    assert not request.get("refusals"), request.get("refusals")
    added = request["applied_tasks"][0]
    assert added in data["tasks"]["M01-002"]["depends_on"]
    assert "new-queue-depth" not in data["tasks"]["M01-002"]["depends_on"]
    assert code == 0


def test_a_dependency_must_name_a_feature_in_the_copy(objected, project, monkeypatch):
    patched(
        monkeypatch,
        {"analysis": "points at thin air", "revise": {"M01-002": {"depends_on": ["M09-999"]}}},
    )
    _run(objected)
    assert "bad-dependency" in _reasons(state.load(project))


def test_a_cycle_is_refused(objected, project, monkeypatch):
    patched(monkeypatch, {"revise": {"M01-001": {"depends_on": ["M01-002"]}}})
    _run(objected)
    assert "dependency-cycle" in _reasons(state.load(project))


def test_a_task_may_not_wait_for_its_own_milestone_gate(
    objected, project, monkeypatch
):
    """The gate waits for the milestone, so this edge closes a loop through it."""
    gate_id = next(
        task_id
        for task_id, task in state.load(project)["tasks"].items()
        if task.get("kind") == "gate" and task_id != "G-FINAL"
    )
    patched(monkeypatch, {"revise": {"M01-001": {"depends_on": [gate_id]}}})
    _run(objected)
    assert "dependency-cycle" in _reasons(state.load(project))


def test_two_overlapping_features_can_be_merged(objected, project, monkeypatch):
    """What the patch language could not say at all: one feature absorbs another."""
    before = state.load(project)["tasks"]
    patched(
        monkeypatch,
        dict(
            ADDS_THE_TASK,
            remove=["M01-002"],
            revise={
                "M01-001": {
                    "title": "Event log and its projection",
                    "requirement_ids": ["REQ-001", "REQ-002"],
                    "acceptances": [
                        *(item["text"] for item in before["M01-001"]["acceptances"]),
                        "a replay reproduces the projection byte-for-byte",
                    ],
                }
            },
        ),
    )
    code, out, _ = _run(objected)
    data = state.load(project)
    assert "M01-002" not in data["tasks"], out
    merged = data["tasks"]["M01-001"]
    assert merged["requirement_ids"] == ["REQ-001", "REQ-002"]
    request = repair.requests(data)[-1]
    assert request["removed_tasks"] == ["M01-002"]
    # Nothing still points at the task that is gone, gates included.
    for task in data["tasks"].values():
        assert "M01-002" not in task.get("depends_on", []), task["id"]
    assert code == 0, out


def test_gates_follow_their_milestone_after_a_repair(objected, project, monkeypatch):
    patched(monkeypatch, ADDS_THE_TASK)
    _run(objected)
    data = state.load(project)
    added = _added(data)[0]
    gate = next(
        task
        for task in data["tasks"].values()
        if task.get("kind") == "gate" and task["id"] != "G-FINAL"
        and task.get("milestone", "M01") == added["milestone"]
    )
    assert added["id"] in gate["depends_on"]
    assert "REQ-003" in gate["requirement_ids"]


def test_a_requirement_may_move_to_a_feature_the_edit_adds(
    objected, project, monkeypatch
):
    """Splitting an overloaded task is not dropping a requirement."""
    patched(
        monkeypatch,
        {
            "add": {
                "split-off": {
                    "title": "Fsync the log on append",
                    "milestone": "M01",
                    "requirement_ids": ["REQ-001"],
                    "acceptances": [
                        "`go test ./store -run Fsync` passes",
                        "store/fsync.go calls fsync before returning",
                    ],
                    "allowed": ["store/fsync.go"],
                }
            },
            "revise": {
                "M01-001": {
                    "requirement_ids": [],
                    "acceptances": [
                        "a failing test in store/log_test.go reproduces a torn append",
                        "`go test ./store` passes with appends fsync'd in order",
                        "store/log.go exposes the writer the split task calls",
                    ],
                }
            },
        },
    )
    _run(objected)
    data = state.load(project)
    assert data["tasks"]["M01-001"]["requirement_ids"] == []
    moved = [t for t in data["tasks"].values() if t.get("title") == "Fsync the log on append"]
    assert len(moved) == 1
    assert moved[0]["requirement_ids"] == ["REQ-001"]


# --------------------------------------------------------------------------
# the bar does not drop


def test_a_revision_may_not_drop_a_criterion(objected, project, monkeypatch):
    """The failure the review warned about: closing a finding by lowering the bar."""
    patched(monkeypatch, {"revise": {"M01-001": {"acceptances": ["it works"]}}})
    code, out, _ = _run(objected)
    data = state.load(project)
    assert len(data["tasks"]["M01-001"]["acceptances"]) == 2
    assert "weakened-criteria" in _reasons(data)
    assert code == 1
    assert "refused" in out


def test_a_revision_may_not_drop_coverage(objected, project, monkeypatch):
    patched(monkeypatch, {"revise": {"M01-001": {"requirement_ids": []}}})
    _run(objected)
    data = state.load(project)
    assert data["tasks"]["M01-001"]["requirement_ids"] == ["REQ-001"]
    assert "coverage-regression" in _reasons(data)


def test_a_removal_may_not_drop_coverage(objected, project, monkeypatch):
    """Deleting the only feature for a requirement is the same drop, said differently."""
    patched(monkeypatch, dict(ADDS_THE_TASK, remove=["M01-002"]))
    _run(objected)
    data = state.load(project)
    assert "M01-002" in data["tasks"]
    assert "coverage-regression" in _reasons(data)


def test_a_revision_may_not_invent_a_requirement(objected, project, monkeypatch):
    patched(
        monkeypatch,
        {"revise": {"M01-001": {"requirement_ids": ["REQ-001", "REQ-999"]}}},
    )
    _run(objected)
    data = state.load(project)
    assert data["tasks"]["M01-001"]["requirement_ids"] == ["REQ-001"]
    assert "unknown-requirement" in _reasons(data)


def test_a_gate_is_not_an_adjudicators_to_rewrite(objected, project, monkeypatch):
    """A gate's criteria are the plan's own bar, not a task contract."""
    data = state.load(project)
    gate_id = next(t["id"] for t in data["tasks"].values() if t.get("kind") == "gate")
    before = list(data["tasks"][gate_id]["acceptances"])
    patched(
        monkeypatch,
        {"revise": {gate_id: {"acceptances": ["it all works", "b", "c", "d", "e"]}}},
    )
    _run(objected)
    data = state.load(project)
    assert data["tasks"][gate_id]["acceptances"] == before
    assert "gate-edited" in _reasons(data)


def test_a_started_task_is_not_editable(objected, project, monkeypatch):
    with state.transaction(project) as data:
        data["tasks"]["M01-001"]["status"] = "running"
    patched(monkeypatch, REVISES_THE_TASK)
    _run(objected)
    data = state.load(project)
    assert len(data["tasks"]["M01-001"]["acceptances"]) == 2
    assert "started-task-edited" in _reasons(data)


def test_a_feature_may_not_change_milestone_in_place(objected, project, monkeypatch):
    patched(monkeypatch, {"revise": {"M01-001": {"milestone": "M02"}}})
    _run(objected)
    assert "fixed-field-edited" in _reasons(state.load(project))


def test_a_new_feature_needs_a_known_milestone(objected, project, monkeypatch):
    patched(
        monkeypatch,
        {"add": {"new-queue-depth": dict(NEW_FEATURE, milestone="M42")}},
    )
    _run(objected)
    data = state.load(project)
    assert not _added(data)
    assert "feature-shape" in _reasons(data)


def test_a_feature_without_criteria_is_refused(objected, project, monkeypatch):
    patched(
        monkeypatch,
        {"add": {"new-queue-depth": dict(NEW_FEATURE, acceptances=[])}},
    )
    _run(objected)
    assert "feature-shape" in _reasons(state.load(project))


# --------------------------------------------------------------------------
# validation, directly


@pytest.fixture
def prepared(objected, project, tmp_path):
    """A round directory writ has prepared, and the findings it was prepared for."""
    data = state.load(project)
    blocking = [f for f in plans.findings(data, open_only=True) if f.severity == "error"]
    directory = tmp_path / "round"
    adjudicate.prepare(project, data, directory, blocking)
    return data, directory, [f.id for f in blocking]


def _answer(ids):
    return {
        "analysis": "",
        "dispositions": [
            {"finding_id": f, "disposition": "accepted", "change": "fixed"} for f in ids
        ],
        "questions": [],
    }


def _add_new(directory):
    folder = directory / "plan" / "features"
    (folder / "new-x.json").write_text(json.dumps(dict(NEW_FEATURE, id="new-x")))


def test_an_untouched_copy_validates_as_no_op(prepared):
    data, directory, ids = prepared
    found, _, diff = adjudicate.validate(
        data, directory, _answer(ids), finding_ids=ids, base_revision=plans.revision(data)
    )
    assert [f.category for f in found] == ["no-op"]
    assert diff == {"revised": [], "added": [], "removed": []}


def test_a_copy_of_an_older_revision_is_stale(prepared):
    data, directory, ids = prepared
    _add_new(directory)
    found, _, _ = adjudicate.validate(
        data, directory, _answer(ids), finding_ids=ids,
        base_revision=plans.revision(data) - 1,
    )
    assert "stale-copy" in [f.category for f in found]


def test_the_requirement_inventory_is_fixed(prepared):
    data, directory, ids = prepared
    _add_new(directory)
    index_path = directory / "plan" / "plan.json"
    index = json.loads(index_path.read_text())
    index["requirements"][0]["text"] = "something easier"
    index_path.write_text(json.dumps(index))
    found, _, _ = adjudicate.validate(
        data, directory, _answer(ids), finding_ids=ids, base_revision=plans.revision(data)
    )
    assert "requirements-edited" in [f.category for f in found]


def test_a_mismatched_filename_is_refused(prepared):
    data, directory, ids = prepared
    folder = directory / "plan" / "features"
    (folder / "new-x.json").write_text(json.dumps(dict(NEW_FEATURE, id="new-y")))
    found, _, _ = adjudicate.validate(
        data, directory, _answer(ids), finding_ids=ids, base_revision=plans.revision(data)
    )
    assert "feature-shape" in [f.category for f in found]


def test_a_decline_needs_a_reason(prepared):
    data, directory, ids = prepared
    _add_new(directory)
    response = _answer(ids)
    response["dispositions"] = [{"finding_id": ids[0], "disposition": "declined"}]
    found, _, _ = adjudicate.validate(
        data, directory, response, finding_ids=ids, base_revision=plans.revision(data)
    )
    assert "undisposed-finding" in [f.category for f in found]


def test_a_sound_copy_validates_clean(prepared):
    data, directory, ids = prepared
    _add_new(directory)
    found, proposed, diff = adjudicate.validate(
        data, directory, _answer(ids), finding_ids=ids, base_revision=plans.revision(data)
    )
    assert [f for f in found if f.blocking] == []
    assert diff["added"] == ["new-x"]
    assert "new-x" in proposed


# --------------------------------------------------------------------------
# a refusal, and the attempt after it


def test_an_empty_edit_is_refused(objected, project, monkeypatch):
    patched(monkeypatch, {"analysis": "I looked and it seems fine"})
    code, out, _ = _run(objected)
    assert code == 1
    assert "refused" in out
    assert "no-op" in _reasons(state.load(project))


def test_an_accepted_finding_must_name_its_change(objected, project, monkeypatch):
    patched(
        monkeypatch,
        dict(
            ADDS_THE_TASK,
            _auto_dispositions=False,
            dispositions=[{"finding_id": "F-0001", "disposition": "accepted"}],
        ),
    )
    code, out, _ = _run(objected)
    assert code == 1
    assert "refused" in out
    assert "undisposed-finding" in _reasons(state.load(project))


def test_a_refused_edit_leaves_the_request_open_for_the_next_round(
    objected, project, monkeypatch
):
    patched(monkeypatch, {"revise": {"M01-001": {"acceptances": ["it works"]}}})
    _run(objected)
    request = repair.requests(state.load(project))[0]
    assert request["status"] == "open"
    assert repair.refusals(request) >= 1


def test_a_refusal_writes_why_next_to_the_copy(objected, project, monkeypatch):
    patched(monkeypatch, {"revise": {"M01-001": {"acceptances": ["it works"]}}})
    _run(objected)
    [folder] = list(_rounds(project).glob("r*/round-1"))
    verdict = json.loads((folder / "validation.json").read_text())
    assert verdict["accepted"] is False
    assert "weakened-criteria" in [item["category"] for item in verdict["problems"]]


def test_a_retry_starts_from_the_refused_copy(objected, project, monkeypatch):
    """The sound edits of a refused attempt are still there on the next one.

    Round 1 adds the missing feature and, in the same attempt, drops a criterion.
    Round 2 only restores the criterion. The feature it never re-created lands
    anyway, because round 2 edited round 1's copy rather than a fresh one.
    """
    two = [
        "a failing test in store/log_test.go reproduces a torn append",
        "`go test ./store` passes with appends fsync'd in order",
    ]
    patched(
        monkeypatch,
        [
            dict(ADDS_THE_TASK, revise={"M01-001": {"acceptances": ["it works"]}}),
            {"revise": {"M01-001": {"acceptances": two}}},
        ],
    )
    code, out, _ = _run(objected)
    data = state.load(project)
    added = _added(data)
    assert [task["repair"]["proposed_as"] for task in added] == ["new-queue-depth"], out
    request = repair.requests(data)[-1]
    assert request["status"] == "applied"
    assert repair.refusals(request) == 1
    # And the retry was told to read why the first one was refused.
    [second] = list(_rounds(project).glob("r*/round-2"))
    assert "round-1/validation.json" in (second / "prompt.txt").read_text()
    assert code == 0, out


def test_a_retry_after_a_landed_repair_starts_fresh(objected, project, monkeypatch):
    """A refused copy of an older revision would undo what landed since."""
    patched(monkeypatch, {"revise": {"M01-001": {"acceptances": ["it works"]}}})
    _run(objected)
    data = state.load(project)
    request = repair.plan_request(data)
    [refused] = list(_rounds(project).glob("r*/round-1"))
    assert (refused / "plan" / "features").is_dir()
    revision = plans.revision(data)
    # Same revision: the refused copy is where the next attempt starts.
    seed, previous = adjudicate._previous_attempt(
        project, request, revision, refused.parent / "round-99"
    )
    assert seed is not None and previous is not None
    # A revision later, it is not.
    seed, previous = adjudicate._previous_attempt(
        project, request, revision + 1, refused.parent / "round-99"
    )
    assert seed is None and previous is None


# --------------------------------------------------------------------------
# the loop is bounded


def test_the_loop_stops_when_writ_keeps_refusing(objected, project, monkeypatch):
    """An edit that never passes must not be tried forever."""
    patched(monkeypatch, {"revise": {"M01-001": {"acceptances": ["it works"]}}})
    code, out, _ = _run(objected, "--max-rounds", "2")
    data = state.load(project)
    assert [r for r in repair.requests(data) if r["status"] == "applied"] == []
    assert "stopped:" in out
    assert "refused" in out
    assert code == 1


def test_a_refused_edit_does_not_spend_a_round(objected, project, monkeypatch):
    """A refusal is information for the next attempt, not a repair that happened.

    Counting agent runs against the budget instead of landed repairs meant two
    refusals — the one thing writ hands straight back with the reason — exhausted a
    plan's whole repair allowance.
    """
    patched(
        monkeypatch,
        [
            {"analysis": "first try", "revise": {"M01-002": {"depends_on": ["M09-999"]}}},
            # The retry starts from the refused copy, so it has to undo the bad edge.
            dict(ADDS_THE_TASK, revise={"M01-002": {"depends_on": ["M01-001"]}}),
        ],
    )
    code, out, _ = _run(objected, "--max-rounds", "1")
    data = state.load(project)
    applied = [r for r in repair.requests(data) if r["status"] == "applied"]
    assert applied, out
    assert applied[-1].get("refusals"), applied[-1]
    assert repair.plan_rounds(data) == 1
    assert code == 0


def test_zero_rounds_adjudicates_nothing(objected, project, monkeypatch):
    patched(monkeypatch, ADDS_THE_TASK)
    code, out, _ = _run(objected, "--max-rounds", "0")
    assert repair.requests(state.load(project)) == []
    # Says what happened rather than reporting a budget spent: nothing was tried,
    # which is not the same fact as a plan that has been repaired to its limit.
    assert "no repair was allowed" in out
    assert code == 1


def test_an_adjudicator_that_writes_nothing_stops_the_loop(objected, project):
    code, out, err = objected("adjudicate", "--agent", agent(MUTE), "--no-critics")
    assert code == 1
    assert "wrote no response" in out + err
    # And it did not leave the request claiming to be mid-planning.
    request = repair.requests(state.load(project))[0]
    assert request["status"] == "open"


def test_a_copy_that_cannot_be_promoted_leaves_the_request_open(
    objected, project, monkeypatch
):
    """Any promotion failure is survivable, and named as writ's, not the agent's.

    A copy that passed validation and then raised on the way in once took the
    exception out through `loop`, leaving the request at `planning` — a status that
    is neither finished nor retryable by anything.
    """
    patched(monkeypatch, ADDS_THE_TASK)

    def explode(*args, **kwargs):
        raise WritError("task M01-003 already exists")

    monkeypatch.setattr(adjudicate, "promote", explode)
    code, out, err = _run(objected)
    text = out + err
    assert "could not be applied" in text, text
    assert "adjudicator failed" not in text, text
    request = repair.requests(state.load(project))[0]
    assert request["status"] == "open", request["status"]
    # Nothing half-applied: the transaction rolled back.
    assert not _added(state.load(project))


def test_a_question_stops_the_loop_and_reaches_the_decision_log(
    objected, project, monkeypatch
):
    patched(
        monkeypatch,
        {
            "_auto_dispositions": False,
            "questions": [
                {
                    "finding_id": "F-0001",
                    "question": "Is queue depth per shard or per cluster?",
                }
            ],
        },
    )
    code, out, _ = _run(objected)
    data = state.load(project)
    assert data["decisions"], data
    assert "per shard" in data["decisions"][0]["title"]
    assert code == 1


def _per_shard(project):
    """A question about the fixture's finding, with the answer the asker would give."""
    [finding] = [
        f
        for f in plans.findings(state.load(project), open_only=True)
        if f.severity == "error"
    ]
    return {
        "_auto_dispositions": False,
        "questions": [
            {
                "finding_id": finding.id,
                "question": "Is queue depth per shard or per cluster?",
                "recommendation": "Per shard: the operator view is per shard.",
            }
        ],
    }


def test_autonomous_mode_answers_a_question_with_its_recommendation(
    objected, project, monkeypatch
):
    patched(monkeypatch, [_per_shard(project), ADDS_THE_TASK])
    code, out, err = _run(objected, "--autonomous")
    assert code == 0, out + err
    assert "with their recommendations (autonomous)" in out
    data = state.load(project)
    record = data["decisions"][0]
    assert record["status"] == "active"
    assert record["decision"] == "Per shard: the operator view is per shard."
    assert record["confirmed_by"] == "autonomous"
    assert _added(data)
    # the ruling is handed to the next round with the finding it settles
    rounds = sorted(_rounds(project).glob("*/round-*"))
    handed = json.loads((rounds[-1] / "to-fix.json").read_text())
    assert "Writ decided this autonomously" in handed[0]["suggested_action"]


def test_autonomous_mode_still_holds_a_question_without_a_recommendation(
    objected, project, monkeypatch
):
    spec = {
        "_auto_dispositions": False,
        "questions": [{"finding_id": "F-0001", "question": "Per shard?"}],
    }
    patched(monkeypatch, spec)
    code, _, _ = _run(objected, "--autonomous")
    assert code == 1
    assert state.load(project)["decisions"][0]["status"] == "proposed"


def test_the_same_question_twice_goes_to_a_person(objected, project, monkeypatch):
    """One autonomous answer per finding: asked again, it is going in circles."""
    patched(monkeypatch, [_per_shard(project)] * 2)
    code, _, _ = _run(objected, "--autonomous")
    assert code == 1
    statuses = [item["status"] for item in state.load(project)["decisions"]]
    assert statuses == ["active", "proposed"]


def test_edits_and_a_question_both_land(objected, project, monkeypatch):
    patched(
        monkeypatch,
        dict(
            ADDS_THE_TASK,
            questions=[{"finding_id": "F-0001", "question": "Per shard or per cluster?"}],
        ),
    )
    _, out, _ = _run(objected)
    data = state.load(project)
    assert _added(data)
    assert data["decisions"]
    assert "question" in out


def test_a_finding_that_survives_its_repair_is_escalated(objected, project):
    """The worse bound: the same objection coming back after a repair closed it."""
    with state.transaction(project) as data:
        record = [
            item
            for item in plans.finding_records(data)
            if item["source"] == "critic:fidelity"
        ][0]
        record["seen_count"] = repair.REPEAT_FINDING_LIMIT + 1
        record["reopened_at"] = "2026-01-01T00:00:00Z"
        repair.open_request(
            data,
            gate_id=None,
            finding_ids=[record["id"]],
            summary="still open",
            actor="adjudicator",
        )["status"] = "applied"
    assert repair.plan_repeat_findings(state.load(project))
    assert "survived" in repair.plan_exhausted(state.load(project), max_rounds=9)


def test_repeated_advisories_do_not_escalate_the_plan(objected, project):
    """The bound is about findings that were repaired, so advisories are not it.

    A critic re-reports every note it still believes each time it re-reads, so a
    plan with seventy notes crosses any seen-count limit the first time the critics
    run twice — on a plan whose blocking findings were being fixed exactly as
    intended.
    """
    with state.transaction(project) as data:
        plans.record_findings(
            data,
            [
                plancheck.Finding(
                    severity=severity,
                    category="unjustified-task",
                    message="nothing in the design asks for it",
                    where="M01-001",
                    suggested_action="name the requirement it serves",
                    source="critic:feasibility",
                )
                for severity in ("note", "warning")
            ],
            scope="critic:feasibility",
        )
        for record in plans.finding_records(data):
            if record["severity"] in ("note", "warning"):
                record["seen_count"] = repair.REPEAT_FINDING_LIMIT + 2
                record["reopened_at"] = "2026-01-01T00:00:00Z"
        repair.open_request(
            data,
            gate_id=None,
            finding_ids=[],
            summary="one round landed",
            actor="adjudicator",
        )["status"] = "applied"
    data = state.load(project)
    assert repair.plan_repeat_findings(data) == []
    assert repair.plan_exhausted(data, max_rounds=9) == ""


def test_a_blocking_finding_reopened_once_escalates(objected, project):
    """`reopened_at` alone is the signal: a re-check disagreed with a repair."""
    with state.transaction(project) as data:
        record = [
            item
            for item in plans.finding_records(data)
            if item["source"] == "critic:fidelity"
        ][0]
        record["seen_count"] = 1
        record["reopened_at"] = "2026-01-01T00:00:00Z"
        repair.open_request(
            data,
            gate_id=None,
            finding_ids=[record["id"]],
            summary="still open",
            actor="adjudicator",
        )["status"] = "applied"
    data = state.load(project)
    assert repair.plan_repeat_findings(data) == [record["id"]]
    assert "survived" in repair.plan_exhausted(data, max_rounds=9)


def test_the_plan_bound_counts_only_applied_repairs(objected, project):
    with state.transaction(project) as data:
        repair.open_request(
            data,
            gate_id=None,
            finding_ids=[],
            summary="refused twice",
            actor="adjudicator",
        )
    # An open request is not a round: nothing landed, so nothing was spent.
    assert repair.plan_rounds(state.load(project)) == 0



# --------------------------------------------------------------------------
# a finding closes on a re-check, not on the patch's word


#: a critic that keeps objecting no matter what the patch did.
#:
#: Which critic it is comes from the report path, not the brief: writ writes each
#: critic's report under a directory named for it, and no critic's brief happens to
#: contain its own name.
STUBBORN_CRITIC = """
import json, os, re, sys
prompt = sys.stdin.read()
path = re.search(r'Write your findings as JSON to this exact path:\\n  (\\S+)', prompt).group(1)
findings, still_open = [], []
verify = os.path.join(os.path.dirname(path), "verify.json")
if "/fidelity/" in path and os.path.exists(verify):
    # a verify pass: every earlier blocker still stands
    still_open = [item["id"] for item in json.load(open(verify))["open"]]
elif "/fidelity/" in path:
    findings = [{
        "severity": "blocking",
        "category": "uncovered-requirement",
        "where": "REQ-003",
        "message": "No task implements the queue depth view",
        "suggested_action": "add a task, or mark it out of scope",
    }]
open(path, "w").write(json.dumps(
    {"findings": findings, "still_open": still_open, "summary": "still not covered"}
))
"""

#: a critic satisfied by whatever the patch did
SATISFIED_CRITIC = """
import json, re, sys
prompt = sys.stdin.read()
path = re.search(r'Write your findings as JSON to this exact path:\\n  (\\S+)', prompt).group(1)
open(path, "w").write(json.dumps({"findings": [], "summary": "the repair covers it"}))
"""


def test_the_critics_re_read_the_patched_plan(objected, project, monkeypatch):
    """A critic that passed revision 2 has not reviewed revision 3."""
    patched(monkeypatch, ADDS_THE_TASK)
    objected(
        "adjudicate",
        "--agent",
        agent(ADJUDICATOR),
        "--critic-agent",
        agent(SATISFIED_CRITIC),
    )
    data = state.load(project)
    revision = plans.revision(data)
    reviewed = {
        entry["critic"]
        for entry in data.get("reviews", [])
        if entry.get("revision") == revision
    }
    assert reviewed, data.get("reviews")


def test_a_finding_a_critic_still_reports_does_not_close(
    objected, project, monkeypatch
):
    """The invariant: the patch's claim does not settle it, the re-review does.

    The adjudicator accepts the finding and adds work. The coverage critic reads the
    patched plan and says the requirement is still uncovered. The finding has to be
    open at the end — a repair that closed its own objection would be the loop
    laundering a plan past its critics.
    """
    patched(monkeypatch, ADDS_THE_TASK)
    code, out, _ = objected(
        "adjudicate",
        "--agent",
        agent(ADJUDICATOR),
        "--critic-agent",
        agent(STUBBORN_CRITIC),
        "--max-rounds",
        "1",
    )
    data = state.load(project)
    reopened = [
        item
        for item in plans.finding_records(data)
        if item["category"] in ("missing-coverage", "uncovered-requirement")
        and item["disposition"] == "open"
    ]
    assert reopened, plans.finding_records(data)
    assert code == 1


# --------------------------------------------------------------------------
# what it refuses to do at all


def test_adjudicating_a_clean_plan_does_nothing(writ, project, design, tmp_path):
    artifact = tmp_path / "plan.json"
    artifact.write_text(json.dumps(PLAN), encoding="utf-8")
    writ("init")
    writ("plan", str(design), "--from-plan", str(artifact), "--auto-approve")
    code, out, _ = writ("adjudicate", "--no-critics")
    assert code == 0
    assert "nothing blocking" in out
    assert repair.requests(state.load(project)) == []


def test_an_executing_plan_is_repaired_by_its_gates(objected, project):
    with state.transaction(project) as data:
        plans.set_status(data, "executing")
    code, _, err = objected("adjudicate", "--no-critics")
    assert code == 2
    assert "executing" in err


def test_there_is_nothing_to_adjudicate_without_a_plan(writ):
    writ("init")
    code, _, err = writ("adjudicate", "--no-critics")
    assert code == 2
    assert "no plan" in err


def test_a_plan_request_is_never_dispatched_as_a_gate_repair(objected, project):
    """The scheduler's half of the separation: a plan request has no gate."""
    from writ import orchestrator

    with state.transaction(project) as data:
        plans.set_status(data, "approved")
        repair.open_request(
            data,
            gate_id=None,
            finding_ids=[],
            summary="open, pre-execution",
            actor="adjudicator",
        )
    data = state.load(project)
    job = orchestrator.next_job(data, busy=[], budget=None, started=[])
    assert job is None or job.role != "repair"
    # And `gate_requests` is the view the scheduler side should be reading.
    assert repair.gate_requests(data) == []


# --------------------------------------------------------------------------
# reached from `writ plan`


def test_plan_repair_answers_what_the_critics_found_before_approval(
    writ, project, design, tmp_path, monkeypatch
):
    """One command from document to runnable graph, findings answered on the way.

    The loop existed but nothing in `writ plan` reached it, so an unattended run
    that asked for the critics and `--auto-approve` stopped at `needs-approval`
    with nobody whose job was to answer what they found. `--repair` is that path,
    and approval still comes last: it approves the plan as repair left it.
    """
    artifact = tmp_path / "plan.json"
    artifact.write_text(json.dumps(PLAN), encoding="utf-8")
    writ("init")
    # Reports on its first read and is satisfied on the re-read, which is what a
    # repair that worked looks like from a critic's side. A stub that reported the
    # same finding forever would be testing that a finding survives its own repair,
    # which is a different claim and already covered above.
    monkeypatch.setenv("WRIT_TEST_ONCE", str(tmp_path / "reported"))
    patched(monkeypatch, ADDS_THE_TASK)
    code, out, _ = writ(
        "plan", str(design), "--from-plan", str(artifact),
        "--critics", "fidelity", "--critic-agent", agent(CRITIC_ONCE),
        "--repair", "--adjudicator-agent", agent(ADJUDICATOR),
        "--auto-approve", "--quiet",
    )
    assert code == 0
    assert "repairing the plan" in out
    data = state.load(project)
    # The patch landed, the finding closed on the re-check, and approval followed.
    assert repair.requests(data)
    record = plans.plan_status(data)
    assert record["status"] == "approved"
    assert record["approved_by"] == "writ --auto-approve"


def test_check_stops_listing_what_the_repair_answered(
    writ, project, design, tmp_path, monkeypatch
):
    """The end of the loop, from the reader's side.

    Three things had to be true for this to work and none of them was: the patch
    had to be accepted, the critic's re-read had to be able to close its own
    finding, and the adjudicator's `accepted` had to become `resolved` once nothing
    reported it. Until then `writ check` listed every objection the repair had just
    answered, which is what a person reads to decide whether to approve.
    """
    artifact = tmp_path / "plan.json"
    artifact.write_text(json.dumps(PLAN), encoding="utf-8")
    writ("init")
    monkeypatch.setenv("WRIT_TEST_ONCE", str(tmp_path / "reported"))
    patched(monkeypatch, ADDS_THE_TASK)
    writ(
        "plan", str(design), "--from-plan", str(artifact),
        "--critics", "fidelity", "--critic-agent", agent(CRITIC_ONCE),
        "--repair", "--adjudicator-agent", agent(ADJUDICATOR),
        "--quiet",
    )
    data = state.load(project)
    answered = [
        record["id"]
        for record in plans.finding_records(data)
        if record["category"] in ("missing-coverage", "uncovered-requirement")
    ]
    assert answered
    for finding_id in answered:
        assert plans.get_finding(data, finding_id)["disposition"] == "resolved"
    code, out, _ = writ("check")
    # Not listed, because it is not open — and the plan is no longer held by it.
    for finding_id in answered:
        assert finding_id not in out
    assert code == 0


def test_plan_without_repair_leaves_the_findings_standing(
    writ, project, design, tmp_path, monkeypatch
):
    """Opt-in, like the critics: the loop spends agent runs, so it waits to be asked."""
    artifact = tmp_path / "plan.json"
    artifact.write_text(json.dumps(PLAN), encoding="utf-8")
    writ("init")
    monkeypatch.setenv("WRIT_TEST_ONCE", str(tmp_path / "reported"))
    patched(monkeypatch, ADDS_THE_TASK)
    code, out, _ = writ(
        "plan", str(design), "--from-plan", str(artifact),
        "--critics", "fidelity", "--critic-agent", agent(CRITIC_ONCE),
        "--auto-approve", "--quiet",
    )
    assert code == 0
    assert "repairing the plan" not in out
    data = state.load(project)
    assert repair.requests(data) == []
    assert plans.plan_status(data)["status"] == "needs-approval"


def test_a_resumed_round_reaches_the_phase_record(objected, project, monkeypatch):
    """`writ adjudicate` draws its rounds into the attempt they belong to.

    The command is how a stopped loop is resumed: the loop gives up with findings
    open, a human settles them, and this carries on. It wrote nothing to the phase
    record, so the dashboard kept showing the planning run that stopped — the same
    critics, the one repair box it had already drawn — while the rounds that
    followed were on disk and in `state.json`. The loop had run and the only view of
    it said it had not.
    """
    patched(monkeypatch, ADDS_THE_TASK)
    # A phase for the plan under test, as `writ plan` would have left it: closed,
    # with the steps that ran. The fixture commits from `--from-plan`, which records
    # no pipeline, so the id the two are matched on is set here too.
    plan_id = "design-20260101T000000"
    with state.transaction(project) as data:
        plans.plan_status(data)["pipeline"] = {"plan_id": plan_id}
    phase_id = phases.begin(
        project,
        doc="design.md",
        plan_id=plan_id,
        steps=[phases.make_step(id="commit", kind="commit", name="commit", wave=0)],
    )
    phases.finish(project, phase_id, status="done")

    code, out, _ = objected(
        "adjudicate", "--agent", agent(ADJUDICATOR), "--no-critics"
    )
    assert code == 0, out
    record = phases.current(state.load(project)) or {}
    rounds = [
        entry for entry in record.get("steps", []) if entry.get("kind") == "repair"
    ]
    assert rounds, [entry.get("id") for entry in record.get("steps", [])]
    assert rounds[0]["status"] == "ok", rounds[0]
    assert "applied" in rounds[0].get("note", ""), rounds[0]
    # And the phase it reopened is closed again, so nothing looks in flight.
    assert record.get("status") == "done", record.get("status")
    assert record.get("finished_at")


def test_a_round_is_not_drawn_into_another_plans_attempt(objected, project, monkeypatch):
    """A phase for a different plan is left alone.

    Adjudication is about one committed plan. Attaching its rounds to whatever
    attempt happened to be newest would draw them into a graph they were no part of,
    so a phase whose `plan_id` does not match is declined and the rounds go
    unrecorded rather than recorded in the wrong place.
    """
    patched(monkeypatch, ADDS_THE_TASK)
    phase_id = phases.begin(
        project,
        doc="other.md",
        plan_id="some-other-plan",
        steps=[phases.make_step(id="commit", kind="commit", name="commit", wave=0)],
    )
    phases.finish(project, phase_id, status="done")

    code, out, _ = objected(
        "adjudicate", "--agent", agent(ADJUDICATOR), "--no-critics"
    )
    assert code == 0, out
    record = phases.current(state.load(project)) or {}
    assert record.get("plan_id") == "some-other-plan"
    assert [e for e in record.get("steps", []) if e.get("kind") == "repair"] == []


def test_a_new_blocking_finding_after_a_patch_opens_another_round(
    writ, project, design, tmp_path, monkeypatch
):
    """The loop keeps going while each round is answering something new.

    This is the case the production bug broke, and nothing covered it: round 1 lands,
    the critics re-read the patched plan, and they object to the work the patch just
    added. That must open round 2 — a plan is not beyond repair because repairing it
    revealed the next problem.

    It broke because the repeat-finding bound counted advisories. A real plan carries
    dozens of notes the critics re-report on every read, so the first re-review pushed
    all of them past the limit at once and the loop escalated, leaving the blocking
    findings it had just been handed unanswered.
    """
    artifact = tmp_path / "plan.json"
    artifact.write_text(json.dumps(PLAN), encoding="utf-8")
    writ("init")
    monkeypatch.setenv("WRIT_TEST_READS", str(tmp_path / "reads"))
    # Advisories alongside the blocking ones, at the volume a real plan has, so the
    # test fails if they are ever counted towards escalation again.
    patched(monkeypatch, ADDS_THE_TASK)
    with state.transaction(project) as data:
        plans.record_findings(
            data,
            [
                plancheck.Finding(
                    severity="note",
                    category="unknown-path",
                    message=f"allowed path 'src/thing{n}.py' does not exist yet",
                    where="M01-001",
                    suggested_action="confirm the path",
                    source="critic:feasibility",
                )
                for n in range(40)
            ],
            scope="critic:feasibility",
        )
        for record in plans.finding_records(data):
            if record["severity"] == "note":
                record["seen_count"] = repair.REPEAT_FINDING_LIMIT + 3

    code, out, _ = writ(
        "plan", str(design), "--from-plan", str(artifact),
        "--critics", "fidelity", "--critic-agent", agent(CRITIC_MOVES_ON),
        "--repair", "--adjudicator-agent", agent(ADJUDICATOR),
        "--max-rounds", "4", "--quiet",
    )
    data = state.load(project)
    applied = [r for r in repair.requests(data) if r.get("status") == "applied"]
    assert len(applied) >= 2, (
        f"the loop stopped after {len(applied)} round(s); out:\n{out}"
    )
    # Both objections were answered, and nothing blocking is left standing.
    # Both of the critic's objections were answered. Deliberately not "nothing
    # blocking is left": the stub patch adds a task owning a path an earlier one
    # owns, so writ's own shared-ownership check objects to the fixture — which is
    # that check doing its job, and not what this test is about.
    raised = [
        record
        for record in plans.finding_records(data)
        if record["scope"] == "critic:fidelity"
    ]
    assert len(raised) == 2, [r["id"] for r in raised]
    for record in raised:
        assert record["disposition"] in ("resolved", "accepted"), record
    # Each round answered a different objection, which is what distinguishes this
    # from a finding surviving its repair.
    assert {tuple(record["requirement_ids"]) for record in raised} == {
        ("REQ-003",),
        ("REQ-004",),
    }


def test_each_resumed_round_gets_its_own_step(objected, project, monkeypatch):
    """Two rounds, two boxes. The second is appended when it opens.

    How many rounds there will be is not knowable when the loop starts — it depends
    on what each patch fixed — so the record has to grow as the loop does. A single
    step reused by every round would show one repair where three happened.
    """
    monkeypatch.setenv("WRIT_TEST_READS", str(project / "reads"))
    patched(monkeypatch, ADDS_THE_TASK)
    plan_id = "design-20260101T000000"
    with state.transaction(project) as data:
        plans.plan_status(data)["pipeline"] = {"plan_id": plan_id}
    phase_id = phases.begin(
        project,
        doc="design.md",
        plan_id=plan_id,
        steps=[phases.make_step(id="commit", kind="commit", name="commit", wave=0)],
    )
    phases.finish(project, phase_id, status="done")

    code, out, _ = objected(
        "adjudicate",
        "--agent", agent(ADJUDICATOR),
        "--critics", "fidelity",
        "--critic-agent", agent(CRITIC_MOVES_ON),
        "--max-rounds", "4",
    )
    record = phases.current(state.load(project)) or {}
    rounds = [e for e in record.get("steps", []) if e.get("kind") == "repair"]
    assert len(rounds) >= 2, (
        f"{len(rounds)} repair step(s) for a loop that ran more than one round; "
        f"steps: {[e.get('id') for e in record.get('steps', [])]}\n{out}"
    )
    # Distinct ids, each pointing at its own transcript directory.
    assert len({e["id"] for e in rounds}) == len(rounds), rounds
    assert len({e.get("directory") for e in rounds}) == len(rounds), rounds
    # And the re-reviews landed on the same phase.
    assert [e for e in record.get("steps", []) if e.get("kind") == "critic"], record
