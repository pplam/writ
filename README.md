# Writ

Plan, dispatch, and track coding-agent work against a design document.

Writ takes a markdown design doc, has a coding agent turn it into a
dependency-ordered task DAG with explicit acceptance criteria, hands one task at
a time back to an agent, and keeps an append-only decision log. State is plain
files — JSON, markdown, and logs — under `<project>/.writ`.

Two premises. An agent should never be asked to "implement the design": it gets
one bounded task, a stated bar, and a guardrail list. And whether that bar was
met is decided by agents, not typed in by a human — the agent that did the work
reports a structured verdict with evidence, and a reviewer agent that did not
write the code decides whether to sign it off. Everything either of them claimed
stays inspectable afterwards.

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
writ plan docs/design.md               # an agent reads the doc and the repo

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
  state.json          milestones, tasks, runs, decisions
  decisions.md        human-readable mirror of the decision log
  run.session         the pid of the active `writ run`, if any
  plans/<plan-id>/
    prompt.txt        what the planning agent was asked
    plan.json         the plan it returned, before validation
    stdout.log
    stderr.log
  runs/<run-id>/
    prompt.txt        exactly what the agent was given
    verdict.json      what it claimed, per criterion, with evidence
    stdout.log
    stderr.log
    supervisor.log    detached runs only
```

Writes are atomic (temp file + replace) and serialized by an advisory lock, so
a detached run and an interactive `writ status` never corrupt each other.

## Planning

`writ plan` hands the design document and the repository to a coding agent and
asks for a plan as JSON: milestones, tasks, checkable acceptance criteria, the
dependency edges between them, and the paths each task may touch. Planning is a
judgement call — which work is one bounded session, what the real bar is, what
must land first — and reading the repo is part of making it.

```bash
writ plan docs/design.md --dry-run     # print the planning prompt
writ plan docs/design.md               # plan, validate, commit
writ plan docs/design.md --agent claude --model opus
writ plan docs/design.md --instructions "storage layer first"
```

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
| `--quiet` | do not mirror the agent's output to the terminal |
| `--parallel` | leave tasks independent instead of chaining them |
| `--append` | plan additional work alongside an existing plan |
| `--force` | replace the existing plan |
| `-- <args>` | everything after `--` is passed to the agent |

With `--append`, the prompt lists the tasks that already exist and their status,
so the agent plans only what is missing and can depend on what is already there.

Dependencies come from the plan. Tasks the plan left unordered fall back to a
linear chain in plan order, since build order is usually load-bearing; use
`--parallel` to leave them independent. Re-wire anything with
`writ task <id> --depends`.

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
                                                    │
                                         agent writes a verdict
                                                    │
                    ┌───────────────────────────────┼──────────────┐
                    ▼                               ▼              ▼
             awaiting-review                     failed         blocked
                    │
                 review ──> reviewing ──┬──> completed   (reviewer accepted)
                                        └──> failed      (reviewer rejected)
```

`ready` is derived from the DAG, never stored.

If the process working on a task dies, the task goes back to the last status it
can be resumed from: `running` returns to `planned`, and `reviewing` returns to
`awaiting-review`. The next `writ run` (or `writ cancel` with no id) does that
reconciliation.

### Statuses are set by agents, not by hand

The implementing agent reports a structured verdict; a reviewer agent that did
not write the code checks it. Between them they own every status change that
represents a judgement about the work:

- the **implementing agent** can pass criteria and reach `awaiting-review`. It
  cannot mark its own task `completed` — an agent grading its own homework is
  not evidence.
- the **reviewer agent** re-runs the tests and re-reads the diff, and its
  decision is what produces `completed` or `failed`.

So `writ set` covers only workflow moves (`planned`, `running`, `blocked`,
`failed`). It has no `completed`, with or without `--force`.

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
evidence is rejected, as is an `outcome: complete` that any criterion
contradicts, or a verdict about criteria the task does not have. Rejected
verdicts leave the task untouched and print why.

