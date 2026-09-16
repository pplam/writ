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
