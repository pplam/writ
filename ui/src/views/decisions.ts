/**
 * Decisions an agent recorded, as a register, proposals first.
 *
 * A proposal is a fork an agent hit that the design document did not settle. It
 * stays inert until a human rules on it, which makes it the one kind of work no
 * `writ run` will ever clear — so the detail shows the ruling command rather than
 * a button. The dashboard is read-only, and a decision is exactly the kind of
 * thing that should be typed deliberately with a reason attached.
 *
 * A table rather than a card per decision: a project collects dozens, and a card
 * that prints context, decision and consequences in full made the page a wall
 * of prose in which the three proposals that needed a ruling were hard to find.
 * The register answers "which, from where, in what state"; the drawer holds the
 * reasoning for the one being read.
 */

import { activate, classes, code, el, replace } from '../dom.js';
import { ago, stamp } from '../format.js';
import type { Decision } from '../types.js';

export interface DecisionHandlers {
  onSelect(id: string): void;
  onTask(id: string): void;
}

export const DECISION_FILTERS: Record<string, (decision: Decision) => boolean> = {
  all: () => true,
  proposed: (d) => d.status === 'proposed',
  active: (d) => d.status === 'active',
  superseded: (d) => d.status === 'superseded',
  rejected: (d) => d.status === 'rejected',
};

/** Proposals first, since they are the ones waiting; then newest first. */
function decisionWeight(decision: Decision): number {
  return decision.status === 'proposed' ? 0 : decision.status === 'active' ? 1 : 2;
}

export function renderDecisions(
  host: HTMLElement,
  decisions: Decision[],
  options: { filter: string; query: string; selected: string | null },
  handlers: DecisionHandlers,
): void {
  if (!decisions.length) {
    replace(host,
      el('p', { class: 'empty' }, 'No decisions recorded. Agents propose them as they work.'),
    );
    return;
  }
  const predicate = DECISION_FILTERS[options.filter] ?? DECISION_FILTERS.all;
  const query = options.query.trim().toLowerCase();
  const rows = decisions
    .filter(predicate)
    .filter(
      (d) =>
        !query ||
        d.id.toLowerCase().includes(query) ||
        d.title.toLowerCase().includes(query) ||
        d.task.toLowerCase().includes(query),
    )
    .sort((a, b) => decisionWeight(a) - decisionWeight(b) || (b.at || '').localeCompare(a.at || ''));

  if (!rows.length) {
    replace(host, el('p', { class: 'empty' }, 'No decisions match.'));
    return;
  }
  replace(host,
    el(
      'table',
      { class: 'data-table decision-table' },
      el(
        'thead',
        {},
        el(
          'tr',
          {},
          el('th', { class: 'col-id' }, 'Decision'),
          el('th', {}, 'Title'),
          el('th', { class: 'col-status' }, 'Status'),
          el('th', { class: 'col-id' }, 'From task'),
          el('th', { class: 'col-by' }, 'By'),
          el('th', { class: 'col-when' }, 'Recorded'),
        ),
      ),
      el('tbody', {}, ...rows.map((d) => decisionRow(d, d.id === options.selected, handlers))),
    ),
  );
}

function decisionRow(decision: Decision, isSelected: boolean, handlers: DecisionHandlers): HTMLElement {
  const row = el(
    'tr',
    { class: classes('decision-row', decision.status, isSelected && 'selected') },
    el('td', { class: 'col-id' }, code(decision.id)),
    el(
      'td',
      { class: 'col-title' },
      el(
        'div',
        { class: 'cell-title' },
        el('span', { class: 'clip' }, decision.title),
        decision.supersedes
          ? el('span', { class: 'tag', title: `supersedes ${decision.supersedes}` }, `↺ ${decision.supersedes}`)
          : null,
      ),
    ),
    el('td', { class: 'col-status' }, el('span', { class: classes('pill', decision.status) }, decision.status)),
    el('td', { class: 'col-id' }, decision.task ? code(decision.task) : el('span', { class: 'muted' }, '—')),
    el('td', { class: 'col-by muted small' }, el('span', { class: 'clip' }, decision.by || '—')),
    el('td', { class: 'col-when muted', title: decision.at }, ago(decision.at)),
  );
  activate(row, `decision:${decision.id}`, () => handlers.onSelect(decision.id));
  return row;
}

export function renderDecisionDetail(
  host: HTMLElement,
  decision: Decision,
  handlers: DecisionHandlers,
): void {
  const taskButton = decision.task
    ? el('button', { class: 'link mono', type: 'button' }, decision.task)
    : null;
  taskButton?.addEventListener('click', () => handlers.onTask(decision.task));

  const item = (label: string, value: Node | string) =>
    el(
      'div',
      { class: 'meta-item' },
      el('span', { class: 'meta-label' }, label),
      el('span', { class: 'meta-value' }, value),
    );

  replace(host,
    el(
      'article',
      { class: classes('decision', decision.status) },
      el(
        'header',
        { class: 'detail-head' },
        el(
          'div',
          { class: 'detail-title' },
          code(decision.id),
          el('span', { class: classes('pill', decision.status) }, decision.status),
        ),
        el('h2', {}, decision.title),
      ),
      el(
        'div',
        { class: 'meta-row' },
        decision.by ? item('by', decision.by) : null,
        taskButton ? item('from task', taskButton) : null,
        decision.at ? item('recorded', stamp(decision.at)) : null,
        decision.supersedes ? item('supersedes', code(decision.supersedes)) : null,
      ),
      decision.status === 'proposed' ? ruling(decision) : null,
      field('Context', decision.context),
      field('Decision', decision.decision),
      field('Consequences', decision.consequences),
      decision.reason ? field('Reason given', decision.reason) : null,
    ),
  );
}

function field(label: string, value: string): HTMLElement | null {
  if (!value) return null;
  return el('div', { class: 'field' }, el('h4', {}, label), el('p', { class: 'prose' }, value));
}

function ruling(decision: Decision): HTMLElement {
  return el(
    'div',
    { class: 'ruling' },
    el('h4', {}, 'Waiting on a ruling'),
    el(
      'p',
      { class: 'muted small' },
      'An agent hit a fork the design did not settle. Until you rule, this is recorded but not in force.',
    ),
    el('pre', { class: 'command' }, `writ set ${decision.id} active\nwrit set ${decision.id} rejected --reason "..."`),
  );
}
