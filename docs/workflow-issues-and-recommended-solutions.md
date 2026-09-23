# Workflow Issues and Recommended Solutions

Triaged against the implementation on 2026-09-22. Each issue below was confirmed by
reading the cited code. Items from the original review that did not survive triage are
listed at the end with the reason.

Ordering is by urgency: priority 1 issues can corrupt state or wedge a run, priority 2
issues let an unsound plan reach execution.

---

## Priority 1 — correctness and durability

### 1. Worker exceptions leave prepared runs unreconciled — **implemented**

**The diagnosis held.** `_prepare` claimed the task and `runner.execute` was called
with no `try/finally`; both handlers returned an `Outcome` carrying an error string
and never touched the task record.

**What was built.** `runner.reconcile` (`writ/runner.py`), called from
`orchestrator._stranded` on the way out of every worker that raises, and from
`runner.execute_guarded` for the single-shot entry points — `writ dispatch`,
`writ review` and the detached supervisor had the same exposure.

The invariant is now: **for every prepared run, exactly one of the worker's own
recording or `reconcile` happens.**

- `except BaseException`, not `Exception`. A `KeyboardInterrupt` delivered to a
  worker thread strands a run identically, and leaving the store inconsistent is
  not better for having been caused by a signal.
- The run is marked `interrupted` when the failure is retryable and `failed` when
  it is not — the distinction `reap` already drew, so a provider timeout does not
  read forever after as a run that failed on its merits.
- Implementation tasks return to `planned`, review tasks to `awaiting-review`
  (`INTERRUPTED_STATUS`, reused rather than restated).
- A repair run reopens its request. `prepare` moves it to `planning`, and a
  request stuck there is one the scheduler will never pick up again — the same
  treatment a refused patch already gets.
- The exception type, message and the innermost three traceback frames are
  persisted to `run["failure"]`, and milestones are refreshed before returning.
- **Idempotent.** A worker can raise *after* `_finish` committed, in which case
  the recorded outcome is the true one and only the classification is attached.
  `Reconciliation.settled` says which happened.
- Reconciliation can itself fail — if the state lock is what broke, writing the
  reconciliation needs that lock. That is reported in the outcome rather than
  raised, because losing the other agents in flight would turn one stranded run
  into a lost session. `reap` remains the backstop.

---

### 2. State replacement lacks crash durability — **implemented**

**The diagnosis held.** `tmp.write_text` then `tmp.replace` is atomic against
concurrent readers and says nothing about power loss.

**What was built.** `state._write` now does the four steps in order: write the
temporary file, `fsync` it, `os.replace` (the commit point), `fsync` the
containing directory. `_fsync_dir` is best effort by design — not every
filesystem allows opening a directory for it, and a platform that refuses leaves
the rename exactly as durable as it was before, with the file's own contents
still flushed.

- A write that fails before the replace leaves the previous document intact and
  removes its own temporary file. Better an old state than a truncated one.
- `state.sweep_temporaries` clears debris from a crash, at `initialize` and inside
  `reap` — the one place writ already runs to clean up after a death. A temporary
  whose writing process is still alive is left alone: the pid is in the filename
  precisely so a concurrent writer's work in progress can be told from a dead
  one's leavings.
- `WRIT_FSYNC=0` opts out, documented as making the store only as durable as the
  page cache.

---

### 3. Process identity is a bare PID — **implemented**

**The diagnosis held**, in all three parts. One new module, `writ/procs.py`, and
one composite identity recorded everywhere ownership is: `{pid, start_time, host,
token}`.

The probe is two-stage, because it matters that it is cheap: `os.kill(pid, 0)` is
a syscall, while reading a start time costs a `/proc` read or a `ps` fork, so the
expensive half only runs for pids that are alive — and start times are cached, as
this is now on the path of every state transaction.

Three predicates, and the asymmetry between them is the design:

- `alive` — is this still the process that was recorded. A record from another
  host is *alive*, because this machine has no standing to say otherwise.
- `confirmed_dead` — can this machine **prove** the owner is gone. Breaking a
  lock, reaping a run and stealing a session are all destructive, so they need
  proof rather than a failed probe.
- `safe_to_signal` — the pid to signal, or None. Guards `killpg`, the one
  irreversible thing writ does to something outside itself, and also refuses a pid
  that resolves to writ's own process group.

Where a start time cannot be read, identity degrades to the pid alone — no worse
than before. A bare pid from an older writ is honoured, so an in-flight project
keeps running across the upgrade.

**3a, the ranked-highest one.** `state._lock` now uses `fcntl.flock` where the
platform has it, which makes the hard half the kernel's: a lock held by a process
that dies is released when its descriptors close, so there is no stale lock to
guess about and **no timeout at which a held lock can be taken from a holder that
is merely slow**. The lock file is deliberately never unlinked — unlinking is what
would reintroduce the race, since the next writer would create a fresh inode and
lock that instead.

The no-`flock` fallback keeps create-exclusive locking, with three changes that
make the age timeout a last resort rather than the first rule: a lock is broken
immediately when its owner is *proven* gone; a holder heartbeats its own mtime
while it works, so age measures abandonment rather than duration; and release
unlinks only a lock this process still owns.

**3b.** `runner.run_owner` is the precedence chain — supervisor, then the agent's
own process, then the claiming process — and `run_alive` / `run_abandoned` are the
two questions asked of it. The precedence is load-bearing: asking whether
*anything* on the list is running would report every crashed agent as working for
as long as the terminal that started it stayed open.

