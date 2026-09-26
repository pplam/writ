/* built from ui/src (434712eb28d0) */
/*
 * writ dashboard — compiled from ui/src by ui/build.mjs.
 * Do not edit: change the TypeScript and rebuild.
 */
(() => {
"use strict";
// ---- types.js ----
/**
 * The shape of what `writ/api.py` returns.
 *
 * Hand-written rather than generated: the payload is small and stable, and a
 * generator would be another build step to install and keep working. These types
 * are the contract, and `tests/test_api.py` asserts the Python side matches them
 * field by field, so a rename on either side fails a test rather than silently
 * producing an undefined in the browser.
 */

// ---- dom.js ----
/**
 * Element helpers.
 *
 * No framework: this app renders a handful of views from one JSON document, and
 * a framework would be a build-time dependency and a runtime download to save
 * very little. What it would genuinely save is safe interpolation, so that is
 * what these helpers provide — `el` never parses a string as HTML, which means
 * a task title containing `<script>` is text, not markup.
 */
function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    apply(node, attrs);
    append(node, children);
    return node;
}
const SVG = 'http://www.w3.org/2000/svg';
function svg(tag, attrs, ...children) {
    const node = document.createElementNS(SVG, tag);
    apply(node, attrs);
    append(node, children);
    return node;
}
function apply(node, attrs) {
    if (!attrs)
        return;
    for (const [key, value] of Object.entries(attrs)) {
        if (value === undefined || value === false)
            continue;
        // `style` has to go through the CSSOM, not setAttribute. Our own CSP sets
        // style-src 'self' without 'unsafe-inline', which makes the browser drop a
        // style *attribute* silently — no error, no console warning, the element
        // just renders unstyled. Every progress meter rendered full for exactly
        // that reason. Assigning cssText is not inline style as far as CSP is
        // concerned, so it survives, and we keep the policy.
        if (key === 'style' && node instanceof HTMLElement) {
            node.style.cssText = String(value);
            continue;
        }
        node.setAttribute(key, String(value));
    }
}
function append(node, children) {
    for (const child of children) {
        if (child === null || child === undefined || child === false)
            continue;
        node.append(typeof child === 'string' ? document.createTextNode(child) : child);
    }
}
function clear(node) {
    node.replaceChildren();
}
/** A short id-ish label, monospaced by class rather than by inline style. */
function code(text) {
    return el('code', {}, text);
}
function classes(...names) {
    return names.filter(Boolean).join(' ');
}
/**
 * `replaceChildren` for a list that may contain nulls.
 *
 * Conditional sections read best as `condition ? node : null` inline, but the
 * native method rejects null. This filters, so callers keep the readable form.
 */
function replace(node, ...children) {
    node.replaceChildren(...children.filter((child) => child !== null && child !== undefined && child !== false));
}

// ---- format.js ----
/**
 * Presentation of values that appear in more than one view.
 *
 * These exist so a status looks the same everywhere, and so durations and times
 * are formatted once. Every function here is pure, which is also what makes them
 * testable without a DOM.
 */
/**
 * Status marks, the same ones the terminal uses.
 *
 * Matching `render.STATUS_MARKS` is deliberate: someone reading the dashboard
 * and someone reading `writ list` should not have to learn two vocabularies.
 */
const MARKS = {
    planned: '·',
    ready: '>',
    running: '*',
    reviewing: '*',
    starting: '*',
    'awaiting-review': '?',
    interrupted: '?',
    blocked: '!',
    completed: '+',
    failed: 'x',
    cancelled: '-',
};
function mark(status) {
    return MARKS[status] ?? '·';
}
/** Statuses that mean an agent is working right now. */
function isLive(status) {
    return status === 'running' || status === 'reviewing' || status === 'starting';
}
/** Seconds as something a human reads at a glance. */
function duration(seconds) {
    if (seconds === null || seconds === undefined)
        return '';
    if (seconds < 1)
        return '<1s';
    if (seconds < 60)
        return `${Math.round(seconds)}s`;
    const minutes = Math.floor(seconds / 60);
    const rest = Math.round(seconds % 60);
    if (minutes < 60)
        return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
    const hours = Math.floor(minutes / 60);
    return `${hours}h ${minutes % 60}m`;
}
/**
 * A timestamp as elapsed time.
 *
 * Absolute times in a log are hard to read while watching a run: "4m ago" is the
 * question you actually have. The exact value goes in a tooltip.
 */
function ago(stamp, now = Date.now()) {
    if (!stamp)
        return '';
    const then = Date.parse(stamp);
    if (Number.isNaN(then))
        return '';
    const seconds = Math.max(0, (now - then) / 1000);
    if (seconds < 45)
        return 'just now';
    if (seconds < 90)
        return 'a minute ago';
    if (seconds < 3600)
        return `${Math.round(seconds / 60)}m ago`;
    if (seconds < 86400)
        return `${Math.round(seconds / 3600)}h ago`;
    return `${Math.round(seconds / 86400)}d ago`;
}
function clock(stamp) {
    if (!stamp)
        return '';
    const parsed = new Date(stamp);
    if (Number.isNaN(parsed.getTime()))
        return stamp;
    return parsed.toLocaleTimeString([], {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
    });
}
function clip(text, chars) {
    return text.length <= chars ? text : `${text.slice(0, chars - 1)}…`;
}
/** `3/7`, or an empty string when there is nothing to count. */
function ratio(passed, total) {
    return total ? `${passed}/${total}` : '';
}
function percent(done, total) {
    return total ? Math.round((done / total) * 100) : 0;
}
/** The status a milestone or task list should sort to the top. */
const WEIGHT = {
    running: 0,
    reviewing: 0,
    'awaiting-review': 1,
    ready: 2,
    failed: 3,
    blocked: 4,
    planned: 5,
    cancelled: 6,
    completed: 7,
};
function statusWeight(status) {
    return WEIGHT[status] ?? 9;
}
function plural(count, word, suffix = 's') {
    return `${count} ${word}${count === 1 ? '' : suffix}`;
}

// ---- store.js ----
/**
 * The client's connection to the server: one snapshot, pushed on every change.
 *
 * The server sends whole snapshots rather than diffs. A diff protocol would save
 * bandwidth this app does not care about and cost a class of bug it very much
 * does: a page that has applied nine of ten deltas is subtly wrong with no way
 * to notice. A whole snapshot is idempotent, so a missed message is corrected by
 * the next one, and a reconnect needs no replay.
 */
const RETRY_MS = 1500;
class Store {
    snapshot = null;
    listeners = new Set();
    stateListeners = new Set();
    source = null;
    state = 'connecting';
    retry = null;
    stopped = false;
    get current() {
        return this.snapshot;
    }
    get connection() {
        return this.state;
    }
    onSnapshot(listener) {
        this.listeners.add(listener);
    }
    onConnection(listener) {
        this.stateListeners.add(listener);
    }
    /**
     * Load once, then follow.
     *
     * The initial fetch matters: on a quiet project no change is coming, and a page
     * that only listened would sit empty until someone happened to run something.
     */
    async start() {
        try {
            this.apply(await this.fetchJson('api/snapshot'));
        }
        catch {
            // The stream will deliver one shortly; no need to fail the whole page.
        }
        this.watchVisibility();
        // A tab restored in the background loads hidden and never fires a
        // visibilitychange, so check the state rather than waiting to be told.
        if (document.hidden) {
            this.stopped = true;
            this.setState('paused');
            return;
        }
        this.listen();
    }
    /**
     * Drop the stream while the tab is hidden, and catch up when it returns.
     *
     * Not an optimisation — a correctness fix found by leaving tabs open. A browser
     * allows about six connections per origin, and an event stream holds one for as
     * long as the tab lives. Half a dozen forgotten writ tabs therefore starve the
     * next one: it loads its HTML and then hangs with no error, because its stream
     * and its fetches are queued behind streams nobody is watching.
     *
     * A hidden tab has nothing to render, so it gives the connection back. Becoming
     * visible re-fetches before re-listening, since the changes it missed are not
     * replayed — and with whole snapshots, catching up is just asking again.
     */
    watchVisibility() {
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) {
                this.stopped = true;
                this.close();
                this.setState('paused');
                return;
            }
            this.stopped = false;
            void this.fetchJson('api/snapshot')
                .then((snapshot) => this.apply(snapshot))
                .catch(() => undefined)
                .finally(() => this.listen());
        });
    }
    close() {
        if (this.retry !== null) {
            window.clearTimeout(this.retry);
            this.retry = null;
        }
        this.source?.close();
        this.source = null;
    }
    listen() {
        if (this.stopped)
            return;
        this.close();
        this.source = new EventSource('events');
        this.source.addEventListener('snapshot', (event) => {
            this.setState('live');
            this.apply(JSON.parse(event.data));
        });
        this.source.addEventListener('ping', () => this.setState('live'));
        this.source.onopen = () => this.setState('live');
        this.source.onerror = () => {
            if (this.stopped)
                return;
            this.setState('reconnecting');
            this.close();
            // The usual cause is the server restarting, so retry rather than asking
            // the reader to reload a page that can fix itself.
            this.retry = window.setTimeout(() => this.listen(), RETRY_MS);
        };
    }
    apply(snapshot) {
        this.snapshot = snapshot;
        for (const listener of this.listeners)
            listener(snapshot);
    }
    setState(state) {
        if (this.state === state)
            return;
        this.state = state;
        for (const listener of this.stateListeners)
            listener(state);
    }
    /**
     * Details are fetched on demand rather than pushed.
     *
     * A task's evidence and a run's prompt and logs are large and only interesting
     * when opened. Pushing them on every change would make the snapshot enormous
     * to keep panels current that nobody is looking at.
     */
    task(id) {
        return this.fetchJson(`api/task/${encodeURIComponent(id)}`);
    }
    run(id) {
        return this.fetchJson(`api/run/${encodeURIComponent(id)}`);
    }
    /**
     * A planning step's live output, polled rather than pushed.
     *
     * Polled for two reasons. The snapshot stream fires on `state.json` changing,
     * and a step's transcript grows continuously while that file does not move at
     * all — so a snapshot watcher would never learn there was new output. And a
     * second `EventSource` would take another of the roughly six connections a
     * browser allows per origin, which is the bug the visibility handling above
     * exists to avoid; a short poll of a small payload, only while someone is
     * looking at that step, gives the connection straight back.
     */
    stepOutput(id) {
        return this.fetchJson(`api/phase/step/${encodeURIComponent(id)}`);
    }
    async fetchJson(path) {
        const response = await fetch(path, { headers: { accept: 'application/json' } });
        if (!response.ok) {
            throw new Error(`${path}: ${response.status} ${response.statusText}`);
        }
        return (await response.json());
    }
}

// ---- views/graph.js ----
/**
 * The DAG, drawn from the geometry the server computed.
 *
 * The layout is deliberately not done here. Depth, column and row come from
 * `api.graph` because they are derived from dependency rules that already exist
 * in Python; recomputing them in TypeScript would be a second implementation of
 * the same rules, free to disagree with the scheduler about what can run.
 */
