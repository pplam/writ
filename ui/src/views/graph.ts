/**
 * The DAG, drawn from the geometry the server computed.
 *
 * The layout is deliberately not done here. Depth, column and row come from
 * `api.graph` because they are derived from dependency rules that already exist
 * in Python; recomputing them in TypeScript would be a second implementation of
 * the same rules, free to disagree with the scheduler about what can run.
 */

import { classes, clear, svg } from '../dom.js';
import { isLive, mark, ratio } from '../format.js';
import type { Graph, GraphEdge, GraphNode } from '../types.js';

const W = 200;
const H = 64;

export interface GraphHandlers {
  onSelect(id: string): void;
}

export function renderGraph(
  host: HTMLElement,
  graph: Graph,
  selected: string | null,
  handlers: GraphHandlers,
): void {
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

  for (const edge of graph.edges) canvas.append(drawEdge(edge));
  for (const node of graph.nodes) {
    canvas.append(drawNode(node, node.id === selected, handlers));
  }
  host.append(canvas);
}

function drawEdge(edge: GraphEdge): SVGElement {
  // A cubic curve with horizontal ends: parallel diagonals between columns are
  // hard to follow, and flat ends make it obvious which side of a node an edge
  // leaves and enters.
  const lift = Math.max(30, (edge.x2 - edge.x1) / 2);
  return svg('path', {
    class: classes('edge', edge.satisfied ? 'satisfied' : 'pending'),
    d: `M ${edge.x1} ${edge.y1} C ${edge.x1 + lift} ${edge.y1}, ${edge.x2 - lift} ${edge.y2}, ${edge.x2} ${edge.y2}`,
  });
}

function drawNode(
  node: GraphNode,
  isSelected: boolean,
  handlers: GraphHandlers,
): SVGElement {
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
  group.append(
    svg('text', { class: 'node-count', x: W - 11, y: 19, 'text-anchor': 'end' },
      ratio(node.passed, node.total)),
  );
  group.append(svg('text', { class: 'node-title', x: 11, y: 38 }, node.title));
  group.append(svg('text', { class: 'node-status', x: 11, y: 54 }, node.status));

  if (node.total) {
    group.append(svg('rect', { class: 'track', x: 11, y: H - 7, width: W - 22, height: 3, rx: 1.5 }));
    group.append(
      svg('rect', {
        class: 'fill',
        x: 11,
        y: H - 7,
        width: ((W - 22) * node.passed) / node.total,
        height: 3,
        rx: 1.5,
      }),
    );
  }

  const select = () => handlers.onSelect(node.id);
  group.addEventListener('click', select);
  group.addEventListener('keydown', (event) => {
    const key = (event as KeyboardEvent).key;
    if (key === 'Enter' || key === ' ') {
      event.preventDefault();
      select();
    }
  });
  return group;
}

function tooltip(node: GraphNode): string {
  const lines = [`${node.id}  ${node.title}`, node.status];
  if (node.total) lines.push(`${node.passed}/${node.total} acceptance criteria`);
  if (node.depends_on.length) lines.push(`after: ${node.depends_on.join(', ')}`);
  if (node.blocked_by.length) lines.push(`waiting on: ${node.blocked_by.join(', ')}`);
  if (node.blocks.length) lines.push(`blocks: ${node.blocks.join(', ')}`);
  return lines.join('\n');
}

function emptyState(): HTMLElement {
  const box = document.createElement('div');
  box.className = 'empty';
  box.textContent = 'No tasks yet. Run writ plan to build the graph.';
  return box;
}

/** Long titles are clipped in SVG, which has no text overflow of its own. */
export function fitTitles(host: HTMLElement): void {
  for (const text of host.querySelectorAll<SVGTextElement>('.node-title')) {
    const limit = W - 22;
    let content = text.textContent ?? '';
    while (content.length > 1 && text.getComputedTextLength() > limit) {
      content = content.slice(0, -2);
      text.textContent = `${content}…`;
    }
  }
}
