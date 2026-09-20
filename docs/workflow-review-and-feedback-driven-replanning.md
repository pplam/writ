## Executive summary

Writ already has a solid execution engine: durable state, atomic writes, DAG scheduling, resumability, per-task acceptance criteria, independent review, and bounded rework.

The weak point is exactly the one you identified: **the entire execution graph is still effectively authored by one planning agent**. Writ validates that the plan is structurally valid, but not that it is *complete, feasible, correctly decomposed, or semantically faithful to the design*. A plan can therefore be syntactically perfect and still fail during execution.

The best improvement is not simply “use a stronger planning prompt.” It is to turn planning into a **multi-stage, evidence-producing planning pipeline** with independent analysis, plan synthesis, adversarial critique, repair, and an explicit approval/validation gate before execution.

---

# 1. Current workflow

## 1.1 Planning

The current path is:

```text
design document
    │
    ▼
single planning agent
    │
    ▼
JSON plan
    │
    ├─ load_plan()
    ├─ basic field validation
    ├─ dependency reference validation
    ├─ unresolved design-section detection
    ├─ Writ-owned ID assignment
    ├─ dependency translation
    └─ check_dag()
    │
    ▼
tasks committed to .writ/state.json
```

The planning agent is asked to determine:

- milestones
- task boundaries
- task titles
- acceptance criteria
- dependencies
- allowed and forbidden paths
- design-document traceability
- notes, assumptions, and risks

The relevant implementation is primarily:

- `writ/planning.py`
- `writ/planner.py`
- `writ/commands.py`
- `writ/model.py`

The deterministic `--extract` path is intentionally much simpler: headings become milestones/tasks and explicit gate markers become acceptance criteria.

## 1.2 Execution

Execution is:

```text
ready task
    │
    ▼
implementing agent
    │
    ▼
agent verdict
    │
    ▼
awaiting-review
    │
    ▼
independent reviewer
    │
    ├─ accept → completed
    ├─ reject with budget → planned/rework
    └─ reject after budget → failed
```

The scheduler in `writ/orchestrator.py`:

- selects only dependency-ready tasks
- prioritizes reviews over new implementation
- claims work through `runner.prepare`
- supports parallel workers
- records durable runs
- reaps interrupted processes
- supports resumability
- sends rejected work back for bounded rework
- reports transitive blocked work

This is a strong foundation. The implementation/review separation is particularly useful.

---

# 2. What is already good

Several parts of the current design should be preserved.

## Durable, resumable execution

State is persisted in `.writ/state.json`, and runs have their own directories. This means planning and execution do not depend on in-memory scheduler state.

## Independent review

The implementing agent cannot mark its own task completed. Completion requires a separate reviewer. This prevents a weak implementation verdict from silently completing work.

## Per-criterion evidence

Acceptance criteria are not just a task-level boolean. The verdict format requires evidence for each criterion, and reviewers must independently verify them.

## Bounded rework

A rejection carries reviewer findings into the next implementation attempt. This is better than simply marking a task failed and losing the most useful diagnostic context.

## DAG scheduling

The scheduler handles:

- forks
- joins
- dependency readiness
- parallel execution
- critical-path-oriented ordering
- interrupted work

The execution phase is substantially more mature than the planning phase.

---

# 3. Current planning weaknesses

## 3.1 Validation is mostly syntactic

`planning.load_plan()` checks that:

- the JSON is valid
- milestones exist
- tasks exist
- task titles exist
- acceptance criteria are non-empty
- IDs are not duplicated
- self-dependencies are rejected

Later, `check_dag()` checks:

- unknown dependencies
- cycles

That catches malformed plans, but not bad plans.

For example, these plans can pass structural validation:

```json
{
  "title": "Implement the entire feature",
  "acceptances": ["it works"]
}
```

Or:

```json
{
  "title": "Update backend",
  "allowed": ["backend/"],
  "acceptances": ["the complete product works"]
}
```

Or a plan that completely omits an important requirement from the design.

The current rules tell the planner not to make these mistakes, but the system does not independently prove that it avoided them.

## 3.2 No independent requirements coverage check

There is no formal mapping like:

```text
design requirement → one or more tasks → acceptance criteria → verification
```

The only design traceability check is `unresolved_sections()`, which detects task sections that do not exist. It does not detect:

- requirements that have no task
- requirements that have a task but no acceptance criterion
- acceptance criteria unsupported by the design
- important constraints mentioned only in prose
- cross-cutting requirements omitted from all milestones