const W = 200;
const H = 64;
function renderGraph(host, graph, selected, handlers) {
    clear(host);
    if (!graph.nodes.length) {
        host.append(emptyState());
        return;
    }
    const canvas = svg('svg', {
        class: 'dag',
        width: graph.width,
        height: graph.height,
        viewBox: `0 0 ${graph.width} ${graph.height}`,
        role: 'img',
        'aria-label': `dependency graph, ${graph.nodes.length} tasks in ${graph.levels} levels`,
    });
    for (const edge of graph.edges)
        canvas.append(drawEdge(edge));
    for (const node of graph.nodes) {
        canvas.append(drawNode(node, node.id === selected, handlers));
    }
    host.append(canvas);
}
function drawEdge(edge) {
    // A cubic curve with horizontal ends: parallel diagonals between columns are
    // hard to follow, and flat ends make it obvious which side of a node an edge
    // leaves and enters.
    const lift = Math.max(30, (edge.x2 - edge.x1) / 2);
    return svg('path', {
        class: classes('edge', edge.satisfied ? 'satisfied' : 'pending'),
        d: `M ${edge.x1} ${edge.y1} C ${edge.x1 + lift} ${edge.y1}, ${edge.x2 - lift} ${edge.y2}, ${edge.x2} ${edge.y2}`,
    });
}
function drawNode(node, isSelected, handlers) {
    const group = svg('g', {
        class: classes('node', node.status, isLive(node.status) && 'live', isSelected && 'selected'),
        transform: `translate(${node.x} ${node.y})`,
        tabindex: 0,
        role: 'button',
        'aria-label': `${node.id} ${node.title}, ${node.status}`,
    });
    group.append(svg('title', {}, tooltip(node)));
    group.append(svg('rect', { class: 'box', width: W, height: H, rx: 8 }));
    group.append(svg('text', { class: 'node-mark', x: 11, y: 19 }, mark(node.status)));
    group.append(svg('text', { class: 'node-id', x: 26, y: 19 }, node.id));
    group.append(svg('text', { class: 'node-count', x: W - 11, y: 19, 'text-anchor': 'end' }, ratio(node.passed, node.total)));
    group.append(svg('text', { class: 'node-title', x: 11, y: 38 }, node.title));
    group.append(svg('text', { class: 'node-status', x: 11, y: 54 }, node.status));
    if (node.total) {
        group.append(svg('rect', { class: 'track', x: 11, y: H - 7, width: W - 22, height: 3, rx: 1.5 }));
        group.append(svg('rect', {
            class: 'fill',
            x: 11,
            y: H - 7,
            width: ((W - 22) * node.passed) / node.total,
            height: 3,
            rx: 1.5,
        }));
    }
    const select = () => handlers.onSelect(node.id);
    group.addEventListener('click', select);
    group.addEventListener('keydown', (event) => {
        const key = event.key;
        if (key === 'Enter' || key === ' ') {
            event.preventDefault();
            select();
        }
    });
    return group;
}
function tooltip(node) {
    const lines = [`${node.id}  ${node.title}`, node.status];
    if (node.total)
        lines.push(`${node.passed}/${node.total} acceptance criteria`);
    if (node.depends_on.length)
        lines.push(`after: ${node.depends_on.join(', ')}`);
    if (node.blocked_by.length)
        lines.push(`waiting on: ${node.blocked_by.join(', ')}`);
    if (node.blocks.length)
        lines.push(`blocks: ${node.blocks.join(', ')}`);
    return lines.join('\n');
}
function emptyState() {
    const box = document.createElement('div');
    box.className = 'empty';
    box.textContent = 'No tasks yet. Run writ plan to build the graph.';
    return box;
}
/** Long titles are clipped in SVG, which has no text overflow of its own. */
function fitTitles(host) {
    for (const text of host.querySelectorAll('.node-title')) {
        const limit = W - 22;
        let content = text.textContent ?? '';
        while (content.length > 1 && text.getComputedTextLength() > limit) {
            content = content.slice(0, -2);
            text.textContent = `${content}…`;
        }
    }
}

// ---- views/phase.js ----
/**
 * The pre-execution phase: every agent writ runs before a single task is dispatched.
 *
 * This is the one card on the dashboard that can be busy while nothing has been
 * executed. `writ plan --critics --repair` is four to fourteen agent runs — three
 * analyses, a synthesis, the critics, then the repair rounds — and until this card
 * existed the page had nothing to say for the whole of it, because writ wrote
 * nothing to `state.json` until the commit at the end.
 *
 * Drawn from the server's geometry, for the same reason the task graph is: a column
 * is a wave, and waves come from `analysis.waves` and `critics.waves` — the
 * functions that decide what writ actually runs at once. Laying it out here would
 * be a second implementation of those rules, free to draw two boxes side by side
 * that writ intends to run one after the other.
 *
 * It shows the future as well as the past. A pending step is a box that is already
 * there, so a reader sees the shape of the attempt — which analyses, which critics,
 * whether repair is armed — rather than boxes appearing from nowhere one at a time.
 */
/** Node geometry. Only the box: every position comes from the server. */
const STEP_W = 200;
const STEP_H = 64;
/**
 * Status marks. The same vocabulary as the terminal and the rest of the dashboard,
 * with `reused` reading as the absence of a run rather than a kind of success.
 */
const STEP_MARKS = {
    pending: '○',
    running: '*',
    ok: '✓',
    reused: '·',
    failed: '✗',
    skipped: '–',
    abandoned: '?',
};
/** What each kind of step is, for a reader who has not read the source. */
const KIND_WORDS = {
    stage: 'analysis',
    synthesis: 'synthesis',
    commit: 'writ',
    critic: 'critic',
    repair: 'adjudicator',
    approval: 'writ',
};
function isPhase(phase) {
    return Boolean(phase) && typeof phase.id === 'string' && phase.id !== '';
}
/**
 * The phase as the Plan page's first card.
 *
 * Above the plan's own status, and in place of the pipeline's stage list, because
 * this answers a question that comes first in time: the status card says whether
 * work may start, and this says whether the thing being judged has finished being
 * built. During a planning run it is the only card with anything new to say.
 */
function renderPhase(host, phase, selected, handlers) {
    const done = (phase.counts.ok ?? 0) + (phase.counts.reused ?? 0);
    const failed = phase.counts.failed ?? 0;
    const holder = el('div', { class: 'phase-holder', 'data-scroll-key': 'phase' });
    holder.append(canvasFor(phase, selected, handlers));
    replace(host, el('header', { class: 'plan-head' }, el('h2', {}, 'Planning'), el('span', { class: classes('pill', phase.status) }, phase.status), phase.running
        ? el('span', { class: 'count live' }, el('span', { class: 'spinner', 'aria-hidden': 'true' }), el('span', { class: 'label' }, liveLabel(phase)))
        : null, phase.plan_id ? code(phase.plan_id) : null, phase.started_at
        ? el('span', { class: 'muted small', title: phase.started_at }, ago(phase.started_at))
        : null), el('p', { class: 'muted small' }, sentence(phase)), holder, el('div', { class: 'meta-row' }, el('span', {}, `${done}/${phase.steps.length} steps`), failed
        ? el('span', { class: 'count bad' }, el('b', {}, String(failed)), el('span', { class: 'label' }, 'failed'))
        : null, phase.counts.skipped
        ? el('span', {}, `${phase.counts.skipped} not reached`)
        : null, phase.counts.abandoned
        ? el('span', { class: 'count warn' }, el('b', {}, String(phase.counts.abandoned)), el('span', { class: 'label' }, 'abandoned'))
        : null, el('span', { class: 'muted small' }, 'a column runs at once')), phase.note ? el('p', { class: 'prose muted small' }, phase.note) : null);
}
/** What the phase is doing, in one sentence, in the present tense while it runs. */
function sentence(phase) {
    if (phase.running) {
        return 'Running the agents that produce the plan. Nothing is dispatched until this finishes and the plan is approved.';
    }
    if (phase.status === 'abandoned') {
        return 'The process that was planning is gone. Whatever step it was inside never reported; its transcript is on disk.';
    }
    if (phase.status === 'failed') {
        return 'Planning did not finish. What ran is below, and completed steps are reused if you run it again with the same --plan-id.';
    }
    if (phase.status === 'stopped') {
        return 'Planning stopped where it was asked to. Nothing after the last completed step was run.';
    }
    return 'Every step of the planning phase, in the order writ ran them.';
}
function liveLabel(phase) {
    if (!phase.live.length)
        return 'working';
    const names = phase.steps.filter((step) => phase.live.includes(step.id)).map((step) => step.name);
    return names.length > 2 ? `${plural(names.length, 'agent')} running` : names.join(', ');
}
function canvasFor(phase, selected, handlers) {
    const canvas = svg('svg', {
        class: 'dag phase-dag',
        width: phase.width,
        height: phase.height,
        viewBox: `0 0 ${phase.width} ${phase.height}`,
        role: 'img',
        'aria-label': `planning phase, ${plural(phase.steps.length, 'step')}`,
    });
    for (const edge of phase.edges)
        canvas.append(stepEdge(edge));
    for (const step of phase.steps) {
        canvas.append(stepNode(step, step.id === selected, handlers));
    }
    return canvas;
}
function stepEdge(edge) {
    const lift = Math.max(30, (edge.x2 - edge.x1) / 2);
    return svg('path', {
        class: classes('edge', edge.satisfied ? 'satisfied' : 'pending'),
        d: `M ${edge.x1} ${edge.y1} C ${edge.x1 + lift} ${edge.y1}, ${edge.x2 - lift} ${edge.y2}, ${edge.x2} ${edge.y2}`,
    });
}
function stepNode(step, isSelected, handlers) {
    const group = svg('g', {
        class: classes('node', 'step', step.status, step.status === 'running' && 'live', isSelected && 'selected'),
        transform: `translate(${step.x} ${step.y})`,
        tabindex: 0,
        role: 'button',
        'aria-label': `${step.name}, ${KIND_WORDS[step.kind]}, ${step.status}`,
    });
    group.append(svg('title', {}, stepTooltip(step)));
    group.append(svg('rect', { class: 'box', width: STEP_W, height: STEP_H, rx: 8 }));
    group.append(svg('text', { class: 'node-mark', x: 11, y: 19 }, STEP_MARKS[step.status] ?? '·'));
    group.append(svg('text', { class: 'node-id', x: 26, y: 19 }, step.name));
    group.append(svg('text', { class: 'node-count', x: STEP_W - 11, y: 19, 'text-anchor': 'end' }, duration(step.duration)));
    group.append(svg('text', { class: 'node-title', x: 11, y: 38 }, step.summary || KIND_WORDS[step.kind]));
    group.append(svg('text', { class: 'node-status', x: 11, y: 54 }, step.error ? 'failed' : step.note || step.status));
    const select = () => handlers.onStep(step.id);
    group.addEventListener('click', select);
    group.addEventListener('keydown', (event) => {
        const key = event.key;
        if (key === 'Enter' || key === ' ') {
            event.preventDefault();
            select();
        }
    });
    return group;
}
function stepTooltip(step) {
    const lines = [`${step.name}  (${KIND_WORDS[step.kind]})`, step.status];
    if (step.summary)
        lines.push(step.summary);
    if (step.display)
        lines.push(step.display);
    if (step.duration !== null)
        lines.push(duration(step.duration));
    if (step.artifact)
        lines.push(`wrote ${step.artifact}`);
    if (step.error)
        lines.push(step.error);
    return lines.join('\n');
}
/** Long summaries are clipped in SVG, which has no text overflow of its own. */
function fitStepTitles(host) {
    for (const text of host.querySelectorAll('.step .node-title, .step .node-status')) {
        const limit = STEP_W - 22;
        let content = text.textContent ?? '';
        while (content.length > 1 && text.getComputedTextLength() > limit) {
            content = content.slice(0, -2);
            text.textContent = `${content}…`;
        }
    }
}
/**
 * One step in the drawer: what ran it, how it ended, and what it is saying.
 *
 * The command is here because it is the first thing anyone does with a step that
 * hung or wrote nothing: run its own invocation by hand. It is the resolved
 * command, with the model and event flags this step chose, not the agent's name.
 */
