/**
 * The pre-execution phase: every agent writ runs before a single task is dispatched.
 *
 * This is the one card on the dashboard that can be busy while nothing has been
 * executed. `writ plan --critics --repair` is four to fourteen agent runs — three
 * analyses, a synthesis, the critics, then the repair rounds — and until this card
 * existed the page had nothing to say for the whole of it, because writ wrote
 * nothing to `state.json` until the commit at the end.
 *
 * Drawn from the server's geometry, for the same reason the task graph is: a column
 * is a wave, and waves come from `analysis.waves` and `critics.waves` — the
 * functions that decide what writ actually runs at once. Laying it out here would
 * be a second implementation of those rules, free to draw two boxes side by side
 * that writ intends to run one after the other.
 *
 * It shows the future as well as the past. A pending step is a box that is already
 * there, so a reader sees the shape of the attempt — which analyses, which critics,
 * whether repair is armed — rather than boxes appearing from nowhere one at a time.
 */

import { classes, code, el, svg, replace } from '../dom.js';
import { ago, duration, plural } from '../format.js';
import type { Phase, PhaseEdge, PhaseStep, StepOutput } from '../types.js';

/** Node geometry. Only the box: every position comes from the server. */
const STEP_W = 200;
const STEP_H = 64;

/**
 * Status marks. The same vocabulary as the terminal and the rest of the dashboard,
 * with `reused` reading as the absence of a run rather than a kind of success.
 */
const STEP_MARKS: Record<PhaseStep['status'], string> = {
  pending: '○',
  running: '*',
  ok: '✓',
  reused: '·',
  failed: '✗',
  skipped: '–',
  abandoned: '?',
};

/** What each kind of step is, for a reader who has not read the source. */
const KIND_WORDS: Record<PhaseStep['kind'], string> = {
  stage: 'analysis',
  synthesis: 'synthesis',
  commit: 'writ',
  critic: 'critic',
  repair: 'adjudicator',
  approval: 'writ',
};

export interface PhaseHandlers {
  onStep(id: string): void;
}

export function isPhase(phase: unknown): phase is Phase {
  return Boolean(phase) && typeof (phase as Phase).id === 'string' && (phase as Phase).id !== '';
}

/**
 * The phase as the Plan page's first card.
 *
 * Above the plan's own status, and in place of the pipeline's stage list, because
 * this answers a question that comes first in time: the status card says whether
 * work may start, and this says whether the thing being judged has finished being
 * built. During a planning run it is the only card with anything new to say.
 */
export function renderPhase(
  host: HTMLElement,
  phase: Phase,
  selected: string | null,
  handlers: PhaseHandlers,
): void {
  const done = (phase.counts.ok ?? 0) + (phase.counts.reused ?? 0);
  const failed = phase.counts.failed ?? 0;
  const holder = el('div', { class: 'phase-holder', 'data-scroll-key': 'phase' });
  holder.append(canvasFor(phase, selected, handlers));
  replace(host,
    el(
      'header',
      { class: 'plan-head' },
      el('h2', {}, 'Planning'),
      el('span', { class: classes('pill', phase.status) }, phase.status),
      phase.running
        ? el('span', { class: 'count live' },
            el('span', { class: 'spinner', 'aria-hidden': 'true' }),
            el('span', { class: 'label' }, liveLabel(phase)))
        : null,
      phase.plan_id ? code(phase.plan_id) : null,
      phase.started_at
        ? el('span', { class: 'muted small', title: phase.started_at }, ago(phase.started_at))
        : null,
    ),
    el('p', { class: 'muted small' }, sentence(phase)),
    holder,
    el(
      'div',
      { class: 'meta-row' },
      el('span', {}, `${done}/${phase.steps.length} steps`),
      failed
        ? el('span', { class: 'count bad' }, el('b', {}, String(failed)), el('span', { class: 'label' }, 'failed'))
        : null,
      phase.counts.skipped
        ? el('span', {}, `${phase.counts.skipped} not reached`)
        : null,
      phase.counts.abandoned
        ? el('span', { class: 'count warn' },
            el('b', {}, String(phase.counts.abandoned)),
            el('span', { class: 'label' }, 'abandoned'))
        : null,
      el('span', { class: 'muted small' }, 'a column runs at once'),
    ),
    phase.note ? el('p', { class: 'prose muted small' }, phase.note) : null,
  );
}

