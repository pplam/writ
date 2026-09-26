/**
 * The task list and the task detail panel.
 *
 * The list is filterable because the useful questions are subsets: what is
 * blocked, what is waiting on me, what failed. The detail panel exists because
 * `writ show` is the command people run most and it answers "what is this task
 * supposed to prove, and what has it proved so far".
 */

import { activate, classes, code, el, liveClock, replace } from '../dom.js';
import { ago, isLive, mark, percent, ratio, roleLabel, stamp, statusWeight } from '../format.js';
import type { Acceptance, Evidence, RunRow, Task, TaskRow } from '../types.js';

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

  if (!rows.length) {
    replace(host, el('p', { class: 'empty' }, tasks.length ? 'No tasks match.' : 'No tasks yet.'));
    return;
  }
  replace(host,
    el(
      'table',
      { class: 'data-table task-table' },
      el(
        'thead',
        {},
        el(
          'tr',
          {},
          el('th', { class: 'col-mark' }),
          el('th', { class: 'col-id' }, 'Task'),
          el('th', {}, 'Title'),
          el('th', { class: 'col-status' }, 'Status'),
          el('th', { class: 'col-criteria' }, 'Criteria'),
          el('th', { class: 'col-num' }, 'Runs'),
          el('th', { class: 'col-num' }, 'Agent time'),
          el('th', { class: 'col-when' }, 'Last run'),
        ),
      ),
      el('tbody', {}, ...rows.map((t) => taskRow(t, t.id === options.selected, handlers))),
    ),
  );
}

function taskRow(task: TaskRow, isSelected: boolean, handlers: TaskHandlers): HTMLElement {
  const live = isLive(task.status);
  const row = el(
    'tr',
    { class: classes('task-row', task.status, live && 'live', isSelected && 'selected') },
    el('td', { class: 'col-mark' }, live
      ? el('span', { class: 'spinner', 'aria-hidden': 'true' })
      : el('span', { class: classes('mark', task.status) }, mark(task.status))),
    el('td', { class: 'col-id' }, code(task.id)),
    el(
      'td',
      { class: 'col-title' },
      el(
        'div',
        { class: 'cell-title' },
        el('span', { class: 'clip' }, task.title),
        task.kind === 'gate' ? el('span', { class: 'tag' }, 'gate') : null,
        task.rework_attempts
          ? el('span', { class: 'tag warn', title: 'times a reviewer sent it back' },
              `rework ${task.rework_attempts}`)
          : null,
        task.blocked_by.length
          ? el('span', { class: 'tag', title: `waiting on ${task.blocked_by.join(', ')}` },
              `waits on ${task.blocked_by.length}`)
          : null,
      ),
    ),
    el('td', { class: 'col-status' }, el('span', { class: classes('pill', task.status) }, task.status)),
    el('td', { class: 'col-criteria' }, criteriaMeter(task.passed, task.total)),
    el('td', { class: 'col-num tnum' }, task.runs ? String(task.runs) : '—'),
    el('td', { class: 'col-num tnum' }, liveClock(task.agent_seconds, task.live_since)),
    el(
      'td',
      { class: 'col-when muted', title: task.finished_at ?? task.started_at ?? '' },
      task.live_since ? 'now' : ago(task.finished_at ?? task.started_at) || '—',
    ),
  );
  // What this row opens. The drawer hands focus back here when dismissed, and
  // it cannot hold the element itself: opening re-renders the list, so the
  // node that was clicked is gone by the time the drawer is on screen.
  activate(row, `task:${task.id}`, () => handlers.onSelect(task.id));
  return row;
}

/** A small bar and the count beside it, so a column of them reads at a glance. */
function criteriaMeter(passed: number, total: number): HTMLElement {
  if (!total) return el('span', { class: 'muted' }, '—');
  return el(
    'div',
    { class: 'mini-meter' },
    el('div', { class: 'meter thin' },
      el('div', { class: 'meter-fill', style: `width:${percent(passed, total)}%` })),
    el('span', { class: 'tnum' }, ratio(passed, total)),
  );
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
      el(
        'div',
        { class: 'detail-title' },
        el('span', { class: classes('mark', task.status) }, mark(task.status)),
        code(task.id),
        el('span', { class: classes('pill', task.status) }, task.status),
      ),
      el('h2', {}, task.title),
    ),
    metaRow(task),
    // Above the criteria, because for a blocked task this is the answer to the only
    // question being asked. It used to be readable only as one evidence line below
    // four other sections, while Dependencies showed everything satisfied — so the
    // page looked like writ had stopped for no reason it could name.
    blockedSection(task),
    // A held gate reads as `blocked` and has no unmet dependency, so without this
    // the page shows a stopped project with nothing saying why. Above the criteria
    // for the same reason `blockedSection` is: it is the only question being asked.
    heldSection(task),
    acceptanceSection(task),
    gateSection(task),
    task.notes ? section('Notes', null, el('p', { class: 'prose' }, task.notes)) : null,
    dependencySection(task),
    guardrailSection(task),
    runSection(task, handlers),
    evidenceSection(task),
  );
}

