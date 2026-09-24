# Planning redesign: plan files, working-copy repair, file-referenced prompts, features with contracts

Status: implemented on the `planning-redesign` branch. This document came first
and the code follows it. Where they disagree, the code has a bug. §1–§3 are the
storage and repair mechanics. §4 (the feature model) and §5 (the simpler
pipeline) change what a plan *is*.

## Why

A real plan (`agent-memory-system-design-20260923T135720`) never converged. It
ran two repair rounds, and each round left more blocking findings than it
closed. Three structural causes are addressed here:

1. **Repair was a patch language over a live graph.** The adjudicator could only
   express changes as `add_tasks` / `revise_tasks` / `add_dependencies` entries
   against `state.json`. Merging two overlapping tasks, moving a criterion between
   tasks, or deleting a task that another task subsumes could not be expressed at
   all. Splitting a task took a revision and an addition that had to agree with
   each other, and the validator judged each entry against the pre-patch graph.
   A refusal cost the whole patch, so the agent rewrote everything on every retry.
2. **The plan had no stable on-disk form.** The synthesizer's `plan.json` held the
   draft ids (`T-log`). The committed graph (`M01-001`) existed only inside
   `state.json`, and critics and the adjudicator got it as ad-hoc JSON pasted
   into their prompts. Reviews lived in `.writ/reviews/`, adjudication in
   `.writ/adjudication/`, and analyses in `.writ/plans/<id>/`: three trees for
   one plan, with absolute and relative paths mixed.
3. **Every prompt carried every input.** The synthesis prompt rendered all three
   analyses inline. The critic prompt pasted the whole plan. The adjudicator
   prompt pasted the plan and every finding, narrowed by heuristics
   (`FULL_VIEW_TASKS`, `_relevant_tasks`) once that got too big. One plan
   produced a 240KB prompt of which the findings touched 6 tasks. Agents can read
   files, so writ should say what exists, what each file is for, and where to
   write. The agent can then read what it needs.

## 1. The plan directory

One directory per plan holds everything about it:

```
.writ/plans/<plan-id>/
  requirements.json         analysis stage artifacts (§5)
  inventory.json            the short repo summary
  <stage>/                  each stage's transcript: prompt.txt, stdout.log, ...
  draft.json                the synthesizer's output, draft ids, before commit
  synthesis/                the synthesizer's transcript
  plan.json                 INDEX of the committed plan at its current revision
  features/<task-id>.json   one file per task (gates included), committed ids
  reviews/r<rev>/
    known-findings.json     what writ already found, pre-filtered for critics
    <critic>/findings.json  each critic's report, plus its transcript
  rounds/r<rev>/round-<n>/  one plan-repair attempt (see §2)
```

- `plan.json` is small. It holds the plan id, revision, design document, the
  requirement inventory (id, priority, status, text, details), the milestones
  (none for a features plan), and one summary row per feature: id, title, kind,
  milestone, status, the interface names it owns, provides and consumes,
  depends_on, requirement_ids and file. A reader can see the whole graph without opening any feature file.
- `features/<id>.json` holds one dispatchable unit in full: title, kind,
  milestone, goal, owns, provides, consumes (the last four for features only),
  notes, design_section, requirement_ids, depends_on, acceptances (as text),
  allowed and forbidden.
- **`state.json` stays the source of truth** for runtime state (statuses,
  criteria sign-off, runs). The plan files are its plan-phase projection. Writ
  writes them on commit and on every promoted repair. When they are missing or
  behind the current revision (an older project, an approval bump, a gate
  repair), `planfiles.ensure` re-exports them before anything reads them.
- The current plan id is recorded in `state.json` at `plan.id`. A project that
  predates this gets one minted on first export.
- Every path writ records or shows an agent is **relative to the repository
  root**. Prompts state the root once.
- The synthesizer's output is `draft.json`. It is no longer named `plan.json`,
  because `plan.json` is the committed index, and the two contain different ids.

## 2. Plan repair edits a working copy

Plan-phase repair and execution-phase repair are now different operations:

|                     | plan phase (`writ adjudicate`, `plan --repair`) | execution phase (gate repair) |
|---------------------|--------------------------------------------------|-------------------------------|
| what may change     | anything about a `planned` task                  | add tasks and edges only      |
| how it is expressed | edit the plan files in a working copy            | `patch.json` (add-only)       |
| why                 | nothing has run, so no contract has been met     | work is done or in flight     |

`revise_tasks`, `REVISE_SCHEMA` and `PLAN_PATCH_RULES` are removed from
`repair.py`. A gate repair cannot revise tasks and never could.

### A round

For each attempt, writ prepares `rounds/r<rev>/round-<n>/`:

```
to-fix.json      the blocking findings this round must answer, and nothing else
plan/plan.json   a copy of the index (read-only reference)
plan/features/   a copy of every feature file: THIS is what the agent edits
validation.json  written by writ after the attempt: accepted, or why not
response.json    written by the agent: analysis, dispositions, questions
```

`r<rev>` is the revision the loop started at. Every attempt in that run of the
loop goes under it, including those after a promoted round bumped the revision,
and `round-<n>` numbering continues from what is already on disk. That way a
resumed `writ adjudicate` never overwrites a working copy that an earlier refusal
left behind.

The agent edits `plan/features/` in place:

- **revise** a feature by editing its file,
- **add** one by creating `plan/features/<new-id>.json` with a new id of its
  choosing (writ assigns the real `FT-nnn` or `M<nn>-<nnn>` id on promotion
  and rewrites every `depends_on` that names it; a feature's edges to other
  features are re-derived from the edited contracts, and its fence follows its
  `owns`),
- **remove** one by deleting its file (for example when merging two overlapping
  features into one).

The feature files in `plan/features/` define which features exist. The copied
index is reference only, and the agent does not maintain it.

In `response.json`, every finding in `to-fix.json` gets a disposition:
`accepted` (with `change`: what was edited) or `declined` (with `reason`: the
evidence). A finding can also be named in `questions` when it needs a human
ruling.

### Validation

Writ reads the working copy back and refuses it, writing each problem to
`validation.json`, when any of these fail:

- **shape**: every file parses, has a title and at least one acceptance
  criterion, and has an `id` that matches its filename;
- **requirements are fixed**: the copied index's requirement block is
  byte-for-byte what writ wrote (hash check), and no feature claims an id that
  is not in the inventory;
- **coverage does not regress**: every requirement some ordinary task covered
  before is still covered by some ordinary task after (gates do not count);
- **the bar does not drop**: a surviving feature does not end with fewer
  criteria than it had;
- **only unstarted work changes**: a feature whose task is not `planned` may not
  be edited or removed, and a gate may not be edited or removed at all;
- **the graph is legal**: every dependency names a feature that exists in the
  copy, there are no self-edges, and no cycles;
- **every finding is answered**: each id in `to-fix.json` is dispositioned or
  raised as a question, a decline carries a reason, and an accept names a change;
- **something happened**: an edit that changes nothing and asks nothing is
  refused.

On refusal, the next attempt **starts from the refused working copy**, not
from a fresh one. Its prompt lists the previous `validation.json` as required
reading. The agent fixes what was listed, and the sound edits it already made
are still on disk. This replaces the old `accepted_entries` bookkeeping. The
budget is unchanged: `repair.MAX_PATCH_ATTEMPTS` refusals per request.

### Promotion

A valid copy is diffed against `state.json` and applied in one transaction:
changed fields are replaced on each revised task (a criterion whose text is
unchanged keeps its status), new tasks are inserted under minted ids, and
removed tasks are deleted. Each gate's `depends_on` and `requirement_ids` are
recomputed from its milestone's membership. The DAG is re-checked,
dispositions and questions are recorded, the request is closed as `applied`,
and the revision bumps. Writ then re-exports the plan files at the new revision,
re-runs its deterministic checks, and (when critics are in play) re-runs the
critics. Findings still close only on a re-check, never on the agent's word.

## 3. File-referenced prompts

Every agent prompt (analysis stages, synthesis, critics, adjudicator) has the
same shape, built by one helper (`prompts.py`):

```
<role, one paragraph>

Repository root: /abs/path  — every path below is relative to it.

Read first:
  - .writ/plans/<id>/requirements.json — the fixed requirement inventory
  - ...
Read as needed:
  - .writ/plans/<id>/features/ — one file per feature, in full
  - ...

<the brief: what to check / decide, the rules>

Write your <output> as JSON to this exact path:
  .writ/plans/<id>/...
Schema: ...
```

What is inlined: role, brief, rules, schema, output path, and small scalars
(revision, round number, counts). What is referenced: the design document,
analysis artifacts, the plan index, feature files, findings and validation
reports.

Writ pre-filters what it hands over, so "read this file" is never "read this
and ignore most of it":

- critics get `reviews/r<rev>/known-findings.json`: writ's open blocking and
  advisory findings, not notes, not other critics' reports;
- the adjudicator gets `to-fix.json`: blocking findings only;

## 4. Features with contracts; file detail is decided later

The same plan (`agent-memory-system-design`) had 60+ requirements, 40+ tasks and
several hundred findings. Most of the findings argued about file names and test
names for code that did not exist yet. The plan was too fine-grained for
anything to check it cheaply: every requirement was one sentence, every task
named its files, and every criterion named a test. So the plan model is
coarsened.

### Requirements are capabilities