This is probably the largest gap.

## 3.3 One agent performs too many different reasoning jobs

The planner must simultaneously:

1. understand the product/design
2. inspect the repository
3. identify existing functionality
4. decompose work
5. infer dependencies
6. define acceptance criteria
7. define file boundaries
8. decide what is already implemented
9. predict integration risks

Those are different tasks requiring different perspectives. A single model response is prone to:

- premature decomposition
- missing repository constraints
- underestimating integration work
- inventing dependencies
- omitting tests
- confusing implementation order with conceptual order
- missing non-functional requirements

## 3.4 Dependency correctness is not semantically checked

The graph validator checks whether the graph is legal, not whether it is correct.

It cannot detect:

- a missing dependency
- a dependency on an overly broad task
- a task depending on a test task when it should depend on an API/task contract
- a task that touches files owned by a parallel sibling
- integration work missing after parallel branches
- a graph that is technically acyclic but operationally impossible

The planner's dependency list is accepted as truth.

## 3.5 The default chaining behavior can hide planner omissions

In `_resolve_depends()`:

```python
if not task.depends_on:
    return [previous] if chain and previous else []
```

Unless `--parallel` is used, tasks with no explicitly stated dependencies are chained to the previous task.

This is conservative from a correctness perspective, but it has two drawbacks:

1. It can create artificial serialization.
2. More importantly, it can make an incomplete dependency model look valid.

For example, if the planner forgets that task C requires task A, Writ may still execute C after some unrelated task B merely because of plan order. The workflow will not fail because the graph is invalid; it may fail later because the dependency was semantically missing.

The same issue appears when appending plans: the first new task may be chained to the last existing task unless explicitly configured otherwise.

## 3.6 Acceptance criteria are not checked deeply enough

The prompt says each task should have 2–6 checkable criteria, but validation only requires a non-empty list.

There is no automated check for:

- vague criteria
- duplicate criteria
- criteria that cannot be verified by the task
- criteria that depend on sibling tasks
- criteria incompatible with `allowed`
- criteria lacking a test or observable artifact
- criteria that are really requirements for the whole project rather than the task

## 3.7 Path fences are not validated

`allowed` and `forbidden` are stored but not deeply analyzed.

The system does not currently appear to reject or warn about:

- nonexistent paths
- overlapping allowed/forbidden paths
- two parallel tasks owning the same files
- a task with no allowed path that modifies a critical shared component
- acceptance criteria requiring files outside the allowed scope

This is a major source of parallel execution failures.

## 3.8 No pre-execution feasibility check

Before implementation begins, Writ does not ask:

- Can the repository currently build?
- Do the referenced test directories exist?
- Are required dependencies or tools available?
- Are the claimed ownership boundaries realistic?
- Does the existing code support the proposed integration points?
- Can the tasks be implemented independently in the stated order?

These checks should happen before spending implementation tokens.

## 3.9 Review is task-local, not plan-global

The reviewer checks a task against its own criteria. It does not generally ask:

- Was a design requirement omitted from the overall plan?
- Does this task conflict with another task?
- Does the final architecture satisfy the original design?
- Did two tasks implement incompatible assumptions?
- Is an integration task missing?
- Does the dependency graph still make sense after decisions made during implementation?

This means the current system has strong local verification but weak global verification.

---

# 4. Recommended workflow: staged planning with independent critics

I recommend replacing the single planner with the following pipeline.

```text
Design document
      │
      ▼
1. Requirements extraction
      │
      ▼
2. Repository reconnaissance
      │
      ▼
3. Test and verification analysis
      │
      ▼
4. Independent candidate plans
      │
      ▼
5. Plan synthesis
      │
      ▼
6. Adversarial plan review
      │
      ├─ missing requirements?
      ├─ bad dependencies?
      ├─ impossible scopes?
      ├─ missing integration?
      ├─ weak acceptance criteria?
      └─ conflicts between tasks?
      │
      ▼
7. Plan repair / adjudication
      │
      ▼
8. Static validation and feasibility checks
      │
      ▼
9. Human approval or explicit auto-approval
      │
      ▼
10. Execution
      │
      ▼
11. Milestone-level integration review
      │
      ▼
12. Global acceptance review
```

The key principle is:

> No single agent should be trusted to simultaneously invent the requirements map, task decomposition, dependency graph, and acceptance contract.

---