function renderStepDetail(host, step, output) {
    replace(host, el('header', { class: 'detail-head' }, el('h2', {}, step.name), el('span', { class: classes('pill', step.status) }, step.status), el('span', { class: 'muted small' }, KIND_WORDS[step.kind])), step.summary ? el('p', { class: 'muted' }, step.summary) : null, el('div', { class: 'meta-row' }, step.duration !== null ? el('span', {}, duration(step.duration)) : null, step.started_at
        ? el('span', { class: 'muted small', title: step.started_at }, `started ${ago(step.started_at)}`)
        : null, step.exit_code !== null ? el('span', {}, `exit ${step.exit_code}`) : null, step.model ? code(step.model) : null, step.artifact ? code(step.artifact) : null), step.note ? el('p', { class: 'muted small' }, step.note) : null, step.error ? el('p', { class: 'prose error' }, step.error) : null, step.command ? el('pre', { class: 'command' }, step.command) : null, step.directory ? el('pre', { class: 'command' }, step.directory) : null, ...outputPanes(step, output));
}
function outputPanes(step, output) {
    if (!step.has_output) {
        // A commit or an approval is writ's own work. An empty output pane here would
        // read as an agent that said nothing rather than as one that never existed.
        return [el('p', { class: 'muted small' }, 'This step is writ itself, not an agent: there is no transcript.')];
    }
    if (!output)
        return [el('p', { class: 'muted' }, 'Loading output…')];
    const activity = output.activity.length
        ? el('pre', { class: 'log activity', 'data-scroll-key': `step-activity:${step.id}` }, output.activity.join('\n'))
        : null;
    const text = output.text.text
        ? el('pre', { class: 'log', 'data-scroll-key': `step-log:${step.id}` }, output.text.text)
        : null;
    if (!activity && !text) {
        return [
            el('p', { class: 'muted small' }, step.status === 'pending'
                ? 'Not started yet.'
                : 'Nothing on the transcript yet. The agent has not produced output.'),
        ];
    }
    return [
        activity ? el('h3', {}, 'Activity') : null,
        activity,
        text ? el('h3', {}, 'Output') : null,
        text,
        output.text.truncated ? el('p', { class: 'muted small' }, 'showing the tail of a longer log') : null,
    ];
}

// ---- views/overview.js ----
/**
 * The overview: where the project stands, what is happening now, what it cost.
 *
 * Laid out as two regions rather than a bag of equal cards, because the cards
 * are not equal shapes. Progress, live agents, decisions and throughput are
 * fixed-height summaries; milestones and activity are lists that grow with the
 * project. Flowing all six through one `auto-fit` grid gave the timeline a
 * third of the width and made it eight hundred pixels tall while two thirds of
 * the row sat empty. So: a summary band across the top, then a two-column split
 * with the lists side by side.
 *
 * Ordered by what a reader needs first. Live agents come before totals because
 * during a run that is the only volatile thing on the page; proposed decisions
 * come next because they are the one class of work no `writ run` will ever
 * clear, so a project can sit there quietly blocked on a human.
 */
const STATUS_ORDER = [
    'running',
    'reviewing',
    'awaiting-review',
    'ready',
    'planned',
    'blocked',
    'failed',
    'cancelled',
    'completed',
];
function renderOverview(host, snapshot, handlers) {
    const { overview } = snapshot;
    replace(host, el('div', { class: 'ov-band' }, progressCard(overview), liveCard(overview, handlers), attentionCard(overview, handlers)), el('div', { class: 'ov-split' }, el('div', { class: 'ov-column' }, milestonesCard(overview, handlers), throughputCard(overview)), activityCard(snapshot.activity, handlers)));
}
function card(title, ...body) {
    return el('section', { class: 'card' }, el('h2', {}, title), ...body);
}
/** A card whose heading carries a count, so the title is not a lie when empty. */
function countedCard(title, count, ...body) {
    return el('section', { class: 'card' }, el('h2', {}, title, count ? el('span', { class: 'h2-count' }, String(count)) : null), ...body);
}
function progressCard(overview) {
    const done = percent(overview.completed, overview.tasks);
    return el('section', { class: 'card ov-progress' }, el('h2', {}, 'Progress'), el('div', { class: 'headline' }, el('span', { class: 'big' }, `${done}%`), el('span', { class: 'muted' }, `${overview.completed} of ${plural(overview.tasks, 'task')} complete`)), el('div', { class: 'meter' }, el('div', { class: 'meter-fill', style: `width:${done}%` })), el('ul', { class: 'chips' }, ...STATUS_ORDER.filter((status) => overview.counts[status]).map((status) => el('li', { class: classes('chip', status) }, el('span', { class: 'chip-mark' }, mark(status)), el('b', {}, String(overview.counts[status])), el('span', { class: 'chip-label' }, status)))));
}
function liveCard(overview, handlers) {
    const rows = overview.active_runs;
    return countedCard('Working now', rows.length, rows.length
        ? el('ul', { class: 'run-list' }, ...rows.map((run) => liveRow(run, handlers)))
        : el('p', { class: 'blank' }, 'No agents running.'));
}
function liveRow(run, handlers) {
    const row = el('li', { class: 'run-row live' }, el('span', { class: 'spinner', 'aria-hidden': 'true' }), el('button', { class: 'link', type: 'button', 'data-opens': `task:${run.task}` }, run.task), el('span', { class: 'verb' }, run.role === 'reviewer' ? 'review' : 'dispatch'), el('span', { class: 'grow' }), el('span', { class: 'muted mono small clip' }, run.model || run.command), el('span', { class: 'muted tnum', title: run.started_at ?? '' }, duration(run.duration)));
    row.querySelector('button')?.addEventListener('click', () => handlers.onTask(run.task));
    return row;
}
/**
 * What is waiting on a human. Proposed decisions and failures both qualify:
 * neither will clear on its own, and both are easy to miss while a run is
 * printing progress.
 */
function attentionCard(overview, handlers) {
    const proposals = overview.proposed_decisions;
    const failed = overview.counts.failed ?? 0;
    const review = overview.counts['awaiting-review'] ?? 0;
    if (!proposals && !failed && !review) {
        return card('Waiting on you', el('p', { class: 'blank' }, 'Nothing needs a human.'));
    }
    const link = (label, view, kind) => {
        const button = el('button', { class: classes('need', kind), type: 'button' }, label);
        button.addEventListener('click', () => handlers.onGoto(view));
        return button;
    };
    return countedCard('Waiting on you', proposals + failed, el('div', { class: 'needs' }, proposals
        ? link(`${plural(proposals, 'decision')} to rule on`, 'decisions', 'review')
        : null, failed ? link(`${plural(failed, 'task')} failed`, 'tasks', 'bad') : null, 
    // Not a human's job, but worth distinguishing from idle: a run will pick
    // these up, so they are listed without the urgent styling.
    review ? link(`${review} awaiting review`, 'tasks', 'calm') : null), proposals
        ? el('p', { class: 'muted small' }, 'Proposals stay inert until confirmed, so no run will clear them.')
        : null);
}
function throughputCard(overview) {
    const t = overview.throughput;
    const stat = (label, value, hint) => el('div', { class: 'stat' }, el('span', { class: 'stat-value' }, value), el('span', { class: 'stat-label' }, label), hint ? el('span', { class: 'stat-hint' }, hint) : null);
    return card('Agent work', el('div', { class: 'stats' }, stat('runs', String(t.runs)), stat('in agents', duration(t.agent_seconds)), stat('median run', duration(t.median_seconds)), stat('reviews', String(t.reviews), t.reviews ? `${t.rejected} rejected` : undefined), stat('failed', String(t.failures))));
}
function milestonesCard(overview, handlers) {
    const button = el('button', { class: 'link small', type: 'button' }, 'all milestones');
    button.addEventListener('click', () => handlers.onGoto('milestones'));
    return el('section', { class: 'card' }, el('h2', {}, 'Milestones', el('span', { class: 'grow' }), button), overview.milestones.length
        ? el('ul', { class: 'milestone-list' }, ...overview.milestones.map((m) => milestoneRow(m, handlers)))
        : el('p', { class: 'blank' }, 'No milestones.'));
}
/**
 * A milestone as a grid row, not a flex line. The parts have wildly different
 * widths — a two-word title next to a long one — and flex-wrap turned that into
 * a ragged block per milestone. A grid keeps id, bar and count in a column.
 */
function milestoneRow(m, handlers) {
    const done = percent(m.done, m.total);
    const row = el('li', { class: classes('milestone', m.status, 'clickable'), tabindex: 0, role: 'button' }, el('span', { class: classes('mark', m.status) }, mark(m.status)), code(m.id), el('span', { class: 'title clip' }, m.title), el('div', { class: 'meter thin' }, el('div', { class: 'meter-fill', style: `width:${done}%` })), el('span', { class: 'muted mono tnum' }, ratio(m.done, m.total)));
    const open = () => handlers.onGoto('milestones');
    row.addEventListener('click', open);
    row.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            open();
        }
    });
    return row;
}
function activityCard(events, handlers) {
    return el('section', { class: 'card ov-activity' }, el('h2', {}, 'Recent activity'), events.length
        ? el('ol', { class: 'activity' }, ...events.slice(0, 24).map((e) => activityRow(e, handlers)))
        : el('p', { class: 'blank' }, 'Nothing has happened yet.'));
}
/**
 * Two lines, not one. The event text and its summary were competing for a
 * single ellipsised row, which cut both; the summary is the agent's own sentence
 * about what it did, so it gets its own line under the event.
 */
function activityRow(event, handlers) {
    const row = el('li', { class: classes('event', event.kind, isLive(event.status) && 'live') }, el('span', { class: classes('mark', event.status) }, mark(event.status)), el('div', { class: 'event-body' }, el('div', { class: 'event-line' }, el('span', { class: 'what' }, event.text), el('span', { class: 'grow' }), el('span', { class: 'when muted', title: event.at }, ago(event.at))), event.summary ? el('p', { class: 'event-summary' }, event.summary) : null));
    row.setAttribute('title', `${clock(event.at)} · ${event.text}`);
    if (event.run) {
        row.classList.add('clickable');
        row.setAttribute('data-opens', `run:${event.run}`);
        row.addEventListener('click', () => handlers.onRun(event.run));
    }
    return row;
}

// ---- views/tasks.js ----
/**
 * The task list and the task detail panel.
 *
 * The list is filterable because the useful questions are subsets: what is
 * blocked, what is waiting on me, what failed. The detail panel exists because
 * `writ show` is the command people run most and it answers "what is this task
 * supposed to prove, and what has it proved so far".
 */
