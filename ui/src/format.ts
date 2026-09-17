/**
 * Presentation of values that appear in more than one view.
 *
 * These exist so a status looks the same everywhere, and so durations and times
 * are formatted once. Every function here is pure, which is also what makes them
 * testable without a DOM.
 */

import type { TaskStatus } from './types.js';

/**
 * Status marks, the same ones the terminal uses.
 *
 * Matching `render.STATUS_MARKS` is deliberate: someone reading the dashboard
 * and someone reading `writ list` should not have to learn two vocabularies.
 */
export const MARKS: Record<string, string> = {
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

export function mark(status: string): string {
  return MARKS[status] ?? '·';
}

/** Statuses that mean an agent is working right now. */
export function isLive(status: string): boolean {
  return status === 'running' || status === 'reviewing' || status === 'starting';
}

/** Seconds as something a human reads at a glance. */
export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '';
  if (seconds < 1) return '<1s';
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  if (minutes < 60) return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

/**
 * A timestamp as elapsed time.
 *
 * Absolute times in a log are hard to read while watching a run: "4m ago" is the
 * question you actually have. The exact value goes in a tooltip.
 */
export function ago(stamp: string | null | undefined, now = Date.now()): string {
  if (!stamp) return '';
  const then = Date.parse(stamp);
  if (Number.isNaN(then)) return '';
  const seconds = Math.max(0, (now - then) / 1000);
  if (seconds < 45) return 'just now';
  if (seconds < 90) return 'a minute ago';
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

export function clock(stamp: string | null | undefined): string {
  if (!stamp) return '';
  const parsed = new Date(stamp);
  if (Number.isNaN(parsed.getTime())) return stamp;
  return parsed.toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

export function clip(text: string, chars: number): string {
  return text.length <= chars ? text : `${text.slice(0, chars - 1)}…`;
}

/** `3/7`, or an empty string when there is nothing to count. */
export function ratio(passed: number, total: number): string {
  return total ? `${passed}/${total}` : '';
}

export function percent(done: number, total: number): number {
  return total ? Math.round((done / total) * 100) : 0;
}

/** The status a milestone or task list should sort to the top. */
const WEIGHT: Record<string, number> = {
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

export function statusWeight(status: TaskStatus | string): number {
  return WEIGHT[status] ?? 9;
}

export function plural(count: number, word: string, suffix = 's'): string {
  return `${count} ${word}${count === 1 ? '' : suffix}`;
}
