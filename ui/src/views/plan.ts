/**
 * The plan as a reviewed artifact: its status, what stands against it, and what
 * every requirement has to show.
 *
 * Three records that are one question. The plan's status says whether work may
 * start; the findings say why not; the coverage matrix says whether what is being
 * built is what was asked for. Reading them apart is how a plan gets approved with
 * a requirement nobody implemented — the status looked fine on its own.
 *
 * Read-only, like the rest of the dashboard. A finding is disposed of with a reason
 * attached, and a reason is something you type deliberately, so this shows the
 * command rather than offering a button.
 */

import { classes, code, el, replace } from '../dom.js';
import { ago, percent, plural } from '../format.js';
import type { Coverage, Finding, Phase, Pipeline, PipelineStage, Plan, Repair } from '../types.js';
import { isPhase, renderPhase, type PhaseHandlers } from './phase.js';

/** Which findings a reader is shown first. */
export const FINDING_FILTERS: Record<string, (finding: Finding) => boolean> = {
  open: (f) => f.disposition === 'open',
  blocking: (f) => f.disposition === 'open' && f.severity === 'error',
  answered: (f) => f.disposition !== 'open',
  all: () => true,
};

export interface PlanHandlers {
  onTask?: (id: string) => void;
  onStep?: (id: string) => void;
}

export function renderPlan(
  host: HTMLElement,
  plan: Plan,
  findings: Finding[],
  coverage: Coverage[],
  repairs: Repair[],
  options: { filter: string; phase?: Phase | Record<string, never>; step?: string | null },
  handlers: PlanHandlers = {},
): void {
  const shown = findings.filter(FINDING_FILTERS[options.filter] ?? FINDING_FILTERS.open);
  const pipeline = plan.pipeline as Pipeline;
  const phase = options.phase;
  const watched = isPhase(phase);
  replace(host,
    // First, because it comes first in time. The status card says whether work may
    // start; this says whether the thing being judged has finished being built —
    // and while planning is running it is the only card with news.
    watched ? phaseCard(phase, options.step ?? null, handlers) : null,
    statusCard(plan),
    plan.held_gates.length ? heldCard(plan, handlers) : null,
    // Above the findings: the findings say what is wrong with the plan, and this
    // says what the plan was derived from. A reader deciding whether to trust a
    // finding about coverage wants to know whether a requirements stage ran at all.
    pipeline && pipeline.plan_id ? pipelineCard(pipeline, !watched) : null,
    findingsCard(shown, findings, options.filter),
    coverage.length ? coverageCard(coverage, handlers) : null,
    repairs.length ? repairsCard(repairs, handlers) : null,
  );
}

/** The phase graph, in its own card so the Plan page stays a stack of cards. */
function phaseCard(phase: Phase, step: string | null, handlers: PlanHandlers): HTMLElement {
  const card = el('section', { class: classes('card', 'phase-card', phase.status === 'failed' && 'urgent') });
  const forward: PhaseHandlers = { onStep: (id) => handlers.onStep?.(id) };
  renderPhase(card, phase, step, forward);
  return card;
}

/** How each stage ended, as a mark and a word. */
const STAGE_MARKS: Record<PipelineStage['state'], string> = {
  ok: '✓',
  reused: '·',
  failed: '✗',
  pending: '○',
};

/**
 * The staged pipeline the plan came out of.
 *
 * The plan's own status card says whether work may start. This says what the plan
 * rests on, which is a different question and the one that explains a finding: a
 * pipeline that stopped at `inventory` produced no verification artifact, so
 * nothing worked out how any requirement would be demonstrated, and every
 * acceptance bar in the plan is the synthesizer's own invention.
 *
 * The baseline gets its own line for the reason the API comments on: a suite that
 * was already failing when planning started will be blamed on whichever task first
 * runs into it.
 */
