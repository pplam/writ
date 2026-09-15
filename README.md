# Forge

Plan, dispatch, and track coding-agent work against a design document.

Forge takes a markdown design doc, turns it into a dependency-ordered task DAG
with explicit acceptance criteria, hands one task at a time to a coding agent,
and keeps an append-only decision log. State is plain files — JSON, markdown,
and logs — under `<project>/.forge`.

The premise: an agent should never be asked to "implement the design". It gets
one bounded task, a stated bar, and a guardrail list. Everything it did stays
inspectable afterwards.

## Install

```bash
uv tool install --editable /path/to/forge
```

Or run without installing:

```bash
python -m forge --help
```

## Quick start

```bash
cd ~/projects/my-service

forge init
forge plan docs/design.md --dry-run     # preview
forge plan docs/design.md               # commit the plan

forge status                            # progress, ready work, live runs
forge next                              # what can start now
forge show M01-001                      # the task and its acceptance bars

forge dispatch M01-001 --agent claude --detach
forge logs M01-001 --follow             # stream the agent's output
forge status                            # from any other terminal, any time

forge accept M01-001 1 passed
forge complete M01-001 --evidence "go test ./... green"

forge decision add \
  --title "Fixture-only tests" \
  --decision "Automated tests never touch a live platform." \
  --context "Platform quotas are small and debugging consumes them." \
  --task M01-001
```

## Storage

```
<project>/.forge/
  state.json          milestones, tasks, runs, decisions
  decisions.md        human-readable mirror of the decision log
  runs/<run-id>/
    prompt.txt        exactly what the agent was given
    stdout.log
    stderr.log
    supervisor.log    detached runs only
```

Writes are atomic (temp file + replace) and serialized by an advisory lock, so
a detached run and an interactive `forge status` never corrupt each other.

## Planning

`forge plan` maps level-2 headings to milestones and level-3 headings to tasks.
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
usually load-bearing. Re-wire any task with `forge edit-task <id> --depends`.

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
| `forge init [--force]` | create the project store |
| `forge plan <doc>` | derive milestones, tasks, acceptance criteria |
| `forge status` | progress bars, counts, ready work, live runs |
| `forge tasks [--status S] [--milestone M] [--ready]` | list tasks |
| `forge milestones` | rollups derived from member tasks |
| `forge show <id>` | a task or milestone in full |
| `forge next [--limit N]` | dispatchable tasks |
| `forge graph [--dot]` | the dependency DAG |

**Change**

| Command | Purpose |
|---|---|
| `forge start\|complete\|fail\|block\|reset <id> [--evidence T] [--force]` | transitions |
| `forge accept <id> <n> passed\|failed\|pending` | per-criterion sign-off |
| `forge add-task <title> [--id] [--milestone] [--depends] [--acceptance] [--allow] [--forbid]` | hand-written task |
| `forge edit-task <id> [--title] [--depends] [--acceptance] [--allow] [--forbid]` | amend |

**Dispatch and monitor**

| Command | Purpose |
|---|---|
| `forge dispatch <id> --agent CMD [--detach] [--timeout S] [--cwd D] [--dry-run] [-- args]` | run an agent |
| `forge runs [--task T] [--active]` | run history |
| `forge run <run-id>` | one run in detail |
| `forge logs <run-id\|task-id> [--follow] [--stderr] [--tail N]` | agent output |
| `forge cancel <run-id>` | stop an active run |
| `forge reap` | reconcile runs whose process died |
| `forge watch [--interval S] [--once] [--until-idle]` | live status view |

**Decision log**

| Command | Purpose |
|---|---|
| `forge decision add --title T --decision D [--context] [--consequences] [--supersedes ID] [--task ID]` | append a record |
| `forge decision list [--active] [--task ID]` | list records |
| `forge decision show <id>` | one record |
| `forge decision export [--out FILE]` | render as markdown |

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
forge dispatch M01-001 --dry-run
```

The prompt is written to the run directory and delivered to the agent on stdin.
Arguments after `--` are forwarded to the agent command:

```bash
forge dispatch M01-001 --agent claude -- --model sonnet
```

`--detach` hands the run to a supervisor process that outlives the CLI, so you
can close the terminal and still get a recorded outcome. `--timeout` kills the
process group and records exit 124. If a machine dies mid-run, `forge reap`
reconciles the orphaned records.

## Tests

```bash
uv run --with pytest python -m pytest
```

72 tests, hermetic — the "agents" under test are short `python -c` commands, so
nothing touches the network.
