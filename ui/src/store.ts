/**
 * The client's connection to the server: one snapshot, pushed on every change.
 *
 * The server sends whole snapshots rather than diffs. A diff protocol would save
 * bandwidth this app does not care about and cost a class of bug it very much
 * does: a page that has applied nine of ten deltas is subtly wrong with no way
 * to notice. A whole snapshot is idempotent, so a missed message is corrected by
 * the next one, and a reconnect needs no replay.
 */

import type { Run, Snapshot, StepOutput, Task } from './types.js';

type Listener = (snapshot: Snapshot) => void;
type StateListener = (state: ConnectionState) => void;

export type ConnectionState = 'connecting' | 'live' | 'reconnecting' | 'paused';

const RETRY_MS = 1500;

export class Store {
  private snapshot: Snapshot | null = null;
  private listeners = new Set<Listener>();
  private stateListeners = new Set<StateListener>();
  private source: EventSource | null = null;
  private state: ConnectionState = 'connecting';
  private retry: number | null = null;
  private stopped = false;

  get current(): Snapshot | null {
    return this.snapshot;
  }

  get connection(): ConnectionState {
    return this.state;
  }

  onSnapshot(listener: Listener): void {
    this.listeners.add(listener);
  }

  onConnection(listener: StateListener): void {
    this.stateListeners.add(listener);
  }

  /**
   * Load once, then follow.
   *
   * The initial fetch matters: on a quiet project no change is coming, and a page
   * that only listened would sit empty until someone happened to run something.
   */
  async start(): Promise<void> {
    try {
      this.apply(await this.fetchJson<Snapshot>('api/snapshot'));
    } catch {
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
  private watchVisibility(): void {
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        this.stopped = true;
        this.close();
        this.setState('paused');
        return;
      }
      this.stopped = false;
      void this.fetchJson<Snapshot>('api/snapshot')
        .then((snapshot) => this.apply(snapshot))
        .catch(() => undefined)
        .finally(() => this.listen());
    });
  }

  private close(): void {
    if (this.retry !== null) {
      window.clearTimeout(this.retry);
      this.retry = null;
    }
    this.source?.close();
    this.source = null;
  }

  private listen(): void {
    if (this.stopped) return;
    this.close();
    this.source = new EventSource('events');
    this.source.addEventListener('snapshot', (event) => {
      this.setState('live');
      this.apply(JSON.parse((event as MessageEvent<string>).data) as Snapshot);
    });
    this.source.addEventListener('ping', () => this.setState('live'));
    this.source.onopen = () => this.setState('live');
    this.source.onerror = () => {
      if (this.stopped) return;
      this.setState('reconnecting');
      this.close();
      // The usual cause is the server restarting, so retry rather than asking
      // the reader to reload a page that can fix itself.
      this.retry = window.setTimeout(() => this.listen(), RETRY_MS);
    };
  }

  private apply(snapshot: Snapshot): void {
    this.snapshot = snapshot;
    for (const listener of this.listeners) listener(snapshot);
  }

  private setState(state: ConnectionState): void {
    if (this.state === state) return;
    this.state = state;
    for (const listener of this.stateListeners) listener(state);
  }

  /**
   * Details are fetched on demand rather than pushed.
   *
   * A task's evidence and a run's prompt and logs are large and only interesting
   * when opened. Pushing them on every change would make the snapshot enormous
   * to keep panels current that nobody is looking at.
   */
  task(id: string): Promise<Task> {
    return this.fetchJson<Task>(`api/task/${encodeURIComponent(id)}`);
  }

  run(id: string): Promise<Run> {
    return this.fetchJson<Run>(`api/run/${encodeURIComponent(id)}`);
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
  stepOutput(id: string): Promise<StepOutput> {
    return this.fetchJson<StepOutput>(`api/phase/step/${encodeURIComponent(id)}`);
  }

  private async fetchJson<T>(path: string): Promise<T> {
    const response = await fetch(path, { headers: { accept: 'application/json' } });
    if (!response.ok) {
      throw new Error(`${path}: ${response.status} ${response.statusText}`);
    }
    return (await response.json()) as T;
  }
}
