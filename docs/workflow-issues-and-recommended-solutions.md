# Workflow Issues and Recommended Solutions

Triaged against the implementation on 2026-09-22. Each issue below was confirmed by
reading the cited code. Items from the original review that did not survive triage are
listed at the end with the reason.

Ordering is by urgency: priority 1 issues can corrupt state or wedge a run, priority 2
issues let an unsound plan reach execution.

---

## Priority 1 — correctness and durability

### 1. Worker exceptions leave prepared runs unreconciled

**Confirmed.** `_prepare` claims the task by writing `task["status"] = "running"` or
`"reviewing"` (`writ/runner.py:1069-1071`) and appends the run id. `runner.execute` is
then called at `writ/orchestrator.py:694` with no `try/finally` that restores state:
both handlers (`writ/orchestrator.py:701-703`) return an `Outcome` carrying an error
string and never touch the task record.

A provider crash, subprocess failure, or any unexpected exception therefore strands the
task mid-status with no durable outcome. The run looks active, the process is gone, and
recovery depends on a later `reap`.

Possible stuck states:

- task remains `running` or `reviewing`;
- run remains active;
- process has already disappeared.

**Recommended solution.** Guarantee reconciliation for every prepared run:

- wrap the worker lifecycle in `try/finally`;
- if execution raises, mark the run failed or interrupted;
- restore implementation tasks to `planned` and review tasks to `awaiting-review`;
- persist the exception type, message, and traceback location;
- refresh milestone state before returning.

This is the failure that silently wedges a project, and the fix is contained.

---

### 2. State replacement lacks crash durability

**Confirmed.** `_write` (`writ/state.py:170-178`) calls `tmp.write_text(...)` then
`tmp.replace(target)`. `os.replace` is atomic against concurrent *readers*, not against
power loss: neither the file nor the containing directory is flushed, so a machine crash
can lose the most recent committed state.

**Recommended solution.** For durable state transitions:

1. Write the temporary file.
2. Flush and `fsync` the temporary file.
3. Atomically replace the target.
4. `fsync` the containing directory where supported.
5. Clean up orphaned temporary files during startup.

Make this the default for state and critical run metadata, with an explicitly documented
performance option if needed. Roughly four lines, and it is the durability floor every
other guarantee sits on.

---

### 3. Process identity is a bare PID, across locks, sessions, and cancellation

Three separate symptoms, one root cause and one fix. Do them as a single piece of work.

**3a. Age-only stale-lock breaking — confirmed.** `_lock_is_stale`
(`writ/state.py:213-218`) compares `st_mtime` against a flat `LOCK_STALE_SECONDS = 60.0`
(`writ/state.py:36`), and `_release` (`writ/state.py:221`) unlinks the lock regardless of
owner. The mtime is written once at acquire and never refreshed, so any transaction
holding the lock longer than 60s — a slow agent write, an OS-paused process, a slow
filesystem — has its lock stolen while still inside the critical section. That is
concurrent writes to `state.json` with no detection.

**3b. PID-only process identity — confirmed.** Ownership is recorded as a bare pid
(`writ/runner.py:1058` `owner_pid`, `writ/orchestrator.py:215`) and checked with
`process_alive` (`writ/runner.py:1587`), which is a `kill(pid, 0)` liveness probe. PIDs
are reused after a process exits, so an unrelated process can be mistaken for the
original owner: stale runs treated as active, recovery skipped, or cancellation
(`os.killpg` at `writ/runner.py:1579`) targeting the wrong process group.

**3c. Session claiming is not atomic — confirmed.** `claim_session`
(`writ/orchestrator.py:197-215`) reads the existing pid, checks `process_alive`, then
writes the replacement. Two processes can pass the check before either writes — a
textbook TOCTOU on the file that is supposed to prevent double dispatch.

**Recommended solution.** Record and verify a composite identity everywhere:

```json
{"pid": 4711, "start_time": 1758500000.0, "hostname": "...", "token": "..."}
```

- treat a process as the recorded owner only when the full identity matches;
- refresh a heartbeat while a lock is held when long operations are possible;
- break a lock only when its owner is confirmed dead or its token is demonstrably
  orphaned;
- use OS-level advisory locks where available;
- keep the age timeout only as last-resort recovery, with an explicit warning;
- create the session file with `O_CREAT | O_EXCL` or store ownership inside the state
  transaction, and release it only when the stored token belongs to the releasing
  process.

Ranking note: 3a outranks 3b and 3c because a stolen state lock corrupts data, whereas a
bad session claim mostly produces a confusing error.

---

### 4. Transient infrastructure failures are not separated from task failures

**Confirmed.** The error path at `writ/orchestrator.py:701-703` flattens a provider
timeout, a subprocess-spawn failure, a state-lock timeout, and a genuine reviewer
rejection into the same `Outcome.error` string. Tasks burn rework attempts on transient
failures and are then reported as having failed on technical merit.

The task rework budget is not an appropriate mechanism for infrastructure retries, and
this corrupts the signal the whole review loop depends on.

