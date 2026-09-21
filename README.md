# Writ

Plan, dispatch, and track coding-agent work against a design document.

Writ takes a markdown design doc, has a coding agent turn it into a
dependency-ordered task DAG with explicit acceptance criteria, hands one task at
a time back to an agent, and keeps an append-only decision log. State is plain
files — JSON, markdown, and logs — under `<project>/.writ`.

Three premises.

An agent should never be asked to "implement the design": it gets one bounded
task, a stated bar, and a guardrail list.

Whether that bar was met is decided by agents, not typed in by a human. The agent
that did the work reports a structured verdict with evidence, and a reviewer agent
that did not write the code decides whether to sign it off. Everything either of
them claimed stays inspectable afterwards.

And the plan gets the same treatment as the work. A plan is a reviewed artifact,
not the direct output of one agent: its requirements are inventoried and their
coverage checked, independent critics read it, a human approves it on the record,
and gates judge whether the finished work actually adds up. `writ run` will not
start a plan nobody has signed off.

**Contents** — [Install](#install) · [Quick start](#quick-start) ·
[Storage](#storage) · [Planning](#planning) · [Task lifecycle](#task-lifecycle) ·
[Configuration](#configuration) · [Commands](#commands) · [Watching it work](#watching-it-work) ·
[Running the whole graph](#running-the-whole-graph) · [Dispatch](#dispatch) ·
[Agent invocation](#agent-invocation) · [Tests](#tests)

## Install

```bash
uv tool install --editable /path/to/writ
```

Or run without installing:

```bash
python -m writ --help
```

## Quick start

```bash
cd ~/projects/my-service

writ init
writ plan docs/design.md --dry-run     # see what the planner will be asked
writ plan docs/design.md               # three analyses, then a plan built on them

writ check                             # what writ can prove about the plan
writ coverage                          # every requirement, and what covers it
writ critique                          # five agents read it and report findings
writ approve                           # sign the plan off; `writ run` requires it

writ status                            # progress, ready work, live runs
writ list --ready                      # what can start now
writ show M01-001                      # the task and its acceptance bars

writ run --parallel 3                  # walk the whole graph: dispatch, review,
                                       # repeat, three agents at a time
writ status --watch                    # from any other terminal, any time

writ list decisions --proposed         # choices the agents had to make
writ set D-0001 active                 # confirm one, or reject it with --reason
```

To drive one task at a time instead:

```bash
writ dispatch M01-001 --agent claude   # implements it, reports a verdict
writ logs M01-001 --follow             # stream the agent's output
writ review M01-001 --agent codex      # a different agent checks the claim
                                       # its decision completes or fails the task
```

## Storage

```
<project>/.writ/
  state.json          the whole project: the plan and its status, requirements,
                      milestones, tasks and gates, runs, findings, repair
                      requests, decisions
  config.json         your defaults: which agent fills each role, and how
                      `writ run` behaves. Written once by `writ init` holding
                      writ's own defaults, and never touched again
  decisions.md        human-readable mirror of the decision log
  run.session         the pid of the active `writ run`, if any
  plans/<plan-id>/
    prompt.txt        what the planning agent was asked
    plan.json         the plan it returned, before validation
    stdout.log
    stderr.log
  reviews/r<revision>/<critic>/
    prompt.txt        what that critic was asked, at that plan revision
    findings.json     what it found
    stdout.log
    stderr.log
  runs/<run-id>/
    prompt.txt        exactly what the agent was given
    verdict.json      what it claimed, per criterion, with evidence
    patch.json        repair runs only: the graph change it proposed
    stdout.log
    stderr.log
    supervisor.log    detached runs only
```

Writes are atomic (temp file + replace) and serialized by an advisory lock, so
a detached run and an interactive `writ status` never corrupt each other.

## Planning

`writ plan` turns a design document into a task DAG: milestones, tasks, checkable
acceptance criteria, the dependency edges between them, and the paths each task
may touch. Planning is a judgement call — which work is one bounded session, what
the real bar is, what must land first — and reading the repo is part of making it.

It runs as a **staged pipeline** rather than one agent call, because those are
different judgements and running them together loses what makes a plan checkable.
Three analyses write artifacts first, then a synthesis agent decomposes the work
from them:

```text
requirements.json   every obligation the document states, one per entry
inventory.json      what the repository already is, does, and tests — plus the
                    baseline: did the suite pass before any of this started
verification.json   how each obligation could actually be demonstrated
        │
        ▼
plan.json           the decomposition, synthesized from all three
```

Each artifact is checkable before the next stage sees it: an inventory claiming
coverage of a requirement nobody recorded is rejected at the stage that invented
it, rather than becoming a task no obligation asked for. And the synthesizer is
held to them — the requirement inventory arrives as a fixed list, and a dropped or
invented id is a finding on the plan (`analysis.reconcile`). A single agent writing
the inventory and the tasks together can never disagree with itself, so the
omission leaves no trace anywhere; fixing the list first is what makes it findable.

```bash
writ plan docs/design.md --dry-run     # print the planning prompt
writ plan docs/design.md               # analyse, synthesize, validate, commit
writ plan docs/design.md --agent claude --model opus
writ plan docs/design.md --instructions "storage layer first"

writ plan docs/design.md --stage requirements   # stop after one analysis
writ plan docs/design.md --plan-id design-20260101T120000   # resume a pipeline
writ plan docs/design.md --refresh              # redo stages already done
writ plan docs/design.md --no-stages            # the older single-shot planner
```

Stages are resumable. Artifacts live under `.writ/plans/<plan-id>/`, and a stage
whose artifact is already there is reused rather than re-run, so a pipeline that
failed at synthesis does not pay for three analyses again. A failed stage stops the
pipeline instead of synthesising from a partial set.

Deliberately absent: competing candidate plans. With the requirement inventory
fixed, the useful disagreement about a plan is about coverage of a known list —
which the critics below produce by reading the one plan adversarially. Two plans
with no shared vocabulary would need a third agent to choose between them, and
that agent would be the unreviewed author again.

The agent's output is mirrored to your terminal as it arrives, prefixed with
`|`, so a long planning run is visibly working rather than looking hung. The
full transcript is written to disk either way; `--quiet` keeps it to the file.

The agent's freedom stops at the schema. Every field is validated before it
reaches project state: a task with no stated bar, a dependency on a task that
does not exist, a cycle, or a design section the document never contained is
rejected with the offending field named. Writ assigns the ids — whatever the
plan proposed is kept only to rewire its dependencies onto real ones.

The raw plan and the full transcript stay under `.writ/plans/<plan-id>/`. If a
plan is nearly right, edit `plan.json` and re-import it with `--from-plan`
instead of paying for another run.

| Flag | Effect |
|---|---|
| `--agent CMD` | planning agent (default `pi`) |
| `--model NAME` | model for the planning agent |
| `--instructions T` | extra guidance: scope, priorities, constraints |
| `--dry-run` | print the prompt (or preview an imported plan), write nothing |
| `--from-plan PATH` | import a plan artifact, run no agent |
| `--extract` | skip the agent entirely (see below) |
| `--timeout S` | kill the planner after S seconds (default 1800) |
| `--cwd D` | working directory for the agent (default: `--root`) |
| `--quiet` | do not mirror the agent's output to the terminal |
| `--chain` | order unordered tasks linearly in plan order (old default) |
| `--no-gates` | commit the plan without milestone or final gates |
| `--critics [NAMES]` | after committing, have independent critics read it (all five if unnamed) |
| `--critic-agent CMD` | agent for the critics (default: the planning agent) |
| `--critic-model NAME` | model for the critics |
| `--append` | plan additional work alongside an existing plan |
| `--force` | replace the existing plan |
| `-- <args>` | everything after `--` is passed to the agent |

With `--append`, the prompt lists the tasks that already exist and their status,
so the agent plans only what is missing and can depend on what is already there.

Dependencies come from the plan, and only from the plan. A task the plan left
unordered is left unordered — writ used to fall back to a linear chain in plan
order, which meant an edge nobody stated looked exactly like an edge someone
reasoned about, and a plan with a missing edge ran correctly by accident until the
day it was parallelised. The planner is now told to state every edge it needs.
`--chain` restores the old behaviour; re-wire anything with
`writ task <id> --depends`.

### The plan is a reviewed artifact

A plan can be structurally perfect and still be wrong: complete in shape,
infeasible in practice, or quietly missing an obligation the design stated. So
committing a plan is not the same as accepting it.

The pipeline's first stage produces a **requirement inventory** before any task —
every obligation the document states, one per entry, each with the heading it came
from. Tasks then name the requirements they discharge. That is what makes coverage
checkable rather than a matter of reading both documents side by side:

```bash
writ coverage                 # requirement → tasks → state
writ coverage --uncovered     # only the holes
```

A requirement nothing implements is a finding. So is a criterion nothing could
check (`"it works"`), a task named after the whole project, two unordered tasks
owning the same file, a fence that contradicts itself, and a plan whose tasks
never meet. `writ check` runs all of it:

```bash
writ check                    # re-check the committed plan
writ check --all              # including findings already dealt with
writ list findings --open     # the ledger
```

Findings carry a severity. Only an `error` blocks: the plan is held at
`needs-approval` and `writ run` refuses to start it. Warnings and notes are
recorded and readable and do not stop anything.

A clean check does **not** approve the plan. Those are different claims — "nothing
writ can prove is wrong with this" is much weaker than "somebody signed this off",
and every defect a deterministic check cannot see (an omitted requirement, an edge
that is legal but incorrect, a criterion nothing can demonstrate) passes a clean
check by construction. So approval takes an actor:

```bash
writ approve --by ada --reason "read it through"
writ approve --force --reason "known gap, shipping the spike"
```

For automation that has to get from a document to a running graph unattended:

```bash
writ plan docs/design.md --auto-approve
```

which approves the plan only when nothing blocking stands against it. It is not a
silent `--force`: a blocking finding still holds the plan, because overruling
writ's own objection is a judgement and the record has to say whose.

Or answer one finding at a time, which is the more useful shape when you disagree
with a particular objection rather than with all of them:

```bash
writ show F-0007                                    # what was said, and by whom
writ set F-0007 accepted --reason "known, shipping" # stands, run anyway
writ set F-0007 declined --reason "served by /stats" # the reviewer is wrong
```

Either way the objections survive. `--force` marks every open finding `accepted`
rather than deleting it, with who accepted it and why, so a later reader can see
the plan ran with known gaps and which ones they were. Both single answers need a
reason too: an accepted finding without one is the silent ignoring this exists to
prevent, and a declined one without one is an unargued assertion that the reviewer
was wrong. Neither can be set `resolved` — that word is earned when a check or a
gate demonstrates the outcome the finding asked for.

The ledger keeps ids across re-checks: a finding that is still true keeps `F-0007`
and its first-seen revision, one that has gone away is closed as `resolved` rather
than vanishing, and one that comes back is reopened. "This has been objected to
since revision 2" is a thing the plan can tell you.

### Critics

Writ's own checks are deterministic, which is their limit: they can prove a
criterion names no command, but not that a task is the wrong task. "Add caching to
the resolver" passes every structural check and may still be sized wrong, fenced
wrong, and resting on an assumption the design never made.

That needs a reader, and it must not be the author — a planner asked to review its
own plan already made every call it would be checking. So `writ critique` runs five
agents that did not write it, each with one question:

| critic | asks |
| --- | --- |
| `coverage` | does this build what the design asked for, or something adjacent? |
| `dependency` | does the graph's shape reflect real contracts between tasks? |
| `scope` | is each task one bounded piece of work with a real fence? |
| `acceptance` | could a second party tell, from the criteria alone, that a task is done? |
| `feasibility` | does this plan survive contact with the repository? |

```bash
writ critique                             # all five, over the committed plan
writ critique --critics coverage,scope    # just those
writ plan design.md --critics             # plan and critique in one pass
```

Five narrow reviews find more than one general one: a reviewer asked about
everything grades the plan as a whole and reports the first thing it notices, while
a reviewer asked only about dependency edges has to go and look at every edge. Two
critics flagging one task from different angles is signal a single verdict cannot
produce.

Critics report findings, not rewrites — same rule as a gate. Their findings go into
the same ledger with the same severities and the same dispositions, so a blocking
critic finding holds the plan exactly as a structural one does, and `writ check`,
`writ set` and `writ approve` need no new vocabulary for them. A critic that fails
does not fail the review; it is recorded as failed by name, rather than the review
quietly reducing to whoever succeeded. Finding nothing is a legitimate result.

A review is tied to the plan revision it read, so `writ check` can tell you the
critics passed a plan that has since been repaired.

### Gates, and repair

Every task passing its own review does not mean the milestone works. The defects
that survive a per-task review are the ones between tasks: an interface each side
implemented differently, an obligation both assumed the other had.

So a plan gets a **gate** per milestone and one at the end. A gate is an ordinary
task with `kind: "gate"` — it lives in the same graph, the same scheduler walks it,
the same dependency rules order it — except that it writes no code. It reads the
requirement inventory and the seams, and returns `pass`, `needs-repair`, or
`needs-decision`.

```bash
writ list gates               # what each gate has decided
writ list repairs             # every time a gate asked for the plan to change
```

A gate that fails does not fail the graph. What has gone wrong is the *plan*, so
its findings go to the ledger, writ asks a repair planner for a patch, and the
gate waits. The patch may add tasks and add edges — nothing else. It cannot weaken
an acceptance criterion, drop a requirement, delete a task, or touch a task an
agent is working on; writ validates every one of those before applying any of it,
and a refused patch is returned to the planner with its reasons.

The repair tasks go **in front of** the gate: they depend on the completed work,
and the gate gains a dependency on them. Downstream keeps waiting on the gate, the
graph stays acyclic however many rounds it takes, and the gate is asked again on
the repaired code. A finding closes when the gate passes on that code — not when
its repair task reports completion.

Both loops are bounded. A gate that has asked for repair twice, or whose findings
keep coming back, stops for a human rather than cycling; so does a planner whose
patches writ keeps refusing. `writ run` reports a held gate as waiting, not as
failed, because the work behind it is not broken — it is parked on a decision.

### Extraction fallback

`writ plan --extract` uses the old deterministic parser: level-2 headings become
milestones, level-3 headings become tasks, and acceptance criteria are lifted
from explicit gate markers — `**Pass:**`, `**Gate:**`, `Acceptance:` — split
into individually checkable bars. Sections with no stated gate get generic
criteria, so you can see which parts of the design never defined "done". It
invents nothing, and it judges nothing. Useful when the document is already
structured as a task list, or when you want no agent in the loop.

`--level N` and `--flat` apply to this mode only.

## Task lifecycle

```
planned ──(deps complete)──> ready ──dispatch──> running
   ▲                                                │
   │                                     agent writes a verdict
   │                                                │
   │                ┌───────────────────────────────┼──────────────┐
   │                ▼                               ▼              ▼
   │         awaiting-review                     failed         blocked
   │                │
   │             review ──> reviewing ──┬──> completed  (reviewer accepted)
   │                                    ├──> planned    (rejected, rework left)
   └────────────────────────────────────┘
                                        └──> failed     (rejected, budget spent)
```

`ready` is derived from the DAG, never stored.

A rejection returns the task to the queue rather than ending it, and the next
agent on it is handed the review — see [Rework](#rework).

If the process working on a task dies, the task goes back to the last status it
can be resumed from: `running` returns to `planned`, and `reviewing` returns to
`awaiting-review`. The next `writ run` (or `writ cancel` with no id) does that
reconciliation.

`blocked` is the one status nothing clears on its own, so an agent that reports it
must say what stopped it, and writ keeps that sentence on the task. `writ show`
prints it beside the status and the dashboard leads the panel with it, because a
task blocked by its own report has no unsatisfied dependency — every dependency
reads as met, and without the reason the only next step visible to a reader is the
word "blocked".

### Statuses are set by agents, not by hand

The implementing agent reports a structured verdict; a reviewer agent that did
not write the code checks it. Between them they own every status change that
represents a judgement about the work:

- the **implementing agent** can pass criteria and reach `awaiting-review`. It
  cannot mark its own task `completed` — an agent grading its own homework is
  not evidence.
- the **reviewer agent** re-runs the tests and re-reads the diff, and its
  decision is what produces `completed`, another attempt, or `failed`.

So `writ set` covers only workflow moves, and has no `completed` with or without
`--force`. A status is a value rather than a verb — one command for every
transition, so the legal ones live in one place and `--help` lists them:

```bash
writ set M01-001 running
writ set M01-001 blocked --evidence "waiting on the storage decision"
```

Two values are absent on purpose: `ready` is derived from the DAG, and `completed`
belongs to the review flow above. Because ids carry their own type, the same verb
rules on a decision an agent proposed or disposes of a finding — see
[the decision log](#the-decision-log).

### The verdict

Every dispatched agent is told to write `verdict.json` into its run directory:

```json
{
  "outcome": "complete",
  "summary": "what changed and what it now does",
  "criteria": [
    {"number": 1, "status": "passed",
     "evidence": "python3 -m pytest -q -> 5 passed"}
  ],
  "notes": "assumptions, deviations, risks"
}
```

Writ validates it before applying it. A criterion marked `passed` with no
evidence is rejected, as is a verdict about criteria the task does not have.
Rejected verdicts leave the task untouched and print why. An `outcome` that the
verdict's own criteria contradict is lowered to match them rather than rejected,
since the criteria are the part carrying evidence — and so is one that claims
success while saying nothing about a criterion, because leaving a bar out is not a
way of meeting it.

It is read from the path the agent was given, or from JSON printed to stdout in a
fenced block. Failing both, writ looks for a verdict-shaped file the run wrote
elsewhere in the project — agents invent their own conventions, and a complete
report at the wrong path is still a report. A file found that way must postdate
the run and validate for its role, and where it came from is recorded, because
the fix for an agent that ignores the path is in the prompt.

**An exit code is not a verdict.** A process can exit 0 having done nothing, so
a run that produces no usable verdict moves no criterion and the transcript is
left for you to read. Where the task lands depends on what was lost: an
implementation returns to `planned`, while a lost review leaves the task at
`awaiting-review`, because the code still stands and only the judgement is
missing. Conversely a non-zero exit with a valid verdict still records the
criteria the agent did meet.

### Rework

A reviewer's rejection is a finding, not a dead end, so it buys the task another
attempt instead of ending it:

```
writ review M01-001 --agent codex
...
M01-001 -> planned (0/3 criteria passed, judged by the reviewer)
sent back for rework (1 of 2): the next agent on this task is given this review
next: writ dispatch M01-001
```

The task returns to `planned` — back in the ready set, with the same criteria to
meet — and `writ run` picks it up in the same session it was rejected in:

```
dispatch M01-001  ->  claude -p --permission-mode ...
         ? M01-001  awaiting-review  3/3
review   M01-001  ->  codex exec --full-auto
         · M01-001  rework 1/2  0/3  unmet 1, 2, 3
           the test named in criterion 2 asserts nothing
rework   M01-001  ->  claude -p --permission-mode ...
         ? M01-001  awaiting-review  3/3
review   M01-001  ->  codex exec --full-auto
         + M01-001  completed  3/3
```

**The rejection travels with the task.** This is the part that makes another
attempt worth anything: a re-dispatch with the original prompt is the same prompt,
and an agent given the same prompt has every reason to write the same code. So the
reworking agent is told, before the design excerpt, what happened:

```
THIS TASK WAS ALREADY IMPLEMENTED AND THE REVIEW REJECTED IT. You are attempt 2,
and writ allows 2 rework attempts before the task is left failed for a human.

The code from the previous attempt is still in the working tree. You are fixing
it, not starting over — read it first, and keep whatever the review did not
object to.

reviewer(codex) reviewed it and rejected it
  the test named in criterion 2 asserts nothing

What it found, by criterion:
  2. reviewer marked this failed
     reviewer: ran pytest -q; test_merges_lists passes with the body commented out
     the previous attempt claimed this passed: pytest -q -> 12 passed
```

Both sides of the disagreement, on purpose. The previous agent's claim is not
evidence, but the *contradiction* is informative: a criterion the implementer said
it verified and the reviewer found broken points at a test that does not test what
it says, while one the implementer never claimed points at work that was simply
not done. Those need different second attempts, and an agent given only the
rejection cannot tell them apart.

The next reviewer is told too, as a checklist rather than a conclusion — it still
judges for itself, and a previous rejection is not evidence either:

```
This work was rejected 1 time already, most recently by reviewer(codex), and has
been reworked since. What that review objected to:
  2. failed: ran pytest -q; test_merges_lists passes with the body commented out

Check those specifically, on top of the criteria. They are the bars this attempt
exists to clear, and an unaddressed one is a rejection.
```

**The budget is finite.** `--max-rework` defaults to 2, so a task gets three
implementation attempts in total. An agent that cannot satisfy a reviewer in three
tries is not going to be argued into it by a fourth — at that point the task, its
criteria, or the design is what is wrong, and that is a human's call:

```
M01-001 -> failed (0/3 criteria passed, judged by the reviewer)
rework budget of 2 attempts is spent, so this is left failed for you
next: writ show M01-001   # see what it could not meet
```

`--max-rework 0` fails on the first rejection. Either way the rejection is kept on
the task, so `writ show` answers the question a bare `failed` cannot — what it was
sent back for, how many times, and by whom:

```
rework: attempt 2 of 2, rejected by reviewer(codex) at 2026-02-11T09:22:41+00:00 — budget spent, left failed
  2. failed: ran pytest -q; test_merges_lists passes with the body commented out
  notes: the list-merge path is still unreachable
```

Two things rework deliberately does not change. Attempts do not count against
`--max-tasks`, which caps how much of the graph a session takes on rather than how
many agents it runs — refusing a rework would leave a task failed for want of a
slot it had already spent. And `awaiting-review` still does not unblock dependents:
work built on a task whose review might send it back is work built on a premise
that has not been checked.

### The decision log

A task's verdict also carries the choices the design document did not make. An
agent implementing "merge these config files, later files winning" has to decide
what happens when a list meets a list — the document does not say, and whichever
way it goes, the next agent inherits it and cannot tell a deliberate choice from
an accident. So the verdict has a `decisions` array, and entries land in the log
attributed to the agent that made them.

Reviewers propose too, which is where it earns its keep: a choice visible in the
diff but absent from the implementer's report is exactly what an independent
reader catches.

**Proposals are not commitments.** They land as `proposed` and do nothing until
you rule on them:

```bash
writ list decisions --proposed
writ show D-0001                       # the choice, its context, its consequences
writ set D-0001 active                 # binding from here on
writ set D-0002 rejected --reason "packaging is M03, not this task's call"
```

An agent may report what it decided; it may not commit the project on its own
authority. That is the same split as acceptance criteria — the agent claims,
something else decides — and it is why there is no command for writing a decision
by hand. A record exists because someone made the choice while doing the work.

Rejected proposals stay in the log with their reason. That an agent proposed
something and was turned down is worth knowing, and deleting it invites the same
proposal next week.

Records are append-only. Confirming a replacement writes a new entry and marks
the old one `superseded`; nothing is edited in place:

```bash
writ set D-0007 active --supersedes D-0001
```

### When a human needs the last word

`writ override <id> <status> --reason ...` does what `set` and `accept` used to,
and is the only way to reach `completed` by hand. It requires a reason and
attributes everything it writes to `operator` rather than to an agent, so the
evidence log stays honest about who decided what:

```bash
writ override M01-001 completed --reason "verified on staging" --accept 1 --accept 2
writ override M01-002 failed --reason "criterion 2 regressed" --accept 2=failed
```

## Configuration

Every agent and model writ uses can be given on the command line, and the ones
that matter are the ones easiest to forget. `--reviewer` above all: leave it off
and the review runs on the model that wrote the code.

So a project can write its choices down once, in `.writ/config.json`:

```json
{
  "agents": {
    "planner":     {"command": "claude", "model": "opus", "timeout": 1800},
    "critic":      {"command": "codex",  "model": "gpt-5-codex"},
    "implementer": {"command": "claude", "model": "sonnet"},
    "reviewer":    {"command": "codex",  "model": "gpt-5-codex"}
  },
  "run": {"parallel": 3, "order": "depth", "max_rework": 2}
}
```

Then `writ run` with no flags uses all of it. `writ init` writes this file for
you, holding every field writ accepts at writ's own default, so editing it is a
matter of changing a value rather than working out what can be set. A comment
block at the top explains it: one line per agent, one per `run` setting, and what
each `null` falls back to — so the table below is in the project rather than only
here.
`config.example.json` in this repository is a filled-in example.

Four roles, because that is how many writ actually distinguishes:

| role | used by | falls back to |
|---|---|---|
| `planner` | `writ plan` — its analysis stages and its synthesis | `pi` |
| `critic` | `writ critique`, `writ plan --critics` | the planning agent |
| `implementer` | `writ run`, `writ dispatch`, **and gates and repair planners** | `pi` |
| `reviewer` | `writ review`, `writ run` | the implementing agent |

Each takes `command`, `model`, and `timeout`. Under `run`: `parallel`, `order`,
and `max_rework`, which `writ review` honours too since it is the same budget.

Note where gates sit. A gate judges whether integrated work adds up, which is a
review, but it runs on the implementing agent — so the model that wrote the code
is the one asked whether the milestone holds. Pass `--reviewer` and it still only
affects task review. Worth knowing until it is worth changing.

**A flag always wins.** The config says what this project decided; a flag says
what you are doing right now. `writ agents` prints what is in effect:

```
ROLE         COMMAND                   MODEL        TIMEOUT
-----------  ------------------------  -----------  -------
planner      claude                    opus         1800
critic       codex                     gpt-5-codex  -
implementer  claude                    sonnet       -
reviewer     codex                     gpt-5-codex  1800

from .writ/config.json; a flag overrides any of it
```

**An unknown key is an error, not a shrug.** A config is hand-edited, so a typo
in one is as likely as a typo in a flag — and a silently ignored `"reviewr"` would
leave review running on the implementing model while the file on disk says it does
not. So it is refused, with the name it was probably reaching for:

```
writ: .writ/config.json: unknown role 'reviewr' (did you mean 'reviewer'?);
known roles: planner, critic, implementer, reviewer
```

That happens before any agent starts, not three tasks into a run.

**`null` means writ's default**, the same as leaving the key out. That is what
lets the generated config name every field: an unset `implementer.timeout` means
*no* timeout, and an unset `reviewer.command` means the implementing agent, and
neither has a value that says so.

Any key beginning with `_` is a comment, since JSON has nowhere else to put one —
useful for the reason behind a choice, which outlives the choice:

```json
{"agents": {
  "_reviewer": "codex caught the seam bugs claude kept missing",
  "reviewer": {"command": "codex"}
}}
```

`writ init` creates this file, and that is the only time writ writes it. Every
value in the generated file is the one writ would have used anyway, so a project
that never opens it runs exactly as it would with no config at all — including
the reviewer, which is `null` rather than pinned to an agent, because its real
default is the implementing agent and no value says that. After that it is
yours: nothing reformats or edits it, and `init --force` resets project state
while keeping it — how this project runs agents is still true of the next plan
written in it. Note that `.writ/` is usually gitignored, so this is a
per-checkout file rather than a shared one.

## Commands

Twenty commands, organized by what you are doing rather than what type it
operates on.

**Set up**

| Command | Purpose |
|---|---|
| `writ init [--force]` | create the project store |
| `writ plan <doc>` | analyse the document and repo in stages, then synthesize milestones, tasks, acceptance criteria |
| `writ plan <doc> --stage NAME` | run the analyses up to that stage and stop, committing nothing |
| `writ plan <doc> --plan-id ID [--refresh]` | resume a pipeline, reusing (or redoing) the artifacts it already wrote |
| `writ plan <doc> --no-stages` | the older single-shot planner: one agent, every judgement at once |
| `writ plan <doc> --auto-approve` | approve on commit when nothing blocking stands against it |
| `writ check [--all] [--quiet]` | re-check the committed plan and report what stands against it |
| `writ critique [--critics NAMES] [--agent CMD] [--model M] [--timeout S] [--cwd D] [--quiet]` | independent critics read the plan and report findings |
| `writ approve [--by WHO] [--reason R] [--force]` | sign the plan off, which is what `writ run` requires |

**Look**

| Command | Purpose |
|---|---|
| `writ status [--watch] [--interval S] [--until-idle] [--no-clear]` | progress, ready work, live runs |
| `writ list [tasks\|milestones\|runs\|decisions\|findings\|requirements\|gates\|repairs] [--status S] [--milestone M] [--task T] [--ready] [--awaiting-review] [--proposed] [--open] [--uncovered] [--active] [--limit N]` | any collection |
| `writ coverage [--uncovered] [--requirement ID]` | requirement → tasks → state |
| `writ show <id> [--verbose] [--prompt]` | any single thing |
| `writ graph [--levels] [--verbose] [--dot]` | the dependency DAG |
| `writ serve [--port N] [--host H] [--no-open]` | a live web view of the whole project |
| `writ logs <run-id\|task-id> [--follow] [--stderr] [--tail N]` | agent output |

**Change**

| Command | Purpose |
|---|---|
| `writ set <id> <status> [--evidence T] [--reason R] [--by WHO] [--supersedes ID] [--force]` | a task's workflow status, a ruling on a proposed decision, or a finding's disposition |
| `writ override <id> <status> --reason R [--accept N[=STATUS]]` | human judgement, attributed to you |
| `writ task [id] [--title T] [--milestone M] [--depends] [--acceptance] [--allow] [--forbid]` | create, or amend with an id |

**Run agents**

| Command | Purpose |
|---|---|
| `writ run [--parallel N] [--order id\|depth\|unlocks] [--max-tasks N] [--max-rework N] [--agent CMD] [--model M] [--reviewer CMD] [--reviewer-model M] [--reviewer-timeout S] [--timeout S] [--cwd D] [--force] [--quiet] [--dry-run]` | walk the whole graph until it is done or stuck |
| `writ dispatch <id> [--agent CMD] [--model M] [--detach] [--force] [--timeout S] [--cwd D] [--quiet] [--dry-run] [-- args]` | an agent implements the task and reports a verdict |
| `writ review [id] [--agent CMD] [--model M] [--timeout S] [--cwd D] [--max-rework N] [--force] [--quiet] [--dry-run]` | a second agent verifies and signs off; no id reviews all awaiting |
| `writ cancel [run-id]` | stop a run, or reap dead ones when given no id |
| `writ agents [--agent CMD] [--model M]` | how writ invokes each agent headlessly |

Every command accepts `--root <project>` (default: current directory) and
`--json` for machine-readable output.

### Ids carry their own type

`show` and `logs` do not need to be told what kind of id they were handed — the
shape says it:

```bash
writ show M01                  # a milestone, its rollup, its tasks
writ show M01 --verbose        # every member task expanded in full
writ show M01-001              # a task: gates, fencing, runs, evidence
writ show M01-001-20260916T…   # one agent run
writ show M01-001-20260916T… --prompt   # exactly what that agent was given
writ show D-0001               # one decision
```

A task view shows both directions of the DAG (what it waits on, and what it
unblocks), the design section it came from, every run tagged by role
(`[agent]` / `[reviewer]`), and the evidence log attributed to whoever produced
each line. Each acceptance criterion carries the evidence behind it and the name
of the agent that judged it, so "2/2 passed" can always be traced back to the
commands someone actually ran.

### The graph is a shape, not a list

`writ graph` follows dependencies forwards, so the structure is legible at a
glance:

```
> M01-001  Foundations
├─ · M01-002  Schema
│  ├─ · M01-004  Auth
│  │  └─ ↩ M01-007
│  └─ ↩ M01-005
└─ · M01-003  HTTP layer
   ├─ · M01-005  Handlers
   │  └─ ↩ M01-007
   └─ · M01-006  Rate limit
      └─ · M01-007  Deploy

7 tasks, 4 deep, up to 3 in parallel   ↩ joins a task drawn under its last dependency
```

A DAG is not a tree: a task can be reached by several paths. Expanding it under
each one would draw the same work repeatedly and imply it happens more than once,
so each task is expanded exactly once — beneath the dependency that comes last,
the one actually gating it — and every other path shows `↩` and the id. That
placement is what makes the drawing readable as a plan: a task never appears
before something it waits on.

`--levels` groups by dependency depth instead, which is the better read on a large
graph and answers a different question: what could run at the same time.

```
level 3  (3 tasks)
  · M01-004  Auth   after M01-002
  · M01-005  Handlers   after M01-002, M01-003
  · M01-006  Rate limit   after M01-003
```

`--verbose` adds each task's status and acceptance count, and `--json` gives the
levels plus both edge directions per task.

### Rendering the graph as an image

`--dot` emits graphviz, so pipe it to `dot`:

```bash
writ graph --dot | dot -Tsvg -o graph.svg
writ graph --dot | dot -Tpng -o graph.png       # for pasting into a review
writ graph --dot > graph.dot                    # keep the source
```

Install graphviz first: `brew install graphviz`, `apt install graphviz`, or
`choco install graphviz`. On macOS, `open graph.svg` opens it in a browser.

Nodes carry the same status the terminal view shows — the status name, the
acceptance count, a muted fill by state, and a heavier border on tasks that can
start now — because progress is the reason to look at a picture of the graph.
Tasks are clustered by milestone.

`dot` is the right engine here; it ranks nodes by dependency depth, which is what
the graph means. `neato` and `circo` will render it but arrange it by other
criteria, losing the ordering.

For a graph too wide for a page, `-Grankdir=TB` stacks it vertically, and
`unflatten` before `dot` evens out the aspect ratio:

```bash
writ graph --dot | unflatten -l3 | dot -Tsvg -o graph.svg
```

## Watching it work

A command answers one question. That is right for driving work and wrong for
watching it: during a long run the questions come faster than you can type them,
and they are about relationships — which task is this run for, what was that agent
actually told, which criterion did the reviewer reject.

`writ serve` puts all of it on one page and follows the store:

```bash
writ serve                    # opens a browser on localhost:8731
writ serve --no-open          # just print the url
writ serve --port 9000
```

```
writ serve on http://localhost:8731/
read-only; following .writ/state.json  (^C to stop)
```

Leave it open in one window and `writ run` in another. Nothing needs reloading:
statuses change as agents work, live ones pulse, and the header counts move.

Seven views, reachable by number key:

| View | What it answers |
| --- | --- |
| Overview | where the project stands, what is running, what waits on you |
| Plan | whether the plan is approved, what stands against it, what every requirement has to show |
| Graph | the shape of the work and what could run at once |
| Tasks | every task, filterable, with full detail on click |
| Milestones | progress against the plan's own structure |
| Runs | every agent invocation, newest first |
| Decisions | forks an agent hit that a human has not settled |

The Plan view puts the three records that are really one question on one page: the
plan's status says whether work may start, the findings say why not, and the
requirement matrix says whether what is being built is what was asked for. Reading
them apart is how a plan gets approved with a requirement nobody implemented — the
status looked fine on its own. Gates held for a human come first when there are
any, because that is the state where nothing is running, nothing is broken, and
nothing will change until a person acts.

It is read-only, like the rest of the dashboard. Disposing of a finding takes a
reason, and a reason is something to type deliberately, so the page shows the
command rather than offering a button.

### What an agent was actually told

The observability payoff is the run panel. A run holds three things that exist on
disk and are otherwise a `cat` away at best:

- **the prompt** — exactly what the agent was given, which is how you find out why
  it did something strange
- **stdout and stderr** — what it printed while working
- **the verdict** — what it claimed, criterion by criterion, with its evidence

Click any run to get all four as tabs, with the prompt first, because the usual
question about a surprising run is what it was told. A task's panel shows the same
from the other direction: each acceptance criterion with the evidence an agent
gave for it, who judged it, and the runs that touched it.

Failures explain themselves. The most confusing thing writ can do is exit 0 and
move nothing, which happens when an agent never writes its verdict:

```
PROBLEM
exited without writing a usable verdict. Its acceptance criteria were left
untouched, and the task was returned to the queue rather than judged
```

What became of the task is read off the run, not assumed, because it is not the
same answer every time. A reviewer that fails to report leaves a finished
implementation standing, so its task holds at `awaiting-review` and the panel says
so — telling that reader the task went back to the queue would send them to
re-dispatch work that is already done. Only a lost *implementation* returns to the
queue.

When the transcript is empty too, writ says so rather than sending you to read
nothing. An agent CLI that cannot reach its model often reports that as exit 0
with no output, so a silent run is a failed invocation, not a skipped report:

```
PROBLEM
exited without writing a usable verdict — and printed no output at all, so it
most likely never reached a model (unknown model id, missing provider
credentials, or exhausted quota)
```

The opposite shape of that failure is a transcript full of work that still judged
nothing, because the agent's last act was a tool call that got printed instead of
run. A model that garbles its own call syntax ends its turn as if it had merely
spoken, and the harness exits 0 having done nothing. Reading the transcript for a
reason the agent declined to report finds none, so writ names the fragment at the
end of it:

```
PROBLEM
exited without writing a usable verdict — and its transcript ends in a tool call
that was printed rather than made, so the model garbled the call syntax and the
turn ended without it doing the work
```

Nothing in the prompt causes that, and the remedy is a model whose tool calling is
more reliable.

A verdict that was written but rejected says which field was wrong:

```
PROBLEM
.writ/runs/M01-002-.../verdict.json: outcome is 'blocked' but blocked_on is empty
```

A headline claim the criteria under it contradict is lowered rather than thrown
away. An agent that meets three bars of four, says which with evidence, and then
heads its report `complete` got one field wrong and three right; writ records
`incomplete`, keeps the evidence for the three, and says what it did:

```
PROBLEM
outcome was 'complete' but criteria 4 are not passed, so writ recorded 'incomplete'
```

Nothing the agent did not itself mark `passed` is ever credited, so the
adjustment only lowers a claim. It is on the task's own log too, because the next
agent to pick the task up is the one that needs to know.

A criterion left out of the report is read as the `pending` it should have been:

```
PROBLEM
decision was 'pass' but criteria 1, 2, 3 were not reported on, so nothing
confirmed them; writ recorded 'needs-repair'
```

Silence was the one way past this check, since a report mentioning no criteria had
none unmet — worst of all in a gate, which could sign off a milestone with
`"criteria": []`. Which bars a verdict owes a report on is a fact about the task
rather than about the report, so writ fills that in: the only way to claim
`complete`, `accept` or `pass` is to say something about every one. A gate lowered
this way also gets a blocking finding per skipped criterion, because a repair
planner reads nothing else.

### The graph, live

The graph view is laid out by dependency depth, left to right, so a column is work
that could run at once and the picture shows the parallelism the DAG allows. Each
node carries its id, title, status and acceptance count, with a bar for criteria
passed. Dashed edges are dependencies still outstanding; solid green ones are
satisfied. Hover for the full title and both edge directions.

A dependency naming a task that does not exist is called out rather than rendered
as a task that merely looks slow:

```
depends on M99-999, which does not exist — this task cannot become ready
```

### Three things it is not

- **Not a control panel.** It is read-only, and not by convention: there is no
  route that writes. A tab left open in a forgotten window cannot dispatch,
  cancel, override, or rule on a decision, which is also why it needs no auth
  token, no CSRF defence and no confirmations. Driving the project stays in the
  terminal, where the flags and the reasons are. Decisions are the clearest case:
  the page shows you the proposal and the command to rule on it, because a ruling
  should be typed deliberately with a reason attached.
- **Not a dependency.** `pip install writ` still pulls in nothing. The page is one
  compiled script and one stylesheet, served by `http.server` over server-sent
  events, and it works offline. The UI is written in TypeScript under `ui/src` and
  compiled to `writ/static` by `node ui/build.mjs`; that output is committed, and
  a test fails if it drifts from the source. Node is a contributor's tool, never a
  user's.
- **Not exposed.** It binds `127.0.0.1`, because the page has no authentication and
  does not need any while only this machine can reach it. `--host 0.0.0.0` works
  and warns: anyone who can route to the port can then read your design, task
  titles, agent prompts and logs.

A hidden tab stops following and says `paused (tab hidden)`, catching up when you
come back. Not an optimisation — a browser allows about six connections per
origin, and a held-open stream uses one, so a handful of forgotten writ tabs would
otherwise starve the next one you opened.

## Running the whole graph

`writ dispatch` and `writ review` each move one task one step. `writ run` is the
loop around them: it dispatches what is ready, reviews what gets reported, and
repeats until the graph is finished or nothing can move.

```bash
writ run                               # one agent at a time, to the end
writ run --parallel 3                  # three at a time where the graph allows
writ run --max-tasks 5                 # start at most five tasks, then stop
writ run --max-tasks 0                 # review what is waiting, start nothing
writ run --order depth                 # longest chain of work first
writ run --max-rework 0                # fail a task the first time it is rejected
writ run --dry-run                     # the projected walk, spending nothing
```

Work only runs when its dependencies are `completed`, and a task is only
`completed` by a reviewer. So `--parallel` is bounded by the shape of the graph,
not just the number: a chain of four tasks runs serially however high you set it,
and a fan of eight runs eight-wide.

Reviews are scheduled ahead of new dispatches. Reported work that nobody has
checked is what blocks everything downstream, so clearing it first keeps the
frontier moving. For the same reason `--max-tasks` caps how many tasks *start*,
but never refuses a review — stopping with work stuck at `awaiting-review` would
be worse than not having started it. Rework is exempt for the same reason: a task
the session already started gets its next attempt even once the budget is spent,
since refusing it would leave that task failed over a slot it had already used.

Use a different model for review than for implementation:

```bash
writ run --parallel 3 \
  --agent claude --model sonnet \
  --reviewer codex --reviewer-model gpt-5-codex
```

`--reviewer` defaults to `--agent`, which is convenient and weaker: a model
checking its own work agrees with itself more than it should.
`--reviewer-model` and `--reviewer-timeout` each apply whether or not `--reviewer`
was given, so review can be a different model of the same agent, or the same agent
on a longer leash. Set them once in
[`.writ/config.json`](#configuration) rather than on every invocation.

### How a task reaches an agent

One task, one step at a time. `writ run` does exactly this, repeatedly:

```
     the store                        the agent process
  .writ/state.json
         │
   1. select    ── the graph says M01-002 is ready
         │
   2. claim     ── status: planned -> running, owner_pid recorded
         │          (from here the task is no longer selectable)
         │
   3. prompt    ── runs/M01-002-…/prompt.txt
         │                    │
         │                    └── on stdin ──>  claude -p
         │                                        │  │
         │            stdout.log, stderr.log  <────┘  │
         │            (to disk, not your terminal)     │
         │                                              │
   4. verdict   <── runs/M01-002-…/verdict.json  <─────┘
         │
   5. apply     ── criteria marked from the verdict,
         │          status: running -> awaiting-review
         ▼
```

Step 2 is what makes concurrency safe. The claim is a write inside the same
advisory lock as every other write, so a task stops being selectable *before* its
agent starts rather than after. Two schedulers cannot both pick it up, and
neither can a `writ dispatch` running alongside.

Step 4 is what makes progress real. The task's status comes from the file the
agent wrote, not from its exit code — a process can exit 0 having done nothing.
No verdict means no criterion moves, and writ says so.

A review is the same five steps with a different prompt and a different landing
place: `awaiting-review -> reviewing`, then `completed`, back to `planned` for
rework, or `failed`. That is the only transition that produces `completed`, which
is why the loop must run both phases.

### How the DAG advances

Nothing walks the graph. Each completion changes one task's status, and `ready`
is recomputed from the DAG every time the scheduler looks:

```
              ready = planned AND every dependency completed
```

So the frontier moves as a consequence of work finishing, not because anything
tracks position. Take this graph:

```
> M01-001  Foundations
├─ · M01-002  Schema
│  ├─ · M01-004  Store
│  │  └─ ↩ M01-006
│  └─ ↩ M01-005
└─ · M01-003  Config
   └─ · M01-005  Handlers
      └─ · M01-006  Wire up

6 tasks, 4 deep, up to 2 in parallel
```

Watch what is selectable as completions accumulate:

| completed so far | ready next | why |
|---|---|---|
| — | `M01-001` | nothing else has its deps met |
| `M01-001` | `M01-002`, `M01-003` | both depend only on `M01-001` |
| … `+ M01-002` | `M01-003`, `M01-004` | `M01-004` opens; `M01-005` still waits on `M01-003` |
| … `+ M01-003` | `M01-004`, `M01-005` | `M01-005` joins both branches |
| … `+ M01-004`, `M01-005` | `M01-006` | the final join |

That table is `writ list --ready` at each point, and you can watch it move with
`writ status --watch` from another terminal while a run is going.

`awaiting-review` is deliberately not enough to open the next task. If it were,
an agent's own claim about its work would unblock the tasks built on top of it,
and a rejected verdict would mean unwinding work that had already started from a
false premise.

### How parallelism actually plays out

The graph above, run with `--parallel 2`, produces this — real output, not a
sketch:

```
running up to 2 agents at a time
logs: .writ/runs
─────────────────────────────────────────────────────────────
dispatch M01-001  ->  claude -p
         ? M01-001  awaiting-review  1/1
review   M01-001  ->  codex exec -
         + M01-001  completed  1/1
dispatch M01-002  ->  claude -p
dispatch M01-003  ->  claude -p
         ? M01-003  awaiting-review  1/1
review   M01-003  ->  codex exec -
         ? M01-002  awaiting-review  1/1
review   M01-002  ->  codex exec -
         + M01-003  completed  1/1
         + M01-002  completed  1/1
dispatch M01-004  ->  claude -p
dispatch M01-005  ->  claude -p
         ? M01-005  awaiting-review  1/1
review   M01-005  ->  codex exec -
         ? M01-004  awaiting-review  1/1
review   M01-004  ->  codex exec -
         + M01-005  completed  1/1
         + M01-004  completed  1/1
dispatch M01-006  ->  claude -p
         ? M01-006  awaiting-review  1/1
review   M01-006  ->  codex exec -
         + M01-006  completed  1/1
─────────────────────────────────────────────────────────────
ran 12 agents over 6 tasks in 5s
completed 6, failed 0
project 6/6 tasks complete
```

Three marks: `?` reported and awaiting review, `+` completed, `x` failed.

The timeline underneath it, from the recorded start and finish of each run:

```
  +0s  M01-001 dispatch ██
  +0s  M01-001 review   ████
  +1s  M01-002 dispatch     ████      two at once: both deps met, and
  +1s  M01-003 dispatch     ████      neither depends on the other
  +2s  M01-002 review           ██
  +2s  M01-003 review           ██
  +2s  M01-004 dispatch         ████
  +2s  M01-005 dispatch         ████
  +3s  M01-004 review               ████
  +3s  M01-005 review               ████
  +4s  M01-006 dispatch                 ██   the join: waited for both
  +4s  M01-006 review                   ████
```

The waves are the graph's width, not a batching strategy. The scheduler never
waits for a round to end: it refills the moment a slot frees. Give the same
graph agents that take unequal time and the phases stop lining up:

```
dispatch M01-002  ->  claude -p          (slow)
dispatch M01-003  ->  claude -p          (fast)
         ? M01-003  awaiting-review      M01-002 is still working
review   M01-003  ->  codex exec -
         + M01-003  completed
dispatch M01-004  ->  claude -p          its slot freed, so it starts now
         ? M01-002  awaiting-review
review   M01-002  ->  codex exec -
```

A review of one task and a dispatch of another run side by side. `--parallel N`
is a budget of concurrent agents, not a batch size, and it is spent on whatever
is most useful at that moment — a pending review first, then new work.
`--parallel 4` on this graph still peaks at 2, because that is as wide as the
graph gets.

Inside one process it works like this:

```
  scheduler thread                    worker pool (--parallel N)
  ────────────────                    ────────────────────────
  while a slot is free:
      select + claim  ───submit───>  run the agent, apply its verdict
  wait for any to finish  <───────  report the outcome
  repeat
```

One thread selects and claims; workers only run agents and record verdicts. Two
threads both asking "what is ready?" could answer with the same task, so the
question is only ever asked in one place.

### Reading the progress log

The log is one line per event, not a transcript. Several agents talking at once
is unreadable, so `writ run` reports transitions instead — what started, what it
produced, and what that changed:

```
dispatch M01-002  ->  claude -p                 an agent started
         ? M01-002  awaiting-review  3/3        it reported, three bars passed
review   M01-002  ->  codex exec -              a reviewer started
         · M01-002  rework 1/2  0/3  unmet 1,2  it rejected two of the three
           the retry path is untested           the reviewer's own words
rework   M01-002  ->  claude -p                 a second attempt, given the review
```

Flush-left lines are agents starting; indented lines are the store changing. The
counts are acceptance criteria, so `0/3  unmet 1, 2` names which bars are
still open without needing `writ show`.

A rejection reads as `rework 1/2` rather than as the bare `planned` it stores.
Both are true, and `planned` is the useless one to print: the reader's question
about a rejected task is whether anything happens next, and `planned` reads as
though it had never run. The re-dispatch is called `rework` for the same reason.

A failure carries the agent's one-line reason, because that is the line you
actually read when something goes wrong. A pass does not — there it would be
noise. Proposed decisions are reported as they happen, since they are a side
effect worth noticing:

```
         ? M01-001  awaiting-review  3/3
           proposed 2 decisions: Frames are length-prefixed; Timeouts are per-request
```

Problems that are not verdicts appear the same way — a timeout as `(exit 124)`, a
crash as its exit code, an unusable verdict with the parse error that rejected it:

```
         x M01-001  failed  0/3  (exit 124)
         · M01-001  planned  3/3  (…/verdict.json: decision must be one of accept, reject (got None))
```

The second is worth reading twice. A reviewer wrote something writ could not
parse, so no criterion moved and the task went back to `planned` — it kept the
three bars the implementer had already earned rather than losing them to a
reviewer's malformed file.

`--quiet` drops the started lines and keeps the transitions. `--json` emits the
same events as objects, each carrying `status`, `criteria`, `unmet`, `summary`,
and `decisions`, so a wrapper does not have to parse the text.

Full agent output is always on disk, whatever the log shows:

```bash
writ logs M01-002                      # what that agent actually printed
writ logs M01-002 --stderr
writ show M01-002 --verbose            # the verdict, per criterion, with evidence
```

### Which ready task goes first

When more tasks are ready than there are free slots, something has to choose.
`--order` picks the rule:

| order | starts | good for |
|---|---|---|
| `id` (default) | lowest task id | following the design document's own order |
| `depth` | longest chain of remaining work | wide pools, where depth is the limit |
| `unlocks` | the task the most others wait on | opening the graph early |

Take a graph with a shallow hub that unblocks three leaves, next to a four-deep
chain:

```
> M01-001  Root
├─ · M01-002  Hub (unblocks 3)
│  ├─ · M01-004  leaf A
│  ├─ · M01-005  leaf B
│  └─ · M01-006  leaf C
└─ · M01-003  Deep head
   └─ · M01-007  deep 2
      └─ · M01-008  deep 3
         └─ · M01-009  deep 4
```

The three orders disagree about what to do once the root is done:

```
--order id        M01-001  M01-002  M01-003  M01-004 …
--order depth     M01-001  M01-003  M01-007  M01-002 …   drives the chain
--order unlocks   M01-001  M01-002  M01-003  M01-007 …   opens the hub
```

On that graph at `--parallel 3` all three orders finish within a second of each
other — it is too small for the choice to matter. The gain shows up when a deep
chain is numbered *late*, so `id` saves it for last:

```
23 tasks: twelve shallow leaves (low ids) beside a ten-deep chain (high ids)
--parallel 4

  --order id        16-17s      the chain only starts once the leaves are done
  --order depth     12-13s      the chain runs from the start, leaves fill in
  --order unlocks   12-13s
```

That is roughly 22%, and about the best this buys. Across random graphs the
difference is under 1% at `--parallel 2`, around 5% at 4, and about 10% at 8 —
because below a wide pool the limit is total work divided by workers, and no
ordering changes how much work there is. Only once the pool can absorb the whole
ready set does the graph's depth start to bind.

So `id` stays the default. It is predictable: work proceeds roughly in the order
the design document laid out, and two runs over the same graph pick the same
tasks in the same sequence, which is worth more when reading a transcript than a
few percent of wall clock. Reach for `depth` when you are running wide and the
plan's numbering does not already put the long pole first.

An order only chooses among tasks that are *already* ready. It cannot make a task
runnable sooner, so no `--order` can produce a run the dependency rules would not
allow — and switching order between sessions is safe, since it is a preference for
one run rather than stored state.

### Preview before spending

`--dry-run` walks the graph in memory and prints the invocations it would make:

```
would run 8 agent invocations, up to 2 at a time:
   1. dispatch M01-001
   2. review   M01-001
   3. dispatch M02-001
   4. review   M02-001
   ...

a projection, not a promise: a rejected verdict changes what comes next
```

It assumes every review accepts. One rejection changes the rest of the walk,
which is why it is a projection and says so.

### Stopping and resuming

`writ run` is resumable because it holds no state of its own: everything it
decides from is in `.writ/state.json`, and every step is recorded before the next
one starts. Run it again and it picks up from wherever the graph got to.

Stopping has two levels. The first `^C` stops scheduling new work and lets the
agents already running finish, so their verdicts still count:

```
stopping: finishing the agents already running (^C again to kill)
         + M01-005  completed
         + M01-004  completed
──────────────────────────────────────────────────────────────
ran 4 agents over 2 tasks in 7s
completed 2, failed 0
project 5/6 tasks complete
ready to dispatch: M01-006

stopped early; `writ run` again picks up where this left off
```

A second `^C` kills them. A killed agent loses its own work, never the task's
place in the graph — the task returns to the queue rather than being recorded as
failed, because a deliberate stop and a failing agent mean different things.

If the machine dies outright, nothing gets to clean up. The next `writ run`
reconciles those records before it decides what to do:

```
resuming: reconciled 3 interrupted run(s)
```

An interrupted implementation goes back to `planned`. An interrupted *review*
goes back to `awaiting-review`, not `planned`: the work still stands, only the
judgement was lost, and re-implementing it would throw away a finished task.

One `writ run` at a time per project. A second refuses rather than racing it:

```
writ: another writ run is active (pid 4131). Wait for it, stop it, or pass
--force if you know it is gone.
```

A session file left behind by a killed run is not treated as active, so this
normally resolves itself; `--force` is for the case where it does not.

That check is a courtesy, not the actual safety net. Claiming a task happens
inside the same lock that guards every other write, so a task with a live agent
on it is refused whatever route you take — a forced second `writ run`, or a
`writ dispatch` alongside one:

```
writ: M01-001 already has a running agent (run M01-001-20260916T120150).
Wait for it, or stop it with `writ cancel M01-001-20260916T120150`.
```

A recorded-but-dead run does not count as live, or a crashed agent would hold its
task forever.

### When it stops early

A failed task parks everything downstream of it, and the summary says what:

```
ran 4 agents over 2 tasks in 12s
completed 0, failed 2
project 0/6 tasks complete
blocked by failed work: M01-003, M01-004, M01-005   (failed: M01-002)
```

The first list is transitive: if C waits on B waits on a failed A, both B and C
are reported, because both are equally stuck. The second names only what actually
failed, which is the id to go and look at — the rest are casualties, and a list
that mixed them read as though a task had failed when it had merely been waiting.

`writ run` exits 1 when anything failed, so it can be used in a script.

## Dispatch

The prompt is assembled from the authoritative documents, the task, its
acceptance criteria, any allow/forbid lists, the matching design section, a fixed
guardrail block (test-first, minimum change, no weakened invariants, no live
network), and the verdict contract: the exact path to write, the schema, and the
rule that a criterion marked `passed` needs re-runnable evidence. Earlier
attempts on the same task are included, so a retry can see what already failed.

Preview either prompt without running anything:

```bash
writ dispatch M01-001 --dry-run
writ review M01-001 --dry-run
```

The review prompt shows the implementer's claims and tells the reviewer to treat
them as claims. It gets them because a reviewer that cannot see the claim cannot
tell a misleading one from an honest one.

The prompt is written to the run directory and delivered to the agent on stdin,
and the agent's output is mirrored to your terminal as it arrives (`--quiet` to
suppress). Arguments after `--` are forwarded to the agent command:

```bash
writ dispatch M01-001 --agent claude --model sonnet
writ dispatch M01-001 --agent claude -- --dangerously-skip-permissions
```

`--detach` hands the run to a supervisor process that outlives the CLI, so you
can close the terminal and still get a recorded outcome. `--timeout` kills the
process group and records exit 124. If a machine dies mid-run, `writ cancel`
with no id reconciles the orphaned records.

`writ review` takes the same agent, model, and timeout flags. With no task id it
reviews everything currently awaiting review, which is the usual way to run it:

```bash
writ review                            # everything awaiting
writ list --awaiting-review            # see that queue first
```

Using a different model for review than for implementation is worth doing: an
independent check is only as independent as the thing performing it.

## Agent invocation

Every coding agent CLI opens an interactive session by default. Piping a prompt
into bare `pi` or `claude` does not run them headless — they wait on a terminal
that is not there, and the run hangs until the timeout kills it.

So Writ adds the non-interactive flag itself, and translates `--model` to
whatever each agent calls it:

```bash
writ agents                                   # the whole table
writ agents --agent codex --model gpt-5-codex  # codex exec --model gpt-5-codex -
```

| Agent | Invocation |
|---|---|
| `pi`, `claude`, `cursor-agent` | `-p` |
| `codex` | `exec -` (prompt on stdin) |
| `opencode` | `run` |
| `amp` | `-x` |
| `gemini` | none needed; a pipe is enough |

Explicit flags win. `--agent 'pi -p'` is not given a second `-p`, and
`--agent 'codex exec'` is not given a second `exec`. Any other command is passed
through untouched, with a warning that Writ cannot confirm it runs without a
terminal — add its own headless flag to `--agent`. When a run is killed on
timeout having produced no output at all, that is the first thing Writ suggests
checking.

## Tests

```bash
uv run --with pytest python -m pytest
```

723 tests, hermetic — the "agents" under test are short `python -c` commands, so
nothing touches the network.