/**
 * Why a gate is parked, and what would move it.
 *
 * Distinct from `blockedSection`, which reports an agent's own account of what
 * stopped it. This is writ declining to spend more agents: the repair loop is out
 * of rounds, or the gate asked a question only a person can answer. The remedy is
 * never "wait" — it is reading the record named here.
 */
function heldSection(task: Task): HTMLElement | null {
  const held = task.held;
  if (!held) return null;
  const advice: Record<string, string> = {
    'awaiting-repair': 'A repair is being planned. The gate will be asked again on the repaired code.',
    'needs-decision': 'The gate asked something only a person can settle. Rule on it in the decision log.',
    'repair-exhausted': 'The repair loop ran out of rounds. What is wrong is the plan, not the wording of a patch.',
    'repair-refused': 'Writ turned down every patch the planner proposed. Read the refusals before re-planning.',
  };
  return section(
    'Held',
    held.at ? ago(held.at) : null,
    el('div', { class: 'meta-row' }, el('span', { class: 'pill needs-repair' }, held.reason)),
    el('p', { class: 'prose' }, advice[held.reason] ?? 'This gate will not re-run on its own.'),
    held.detail ? el('p', { class: 'prose' }, held.detail) : null,
    held.questions?.length
      ? el('ul', { class: 'held-questions' }, ...held.questions.map((q) => el('li', {}, q)))
      : null,
    held.request ? el('pre', { class: 'command' }, `writ show ${held.request}`) : null,
  );
}

/**
 * What a gate has decided, oldest first.
 *
 * A gate is asked again after every repair, so its history is the record of the
 * plan converging — or not. Read in order it says whether each round closed
 * something or found the same thing again, which one attempt cannot say.
 */
function gateSection(task: Task): HTMLElement | null {
  if (task.kind !== 'gate' || !task.gate_attempts.length) return null;
  return section(
    'Gate reviews',
    String(task.gate_attempts.length),
    el('ol', { class: 'gate-attempts' }, ...task.gate_attempts.map((attempt) => el(
      'li',
      { class: classes('gate-attempt', attempt.decision) },
      el(
        'div',
        { class: 'meta-row' },
        el('span', { class: classes('pill', attempt.decision) }, attempt.decision),
        el('span', { class: 'muted small' }, `revision ${attempt.revision}`),
        attempt.actor ? el('span', { class: 'muted small' }, attempt.actor) : null,
        attempt.at ? el('span', { class: 'muted small', title: attempt.at }, ago(attempt.at)) : null,
      ),
      attempt.summary ? el('p', { class: 'prose' }, attempt.summary) : null,
      attempt.findings.length
        ? el('div', { class: 'meta-row' }, el('span', {}, 'raised '), ...attempt.findings.map(code))
        : null,
    ))),
  );
}

/**
 * A titled section. The optional note is a count or ratio: it belongs to the
 * title but is not part of its name, so it is a separate quieter element rather
 * than punctuation inside the string.
 */
function section(
  title: string,
  note: string | null,
  ...body: (Node | null | false)[]
): HTMLElement {
  return el(
    'section',
    { class: 'detail-section' },
    el('h3', {}, title, note ? el('span', { class: 'h3-note' }, note) : null),
    ...body,
  );
}

/**
 * Where this task came from. Rendered as discrete labelled items: these used to
 * be bare spans separated only by a flex gap, so a task's kind ran straight
 * into a design section and then into a file path with nothing to show where
 * one ended and the next began.
 */
function metaRow(task: Task): HTMLElement {
  const item = (label: string, value: Node | string, mono = false) =>
    el(
      'div',
      { class: 'meta-item' },
      el('span', { class: 'meta-label' }, label),
      typeof value === 'string'
        ? el('span', { class: classes('meta-value', mono && 'mono') }, value)
        : el('span', { class: 'meta-value' }, value),
    );

  const bits: HTMLElement[] = [];
  bits.push(item('kind', task.kind));
  if (task.agent_seconds || task.live_since) {
    bits.push(item('agent time', liveClock(task.agent_seconds, task.live_since)));
  }
  if (task.started_at) bits.push(item('started', stamp(task.started_at)));
  if (task.design_section) bits.push(item('section', task.design_section));
  if (task.design_doc) bits.push(item('design', basename(task.design_doc), true));
  bits.push(item('updated', ago(task.updated_at)));
  const row = el('div', { class: 'meta-row' }, ...bits);
  if (task.design_doc) row.setAttribute('title', task.design_doc);
  return row;
}

