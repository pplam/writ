/**
 * The task list and the task detail panel.
 *
 * The list is filterable because the useful questions are subsets: what is
 * blocked, what is waiting on me, what failed. The detail panel exists because
 * `writ show` is the command people run most and it answers "what is this task
 * supposed to prove, and what has it proved so far".
 */

import { classes, code, el, replace } from '../dom.js';
import { ago, isLive, mark, ratio, statusWeight } from '../format.js';
import type { Acceptance, Task, TaskRow } from '../types.js';

export interface TaskHandlers {
  onSelect(id: string): void;
  onRun(id: string): void;
}

export const FILTERS: Record<string, (task: TaskRow) => boolean> = {
  all: () => true,
  live: (t) => isLive(t.status),
  ready: (t) => t.status === 'ready',
  'awaiting review': (t) => t.status === 'awaiting-review',
  blocked: (t) => t.status === 'blocked' || t.blocked_by.length > 0,
  failed: (t) => t.status === 'failed',
  done: (t) => t.status === 'completed',
};

export function renderTaskList(
  host: HTMLElement,
  tasks: TaskRow[],
  options: { filter: string; query: string; selected: string | null },
  handlers: TaskHandlers,
): void {
  const predicate = FILTERS[options.filter] ?? FILTERS.all;
  const query = options.query.trim().toLowerCase();
  const rows = tasks
    .filter(predicate)
    .filter(
      (task) =>
        !query ||
        task.id.toLowerCase().includes(query) ||
        task.title.toLowerCase().includes(query),
    )
    .sort((a, b) => statusWeight(a.status) - statusWeight(b.status) || a.id.localeCompare(b.id));

  replace(host,
    rows.length
      ? el('ul', { class: 'task-list' }, ...rows.map((t) => taskRow(t, t.id === options.selected, handlers)))
      : el('p', { class: 'empty' }, 'No tasks match.'),
  );
}

function taskRow(task: TaskRow, isSelected: boolean, handlers: TaskHandlers): HTMLElement {
  const row = el(
    'li',
    {
      class: classes('task-row', task.status, isLive(task.status) && 'live', isSelected && 'selected'),
      tabindex: 0,
      role: 'button',
    },
    el('span', { class: 'mark' }, mark(task.status)),
    code(task.id),
    el('span', { class: 'title' }, task.title),
    el('span', { class: 'grow' }),
    task.blocked_by.length
      ? el('span', { class: 'muted small', title: `waiting on ${task.blocked_by.join(', ')}` },
          `waits on ${task.blocked_by.length}`)
      : null,
    el('span', { class: 'muted mono' }, ratio(task.passed, task.total)),
    el('span', { class: classes('pill', task.status) }, task.status),
  );
  const select = () => handlers.onSelect(task.id);
  row.addEventListener('click', select);
  row.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      select();
    }
  });
  return row;
}

export function renderTaskDetail(
  host: HTMLElement,
  task: Task,
  handlers: TaskHandlers,
): void {
  replace(host,
    el(
      'header',
      { class: 'detail-head' },
      el('span', { class: classes('mark', task.status) }, mark(task.status)),
      code(task.id),
      el('h2', {}, task.title),
      el('span', { class: classes('pill', task.status) }, task.status),
    ),
    metaRow(task),
    acceptanceSection(task),
    task.notes ? section('Notes', el('p', { class: 'prose' }, task.notes)) : null,
    guardrailSection(task),
    dependencySection(task),
    runSection(task, handlers),
    evidenceSection(task),
  );
}

function section(title: string, ...body: (Node | null | false)[]): HTMLElement {
  return el('section', { class: 'detail-section' }, el('h3', {}, title), ...body);
}

function metaRow(task: Task): HTMLElement {
  const bits: HTMLElement[] = [];
  if (task.milestone) bits.push(el('span', {}, 'milestone ', code(task.milestone)));
  if (task.design_section) bits.push(el('span', {}, `§ ${task.design_section}`));
  if (task.design_doc) bits.push(el('span', { class: 'mono small' }, task.design_doc));
  bits.push(el('span', { title: task.updated_at }, `updated ${ago(task.updated_at)}`));
  return el('div', { class: 'meta-row' }, ...bits);
}

