# Workflow Issues and Recommended Solutions

## 1. Mandatory planning quality gates

### Existing issue

The staged planning pipeline, deterministic checks, and adversarial critics are implemented, but several stages remain optional. A plan can potentially proceed after deterministic validation without:

- independent critic review of the current plan revision;
- complete coverage, dependency, scope, acceptance, and feasibility review;
- resolution of blocking findings;
- confirmation that all required planning artifacts exist;
- confirmation that unresolved ambiguities have been explicitly approved.

### Recommended solution

Make the following prerequisites mandatory before execution:

1. Requirements, repository inventory, and verification artifacts exist.
2. Deterministic plan validation has completed.
3. All required critics have reviewed the current plan revision.
4. No blocking findings remain open.
5. Every `must` requirement is covered, evidenced as existing, or explicitly approved as out of scope/deferred.
6. No unresolved mandatory ambiguity remains without an explicit decision.
7. Milestone and final integration gates are installed.
8. The plan revision has not changed since the latest checks and critic reviews.

Retain forced approval as an escape hatch, but require a reason and record that the normal quality gate was bypassed.

---

## 2. Candidate-plan comparison is absent

### Existing issue

The current workflow uses requirements extraction, repository reconnaissance, verification analysis, and one synthesis plan. Independent candidate plans are not generated or compared.

This is acceptable only if the intended workflow deliberately removes that stage. The documentation and workflow diagram should not imply that candidate-plan generation exists when it does not.

### Recommended solution

Choose one of the following explicitly:

- Keep the stage removed and update all documentation to describe the actual workflow.
- Generate multiple candidate plans for high-risk or ambiguous projects only.
- Generate separate implementation-oriented and verification/integration-oriented plans, then have a synthesizer compare and adjudicate them.

Do not concatenate candidate plans. Require the synthesizer to explain which decisions were selected and why.

---

## 3. Requirements extraction can still omit design obligations

### Existing issue

Later stages can detect requirements dropped from the extracted inventory, but they cannot reliably detect an obligation that the requirements agent omitted in the first place unless a critic rereads the design document. Critic review is not necessarily mandatory.

### Recommended solution

Add an independent requirements completeness check:

- require the coverage critic to reread the design document;
- compare extracted requirements against headings, normative language, constraints, interfaces, and non-functional requirements;
- block approval when a design obligation has no inventory entry;
- preserve the source heading and relevant quote for every requirement.

---

## 4. Baseline results are agent-reported

### Existing issue

The repository inventory records baseline commands and results, but the result is supplied by the analysis agent. The system validates the result's structure, not whether the command was actually executed successfully.

A plan can therefore contain an inaccurate baseline such as a reported passing test suite that was never run.

### Recommended solution

Add a deterministic baseline runner that:

- executes declared baseline commands from the repository root;
- records the exact command, exit code, duration, and timestamp;
- stores stdout and stderr as artifacts;
- records tool and environment versions;
- distinguishes pre-existing failures from new failures;
- compares observed results with agent-reported results;
- blocks approval when the baseline is unknown unless explicitly overridden.

Commands should run under an explicit safety policy or allowlist.

---

## 5. Verification links rely too heavily on generated text

### Existing issue

The verification artifact, tasks, acceptance criteria, and gates are related mainly through generated text and indirect reconciliation. A requirement may have a verification method in an artifact without a guaranteed explicit link to a task criterion and final evidence.

### Recommended solution

Introduce stable verification IDs and enforce an explicit chain:

```text
requirement → verification method → task → acceptance evidence → milestone/final gate
```

For example:

```json
{
  "id": "VER-001",
  "requirement_id": "REQ-001",
  "kind": "test",
  "command": "pytest -q tests/test_parser.py"
}
```

Tasks should reference `verification_ids`, and gate evidence should reference the resulting verification records.

---

## 6. Plan repair is not a complete pre-execution adjudication loop

### Existing issue

The repository has repair machinery for findings and execution-time gate failures, but it does not consistently perform a bounded pre-execution loop of:

```text
critic findings → repair proposal → patch validation → apply → re-check → re-review
```

