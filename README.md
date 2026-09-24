# Writ

Turn a design document into finished code, using coding agents under supervision.

Writ has an agent plan the work as a dependency graph with explicit acceptance
criteria, checks and reviews that plan before anything runs, then hands the tasks
to coding agents one at a time. For each task a second agent verifies the result.
Everything is recorded as plain files under `<project>/.writ/`.

It rests on three rules:

- **An agent never gets "implement the design".** It gets one bounded task, a
  stated bar, and a fence of files it may touch.
- **The agent that did the work does not grade it.** The implementer reports a
  verdict with evidence, and a separate reviewer agent accepts or rejects it.
- **The plan is reviewed like the work.** Requirements are inventoried and their
  coverage checked, critics read the plan, and `writ run` refuses a plan nobody
  approved.

## Install

```bash
uv tool install --editable /path/to/writ     # or: python -m writ --help
```

Python 3.11+, no dependencies.

## Quick start

```bash
cd ~/projects/my-service
writ init                       # create .writ/ and a commented config.yaml
writ build docs/design.md -p 3  # plan, approve, run with three agents at a time
```

`writ build` does the whole flow in one command. Run it again to resume. To do
each step yourself instead:

```bash
writ plan docs/design.md        # analyse the doc and repo, commit a plan
writ check                      # what writ can prove is wrong with it
writ coverage                   # every requirement, and what covers it
writ critique                   # independent agents read the plan
writ approve --reason "read it" # sign it off; `writ run` requires this
writ run --parallel 3           # dispatch, review, repeat until done or stuck
writ status --watch             # follow along from another terminal
writ serve                      # or in the browser
```

## How it works

```text
design doc(s)
    │  writ plan
    ▼
requirements ─┐
inventory   ──┴─► synthesis ─► plan ─► checks + critics ─► (repair) ─► approve
                                                                        │
    ┌───────────────────────────────────────────────────────────────────┘
    │  writ run
    ▼
ready task ─► implementer ─► verdict ─► reviewer ─┬─► completed ─► unblocks more
                                                  └─► rejected ─► rework
milestone done ─► gate: does the integrated work add up? ─► pass / repair plan
```

### 1. Planning

`writ plan` runs a staged pipeline rather than one agent call:

1. **requirements**: the capabilities the document asks for, each with an id
   (`REQ-001`) and the heading it came from.
2. **inventory**: a short survey of the repo, covering language, test command,
   baseline and existing components.
3. **synthesis**: 4–12 **features** built on both. Each has a goal, the component
   it owns, the interfaces it provides and consumes, the requirements it covers,
   and 3–6 observable acceptance criteria. Edges come from provides/consumes, so
   nobody writes them by hand.

Writ validates every field before the plan reaches state. That rules out missing
criteria, dependencies on nothing, cycles, sections the document never had, and
requirements that were dropped or invented. Writ also assigns the real ids. Stage
artifacts live in `.writ/plans/<plan-id>/` and are reused on a retry, so a failed
synthesis does not pay for the analyses again.

**Several documents.** `writ plan api.md storage.md` reads them as one design.
Each task records the document its section came from. Where two documents share a
heading, the section is cited as `api.md / Endpoints`.

Other ways in:

```bash
writ plan design.md --dry-run               # print the prompt, spend nothing
writ plan design.md --stage requirements    # stop after one analysis
writ plan design.md --plan-id ID            # resume a pipeline
writ plan design.md --from-plan draft.json  # re-import an edited plan, no agent
writ plan design.md --extract               # no agent: headings become tasks
writ plan more.md --append                  # add work to an existing plan
```

### 2. Review and approval

A committed plan is a **draft**, not an accepted one.

- **Checks** (`writ check`, automatic on commit) find what can be proven wrong:
  an uncovered requirement, a criterion nothing could test, overlapping file
  ownership, a cycle. Only `error` findings block.
- **Critics** (`writ critique`, or `plan --critics`) are agents that did not write
  the plan. `fidelity` asks whether it covers the design. `feasibility` asks
  whether it can be built here.
- **Repair** (`writ adjudicate`, or `plan --repair`) gives an agent a working copy
  of the plan to fix its blocking findings. Writ accepts the edit only if
  coverage does not regress and the graph stays sound. The number of rounds is
  bounded.
- **Approval** always has an actor behind it. A clean check is not approval:

```bash
writ approve --by ada --reason "read it through"
writ set F-0007 accepted --reason "known gap"          # or answer one finding
writ plan design.md --critics --repair --auto-approve  # unattended
```

`--auto-approve` approves only when nothing blocking stands against the plan. It
never overrides a finding.

### 3. Execution

`writ run` walks the graph. It dispatches ready tasks, up to `--parallel` at a
time, and reviews finished ones before starting new work, because completions are
what unblock more of the graph.

```text
planned ─► ready ─► running ─► awaiting-review ─► reviewing ─┬─► completed
   ▲                  │                                      ├─► planned (rework)
   └──────────────────┴─► failed / blocked                   └─► failed
```