# 5. Proposed planning stages

## Stage A: Requirements extraction

Have one agent produce a normalized requirements inventory, not a DAG.

Example:

```json
{
  "requirements": [
    {
      "id": "REQ-001",
      "text": "The system must reject malformed input deterministically.",
      "source": {
        "document": "design.md",
        "section": "Input handling",
        "quote": "..."
      },
      "type": "behavior",
      "priority": "must",
      "verification_hints": [
        "negative test",
        "stable error shape"
      ],
      "ambiguities": []
    }
  ]
}
```

This agent should not decide task boundaries.

Its job is to answer:

- What does the design require?
- What is optional versus mandatory?
- What constraints are stated?
- What is ambiguous?
- What must be demonstrated?

This creates the canonical coverage target.

## Stage B: Repository reconnaissance

A separate agent inspects the repository and produces an inventory:

```json
{
  "components": [
    {
      "name": "event store",
      "paths": ["writ/state.py", "writ/runner.py"],
      "existing_behavior": "...",
      "test_locations": ["tests/test_state.py"],
      "extension_points": ["..."],
      "risks": ["..."]
    }
  ],
  "existing_coverage": [
    {
      "requirement_id": "REQ-003",
      "status": "partial",
      "evidence": "tests/test_api.py::test_..."
    }
  ],
  "baseline_commands": [
    "pytest -q"
  ],
  "baseline_result": {
    "status": "pass",
    "summary": "..."
  }
}
```

This prevents the planning agent from treating existing work as new work or assuming nonexistent directories.

## Stage C: Verification/test strategy

A third analysis focuses only on how requirements can be verified:

```json
{
  "requirement_id": "REQ-001",
  "verification": [
    {
      "kind": "test",
      "location": "tests/test_parser.py",
      "command": "pytest -q tests/test_parser.py",
      "observable": "malformed input produces stable error"
    }
  ]
}
```

This is important because many weak plans contain plausible implementation tasks but no reliable way to prove completion.

## Stage D: Candidate plans

Generate one or more candidate decompositions from the previous artifacts.

A candidate task should contain more metadata than today:

```json
{
  "id": "TASK-012",
  "title": "Reject malformed event records",
  "requirement_ids": ["REQ-001", "REQ-004"],
  "component_ids": ["event-parser"],
  "depends_on": ["TASK-008"],
  "allowed": ["writ/parser.py", "tests/test_parser.py"],
  "forbidden": ["writ/state.py"],
  "acceptances": [
    {
      "text": "pytest -q tests/test_parser.py passes",
      "verification": "pytest -q tests/test_parser.py"
    }
  ],
  "integration_risks": [],
  "assumptions": []
}
```

The important addition is explicit requirement coverage.

## Stage E: Plan synthesis

A synthesizer combines:

- requirement inventory
- repository inventory
- test strategy
- candidate plans
- existing Writ state

It produces the actual executable plan.

Unlike the current planner, it should be forbidden from silently dropping unresolved items. Every requirement must end in one of:

- covered by task(s)
- already implemented, with evidence
- intentionally out of scope, with explanation
- blocked by an ambiguity requiring human input

## Stage F: Adversarial plan review

Run several independent critics, preferably with different prompts or models.

### Coverage critic

Checks:

- every must-have requirement is covered
- no task lacks a requirement or explicit infrastructure justification
- every requirement has verification
- cross-cutting requirements are represented

### Dependency critic

Checks:

- missing edges
- unnecessary edges
- cycles
- dependencies based on actual contracts rather than task ordering
- integration/join tasks
- whether parallel branches can really coexist

### Scope/ownership critic

Checks:

- allowed and forbidden paths
- overlapping ownership
- tasks that are too broad
- tasks that are too small
- tasks that cannot be implemented within their fence
- tasks likely to modify shared files

### Acceptance critic

Checks:

- criteria are observable
- criteria are task-local
- criteria are not vague
- criteria do not require sibling work
- commands and expected artifacts exist
- test requirements are sufficient

### Repository feasibility critic

Checks:

- referenced paths exist or are intentionally new
- test commands are plausible
- baseline build/test state is known
- proposed architecture matches existing conventions
- dependencies and tools are available

Each critic should return structured findings, not prose only.

Example:

```json
{
  "severity": "error",
  "category": "missing-coverage",
  "requirement_id": "REQ-007",
  "message": "No task verifies timeout behavior.",
  "suggested_action": "Add a task or acceptance criterion covering timeout expiry."
}
```

