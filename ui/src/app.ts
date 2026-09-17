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

/**
 * Whether a click that landed outside the drawer should dismiss it.
 *
 * Called with the detail that was open when the click began and the one open
 * after every other handler has run, which is what makes clicking a second task
 * row swap the drawer's contents instead of closing and reopening it: that click
 * changed the key, so it opened a detail rather than dismissing one. Reading the
 * key twice around the same event is deterministic — capture runs before bubble
 * on one dispatch — where a timer racing the click would not be.
 *
 * Clicking the row that is already open counts as a dismissal, which makes a row
 * a toggle. That is the reading a reader is most likely to have in mind, and the
 * alternative is a click that visibly does nothing.
 */
export function dismissesOnClick(context: {
  openKey: string | null;
  keyAtPress: string | null;
  insideDrawer: boolean;
}): boolean {
  if (context.openKey === null) return false;
  if (context.insideDrawer) return false;
  return context.openKey === context.keyAtPress;
}

/**
 * Whether focus leaving the drawer should dismiss it.
 *
 * `nowhere` is the case this exists for. The drawer re-fetches and replaces its
 * contents on every snapshot, so anything focused inside it — a run row reached
 * by keyboard — is destroyed and focus falls back to the body, firing focusout
 * with no relatedTarget. Treating that as "focus left" would close the panel by
 * itself every time an agent reported anything, which is precisely when someone
 * is watching it. So only a move to a known element outside dismisses.
 */
