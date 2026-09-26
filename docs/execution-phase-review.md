# Execution Phase Review

This document reviews `writ run`, the execution phase: implement → review → rework, then
gate → repair → gate. It asks three questions:

1. Does the workflow drive every task to completion, or can work get stuck?
2. Is agent context and stored data managed efficiently?
3. Does the review loop catch bad work?

The review was done on 2026-09-25, against the `planning-redesign` branch after commit
f61f613. Each finding was checked against the code, and the robustness findings were
reproduced with probe scripts running the real orchestrator.

Measurements come from a complete run in `~/projects/mmm/.writ`. That project was 10
tasks and 19 runs, and every task completed.

Each finding has a **status**:

- **Fixed** means fixed in this change, with tests.
- **Partly fixed** means some of it is fixed and the rest is described.
- **Deferred** means it is left for later, with the reason.

---

## Summary

The workflow's structure is sound:

- Session claiming, reconcile-on-raise, resume, bounded rework and bounded gate repair are
  all correct.
- The gate → repair → gate loop cannot run forever. It has three independent bounds.

The weak points were at the edges, where a run ends without a verdict:

- Any non-zero exit that `failures.from_run` did not recognise set the task to `failed`,
  and the scheduler never selects a failed task again. One HTTP 529 on a gate stopped the
  whole project.
- Running `writ run` again then said nothing could start and **exited 0**.
- A reviewer that crashed failed the implementation it was supposed to review.

Gates also passed work they had asked to repair:

- A `needs-repair` verdict whose findings were marked `"high"` or `"major"` counted as
  advisory only.
- A `needs-repair` verdict with only failed criteria and no findings also counted as
  passed.
- In both cases the gate was marked `completed`.

On context, prompts carried the wrong things:

- The final gate's prompt was 61% decision text, pasted in full.
- On a plan with milestones, that same gate saw no decisions at all.
- Implementers saw no decisions from the work they build on.
- No prompt named the project's test command, so every agent had to find it again.

---

## 1. Robustness: do all tasks finish?

### H1. An unjudged non-zero exit failed the task permanently — **fixed**

**Before.** `runner._finish`, in its no-verdict branch, did this:

```python
elif code != 0:
    task["status"] = "failed"
```

That applied to every role. `failures.from_run` recognised only exit 124 (timeout) and
total silence. `failed` is never selected again, because `model.effective_status` only
promotes `planned`.

A probe reproduced it. The agent printed `error: upstream provider returned 529
overloaded` and exited 1 on the gate. All four tasks had completed, but the three
milestone gates went to `failed`. A second `writ run` printed `nothing can start` and
exited 0.

**Now.**

- **Provider errors are recognised.** `runner.provider_error` (`writ/runner.py:1286`)
  scans the last 8 KB of stderr and stdout for rate-limit, overload, quota and 5xx
  markers, and for connection-reset markers. A match is recorded as `run["provider_error"]`.
  `failures.from_run` (`writ/failures.py:320`) classifies it as retryable
  `INFRASTRUCTURE`, so it uses the existing backoff budget.
- **A missing agent is recognised.** Exit 127 is classified as `UNAVAILABLE`.
- **Unrecognised failures depend on the role.** When `from_run` still cannot classify the
  exit, no role ends at `failed`:

  | Role        | Where the task goes |
  |-------------|---------------------|
  | reviewer    | back to `awaiting-review` (see H2) |
  | gate        | back to `planned` |
  | implementer | sent back through the rework budget by `verdict.send_back_unfinished` (`writ/verdict.py:1431`). The next attempt is told the previous one exited N without a usable verdict. |

  The task reaches `failed` only when the rework budget runs out, the same as a rejection.

### H2. A reviewer's crash failed the implementation — **fixed**

This had the same cause as H1. A reviewer that exited non-zero set the *implementation*
to `failed`, even though no one had judged it.

Now, a reviewer that ends without a verdict leaves the task at `awaiting-review`, and the
failure is classified as retryable `INFRASTRUCTURE`. An unjudged review is a machinery
failure, not a judgement of the work. The retry happens inside the same session.

