/**
 * Decisions an agent recorded, proposals first.
 *
 * A proposal is a fork an agent hit that the design document did not settle. It
 * stays inert until a human rules on it, which makes it the one kind of work no
 * `writ run` will ever clear — so this view shows the ruling command rather than
 * a button. The dashboard is read-only, and a decision is exactly the kind of
 * thing that should be typed deliberately with a reason attached.
 */

import { classes, code, el, replace } from '../dom.js';
import { ago } from '../format.js';
import type { Decision } from '../types.js';

export function renderDecisions(host: HTMLElement, decisions: Decision[]): void {
  if (!decisions.length) {
    replace(host,
      el('p', { class: 'empty' }, 'No decisions recorded. Agents propose them as they work.'),
    );
    return;
  }
  const proposed = decisions.filter((d) => d.status === 'proposed');
  const settled = decisions.filter((d) => d.status !== 'proposed');
  replace(host,
    proposed.length
      ? el(
          'section',
          { class: 'card urgent' },
          el('h2', {}, `Waiting on a ruling (${proposed.length})`),
          el(
            'p',
            { class: 'muted small' },
            'An agent hit a fork the design did not settle. Until you rule, this is recorded but not in force.',
          ),
          el('div', { class: 'decision-grid' }, ...proposed.map(decisionCard)),
        )
      : null,
    settled.length
      ? el(
          'section',
          { class: 'card' },
          el('h2', {}, 'Settled'),
          el('div', { class: 'decision-grid' }, ...settled.map(decisionCard)),
        )
      : null,
  );
}

function decisionCard(decision: Decision): HTMLElement {
  return el(
    'article',
    { class: classes('decision', decision.status) },
    el(
      'header',
      {},
      code(decision.id),
      el('h3', {}, decision.title),
      el('span', { class: classes('pill', decision.status) }, decision.status),
    ),
    el(
      'div',
      { class: 'meta-row' },
      decision.by ? el('span', {}, decision.by) : null,
      decision.task ? el('span', {}, 'from ', code(decision.task)) : null,
      decision.at ? el('span', { title: decision.at }, ago(decision.at)) : null,
      decision.supersedes ? el('span', {}, 'supersedes ', code(decision.supersedes)) : null,
    ),
    field('Context', decision.context),
    field('Decision', decision.decision),
    field('Consequences', decision.consequences),
    decision.reason ? field('Reason given', decision.reason) : null,
    decision.status === 'proposed' ? ruling(decision) : null,
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
    el('h4', {}, 'To rule on this'),
    el('pre', { class: 'command' }, `writ set ${decision.id} active\nwrit set ${decision.id} rejected --reason "..."`),
  );
}
