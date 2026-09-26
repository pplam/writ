/**
 * The run log and the run detail panel.
 *
 * This is the observability surface. A run holds the three things you need when
 * an agent did something unexpected: the exact prompt it was given, what it
 * printed, and the verdict it wrote. All three are on disk already — `writ logs`
 * shows one of them — and the reason to put them on a page together is that the
 * question is usually "what did it see, and what did it claim about it".
 */

import { activate, classes, code, el, liveClock, replace } from '../dom.js';
import { ago, isLive, mark, roleLabel, stamp } from '../format.js';
import type { LogTail, Run, RunRow } from '../types.js';

export interface RunHandlers {
  onSelect(id: string): void;
  onTask(id: string): void;
}

/**
 * The subsets worth finding. By role first, because "show me the reviews" was
 * the question the old list could not answer without reading every row; then by
 * outcome, because a failure or a rejection is the other thing people come
 * here for.
 */
export const RUN_FILTERS: Record<string, (run: RunRow) => boolean> = {
  all: () => true,
  live: (r) => isLive(r.status),
  dispatches: (r) => r.role === 'agent',
  reviews: (r) => r.role === 'reviewer',
  gates: (r) => r.role === 'gate',
  repairs: (r) => r.role === 'repair',
  failed: (r) => r.status === 'failed' || r.exit_code !== 0 && r.exit_code !== null,
  rejected: (r) => r.decision === 'reject',
};

export function renderRunList(
  host: HTMLElement,
  runs: RunRow[],
  options: { filter: string; query: string; selected: string | null },
  handlers: RunHandlers,
): void {
  const predicate = RUN_FILTERS[options.filter] ?? RUN_FILTERS.all;
  const query = options.query.trim().toLowerCase();
  const rows = runs
    .filter(predicate)
    .filter((r) => !query || r.id.toLowerCase().includes(query) || r.task.toLowerCase().includes(query));
  if (!rows.length) {
    replace(host, el('p', { class: 'empty' }, runs.length ? 'No runs match.' : 'Nothing has been dispatched yet.'));
    return;
  }
  replace(host,
    el(
      'table',
      { class: 'data-table run-table' },
      el(
        'thead',
        {},
        el(
          'tr',
          {},
          el('th', { class: 'col-mark' }),
          el('th', { class: 'col-role' }, 'Role'),
          el('th', { class: 'col-id' }, 'Task'),
          el('th', {}, 'Outcome'),
          el('th', { class: 'col-model' }, 'Model'),
          el('th', { class: 'col-num' }, 'Duration'),
          el('th', { class: 'col-when' }, 'Started'),
        ),
      ),
      el('tbody', {}, ...rows.map((r) => runRow(r, r.id === options.selected, handlers))),
    ),
  );
}

/** What a run came to, in one pill: the verdict if it gave one, else how it ended. */
function outcomePill(run: RunRow): HTMLElement | null {
  if (isLive(run.status)) return el('span', { class: classes('pill', run.status) }, run.status);
  if (run.decision) return el('span', { class: classes('pill', run.decision) }, run.decision);
  if (run.exit_code !== null && run.exit_code !== 0) {
    return el('span', { class: 'pill failed' }, `exit ${run.exit_code}`);
  }
  if (run.no_verdict) return el('span', { class: 'pill failed' }, 'no verdict');
  if (run.status !== 'completed') return el('span', { class: classes('pill', run.status) }, run.status);
  return run.resulting_status
    ? el('span', { class: classes('pill', run.resulting_status) }, run.resulting_status)
    : el('span', { class: 'pill completed' }, 'completed');
}

function runRow(run: RunRow, isSelected: boolean, handlers: RunHandlers): HTMLElement {
  const live = isLive(run.status);
  const row = el(
    'tr',
    { class: classes('run-row', run.status, live && 'live', isSelected && 'selected') },
    el('td', { class: 'col-mark' }, live
      ? el('span', { class: 'spinner', 'aria-hidden': 'true' })
      : el('span', { class: classes('mark', run.status) }, mark(run.status))),
    el('td', { class: 'col-role' }, el('span', { class: classes('role', run.role) }, roleLabel(run.role))),
    el('td', { class: 'col-id' }, code(run.task)),
    el(
      'td',
      { class: 'col-title' },
      el(
        'div',
        { class: 'cell-title' },
        outcomePill(run),
        run.summary ? el('span', { class: 'clip muted' }, run.summary) : null,
      ),
    ),
    el('td', { class: 'col-model muted mono small' }, el('span', { class: 'clip' }, run.model || '—')),
    el('td', { class: 'col-num tnum' }, liveClock(live ? 0 : run.duration, live ? run.started_at : null)),
    el(
      'td',
      { class: 'col-when muted', title: run.started_at ?? run.created_at },
      ago(run.started_at ?? run.created_at),
    ),
  );
  // See tasks.ts: the detail this row opens, so focus can come back to it.
  activate(row, `run:${run.id}`, () => handlers.onSelect(run.id));
  return row;
}