function pipelineCard(pipeline: Pipeline, withStages: boolean): HTMLElement {
  const failed = pipeline.stage_rows.filter((stage) => stage.state === 'failed');
  const pending = pipeline.stage_rows.filter((stage) => stage.state === 'pending');
  const stopped = failed.length > 0 || pending.length > 0;
  const baseline = pipeline.baseline;
  return el(
    'section',
    { class: classes('card', failed.length > 0 && 'urgent') },
    el(
      'header',
      { class: 'plan-head' },
      el('h2', {}, 'Pipeline'),
      code(pipeline.plan_id),
      pipeline.at ? el('span', { class: 'muted small', title: pipeline.at }, ago(pipeline.at)) : null,
    ),
    el('p', { class: 'muted small' },
      stopped
        ? 'The plan does not rest on every analysis: what is missing was never established.'
        : 'Each analysis ran and the synthesized plan was checked against all of them.'),
    // Only when there is no phase graph above. The graph draws the same steps and
    // draws them live, so showing both would put two accounts of one pipeline on
    // one page — and the reader would have to work out that they agree. Plans made
    // by an older writ, which kept no phase record, still get the list.
    withStages ? el('ol', { class: 'stage-list' }, ...pipeline.stage_rows.map(stageRow)) : null,
    el(
      'div',
      { class: 'meta-row' },
      el('span', {}, `${plural(pipeline.requirements, 'requirement')} inventoried`),
      pipeline.unresolved_ambiguities
        ? el('span', { class: 'count warn' },
            el('b', {}, String(pipeline.unresolved_ambiguities)),
            el('span', { class: 'label' }, 'open questions'))
        : null,
      // A requirement nothing can demonstrate will be signed off on an agent's word
      // and nothing else, which is worth a reader's attention before approval.
      pipeline.undemonstrable.length
        ? el('span', { class: 'count bad' },
            el('b', {}, String(pipeline.undemonstrable.length)),
            el('span', { class: 'label' }, 'undemonstrable'))
        : null,
    ),
    pipeline.undemonstrable.length
      ? el('p', { class: 'muted small' }, 'no way to prove: ', ...pipeline.undemonstrable.map(code))
      : null,
    baselineRow(baseline),
    pipeline.directory ? el('pre', { class: 'command' }, pipeline.directory) : null,
  );
}

function stageRow(stage: PipelineStage): HTMLElement {
  return el(
    'li',
    { class: classes('stage', stage.state) },
    el(
      'header',
      {},
      el('span', { class: 'stage-mark' }, STAGE_MARKS[stage.state]),
      el('b', {}, stage.name),
      el('span', { class: classes('pill', stage.state) }, stage.state),
      stage.artifact ? code(stage.artifact) : null,
      stage.at ? el('span', { class: 'muted small', title: stage.at }, ago(stage.at)) : null,
    ),
    el('p', { class: 'muted small' }, stage.summary),
    stage.error ? el('p', { class: 'prose error' }, stage.error) : null,
  );
}

/**
 * What the repository's own suite did before any of this work started.
 *
 * `unknown` is its own case rather than being folded into a failure: an inventory
 * stage that did not report a baseline is a gap in the analysis, not a red suite.
 */
function baselineRow(baseline: Pipeline['baseline']): HTMLElement | null {
  if (!baseline || (!baseline.status && !baseline.commands.length)) return null;
  const bad = baseline.status === 'fail';
  return el(
    'div',
    { class: classes('baseline', bad && 'bad') },
    el(
      'header',
      {},
      el('b', {}, 'Baseline'),
      el('span', { class: classes('pill', bad ? 'failed' : baseline.status === 'pass' ? 'completed' : 'planned') },
        baseline.status || 'unknown'),
      bad
        ? el('span', { class: 'muted small' }, 'the suite was already failing when planning started')
        : null,
    ),
    baseline.commands.length
      ? el('div', { class: 'meta-row' }, el('span', {}, 'ran '), ...baseline.commands.map(code))
      : null,
    baseline.known_failures.length
      ? el('p', { class: 'muted small' },
          `${plural(baseline.known_failures.length, 'known failure')}: `,
          ...baseline.known_failures.slice(0, 6).map(code),
          baseline.known_failures.length > 6 ? el('span', {}, ' …') : null)
      : null,
  );
}