**An exit code is not a verdict.** A process can exit 0 having done nothing, so
a run that produces no usable verdict moves no criterion; the task returns to
`planned` and the transcript is left for you to read. Conversely a non-zero exit
with a valid verdict still records the criteria the agent did meet.

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

## Commands

Fifteen commands, organized by what you are doing rather than what type it
operates on.

**Set up**

| Command | Purpose |
|---|---|
| `writ init [--force]` | create the project store |
| `writ plan <doc>` | have an agent derive milestones, tasks, acceptance criteria |

**Look**

| Command | Purpose |
|---|---|
| `writ status [--watch] [--interval S] [--until-idle]` | progress, ready work, live runs |
| `writ list [tasks\|milestones\|runs\|decisions] [--status S] [--milestone M] [--task T] [--ready] [--awaiting-review] [--proposed] [--active] [--limit N]` | any collection |
| `writ show <id> [--verbose] [--prompt]` | any single thing |
| `writ graph [--levels] [--verbose] [--dot]` | the dependency DAG |
| `writ logs <run-id\|task-id> [--follow] [--stderr] [--tail N]` | agent output |

**Change**

| Command | Purpose |
|---|---|
| `writ set <id> <status> [--evidence T] [--reason R] [--supersedes ID] [--force]` | a task's workflow status, or a ruling on a proposed decision |
| `writ override <id> <status> --reason R [--accept N[=STATUS]]` | human judgement, attributed to you |
| `writ task [id] [--title T] [--milestone M] [--depends] [--acceptance] [--allow] [--forbid]` | create, or amend with an id |

**Run agents**

| Command | Purpose |
|---|---|
| `writ run [--parallel N] [--max-tasks N] [--agent CMD] [--model M] [--reviewer CMD] [--reviewer-model M] [--timeout S] [--cwd D] [--force] [--quiet] [--dry-run]` | walk the whole graph until it is done or stuck |
| `writ dispatch <id> [--agent CMD] [--model M] [--detach] [--timeout S] [--cwd D] [--quiet] [--dry-run] [-- args]` | an agent implements the task and reports a verdict |
| `writ review [id] [--agent CMD] [--model M] [--timeout S] [--cwd D] [--force] [--quiet] [--dry-run]` | a second agent verifies and signs off; no id reviews all awaiting |
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

### A status is a value, not a verb

```bash
writ set M01-001 running
writ set M01-001 blocked --evidence "waiting on the storage decision"
```

One command for every transition, so the legal values live in one place and
`--help` lists them. Two are absent on purpose: `ready` is derived from the DAG,
and `completed` belongs to the review flow above.

Because ids carry their own type, the same verb rules on a decision an agent
proposed:

```bash
writ set D-0001 active
writ set D-0002 rejected --reason "packaging is M03, not this task's call"
```

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
be worse than not having started it.

Use a different model for review than for implementation:

```bash
writ run --parallel 3 \
  --agent claude --model sonnet \
  --reviewer codex --reviewer-model gpt-5-codex
```

`--reviewer` defaults to `--agent`, which is convenient and weaker: a model
checking its own work agrees with itself more than it should.

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
place: `awaiting-review -> reviewing`, then `completed` or `failed`. That is the
only transition that produces `completed`, which is why the loop must run both
phases.

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
         x M01-002  failed  0/3  unmet 1, 2, 3  it rejected all three
           the retry path is untested           the reviewer's own words
```

Flush-left lines are agents starting; indented lines are the store changing. The
counts are acceptance criteria, so `0/3  unmet 1, 2, 3` names which bars are
still open without needing `writ show`.

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
blocked by failed work: M01-002, M01-003
```

That list is transitive: if C waits on B waits on a failed A, both B and C are
reported, because both are equally stuck. `writ run` exits 1 when anything
failed, so it can be used in a script.

Output is a progress log rather than a transcript — with several agents
interleaved, mirroring their stdout would be unreadable. Each agent's full
output is on disk:

```bash
writ logs M01-003                      # what that agent actually did
writ logs M01-003 --stderr
```

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

72 tests, hermetic — the "agents" under test are short `python -c` commands, so
nothing touches the network.