const FILTERS = {
    all: () => true,
    live: (t) => isLive(t.status),
    ready: (t) => t.status === 'ready',
    'awaiting review': (t) => t.status === 'awaiting-review',
    blocked: (t) => t.status === 'blocked' || t.blocked_by.length > 0,
    failed: (t) => t.status === 'failed',
    done: (t) => t.status === 'completed',
};
function renderTaskList(host, tasks, options, handlers) {
    const predicate = FILTERS[options.filter] ?? FILTERS.all;
    const query = options.query.trim().toLowerCase();
    const rows = tasks
        .filter(predicate)
        .filter((task) => !query ||
        task.id.toLowerCase().includes(query) ||
        task.title.toLowerCase().includes(query))
        .sort((a, b) => statusWeight(a.status) - statusWeight(b.status) || a.id.localeCompare(b.id));
    replace(host, rows.length
        ? el('ul', { class: 'task-list' }, ...rows.map((t) => taskRow(t, t.id === options.selected, handlers)))
        : el('p', { class: 'empty' }, 'No tasks match.'));
}
function taskRow(task, isSelected, handlers) {
    const row = el('li', {
        class: classes('task-row', task.status, isLive(task.status) && 'live', isSelected && 'selected'),
        tabindex: 0,
        role: 'button',
        // What this row opens. The drawer hands focus back here when dismissed, and
        // it cannot hold the element itself: opening re-renders the list, so the
        // node that was clicked is gone by the time the drawer is on screen.
        'data-opens': `task:${task.id}`,
    }, el('span', { class: 'mark' }, mark(task.status)), code(task.id), el('span', { class: 'title' }, task.title), el('span', { class: 'grow' }), task.blocked_by.length
        ? el('span', { class: 'muted small', title: `waiting on ${task.blocked_by.join(', ')}` }, `waits on ${task.blocked_by.length}`)
        : null, el('span', { class: 'muted mono' }, ratio(task.passed, task.total)), el('span', { class: classes('pill', task.status) }, task.status));
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
function renderTaskDetail(host, task, handlers) {
    replace(host, el('header', { class: 'detail-head' }, el('div', { class: 'detail-title' }, el('span', { class: classes('mark', task.status) }, mark(task.status)), code(task.id), el('span', { class: classes('pill', task.status) }, task.status)), el('h2', {}, task.title)), metaRow(task), 
    // Above the criteria, because for a blocked task this is the answer to the only
    // question being asked. It used to be readable only as one evidence line below
    // four other sections, while Dependencies showed everything satisfied — so the
    // page looked like writ had stopped for no reason it could name.
    blockedSection(task), 
    // A held gate reads as `blocked` and has no unmet dependency, so without this
    // the page shows a stopped project with nothing saying why. Above the criteria
    // for the same reason `blockedSection` is: it is the only question being asked.
    heldSection(task), acceptanceSection(task), gateSection(task), task.notes ? section('Notes', null, el('p', { class: 'prose' }, task.notes)) : null, dependencySection(task), guardrailSection(task), runSection(task, handlers), evidenceSection(task));
}
/**
 * Why a gate is parked, and what would move it.
 *
 * Distinct from `blockedSection`, which reports an agent's own account of what
 * stopped it. This is writ declining to spend more agents: the repair loop is out
 * of rounds, or the gate asked a question only a person can answer. The remedy is
 * never "wait" — it is reading the record named here.
 */
function heldSection(task) {
    const held = task.held;
    if (!held)
        return null;
    const advice = {
        'awaiting-repair': 'A repair is being planned. The gate will be asked again on the repaired code.',
        'needs-decision': 'The gate asked something only a person can settle. Rule on it in the decision log.',
        'repair-exhausted': 'The repair loop ran out of rounds. What is wrong is the plan, not the wording of a patch.',
        'repair-refused': 'Writ turned down every patch the planner proposed. Read the refusals before re-planning.',
    };
    return section('Held', held.at ? ago(held.at) : null, el('div', { class: 'meta-row' }, el('span', { class: 'pill needs-repair' }, held.reason)), el('p', { class: 'prose' }, advice[held.reason] ?? 'This gate will not re-run on its own.'), held.detail ? el('p', { class: 'prose' }, held.detail) : null, held.questions?.length
        ? el('ul', { class: 'held-questions' }, ...held.questions.map((q) => el('li', {}, q)))
        : null, held.request ? el('pre', { class: 'command' }, `writ show ${held.request}`) : null);
}
/**
 * What a gate has decided, oldest first.
 *
 * A gate is asked again after every repair, so its history is the record of the
 * plan converging — or not. Read in order it says whether each round closed
 * something or found the same thing again, which one attempt cannot say.
 */
function gateSection(task) {
    if (task.kind !== 'gate' || !task.gate_attempts.length)
        return null;
    return section('Gate reviews', String(task.gate_attempts.length), el('ol', { class: 'gate-attempts' }, ...task.gate_attempts.map((attempt) => el('li', { class: classes('gate-attempt', attempt.decision) }, el('div', { class: 'meta-row' }, el('span', { class: classes('pill', attempt.decision) }, attempt.decision), el('span', { class: 'muted small' }, `revision ${attempt.revision}`), attempt.actor ? el('span', { class: 'muted small' }, attempt.actor) : null, attempt.at ? el('span', { class: 'muted small', title: attempt.at }, ago(attempt.at)) : null), attempt.summary ? el('p', { class: 'prose' }, attempt.summary) : null, attempt.findings.length
        ? el('div', { class: 'meta-row' }, el('span', {}, 'raised '), ...attempt.findings.map(code))
        : null))));
}
/**
 * A titled section. The optional note is a count or ratio: it belongs to the
 * title but is not part of its name, so it is a separate quieter element rather
 * than punctuation inside the string.
 */
function section(title, note, ...body) {
    return el('section', { class: 'detail-section' }, el('h3', {}, title, note ? el('span', { class: 'h3-note' }, note) : null), ...body);
}
/**
 * Where this task came from. Rendered as discrete labelled items: these used to
 * be bare spans separated only by a flex gap, so a milestone id ran straight
 * into a design section and then into a file path with nothing to show where
 * one ended and the next began.
 */
function metaRow(task) {
    const item = (label, value, mono = false) => el('div', { class: 'meta-item' }, el('span', { class: 'meta-label' }, label), typeof value === 'string'
        ? el('span', { class: classes('meta-value', mono && 'mono') }, value)
        : el('span', { class: 'meta-value' }, value));
    const bits = [];
    if (task.milestone)
        bits.push(item('milestone', code(task.milestone)));
    if (task.design_section)
        bits.push(item('section', task.design_section));
    if (task.design_doc)
        bits.push(item('design', basename(task.design_doc), true));
    bits.push(item('updated', ago(task.updated_at)));
    const row = el('div', { class: 'meta-row' }, ...bits);
    if (task.design_doc)
        row.setAttribute('title', task.design_doc);
    return row;
}
/** The path is usually long and usually irrelevant past the filename. */
function basename(path) {
    const parts = path.split('/');
    return parts[parts.length - 1] || path;
}
function acceptanceSection(task) {
    if (!task.acceptances.length) {
        return section('Acceptance', null, el('p', { class: 'blank' }, 'No criteria recorded.'));
    }
    return section('Acceptance', `${ratio(task.passed, task.total)} passed`, el('ol', { class: 'criteria' }, ...task.acceptances.map(criterion)));
}
function criterion(item) {
    return el('li', { class: classes('criterion', item.status) }, el('div', { class: 'criterion-head' }, 
    // ASCII, matching the terminal's marks rather than inventing typographic
    // ones for this one spot.
    el('span', { class: classes('mark', item.status) }, item.status === 'passed' ? '+' : item.status === 'failed' ? 'x' : '·'), el('span', { class: 'criterion-text' }, item.text), el('span', { class: classes('pill', item.status) }, item.status)), 
    // Evidence is the point of a criterion: a claim with nothing behind it is
    // exactly what the reviewer is there to catch, so it is shown, not hidden.
    item.evidence ? el('p', { class: 'evidence' }, item.evidence) : null, item.by
        ? el('p', { class: 'criterion-by muted small', title: item.at }, el('span', { class: 'actor' }, item.by), el('span', {}, ago(item.at)))
        : null);
}
function guardrailSection(task) {
    if (!task.allowed.length && !task.forbidden.length)
        return null;
    const rail = (kind, heading, paths) => el('div', { class: classes('rail', kind) }, el('h4', {}, heading), el('ul', {}, ...paths.map((p) => el('li', { class: 'mono' }, p))));
    return section('Guardrails', null, el('div', { class: 'rails' }, task.allowed.length ? rail('allowed', 'may touch', task.allowed) : null, task.forbidden.length ? rail('forbidden', 'must not touch', task.forbidden) : null));
}
function dependencySection(task) {
    if (!task.depends_on.length && !task.blocks.length && !task.unknown_deps.length)
        return null;
    const group = (heading, ids, kind) => el('div', { class: 'dep-group' }, el('h4', { class: classes(kind) }, heading), el('div', { class: 'dep-line' }, ...ids.map((id) => code(id))));
    return section('Dependencies', null, el('div', { class: 'deps' }, task.depends_on.length ? group('runs after', task.depends_on) : null, task.blocked_by.length ? group('still waiting on', task.blocked_by, 'warn') : null, task.blocks.length ? group('blocks', task.blocks) : null, 
    // A dangling id means this task can never become ready. Say so here rather
    // than letting it sit in the list looking merely slow.
    task.unknown_deps.length
        ? group('missing, so this can never become ready', task.unknown_deps, 'error')
        : null));
}
function runSection(task, handlers) {
    if (!task.run_list.length) {
        return section('Runs', null, el('p', { class: 'blank' }, 'Never dispatched.'));
    }
    return section('Runs', String(task.run_list.length), el('ul', { class: 'run-list' }, ...[...task.run_list].reverse().map((run) => {
        const row = el('li', {
            class: classes('run-row', run.status, isLive(run.status) && 'live', 'clickable'),
            'data-opens': `run:${run.id}`,
        }, el('span', { class: classes('mark', run.status) }, mark(run.status)), el('span', { class: 'verb' }, run.role === 'reviewer' ? 'review' : 'dispatch'), el('span', { class: 'grow' }), run.decision ? el('span', { class: classes('pill', run.decision) }, run.decision) : null, el('span', { class: 'muted mono small clip' }, run.model || run.command), el('span', { class: 'muted small tnum', title: run.started_at ?? '' }, ago(run.started_at)));
        row.addEventListener('click', () => handlers.onRun(run.id));
        return row;
    })));
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
function blockedSection(task) {
    if (!task.blocked_on)
        return null;
    return section('Blocked on', null, el('p', { class: 'prose blocked-reason' }, task.blocked_on));
}
function evidenceSection(task) {
    if (!task.evidence.length)
        return null;
    return section('History', null, el('ol', { class: 'history' }, ...[...task.evidence].reverse().map((item) => el('li', {}, el('span', { class: 'actor' }, item.actor), el('span', { class: 'history-text' }, item.text), el('span', { class: 'grow' }), el('span', { class: 'muted small tnum', title: item.at }, ago(item.at))))));
}

// ---- views/runs.js ----
/**
 * The run log and the run detail panel.
 *
 * This is the observability surface. A run holds the three things you need when
 * an agent did something unexpected: the exact prompt it was given, what it
 * printed, and the verdict it wrote. All three are on disk already — `writ logs`
 * shows one of them — and the reason to put them on a page together is that the
 * question is usually "what did it see, and what did it claim about it".
 */
const RUN_FILTERS = {
    all: () => true,
    live: (r) => isLive(r.status),
    reviews: (r) => r.role === 'reviewer',
    failed: (r) => r.status === 'failed' || r.exit_code !== 0 && r.exit_code !== null,
    rejected: (r) => r.decision === 'reject',
};
function renderRunList(host, runs, options, handlers) {
    const predicate = RUN_FILTERS[options.filter] ?? RUN_FILTERS.all;
    const rows = runs.filter(predicate);
    replace(host, rows.length
        ? el('ul', { class: 'run-list wide' }, ...rows.map((r) => runRow(r, r.id === options.selected, handlers)))
        : el('p', { class: 'empty' }, 'No runs match.'));
}
function runRow(run, isSelected, handlers) {
    const row = el('li', {
        class: classes('run-row', run.status, isLive(run.status) && 'live', isSelected && 'selected', 'clickable'),
        tabindex: 0,
        role: 'button',
        // See tasks.ts: the detail this row opens, so focus can come back to it.
        'data-opens': `run:${run.id}`,
    }, el('span', { class: 'mark' }, mark(run.status)), el('span', { class: classes('verb', run.role) }, run.role === 'reviewer' ? 'review' : 'dispatch'), code(run.task), el('span', { class: 'muted mono small' }, run.model || run.command), el('span', { class: 'grow' }), run.decision ? el('span', { class: classes('pill', run.decision) }, run.decision) : null, run.exit_code !== null && run.exit_code !== 0
        ? el('span', { class: 'pill failed' }, `exit ${run.exit_code}`)
        : null, el('span', { class: 'muted mono small' }, duration(run.duration)), el('span', { class: 'muted small', title: run.started_at ?? run.created_at }, ago(run.started_at ?? run.created_at)));
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
function renderRunDetail(host, run, handlers) {
    const taskButton = el('button', { class: 'link', type: 'button' }, run.task);
    taskButton.addEventListener('click', () => handlers.onTask(run.task));
    replace(host, el('header', { class: 'detail-head' }, el('span', { class: classes('mark', run.status) }, mark(run.status)), code(run.id), el('span', { class: classes('pill', run.status) }, run.status), run.exit_code !== null ? el('span', { class: 'muted mono' }, `exit ${run.exit_code}`) : null), el('div', { class: 'meta-row' }, el('span', {}, run.role === 'reviewer' ? 'review of ' : 'dispatch of ', taskButton), run.model ? el('span', { class: 'mono small' }, run.model) : null, el('span', { class: 'mono small' }, run.command), run.duration !== null ? el('span', {}, duration(run.duration)) : null, run.started_at ? el('span', { title: run.started_at }, `started ${clock(run.started_at)}`) : null), verdictSection(run), problemSection(run), tabs(run));
}
function verdictSection(run) {
    if (!run.decision && !run.summary && !run.unmet.length)
        return null;
    return el('section', { class: 'detail-section' }, el('h3', {}, 'Verdict'), el('div', { class: 'verdict-head' }, run.decision ? el('span', { class: classes('pill', run.decision) }, run.decision) : null, run.resulting_status
        ? el('span', { class: 'muted' }, `task became ${run.resulting_status}`)
        : null, run.unmet.length ? el('span', { class: 'warn' }, `unmet: ${run.unmet.join(', ')}`) : null), run.summary ? el('p', { class: 'prose' }, run.summary) : null, run.decisions.length
        ? el('div', { class: 'proposed' }, el('h4', {}, 'decisions proposed'), el('ul', {}, ...run.decisions.map((title) => el('li', {}, title))))
        : null);
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
function noVerdictOutcome(run) {
    if (!run.resulting_status)
        return 'the task was left unjudged';
    if (run.resulting_status === 'awaiting-review') {
        return 'the task is still awaiting review, with the implementation intact';
    }
    if (run.resulting_status === 'planned') {
        return 'the task was returned to the queue rather than judged';
    }
    return `the task became ${run.resulting_status} rather than judged`;
}
function problemSection(run) {
    if (!run.verdict_error &&
        !run.no_verdict &&
        !run.note &&
        !run.verdict_downgraded &&
        !run.verdict_misplaced)
        return null;
    return el('section', { class: 'detail-section problem' }, el('h3', {}, 'Problem'), 
    // The most confusing failure writ has: the agent exits 0, the run reads
    // "completed", and the task did not move. Nothing on the page explains that
    // unless this does.
    run.no_verdict
        ? el('p', { class: 'error' }, 
        // A second sentence rather than another clause. The reason already carries
        // its own "— and ..." diagnosis for the silent and unparsed-call cases,
        // and hanging the consequence off that too produced a sentence with three
        // clauses and two dashes that had to be read twice.
        `${run.no_verdict}. Its acceptance criteria were left untouched, and ` +
            `${noVerdictOutcome(run)}.`)
        : null, 
    // A silent run is not an agent that skipped its report, and telling the two
    // apart is the difference between re-reading a transcript that says nothing
    // and going to look at the agent's own configuration.
    run.no_verdict && run.no_output
        ? el('p', { class: 'muted' }, 'The transcript is empty, so start with the invocation rather than the ' +
            'prompt: check the model id, that the agent is authenticated for ' +
            'that provider, and that its quota is not exhausted. Running ', el('code', {}, run.command), ' by hand usually says which.')
        : null, 
    // The transcript looks like work, so the reader's instinct is to read it for a
    // reason the agent declined to report. There isn't one: it never got that far.
    run.no_verdict && run.unparsed_tool_call
        ? el('p', { class: 'muted' }, 'The transcript ends with tool-call markup as text, so the model wrote a ' +
            'call the agent could not parse and the turn ended there. Nothing in ' +
            'the prompt causes that — try the task on a model whose tool calling ' +
            'is more reliable.')
        : null, run.verdict_error ? el('p', { class: 'error' }, run.verdict_error) : null, 
    // Not an error: the verdict was applied, with the claim lowered to match the
    // criteria under it. Shown here because a task that reads "failed" against a
    // summary claiming success is otherwise unexplained.
    run.verdict_downgraded
        ? el('p', { class: 'muted' }, run.verdict_downgraded)
        : null, 
    // Also not an error: the report was found and used, just not where writ put
    // the agent's instructions. Shown so a recurring habit is visible.
    run.verdict_misplaced
        ? el('p', { class: 'muted' }, 'The verdict was written to ', el('code', {}, run.verdict_misplaced), ' rather than the path the agent was given. Writ used it from there.')
        : null, run.note ? el('p', { class: 'muted' }, run.note) : null);
}
/**
 * Prompt, stdout, stderr and the raw verdict as tabs.
 *
 * Tabs rather than four stacked panes: each is long, and the reader wants one at
 * a time. The prompt is first because it is the one artifact nothing else
 * surfaces, and the usual question about a surprising run is what it was told.
 */
function tabs(run) {
    const panes = [
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
        const button = el('button', { class: classes('tab', index === 0 && 'active'), type: 'button', role: 'tab' }, pane.name, pane.note ? el('span', { class: 'tab-note' }, pane.note) : null);
        button.addEventListener('click', () => {
            for (const other of strip.querySelectorAll('.tab'))
                other.classList.remove('active');
            button.classList.add('active');
            holder.replaceChildren(pane.body);
            // Logs are read from the end while a run is in flight.
            if (pane.name !== 'prompt')
                pane.body.scrollTop = pane.body.scrollHeight;
        });
        strip.append(button);
    });
    holder.append(panes[0].body);
    return el('section', { class: 'detail-section' }, el('h3', {}, 'What the agent saw and said'), strip, holder);
}
function logPane(tail) {
    if (!tail.text)
        return el('pre', { class: 'log empty-log' }, '(nothing)');
    const body = pre(tail.text);
    if (tail.truncated) {
        body.prepend(el('div', { class: 'truncated' }, `showing the last part of ${tail.bytes} bytes`));
    }
    return body;
}
function pre(text) {
    return el('pre', { class: 'log' }, text);
}
function bytes(tail) {
    if (!tail.bytes)
        return 'empty';
    if (tail.bytes < 1024)
        return `${tail.bytes} B`;
    return `${Math.round(tail.bytes / 1024)} KB`;
}

// ---- views/decisions.js ----
/**
 * Decisions an agent recorded, proposals first.
 *
 * A proposal is a fork an agent hit that the design document did not settle. It
 * stays inert until a human rules on it, which makes it the one kind of work no
 * `writ run` will ever clear — so this view shows the ruling command rather than
 * a button. The dashboard is read-only, and a decision is exactly the kind of
 * thing that should be typed deliberately with a reason attached.
 */
function renderDecisions(host, decisions) {
    if (!decisions.length) {
        replace(host, el('p', { class: 'empty' }, 'No decisions recorded. Agents propose them as they work.'));
        return;
    }
    const proposed = decisions.filter((d) => d.status === 'proposed');
    const settled = decisions.filter((d) => d.status !== 'proposed');
    replace(host, proposed.length
        ? el('section', { class: 'card urgent' }, el('h2', {}, `Waiting on a ruling (${proposed.length})`), el('p', { class: 'muted small' }, 'An agent hit a fork the design did not settle. Until you rule, this is recorded but not in force.'), el('div', { class: 'decision-grid' }, ...proposed.map(decisionCard)))
        : null, settled.length
        ? el('section', { class: 'card' }, el('h2', {}, 'Settled'), el('div', { class: 'decision-grid' }, ...settled.map(decisionCard)))
        : null);
}
function decisionCard(decision) {
    return el('article', { class: classes('decision', decision.status) }, el('header', {}, code(decision.id), el('h3', {}, decision.title), el('span', { class: classes('pill', decision.status) }, decision.status)), el('div', { class: 'meta-row' }, decision.by ? el('span', {}, decision.by) : null, decision.task ? el('span', {}, 'from ', code(decision.task)) : null, decision.at ? el('span', { title: decision.at }, ago(decision.at)) : null, decision.supersedes ? el('span', {}, 'supersedes ', code(decision.supersedes)) : null), field('Context', decision.context), field('Decision', decision.decision), field('Consequences', decision.consequences), decision.reason ? field('Reason given', decision.reason) : null, decision.status === 'proposed' ? ruling(decision) : null);
}
function field(label, value) {
    if (!value)
        return null;
    return el('div', { class: 'field' }, el('h4', {}, label), el('p', { class: 'prose' }, value));
}
function ruling(decision) {
    return el('div', { class: 'ruling' }, el('h4', {}, 'To rule on this'), el('pre', { class: 'command' }, `writ set ${decision.id} active\nwrit set ${decision.id} rejected --reason "..."`));
}

// ---- views/milestones.js ----
/**
 * Milestones with their tasks: the plan's own structure.
 *
 * A milestone is how the design document was divided, so this is the view that
 * answers "how far through the plan are we" rather than "what is running". Tasks
 * are shown in id order, not sorted by status, because within a milestone the
 * numbering is the intended sequence — and a milestone whose tasks jumped around
 * as agents worked would be unreadable as a plan.
 *
 * The header is a grid rather than a wrapping flex line. Everything after the
 * title used to wrap under it at narrow widths, so the count and the status
 * pill ended up in a second row that looked like content.
 */
function renderMilestones(host, milestones, tasks, handlers) {
    if (!milestones.length) {
        host.replaceChildren(el('p', { class: 'empty' }, 'No milestones yet.'));
        return;
    }
    host.replaceChildren(...milestones.map((milestone) => milestoneCard(milestone, tasks, handlers)));
}
function milestoneCard(milestone, tasks, handlers) {
    const own = tasks
        .filter((task) => task.milestone === milestone.id)
        .sort((a, b) => a.id.localeCompare(b.id));
    const done = percent(milestone.done, milestone.total);
    const left = milestone.total - milestone.done;
    return el('section', { class: classes('card milestone-card', milestone.status) }, el('header', { class: 'milestone-head' }, el('span', { class: classes('mark', milestone.status) }, mark(milestone.status)), code(milestone.id), el('h2', { class: 'clip' }, milestone.title), el('span', { class: classes('pill', milestone.status) }, milestone.status), el('div', { class: 'milestone-progress' }, el('div', { class: 'meter thin' }, el('div', { class: 'meter-fill', style: `width:${done}%` })), el('span', { class: 'muted mono tnum' }, `${ratio(milestone.done, milestone.total)}`))), own.length
        ? el('ul', { class: 'task-list compact' }, ...own.map((task) => milestoneTaskRow(task, handlers)))
        : el('p', { class: 'blank' }, 'No tasks in this milestone.'), left
        ? el('p', { class: 'milestone-foot muted small' }, `${plural(left, 'task')} left`)
        : null);
}
function milestoneTaskRow(task, handlers) {
    const row = el('li', {
        class: classes('task-row', task.status, 'clickable'),
        tabindex: 0,
        role: 'button',
        // See tasks.ts: names the detail this row opens, so dismissing the drawer
        // can return focus to it after the list has been re-rendered.
        'data-opens': `task:${task.id}`,
    }, el('span', { class: classes('mark', task.status) }, mark(task.status)), code(task.id), el('span', { class: 'title clip' }, task.title), el('span', { class: 'grow' }), task.blocked_by.length
        ? el('span', { class: 'muted small', title: `waiting on ${task.blocked_by.join(', ')}` }, `waits on ${task.blocked_by.length}`)
        : null, el('span', { class: 'muted mono tnum' }, ratio(task.passed, task.total)), el('span', { class: classes('pill', task.status) }, task.status));
    row.addEventListener('click', () => handlers.onTask(task.id));
    row.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            handlers.onTask(task.id);
        }
    });
    return row;
}

