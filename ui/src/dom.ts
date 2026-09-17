/**
 * Element helpers.
 *
 * No framework: this app renders a handful of views from one JSON document, and
 * a framework would be a build-time dependency and a runtime download to save
 * very little. What it would genuinely save is safe interpolation, so that is
 * what these helpers provide — `el` never parses a string as HTML, which means
 * a task title containing `<script>` is text, not markup.
 */

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
