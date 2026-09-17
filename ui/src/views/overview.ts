/**
 * The overview: where the project stands, what is happening now, what it cost.
 *
 * Laid out as two regions rather than a bag of equal cards, because the cards
 * are not equal shapes. Progress, live agents, decisions and throughput are
 * fixed-height summaries; milestones and activity are lists that grow with the
 * project. Flowing all six through one `auto-fit` grid gave the timeline a
 * third of the width and made it eight hundred pixels tall while two thirds of
 * the row sat empty. So: a summary band across the top, then a two-column split
 * with the lists side by side.
 *
 * Ordered by what a reader needs first. Live agents come before totals because
 * during a run that is the only volatile thing on the page; proposed decisions
 * come next because they are the one class of work no `writ run` will ever
 * clear, so a project can sit there quietly blocked on a human.
 */

import { classes, code, el, replace } from '../dom.js';
import { ago, clock, duration, isLive, mark, percent, plural, ratio } from '../format.js';
import type { ActivityEvent, MilestoneRow, Overview, RunRow, Snapshot } from '../types.js';

export interface OverviewHandlers {
  onTask(id: string): void;
  onRun(id: string): void;
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
    el(
      'div',
      { class: 'ov-band' },
      progressCard(overview),
      liveCard(overview, handlers),
      attentionCard(overview, handlers),
    ),
    el(
      'div',
      { class: 'ov-split' },
      el(
        'div',
        { class: 'ov-column' },
        milestonesCard(overview, handlers),
        throughputCard(overview),
      ),
      activityCard(snapshot.activity, handlers),
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

function progressCard(overview: Overview): HTMLElement {
  const done = percent(overview.completed, overview.tasks);
  return el(
    'section',
    { class: 'card ov-progress' },
    el('h2', {}, 'Progress'),
    el(
      'div',
      { class: 'headline' },
      el('span', { class: 'big' }, `${done}%`),
      el(
        'span',
        { class: 'muted' },
        `${overview.completed} of ${plural(overview.tasks, 'task')} complete`,
      ),
    ),
    el('div', { class: 'meter' }, el('div', { class: 'meter-fill', style: `width:${done}%` })),
    el(
      'ul',
      { class: 'chips' },
      ...STATUS_ORDER.filter(
        (status) => overview.counts[status as keyof typeof overview.counts],
      ).map((status) =>
        el(
          'li',
          { class: classes('chip', status) },
          el('span', { class: 'chip-mark' }, mark(status)),
          el('b', {}, String(overview.counts[status as keyof typeof overview.counts])),
          el('span', { class: 'chip-label' }, status),
        ),
      ),
    ),
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
    el('button', { class: 'link', type: 'button', 'data-opens': `task:${run.task}` }, run.task),
    el('span', { class: 'verb' }, run.role === 'reviewer' ? 'review' : 'dispatch'),
    el('span', { class: 'grow' }),
    el('span', { class: 'muted mono small clip' }, run.model || run.command),
    el('span', { class: 'muted tnum', title: run.started_at ?? '' }, duration(run.duration)),
  );
  row.querySelector('button')?.addEventListener('click', () => handlers.onTask(run.task));
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

function throughputCard(overview: Overview): HTMLElement {
  const t = overview.throughput;
  const stat = (label: string, value: string, hint?: string) =>
    el(
      'div',
      { class: 'stat' },
      el('span', { class: 'stat-value' }, value),
      el('span', { class: 'stat-label' }, label),
      hint ? el('span', { class: 'stat-hint' }, hint) : null,
    );
  return card(
    'Agent work',
    el(
      'div',
      { class: 'stats' },
      stat('runs', String(t.runs)),
      stat('in agents', duration(t.agent_seconds)),
      stat('median run', duration(t.median_seconds)),
      stat('reviews', String(t.reviews), t.reviews ? `${t.rejected} rejected` : undefined),
      stat('failed', String(t.failures)),
    ),
  );
}

function milestonesCard(overview: Overview, handlers: OverviewHandlers): HTMLElement {
  const button = el('button', { class: 'link small', type: 'button' }, 'all milestones');
  button.addEventListener('click', () => handlers.onGoto('milestones'));
  return el(
    'section',
    { class: 'card' },
    el('h2', {}, 'Milestones', el('span', { class: 'grow' }), button),
    overview.milestones.length
      ? el(
          'ul',
          { class: 'milestone-list' },
          ...overview.milestones.map((m) => milestoneRow(m, handlers)),
        )
      : el('p', { class: 'blank' }, 'No milestones.'),
  );
}

/**
 * A milestone as a grid row, not a flex line. The parts have wildly different
 * widths — a two-word title next to a long one — and flex-wrap turned that into
 * a ragged block per milestone. A grid keeps id, bar and count in a column.
 */
function milestoneRow(m: MilestoneRow, handlers: OverviewHandlers): HTMLElement {
  const done = percent(m.done, m.total);
  const row = el(
    'li',
    { class: classes('milestone', m.status, 'clickable'), tabindex: 0, role: 'button' },
    el('span', { class: classes('mark', m.status) }, mark(m.status)),
    code(m.id),
    el('span', { class: 'title clip' }, m.title),
    el('div', { class: 'meter thin' }, el('div', { class: 'meter-fill', style: `width:${done}%` })),
    el('span', { class: 'muted mono tnum' }, ratio(m.done, m.total)),
  );
  const open = () => handlers.onGoto('milestones');
  row.addEventListener('click', open);
  row.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      open();
    }
  });
  return row;
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
 * Two lines, not one. The event text and its summary were competing for a
 * single ellipsised row, which cut both; the summary is the agent's own sentence
 * about what it did, so it gets its own line under the event.
 */
function activityRow(event: ActivityEvent, handlers: OverviewHandlers): HTMLElement {
  const row = el(
    'li',
    { class: classes('event', event.kind, isLive(event.status) && 'live') },
    el('span', { class: classes('mark', event.status) }, mark(event.status)),
    el(
      'div',
      { class: 'event-body' },
      el(
        'div',
        { class: 'event-line' },
        el('span', { class: 'what' }, event.text),
        el('span', { class: 'grow' }),
        el('span', { class: 'when muted', title: event.at }, ago(event.at)),
      ),
      event.summary ? el('p', { class: 'event-summary' }, event.summary) : null,
    ),
  );
  row.setAttribute('title', `${clock(event.at)} · ${event.text}`);
  if (event.run) {
    row.classList.add('clickable');
    row.setAttribute('data-opens', `run:${event.run}`);
    row.addEventListener('click', () => handlers.onRun(event.run as string));
  }
  return row;
}