function statusCard(plan: Plan): HTMLElement {
  return el(
    'section',
    { class: classes('card', !plan.runnable && 'urgent') },
    el(
      'header',
      { class: 'plan-head' },
      el('h2', {}, 'Plan'),
      el('span', { class: classes('pill', plan.status) }, plan.status),
      el('span', { class: 'muted small' }, `revision ${plan.revision}`),
    ),
    el(
      'p',
      { class: 'muted' },
      plan.runnable
        ? 'Approved. `writ run` will dispatch work under this plan.'
        : 'Not approved: `writ run` will refuse to start.',
    ),
    el(
      'div',
      { class: 'meta-row' },
      el('span', {}, `${plural(plan.blocking, 'blocking finding')}`),
      el('span', {}, `${plan.advisory} advisory`),
      el('span', {}, `${plural(plan.requirements, 'requirement')}`),
      plan.uncovered.length
        ? el('span', { class: 'count bad' }, el('b', {}, String(plan.uncovered.length)), el('span', { class: 'label' }, 'uncovered'))
        : null,
      plan.open_repairs.length ? el('span', {}, `${plural(plan.open_repairs.length, 'open repair')}`) : null,
    ),
    plan.approved_by
      ? el(
          'div',
          { class: 'meta-row' },
          el('span', {}, `approved by ${plan.approved_by}`),
          plan.approved_at ? el('span', { title: plan.approved_at }, ago(plan.approved_at)) : null,
          // An approval that overruled findings is the one a later reader most
          // needs to see, so it is a badge rather than a line of prose.
          plan.forced ? el('span', { class: 'pill forced' }, 'forced') : null,
        )
      : null,
    plan.approval_note ? el('p', { class: 'prose' }, plan.approval_note) : null,
    !plan.runnable && !plan.blocking
      ? el('div', { class: 'ruling' },
          el('h4', {}, 'Nothing blocking is open'),
          el('p', { class: 'muted small' }, 'The status comes from a check, so re-check it to approve on that basis.'),
          el('pre', { class: 'command' }, 'writ check'))
      : null,
  );
}

/**
 * Gates parked on a human.
 *
 * First card on the page when it is present, because it is the one state where
 * nothing is running, nothing is broken, and nothing will change until a person
 * acts. `writ run` reports these as waiting; a dashboard that showed them the same
 * way it shows a running task would leave a project silently stopped.
 */
function heldCard(plan: Plan, handlers: PlanHandlers): HTMLElement {
  return el(
    'section',
    { class: 'card urgent' },
    el('h2', {}, `Held for a human (${plan.held_gates.length})`),
    el('p', { class: 'muted small' }, 'These gates will not re-run on their own. The work behind them is not broken — it is parked.'),
    el('div', { class: 'held-grid' }, ...plan.held_gates.map((gate) => el(
      'article',
      { class: 'held' },
      el('header', {}, taskLink(gate.id, handlers), el('span', { class: 'pill needs-repair' }, gate.reason)),
      el('pre', { class: 'command' }, `writ show ${gate.id}`),
    ))),
  );
}

function findingsCard(shown: Finding[], all: Finding[], filter: string): HTMLElement {
  const counts = Object.fromEntries(
    Object.entries(FINDING_FILTERS).map(([name, test]) => [name, all.filter(test).length]),
  );
  return el(
    'section',
    { class: 'card' },
    el('h2', {}, 'Findings'),
    el('div', { class: 'meta-row' },
      ...Object.keys(FINDING_FILTERS).map((name) =>
        el(
          'span',
          { class: classes('count', name === filter && 'selected', name === 'blocking' && counts[name] > 0 && 'bad') },
          el('b', {}, String(counts[name])),
          el('span', { class: 'label' }, name),
        )),
    ),
    shown.length
      ? el('div', { class: 'finding-list' }, ...shown.map(findingRow))
      : el('p', { class: 'empty' }, all.length
          ? 'Nothing in this view.'
          : 'No findings. Run `writ check` for the structural ones, `writ critique` for the read.'),
  );
}

function findingRow(finding: Finding): HTMLElement {
  const open = finding.disposition === 'open';
  return el(
    'article',
    { class: classes('finding', finding.severity, !open && 'answered') },
    el(
      'header',
      {},
      code(finding.id),
      el('span', { class: classes('pill', finding.severity) }, finding.severity),
      finding.where ? code(finding.where) : null,
      el('span', { class: 'muted small' }, finding.category),
      // Who raised it. `writ` is a deterministic check, `critic:coverage` an
      // independent reader, `gate:G-M01` the milestone's own review — different
      // kinds of claim, and the reader weighs them differently.
      el('span', { class: 'muted small' }, finding.source),
      !open ? el('span', { class: classes('pill', finding.disposition) }, finding.disposition) : null,
    ),
    el('p', { class: 'prose' }, finding.message),
    finding.suggested_action ? el('p', { class: 'muted small' }, '→ ', finding.suggested_action) : null,
    finding.reason ? el('p', { class: 'muted small' }, `${finding.disposition}: ${finding.reason}`) : null,
    open && finding.severity === 'error'
      ? el('pre', { class: 'command' },
          `writ set ${finding.id} accepted --reason "..."\nwrit set ${finding.id} declined --reason "..."`)
      : null,
  );
}