## Stage G: Repair and adjudication

A repair agent receives:

- the candidate plan
- all critic findings
- the original requirements inventory
- the repository inventory
- unresolved ambiguities

It must produce either:

```json
{
  "status": "ready",
  "plan": "...",
  "remaining_risks": []
}
```

or:

```json
{
  "status": "needs-human-decision",
  "questions": [
    {
      "id": "Q-001",
      "question": "Should timeout be per request or per operation?"
    }
  ]
}
```

It should not be allowed to simply ignore a critic finding. Every finding needs a disposition:

```json
{
  "finding_id": "F-004",
  "resolution": "accepted",
  "change": "Added TASK-019",
  "reason": ""
}
```

This creates a reviewable planning audit trail.

---

# 6. Static checks Writ should add

The multi-agent process is valuable, but Writ should also enforce deterministic checks.

## 6.1 Requirement coverage matrix

Add a plan-level coverage structure:

```json
{
  "requirements": [
    {
      "id": "REQ-001",
      "source": "design.md#Input handling",
      "status": "covered",
      "tasks": ["M01-002"],
      "verification": ["M01-002.1"]
    }
  ]
}
```

Reject or warn when:

- a must-have requirement has no task
- a task references no requirement
- a requirement has no verification
- a requirement is marked already implemented without evidence

## 6.2 Stronger acceptance validation

At minimum:

- require 2–6 criteria for generated plans
- reject duplicate criteria
- require each criterion to include either:
  - a command
  - a test path
  - an observable artifact
  - an explicit behavior
- reject generic phrases such as:
  - “works correctly”
  - “code is clean”
  - “fully implemented”
  - “all requirements met”

Do not make this entirely regex-based; use a critic agent for semantic quality, but deterministic checks should catch obvious failures.

## 6.3 Validate path fences

Add checks for:

- allowed paths that do not exist, unless marked `new`
- forbidden paths overlapping allowed paths
- multiple parallel tasks owning the same path
- acceptance criteria requiring files outside allowed scope
- tasks with broad scope such as the repository root

## 6.4 Baseline feasibility check

Before plan approval:

1. run the baseline test/build command
2. verify referenced test paths
3. verify referenced source paths
4. check for clean/known git state
5. record current failures separately from planned failures

This is important because otherwise every later failure is ambiguous.

## 6.5 Remove implicit chaining from the correctness model

I would change the semantics so that omitted dependencies remain omitted.

If Writ wants a conservative mode, expose it explicitly:

```bash
writ plan design.md --default-dependency-policy chain
```

But the default should probably be:

```text
No stated dependency means independent.
```

Artificial chaining hides planner omissions and unnecessarily reduces parallelism.

If a task truly needs ordering, the plan should say why.

## 6.6 Add explicit integration tasks

Every plan should answer:

- What combines the parallel branches?
- What validates the complete feature?
- What verifies cross-component behavior?

A graph with multiple branches and no integration/join task should receive a warning or require explicit justification.

## 6.7 Add plan-level gates

Today acceptance is mostly task-local. Add milestone and final-plan gates:

```json
{
  "milestone_gates": [
    {
      "milestone": "M02",
      "criteria": [
        {
          "text": "The storage and API layers work together",
          "command": "pytest -q tests/integration/test_storage_api.py"
        }
      ]
    }
  ],
  "final_gates": [
    {
      "text": "All design requirements are covered",
      "verification": "coverage matrix review"
    },
    {
      "text": "Full project verification passes",
      "command": "pytest -q"
    }
  ]
}
```

These should be reviewed after the relevant tasks complete.

---

# 7. Execution should become feedback-driven

The planner should not be treated as immutable once execution starts.

A stronger lifecycle is:

```text
approved plan
    │
    ▼
execute first frontier
    │
    ▼
review implementation
    │
    ▼
observe:
  - unexpected dependencies
  - missing shared interfaces
  - contradictory assumptions
  - impossible acceptance criteria
  - repeated rework
    │
    ├─ local issue → normal task rework
    ├─ task contract issue → revise task
    ├─ graph issue → plan repair
    └─ design ambiguity → human decision
```

## Trigger replanning when

- a task is rejected twice
- two tasks propose conflicting decisions
- a task reports a missing prerequisite
- a task is blocked by another component not in the graph
- a reviewer says the criterion is not achievable within scope
- a task needs to modify a forbidden/shared path
- a new dependency is discovered
- a milestone integration gate fails

