# Writ

Plan, dispatch, and track coding-agent work against a design document.

Writ takes a markdown design doc, turns it into a dependency-ordered task DAG
with explicit acceptance criteria, hands one task at a time to a coding agent,
and keeps an append-only decision log. State is plain files — JSON, markdown,
and logs — under `<project>/.writ`.

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
writ plan docs/design.md --dry-run     # preview
writ plan docs/design.md               # commit the plan

writ status                            # progress, ready work, live runs
writ next                              # what can start now
writ show M01-001                      # the task and its acceptance bars

writ dispatch M01-001 --agent claude --detach
writ logs M01-001 --follow             # stream the agent's output
writ status                            # from any other terminal, any time

writ accept M01-001 1 passed
writ complete M01-001 --evidence "go test ./... green"

writ decision add \
  --title "Fixture-only tests" \
  --decision "Automated tests never touch a live platform." \
  --context "Platform quotas are small and debugging consumes them." \
  --task M01-001
```

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

## Storage

```
<project>/.writ/
  state.json          milestones, tasks, runs, decisions
  decisions.md        human-readable mirror of the decision log
  runs/<run-id>/
    prompt.txt        exactly what the agent was given
    stdout.log
    stderr.log
    supervisor.log    detached runs only
```

Writes are atomic (temp file + replace) and serialized by an advisory lock, so
a detached run and an interactive `writ status` never corrupt each other.

## Planning

`writ plan` maps level-2 headings to milestones and level-3 headings to tasks.
Acceptance criteria are lifted from explicit gate markers in the document —
`**Pass:**`, `**Gate:**`, `Acceptance:` — split into individually checkable
bars. Sections with no stated gate get generic criteria, so you can see which
parts of the design never defined "done".

| Flag | Effect |
|---|---|
| `--dry-run` | print the plan, write nothing |
| `--level N` | heading level for milestones (default 2) |
| `--flat` | one task per milestone, ignore sub-headings |
| `--parallel` | leave tasks independent instead of chaining them |
| `--append` | add another document to an existing plan |
| `--force` | replace the existing plan |

Dependencies default to a linear chain in document order — build order is
usually load-bearing. Re-wire any task with `writ edit-task <id> --depends`.

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

**Plan and inspect**

| Command | Purpose |
|---|---|
| `writ init [--force]` | create the project store |
| `writ plan <doc>` | derive milestones, tasks, acceptance criteria |
| `writ status` | progress bars, counts, ready work, live runs |
| `writ tasks [--status S] [--milestone M] [--ready]` | list tasks |
| `writ milestones` | rollups derived from member tasks |
| `writ show <id>` | a task or milestone in full |
| `writ next [--limit N]` | dispatchable tasks |
| `writ graph [--dot]` | the dependency DAG |

**Change**

| Command | Purpose |
|---|---|
| `writ start\|complete\|fail\|block\|reset <id> [--evidence T] [--force]` | transitions |
| `writ accept <id> <n> passed\|failed\|pending` | per-criterion sign-off |
| `writ add-task <title> [--id] [--milestone] [--depends] [--acceptance] [--allow] [--forbid]` | hand-written task |
| `writ edit-task <id> [--title] [--depends] [--acceptance] [--allow] [--forbid]` | amend |

**Dispatch and monitor**

| Command | Purpose |
|---|---|
| `writ dispatch <id> --agent CMD [--detach] [--timeout S] [--cwd D] [--dry-run] [-- args]` | run an agent |
| `writ runs [--task T] [--active]` | run history |
| `writ run <run-id>` | one run in detail |
| `writ logs <run-id\|task-id> [--follow] [--stderr] [--tail N]` | agent output |
| `writ cancel <run-id>` | stop an active run |
| `writ reap` | reconcile runs whose process died |
| `writ watch [--interval S] [--once] [--until-idle]` | live status view |

**Decision log**

| Command | Purpose |
|---|---|
| `writ decision add --title T --decision D [--context] [--consequences] [--supersedes ID] [--task ID]` | append a record |
| `writ decision list [--active] [--task ID]` | list records |
| `writ decision show <id>` | one record |
| `writ decision export [--out FILE]` | render as markdown |

Records are append-only. Superseding writes a new entry and marks the old one
`superseded`; nothing is edited in place.

Every command accepts `--root <project>` (default: current directory) and
`--json` for machine-readable output.

## Dispatch

The prompt is assembled from the authoritative documents, the task, its
acceptance criteria, any allow/forbid lists, the matching design section, and a
fixed guardrail block (test-first, minimum change, no weakened invariants, no
live network, report assumptions and deviations). Preview it with:

```bash
writ dispatch M01-001 --dry-run
```

The prompt is written to the run directory and delivered to the agent on stdin.
Arguments after `--` are forwarded to the agent command:

```bash
writ dispatch M01-001 --agent claude -- --model sonnet
```

`--detach` hands the run to a supervisor process that outlives the CLI, so you
can close the terminal and still get a recorded outcome. `--timeout` kills the
process group and records exit 124. If a machine dies mid-run, `writ reap`
reconciles the orphaned records.

## Tests

```bash
uv run --with pytest python -m pytest
```

72 tests, hermetic — the "agents" under test are short `python -c` commands, so
nothing touches the network.
