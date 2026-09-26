/**
 * The overview: where the project stands, what is happening now, what it cost.
 *
 * A strip of headline figures across the top, then a two-column split: the
 * volatile things on the left (agents working now, what is waiting on a human)
 * and the record on the right (activity). Each figure in the strip links to the
 * view that explains it, so the overview is also the way into everything else.
 *
 * Ordered by what a reader needs first. Live agents come before totals because
 * during a run that is the only volatile thing on the page; proposed decisions
 * come next because they are the one class of work no `writ run` will ever
 * clear, so a project can sit there quietly blocked on a human.
 */

import { activate, classes, code, el, liveClock, replace } from '../dom.js';
import { ago, clock, duration, isLive, mark, percent, plural, roleLabel } from '../format.js';
import type { ActivityEvent, Overview, RunRow, Snapshot } from '../types.js';

export interface OverviewHandlers {
  onTask(id: string): void;
  onRun(id: string): void;
  onDecision(id: string): void;
  onGoto(view: string): void;
}

const STATUS_ORDER = [
  'running',
  'reviewing',
  'awaiting-review',
  'ready',
  'planned',
  'blocked',
  'failed',
  'cancelled',
  'completed',
];

export function renderOverview(
  host: HTMLElement,
  snapshot: Snapshot,
  handlers: OverviewHandlers,
): void {
  const { overview } = snapshot;
  replace(
    host,
    kpiStrip(overview, handlers),
    el(
      'div',
      { class: 'ov-split' },
      el(
        'div',
        { class: 'ov-column' },
        liveCard(overview, handlers),
        attentionCard(overview, handlers),
        statusBreakdown(overview),
      ),
      activityCard(snapshot.activity, handlers),
    ),
  );
}

/**
 * The headline figures. Each is a button to the view that holds the detail
 * behind it: the number is the question, the view is the answer.
 */
function kpiStrip(overview: Overview, handlers: OverviewHandlers): HTMLElement {
  const t = overview.throughput;
  const done = percent(overview.completed, overview.tasks);
  const kpi = (
    label: string,
    value: Node | string,
    note: Node | string | null,
    view: string,
    kind?: string,
  ) => {
    const node = el(
      'button',
      { class: classes('kpi', kind), type: 'button' },
      el('span', { class: 'kpi-label' }, label),
      el('span', { class: 'kpi-value' }, value),
      note ? el('span', { class: 'kpi-note' }, note) : null,
    );
    node.addEventListener('click', () => handlers.onGoto(view));
    return node;
  };
  return el(
    'div',
    { class: 'kpis' },
    kpi(
      'Progress',
      `${done}%`,
      el(
        'span',
        { class: 'kpi-meter' },
        el('span', { class: 'meter thin' }, el('span', { class: 'meter-fill', style: `width:${done}%` })),
        `${overview.completed}/${overview.tasks} tasks`,
      ),
      'tasks',
    ),
    kpi('Working now', String(overview.live), overview.live ? 'agents running' : 'idle', 'runs', overview.live ? 'live' : undefined),
    kpi(
      'To rule on',
      String(overview.proposed_decisions),
      'proposed decisions',
      'decisions',
      overview.proposed_decisions ? 'warn' : undefined,
    ),
    kpi('Runs', String(t.runs), `${t.reviews} review${t.reviews === 1 ? '' : 's'} · ${t.rejected} rejected`, 'runs'),
    kpi('Agent time', duration(t.agent_seconds) || '0s', `median run ${duration(t.median_seconds) || '—'}`, 'runs'),
    kpi(
      'Failures',
      String(t.failures),
      t.failures ? 'runs that failed' : 'none',
      'runs',
      t.failures ? 'bad' : undefined,
    ),
  );
}

function card(title: string, ...body: (Node | string | false | null)[]): HTMLElement {
  return el('section', { class: 'card' }, el('h2', {}, title), ...body);
}

/** A card whose heading carries a count, so the title is not a lie when empty. */
function countedCard(
  title: string,
  count: number,
  ...body: (Node | string | false | null)[]
): HTMLElement {
  return el(
    'section',
    { class: 'card' },
    el(
      'h2',
      {},
      title,
      count ? el('span', { class: 'h2-count' }, String(count)) : null,
    ),
    ...body,
  );
}

function liveCard(overview: Overview, handlers: OverviewHandlers): HTMLElement {
  const rows = overview.active_runs;
  return countedCard(
    'Working now',
    rows.length,
    rows.length
      ? el('ul', { class: 'run-list' }, ...rows.map((run) => liveRow(run, handlers)))
      : el('p', { class: 'blank' }, 'No agents running.'),
  );
}