export function renderRunDetail(host: HTMLElement, run: Run, handlers: RunHandlers): void {
  const taskButton = el('button', { class: 'link', type: 'button' }, run.task);
  taskButton.addEventListener('click', () => handlers.onTask(run.task));

  replace(host,
    el(
      'header',
      { class: 'detail-head' },
      el(
        'div',
        { class: 'detail-title' },
        el('span', { class: classes('mark', run.status) }, mark(run.status)),
        el('span', { class: classes('role', run.role) }, roleLabel(run.role)),
        el('span', { class: classes('pill', run.status) }, run.status),
        run.exit_code !== null ? el('span', { class: 'muted mono small' }, `exit ${run.exit_code}`) : null,
      ),
      el('h2', {}, `${roleLabel(run.role)} of `, taskButton),
      el('div', { class: 'muted mono small' }, run.id),
    ),
    runMeta(run),
    verdictSection(run),
    problemSection(run),
    tabs(run),
  );
}

/** Discrete labelled facts, the same shape as the task drawer's. */
function runMeta(run: Run): HTMLElement {
  const live = isLive(run.status);
  const item = (label: string, value: Node | string, mono = false) =>
    el(
      'div',
      { class: 'meta-item' },
      el('span', { class: 'meta-label' }, label),
      el('span', { class: classes('meta-value', mono && 'mono') }, value),
    );
  return el(
    'div',
    { class: 'meta-row' },
    item('duration', liveClock(live ? 0 : run.duration, live ? run.started_at : null)),
    run.started_at ? item('started', stamp(run.started_at)) : null,
    run.finished_at ? item('finished', stamp(run.finished_at)) : null,
    run.model ? item('model', run.model, true) : null,
    item('command', run.command, true),
  );
}

function verdictSection(run: Run): HTMLElement | null {
  if (!run.decision && !run.summary && !run.unmet.length) return null;
  return el(
    'section',
    { class: 'detail-section' },
    el('h3', {}, 'Verdict'),
    el(
      'div',
      { class: 'verdict-head' },
      run.decision ? el('span', { class: classes('pill', run.decision) }, run.decision) : null,
      run.resulting_status
        ? el('span', { class: 'muted' }, `task became ${run.resulting_status}`)
        : null,
      run.unmet.length ? el('span', { class: 'warn' }, `unmet: ${run.unmet.join(', ')}`) : null,
    ),
    run.summary ? el('p', { class: 'prose' }, run.summary) : null,
    run.decisions.length
      ? el(
          'div',
          { class: 'proposed' },
          el('h4', {}, 'decisions proposed'),
          el('ul', {}, ...run.decisions.map((title) => el('li', {}, title))),
        )
      : null,
  );
}

/**
 * What became of the task, for a run that judged nothing.
 *
 * Read off the run rather than asserted, because it is not the same for every
 * such run and the wrong version is worse than none: a reviewer that fails to
 * report leaves a finished implementation standing at `awaiting-review`, and
 * telling that reader their task "was returned to the queue" sends them to
 * re-dispatch work that is already done. An implementer that fails to report is
 * the case that really does go back.
 *
 * Falls back to naming the gap rather than filling it. A run recorded before the
 * status was kept has nothing to report here, and saying so is honest where
 * picking the likelier answer would not be.
 */
function noVerdictOutcome(run: Run): string {
  if (!run.resulting_status) return 'the task was left unjudged';
  if (run.resulting_status === 'awaiting-review') {
    return 'the task is still awaiting review, with the implementation intact';
  }
  if (run.resulting_status === 'planned') {
    return 'the task was returned to the queue rather than judged';
  }
  return `the task became ${run.resulting_status} rather than judged`;
}