**Recommended solution.** Classify failures into separate categories:

- retryable infrastructure failure;
- non-retryable task failure;
- reviewer rejection;
- human-blocked decision.

For retryable infrastructure failures add a separate bounded retry budget, exponential
backoff with jitter, durable retry timestamps, a recorded classification and reason, and
idempotency keys. Report clearly that the task was not rejected on technical merit. Do
not consume task rework attempts.

---

## Priority 2 — plan integrity

### 5. Baseline results are agent-reported

**Confirmed.** Validation checks only that `baseline_result` is an object and that
`status` is one of pass/fail/unknown (`writ/analysis.py:546-550`). No baseline command is
ever executed; `baseline_commands` is carried as data
(`writ/analysis.py:561`, `writ/analysis.py:1188-1190`).

A plan can therefore claim a green test suite that was never run, and every downstream
regression judgement inherits that error.

**Recommended solution.** Add a deterministic baseline runner that:

- executes declared baseline commands from the repository root;
- records the exact command, exit code, duration, and timestamp;
- stores stdout and stderr as artifacts;
- records tool and environment versions;
- distinguishes pre-existing failures from new failures;
- compares observed results with agent-reported results;
- blocks approval when the baseline is unknown unless explicitly overridden.

Commands should run under an explicit safety policy or allowlist.

---

### 6. Planning quality gates are optional

**Confirmed.** `approve` (`writ/plans.py:492-530`) checks exactly one thing: that no open
finding has `severity == "error"`. There is no check that critics ran, that they reviewed
the *current* plan revision, or that required planning artifacts exist.

The sharpest edge is revision staleness — a critic approval carried over from an earlier
revision is worse than no approval, because it reads as review that did not happen.

This item absorbs two related findings from the original review, since both reduce to
"make a critic mandatory and give it a better checklist":

- *Requirements completeness.* Later stages can detect requirements dropped from the
  inventory but not an obligation the requirements agent never extracted. Require the
  coverage critic to reread the design document, compare extracted requirements against
  headings, normative language, constraints, interfaces, and non-functional
  requirements, block when a design obligation has no inventory entry, and preserve the
  source heading and quote for every requirement.
- *Semantic dependency correctness.* The graph validator catches cycles and unknown
  references, but an acyclic graph can still order tasks impossibly. Require the
  dependency critic for every executable plan, covering missing contract dependencies,
  unnecessary serialization edges, shared interfaces and schemas, parallel branches that
  cannot safely coexist, and missing integration or join tasks. Record the reason for
  every nontrivial dependency.

**Recommended solution.** Make these prerequisites mandatory before execution:

1. Requirements, repository inventory, and verification artifacts exist.
2. Deterministic plan validation has completed.
3. All required critics have reviewed the current plan revision.
4. No blocking findings remain open.
5. Every `must` requirement is covered, evidenced as existing, or explicitly approved as
   out of scope or deferred.
6. No unresolved mandatory ambiguity remains without an explicit decision.
7. Milestone and final integration gates are installed.
8. The plan revision has not changed since the latest checks and critic reviews.

Retain forced approval as an escape hatch. The existing `--force --reason` handling
(`writ/plans.py:520-528`) is already the right shape: keep requiring a reason and record
that the normal quality gate was bypassed.

---

### 7. Plan repair has no bounded pre-execution loop — **implemented**

**The diagnosis held.** A complete repair loop existed for execution-time failures
and had no pre-execution counterpart: `repair.open_request` took `gate_id` as a
required argument, and gates do not exist until execution starts. During planning a
finding had two ends — hand disposal via `plans.dispose`, or `writ approve --force`
sweeping the lot through `accept_all`. Neither is a repair.

**What was built.** `writ adjudicate` (`writ/adjudicate.py`, `commands.cmd_adjudicate`),
the pre-execution half of the same loop:

```text
check + critics → findings → adjudicator proposes a patch → writ validates it
→ apply → re-check → re-run the critics → repeat, bounded
```

It reuses the gate machinery rather than duplicating it, which was the point:

- **Requests are scoped, not gate-keyed.** `repair.open_request` takes `gate_id` as
  optional; `repair.PLAN_SCOPE`, `scope_of`, `is_plan_request` and `plan_request`
  read the scope. `validate`, `apply_patch` and the round bounds are shared
  unchanged.
- **`revise_tasks` is new, and only valid before execution.** Most pre-execution
  findings are about a task that is already in the plan — a vague criterion is fixed
  by writing a better one, not by adding a task to check up on it. A gate repair
  cannot do this (a contract whose bar somebody already met must not change), so
  `_validate_revisions` refuses a revision on a gate-scoped request, on a task that
  is not `planned`, and on a gate.
- **The bar cannot be lowered.** A revision that drops a requirement the task
  covered, reduces its criteria count, or invents a requirement is refused
  (`dropped-requirement`, `weakened-acceptance`, `unknown-requirement`). This is the
  invariant that stops the loop being a way to make a bad plan pass.