The current workflow treats these mostly as task-level failures. Some are actually evidence that the plan is wrong.

## Do not let agents silently mutate the DAG

Agents should not directly edit task dependencies. Instead they should emit structured plan-change proposals:

```json
{
  "type": "add-dependency",
  "from": "M03-002",
  "to": "M02-004",
  "reason": "M03-002 consumes the interface created by M02-004",
  "evidence": "..."
}
```

A plan-repair process or human then approves the change.

---

# 8. Recommended state model

The current `planned` state is too overloaded. It can mean:

- newly planned
- not yet approved
- approved and ready eventually
- returned for rework
- awaiting plan repair

Consider adding planning states separately from execution states:

```text
draft-plan
plan-under-review
plan-needs-decision
plan-approved
ready
running
awaiting-review
rework
blocked
completed
failed
```

At the plan level:

```json
{
  "plan_status": "draft|reviewing|needs-human|approved|executing|complete",
  "artifacts": {
    "requirements": "...",
    "repository_inventory": "...",
    "test_strategy": "...",
    "candidate_plan": "...",
    "critic_reports": "...",
    "repaired_plan": "..."
  },
  "findings": [],
  "decisions": []
}
```

This avoids committing a plan into executable state before it has passed planning review.

---

# 9. Proposed command experience

The current command:

```bash
writ plan design.md
```

could evolve into:

```bash
writ plan design.md
```

which runs the full pipeline and stops before execution if there are unresolved issues.

Useful commands:

```bash
writ plan design.md --stage requirements
writ plan design.md --stage inventory
writ plan design.md --stage synthesize
writ plan review PLAN_ID
writ plan findings PLAN_ID
writ plan approve PLAN_ID
writ plan repair PLAN_ID
writ plan import PLAN_JSON
writ plan explain TASK_ID
writ plan coverage
```

The normal path could remain simple:

```bash
writ init
writ plan docs/design.md
writ run --parallel 4
```

But internally `writ plan` would now create several artifacts and require the plan to reach `approved`.

For automation, support:

```bash
writ plan design.md --auto-approve
```

but only when no error-severity findings remain.

---

# 10. Concrete implementation sequence

I would implement this incrementally rather than rewriting the whole system.

## Phase 1: Strengthen deterministic validation

Modify `writ/planning.py`, `writ/model.py`, and plan import logic to add:

- explicit requirement IDs
- task-to-requirement references
- stronger acceptance checks
- path fence validation
- duplicate/overlap detection
- plan-level warnings
- integration-task detection
- plan validation report

This gives immediate value even with one planning agent.

## Phase 2: Add a requirements inventory stage

Add a structured artifact under:

```text
.writ/plans/<plan-id>/requirements.json
```

Have the planner first extract requirements before producing tasks.

Then require the generated plan to reference those IDs.

## Phase 3: Add independent critics

Add artifacts such as:

```text
.writ/plans/<plan-id>/coverage-review.json
.writ/plans/<plan-id>/dependency-review.json
.writ/plans/<plan-id>/scope-review.json
.writ/plans/<plan-id>/acceptance-review.json
```

Run them sequentially or in parallel.

Since you said time is not important, sequential execution is acceptable and may make debugging easier. Parallel critics are still useful because they are independent.

## Phase 4: Add repair/adjudication

Have a repair agent consume the findings and produce a revised plan plus dispositions for every finding.

Do not overwrite earlier artifacts. Preserve every version.

## Phase 5: Add milestone and final integration gates

Keep task-level implementation/review exactly as it is, but add:

- milestone integration review
- final requirements coverage review
- full-suite verification

## Phase 6: Add execution-triggered plan repair

When execution discovers a graph problem, create a plan-change proposal instead of treating every issue as an ordinary task failure.

---

# 11. Suggested planning pipeline in detail

A robust default could look like this:

```text
P0: baseline
    - capture git status
    - run existing tests/build
    - record known failures

P1: requirements analyst
    - extract requirements, constraints, ambiguities
    - assign REQ IDs

P2: repository analyst
    - inspect architecture and existing implementation
    - map requirements to existing code/tests
    - identify extension points and risks

P3: verification analyst
    - define how every requirement can be demonstrated
    - identify missing test infrastructure

P4: plan author
    - produce candidate milestones/tasks/DAG
    - reference REQ IDs
    - include ownership and verification

P5: coverage critic
P6: dependency critic
P7: scope critic
P8: acceptance critic
P9: feasibility critic

P10: plan repairer
    - resolve findings
    - add tasks/edges/gates
    - expose unresolved questions

P11: deterministic Writ validation
    - schema
    - graph
    - coverage
    - scope
    - path
    - verification

P12: approval
    - human approval if needed
    - otherwise auto-approve only with no blocking findings

P13: execution
```

