/**
 * The run log and the run detail panel.
 *
 * This is the observability surface. A run holds the three things you need when
 * an agent did something unexpected: the exact prompt it was given, what it
 * printed, and the verdict it wrote. All three are on disk already — `writ logs`
 * shows one of them — and the reason to put them on a page together is that the
 * question is usually "what did it see, and what did it claim about it".
 */

import { classes, code, el, replace } from '../dom.js';
import { ago, clock, duration, isLive, mark } from '../format.js';
import type { LogTail, Run, RunRow } from '../types.js';

export interface RunHandlers {
  onSelect(id: string): void;
  onTask(id: string): void;
}

export const RUN_FILTERS: Record<string, (run: RunRow) => boolean> = {
  all: () => true,
  live: (r) => isLive(r.status),
  reviews: (r) => r.role === 'reviewer',
  failed: (r) => r.status === 'failed' || r.exit_code !== 0 && r.exit_code !== null,
  rejected: (r) => r.decision === 'reject',
};

export function renderRunList(
  host: HTMLElement,
  runs: RunRow[],
  options: { filter: string; selected: string | null },
  handlers: RunHandlers,
): void {
  const predicate = RUN_FILTERS[options.filter] ?? RUN_FILTERS.all;
  const rows = runs.filter(predicate);
  replace(host,
    rows.length
      ? el('ul', { class: 'run-list wide' }, ...rows.map((r) => runRow(r, r.id === options.selected, handlers)))
      : el('p', { class: 'empty' }, 'No runs match.'),
  );
}

function runRow(run: RunRow, isSelected: boolean, handlers: RunHandlers): HTMLElement {
  const row = el(
    'li',
    {
      class: classes('run-row', run.status, isLive(run.status) && 'live', isSelected && 'selected', 'clickable'),
      tabindex: 0,
      role: 'button',
      // See tasks.ts: the detail this row opens, so focus can come back to it.
      'data-opens': `run:${run.id}`,
    },
    el('span', { class: 'mark' }, mark(run.status)),
    el('span', { class: classes('verb', run.role) }, run.role === 'reviewer' ? 'review' : 'dispatch'),
    code(run.task),
    el('span', { class: 'muted mono small' }, run.model || run.command),
    el('span', { class: 'grow' }),
    run.decision ? el('span', { class: classes('pill', run.decision) }, run.decision) : null,
    run.exit_code !== null && run.exit_code !== 0
      ? el('span', { class: 'pill failed' }, `exit ${run.exit_code}`)
      : null,
    el('span', { class: 'muted mono small' }, duration(run.duration)),
    el('span', { class: 'muted small', title: run.started_at ?? run.created_at }, ago(run.started_at ?? run.created_at)),
  );
  const select = () => handlers.onSelect(run.id);
  row.addEventListener('click', select);
  row.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      select();
    }
  });
  return row;
}

export function renderRunDetail(host: HTMLElement, run: Run, handlers: RunHandlers): void {
  const taskButton = el('button', { class: 'link', type: 'button' }, run.task);
  taskButton.addEventListener('click', () => handlers.onTask(run.task));

  replace(host,
    el(
      'header',
      { class: 'detail-head' },
      el('span', { class: classes('mark', run.status) }, mark(run.status)),
      code(run.id),
      el('span', { class: classes('pill', run.status) }, run.status),
      run.exit_code !== null ? el('span', { class: 'muted mono' }, `exit ${run.exit_code}`) : null,
    ),
    el(
      'div',
      { class: 'meta-row' },
      el('span', {}, run.role === 'reviewer' ? 'review of ' : 'dispatch of ', taskButton),
      run.model ? el('span', { class: 'mono small' }, run.model) : null,
      el('span', { class: 'mono small' }, run.command),
      run.duration !== null ? el('span', {}, duration(run.duration)) : null,
      run.started_at ? el('span', { title: run.started_at }, `started ${clock(run.started_at)}`) : null,
    ),
    verdictSection(run),
    problemSection(run),
    tabs(run),
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
