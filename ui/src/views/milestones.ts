/**
 * Milestones with their tasks: the plan's own structure.
 *
 * A milestone is how the design document was divided, so this is the view that
 * answers "how far through the plan are we" rather than "what is running". Tasks
 * are shown in id order here, not sorted by status, because within a milestone
 * the numbering is the intended sequence.
 */

import { classes, code, el } from '../dom.js';
import { mark, percent, ratio } from '../format.js';
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
    ...milestones.map((milestone) => {
      const own = tasks
        .filter((task) => task.milestone === milestone.id)
        .sort((a, b) => a.id.localeCompare(b.id));
      const done = percent(milestone.done, milestone.total);
      return el(
        'section',
        { class: classes('card milestone-card', milestone.status) },
        el(
          'header',
          { class: 'milestone-head' },
          el('span', { class: 'mark' }, mark(milestone.status)),
          code(milestone.id),
          el('h2', {}, milestone.title),
          el('span', { class: classes('pill', milestone.status) }, milestone.status),
          el('span', { class: 'grow' }),
          el('span', { class: 'muted mono' }, ratio(milestone.done, milestone.total)),
        ),
        el('div', { class: 'meter' }, el('div', { class: 'meter-fill', style: `width:${done}%` })),
        el(
          'ul',
          { class: 'task-list compact' },
          ...own.map((task) => {
            const row = el(
              'li',
              {
                class: classes('task-row', task.status, 'clickable'),
                tabindex: 0,
                role: 'button',
              },
              el('span', { class: 'mark' }, mark(task.status)),
              code(task.id),
              el('span', { class: 'title' }, task.title),
              el('span', { class: 'grow' }),
              el('span', { class: 'muted mono' }, ratio(task.passed, task.total)),
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
          }),
        ),
      );
    }),
  );
}