Test: `tests/test_run.py`, the flaky-reviewer test.

### H3. `writ run` exited 0 with the project unfinished — **fixed**

**Before.** The exit code checked only `session.failed` and `session.errors`.

- `session.held` was written but never read.
- Tasks blocked on infrastructure were ignored.
- The probes exited 0 with three gates held `needs-decision`, and again with a task
  stranded at `awaiting-review`.

**Now.** `orchestrator.unfinished` (`writ/orchestrator.py:1507`) lists why the run ended
early. `writ run` exits 1 if any reason is listed. Text mode prints each reason as
`unfinished: …`, and JSON mode includes an `"unfinished"` key. The reasons are:

- Tasks that ran out of infrastructure retries.
- Tasks started in this session and left neither completed, failed nor blocked, unless
  the run was stopped or aborted.
- Held gates.
- When there is no `--max-tasks` budget: incomplete tasks that nothing in this run could
  start.

`--max-tasks` stops early on purpose, so that case alone is not reported.

### M1. The infrastructure retry budget covered the task's whole life — **fixed**

**Before.** `task["infrastructure"]["attempts"]` grew forever and was shared by every
role.

A probe with the default budget of 2:

- Dispatch timed out twice, was retried, and succeeded.
- The *reviewer's* first timeout was then counted as attempt 3, so it was not retried.
- The task was stranded at `awaiting-review`.

**Now.**

- Each attempt records its role and round: the rework round for implementers and
  reviewers, the repair round for gates.
- `runner.infrastructure_attempts(task, role=…)` (`writ/runner.py:2405`) counts only the
  current role and round. The orchestrator and `reconcile` both use it.
- The idempotency key includes the round.
- Entries written before this change count as round 0.

### M2. `infrastructure.exhausted` was never read — **partly fixed**

The flag is now cleared whenever a new attempt is recorded, and M1 means a new round
starts with a fresh allowance. An exhausted task is still selected again by a later
`writ run`. That is intended, because a new session is how an operator retries once the
provider is back.

What remains is that the flag only tells an operator what happened. The scheduler never
reads it.

### M3. A reviewer that exited 0 without a verdict silently ended the task's session — **fixed**

This is now retryable `INFRASTRUCTURE` (see H2), and if it is still unjudged when the
session ends, the run exits 1 (see H3).

A reviewer that *did* write a verdict file that could not be parsed is different. That
counts as the agent's own error, not an infrastructure failure. The task returns to
`awaiting-review` and the run exits 1.

### M4. A run whose owner is on another host is never reaped — **deferred**

`procs.confirmed_dead` refuses to declare dead a process recorded on a different host.
That is correct for shared checkouts, but if the hostname changes (container, renamed
machine) the task stays at `running` forever.

H3 now reports such a task when this session started it. It is not reported across
sessions.

**Recommended fix.** List these runs in the summary with the recorded host, and point at
`writ cancel <run>`. This needs a decision on how long a foreign-host run may sit before
it is reported, so it was left out.

### M5. Stall reports only listed `planned` dependents — **fixed**

`_stalled` and `_behind` (`writ/orchestrator.py:1391`, `:1487`) now list every dependent
that is not completed or failed. A dependent parked at `awaiting-review` behind a failure
now appears.

### L1. `repair-exhausted` advice is dead code — **deferred**

`_apply_gate` returns `failed` for a gate that is out of repair rounds. `held_gates`
requires `blocked`, so the "out of repair rounds" advice never prints; the gate shows as
generic failed work.

Changing it to `blocked` changes what `writ show`, the dashboard and resume do with an
exhausted gate. That behaviour change deserves its own review.

### L2. A `repair-refused` hold is not added to `session.held` — **fixed indirectly**

`orchestrator.unfinished` reads held gates from the stored state, not from
`session.held`. A refused repair therefore makes the run exit 1.

### L3. Signal handling does nothing off the main thread — **deferred**

This is latent: only `cmd_run` calls `orchestrator.run`, and it does so on the main
thread.

### L4. Repair bounds are not configurable — **deferred**