This is intentionally more expensive. That is appropriate given your stated preference for correctness over implementation time.

---

# 12. Highest-value changes

If implementing only a few improvements, I would prioritize these:

1. **Requirements coverage matrix**
2. **Independent plan critics**
3. **Explicit plan approval state**
4. **No implicit dependency chaining by default**
5. **Plan-level integration/final gates**
6. **Execution-triggered plan repair**
7. **Path ownership and acceptance feasibility validation**

These directly address the failure mode you described.

---

# Final recommendation

The central design change should be:

> Treat the plan as a reviewed engineering artifact, not as the direct output of one agent.

The current workflow is:

```text
one agent → validated JSON → execution
```

The improved workflow should be:

```text
requirements analysis
→ repository analysis
→ verification analysis
→ candidate plan
→ independent critiques
→ repaired plan
→ deterministic validation
→ approval
→ execution
→ milestone/global review
→ feedback-driven replanning
```

Writ's current execution machinery can remain largely intact. The largest architectural work belongs around `writ/planning.py` and plan state management, not in the scheduler itself.

The current scheduler is already capable of executing a good graph. The problem is that Writ currently has too little confidence that the graph represents the complete and feasible work required by the design.

Yes to all three. **Milestone/global reviewers can automatically trigger replanning; replanning can add repair tasks and dependencies; execution can then resume automatically.**

The important constraint is that a reviewer should **report findings and request repair—not directly rewrite the live graph**. Writ should control validation, application, and resumption.

This would be new functionality. Today, Writ automatically reworks a rejected task, but does not automatically repair the plan.

## 1. The feedback loop

The proposed loop is:

```text
Execute tasks
     │
     ▼
Milestone or global review
     │
     ├─ pass ───────────────────────────────► continue / finish
     │
     └─ findings
            │
            ▼
       Classify findings
            │
            ├─ local implementation defect ─► task rework
            ├─ missing work / integration ──► plan repair
            ├─ infrastructure failure ──────► retry / pause
            └─ requirement ambiguity ───────► human decision
                                              
       Plan repair
            │
            ▼
       Proposed graph patch
            │
            ▼
       Validate + independently review
            │
            ├─ unsafe / unresolved ─────────► human decision
            │
            └─ approved
                   │
                   ▼
             Apply atomically
                   │
                   ▼
             Resume execution
                   │
                   ▼
             Repeat affected review
```

The workflow finishes only when the **global acceptance review passes on the current integrated code**, not merely when all implementation tasks are completed.

That distinction is essential: a graph can finish successfully while omitting required work.

---

## 2. What the milestone and global reviewers review

These reviews need different contracts from the current task reviewer.

### Task reviewer

Question:

> Did this implementation satisfy this task’s acceptance criteria?

This remains narrow and supports the existing rework loop.

### Milestone reviewer

Question:

> Do the integrated changes achieve the milestone’s intended outcome, including interactions between its tasks?

It checks:

- milestone requirements
- integration between completed tasks
- compatibility of interfaces
- cross-component tests
- consequential assumptions
- missing work that individual task criteria did not capture

### Global reviewer

Question:

> Does the integrated product satisfy the approved requirements and constraints?

It reads the requirements and design directly, not only the task list. Otherwise it would inherit the original planner’s omissions.

It checks:

- requirements coverage
- end-to-end behavior
- cross-milestone integration
- regression tests
- compatibility, migration, security, and other applicable constraints
- unresolved decisions and known findings

**Milestone reviews detect local integration gaps early; global review catches gaps across the whole implementation.**

---

## 3. How a reviewer triggers replanning

Extend the review report with structured findings.

For example:

```json
{
  "decision": "needs-repair",
  "scope": "milestone:M03",
  "reviewed_revision": "abc123",
  "findings": [
    {
      "id": "F-021",
      "category": "missing-integration",
      "severity": "blocking",
      "requirement_ids": ["REQ-014"],
      "summary": "CLI-created timeout values never reach the execution engine.",
      "evidence": [
        "tests/integration/test_timeout.py::test_cli_timeout failed",
        "The CLI parses timeout but does not pass it into runner.execute."
      ],
      "affected_components": ["cli", "runner"],
      "required_outcome": "The timeout supplied through the CLI controls execution."
    }
  ]
}
```