/** The path is usually long and usually irrelevant past the filename. */
function basename(path: string): string {
  const parts = path.split('/');
  return parts[parts.length - 1] || path;
}

function acceptanceSection(task: Task): HTMLElement {
  if (!task.acceptances.length) {
    return section('Acceptance', null, el('p', { class: 'blank' }, 'No criteria recorded.'));
  }
  return section(
    'Acceptance',
    `${ratio(task.passed, task.total)} passed`,
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
      // ASCII, matching the terminal's marks rather than inventing typographic
      // ones for this one spot.
      el(
        'span',
        { class: classes('mark', item.status) },
        item.status === 'passed' ? '+' : item.status === 'failed' ? 'x' : '·',
      ),
      el('span', { class: 'criterion-text' }, item.text),
      el('span', { class: classes('pill', item.status) }, item.status),
    ),
    // Evidence is the point of a criterion: a claim with nothing behind it is
    // exactly what the reviewer is there to catch, so it is shown, not hidden.
    item.evidence ? el('p', { class: 'evidence' }, item.evidence) : null,
    item.by
      ? el(
          'p',
          { class: 'criterion-by muted small', title: item.at },
          el('span', { class: 'actor' }, item.by),
          el('span', {}, ago(item.at)),
        )
      : null,
  );
}

function guardrailSection(task: Task): HTMLElement | null {
  if (!task.allowed.length && !task.forbidden.length) return null;
  const rail = (kind: string, heading: string, paths: string[]) =>
    el(
      'div',
      { class: classes('rail', kind) },
      el('h4', {}, heading),
      el('ul', {}, ...paths.map((p) => el('li', { class: 'mono' }, p))),
    );
  return section(
    'Guardrails',
    null,
    el(
      'div',
      { class: 'rails' },
      task.allowed.length ? rail('allowed', 'may touch', task.allowed) : null,
      task.forbidden.length ? rail('forbidden', 'must not touch', task.forbidden) : null,
    ),
  );
}

function dependencySection(task: Task): HTMLElement | null {
  if (!task.depends_on.length && !task.blocks.length && !task.unknown_deps.length) return null;
  const group = (heading: string, ids: string[], kind?: string) =>
    el(
      'div',
      { class: 'dep-group' },
      el('h4', { class: classes(kind) }, heading),
      el('div', { class: 'dep-line' }, ...ids.map((id) => code(id))),
    );
  return section(
    'Dependencies',
    null,
    el(
      'div',
      { class: 'deps' },
      task.depends_on.length ? group('runs after', task.depends_on) : null,
      task.blocked_by.length ? group('still waiting on', task.blocked_by, 'warn') : null,
      task.blocks.length ? group('blocks', task.blocks) : null,
      // A dangling id means this task can never become ready. Say so here rather
      // than letting it sit in the list looking merely slow.
      task.unknown_deps.length
        ? group('missing, so this can never become ready', task.unknown_deps, 'error')
        : null,
    ),
  );
}

/**
 * Every run on this task, oldest first, as a timeline.
 *
 * Oldest first because the order is the story: a dispatch, its review, a
 * rejection, the rework, the review that accepted it. Each row says which of
 * those it was, so the reviews are findable from the task they judged.
 */
function runSection(task: Task, handlers: TaskHandlers): HTMLElement {
  if (!task.run_list.length) {
    return section('Runs', null, el('p', { class: 'blank' }, 'Never dispatched.'));
  }
  const reviews = task.run_list.filter((run) => run.role === 'reviewer').length;
  return section(
    'Runs',
    `${task.run_list.length}${reviews ? ` · ${reviews} review${reviews === 1 ? '' : 's'}` : ''}`,
    el('ol', { class: 'timeline' }, ...task.run_list.map((run) => timelineRow(run, handlers))),
  );
}

function timelineRow(run: RunRow, handlers: TaskHandlers): HTMLElement {
  const live = isLive(run.status);
  const outcome = run.decision || (run.status === 'completed' ? run.resulting_status : run.status);
  const row = el(
    'li',
    { class: classes('timeline-row', run.status, live && 'live') },
    el('span', { class: classes('role', run.role) }, roleLabel(run.role)),
    el(
      'div',
      { class: 'timeline-body' },
      el(
        'div',
        { class: 'timeline-line' },
        outcome ? el('span', { class: classes('pill', outcome) }, outcome) : null,
        el('span', { class: 'muted mono small clip' }, run.model || run.command),
        el('span', { class: 'grow' }),
        el('span', { class: 'tnum small' }, liveClock(live ? 0 : run.duration, live ? run.started_at : null)),
        el('span', { class: 'muted small tnum', title: run.started_at ?? '' }, stamp(run.started_at)),
      ),
      run.summary ? el('p', { class: 'timeline-summary' }, run.summary) : null,
    ),
  );
  activate(row, `run:${run.id}`, () => handlers.onRun(run.id));
  return row;
}