The defaults are 2 rounds, 2 repeats of the same finding, and 2 patch attempts. They are
reasonable, and no run has hit them.

---

## 2. Review-loop effectiveness

### F1. A gate passed work it had asked to repair — **fixed**

**Before.**

- `_gate_findings` mapped only `"error"` and `"critical"` to blocking. Any other value,
  including `"high"`, `"major"` and `"blocker"`, silently became advisory.
- `_apply_gate` blocks only on blocking findings. So `needs-repair` with only
  high-severity findings, or with failed criteria and no findings, returned `completed`.

**Now** (`writ/verdict.py`):

- `SEVERITY_ALIASES` (line 53) maps the common spellings of each side.
- **Unknown severities are treated as blocking.** A gate cannot pass work by using a word
  writ does not know.
- A `needs-repair` verdict with unmet criteria and no blocking finding gets one finding
  added per unmet criterion (`_unmet_findings`, line 679). The same path already
  downgraded a `pass` that had unmet criteria.

### F2. Nothing named the test command — **fixed** (see C4)

writ still never runs the tests itself. Every judgement is an agent's. What changed is
that every agent is now told which command decides "green".

Running the command in writ itself is the open item #5 in
`workflow-issues-and-recommended-solutions.md`. It was not attempted here.

### F3. Verdict parsing was stricter than agents write — **partly fixed**

Common spellings of the enums are now accepted (`_ALIASES`, `writ/verdict.py:889`):

| Field             | Spellings accepted |
|-------------------|--------------------|
| criterion status  | `pass`/`ok`/`met`/`done` → `passed`; `fail`/`unmet` → `failed`; `unchecked`/`skipped` → `pending` |
| outcome           | `done` → `complete` |
| review decision   | `approved` → `accept` |
| gate decision     | the common spellings of each value |

Previously each of these made the whole verdict unusable.

**Not changed:** the minimum length for a decision's text, and the non-empty
`blocked_on` rule. Both reject verdicts that really are uninformative.

### F3b. An implementer reporting `incomplete` failed the task outright — **fixed**

The agent had said, truthfully, that it ran out of room. Now the task is sent back
through the rework budget. The next attempt's prompt opens with **"A PREVIOUS ATTEMPT AT
THIS TASK DID NOT FINISH"** and includes the previous summary, the unmet criteria and the
notes (`runner._unfinished_section`, `writ/runner.py:275`). `writ show` labels the record
"returned unfinished".

### F4. Rework and review use the same model as implementation — **deferred**

