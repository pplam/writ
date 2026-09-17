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

function problemSection(run: Run): HTMLElement | null {
  if (!run.verdict_error && !run.no_verdict && !run.note && !run.verdict_downgraded)
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
          `${run.no_verdict} — so its acceptance criteria were left untouched, and ` +
            'the task was returned to the queue rather than judged',
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
    run.verdict_error ? el('p', { class: 'error' }, run.verdict_error) : null,
    // Not an error: the verdict was applied, with the claim lowered to match the
    // criteria under it. Shown here because a task that reads "failed" against a
    // summary claiming success is otherwise unexplained.
    run.verdict_downgraded
      ? el('p', { class: 'muted' }, run.verdict_downgraded)
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
