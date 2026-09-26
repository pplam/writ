/**
 * Element helpers.
 *
 * No framework: this app renders a handful of views from one JSON document, and
 * a framework would be a build-time dependency and a runtime download to save
 * very little. What it would genuinely save is safe interpolation, so that is
 * what these helpers provide — `el` never parses a string as HTML, which means
 * a task title containing `<script>` is text, not markup.
 */

import { duration, elapsed, serverNow } from './format.js';

type Attrs = Record<string, string | number | boolean | undefined>;
type Child = Node | string | null | undefined | false;

export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs?: Attrs,
  ...children: Child[]
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  apply(node, attrs);
  append(node, children);
  return node;
}

const SVG = 'http://www.w3.org/2000/svg';

export function svg(tag: string, attrs?: Attrs, ...children: Child[]): SVGElement {
  const node = document.createElementNS(SVG, tag);
  apply(node as unknown as HTMLElement, attrs);
  append(node as unknown as HTMLElement, children);
  return node;
}

function apply(node: Element, attrs?: Attrs): void {
  if (!attrs) return;
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === false) continue;
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

function append(node: Element, children: Child[]): void {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    node.append(typeof child === 'string' ? document.createTextNode(child) : child);
  }
}

export function clear(node: Element): void {
  node.replaceChildren();
}

/** A short id-ish label, monospaced by class rather than by inline style. */
export function code(text: string): HTMLElement {
  return el('code', {}, text);
}

export function classes(...names: (string | false | undefined)[]): string {
  return names.filter(Boolean).join(' ');
}

/**
 * `replaceChildren` for a list that may contain nulls.
 *
 * Conditional sections read best as `condition ? node : null` inline, but the
 * native method rejects null. This filters, so callers keep the readable form.
 */
export function replace(node: Element, ...children: Child[]): void {
  node.replaceChildren(
    ...children.filter((child): child is Node | string => child !== null && child !== undefined && child !== false),
  );
}

/**
 * Make a row open something on click, Enter or Space.
 *
 * `opens` is the detail key the drawer hands focus back to when it closes; see
 * `findOpener` in app.ts for why it is looked up rather than remembered.
 */
export function activate(node: HTMLElement | SVGElement, opens: string | null, run: () => void): void {
  node.setAttribute('tabindex', '0');
  node.setAttribute('role', 'button');
  if (opens) node.setAttribute('data-opens', opens);
  node.addEventListener('click', run);
  node.addEventListener('keydown', (event) => {
    const key = (event as KeyboardEvent).key;
    if (key === 'Enter' || key === ' ') {
      event.preventDefault();
      run();
    }
  });
}

/**
 * A duration that keeps counting while its run is live.
 *
 * `base` is the seconds already spent, `since` the start of the run still going
 * (or null). The figure is written now and then refreshed by `tickClocks` every
 * second, in place, so a live task's time moves without a snapshot having to
 * arrive — and without a repaint that would cost a reader their scroll or focus.
 */
export function liveClock(base: number | null, since: string | null, empty = '—'): HTMLElement {
  const node = el('span', { class: classes('clock', !!since && 'ticking') });
  if (since) {
    node.dataset.tickBase = String(base ?? 0);
    node.dataset.tickSince = since;
  }
  const seconds = since ? elapsed(base ?? 0, since, serverNow()) : base;
  node.textContent = seconds ? duration(seconds) : since ? '<1s' : empty;
  return node;
}

/** Refresh every live clock under `root`. SVG text works the same way. */
export function tickClocks(root: ParentNode): void {
  const now = serverNow();
  for (const node of root.querySelectorAll<HTMLElement | SVGElement>('[data-tick-since]')) {
    const base = Number(node.dataset.tickBase ?? 0);
    node.textContent = duration(elapsed(base, node.dataset.tickSince, now)) || '<1s';
  }
}