A second opinion from the same model finds fewer problems. This is a configuration
decision (`--reviewer-model` exists but defaults to the implementer's model). Changing the
default needs a view on cost.

### F5. Repair tasks can lack a fence or overlap completed work — **deferred**

Repair patches are already validated to be add-only. Checking their fence against
completed tasks' files is worthwhile but separate work.

### F6. A malformed gate verdict loses the attempt record — **deferred**

This is low impact now that an unjudged gate returns to `planned` and is retried.

---

## 3. Context and data efficiency

Measured on the mmm run:

| Item | Measurement |
|------|-------------|
| Implementer prompts | 10–16 KB |
| G-FINAL gate prompt | 26.5 KB, of which 16 KB (61%) was decision text |
| Decisions | 46 in total, 15.5 KB, median 313 characters, all still `proposed` |
| state.json | 288 KB |

### C1. The final gate saw no decisions on a milestone plan — **fixed**

`_agreed_decisions` matched decisions against the gate's direct `depends_on`. A final
gate depends on *milestone gates*, and decisions are recorded against tasks, so the
result was empty. That happened on exactly the gate that checks the decisions agree with
each other.

On mmm the final gate depended on FT-001…FT-009 directly, so it received every decision
in full instead.

**Now.**

- `runner._work_under` (`writ/runner.py:636`) walks through gates down to their tasks.
- Rejected and superseded decisions are dropped.
- Each decision is cut to 200 characters (`DECISION_LIMIT`).
- A pointer to `.writ/decisions` is added for the full text.

On mmm this brings the decision block from about 16 KB to at most about 9 KB. The gate
only needs to see whether two decisions disagree, not their whole reasoning.

### C2. Implementers saw no upstream decisions — **fixed**

An implementer building on FT-001 could not see what FT-001 decided: the file format,
the error convention, the naming. It had to reconstruct those from the code, or
contradict them.

`build_prompt` now adds "Decisions the work you build on already made — build to these",
drawn from the task's upstream work, walking through gates the same way and with the
same abridging.

### C3. Decisions are never pruned or confirmed — **deferred**

All 46 of mmm's decisions are still `proposed`. Nothing moves them to `accepted`, and
nothing retires the ones later work superseded. C1's filtering and abridging limit the
cost in prompts. A step that confirms decisions is a workflow change of its own.

### C4. No prompt named the test command — **fixed**

Every mmm agent rediscovered `PYTHONPATH=src python3 -m unittest discover -s src`,
spending turns each time, and nothing guaranteed they all ran the same thing.

`runner.verify_commands` (`writ/runner.py:683`) picks one source, in this order:

1. `writ run --verify CMD`
2. the `run.verify` setting in `.writ/config.yaml`
3. the baseline commands found during planning (`plan.pipeline.baseline.commands`)

The result is named in the implementer's, reviewer's and gate's prompts. On a
greenfield plan the baseline is empty, which was the case on mmm. `run.verify` is the way
to supply the command once the project has one.

### C5. The reviewer is told to read "the diff" but given no base — **deferred**

While parallel agents share one working tree, a list of changed files per task cannot be
trusted, because any file may have been changed by a sibling. This is open item #9 in
`workflow-issues-and-recommended-solutions.md`, and it should be solved there, with a
worktree per task, rather than guessed at here.

### C6. Smaller items — **deferred**

- **Upstream summaries** are cut at 160 characters. This is tight but acceptable: the
  full summaries are available through `writ show`.
- **The design excerpt** repeats the requirement details, about 2.5 KB per prompt. Worth
  removing, but the excerpt is the source and the details are derived from it, so
  deciding which one to drop needs care.
- **`DECISION_RULES`** is sent to the reviewer, which records no decisions. About 1 KB.
- **State I/O** is about 12 loads and 6 rewrites of `state.json` per task. That is fine at
  300 KB, and would become noticeable around 3 MB.

---

## 4. Already handled well

These were checked and should be left alone:

- **Session claiming.** `O_CREAT|O_EXCL` plus a staged link, and release scoped to the
  claim's token.
- **Worker failures.** `_execute` catches `BaseException` and reconciles. `cancel` claims
  the run before killing it, and `_terminate` refuses to signal a recycled pid.
- **Resume.** `reap` plus `reconcile` cover every prepared run, and `INTERRUPTED_STATUS`
  restores `running` → `planned` and `reviewing` → `awaiting-review`.
- **Rework.** It is bounded and durable, and its findings are fed into the next prompt.
- **Gate repair.** It is bounded three ways: rounds, repeated findings, and refused
  patches. `apply_patch` keeps the graph acyclic.
- **Scheduling.**
  - Retries back off with jitter, and a task waiting to retry is not selected.
  - Reviews run first and repairs run before new work.
  - Independent branches keep running after a failure.
- **Prompt content.**
  - Design docs are referenced by path, not pasted.
  - State is never pasted into a prompt.
  - A re-review is told what it objected to last time.

---

## 5. Tests

The existing tests that encoded the old behaviour were updated:

- a crashed implementer now goes to `planned` with a rework reason, not `failed`;
- a silent gate or reviewer now makes the run exit 1.

New tests cover:

- gate severity aliases, and unknown severities treated as blocking;
- failed criteria turned into findings;
- the enum spellings accepted;
- an implementer reporting `incomplete` sent back, and failing once the budget runs out;
- a provider 529 classified as retryable;
- a crashed and then a flaky reviewer;
- the infrastructure budget counted per role and round;
- the exit code for a stranded or budget-exhausted run;
- gate decisions reached through milestone gates, abridged, with rejected ones left out;
- upstream decisions shown to implementers;
- the verify command, from each of its three sources, in every execution prompt.