Writ receives this report and creates a **repair request**.

The reviewer may suggest a solution, but its main responsibility is to establish:

1. What is wrong?
2. Which approved requirement is affected?
3. What evidence demonstrates the problem?
4. What observable outcome would close the finding?

The repair planner decides how to decompose the fix.

This avoids turning reviewers into a second unvalidated planning authority.

---

## 4. How dynamic patch tasks are created

The repair planner receives a bounded context:

- the findings
- the relevant requirements
- the current graph and task contracts
- completed implementation evidence
- the current repository revision
- affected interfaces and decisions
- previous repair attempts

It proposes a **graph patch**, not an entirely new plan.

For example:

```json
{
  "base_plan_revision": 7,
  "repair_request": "RR-004",
  "add_tasks": [
    {
      "id": "proposed-timeout-fix",
      "title": "Propagate CLI timeout into execution",
      "requirement_ids": ["REQ-014"],
      "resolves_findings": ["F-021"],
      "depends_on": ["M02-003", "M03-002"],
      "allowed": [
        "writ/cli.py",
        "writ/runner.py",
        "tests/integration/test_timeout.py"
      ],
      "acceptances": [
        "A failing integration test reproduces the lost timeout.",
        "The CLI timeout terminates an overlong execution.",
        "Existing runner timeout tests continue to pass."
      ]
    }
  ],
  "hold_gates": ["G-M03"],
  "repeat_reviews": ["G-M03", "G-GLOBAL"]
}
```

Writ assigns the permanent task ID and applies the patch after validation.

The updated graph might be:

```text
M02-003 ─────┐
             ├── Repair task ── task review ── M03 gate review
M03-002 ─────┘                                      │
                                                   ▼
                                             downstream work
```

The repair task then uses the **same implementation → independent review → bounded rework** machinery Writ already has.

There is no need for a separate patch-execution engine.

---

## 5. Model review gates explicitly

I recommend making milestone/global reviews explicit **gate objects**.

```text
Milestone implementation tasks
             │
             ▼
       Milestone gate
             │
             ▼
      Dependent milestone
```

A gate is different from an ordinary implementation task:

- it does not modify production code
- it judges an integrated outcome
- it can pass, request repair, or require a decision
- it retains a history of review attempts
- it is satisfied only for the code and contracts it actually reviewed

For example:

```text
M03 implementation complete
          │
          ▼
G-M03 review
          │
          ├─ passed → unlock M04
          │
          └─ needs repair
                 │
                 ▼
             Repair tasks
                 │
                 ▼
             G-M03 re-review
```

### Avoid a dependency cycle

A repair task must **not depend on the gate passing**. The gate has just failed.

Instead:

- repair tasks depend on the completed prerequisites they need
- the gate waits for those repair tasks before its next review
- downstream tasks continue to wait for the gate to pass

This preserves an acyclic execution graph while allowing repeated review attempts.

---

## 6. When execution resumes

After an approved graph patch is applied:

1. Writ inserts the repair tasks.
2. Writ holds the affected gates.
3. New tasks become eligible when their prerequisites are satisfied.
4. The scheduler dispatches them normally.
5. Their task reviewers verify the fixes.
6. The affected milestone/global reviewers run again.
7. Passing gates release downstream work.

**No manual restart should be required in the normal case.** `writ run` should remain active through repair planning and resume scheduling when the patch lands.

Unrelated work can continue while a repair is being planned.

However, for a first implementation I would choose the safer behavior:

> Pause the affected execution region, let its in-flight work settle, repair the graph, then resume.

Since speed is not your primary concern, this is much easier to make correct than editing contracts underneath running agents.

---

## 7. What happens to completed tasks?

Do not erase history or automatically reset every completed task.

There are two distinct facts:

- **Historical completion:** a reviewer accepted task T against its contract at revision X.
- **Current validity:** the current integrated code still satisfies the relevant requirement.

A patch can preserve the first while invalidating the second.

For example:

```text
Task A: completed at revision X
Task B: completed at revision Y
Integration review: A and B do not work together
Repair task C: fixes the integration
Integration gate: reviewed again at revision Z
```