Persistent findings may instead require manual intervention or force approval.

### Recommended solution

Add a pre-execution adjudication loop:

1. Run deterministic validation and all required critics.
2. Collect open blocking findings.
3. Ask a separate adjudicator to propose a structured patch.
4. Validate the patch against the current revision.
5. Apply only valid patches.
6. Re-run deterministic checks and all critics against the new revision.
7. Stop after a bounded number of rounds.
8. Escalate repeated or unresolved findings to a human.

A repair must not be allowed to silently change requirements, remove evidence, or weaken acceptance criteria.

---

## 7. Unknown paths are not strict enough

### Existing issue

Nonexistent paths in task fences generally produce advisory notes because they may be new files. This permits a typo to look like a legitimate planned path.

### Recommended solution

Distinguish explicitly between:

- an existing path;
- a declared new path;
- an unknown or unjustified path.

Add a `creates` field or equivalent declaration for new files and directories. Make undeclared unknown paths blocking findings.

---

## 8. Acceptance commands are not independently validated

### Existing issue

Acceptance criteria may name commands, test files, or tools without proving that they exist or can run in the configured repository environment.

### Recommended solution

Add a feasibility phase that validates or safely executes acceptance commands before implementation where possible. Check:

- command availability;
- working directory;
- referenced test paths;
- referenced artifact paths;
- required dependencies;
- expected exit-code conventions.

For commands that cannot run before implementation, require the plan to identify the task that creates the missing test or infrastructure.

---

## 9. Semantic dependency correctness is not deterministic

### Existing issue

The graph validator catches cycles, unknown references, and other structural errors, but not all semantically missing or unnecessary dependencies. An acyclic graph can still execute tasks in an impossible order.

### Recommended solution

Require the dependency critic for every executable plan and make it review:

- missing contract dependencies;
- unnecessary serialization edges;
- shared interfaces and schemas;
- parallel branches that cannot safely coexist;
- missing integration or join tasks.

Record the reason for every nontrivial dependency, such as a file, interface, schema, migration, or generated artifact.

---

## 10. Milestone and final gates rely too much on agent judgement

### Existing issue

Milestone and final gates exist, but their conclusions can still depend primarily on a gate agent's interpretation. A statement such as “the complete product works” is not reliable evidence without executed commands and persisted artifacts.

### Recommended solution

Make gates evidence-driven:

- run milestone-specific verification commands automatically;
- run a canonical final verification matrix at the final gate;
- persist command output, exit codes, and artifacts;
- require the gate agent to interpret the evidence rather than invent it;
- block acceptance when required evidence is absent.

---

## 11. PID-only process identity

### Existing issue

Run recovery and session ownership rely primarily on recorded PIDs. PIDs can be reused after a process exits, causing Writ to mistake an unrelated process for the original owner.

Potential consequences include:

- stale runs treated as active;
- stale sessions treated as active;
- recovery skipped incorrectly;
- cancellation targeting the wrong process.

### Recommended solution

Record and verify process identity using more than a PID:

- PID;
- process start time;
- process group ID;
- executable or command-line identity;
- unique launch token.

Only treat a process as the recorded owner when the identity matches. Use the same identity information for session claims, cancellation, and stale-run recovery.

---

## 12. Age-only stale-lock breaking

### Existing issue

The state lock can be considered stale solely because its modification time exceeds a fixed age. A live writer paused by the operating system or delayed by a slow filesystem can therefore have its lock removed while still writing.

This can cause concurrent writes and state loss.

### Recommended solution

Use ownership-aware locking:

- record PID, process start time, hostname, and a unique lock token;
- refresh a heartbeat while the lock is held when long operations are possible;
- break a lock only when its owner is confirmed dead or its token is demonstrably orphaned;
- use OS-level advisory locks where available;
- preserve the age timeout only as a last-resort recovery mechanism with an explicit warning.

---

## 13. Worker exceptions can leave prepared runs unreconciled

### Existing issue

The orchestrator catches worker exceptions and returns an outcome, but an exception after task/run preparation can leave the task or run in an active state without a durable outcome.