function acceptanceSection(task: Task): HTMLElement {
  if (!task.acceptances.length) {
    return section('Acceptance', el('p', { class: 'muted' }, 'No criteria recorded.'));
  }
  return section(
    `Acceptance (${ratio(task.passed, task.total)})`,
    el('ol', { class: 'criteria' }, ...task.acceptances.map(criterion)),
  );
}

function criterion(item: Acceptance): HTMLElement {
  return el(
    'li',
    { class: classes('criterion', item.status) },
    el(
      'div',
      { class: 'criterion-head' },
      el('span', { class: 'mark' }, item.status === 'passed' ? '+' : item.status === 'failed' ? '×' : '·'),
      el('span', { class: 'criterion-text' }, item.text),
      el('span', { class: classes('pill', item.status) }, item.status),
    ),
    // Evidence is the point of a criterion: a claim with nothing behind it is
    // exactly what the reviewer is there to catch, so it is shown, not hidden.
    item.evidence ? el('p', { class: 'evidence' }, item.evidence) : null,
    item.by ? el('p', { class: 'muted small', title: item.at }, `${item.by} · ${ago(item.at)}`) : null,
  );
}

function guardrailSection(task: Task): HTMLElement | null {
  if (!task.allowed.length && !task.forbidden.length) return null;
  return section(
    'Guardrails',
    task.allowed.length
      ? el('div', { class: 'rail allowed' }, el('h4', {}, 'may touch'),
          el('ul', {}, ...task.allowed.map((p) => el('li', { class: 'mono' }, p))))
      : null,
    task.forbidden.length
      ? el('div', { class: 'rail forbidden' }, el('h4', {}, 'must not touch'),
          el('ul', {}, ...task.forbidden.map((p) => el('li', { class: 'mono' }, p))))
      : null,
  );
}

function dependencySection(task: Task): HTMLElement | null {
  if (!task.depends_on.length && !task.blocks.length && !task.unknown_deps.length) return null;
  const list = (ids: string[]) =>
    el('div', { class: 'dep-line' }, ...ids.map((id) => code(id)));
  return section(
    'Dependencies',
    task.depends_on.length
      ? el('div', {}, el('h4', {}, 'after'), list(task.depends_on))
      : null,
    task.blocked_by.length
      ? el('div', {}, el('h4', { class: 'warn' }, 'still waiting on'), list(task.blocked_by))
      : null,
    task.blocks.length ? el('div', {}, el('h4', {}, 'blocks'), list(task.blocks)) : null,
    // A dangling id means this task can never become ready. Say so here rather
    // than letting it sit in the list looking merely slow.
    task.unknown_deps.length
      ? el(
          'div',
          {},
          el('h4', { class: 'error' }, 'missing, so this can never become ready'),
          list(task.unknown_deps),
        )
      : null,
  );
}

function runSection(task: Task, handlers: TaskHandlers): HTMLElement {
  if (!task.run_list.length) {
    return section('Runs', el('p', { class: 'muted' }, 'Never dispatched.'));
  }
  return section(
    `Runs (${task.run_list.length})`,
    el(
      'ul',
      { class: 'run-list' },
      ...[...task.run_list].reverse().map((run) => {
        const row = el(
          'li',
          { class: classes('run-row', run.status, isLive(run.status) && 'live', 'clickable') },
          el('span', { class: 'mark' }, mark(run.status)),
          el('span', { class: 'verb' }, run.role === 'reviewer' ? 'review' : 'dispatch'),
          code(run.id),
          el('span', { class: 'muted mono small' }, run.model || run.command),
          el('span', { class: 'grow' }),
          run.decision ? el('span', { class: classes('pill', run.decision) }, run.decision) : null,
          el('span', { class: 'muted small', title: run.started_at ?? '' }, ago(run.started_at)),
        );
        row.addEventListener('click', () => handlers.onRun(run.id));
        return row;
      }),
    ),
  );
}

function evidenceSection(task: Task): HTMLElement | null {
  if (!task.evidence.length) return null;
  return section(
    'History',
    el(
      'ol',
      { class: 'history' },
      ...[...task.evidence].reverse().map((item) =>
        el(
          'li',
          {},
          el('span', { class: 'muted small', title: item.at }, ago(item.at)),
          el('span', { class: 'actor' }, item.actor),
          el('span', {}, item.text),
        ),
      ),
    ),
  );
}