A completed task should usually stay historically completed. Add repair work and rerun the necessary verification.

If a repair changes an interface that completed downstream work relied on, Writ should mark the affected verification as stale and schedule revalidation or adaptation tasks. **Blindly re-running all completed implementation tasks would waste work and can introduce regressions.**

---

## 8. Safe automatic replanning

Automation should depend on the nature of the change.

| Proposed change | Suggested policy |
|---|---|
| Add a regression test for an approved requirement | Automatic after validation |
| Add missing integration work | Automatic after independent patch review |
| Add a dependency between unstarted tasks | Automatic after impact and cycle checks |
| Split an unstarted task into smaller tasks | Automatic with preserved coverage |
| Change an interface used by completed work | Require impact analysis and downstream revalidation |
| Change a running task’s contract | Hold it; do not mutate silently |
| Weaken an acceptance criterion | Never automatic by default |
| Remove an approved requirement | Human approval |
| Resolve a product ambiguity | Human approval |
| Introduce a breaking change or new external dependency | Policy-controlled, normally human approval |

The core rule is:

> Automatic repair may change the implementation strategy, but must not silently change what success means.

Otherwise a repair planner could make a failing workflow “pass” by deleting the difficult requirement.

---

## 9. Validation before applying a graph patch

Writ—not an agent—should enforce these invariants:

- the patch targets the current plan revision
- referenced tasks and requirements exist
- the revised graph is acyclic
- every blocking finding has a proposed disposition
- requirement coverage is preserved
- acceptance criteria are not silently weakened
- running task contracts remain unchanged
- replacement tasks preserve the obligations of superseded tasks
- affected downstream verification is identified
- no task can start before its new prerequisites are met

An independent patch reviewer then assesses the semantic questions:

- Does this repair actually address the finding?
- Is the proposed scope sufficient?
- Does it introduce another integration gap?
- Are the verification criteria meaningful?

Apply the approved patch atomically using Writ’s existing state transaction mechanism, extended with a plan revision check. If the graph changed meanwhile, the patch must be revalidated rather than blindly applied.

---

## 10. Preventing endless repair loops

Even when implementation time is unimportant, endless autonomous churn is not useful.

Track repair progress by finding, not just by task count:

```text
F-021
  repair round 1 → behavior still broken
  repair round 2 → original defect fixed, regression introduced
  repair round 3 → same regression persists
  → escalate
```

Use separate limits for:

- task rework
- repair rounds per finding
- milestone repair rounds
- global repair rounds

Also detect:

- repeated identical findings
- repeated patches with no new evidence
- alternating incompatible fixes
- graph growth without findings being closed
- repair proposals that keep relaxing the contract

At that point, pause with a focused decision request rather than adding more tasks.

Crucially, **a finding closes only when verification demonstrates the required outcome—not when its repair task merely reports completion.**

---

## 11. Changes needed in Writ

This builds on the current implementation rather than replacing it.

| Area | Change |
|---|---|
| `writ/verdict.py` | Add milestone/global findings and repair-request reports, separate from task verdicts |
| `writ/planning.py` | Add bounded repair planning and graph-patch review |
| `writ/model.py` | Add gates, plan revisions, finding lifecycle, and verification freshness |
| `writ/orchestrator.py` | Schedule gate reviews and repair jobs; hold affected work and auto-resume |
| `writ/state.py` | Persist repair requests, graph patches, gate attempts, and revisions |
| CLI/UI | Show “repairing plan,” unresolved findings, inserted tasks, and why work is held |

One significant scheduler change: today `run()` exits when there are no in-flight jobs and no eligible task jobs. With this design, it must distinguish:

```text
No task is ready because:
  - the project is complete
  - a gate review is due
  - repair planning is pending
  - repair tasks must be inserted
  - a human decision is required
  - execution is genuinely stalled
```

“No ready tasks” is no longer sufficient to conclude that the run is finished.

## Recommendation

Implement this first as a **checkpoint-based repair loop**:

1. Execute a milestone.
2. Review the integrated outcome.
3. If necessary, automatically propose and review a graph patch.
4. Insert repair tasks.
5. Execute them.
6. Repeat the milestone review.
7. Continue.
8. Apply the same loop at global acceptance.

Later, allow unrelated branches to continue during repair.

This gives Writ the key capability it currently lacks: **a wrong initial plan becomes a recoverable hypothesis, rather than a fixed contract that execution must somehow force to succeed.**
