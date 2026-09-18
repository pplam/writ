/**
 * The shape of what `writ/api.py` returns.
 *
 * Hand-written rather than generated: the payload is small and stable, and a
 * generator would be another build step to install and keep working. These types
 * are the contract, and `tests/test_api.py` asserts the Python side matches them
 * field by field, so a rename on either side fails a test rather than silently
 * producing an undefined in the browser.
 */

export type TaskStatus =
  | 'planned'
  | 'ready'
  | 'running'
  | 'awaiting-review'
  | 'reviewing'
  | 'completed'
  | 'failed'
  | 'blocked'
  | 'cancelled'
  | 'interrupted';

export type RunStatus =
  | 'starting'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'interrupted';

export type DecisionStatus = 'proposed' | 'active' | 'rejected' | 'superseded';

export interface Throughput {
  runs: number;
  agent_seconds: number;
  median_seconds: number;
  reviews: number;
  rejected: number;
  failures: number;
}

export interface MilestoneRow {
  id: string;
  title: string;
  status: string;
  done: number;
  total: number;
  counts: Partial<Record<TaskStatus, number>>;
}

export interface Milestone extends MilestoneRow {
  tasks: TaskRow[];
  design_section: string;
  notes: string;
}

export interface TaskRow {
  id: string;
  title: string;
  milestone: string;
  status: TaskStatus;
  stored_status: TaskStatus;
  passed: number;
  total: number;
  depends_on: string[];
  /** Dependency ids naming no task: empty unless the store is corrupt. */
  unknown_deps: string[];
  blocked_by: string[];
  blocks: string[];
  runs: number;
}

export interface Acceptance {
  number: number;
  text: string;
  status: 'passed' | 'failed' | 'unmet';
  evidence: string;
  by: string;
  at: string;
}

export interface Evidence {
  text: string;
  actor: string;
  at: string;
}

export interface Task extends TaskRow {
  design_doc: string;
  design_section: string;
  notes: string;
  allowed: string[];
  forbidden: string[];
  acceptances: Acceptance[];
  evidence: Evidence[];
  run_list: RunRow[];
  created_at: string;
  updated_at: string;
}

export interface RunRow {
  id: string;
  task: string;
  role: string;
  status: RunStatus;
  command: string;
  model: string;
  exit_code: number | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  duration: number | null;
  resulting_status: string;
  verdict_error: string;
  /** Set when the agent wrote no verdict at all, which exit 0 does not reveal. */
  no_verdict: string;
  /** True when it also printed nothing, meaning it likely never ran at all. */
  no_output: boolean;
  /** True when the transcript ends in a tool call that was printed, not made. */
  unparsed_tool_call: boolean;
  /** Set when writ lowered a headline claim its own criteria contradicted. */
  verdict_downgraded: string;
  /** Where a verdict was found, when the agent ignored the path it was given. */
  verdict_misplaced: string;
  decision: string;
  summary: string;
  unmet: number[];
  decisions: string[];
  note: string;
}

export interface LogTail {
  text: string;
  bytes: number;
  truncated: boolean;
}

export interface Run extends RunRow {
  dir: string;
  cwd: string;
  timeout: number | null;
  pid: number | null;
  prompt: string;
  stdout: LogTail;
  stderr: LogTail;
  verdict: unknown | null;
  verdict_raw: string;
}

export interface Decision {
  id: string;
  title: string;
  status: DecisionStatus;
  by: string;
  at: string;
  context: string;
  decision: string;
  consequences: string;
  task: string;
  reason: string;
  supersedes: string;
}

export interface GraphNode extends TaskRow {
  x: number;
  y: number;
  column: number;
}

export interface GraphEdge {
  from: string;
  to: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  satisfied: boolean;
}

export interface Graph {
  nodes: GraphNode[];
  edges: GraphEdge[];
  width: number;
  height: number;
  levels: number;
}

export interface ActivityEvent {
  at: string;
  kind: 'run-started' | 'run-finished' | 'decision';
  text: string;
  task: string;
  run?: string;
  decision?: string;
  status: string;
  summary?: string;
  exit_code?: number | null;
}

export interface Overview {
  project: string;
  design_docs: string[];
  counts: Partial<Record<TaskStatus, number>>;
  tasks: number;
  completed: number;
  live: number;
  milestones: MilestoneRow[];
  active_runs: RunRow[];
  proposed_decisions: number;
  throughput: Throughput;
}

/** Everything the server pushes, and everything the app renders from. */
export interface Snapshot {
  overview: Overview;
  milestones: MilestoneRow[];
  tasks: TaskRow[];
  runs: RunRow[];
  decisions: Decision[];
  graph: Graph;
  activity: ActivityEvent[];
  generated_at: string;
}
