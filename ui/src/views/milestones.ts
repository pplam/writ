/**
 * Milestones with their tasks: the plan's own structure.
 *
 * A milestone is how the design document was divided, so this is the view that
 * answers "how far through the plan are we" rather than "what is running". Tasks
 * are shown in id order, not sorted by status, because within a milestone the
 * numbering is the intended sequence — and a milestone whose tasks jumped around
 * as agents worked would be unreadable as a plan.
 *
 * The header is a grid rather than a wrapping flex line. Everything after the
 * title used to wrap under it at narrow widths, so the count and the status
 * pill ended up in a second row that looked like content.
 */

import { classes, code, el } from '../dom.js';
import { mark, percent, plural, ratio } from '../format.js';
import type { MilestoneRow, TaskRow } from '../types.js';

export interface MilestoneHandlers {
  onTask(id: string): void;
}

export function renderMilestones(
  host: HTMLElement,
  milestones: MilestoneRow[],
  tasks: TaskRow[],
  handlers: MilestoneHandlers,
): void {
  if (!milestones.length) {
    host.replaceChildren(el('p', { class: 'empty' }, 'No milestones yet.'));
    return;
  }
  host.replaceChildren(
    ...milestones.map((milestone) => milestoneCard(milestone, tasks, handlers)),
  );
}

function milestoneCard(
  milestone: MilestoneRow,
  tasks: TaskRow[],
  handlers: MilestoneHandlers,
): HTMLElement {
  const own = tasks
    .filter((task) => task.milestone === milestone.id)
    .sort((a, b) => a.id.localeCompare(b.id));
  const done = percent(milestone.done, milestone.total);
  const left = milestone.total - milestone.done;

  return el(
    'section',
    { class: classes('card milestone-card', milestone.status) },
    el(
      'header',
      { class: 'milestone-head' },
      el('span', { class: classes('mark', milestone.status) }, mark(milestone.status)),
      code(milestone.id),
      el('h2', { class: 'clip' }, milestone.title),
      el('span', { class: classes('pill', milestone.status) }, milestone.status),
      el(
        'div',
        { class: 'milestone-progress' },
        el(
          'div',
          { class: 'meter thin' },
          el('div', { class: 'meter-fill', style: `width:${done}%` }),
        ),
        el(
          'span',
          { class: 'muted mono tnum' },
          `${ratio(milestone.done, milestone.total)}`,
        ),
      ),
    ),
    own.length
      ? el('ul', { class: 'task-list compact' }, ...own.map((task) => milestoneTaskRow(task, handlers)))
      : el('p', { class: 'blank' }, 'No tasks in this milestone.'),
    left
      ? el('p', { class: 'milestone-foot muted small' }, `${plural(left, 'task')} left`)
      : null,
  );
}

function milestoneTaskRow(task: TaskRow, handlers: MilestoneHandlers): HTMLElement {
  const row = el(
    'li',
    {
      class: classes('task-row', task.status, 'clickable'),
      tabindex: 0,
      role: 'button',
      // See tasks.ts: names the detail this row opens, so dismissing the drawer
      // can return focus to it after the list has been re-rendered.
      'data-opens': `task:${task.id}`,
    },
    el('span', { class: classes('mark', task.status) }, mark(task.status)),
    code(task.id),
    el('span', { class: 'title clip' }, task.title),
    el('span', { class: 'grow' }),
    task.blocked_by.length
      ? el(
          'span',
          { class: 'muted small', title: `waiting on ${task.blocked_by.join(', ')}` },
          `waits on ${task.blocked_by.length}`,
        )
      : null,
    el('span', { class: 'muted mono tnum' }, ratio(task.passed, task.total)),
    el('span', { class: classes('pill', task.status) }, task.status),
  );
  row.addEventListener('click', () => handlers.onTask(task.id));
  row.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      handlers.onTask(task.id);
    }
  });
  return row;
}