The requirements stage writes **10–25 capabilities** per document (fewer for a
short one: roughly one per major section, never more than 25). Each carries
`details[]`: the finer obligations it contains, which stay traceable without
getting their own ids. Coverage, reconcile and gates work at capability level.
The final gate is where `details[]` are checked, and its prompt tells it so.

```json
{"id": "REQ-003", "text": "Durable event store", "priority": "must",
 "source": "Storage", "details": ["append is fsync'd before ack", "..."]}
```

### A feature replaces the task and the milestone

```json
{
  "id": "store",
  "title": "Event store",
  "goal": "one paragraph: what exists when this is done",
  "requirement_ids": ["REQ-003", "REQ-004"],
  "owns": ["mmm/store/"],
  "provides": ["EventLog: append(event) -> offset; read(from) -> events"],
  "consumes": ["Config: typed settings loaded from writ.toml"],
  "acceptance": ["3-6 observable behaviours, no file or test names"],
  "notes": "ambiguities, risks"
}
```

- `owns` is a component or directory, never a file list. The feature's fence is
  `owns` plus the repository's test directories (from the repo summary), which
  is what `allowed` holds at runtime.
- `provides` / `consumes` are named interfaces, one line each: `Name: what it
  is`. The name is the text before the first colon, compared case-insensitively.
- The target is 4–12 features. Each one is a subsystem that one agent can build
  on its own.
- A feature is stored as an ordinary task (`kind: task`, `milestone: null`, id
  `FT-001`…; not `F-`, which finding ids already use) carrying the extra fields `goal`, `owns`, `provides` and `consumes`,
  so dispatch, review, gates and the UI need no second engine. There are no
  milestones and no milestone gates. `G-FINAL` depends on every feature.
- The legacy milestone/task draft (`--extract`, or an older `draft.json`) still
  loads and commits as before.

### Edges come from the contracts

`depends_on` is derived: a feature depends on every feature that provides an
interface it consumes. The synthesizer does not write edges. This replaces the
`missing-edge` critic finding and the `shared-ownership` check with one
deterministic check: **every consumed interface has a provider**
(`contract-gap`). An interface provided twice is also a `contract-gap`. An edit
during repair re-derives edges from the edited contracts.

### File names are decided when the feature runs

The dispatch prompt for a feature gives the agent its goal, its contracts, what
its upstream features actually provide (their status and last summary), and
its fence. It then tells the agent to split the work into steps itself, inside
that fence. No step list is stored in the plan.

## 5. The simpler pipeline

1. **Requirements** (coarse capabilities with `details[]`) plus a short **repo
   summary**: language, test command, test directories, baseline, and the few
   components new work attaches to. The `verification` stage and
   `verification.json` are gone. Acceptance lives on the feature.
2. **Synthesize** features and contracts (`draft.json`, the schema above).
3. **Review** with two critics instead of five:
   - `fidelity`: does the plan cover the design, and do the contracts compose?
   - `feasibility`: can it be built here (environment, baseline, conventions)?

   Blocking findings must use one of a closed list of categories:
   `uncovered-requirement`, `contract-gap`, `cycle`, `oversized-feature`,
   `infeasible-env`, `needs-decision`. A blocking finding with any other
   category is recorded as advisory. Each critic reports at most 5 blocking and
   5 advisory findings, and writ truncates beyond that. The prompt says plainly:
   files that don't exist yet are expected, so don't judge file names or test
   names.

   The deterministic checks are cut down to schema (fields present, 1–6
   criteria, 4–12 features advisory), coverage, contract closure, acyclicity and
   oversized features (more than 6 requirements or more than 6 criteria).
   `unknown-path`, `suspect-path`, `unobservable-acceptance`,
   `unused-verification`, `shared-ownership` and the other phrase and path
   heuristics are deleted.
4. **Plan repair rewrites the working copy** (§2), and gets only blocking
   findings. From round 2 the critics run in **verify mode**. Each one gets its
   own earlier blocking findings and the list of features that changed since
   it last read the plan. It answers only "is each one resolved?", and may raise
   a new blocker only on a feature that changed; anything else is recorded as
   advisory. Earlier advisory findings are carried forward untouched. The loop
   runs at most 2 rounds, and whatever is left goes to a human as a decision
   (`needs-decision` findings are routed to the adjudicator's `questions`).
5. **Execution repair is unchanged**: a gate adds tasks in front of itself.

Kept: the fixed requirement list and `reconcile` (dropped or invented
requirements), approval on the record, gates, and the human disposition path.

## Compatibility

- `--from-plan <file>` still reads a synthesizer-shaped document. It is the
  draft format, not the index.
- `.writ/reviews/` and `.writ/adjudication/` from older projects are left where
  they are. New ones are written under the plan directory.
- Execution-phase repair (`repair.py`, gate `patch.json`) is unchanged apart
  from losing `revise_tasks`, which it always refused.