Possible stuck states include:

- task remains `running` or `reviewing`;
- run remains active;
- process has already disappeared;
- recovery depends on a later `reap` call.

### Recommended solution

Guarantee reconciliation for every prepared run:

- wrap the worker lifecycle in a `try/finally` block;
- if execution raises, mark the run failed or interrupted;
- restore implementation tasks to `planned` and review tasks to `awaiting-review` where appropriate;
- persist the exception type, message, and traceback location;
- refresh milestone state before returning.

Add failure-injection tests at every lifecycle boundary.

---

## 14. Session claiming is not fully atomic

### Existing issue

Session claiming checks whether an existing session is live and then writes a new session file. Two processes can pass the check concurrently before either writes the replacement.

### Recommended solution

Make session ownership atomic:

- create the session file using exclusive creation (`O_CREAT | O_EXCL`), or;
- store session ownership inside the state transaction; and
- include a unique owner token, not only a PID.

When releasing a session, remove it only if the stored token belongs to the releasing process.

---

## 15. Transient infrastructure failures are not separated from task failures

### Existing issue

Provider timeouts, quota errors, subprocess-spawn failures, temporary filesystem errors, and state-lock timeouts can be recorded similarly to genuine implementation failures or review rejections.

The task rework budget is not an appropriate mechanism for infrastructure retries.

### Recommended solution

Classify failures into separate categories:

- retryable infrastructure failure;
- non-retryable task failure;
- reviewer rejection;
- human-blocked decision.

For retryable infrastructure failures, add:

- a separate bounded retry budget;
- exponential backoff with jitter;
- durable retry timestamps;
- failure classification and reason;
- idempotency keys;
- clear reporting that the task was not rejected on technical merit.

Do not consume task rework attempts for infrastructure retries.

---

## 16. State replacement lacks full crash durability

### Existing issue

State writes use a temporary file and atomic replacement, which protects readers from partially written JSON. However, the write path does not appear to guarantee that file and directory metadata have been flushed to durable storage before returning.

A machine crash can therefore lose the most recent committed state.

### Recommended solution

For durable state transitions:

1. Write the temporary file.
2. Flush and `fsync` the temporary file.
3. Atomically replace the target.
4. `fsync` the containing directory where supported.
5. Clean up orphaned temporary files during startup.

Make this the default for state and critical run metadata, with an explicitly documented performance option if needed.

---

## 17. Shared working-tree parallelism remains unsafe

### Existing issue

The planner detects overlapping declared ownership, but agents can still modify undeclared files, generated files, lockfiles, shared configuration, or other indirect integration points.

A shared working tree therefore remains vulnerable to interference even when declared path fences do not overlap.

### Recommended solution

Prefer isolated worktrees or branches for parallel implementation tasks:

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

## 18. Required failure-injection coverage is missing

### Existing issue

Normal-path tests cover much of the workflow, but reliability depends on behavior during process, filesystem, locking, and state failures. These paths need explicit validation.

### Recommended solution

Add failure-injection tests for:

- PID reuse or mismatched process identity;
- stale and live lock handling;
- concurrent session claims;
- state write failure before replacement;
- state write failure after replacement;
- subprocess spawn failure;
- timeout and process-group termination;
- worker exceptions after run preparation;
- missing, malformed, or misplaced verdicts;
- reviewer interruption;
- supervisor disappearance;
- transient provider and filesystem failures;
- resume after every interrupted lifecycle stage.

Each test should verify both the persisted run record and the resulting task state.

---

## 19. Required tooling is not guaranteed in the execution environment

### Existing issue

The repository test suite could not be executed in the current environment because `pytest` was unavailable. This indicates that the workflow does not yet guarantee or clearly report the availability of its own validation tooling.

### Recommended solution

Document and enforce project setup requirements:

- provide a reproducible development environment;
- declare test and lint dependencies;
- check required tools before planning and execution;
- report missing tools as a distinct feasibility failure;
- support a documented bootstrap command or virtual environment.

A missing tool must not be confused with a failing test or a failed implementation.