/** What the phase is doing, in one sentence, in the present tense while it runs. */
function sentence(phase: Phase): string {
  if (phase.running) {
    return 'Running the agents that produce the plan. Nothing is dispatched until this finishes and the plan is approved.';
  }
  if (phase.status === 'abandoned') {
    return 'The process that was planning is gone. Whatever step it was inside never reported; its transcript is on disk.';
  }
  if (phase.status === 'failed') {
    return 'Planning did not finish. What ran is below, and completed steps are reused if you run it again with the same --plan-id.';
  }
  if (phase.status === 'stopped') {
    return 'Planning stopped where it was asked to. Nothing after the last completed step was run.';
  }
  return 'Every step of the planning phase, in the order writ ran them.';
}

function liveLabel(phase: Phase): string {
  if (!phase.live.length) return 'working';
  const names = phase.steps.filter((step) => phase.live.includes(step.id)).map((step) => step.name);
  return names.length > 2 ? `${plural(names.length, 'agent')} running` : names.join(', ');
}

function canvasFor(phase: Phase, selected: string | null, handlers: PhaseHandlers): SVGElement {
  const canvas = svg('svg', {
    class: 'dag phase-dag',
    width: phase.width,
    height: phase.height,
    viewBox: `0 0 ${phase.width} ${phase.height}`,
    role: 'img',
    'aria-label': `planning phase, ${plural(phase.steps.length, 'step')}`,
  });
  for (const edge of phase.edges) canvas.append(stepEdge(edge));
  for (const step of phase.steps) {
    canvas.append(stepNode(step, step.id === selected, handlers));
  }
  return canvas;
}

function stepEdge(edge: PhaseEdge): SVGElement {
  const lift = Math.max(30, (edge.x2 - edge.x1) / 2);
  return svg('path', {
    class: classes('edge', edge.satisfied ? 'satisfied' : 'pending'),
    d: `M ${edge.x1} ${edge.y1} C ${edge.x1 + lift} ${edge.y1}, ${edge.x2 - lift} ${edge.y2}, ${edge.x2} ${edge.y2}`,
  });
}

function stepNode(step: PhaseStep, isSelected: boolean, handlers: PhaseHandlers): SVGElement {
  const group = svg('g', {
    class: classes('node', 'step', step.status, step.status === 'running' && 'live', isSelected && 'selected'),
    transform: `translate(${step.x} ${step.y})`,
    tabindex: 0,
    role: 'button',
    'aria-label': `${step.name}, ${KIND_WORDS[step.kind]}, ${step.status}`,
  });
  group.append(svg('title', {}, stepTooltip(step)));
  group.append(svg('rect', { class: 'box', width: STEP_W, height: STEP_H, rx: 8 }));
  group.append(svg('text', { class: 'node-mark', x: 11, y: 19 }, STEP_MARKS[step.status] ?? '·'));
  group.append(svg('text', { class: 'node-id', x: 26, y: 19 }, step.name));
  group.append(
    svg('text', { class: 'node-count', x: STEP_W - 11, y: 19, 'text-anchor': 'end' },
      duration(step.duration)),
  );
  group.append(svg('text', { class: 'node-title', x: 11, y: 38 }, step.summary || KIND_WORDS[step.kind]));
  group.append(
    svg('text', { class: 'node-status', x: 11, y: 54 },
      step.error ? 'failed' : step.note || step.status),
  );

  const select = () => handlers.onStep(step.id);
  group.addEventListener('click', select);
  group.addEventListener('keydown', (event) => {
    const key = (event as KeyboardEvent).key;
    if (key === 'Enter' || key === ' ') {
      event.preventDefault();
      select();
    }
  });
  return group;
}

