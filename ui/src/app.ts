/**
 * The shell: routing, the header, and the detail drawer.
 *
 * State here is deliberately small — which view, which task, which run, which
 * filter. Everything else comes from the snapshot, so a push re-renders from data
 * rather than patching what is on screen. That is why a status change cannot
 * leave the page half-updated.
 *
 * The route lives in the hash so a view is linkable and survives a reload, which
 * matters when the thing you are looking at is a specific run's stderr.
 */

import { classes, el } from './dom.js';
import { isLive, mark, plural } from './format.js';
import { Store, type ConnectionState } from './store.js';
import type { Snapshot } from './types.js';
import { renderDecisions } from './views/decisions.js';
import { fitTitles, renderGraph } from './views/graph.js';
import { renderMilestones } from './views/milestones.js';
import { renderOverview } from './views/overview.js';
import { renderRunDetail, renderRunList, RUN_FILTERS } from './views/runs.js';
import { FILTERS, renderTaskDetail, renderTaskList } from './views/tasks.js';

type ViewName = 'overview' | 'graph' | 'tasks' | 'milestones' | 'runs' | 'decisions';

const VIEWS: { name: ViewName; label: string }[] = [
  { name: 'overview', label: 'Overview' },
  { name: 'graph', label: 'Graph' },
  { name: 'tasks', label: 'Tasks' },
  { name: 'milestones', label: 'Milestones' },
  { name: 'runs', label: 'Runs' },
  { name: 'decisions', label: 'Decisions' },
];

interface Route {
  view: ViewName;
  task?: string;
  run?: string;
}

class App {
  private store = new Store();
  private route: Route = { view: 'overview' };
  private taskFilter = 'all';
  private runFilter = 'all';
  private query = '';

  private nav = el('nav', { class: 'tabs', role: 'tablist' });
  private counts = el('div', { class: 'header-counts' });
  private conn = el('div', { class: 'conn', title: 'connection to writ serve' });
  private body = el('main', { class: 'body' });
  private drawer = el('aside', { class: 'drawer', 'aria-live': 'polite' });

  async start(): Promise<void> {
    document.body.append(this.header(), this.body, this.drawer);
    this.store.onSnapshot(() => this.render());
    this.store.onConnection((state) => this.paintConnection(state));
    window.addEventListener('hashchange', () => {
      this.route = parseHash(location.hash);
      this.render();
    });
    document.addEventListener('keydown', (event) => this.onKey(event));
    this.route = parseHash(location.hash);
    this.paintConnection('connecting');
    await this.store.start();
  }

  private header(): HTMLElement {
    for (const view of VIEWS) {
      const button = el('button', { class: 'tab', type: 'button', role: 'tab' }, view.label);
      button.dataset.view = view.name;
      button.addEventListener('click', () => this.go({ view: view.name }));
      this.nav.append(button);
    }
    return el(
      'header',
      { class: 'top' },
      el('div', { class: 'brand' }, el('span', { class: 'wordmark' }, 'writ')),
      this.nav,
      this.counts,
      this.conn,
    );
  }

  private go(route: Route): void {
    this.route = route;
    location.hash = toHash(route);
    this.render();
  }

  private render(): void {
    const snapshot = this.store.current;
    for (const button of this.nav.querySelectorAll<HTMLButtonElement>('.tab')) {
      button.classList.toggle('active', button.dataset.view === this.route.view);
    }
    if (!snapshot) {
      this.body.replaceChildren(el('p', { class: 'empty' }, 'Loading…'));
      return;
    }
    this.paintCounts(snapshot);
    this.paintView(snapshot);
    this.paintDrawer();
  }