function problemSection(run: Run): HTMLElement | null {
  if (
    !run.verdict_error &&
    !run.no_verdict &&
    !run.note &&
    !run.verdict_downgraded &&
    !run.verdict_misplaced
  )
    return null;
  return el(
    'section',
    { class: 'detail-section problem' },
    el('h3', {}, 'Problem'),
    // The most confusing failure writ has: the agent exits 0, the run reads
    // "completed", and the task did not move. Nothing on the page explains that
    // unless this does.
    run.no_verdict
      ? el(
          'p',
          { class: 'error' },
          // A second sentence rather than another clause. The reason already carries
          // its own "— and ..." diagnosis for the silent and unparsed-call cases,
          // and hanging the consequence off that too produced a sentence with three
          // clauses and two dashes that had to be read twice.
          `${run.no_verdict}. Its acceptance criteria were left untouched, and ` +
            `${noVerdictOutcome(run)}.`,
        )
      : null,
    // A silent run is not an agent that skipped its report, and telling the two
    // apart is the difference between re-reading a transcript that says nothing
    // and going to look at the agent's own configuration.
    run.no_verdict && run.no_output
      ? el(
          'p',
          { class: 'muted' },
          'The transcript is empty, so start with the invocation rather than the ' +
            'prompt: check the model id, that the agent is authenticated for ' +
            'that provider, and that its quota is not exhausted. Running ',
          el('code', {}, run.command),
          ' by hand usually says which.',
        )
      : null,
    // The transcript looks like work, so the reader's instinct is to read it for a
    // reason the agent declined to report. There isn't one: it never got that far.
    run.no_verdict && run.unparsed_tool_call
      ? el(
          'p',
          { class: 'muted' },
          'The transcript ends with tool-call markup as text, so the model wrote a ' +
            'call the agent could not parse and the turn ended there. Nothing in ' +
            'the prompt causes that — try the task on a model whose tool calling ' +
            'is more reliable.',
        )
      : null,
    run.verdict_error ? el('p', { class: 'error' }, run.verdict_error) : null,
    // Not an error: the verdict was applied, with the claim lowered to match the
    // criteria under it. Shown here because a task that reads "failed" against a
    // summary claiming success is otherwise unexplained.
    run.verdict_downgraded
      ? el('p', { class: 'muted' }, run.verdict_downgraded)
      : null,
    // Also not an error: the report was found and used, just not where writ put
    // the agent's instructions. Shown so a recurring habit is visible.
    run.verdict_misplaced
      ? el(
          'p',
          { class: 'muted' },
          'The verdict was written to ',
          el('code', {}, run.verdict_misplaced),
          ' rather than the path the agent was given. Writ used it from there.',
        )
      : null,
    run.note ? el('p', { class: 'muted' }, run.note) : null,
  );
}

/**
 * Prompt, stdout, stderr and the raw verdict as tabs.
 *
 * Tabs rather than four stacked panes: each is long, and the reader wants one at
 * a time. The prompt is first because it is the one artifact nothing else
 * surfaces, and the usual question about a surprising run is what it was told.
 */
function tabs(run: Run): HTMLElement {
  const panes: { name: string; body: HTMLElement; note?: string }[] = [
    { name: 'prompt', body: pre(run.prompt), note: `${run.prompt.length} chars` },
    { name: 'stdout', body: logPane(run.stdout), note: bytes(run.stdout) },
    { name: 'stderr', body: logPane(run.stderr), note: bytes(run.stderr) },
  ];
  if (run.verdict_raw) {
    panes.push({ name: 'verdict.json', body: pre(run.verdict_raw) });
  }

  const strip = el('div', { class: 'tab-strip', role: 'tablist' });
  const holder = el('div', { class: 'tab-body' });

  panes.forEach((pane, index) => {
    const button = el(
      'button',
      { class: classes('tab', index === 0 && 'active'), type: 'button', role: 'tab' },
      pane.name,
      pane.note ? el('span', { class: 'tab-note' }, pane.note) : null,
    );
    button.addEventListener('click', () => {
      for (const other of strip.querySelectorAll('.tab')) other.classList.remove('active');
      button.classList.add('active');
      holder.replaceChildren(pane.body);
      // Logs are read from the end while a run is in flight.
      if (pane.name !== 'prompt') pane.body.scrollTop = pane.body.scrollHeight;
    });
    strip.append(button);
  });

  holder.append(panes[0].body);
  return el(
    'section',
    { class: 'detail-section' },
    el('h3', {}, 'What the agent saw and said'),
    strip,
    holder,
  );
}

function logPane(tail: LogTail): HTMLElement {
  if (!tail.text) return el('pre', { class: 'log empty-log' }, '(nothing)');
  const body = pre(tail.text);
  if (tail.truncated) {
    body.prepend(
      el('div', { class: 'truncated' }, `showing the last part of ${tail.bytes} bytes`),
    );
  }
  return body;
}

function pre(text: string): HTMLElement {
  return el('pre', { class: 'log' }, text);
}

function bytes(tail: LogTail): string {
  if (!tail.bytes) return 'empty';
  if (tail.bytes < 1024) return `${tail.bytes} B`;
  return `${Math.round(tail.bytes / 1024)} KB`;
}