// ---- views/plan.js ----
/**
 * The plan as a reviewed artifact: its status, what stands against it, and what
 * every requirement has to show.
 *
 * Three records that are one question. The plan's status says whether work may
 * start; the findings say why not; the coverage matrix says whether what is being
 * built is what was asked for. Reading them apart is how a plan gets approved with
 * a requirement nobody implemented — the status looked fine on its own.
 *
 * Read-only, like the rest of the dashboard. A finding is disposed of with a reason
 * attached, and a reason is something you type deliberately, so this shows the
 * command rather than offering a button.
 */
/** Which findings a reader is shown first. */
const FINDING_FILTERS = {
    open: (f) => f.disposition === 'open',
    blocking: (f) => f.disposition === 'open' && f.severity === 'error',
    answered: (f) => f.disposition !== 'open',
    all: () => true,
};
function renderPlan(host, plan, findings, coverage, repairs, options, handlers = {}) {
    const shown = findings.filter(FINDING_FILTERS[options.filter] ?? FINDING_FILTERS.open);
    const pipeline = plan.pipeline;
    const phase = options.phase;
    const watched = isPhase(phase);
    replace(host, 
    // First, because it comes first in time. The status card says whether work may
    // start; this says whether the thing being judged has finished being built —
    // and while planning is running it is the only card with news.
    watched ? phaseCard(phase, options.step ?? null, handlers) : null, statusCard(plan), plan.held_gates.length ? heldCard(plan, handlers) : null, 
    // Above the findings: the findings say what is wrong with the plan, and this
    // says what the plan was derived from. A reader deciding whether to trust a
    // finding about coverage wants to know whether a requirements stage ran at all.
    pipeline && pipeline.plan_id ? pipelineCard(pipeline, !watched) : null, findingsCard(shown, findings, options.filter), coverage.length ? coverageCard(coverage, handlers) : null, repairs.length ? repairsCard(repairs, handlers) : null);
}
/** The phase graph, in its own card so the Plan page stays a stack of cards. */
function phaseCard(phase, step, handlers) {
    const card = el('section', { class: classes('card', 'phase-card', phase.status === 'failed' && 'urgent') });
    const forward = { onStep: (id) => handlers.onStep?.(id) };
    renderPhase(card, phase, step, forward);
    return card;
}
/** How each stage ended, as a mark and a word. */
const STAGE_MARKS = {
    ok: '✓',
    reused: '·',
    failed: '✗',
    pending: '○',
};
/**
 * The staged pipeline the plan came out of.
 *
 * The plan's own status card says whether work may start. This says what the plan
 * rests on, which is a different question and the one that explains a finding: a
 * pipeline that stopped at `inventory` produced no verification artifact, so
 * nothing worked out how any requirement would be demonstrated, and every
 * acceptance bar in the plan is the synthesizer's own invention.
 *
 * The baseline gets its own line for the reason the API comments on: a suite that
 * was already failing when planning started will be blamed on whichever task first
 * runs into it.
 */
