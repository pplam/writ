/**
 * The overview: where the project stands, what is happening now, what it cost.
 *
 * Ordered by what a reader needs first. Live agents come before totals because
 * during a run that is the only volatile thing on the page; proposed decisions
 * come next because they are the one class of work no `writ run` will ever
 * clear, so a project can sit there quietly blocked on a human.
 */

import { classes, code, el, replace } from '../dom.js';
import { ago, clock, duration, isLive, mark, percent, plural, ratio } from '../format.js';
import type { ActivityEvent, Overview, RunRow, Snapshot } from '../types.js';

export interface OverviewHandlers {
  onTask(id: string): void;
  onRun(id: string): void;
  onGoto(view: string): void;
}

export function renderOverview(
  host: HTMLElement,
  snapshot: Snapshot,
  handlers: OverviewHandlers,
): void {
  const { overview } = snapshot;
  replace(
    host,
    progressCard(overview),
    liveCard(overview, handlers),
    decisionsCard(overview, handlers),
    throughputCard(overview),
    milestonesCard(overview),
    activityCard(snapshot.activity, handlers),
  );
}

function card(title: string, ...body: (Node | string | false | null)[]): HTMLElement {
  return el('section', { class: 'card' }, el('h2', {}, title), ...body);
}

function progressCard(overview: Overview): HTMLElement {
  const done = percent(overview.completed, overview.tasks);
  const order = [
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
  return card(
    'Progress',
    el(
      'div',
      { class: 'headline' },
      el('span', { class: 'big' }, `${done}%`),
      el('span', { class: 'muted' }, `${overview.completed} of ${plural(overview.tasks, 'task')} complete`),
    ),
    el('div', { class: 'meter' }, el('div', { class: 'meter-fill', style: `width:${done}%` })),
    el(
      'ul',
      { class: 'chips' },
      ...order
        .filter((status) => overview.counts[status as keyof typeof overview.counts])
        .map((status) =>
          el(
            'li',
            { class: classes('chip', status) },
            el('span', { class: 'chip-mark' }, mark(status)),
            String(overview.counts[status as keyof typeof overview.counts]),
            el('span', { class: 'chip-label' }, status),
          ),
        ),
    ),
  );
}

function liveCard(overview: Overview, handlers: OverviewHandlers): HTMLElement {
  const rows = overview.active_runs;
  return card(
    rows.length ? `Working now (${rows.length})` : 'Working now',
    rows.length
      ? el('ul', { class: 'run-list' }, ...rows.map((run) => liveRow(run, handlers)))
      : el('p', { class: 'muted' }, 'No agents running.'),
  );
}

function liveRow(run: RunRow, handlers: OverviewHandlers): HTMLElement {
  const verb = run.role === 'reviewer' ? 'review' : 'dispatch';
  const row = el(
    'li',
    { class: 'run-row live' },
    el('span', { class: 'spinner', 'aria-hidden': 'true' }),
    el('button', { class: 'link', type: 'button' }, run.task),
    el('span', { class: 'verb' }, verb),
    el('span', { class: 'muted mono' }, run.model || run.command),
    el('span', { class: 'grow' }),
    el('span', { class: 'muted', title: run.started_at ?? '' }, duration(run.duration)),
  );
  row.querySelector('button')?.addEventListener('click', () => handlers.onTask(run.task));
  return row;
}

function decisionsCard(overview: Overview, handlers: OverviewHandlers): HTMLElement {
  const count = overview.proposed_decisions;
  if (!count) {
    return card('Decisions', el('p', { class: 'muted' }, 'Nothing waiting on a ruling.'));
  }
  const button = el(
    'button',
    { class: 'link', type: 'button' },
    `${plural(count, 'decision')} waiting on you`,
  );
  button.addEventListener('click', () => handlers.onGoto('decisions'));
  return card(
    'Decisions',
    el('p', { class: 'warn' }, button),
    el('p', { class: 'muted small' }, 'Proposals stay inert until confirmed, so no run will clear them.'),
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
      stat('agent runs', String(t.runs)),
      stat('time in agents', duration(t.agent_seconds)),
      stat('median run', duration(t.median_seconds)),
      stat('reviews', String(t.reviews), t.reviews ? `${t.rejected} rejected` : undefined),
      stat('failed runs', String(t.failures)),
    ),
  );
}

function milestonesCard(overview: Overview): HTMLElement {
  return card(
    'Milestones',
    overview.milestones.length
      ? el(
          'ul',
          { class: 'milestone-list' },
          ...overview.milestones.map((m) => {
            const done = percent(m.done, m.total);
            return el(
              'li',
              { class: 'milestone' },
              el('span', { class: 'mark' }, mark(m.status)),
              code(m.id),
              el('span', { class: 'title' }, m.title),
              el('span', { class: 'grow' }),
              el('span', { class: 'muted mono' }, ratio(m.done, m.total)),
              el('div', { class: 'meter thin' }, el('div', { class: 'meter-fill', style: `width:${done}%` })),
            );
          }),
        )
      : el('p', { class: 'muted' }, 'No milestones.'),
  );
}

function activityCard(events: ActivityEvent[], handlers: OverviewHandlers): HTMLElement {
  return card(
    'Recent activity',
    events.length
      ? el('ol', { class: 'activity' }, ...events.slice(0, 14).map((e) => activityRow(e, handlers)))
      : el('p', { class: 'muted' }, 'Nothing has happened yet.'),
  );
}

function activityRow(event: ActivityEvent, handlers: OverviewHandlers): HTMLElement {
  const row = el(
    'li',
    { class: classes('event', event.kind, isLive(event.status) && 'live') },
    el('span', { class: 'mark' }, mark(event.status)),
    el('span', { class: 'when', title: event.at }, clock(event.at)),
    el('span', { class: 'what' }, event.text),
    event.summary ? el('span', { class: 'muted small' }, event.summary) : null,
    el('span', { class: 'grow' }),
    el('span', { class: 'muted small' }, ago(event.at)),
  );
  if (event.run) {
    row.classList.add('clickable');
    row.addEventListener('click', () => handlers.onRun(event.run as string));
  }
  return row;
}