export function dismissesOnFocus(context: {
  openKey: string | null;
  movedTo: 'inside' | 'outside' | 'nowhere';
}): boolean {
  if (context.openKey === null) return false;
  return context.movedTo === 'outside';
}

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
  /** What was open when the current click began; see `dismissesOnClick`. */
  private keyAtPress: string | null = null;

  async start(): Promise<void> {
    document.body.append(this.header(), this.body, this.drawer);
    this.store.onSnapshot(() => this.render());
    this.store.onConnection((state) => this.paintConnection(state));
    window.addEventListener('hashchange', () => {
      this.route = parseHash(location.hash);
      this.render();
    });
    document.addEventListener('keydown', (event) => this.onKey(event));
    this.watchDismissal();
    this.route = parseHash(location.hash);
    this.paintConnection('connecting');
    await this.store.start();
  }

  /**
   * Dismiss the drawer when attention moves off it.
   *
   * The drawer is a non-modal overlay: the page behind it stays usable, so it is
   * not a dialog and does not trap focus. What it should do is get out of the way
   * once you are plainly looking at something else, by pointer or by keyboard,
   * rather than sitting there until you find Escape or the ×.
   *
   * The key is read in the capture phase, before any row's own handler runs, and
   * compared in the bubble phase after they all have. That is what tells a click
   * that opened a different task from one that was simply elsewhere.
   */
  private watchDismissal(): void {
    document.addEventListener(
      'click',
      () => {
        this.keyAtPress = this.detailKey();
      },
      true,
    );
    document.addEventListener('click', (event) => {
      const target = event.target;
      const insideDrawer = target instanceof Node && this.drawer.contains(target);
      if (
        dismissesOnClick({
          openKey: this.detailKey(),
          keyAtPress: this.keyAtPress,
          insideDrawer,
        })
      ) {
        this.dismiss();
      }
      this.keyAtPress = null;
    });
    this.drawer.addEventListener('focusout', (event) => {
      const next = (event as FocusEvent).relatedTarget;
      const movedTo =
        next === null || next === undefined
          ? 'nowhere'
          : next instanceof Node && this.drawer.contains(next)
            ? 'inside'
            : 'outside';
      if (dismissesOnFocus({ openKey: this.detailKey(), movedTo })) {
        // Not `dismiss()`: focus has already gone where the reader sent it, and
        // pulling it back to the opener would fight them for it.
        this.go({ view: this.route.view });
      }
    });
  }

  /** Identifies the open detail, or null when the drawer is closed. */
  private detailKey(): string | null {
    if (this.route.task) return `task:${this.route.task}`;
    if (this.route.run) return `run:${this.route.run}`;
    return null;
  }

  /**
   * Close the drawer and hand focus back to the row that opened it.
   *
   * Found by `data-opens` rather than remembered as an element: opening the
   * drawer re-renders the list behind it, so the clicked node is already detached
   * by the time the panel is on screen. Re-finding it also means focus lands on
   * the row as it exists now, not on a stale copy.
   *
   * Without this, dismissing leaves focus on the body and the next Tab starts at
   * the top of the page — which for someone who opened the drawer from the
   * twentieth task row is twenty tabs back to where they were.
   */
  private dismiss(): void {
    const key = this.detailKey();
    this.go({ view: this.route.view });
    if (key === null) return;
    findOpener(key)?.focus();
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
    // Rendering rebuilds the lists, so a focused row is destroyed and focus falls
    // to the body. Snapshots arrive every couple of seconds during a run, which is
    // exactly when someone is watching, so a keyboard reader would lose their
    // place repeatedly while doing nothing. Noted before, restored after.
    const focused = this.focusedOpener();
    const scrolled = this.scrollOffsets();
    this.paintCounts(snapshot);
    this.paintView(snapshot);
    this.paintDrawer();
    this.restoreFocus(focused);
    // Last: focusing an element can scroll its container to bring it into view,
    // so the offset has to be settled after focus has moved, not before.
    this.restoreScroll(scrolled);
  }

  /**
   * Scroll offsets of the panes that opted in, by `data-scroll-key`.
   *
   * Repainting a view builds a fresh holder and swaps it in, and scroll position
   * lives on the element being thrown away — so the graph jumped back to the far
   * left every time anything re-rendered, including selecting a node, which is
   * the one moment you are certainly looking at a node somewhere off to the right.
   *
   * Keyed rather than positional because keys are view-specific: switching views
   * finds no match and starts at the top, which is right, while a repaint of the
   * same view restores.
   */
  private scrollOffsets(): Map<string, [number, number]> {
    const saved = new Map<string, [number, number]>();
    for (const pane of this.body.querySelectorAll<HTMLElement>('[data-scroll-key]')) {
      const key = pane.dataset.scrollKey;
      if (key) saved.set(key, [pane.scrollLeft, pane.scrollTop]);
    }
    return saved;
  }

  private restoreScroll(saved: Map<string, [number, number]>): void {
    if (!saved.size) return;
    for (const pane of this.body.querySelectorAll<HTMLElement>('[data-scroll-key]')) {
      const key = pane.dataset.scrollKey;
      const offset = key ? saved.get(key) : undefined;
      if (!offset) continue;
      [pane.scrollLeft, pane.scrollTop] = offset;
    }
  }

  /** The `data-opens` key of the focused row, if a row is what has focus. */
  private focusedOpener(): string | null {
    const active = document.activeElement;
    if (!(active instanceof HTMLElement)) return null;
    return active.getAttribute('data-opens');
  }

  /**
   * Put focus back on the row it was on, if rendering dropped it.
   *
   * Only when focus actually fell to the body: if the reader moved it themselves
   * — into the drawer, into the search box — that is where it belongs, and pulling
   * it back would be the page fighting them for it.
   */
  private restoreFocus(key: string | null): void {
    if (key === null) return;
    const active = document.activeElement;
    if (active !== null && active !== document.body) return;
    findOpener(key)?.focus();
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
        const holder = el('div', { class: 'graph-holder', 'data-scroll-key': 'graph' });
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
    close.addEventListener('click', () => this.dismiss());

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
      this.dismiss();
      return;
    }
    // Number keys jump between views: quick to reach while watching a run.
    const index = Number.parseInt(event.key, 10);
    if (index >= 1 && index <= VIEWS.length) {
      this.go({ view: VIEWS[index - 1].name });
    }
  }
}

/** The row that opens a given detail, as it exists in the DOM right now. */
function findOpener(key: string): HTMLElement | null {
  return document.querySelector<HTMLElement>(`[data-opens="${CSS.escape(key)}"]`);
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