function pipelineCard(pipeline, withStages) {
    const failed = pipeline.stage_rows.filter((stage) => stage.state === 'failed');
    const pending = pipeline.stage_rows.filter((stage) => stage.state === 'pending');
    const stopped = failed.length > 0 || pending.length > 0;
    const baseline = pipeline.baseline;
    return el('section', { class: classes('card', failed.length > 0 && 'urgent') }, el('header', { class: 'plan-head' }, el('h2', {}, 'Pipeline'), code(pipeline.plan_id), pipeline.at ? el('span', { class: 'muted small', title: pipeline.at }, ago(pipeline.at)) : null), el('p', { class: 'muted small' }, stopped
        ? 'The plan does not rest on every analysis: what is missing was never established.'
        : 'Each analysis ran and the synthesized plan was checked against all of them.'), 
    // Only when there is no phase graph above. The graph draws the same steps and
    // draws them live, so showing both would put two accounts of one pipeline on
    // one page — and the reader would have to work out that they agree. Plans made
    // by an older writ, which kept no phase record, still get the list.
    withStages ? el('ol', { class: 'stage-list' }, ...pipeline.stage_rows.map(stageRow)) : null, el('div', { class: 'meta-row' }, el('span', {}, `${plural(pipeline.requirements, 'requirement')} inventoried`), pipeline.unresolved_ambiguities
        ? el('span', { class: 'count warn' }, el('b', {}, String(pipeline.unresolved_ambiguities)), el('span', { class: 'label' }, 'open questions'))
        : null, 
    // A requirement nothing can demonstrate will be signed off on an agent's word
    // and nothing else, which is worth a reader's attention before approval.
    pipeline.undemonstrable.length
        ? el('span', { class: 'count bad' }, el('b', {}, String(pipeline.undemonstrable.length)), el('span', { class: 'label' }, 'undemonstrable'))
        : null), pipeline.undemonstrable.length
        ? el('p', { class: 'muted small' }, 'no way to prove: ', ...pipeline.undemonstrable.map(code))
        : null, baselineRow(baseline), pipeline.directory ? el('pre', { class: 'command' }, pipeline.directory) : null);
}
function stageRow(stage) {
    return el('li', { class: classes('stage', stage.state) }, el('header', {}, el('span', { class: 'stage-mark' }, STAGE_MARKS[stage.state]), el('b', {}, stage.name), el('span', { class: classes('pill', stage.state) }, stage.state), stage.artifact ? code(stage.artifact) : null, stage.at ? el('span', { class: 'muted small', title: stage.at }, ago(stage.at)) : null), el('p', { class: 'muted small' }, stage.summary), stage.error ? el('p', { class: 'prose error' }, stage.error) : null);
}
/**
 * What the repository's own suite did before any of this work started.
 *
 * `unknown` is its own case rather than being folded into a failure: an inventory
 * stage that did not report a baseline is a gap in the analysis, not a red suite.
 */
function baselineRow(baseline) {
    if (!baseline || (!baseline.status && !baseline.commands.length))
        return null;
    const bad = baseline.status === 'fail';
    return el('div', { class: classes('baseline', bad && 'bad') }, el('header', {}, el('b', {}, 'Baseline'), el('span', { class: classes('pill', bad ? 'failed' : baseline.status === 'pass' ? 'completed' : 'planned') }, baseline.status || 'unknown'), bad
        ? el('span', { class: 'muted small' }, 'the suite was already failing when planning started')
        : null), baseline.commands.length
        ? el('div', { class: 'meta-row' }, el('span', {}, 'ran '), ...baseline.commands.map(code))
        : null, baseline.known_failures.length
        ? el('p', { class: 'muted small' }, `${plural(baseline.known_failures.length, 'known failure')}: `, ...baseline.known_failures.slice(0, 6).map(code), baseline.known_failures.length > 6 ? el('span', {}, ' …') : null)
        : null);
}
function statusCard(plan) {
    return el('section', { class: classes('card', !plan.runnable && 'urgent') }, el('header', { class: 'plan-head' }, el('h2', {}, 'Plan'), el('span', { class: classes('pill', plan.status) }, plan.status), el('span', { class: 'muted small' }, `revision ${plan.revision}`)), el('p', { class: 'muted' }, plan.runnable
        ? 'Approved. `writ run` will dispatch work under this plan.'
        : 'Not approved: `writ run` will refuse to start.'), el('div', { class: 'meta-row' }, el('span', {}, `${plural(plan.blocking, 'blocking finding')}`), el('span', {}, `${plan.advisory} advisory`), el('span', {}, `${plural(plan.requirements, 'requirement')}`), plan.uncovered.length
        ? el('span', { class: 'count bad' }, el('b', {}, String(plan.uncovered.length)), el('span', { class: 'label' }, 'uncovered'))
        : null, plan.open_repairs.length ? el('span', {}, `${plural(plan.open_repairs.length, 'open repair')}`) : null), plan.approved_by
        ? el('div', { class: 'meta-row' }, el('span', {}, `approved by ${plan.approved_by}`), plan.approved_at ? el('span', { title: plan.approved_at }, ago(plan.approved_at)) : null, 
        // An approval that overruled findings is the one a later reader most
        // needs to see, so it is a badge rather than a line of prose.
        plan.forced ? el('span', { class: 'pill forced' }, 'forced') : null)
        : null, plan.approval_note ? el('p', { class: 'prose' }, plan.approval_note) : null, !plan.runnable && !plan.blocking
        ? el('div', { class: 'ruling' }, el('h4', {}, 'Nothing blocking is open'), el('p', { class: 'muted small' }, 'The status comes from a check, so re-check it to approve on that basis.'), el('pre', { class: 'command' }, 'writ check'))
        : null);
}
/**
 * Gates parked on a human.
 *
 * First card on the page when it is present, because it is the one state where
 * nothing is running, nothing is broken, and nothing will change until a person
 * acts. `writ run` reports these as waiting; a dashboard that showed them the same
 * way it shows a running task would leave a project silently stopped.
 */