function coverageCard(coverage: Coverage[], handlers: PlanHandlers): HTMLElement {
  const holes = coverage.filter((row) => row.state === 'uncovered' || row.state === 'unevidenced');
  return el(
    'section',
    { class: classes('card', holes.length > 0 && 'urgent') },
    el('h2', {}, 'Requirements'),
    el('p', { class: 'muted small' },
      holes.length
        ? `${plural(holes.length, 'requirement')} with nothing to show for it.`
        : 'Every requirement has a task and a way of being verified.'),
    el('div', { class: 'coverage-list' }, ...coverage.map((row) => coverageRow(row, handlers))),
  );
}

function coverageRow(row: Coverage, handlers: PlanHandlers): HTMLElement {
  const total = row.tasks.length;
  return el(
    'article',
    { class: classes('coverage', row.state) },
    el(
      'header',
      {},
      code(row.id),
      el('span', { class: classes('pill', row.priority) }, row.priority),
      row.declared !== 'planned' ? el('span', { class: classes('pill', row.declared) }, row.declared) : null,
      el('span', { class: classes('pill', row.state) }, row.state),
    ),
    el('p', { class: 'prose' }, row.text),
    el(
      'div',
      { class: 'meta-row' },
      total
        ? el('span', {}, `${row.complete}/${total} complete `, ...row.tasks.map((id) => taskLink(id, handlers)))
        : el('span', { class: 'count bad' }, el('b', {}, '0'), el('span', { class: 'label' }, 'tasks')),
      row.gates.length ? el('span', {}, 'gates ', ...row.gates.map((id) => taskLink(id, handlers))) : null,
    ),
    row.reason ? el('p', { class: 'muted small' }, row.reason) : null,
    total ? meter(row.complete, total) : null,
  );
}

function repairsCard(repairs: Repair[], handlers: PlanHandlers): HTMLElement {
  return el(
    'section',
    { class: 'card' },
    el('h2', {}, 'Repairs'),
    el('p', { class: 'muted small' }, 'Every time a gate has asked for the plan itself to change.'),
    el('div', { class: 'repair-list' }, ...repairs.map((request) => el(
      'article',
      { class: classes('repair', request.status) },
      el(
        'header',
        {},
        code(request.id),
        el('span', { class: classes('pill', request.status) }, request.status),
        el('span', {}, 'from ', taskLink(request.gate, handlers)),
        el('span', { class: 'muted small' }, `round ${request.round}`),
        // A refused patch is writ turning down what the planner proposed. Two of
        // them is why a gate is held, so the count belongs on the row.
        request.refusals
          ? el('span', { class: 'count bad' }, el('b', {}, String(request.refusals)), el('span', { class: 'label' }, 'refused'))
          : null,
      ),
      el('p', { class: 'prose' }, request.summary),
      el(
        'div',
        { class: 'meta-row' },
        request.findings.length ? el('span', {}, 'closes ', ...request.findings.map(code)) : null,
        request.applied_tasks.length ? el('span', {}, 'added ', ...request.applied_tasks.map((id) => taskLink(id, handlers))) : null,
        request.opened_at ? el('span', { title: request.opened_at }, ago(request.opened_at)) : null,
      ),
    ))),
  );
}

function meter(done: number, total: number): HTMLElement {
  const pct = percent(done, total);
  return el(
    'div',
    { class: 'meter', role: 'img', 'aria-label': `${done} of ${total} tasks complete` },
    el('div', { class: 'meter-fill', style: `width:${pct}%` }),
  );
}

function taskLink(id: string, handlers: PlanHandlers): HTMLElement {
  if (!handlers.onTask) return code(id);
  const button = el('button', { class: 'id-link', type: 'button' }, id);
  button.addEventListener('click', () => handlers.onTask?.(id));
  return button;
}