function stepTooltip(step: PhaseStep): string {
  const lines = [`${step.name}  (${KIND_WORDS[step.kind]})`, step.status];
  if (step.summary) lines.push(step.summary);
  if (step.display) lines.push(step.display);
  if (step.duration !== null) lines.push(duration(step.duration));
  if (step.artifact) lines.push(`wrote ${step.artifact}`);
  if (step.error) lines.push(step.error);
  return lines.join('\n');
}

/** Long summaries are clipped in SVG, which has no text overflow of its own. */
export function fitStepTitles(host: HTMLElement): void {
  for (const text of host.querySelectorAll<SVGTextElement>('.step .node-title, .step .node-status')) {
    const limit = STEP_W - 22;
    let content = text.textContent ?? '';
    while (content.length > 1 && text.getComputedTextLength() > limit) {
      content = content.slice(0, -2);
      text.textContent = `${content}…`;
    }
  }
}

/**
 * One step in the drawer: what ran it, how it ended, and what it is saying.
 *
 * The command is here because it is the first thing anyone does with a step that
 * hung or wrote nothing: run its own invocation by hand. It is the resolved
 * command, with the model and event flags this step chose, not the agent's name.
 */
export function renderStepDetail(
  host: HTMLElement,
  step: PhaseStep,
  output: StepOutput | null,
): void {
  replace(host,
    el(
      'header',
      { class: 'detail-head' },
      el('h2', {}, step.name),
      el('span', { class: classes('pill', step.status) }, step.status),
      el('span', { class: 'muted small' }, KIND_WORDS[step.kind]),
    ),
    step.summary ? el('p', { class: 'muted' }, step.summary) : null,
    el(
      'div',
      { class: 'meta-row' },
      step.duration !== null ? el('span', {}, duration(step.duration)) : null,
      step.started_at
        ? el('span', { class: 'muted small', title: step.started_at }, `started ${ago(step.started_at)}`)
        : null,
      step.exit_code !== null ? el('span', {}, `exit ${step.exit_code}`) : null,
      step.model ? code(step.model) : null,
      step.artifact ? code(step.artifact) : null,
    ),
    step.note ? el('p', { class: 'muted small' }, step.note) : null,
    step.error ? el('p', { class: 'prose error' }, step.error) : null,
    step.command ? el('pre', { class: 'command' }, step.command) : null,
    step.directory ? el('pre', { class: 'command' }, step.directory) : null,
    ...outputPanes(step, output),
  );
}

function outputPanes(step: PhaseStep, output: StepOutput | null): (HTMLElement | null)[] {
  if (!step.has_output) {
    // A commit or an approval is writ's own work. An empty output pane here would
    // read as an agent that said nothing rather than as one that never existed.
    return [el('p', { class: 'muted small' }, 'This step is writ itself, not an agent: there is no transcript.')];
  }
  if (!output) return [el('p', { class: 'muted' }, 'Loading output…')];
  const activity = output.activity.length
    ? el('pre', { class: 'log activity', 'data-scroll-key': `step-activity:${step.id}` },
        output.activity.join('\n'))
    : null;
  const text = output.text.text
    ? el('pre', { class: 'log', 'data-scroll-key': `step-log:${step.id}` }, output.text.text)
    : null;
  if (!activity && !text) {
    return [
      el('p', { class: 'muted small' },
        step.status === 'pending'
          ? 'Not started yet.'
          : 'Nothing on the transcript yet. The agent has not produced output.'),
    ];
  }
  return [
    activity ? el('h3', {}, 'Activity') : null,
    activity,
    text ? el('h3', {}, 'Output') : null,
    text,
    output.text.truncated ? el('p', { class: 'muted small' }, 'showing the tail of a longer log') : null,
  ];
}