- The **implementer** gets the task, its criteria, its fence and the design
  section. It writes `verdict.json`, which says for each criterion whether it
  passed and the evidence (`pytest -q -> 5 passed`). A pass without evidence is
  rejected, and an exit code alone is not a verdict.
- The **reviewer** re-runs the checks and reads the diff. Only its decision marks
  a task `completed`.
- **Rework:** a rejected task returns to the queue, and the next attempt receives
  the review. It fails after `--max-rework` rejections (default 2).
  Infrastructure failures such as timeouts or spawn errors are retried separately.
- **Gates:** each milestone, and the plan as a whole, ends in a gate task. It
  writes no code; it judges whether the pieces fit together. A failing gate adds
  repair tasks in front of itself rather than failing the graph.
- **Decisions:** agents report choices the design left open. They arrive as
  `proposed`, and only you can make them binding (`writ set D-0001 active`).

`^C` lets running agents finish, and a second `^C` kills them. Either way the
state stays consistent, and `writ run` (or `writ build`) resumes.

### Building in one step

```bash
writ build design.md                    # plan, approve if nothing blocks, run
writ build api.md storage.md -p 3       # a design in two documents
writ build design.md --no-auto-approve  # stop after planning for a human
writ build                              # resume the plan that is there
writ build new.md --append              # plan another document onto the graph
```

A plan held by a blocking finding stops the build with the reason. Fix or approve
it, then run `writ build` again. A document the current plan does not cover is
refused unless you pass `--append`. `--agent` means the implementer, as under
`writ run`; the planner is `--planner` and the critics `--critic`.

## Configuration

`writ init` writes `.writ/config.yaml`. It holds writ's own defaults, with a
comment on each setting. An edited one might look like this:

```yaml
agents:
  planner:
    command: claude
    model: opus
    timeout: 1800
  critic:
    command: codex        # critics and plan repair
  implementer:
    command: claude       # tasks and gates
    model: sonnet
  reviewer:
    command: codex        # unset: the implementer reviews
plan:
  critics: true
  repair: true
  max_rounds: 2
  auto_approve: false
  instructions: null      # standing guidance for the planner
run:
  parallel: 3
  max_rework: 2
serve:
  port: 8731
```

A flag always overrides the file. An unknown key is an error that names the
closest match, and `null` means writ's default. `writ agents` shows what is in
effect. Pick a different reviewer from the implementer: a review is only as
independent as the model doing it.

Writ adds each agent's headless flag and translates `--model` for `pi`, `claude`,
`codex`, `cursor-agent`, `opencode`, `amp` and `gemini`. Anything after `--` is
passed through to the agent:

```bash
writ dispatch M01-001 --agent claude -- --dangerously-skip-permissions
```

## Commands

| Command | Purpose |
|---|---|
| **Plan** | |
| `writ init` | create the project and its config |
| `writ plan <doc>...` | derive a plan from one or more design documents |
| `writ build [<doc>...]` | plan if needed, then run; again to resume |
| `writ check` | re-check the plan, list what blocks it |
| `writ critique` | critic agents review the plan |
| `writ adjudicate` | repair the plan against its blocking findings |
| `writ approve` | sign the plan off |
| **Run** | |
| `writ run` | work the whole graph |
| `writ dispatch <id>` | one task to an implementer (`--detach` to background it) |
| `writ review [id]` | review one task, or everything awaiting review |
| `writ cancel [run-id]` | stop a run, or reconcile dead ones |
| `writ agents` | how each agent is invoked |
| **Look** | |
| `writ status [--watch]` | progress, ready work, live runs |
| `writ list [kind]` | tasks, milestones, runs, decisions, findings, requirements, gates, repairs |
| `writ show <id>` | anything by id: `M01`, `M01-001`, a run, `D-0001`, `F-0001`, `RR-0001` |
| `writ coverage` | requirement → tasks → state |
| `writ graph [--dot]` | the dependency graph |
| `writ logs <id> [--follow]` | agent output |
| `writ serve` | live web dashboard, read-only |
| **Change** | |
| `writ set <id> <status>` | move a task, rule on a decision, dispose of a finding |
| `writ override <id> <status> --reason R` | a human's final word, the only manual `completed` |
| `writ task [id]` | create or amend a task |

Every command accepts `--root <project>` and `--json`. Each command's `--help`
lists the rest.

## Storage

```text
.writ/
  state.json        the whole project: plan, tasks, runs, findings, decisions
  config.yaml       your defaults; written once by `writ init`
  decisions.md      readable mirror of the decision log
  plans/<plan-id>/  analyses, draft, committed plan, critic reviews, repair rounds
  runs/<run-id>/    prompt.txt, verdict.json, stdout.log, stderr.log
```

Writes are atomic and serialized by a lock, so a running `writ run` and a
`writ status` in another terminal never corrupt each other. Every prompt is kept,
so `writ show <run-id> --prompt` shows exactly what an agent was told.

## Tests

```bash
uv run --with pytest python -m pytest
```

The tests are hermetic: the agents under test are short `python -c` scripts, so
nothing touches the network.
