# Storage redesign: one owner per fact, bounded files, names that say what they are

Status: implemented. The proposal is kept as written except where the build
chose differently; each such place says so, and [As built](#as-built) lists them.

## What the store holds today

Measured on one real `writ build` (12 features, 3 gate repairs, 33 task runs):

| What | Size | Problem |
|---|---|---|
| `plans/<id>/**/events.jsonl` | 14.6 MB | 90% of the store. Raw agent event streams, mostly one-token `message_update` deltas of ~250 bytes each (the synthesis log has 14,446 of them). The same text is repeated in `message_end` and again in `stdout.log`. |
| `state.json` | 464 KB | Rewritten in full, with fsync, on every transaction. `tasks` is 206 KB of it, mostly feature *definitions* that are also in `features/*.json`. |
| `rounds/r<rev>/adjudicate-<n>/plan/` | 1 index + 12 features per attempt | A full copy of the plan, even when the repair changed 2 of the 12 features. The `plan.json` copy is never edited at all. |
| `runs/<task>-<timestamp>/` | 33 folders | `FT-002-…T171036` and `FT-002-…T171453` are the implementer and the reviewer, but only `state.json` says which. `G-FINAL` has five folders: gate, repair, gate, repair, gate. |
| verdicts | 3 copies | `runs/<id>/verdict.json`, `state.runs[id].verdict` and `state.tasks[id].last_verdict`. |

Plan data lives in four places: `state.tasks`, `plans/<id>/features/`, each round's `plan/features/`, and `draft.json`. Only the first is authoritative. `features/` is also stale after a gate repair, because it is exported only during planning.

## Principles

1. **One owner per fact.** Every fact has one authoritative home. Any other copy is a *view*: generated, marked as generated, and rebuilt instead of edited.
2. **Write what changed, not everything.** A transaction's cost scales with the change, not with the project.
3. **Bounded artifacts.** Raw streams are compacted and compressed when a run ends. Retention is a policy, not an accident.
4. **Names say what a thing is.** A folder's name gives its task, its order and its role. Timestamps go in metadata.
5. **Writ writes `.writ/`. An agent writes only the output it was asked for.**

## 1. Plan revisions without copies

A plan changes through changesets, and a repair attempt keeps its changeset, not a copy of the plan.

```
plans/<plan-id>/
  plan.json                   VIEW  index at the head revision
  features/<task-id>.json     VIEW  every task at the head revision, gate repairs included
  rounds/r<rev>/
    known-findings.json
    <critic>/                 run folder + findings.json
    adjudicate-<n>/           run folder +
      to-fix.json             the blocking findings (input)
      response.json           the adjudicator's answers (agent output)
      changes.json            what the attempt changed (writ, after the run)
      validation.json         accepted, or why not (writ)
      workspace/              ONLY while the attempt is running
```

**The workspace is temporary.** Writ writes the feature files into `workspace/<task-id>.json` when an attempt starts, because the adjudicator needs real files to edit in place. When the attempt ends, writ diffs the workspace against the base revision, writes `changes.json` and deletes `workspace/`. It does this whether the attempt was accepted or refused. The index is never copied; the prompt points at `plans/<id>/plan.json`.

**A refused attempt no longer needs its copy.** Today a refused `plan/features/` is kept so the next attempt can start from it (`_previous_attempt`). Replaying the refused `changes.json` onto the same base revision produces the same seed, so nothing is lost.

**`changes.json`** records only the changed fields, before and after, so it is both a readable diff and something that can be replayed:

```json
{
  "base_revision": 1,
  "result_revision": 2,
  "by": "adjudicate-1",
  "added":    {"FT-012": { "...full feature..." }},
  "removed":  ["FT-003"],
  "modified": {
    "FT-004": {"acceptances": {"before": ["..."], "after": ["..."]}},
    "FT-009": {"consumes":    {"before": ["..."], "after": ["..."]}}
  },
  "unreadable": {"FT-010.json": "...the file as the agent left it..."}
}
```

`unreadable` holds, verbatim, any workspace file that was not a JSON object. The validator refuses such an attempt, and the replay puts the file back as it was, so the next attempt sees what it has to fix.

**Revision history** goes in the store (§2b) as one changeset per revision. The author is recorded: an adjudicate attempt, or a gate repair `R-xxx`. So any past revision can be rebuilt, and `writ plan diff r1 r3` becomes a query. Gate repairs create changesets as well, which fixes the stale `features/`: the views are regenerated after *every* plan change, not only during planning.

Result: per attempt, a few KB of changes instead of a full copy of the plan, and one authoritative plan.

## 2. Bounded sizes

### 2a. Event streams: live while running, compact at rest

| When | File | Content |
|---|---|---|
| running | `events.jsonl` | everything, as today, because the UI tails it for live activity |
| after the run | `events.jsonl.gz` | `message_update` deltas dropped, then gzipped |

Dropping the deltas loses nothing: every `message_end` carries the complete message and its `stopReason`, which is the one field `runner` reads the raw stream for. Measured on this run:

| | Size |
|---|---|
| raw | 14.6 MB |
| gzip only | 0.79 MB |
| compacted + gzip | **0.53 MB** (27× smaller) |

Readers (`api._activity_lines`, `runner`'s "did it run" check) open `.gz` transparently. Retention is `writ gc`: it compacts live logs nothing has written to for six hours (runs from before compaction, runs whose writ died) and removes compacted logs older than `--older-than` days (default 14). Prompts, transcripts and outputs are never removed. *As built:* there is no `store.keep_events` setting; `writ gc` was enough.

### 2b. State: sharded, written by difference

`state.json` stays a single dict in memory: `state.load`, `state.transaction` and every `data["tasks"][id]` access in the codebase are unchanged. What changes is persistence:

- **Shards.** The dict is stored as small records in one table, `record(section, item, kind, position, body)`. Keyed sections (`tasks`, `runs`, `milestones`, `requirements`) get one record per entry. Listed sections (`findings`, `repairs`, `decisions`, `phases`, `plans`, `plan_revisions`) get one record per element, in order. Every other top-level field (schema version, counters, flags, the plan pointer) is one record of its own. That is roughly 200 records of 0.2–5 KB for this run.
- **Diff on save.** `transaction()` keeps the serialized bytes of every shard it loaded. On exit it writes only the shards whose bytes changed, plus deletions. A typical transaction (mark a task running, record a run) writes 2–3 KB instead of 464 KB.
- **Backend: SQLite (`.writ/store.db`, stdlib `sqlite3`).** One table, in rollback-journal mode. *As built:* not WAL, which the proposal named. A WAL reader writes (`-wal`/`-shm` files, checkpoints), and `writ serve` must be able to read a live project without touching it; with the journal, the file's mtime also still moves on every commit, which is what the server's watcher uses.
  - `BEGIN IMMEDIATE` makes a multi-shard commit atomic and crash-safe without a hand-written manifest or temp-file protocol.
  - A reader sees the last commit, never part of one.
  - `flock` stays only to serialize writ processes around long operations, as today.
- **Human readability** is kept through views and commands, not by storing everything as one document:
  - `writ state dump` writes the full JSON.
  - `decisions.md` stays generated.
  - `plan.json` and `features/` are views (§1).

   A side effect: an agent can no longer skim or accidentally edit the project state by opening `state.json`.

Alternative with no database: the same shards as individual JSON files, committed by atomically renaming a manifest. It is readable with `cat`, but writ would then own the crash-safety and garbage-collection code that SQLite already provides. It is not recommended.

### 2c. Stop copying run outputs into state

A verdict lives in its run folder. The state keeps a reference and what scheduling needs:

```json
"last_verdict": {"run": "FT-008/02-review", "role": "reviewer", "decision": "accept", "outcome": null, "summary": "...", "blocked_on": null}
```

*As built:* both copies were already summaries (outcome, decision, summary, counts), not the verdict file, so what changed is the `run` reference, which names the folder holding the rest. An operator override has no run and carries none. Evidence text is read from `verdict.json` when a page or prompt needs it. Together with definitions moving into `tasks/<id>` shards, which are written only when they change, this removes most of the 206 KB `tasks` section from ordinary transactions.

## 3. Run folders named by task, order and role

```
runs/
  FT-002/
    01-implement/
    02-review/
  FT-009/
    01-implement/
    02-review/        rejected
    03-implement/     rework
    04-review/
  G-FINAL/
    01-gate/
    02-repair/
    03-gate/
    04-repair/
    05-gate/
```

- The run id is the path: `FT-002/02-review`. The sequence number is allocated inside the state transaction that records the run (`len(task.runs) + 1`), so two runs can never collide. `new_run_id`'s timestamp-collision fallback goes away.
- Timestamps, pid, agent, model, command and exit code go into `meta.json` in the folder. That is the complete record of the run, and the state keeps the summary the scheduler needs.
- The folder roles are `implement`, `review`, `gate` and `repair`. *As built:* `role` in state keeps its values (`agent`, `reviewer`, `gate`, `repair`), and `runner.ROLE_FOLDERS` maps them to folder names. Renaming a stored value would have changed every reader of it, and old records would still carry the old name.
- A number is `1 +` the task's existing runs in the new form, and skips any folder already on disk. Old timestamped runs keep their ids and folders.
- The run id carries a slash, so the UI encodes it into one URL segment and the server unquotes it.

**Every agent run, in any phase, uses the same folder shape:**

```
<run>/
  meta.json          who, what, when, how it ended
  prompt.txt
  stdout.log         the transcript (what text mode would have printed)
  stderr.log
  events.jsonl.gz    compacted raw stream (events.jsonl while running)
  <output>           verdict.json | findings.json | response.json | patch.json
```

Plan-phase runs follow it too: `plans/<id>/analysis/{inventory,requirements,synthesis}/`, the critics, and `adjudicate-<n>/`. One reader (`api`, `runner`) handles all of them.

## Full layout

```
.writ/
  store.db                    project state (records, SQLite, rollback journal)
  state.lock                  serializes writ processes
  config.yaml
  decisions.md                VIEW
  plans/<plan-id>/
    plan.json                 VIEW  head index
    features/<task-id>.json   VIEW  head tasks, repairs included
    draft.json                synthesizer output (pre-commit input, kept for audit)
    inventory.json            analysis artifacts
    requirements.json
    analysis/<stage>/         run folders
    rounds/r<rev>/
      known-findings.json
      <critic>/               run folder + findings.json
      adjudicate-<n>/         run folder + to-fix, response, changes, validation
  runs/<task-id>/<nn>-<role>/ run folders
```

## Safety

- **Crash safety.** State commits are SQLite transactions. A plan change is one transaction: changeset plus shard updates. Views are regenerated afterwards and can always be rebuilt from the store (`writ plan export`), so a crash between the two leaves nothing to repair by hand.
- **Agent writes.**
  - Each prompt names its one output path (as today), and the implementer's working rules forbid edits under `.writ/`.
  - After each task run, writ compares the plan's files with the store (`planfiles.drift`). Any that differ are put back, and the task's evidence names the run that edited them.
  - Nothing an agent can edit is authoritative.
- **Concurrent readers.** The UI server reads committed data only and never writes, not even a journal.
- **Schema.** `schema_version` goes from 1 to 2. `writ migrate` converts a v1 `state.json` into `store.db` and refreshes the plan's files, then leaves the old file in place (and ignored) until `writ migrate --prune`. A v2 writ refuses a v1 store with a message naming the command. `writ state dump` prints the whole project as the one JSON document `state.json` used to be.

## Rollout

Each step ships on its own and pays off by itself:

| Step | Fixes | Touches |
|---|---|---|
| 1. Compact and gzip events at run end; `.gz`-aware readers | size (−96% on disk) | `runner`, `stream`, `api` |
| 2. Temporary adjudicate workspace + `changes.json`; seed from the replayed changeset | duplication | `adjudicate`, `planfiles` |
| 3. Run folders `runs/<task>/<nn>-<role>/` + `meta.json` | naming | `runner`, `api`, UI run links, `writ logs` |
| 4. Views regenerated after every plan change, including gate repairs | stale `features/` | `planfiles`, `repair` |
| 5. Sharded SQLite store, verdict references, `writ migrate` | write amplification, verdict copies | `state`, `verdict`, a migration |

Steps 1–3 answer the three immediate problems and change no state format. Step 5 is the largest, but it stays inside `state.py` because the dict API is kept.

## Open questions

- Keep `draft.json` after commit, or fold it into revision 0's changeset?
- Default retention for event streams: keep forever, or drop them for completed plans after N days?
- Should `writ gc` also remove folders of refused adjudicate attempts once their plan is complete?

## As built

All five steps are in. Where the build differs from the proposal above:

| Proposal | Built | Why |
|---|---|---|
| SQLite in WAL mode | rollback journal | Readers must not write; the watcher needs the mtime to move on commit. |
| `agent` role renamed `implement` | state values kept, mapped to folder names | No migration of stored roles, and no reader changes. |
| `last_verdict` reduced to a summary | it already was; gained a `run` reference | The copies were never the full verdict. |
| `store.keep_events` setting | `writ gc [--older-than DAYS] [--dry-run]` only | One mechanism was enough. |
| workspace copies `features/` | workspace is one file per feature, written from the store | Same content, and no stale copy to trust. |
| `changes.json` fields | also `unreadable`, verbatim files the attempt broke | Needed so a replay rebuilds exactly what was refused. |

Other details worth knowing:

- Revision changesets go in `plan_revisions`, appended in the same commit as the revision bump. `by` names the author (`planner`, `<finding> (adjudicate-<n>)`, `<request> (gate repair)`). Status changes are progress, not plan, and are left out.
- The views are refreshed after any commit that touched `tasks`, `plan`, `requirements`, `milestones` or `design_docs`. Only files whose content differs are rewritten. A failure to write a view never fails the commit, because the store is what counts.
- A commit writes only the records whose serialized bytes changed, plus deletions, under `BEGIN IMMEDIATE`, with `synchronous = FULL` (or `OFF` under `WRIT_FSYNC=0`).