  private paintCounts(snapshot: Snapshot): void {
    const { overview } = snapshot;
    const parts: HTMLElement[] = [
      el(
        'span',
        { class: 'count' },
        el('b', {}, `${overview.completed}/${overview.tasks}`),
        el('span', { class: 'label' }, 'done'),
      ),
    ];
    if (overview.live) {
      parts.push(
        el(
          'span',
          { class: 'count live' },
          el('span', { class: 'spinner', 'aria-hidden': 'true' }),
          el('b', {}, String(overview.live)),
          el('span', { class: 'label' }, 'running'),
        ),
      );
    }
    const waiting = overview.counts['awaiting-review'] ?? 0;
    if (waiting) {
      parts.push(
        el('span', { class: 'count warn' }, el('b', {}, String(waiting)), el('span', { class: 'label' }, 'to review')),
      );
    }
    if (overview.proposed_decisions) {
      const button = el(
        'button',
        { class: 'count warn as-button', type: 'button' },
        el('b', {}, String(overview.proposed_decisions)),
        el('span', { class: 'label' }, 'decisions'),
      );
      button.addEventListener('click', () => this.go({ view: 'decisions' }));
      parts.push(button);
    }
    const failed = overview.counts.failed ?? 0;
    if (failed) {
      parts.push(
        el('span', { class: 'count bad' }, el('b', {}, String(failed)), el('span', { class: 'label' }, 'failed')),
      );
    }
    this.counts.replaceChildren(...parts);
  }

  private paintView(snapshot: Snapshot): void {
    const handlers = {
      onTask: (id: string) => this.go({ view: this.route.view, task: id }),
      onRun: (id: string) => this.go({ view: this.route.view, run: id }),
      onGoto: (view: string) => this.go({ view: view as ViewName }),
      onSelect: (id: string) => this.go({ view: this.route.view, task: id }),
    };

    switch (this.route.view) {
      case 'overview': {
        // Not `.grid`: the overview lays out its own regions, and an auto-fit
        // grid here would treat those regions as cards and column them.
        const holder = el('div', { class: 'overview' });
        renderOverview(holder, snapshot, handlers);
        this.body.replaceChildren(holder);
        break;
      }
      case 'graph': {
        const holder = el('div', { class: 'graph-holder' });
        renderGraph(holder, snapshot.graph, this.route.task ?? null, {
          onSelect: handlers.onSelect,
        });
        this.body.replaceChildren(
          this.toolbar(
            el('span', { class: 'muted small' },
              `${plural(snapshot.graph.nodes.length, 'task')} · ${snapshot.graph.levels} levels deep · a column can run at once`),
          ),
          holder,
        );
        fitTitles(holder);
        break;
      }
      case 'tasks': {
        const list = el('div', { class: 'list-holder' });
        renderTaskList(list, snapshot.tasks, {
          filter: this.taskFilter,
          query: this.query,
          selected: this.route.task ?? null,
        }, { onSelect: handlers.onSelect, onRun: handlers.onRun });
        this.body.replaceChildren(
          this.toolbar(
            this.filterBar(Object.keys(FILTERS), this.taskFilter, (name) => {
              this.taskFilter = name;
              this.render();
            }),
            this.search(),
          ),
          list,
        );
        break;
      }
      case 'milestones': {
        const holder = el('div', { class: 'grid one' });
        renderMilestones(holder, snapshot.milestones, snapshot.tasks, handlers);
        this.body.replaceChildren(holder);
        break;
      }
      case 'runs': {
        const list = el('div', { class: 'list-holder' });
        renderRunList(list, snapshot.runs, {
          filter: this.runFilter,
          selected: this.route.run ?? null,
        }, { onSelect: handlers.onRun, onTask: handlers.onTask });
        this.body.replaceChildren(
          this.toolbar(
            this.filterBar(Object.keys(RUN_FILTERS), this.runFilter, (name) => {
              this.runFilter = name;
              this.render();
            }),
            el('span', { class: 'muted small' }, `${plural(snapshot.runs.length, 'run')}, newest first`),
          ),
          list,
        );
        break;
      }
      case 'decisions': {
        const holder = el('div', { class: 'grid one' });
        renderDecisions(holder, snapshot.decisions);
        this.body.replaceChildren(holder);
        break;
      }
    }
  }

  private toolbar(...children: (Node | null)[]): HTMLElement {
    return el('div', { class: 'toolbar' }, ...children.filter(Boolean) as Node[]);
  }

  private filterBar(names: string[], active: string, pick: (name: string) => void): HTMLElement {
    const bar = el('div', { class: 'filters', role: 'group' });
    for (const name of names) {
      const button = el('button', { class: classes('filter', name === active && 'active'), type: 'button' }, name);
      button.addEventListener('click', () => pick(name));
      bar.append(button);
    }
    return bar;
  }