function liveRow(run: RunRow, handlers: OverviewHandlers): HTMLElement {
  const row = el(
    'li',
    { class: 'run-row live' },
    el('span', { class: 'spinner', 'aria-hidden': 'true' }),
    el('span', { class: classes('role', run.role) }, roleLabel(run.role)),
    code(run.task),
    el('span', { class: 'grow' }),
    el('span', { class: 'muted mono small clip' }, run.model || run.command),
    el('span', { class: 'tnum' }, liveClock(0, run.started_at)),
  );
  activate(row, `run:${run.id}`, () => handlers.onRun(run.id));
  return row;
}

/**
 * What is waiting on a human. Proposed decisions and failures both qualify:
 * neither will clear on its own, and both are easy to miss while a run is
 * printing progress.
 */
function attentionCard(overview: Overview, handlers: OverviewHandlers): HTMLElement {
  const proposals = overview.proposed_decisions;
  const failed = overview.counts.failed ?? 0;
  const review = overview.counts['awaiting-review'] ?? 0;

  if (!proposals && !failed && !review) {
    return card('Waiting on you', el('p', { class: 'blank' }, 'Nothing needs a human.'));
  }

  const link = (label: string, view: string, kind: string) => {
    const button = el('button', { class: classes('need', kind), type: 'button' }, label);
    button.addEventListener('click', () => handlers.onGoto(view));
    return button;
  };

  return countedCard(
    'Waiting on you',
    proposals + failed,
    el(
      'div',
      { class: 'needs' },
      proposals
        ? link(`${plural(proposals, 'decision')} to rule on`, 'decisions', 'review')
        : null,
      failed ? link(`${plural(failed, 'task')} failed`, 'tasks', 'bad') : null,
      // Not a human's job, but worth distinguishing from idle: a run will pick
      // these up, so they are listed without the urgent styling.
      review ? link(`${review} awaiting review`, 'tasks', 'calm') : null,
    ),
    proposals
      ? el(
          'p',
          { class: 'muted small' },
          'Proposals stay inert until confirmed, so no run will clear them.',
        )
      : null,
  );
}

/** How the tasks divide by status, as one stacked bar and its legend. */
function statusBreakdown(overview: Overview): HTMLElement {
  const present = STATUS_ORDER.filter((status) => overview.counts[status as keyof typeof overview.counts]);
  const count = (status: string) => overview.counts[status as keyof typeof overview.counts] ?? 0;
  return card(
    'Tasks by status',
    overview.tasks
      ? el(
          'div',
          { class: 'stack-bar' },
          ...present.map((status) =>
            el('span', {
              class: classes('stack-seg', status),
              title: `${count(status)} ${status}`,
              style: `width:${percent(count(status), overview.tasks)}%`,
            }),
          ),
        )
      : null,
    el(
      'ul',
      { class: 'chips' },
      ...present.map((status) =>
        el(
          'li',
          { class: classes('chip', status) },
          el('span', { class: 'chip-mark' }, mark(status)),
          el('b', {}, String(count(status))),
          el('span', { class: 'chip-label' }, status),
        ),
      ),
    ),
  );
}

function activityCard(events: ActivityEvent[], handlers: OverviewHandlers): HTMLElement {
  return el(
    'section',
    { class: 'card ov-activity' },
    el('h2', {}, 'Recent activity'),
    events.length
      ? el('ol', { class: 'activity' }, ...events.slice(0, 24).map((e) => activityRow(e, handlers)))
      : el('p', { class: 'blank' }, 'Nothing has happened yet.'),
  );
}

/**
 * One line per event, whatever it carries. The agent's summary used to take a
 * second line, which made rows with one twice the height of rows without and
 * the list read as ragged. It now fills the space between the event and its
 * time, cut to fit; the whole sentence is in the row's tooltip and the run.
 */
function activityRow(event: ActivityEvent, handlers: OverviewHandlers): HTMLElement {
  const row = el(
    'li',
    { class: classes('event', event.kind, isLive(event.status) && 'live') },
    el('span', { class: classes('mark', event.status) }, mark(event.status)),
    event.role ? el('span', { class: classes('role', event.role) }, roleLabel(event.role)) : null,
    // The badge already says what kind of run it was, so a start reads as
    // "review · FT-002 started" rather than repeating the verb.
    el('span', { class: 'what' }, event.kind === 'run-started' ? `${event.task} started` : event.text),
    event.summary
      ? el('span', { class: 'event-summary' }, event.summary)
      : el('span', { class: 'grow' }),
    el('span', { class: 'when muted', title: event.at }, ago(event.at)),
  );
  row.setAttribute(
    'title',
    `${clock(event.at)} · ${event.text}${event.summary ? `\n${event.summary}` : ''}`,
  );
  if (event.run) {
    activate(row, `run:${event.run}`, () => handlers.onRun(event.run as string));
  } else if (event.decision) {
    activate(row, `decision:${event.decision}`, () => handlers.onDecision(event.decision as string));
  }
  return row;
}
