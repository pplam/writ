# Writ

Plan, dispatch, and track coding-agent work against a design document.

Writ takes a markdown design doc, has a coding agent turn it into a
dependency-ordered task DAG with explicit acceptance criteria, hands one task at
a time back to an agent, and keeps an append-only decision log. State is plain
files — JSON, markdown, and logs — under `<project>/.writ`.

The premise: an agent should never be asked to "implement the design". It gets
one bounded task, a stated bar, and a guardrail list. Everything it did stays
inspectable afterwards.

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

writ dispatch M01-001 --agent claude --detach
writ logs M01-001 --follow             # stream the agent's output
writ status --watch                    # from any other terminal, any time

writ accept M01-001 1
writ set M01-001 completed --evidence "go test ./... green"

writ decide "Fixture-only tests" \
  --decision "Automated tests never touch a live platform." \
  --context "Platform quotas are small and debugging consumes them." \
  --task M01-001
```

## Storage

```
<project>/.writ/
  state.json          milestones, tasks, runs, decisions
  decisions.md        human-readable mirror of the decision log
  plans/<plan-id>/
    prompt.txt        what the planning agent was asked
    plan.json         the plan it returned, before validation
    stdout.log
    stderr.log
  runs/<run-id>/
    prompt.txt        exactly what the agent was given
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
                        agent exits 0 ──────────────┤
                                                    ▼
                                    planned (awaiting acceptance sign-off)
                        agent exits non-zero ─────> failed
```

`ready` is derived, never stored. Two gates are enforced:

- a task cannot **start** while a dependency is incomplete;
- a task cannot **complete** while an acceptance criterion is unmet.

Both accept `--force`, which records the override rather than hiding it. A
successful agent run does **not** auto-complete the task — the agent's exit code
is evidence, not a verdict.

## Commands

Fourteen commands, organized by what you are doing rather than what type it
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
| `writ list [tasks\|milestones\|runs\|decisions] [--status S] [--milestone M] [--task T] [--ready] [--active] [--limit N]` | any collection |
| `writ show <id> [--verbose] [--prompt]` | any single thing |
| `writ graph [--dot]` | the dependency DAG |
| `writ logs <run-id\|task-id> [--follow] [--stderr] [--tail N]` | agent output |

**Change**

| Command | Purpose |
|---|---|
| `writ set <id> <status> [--evidence T] [--force]` | task status |
| `writ accept <id> <n> [status]` | per-criterion sign-off (defaults to `passed`) |
| `writ task [id] [--title T] [--milestone M] [--depends] [--acceptance] [--allow] [--forbid]` | create, or amend with an id |
| `writ decide <title> --decision D [--context] [--consequences] [--supersedes ID] [--task ID] [--export PATH]` | append a decision |

**Run agents**

| Command | Purpose |
|---|---|
| `writ dispatch <id> [--agent CMD] [--model M] [--detach] [--timeout S] [--cwd D] [--quiet] [--dry-run] [-- args]` | run an agent |
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
unblocks), per-criterion acceptance state, the design section it came from, every
run with its exit code, and the evidence log — including notes the planning agent
left about ambiguities it found.

### A status is a value, not a verb

```bash
writ set M01-001 running
writ set M01-001 completed --evidence "go test ./... green"
```

One command for every transition, so the legal values live in one place and
`--help` lists them. `ready` is absent on purpose: it is derived from the DAG and
never stored, so it cannot be set.

Records in the decision log are append-only. Superseding writes a new entry and
marks the old one `superseded`; nothing is edited in place.

## Dispatch

The prompt is assembled from the authoritative documents, the task, its
acceptance criteria, any allow/forbid lists, the matching design section, and a
fixed guardrail block (test-first, minimum change, no weakened invariants, no
live network, report assumptions and deviations). Preview it with:

```bash
writ dispatch M01-001 --dry-run
```

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