function heldCard(plan, handlers) {
    return el('section', { class: 'card urgent' }, el('h2', {}, `Held for a human (${plan.held_gates.length})`), el('p', { class: 'muted small' }, 'These gates will not re-run on their own. The work behind them is not broken — it is parked.'), el('div', { class: 'held-grid' }, ...plan.held_gates.map((gate) => el('article', { class: 'held' }, el('header', {}, taskLink(gate.id, handlers), el('span', { class: 'pill needs-repair' }, gate.reason)), el('pre', { class: 'command' }, `writ show ${gate.id}`)))));
}
function findingsCard(shown, all, filter) {
    const counts = Object.fromEntries(Object.entries(FINDING_FILTERS).map(([name, test]) => [name, all.filter(test).length]));
    return el('section', { class: 'card' }, el('h2', {}, 'Findings'), el('div', { class: 'meta-row' }, ...Object.keys(FINDING_FILTERS).map((name) => el('span', { class: classes('count', name === filter && 'selected', name === 'blocking' && counts[name] > 0 && 'bad') }, el('b', {}, String(counts[name])), el('span', { class: 'label' }, name)))), shown.length
        ? el('div', { class: 'finding-list' }, ...shown.map(findingRow))
        : el('p', { class: 'empty' }, all.length
            ? 'Nothing in this view.'
            : 'No findings. Run `writ check` for the structural ones, `writ critique` for the read.'));
}
function findingRow(finding) {
    const open = finding.disposition === 'open';
    return el('article', { class: classes('finding', finding.severity, !open && 'answered') }, el('header', {}, code(finding.id), el('span', { class: classes('pill', finding.severity) }, finding.severity), finding.where ? code(finding.where) : null, el('span', { class: 'muted small' }, finding.category), 
    // Who raised it. `writ` is a deterministic check, `critic:coverage` an
    // independent reader, `gate:G-M01` the milestone's own review — different
    // kinds of claim, and the reader weighs them differently.
    el('span', { class: 'muted small' }, finding.source), !open ? el('span', { class: classes('pill', finding.disposition) }, finding.disposition) : null), el('p', { class: 'prose' }, finding.message), finding.suggested_action ? el('p', { class: 'muted small' }, '→ ', finding.suggested_action) : null, finding.reason ? el('p', { class: 'muted small' }, `${finding.disposition}: ${finding.reason}`) : null, open && finding.severity === 'error'
        ? el('pre', { class: 'command' }, `writ set ${finding.id} accepted --reason "..."\nwrit set ${finding.id} declined --reason "..."`)
        : null);
}
function coverageCard(coverage, handlers) {
    const holes = coverage.filter((row) => row.state === 'uncovered' || row.state === 'unevidenced');
    return el('section', { class: classes('card', holes.length > 0 && 'urgent') }, el('h2', {}, 'Requirements'), el('p', { class: 'muted small' }, holes.length
        ? `${plural(holes.length, 'requirement')} with nothing to show for it.`
        : 'Every requirement has a task and a way of being verified.'), el('div', { class: 'coverage-list' }, ...coverage.map((row) => coverageRow(row, handlers))));
}
function coverageRow(row, handlers) {
    const total = row.tasks.length;
    return el('article', { class: classes('coverage', row.state) }, el('header', {}, code(row.id), el('span', { class: classes('pill', row.priority) }, row.priority), row.declared !== 'planned' ? el('span', { class: classes('pill', row.declared) }, row.declared) : null, el('span', { class: classes('pill', row.state) }, row.state)), el('p', { class: 'prose' }, row.text), el('div', { class: 'meta-row' }, total
        ? el('span', {}, `${row.complete}/${total} complete `, ...row.tasks.map((id) => taskLink(id, handlers)))
        : el('span', { class: 'count bad' }, el('b', {}, '0'), el('span', { class: 'label' }, 'tasks')), row.gates.length ? el('span', {}, 'gates ', ...row.gates.map((id) => taskLink(id, handlers))) : null), row.reason ? el('p', { class: 'muted small' }, row.reason) : null, total ? meter(row.complete, total) : null);
}
function repairsCard(repairs, handlers) {
    return el('section', { class: 'card' }, el('h2', {}, 'Repairs'), el('p', { class: 'muted small' }, 'Every time a gate has asked for the plan itself to change.'), el('div', { class: 'repair-list' }, ...repairs.map((request) => el('article', { class: classes('repair', request.status) }, el('header', {}, code(request.id), el('span', { class: classes('pill', request.status) }, request.status), el('span', {}, 'from ', taskLink(request.gate, handlers)), el('span', { class: 'muted small' }, `round ${request.round}`), 
    // A refused patch is writ turning down what the planner proposed. Two of
    // them is why a gate is held, so the count belongs on the row.
    request.refusals
        ? el('span', { class: 'count bad' }, el('b', {}, String(request.refusals)), el('span', { class: 'label' }, 'refused'))
        : null), el('p', { class: 'prose' }, request.summary), el('div', { class: 'meta-row' }, request.findings.length ? el('span', {}, 'closes ', ...request.findings.map(code)) : null, request.applied_tasks.length ? el('span', {}, 'added ', ...request.applied_tasks.map((id) => taskLink(id, handlers))) : null, request.opened_at ? el('span', { title: request.opened_at }, ago(request.opened_at)) : null)))));
}
function meter(done, total) {
    const pct = percent(done, total);
    return el('div', { class: 'meter', role: 'img', 'aria-label': `${done} of ${total} tasks complete` }, el('div', { class: 'meter-fill', style: `width:${pct}%` }));
}
function taskLink(id, handlers) {
    if (!handlers.onTask)
        return code(id);
    const button = el('button', { class: 'id-link', type: 'button' }, id);
    button.addEventListener('click', () => handlers.onTask?.(id));
    return button;
}