/**
 * What stopped a blocked task, when it said so.
 *
 * A task blocked by its own report has no unsatisfied dependency, so the
 * Dependencies section shows every one of them met and the status pill is the only
 * sign anything is wrong. Nothing auto-clears a block either — it waits for a
 * person — so a reason that cannot be found is a task that sits there
 * indefinitely with no visible next step.
 */
function blockedSection(task: Task): HTMLElement | null {
  if (!task.blocked_on) return null;
  return section(
    'Blocked on',
    null,
    el('p', { class: 'prose blocked-reason' }, task.blocked_on),
  );
}

function evidenceSection(task: Task): HTMLElement | null {
  if (!task.evidence.length) return null;
  const items = [...task.evidence].reverse();
  return section(
    'History',
    String(items.length),
    el('ol', { class: 'history' }, ...items.map((item, index) => historyEntry(task.id, item, items.length - index))),
  );
}

/** Entries the reader expanded, so a repaint mid-run does not fold them back up. */
const expandedHistory = new Set<string>();

/** Past this many characters an entry is clamped to a few lines until asked for. */
const HISTORY_CLAMP = 280;

/**
 * One line of a task's history as a feed entry: who, then what, then the words.
 *
 * It was a three-column row, and the actor — `reviewer(pi:ppio/zai-org/…)` in
 * mono — took a third of the drawer, leaving a narrow column of long prose and a
 * timestamp wrapped onto two lines beside it. The actor is now split into the
 * role it played and the model it ran on, both on a header line, and the text
 * gets the full width under it.
 */
function historyEntry(taskId: string, item: Evidence, number: number): HTMLElement {
  const { role, detail } = splitActor(item.actor);
  const { label, body } = splitLead(item.text);
  const key = `${taskId}|${item.at}|${number}`;
  const long = body.length > HISTORY_CLAMP;
  const text = el('p', { class: classes('history-text', long && !expandedHistory.has(key) && 'clamped') }, body);
  let toggle: HTMLElement | null = null;
  if (long) {
    const more = el('button', { class: 'link small', type: 'button' },
      expandedHistory.has(key) ? 'Show less' : 'Show more');
    more.addEventListener('click', (event) => {
      event.stopPropagation();
      const open = !expandedHistory.has(key);
      if (open) expandedHistory.add(key);
      else expandedHistory.delete(key);
      text.classList.toggle('clamped', !open);
      more.textContent = open ? 'Show less' : 'Show more';
    });
    toggle = more;
  }
  return el(
    'li',
    { class: 'history-entry' },
    el(
      'div',
      { class: 'history-head' },
      role in ROLE_ACTORS
        ? el('span', { class: classes('role', role) }, roleLabel(role))
        : el('span', { class: 'history-actor' }, role),
      detail ? el('span', { class: 'muted mono small clip', title: detail }, detail) : null,
      el('span', { class: 'grow' }),
      el('span', { class: 'history-when', title: stamp(item.at) }, ago(item.at)),
    ),
    label ? el('div', { class: 'history-label' }, label) : null,
    body ? text : null,
    toggle,
  );
}

const ROLE_ACTORS: Record<string, true> = { agent: true, reviewer: true, gate: true, repair: true };

/** `reviewer(pi:model)` → role `reviewer`, detail `pi:model`; `operator` stays whole. */
function splitActor(actor: string): { role: string; detail: string } {
  const match = /^([\w-]+)\((.*)\)$/.exec(actor);
  return match ? { role: match[1], detail: match[2] } : { role: actor || 'writ', detail: '' };
}

/**
 * The short lead an entry opens with — `notes`, `review accepted (5/5 criteria
 * passed)` — as a label of its own, so the kind of entry reads before its prose.
 * Only a short lead on the first line counts: a colon deep inside a sentence is
 * part of the sentence.
 */
function splitLead(text: string): { label: string; body: string } {
  const at = text.indexOf(': ');
  if (at > 0 && at <= 60 && !text.slice(0, at).includes('\n')) {
    return { label: text.slice(0, at), body: text.slice(at + 2).trim() };
  }
  return { label: '', body: text };
}