- **Bounded four ways.** Rounds per plan (`repair.plan_exhausted`, `--max-rounds`,
  default 2); refusals per request (`MAX_PATCH_ATTEMPTS`) — a refusal does not spend
  a round, and the next attempt is told which invariant it broke; repeat findings
  (`repair.plan_repeat_findings`); and a question, which stops the loop and reaches
  the decision log.
- **The scheduler stays out of it.** `orchestrator.next_job` skips plan-scoped
  requests, so one is never dispatched against a gate that does not exist.

**A real hole this surfaced.** `plans.record_findings` only reopened a returning
finding if its disposition was `resolved`. The adjudicator sets `accepted` — so an
agent could close its own objection and a re-check that still reported it would not
reopen it, which is exactly the laundering the loop had to be unable to do. Fixed by
`plans._reopens`: an *agent's* acceptance is a claim a later check overturns, a
*person's* is a judgement that stands. Two tests in `tests/test_plans.py` pin both
halves.

The staleness signal is now consumed as well: `critics.unreviewed` already reported
which critics had not read the current revision, and the loop re-runs them after each
applied patch, so a finding closes on a re-review rather than on the patch's word.

**Coverage.** 31 tests in `tests/test_adjudicate.py` — every validation rule, the
refuse-then-succeed path, all four stopping conditions, the critic re-read, and the
scheduler separation. Suite at 838.

**Still open, deliberately.** The loop is opt-in: `writ plan` does not run it, for the
same reason it does not run the critics unasked — it costs an agent run plus a critic
pass per round. Wiring it into `writ plan` behind a flag is the natural follow-on, and
it depends on issue 6: while critic review stays optional, a plan often reaches
approval with nothing recorded to adjudicate.

---

### 8. Unknown paths are advisory

**Confirmed.** A fence path that does not exist produces `severity="note"`
(`writ/plancheck.py:646-655`). A typo is therefore indistinguishable from a legitimately
planned new file.

**Recommended solution.** Distinguish explicitly between an existing path, a declared new
path, and an unknown or unjustified path. Add a `creates` field or equivalent declaration
for new files and directories, and make undeclared unknown paths blocking findings.

Small change, and it pairs naturally with issue 6.

---

### 9. Shared working-tree parallelism remains unsafe

**Confirmed as described.** The planner detects overlapping declared ownership, but agents
can still modify undeclared files, generated files, lockfiles, or shared configuration. A
shared working tree stays vulnerable to interference even when declared path fences do not
overlap.

Urgent in consequence but slow in remedy: this is an architectural change, not a patch.
Schedule it deliberately rather than squeezing it in alongside the priority 1 fixes.

**Recommended solution.** Prefer isolated worktrees or branches for parallel
implementation tasks:

```text
task worktree → implementation → review → integration worktree → merge/check
```

If shared-tree execution remains supported:

- snapshot tracked and untracked files before and after each task;
- reject changes outside the declared scope;
- detect generated and dependency-file changes;
- serialize tasks touching global files;
- require a controlled integration task after parallel branches.

---

## Testing

Failure-injection coverage is the verification for issues 1 through 4, not a separate work
item. Write each test alongside the fix it covers, and have every test assert both the
persisted run record and the resulting task state:

- PID reuse and mismatched process identity;
- stale and live lock handling;
- concurrent session claims;
- state write failure before and after replacement;
- subprocess spawn failure;
- timeout and process-group termination;
- worker exceptions after run preparation;
- missing, malformed, or misplaced verdicts;
- reviewer interruption and supervisor disappearance;
- transient provider and filesystem failures;
- resume after every interrupted lifecycle stage.

---

## Removed from this review

- **Candidate-plan comparison.** Already resolved, and the original review missed it.
  `docs/workflow-review-and-feedback-driven-replanning.md` marks stage 4 as deliberately
  removed in its pipeline diagram and devotes a section ("Stage 4 was removed rather than
  built") to the reasoning: with the requirement inventory fixed first, a rival plan would
  need an unreviewed third agent to adjudicate between candidates. The documentation does
  not overclaim, so there is nothing to align and nothing to build.
- **Verification ID chain.** An appealing schema, but a large refactor buying traceability
  no observed failure yet demands. Revisit if reconciliation bugs appear.
- **Acceptance command feasibility phase.** Most acceptance commands legitimately cannot
  run before implementation, so the phase mostly emits "the task that creates this has
  not run yet". Low signal for the machinery required.
- **Required tooling not guaranteed.** Misdiagnosed. The review reported `pytest` as
  unavailable, but `.venv/bin/python` has pytest 9.1.1 and it is on `PATH`; the venv was
  simply not activated. Declaring dev dependencies and a bootstrap command remains good
  hygiene, but it is documentation work, not a workflow defect.

---

## Note on file concentration

`writ/state.py` is 234 lines and carries both issue 2 and issue 3a. It is the
highest-leverage file in the repository right now.