  private search(): HTMLElement {
    const input = el('input', {
      class: 'search',
      type: 'search',
      placeholder: 'filter by id or title',
      value: this.query,
      'aria-label': 'filter tasks',
    });
    input.addEventListener('input', () => {
      this.query = input.value;
      this.render();
      // Re-rendering replaces the input, so put the cursor back where it was.
      const fresh = this.body.querySelector<HTMLInputElement>('.search');
      fresh?.focus();
      fresh?.setSelectionRange(fresh.value.length, fresh.value.length);
    });
    return input;
  }

  /**
   * The drawer shows one task or one run, fetched on demand.
   *
   * It is re-fetched on every snapshot while open, so a task detail stays current
   * during a run — its criteria fill in as the agent reports them.
   */
  private paintDrawer(): void {
    const { task, run } = this.route;
    if (!task && !run) {
      this.drawer.classList.remove('open');
      this.drawer.replaceChildren();
      return;
    }
    this.drawer.classList.add('open');
    if (!this.drawer.querySelector('.detail-head')) {
      this.drawer.replaceChildren(el('p', { class: 'muted' }, 'Loading…'));
    }
    const close = el('button', { class: 'close', type: 'button', 'aria-label': 'close' }, '×');
    close.addEventListener('click', () => this.go({ view: this.route.view }));

    const handlers = {
      onSelect: (id: string) => this.go({ view: this.route.view, task: id }),
      onRun: (id: string) => this.go({ view: this.route.view, run: id }),
      onTask: (id: string) => this.go({ view: this.route.view, task: id }),
    };

    if (run) {
      void this.store
        .run(run)
        .then((detail) => {
          if (this.route.run !== run) return; // the reader moved on while fetching
          const holder = el('div', { class: 'detail' });
          renderRunDetail(holder, detail, handlers);
          this.drawer.replaceChildren(close, holder);
        })
        .catch((error) => this.drawerError(close, error));
      return;
    }
    void this.store
      .task(task as string)
      .then((detail) => {
        if (this.route.task !== task) return;
        const holder = el('div', { class: 'detail' });
        renderTaskDetail(holder, detail, handlers);
        this.drawer.replaceChildren(close, holder);
      })
      .catch((error) => this.drawerError(close, error));
  }

  private drawerError(close: HTMLElement, error: unknown): void {
    this.drawer.replaceChildren(
      close,
      el('p', { class: 'error' }, `Could not load: ${String(error)}`),
    );
  }

  private paintConnection(state: ConnectionState): void {
    this.conn.className = classes('conn', state);
    this.conn.replaceChildren(
      el('span', { class: 'dot', 'aria-hidden': 'true' }),
      // A paused tab is not a broken one: say why it stopped following.
      el('span', {}, state === 'paused' ? 'paused (tab hidden)' : state),
    );
  }

  private onKey(event: KeyboardEvent): void {
    if (event.target instanceof HTMLInputElement) return;
    if (event.key === 'Escape' && (this.route.task || this.route.run)) {
      this.go({ view: this.route.view });
      return;
    }
    // Number keys jump between views: quick to reach while watching a run.
    const index = Number.parseInt(event.key, 10);
    if (index >= 1 && index <= VIEWS.length) {
      this.go({ view: VIEWS[index - 1].name });
    }
  }
}

function parseHash(hash: string): Route {
  const clean = hash.replace(/^#\/?/, '');
  if (!clean) return { view: 'overview' };
  const [view, kind, id] = clean.split('/');
  const known = VIEWS.some((v) => v.name === view) ? (view as ViewName) : 'overview';
  if (kind === 'task' && id) return { view: known, task: decodeURIComponent(id) };
  if (kind === 'run' && id) return { view: known, run: decodeURIComponent(id) };
  return { view: known };
}

function toHash(route: Route): string {
  if (route.task) return `#/${route.view}/task/${encodeURIComponent(route.task)}`;
  if (route.run) return `#/${route.view}/run/${encodeURIComponent(route.run)}`;
  return `#/${route.view}`;
}

void new App().start();

// Referenced so the marks and live helper are part of the bundle's public shape
// for tests that assert the vocabulary matches the terminal's.
export { isLive, mark };