// ---- app.js ----
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
function dismissesOnClick(context) {
    if (context.openKey === null)
        return false;
    if (context.insideDrawer)
        return false;
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
function dismissesOnFocus(context) {
    if (context.openKey === null)
        return false;
    return context.movedTo === 'outside';
}
const VIEWS = [
    { name: 'overview', label: 'Overview' },
    { name: 'plan', label: 'Plan' },
    { name: 'tasks', label: 'Tasks' },
    { name: 'milestones', label: 'Milestones' },
    { name: 'runs', label: 'Runs' },
    { name: 'decisions', label: 'Decisions' },
];
/** How often a watched step's output is re-fetched while it is still running. */
const OUTPUT_POLL_MS = 1000;
class App {
    store = new Store();
    route = { view: 'overview' };
    taskFilter = 'all';
    runFilter = 'all';
    // Findings default to `open`: the ones already answered are history, and the
    // question this view exists to answer is what stands against the plan now.
    findingFilter = 'open';
    query = '';
    nav = el('nav', { class: 'tabs', role: 'tablist' });
    counts = el('div', { class: 'header-counts' });
    conn = el('div', { class: 'conn', title: 'connection to writ serve' });
    body = el('main', { class: 'body' });
    drawer = el('aside', { class: 'drawer', 'aria-live': 'polite' });
    /** What was open when the current click began; see `dismissesOnClick`. */
    keyAtPress = null;
    /** The timer following a live step's output; see `followStep`. */
    outputPoll = null;
    /** The step that timer is following, so a repaint does not restart it. */
    watching = null;
    async start() {
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
    watchDismissal() {
        document.addEventListener('click', () => {
            this.keyAtPress = this.detailKey();
        }, true);
        document.addEventListener('click', (event) => {
            const target = event.target;
            const insideDrawer = target instanceof Node && this.drawer.contains(target);
            if (dismissesOnClick({
                openKey: this.detailKey(),
                keyAtPress: this.keyAtPress,
                insideDrawer,
            })) {
                this.dismiss();
            }
            this.keyAtPress = null;
        });
        this.drawer.addEventListener('focusout', (event) => {
            const next = event.relatedTarget;
            const movedTo = next === null || next === undefined
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
    detailKey() {
        if (this.route.task)
            return `task:${this.route.task}`;
        if (this.route.run)
            return `run:${this.route.run}`;
        if (this.route.step)
            return `step:${this.route.step}`;
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
    dismiss() {
        const key = this.detailKey();
        this.go({ view: this.route.view });
        if (key === null)
            return;
        findOpener(key)?.focus();
    }
    header() {
        for (const view of VIEWS) {
            const button = el('button', { class: 'tab', type: 'button', role: 'tab' }, view.label);
            button.dataset.view = view.name;
            button.addEventListener('click', () => this.go({ view: view.name }));
            this.nav.append(button);
        }
        return el('header', { class: 'top' }, el('div', { class: 'brand' }, el('span', { class: 'wordmark' }, 'writ')), this.nav, this.counts, this.conn);
    }
    go(route) {
        this.route = route;
        location.hash = toHash(route);
        this.render();
    }
    render() {
        const snapshot = this.store.current;
        for (const button of this.nav.querySelectorAll('.tab')) {
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
    scrollOffsets() {
        const saved = new Map();
        for (const pane of this.body.querySelectorAll('[data-scroll-key]')) {
            const key = pane.dataset.scrollKey;
            if (key)
                saved.set(key, [pane.scrollLeft, pane.scrollTop]);
        }
        return saved;
    }
    restoreScroll(saved) {
        if (!saved.size)
            return;
        for (const pane of this.body.querySelectorAll('[data-scroll-key]')) {
            const key = pane.dataset.scrollKey;
            const offset = key ? saved.get(key) : undefined;
            if (!offset)
                continue;
            [pane.scrollLeft, pane.scrollTop] = offset;
        }
    }
    /** The `data-opens` key of the focused row, if a row is what has focus. */
    focusedOpener() {
        const active = document.activeElement;
        if (!(active instanceof HTMLElement))
            return null;
        return active.getAttribute('data-opens');
    }
    /**
     * Put focus back on the row it was on, if rendering dropped it.
     *
     * Only when focus actually fell to the body: if the reader moved it themselves
     * — into the drawer, into the search box — that is where it belongs, and pulling
     * it back would be the page fighting them for it.
     */
    restoreFocus(key) {
        if (key === null)
            return;
        const active = document.activeElement;
        if (active !== null && active !== document.body)
            return;
        findOpener(key)?.focus();
    }
    paintCounts(snapshot) {
        const { overview } = snapshot;
        const parts = [
            el('span', { class: 'count' }, el('b', {}, `${overview.completed}/${overview.tasks}`), el('span', { class: 'label' }, 'done')),
        ];
        if (overview.live) {
            parts.push(el('span', { class: 'count live' }, el('span', { class: 'spinner', 'aria-hidden': 'true' }), el('b', {}, String(overview.live)), el('span', { class: 'label' }, 'running')));
        }
        const waiting = overview.counts['awaiting-review'] ?? 0;
        if (waiting) {
            parts.push(el('span', { class: 'count warn' }, el('b', {}, String(waiting)), el('span', { class: 'label' }, 'to review')));
        }
        if (overview.proposed_decisions) {
            const button = el('button', { class: 'count warn as-button', type: 'button' }, el('b', {}, String(overview.proposed_decisions)), el('span', { class: 'label' }, 'decisions'));
            button.addEventListener('click', () => this.go({ view: 'decisions' }));
            parts.push(button);
        }
        const failed = overview.counts.failed ?? 0;
        if (failed) {
            parts.push(el('span', { class: 'count bad' }, el('b', {}, String(failed)), el('span', { class: 'label' }, 'failed')));
        }
        this.counts.replaceChildren(...parts);
    }
    paintView(snapshot) {
        const handlers = {
            onTask: (id) => this.go({ view: this.route.view, task: id }),
            onRun: (id) => this.go({ view: this.route.view, run: id }),
            onGoto: (view) => this.go({ view: view }),
            onSelect: (id) => this.go({ view: this.route.view, task: id }),
            onStep: (id) => this.go({ view: this.route.view, step: id }),
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
            case 'tasks': {
                // The graph above the list, on one page: the graph answers what can run
                // and what waits on what, the list answers everything else about the same
                // tasks, and one selection opens the same drawer from either.
                const graph = el('div', { class: 'graph-holder', 'data-scroll-key': 'graph' });
                renderGraph(graph, snapshot.graph, this.route.task ?? null, {
                    onSelect: handlers.onSelect,
                });
                const list = el('div', { class: 'list-holder' });
                renderTaskList(list, snapshot.tasks, {
                    filter: this.taskFilter,
                    query: this.query,
                    selected: this.route.task ?? null,
                }, { onSelect: handlers.onSelect, onRun: handlers.onRun });
                this.body.replaceChildren(el('section', { class: 'task-graph', 'aria-label': 'dependency graph' }, el('div', { class: 'muted small graph-caption' }, `${plural(snapshot.graph.nodes.length, 'task')} · ${snapshot.graph.levels} levels deep · a column can run at once`), graph), this.toolbar(this.filterBar(Object.keys(FILTERS), this.taskFilter, (name) => {
                    this.taskFilter = name;
                    this.render();
                }), this.search()), list);
                fitTitles(graph);
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
                this.body.replaceChildren(this.toolbar(this.filterBar(Object.keys(RUN_FILTERS), this.runFilter, (name) => {
                    this.runFilter = name;
                    this.render();
                }), el('span', { class: 'muted small' }, `${plural(snapshot.runs.length, 'run')}, newest first`)), list);
                break;
            }
            case 'plan': {
                const holder = el('div', { class: 'grid one' });
                renderPlan(holder, snapshot.overview.plan, snapshot.findings, snapshot.coverage, snapshot.repairs, { filter: this.findingFilter, phase: snapshot.phase, step: this.route.step ?? null }, { onTask: handlers.onTask, onStep: handlers.onStep });
                this.body.replaceChildren(this.toolbar(this.filterBar(Object.keys(FINDING_FILTERS), this.findingFilter, (name) => {
                    this.findingFilter = name;
                    this.render();
                }), el('span', { class: 'muted small' }, `revision ${snapshot.overview.plan.revision}`)), holder);
                // After it is in the document: `getComputedTextLength` is zero for an SVG
                // that has not been laid out, so clipping before the swap would measure
                // nothing and clip nothing.
                fitStepTitles(holder);
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
    toolbar(...children) {
        return el('div', { class: 'toolbar' }, ...children.filter(Boolean));
    }
    filterBar(names, active, pick) {
        const bar = el('div', { class: 'filters', role: 'group' });
        for (const name of names) {
            const button = el('button', { class: classes('filter', name === active && 'active'), type: 'button' }, name);
            button.addEventListener('click', () => pick(name));
            bar.append(button);
        }
        return bar;
    }
    search() {
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
            const fresh = this.body.querySelector('.search');
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
    paintDrawer() {
        const { task, run, step } = this.route;
        if (!task && !run && !step) {
            this.stopFollowing();
            this.drawer.classList.remove('open');
            this.drawer.replaceChildren();
            return;
        }
        if (!step)
            this.stopFollowing();
        this.drawer.classList.add('open');
        if (!this.drawer.querySelector('.detail-head')) {
            this.drawer.replaceChildren(el('p', { class: 'muted' }, 'Loading…'));
        }
        const close = el('button', { class: 'close', type: 'button', 'aria-label': 'close' }, '×');
        close.addEventListener('click', () => this.dismiss());
        const handlers = {
            onSelect: (id) => this.go({ view: this.route.view, task: id }),
            onRun: (id) => this.go({ view: this.route.view, run: id }),
            onTask: (id) => this.go({ view: this.route.view, task: id }),
        };
        if (step) {
            this.paintStep(step, close);
            return;
        }
        if (run) {
            void this.store
                .run(run)
                .then((detail) => {
                if (this.route.run !== run)
                    return; // the reader moved on while fetching
                const holder = el('div', { class: 'detail' });
                renderRunDetail(holder, detail, handlers);
                this.drawer.replaceChildren(close, holder);
            })
                .catch((error) => this.drawerError(close, error));
            return;
        }
        void this.store
            .task(task)
            .then((detail) => {
            if (this.route.task !== task)
                return;
            const holder = el('div', { class: 'detail' });
            renderTaskDetail(holder, detail, handlers);
            this.drawer.replaceChildren(close, holder);
        })
            .catch((error) => this.drawerError(close, error));
    }
    /**
     * A planning step: its record from the snapshot, its output from a poll.
     *
     * The record is already in hand — the phase graph was drawn from it — so the
     * panel paints immediately and the output fills in. That matters for a running
     * step: waiting on the fetch would leave the drawer saying "Loading…" for a
     * second every time a snapshot arrived, which during planning is constantly.
     */
    paintStep(id, close) {
        const phase = this.store.current?.phase;
        const found = isPhase(phase) ? phase.steps.find((entry) => entry.id === id) : undefined;
        if (!found) {
            // The step is not on the current phase — an older attempt's link, or a
            // record that has since been trimmed.
            this.stopFollowing();
            this.drawer.replaceChildren(close, el('p', { class: 'muted' }, 'That step is not part of the most recent planning attempt.'));
            return;
        }
        if (this.watching !== found.id || this.outputPoll === null) {
            // First paint, or one whose poll has stopped: show the record now and let
            // the output arrive. Re-rendering a step already being followed would throw
            // away output that is on screen and replace it with "Loading…".
            const holder = el('div', { class: 'detail' });
            renderStepDetail(holder, found, null);
            this.drawer.replaceChildren(close, holder);
        }
        this.followStep(found.id, found.status === 'running');
    }
    /**
     * Fetch a step's output, and keep fetching while it is still running.
     *
     * Polled rather than pushed, and only while the drawer is open on that step.
     * `store.stepOutput` says why at length: a transcript grows without `state.json`
     * moving, so the snapshot stream never learns there is more of it, and a second
     * EventSource would take one of the handful of connections the browser allows.
     */
    followStep(id, live) {
        // Already following it, so leave the timer alone. Snapshots arrive every few
        // tenths of a second during planning and each one repaints the drawer; a
        // restart per snapshot would fetch far more often than the interval says, and
        // the interval itself would never get to fire.
        if (live && this.watching === id && this.outputPoll !== null)
            return;
        this.stopFollowing();
        this.watching = id;
        const paint = () => {
            void this.store
                .stepOutput(id)
                .then((output) => {
                if (this.route.step !== id)
                    return; // the reader moved on while fetching
                const phase = this.store.current?.phase;
                const found = isPhase(phase) ? phase.steps.find((entry) => entry.id === id) : undefined;
                if (!found)
                    return;
                const holder = el('div', { class: 'detail' });
                renderStepDetail(holder, found, output);
                const close = el('button', { class: 'close', type: 'button', 'aria-label': 'close' }, '×');
                close.addEventListener('click', () => this.dismiss());
                const scrolled = this.drawerOffsets();
                this.drawer.replaceChildren(close, holder);
                this.restoreDrawerScroll(scrolled);
                // Stop when the step does. A finished step's transcript is fixed, so
                // polling it further would be asking the same question forever.
                if (found.status !== 'running')
                    this.stopFollowing();
            })
                .catch(() => undefined);
        };
        paint();
        if (live)
            this.outputPoll = window.setInterval(paint, OUTPUT_POLL_MS);
    }
    stopFollowing() {
        this.watching = null;
        if (this.outputPoll === null)
            return;
        window.clearInterval(this.outputPoll);
        this.outputPoll = null;
    }
    /**
     * Where the drawer's log panes are scrolled, so a poll does not rewind them.
     *
     * Same problem as the graph's offsets, and worse here: replacing the pane every
     * second would throw a reader back to the top of an agent's output every second,
     * which is exactly while they are reading it.
     */
    drawerOffsets() {
        const saved = new Map();
        for (const pane of this.drawer.querySelectorAll('[data-scroll-key]')) {
            const key = pane.dataset.scrollKey;
            if (key)
                saved.set(key, [pane.scrollLeft, pane.scrollTop]);
        }
        return saved;
    }
    restoreDrawerScroll(saved) {
        for (const pane of this.drawer.querySelectorAll('[data-scroll-key]')) {
            const key = pane.dataset.scrollKey;
            const offset = key ? saved.get(key) : undefined;
            if (offset) {
                [pane.scrollLeft, pane.scrollTop] = offset;
                continue;
            }
            // A pane that was not there before starts at the tail, which for a live
            // agent's output is the part worth reading.
            pane.scrollTop = pane.scrollHeight;
        }
    }
    drawerError(close, error) {
        this.drawer.replaceChildren(close, el('p', { class: 'error' }, `Could not load: ${String(error)}`));
    }
    paintConnection(state) {
        this.conn.className = classes('conn', state);
        this.conn.replaceChildren(el('span', { class: 'dot', 'aria-hidden': 'true' }), 
        // A paused tab is not a broken one: say why it stopped following.
        el('span', {}, state === 'paused' ? 'paused (tab hidden)' : state));
    }
    onKey(event) {
        if (event.target instanceof HTMLInputElement)
            return;
        if (event.key === 'Escape' && (this.route.task || this.route.run || this.route.step)) {
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
function findOpener(key) {
    return document.querySelector(`[data-opens="${CSS.escape(key)}"]`);
}
function parseHash(hash) {
    const clean = hash.replace(/^#\/?/, '');
    if (!clean)
        return { view: 'overview' };
    const [view, kind, id] = clean.split('/');
    // `graph` was its own page before it moved above the task list; old links
    // still land on the graph.
    const named = view === 'graph' ? 'tasks' : view;
    const known = VIEWS.some((v) => v.name === named) ? named : 'overview';
    if (kind === 'task' && id)
        return { view: known, task: decodeURIComponent(id) };
    if (kind === 'run' && id)
        return { view: known, run: decodeURIComponent(id) };
    if (kind === 'step' && id)
        return { view: known, step: decodeURIComponent(id) };
    return { view: known };
}
function toHash(route) {
    if (route.task)
        return `#/${route.view}/task/${encodeURIComponent(route.task)}`;
    if (route.run)
        return `#/${route.view}/run/${encodeURIComponent(route.run)}`;
    if (route.step)
        return `#/${route.view}/step/${encodeURIComponent(route.step)}`;
    return `#/${route.view}`;
}
void new App().start();
// Referenced so the marks and live helper are part of the bundle's public shape
// for tests that assert the vocabulary matches the terminal's.

})();