**3c.** `claim_session` creates the claim with `O_EXCL`, so the kernel picks the
winner. Two subtleties the tests found:

- creating the file and *then* writing it leaves a window in which the claim
  exists but is empty, and a rival inside that window reads no owner — which is
  indistinguishable from a claim whose writer died. Fixed by staging the content
  and `os.link`-ing it into place, so the name appears already holding an
  identity.
- `release_session` releases only its own claim. After a `--force` start, an
  unconditional unlink would delete the *new* session's file.

---

### 4. Transient infrastructure failures are not separated from task failures — **implemented**

**The diagnosis held.** One `Outcome.error` string carried a provider timeout, a
spawn failure, a lock timeout and a genuine rejection alike.

**What was built.** `writ/failures.py`: six categories (`infrastructure`,
`unavailable`, `task`, `rejection`, `blocked`, `internal`), a `retryable` flag, and
an `on_merit` predicate — whether the failure is a statement about the *work* —
which is the one the session summary reads.

`classify` is deliberately conservative: an exception it does not recognise is
`internal` and never retryable, because spending a budget on an unknown failure
mode is how a crash loop gets mistaken for patience. `unavailable` exists because
not every infrastructure failure is worth retrying — a missing agent binary is
missing on the next attempt too.

The retry budget is separate from rework in every respect that matters:

- **Its own allowance.** `--max-infra-retries`, default 2, per task rather than
  per session.
- **Durable.** Attempts are recorded on the task with a timestamp, the run, the
  category, the reason and an idempotency key, so a session killed mid-backoff and
  resumed does not hand out a fresh allowance. The scheduler reads the count back
  from the store rather than from its own memory.
- **Backoff with jitter**, capped, applied as a reduction from the nominal delay
  so the schedule stays testable.
- **Never charged to rework.** `reconcile` writes the infrastructure record and
  deliberately does not call `open_rework`.
- **Reported as what it is.** `session.infra_retries` and `session.infra_blocked`
  are kept apart from `failed`, `_record` returns early for anything not
  `on_merit`, and a task that exhausts the budget gets an evidence line saying in
  words that it was not rejected on technical merit. `writ show <run>` names the
  category.

A prepare-time failure is classified too: a state-lock timeout there says nothing
about the task, and the old code abandoned the task for the rest of the session.

**The two failures that never raise.** `classify` only ever sees an exception, and
the two commonest infrastructure failures do not produce one. A hung agent is
killed by writ itself and returns 124. An agent that cannot reach a model — wrong
model id, missing credentials, exhausted quota — exits 0 having printed nothing.
Both arrived as an ordinary unjudged run and landed the task at `failed`, which is
the word for work a reviewer read and rejected.

So `failures.from_run` classifies a run that *finished* without a usable verdict:
124 is retryable `infrastructure`, an empty transcript is `unavailable`, and
everything else is `None`. `runner._finish` calls it before deciding where the task
goes, and a classified failure returns the task to the status `prepare` claimed it
from rather than to `failed` — `planned` for an implementation, `awaiting-review`
for a review, the same distinction `reap` draws. Under the scheduler the retry
comes out of the infrastructure budget; under a bare `dispatch` it means fixing the
timeout or the model id is the only thing left to do, instead of first having to
undo a status that says the work failed.

The classification is reported without becoming a session error: `Outcome` carries
it in `failure_reason` rather than `error`, because an outage the scheduler is
retrying should not make `writ run` exit non-zero.

---

### Testing

`tests/test_resilience.py` — 48 tests, one per injected failure, each asserting
**both** the persisted run record and the resulting task state, because a run
marked failed while its task is still `running` reads as consistent from either
side alone.

Covered: worker exceptions after preparation, on a review, and on a repair run;
reconciliation of an already-finished run; reconciliation that cannot write;
single-shot dispatch; spawn failure; fsync on commit and its opt-out; write
failure before and after the replace; orphaned temporaries; pid reuse across
reaping, cancellation and session claiming; a remote owner; a live owner; a live
lock under a zero stale timeout; a dead owner's lock; the fallback heartbeat;
release by a process that lost its lock; eight threads claiming one session;
classification of timeouts, missing agents, lock timeouts, ordinary `WritError`s,
unknown exceptions and `errno` subsets; backoff bounds and jitter; a retry that
succeeds; a budget that exhausts; retries disabled; a real timeout through writ's
own kill, on an implementation and on a review; an agent that printed nothing; a
timeout retried end to end by the scheduler; and the control — a reviewer rejection
still spends rework.

Suite at 886.

**Not covered, deliberately.** An agent that *exits* non-zero having said something
has run and reported for itself, and writ still reads its transcript and its
verdict rather than second-guessing the exit code. The classification is for the
ways a job ends without ever reporting: killed, or never started.

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

The failure-injection list this section used to hold is done, and the coverage it
asked for is written up under issue 4's own Testing heading
(`tests/test_resilience.py`).

Three items on that list were already covered elsewhere and were left where they
were rather than duplicated: missing, malformed and misplaced verdicts
(`tests/test_verdict.py`), timeout and process-group termination
(`tests/test_dispatch.py`), and resume after an interrupted lifecycle stage
(`tests/test_run.py`, which resumes a killed review and asserts the
implementation was not redone).

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

`writ/state.py` carried both issue 2 and issue 3a, which was the observation that
made it the highest-leverage file in the repository. Both are now fixed. It grew
from 234 to 516 lines doing it, and the durability and locking halves are the
obvious seam if it needs splitting later — `writ/procs.py` already took the
process-identity half out of it.
