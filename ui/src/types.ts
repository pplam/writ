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
  /** `gate` nodes review an integrated outcome and write no code. */
  kind: 'task' | 'gate';
  /** Requirement ids from the plan's inventory that this node answers for. */
  requirement_ids: string[];
  passed: number;
  total: number;
  depends_on: string[];
  /** Dependency ids naming no task: empty unless the store is corrupt. */
  unknown_deps: string[];
  blocked_by: string[];
  blocks: string[];
  runs: number;
  /** Times a reviewer rejected this task and sent it back for another attempt. */
  rework_attempts: number;
  /** A rejection this task has not yet answered: it is queued to be reworked. */
  awaiting_rework: boolean;
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

export interface Rework {
  attempt: number;
  /** The --max-rework the rejecting review ran under. */
  max: number;
  /** Extra attempts granted by an operator requeueing an exhausted task. */
  allowance: number;
  /** max + allowance: what `attempt` is actually measured against. */
  budget: number;
  at: string;
  reviewer: string;
  summary: string;
  notes: string;
  unmet: number[];
  /** What the reviewer objected to, per criterion, in its own words. */
  findings: ReworkFinding[];
  /** What the previous attempt claimed, before the review overwrote it. */
  claimed: ReworkFinding[];
  claimed_by: string;
  claimed_summary: string;
  /** The budget ran out: the task is failed rather than queued. */
  exhausted: boolean;
  resolved_at?: string;
  resolved_by?: string;
  reset_by?: string;
  reset_at?: string;
}

export interface ReworkFinding {
  number: number;
  status: string;
  evidence: string;
}

export interface Task extends TaskRow {
  design_doc: string;
  design_section: string;
  notes: string;
  allowed: string[];
  forbidden: string[];
  /** Why a blocked task stopped. Empty unless the task is blocked. */
  blocked_on: string;
  acceptances: Acceptance[];
  evidence: Evidence[];
  /** The most recent rejection, open or answered. Null if never rejected. */
  rework: Rework | null;
  run_list: RunRow[];
  created_at: string;
  updated_at: string;
  /** A gate's review history. Empty on an implementation task. */
  gate_attempts: GateAttempt[];
  /** Why a gate is waiting rather than deciding. Null unless it is held. */
  held: GateHold | null;
}

export interface GateAttempt {
  at: string;
  decision: 'pass' | 'needs-repair' | 'needs-decision';
  actor: string;
  summary: string;
  /** Finding ids this review recorded. */
  findings: string[];
  /** The plan revision the reviewed code belonged to. */
  revision: number;
}

export interface GateHold {
  reason: 'awaiting-repair' | 'needs-decision' | 'repair-exhausted' | 'repair-refused';
  at: string;
  request?: string;
  detail?: string;
  questions?: string[];
}

/** Where the plan stands as a reviewed artifact, not as a set of tasks. */
export interface Plan {
  status: 'draft' | 'needs-approval' | 'approved' | 'executing' | 'complete';
  revision: number;
  approved_by: string;
  approved_at: string;
  approval_note: string;
  /** Approved with blocking findings outstanding, on the record. */
  forced: boolean;
  runnable: boolean;
  blocking: number;
  advisory: number;
  requirements: number;
  uncovered: string[];
  open_repairs: string[];
  held_gates: HeldGate[];
  /** What the staged pipeline produced, or absent for a single-shot plan. */
  pipeline: Pipeline | Record<string, never>;
}

export interface HeldGate {
  id: string;
  reason: string;
}

/**
 * The staged planning pipeline: the analyses a plan was built on.
 *
 * Empty for a plan from the single-shot planner, which is why every field is
 * optional at the call site — `pipeline.plan_id` is the presence test.
 */
export interface Pipeline {
  plan_id: string;
  directory: string;
  at: string;
  stages: string[];
  stage_rows: PipelineStage[];
  requirements: number;
  ambiguities: number;
  unresolved_ambiguities: number;
  undemonstrable: string[];
  baseline: PipelineBaseline;
}

export interface PipelineStage {
  name: string;
  summary: string;
  /** `pending` means the pipeline never reached it, not that it failed. */
  state: 'ok' | 'reused' | 'failed' | 'pending';
  artifact: string;
  error: string;
  exit_code: number | null;
  at: string;
}

export interface PipelineBaseline {
  /** Whether the suite passed *before* any of this plan's work started. */
  status: string;
  commands: string[];
  known_failures: string[];
}

/** One objection to the plan, from writ's own checks or from a gate. */
export interface Finding {
  id: string;
  severity: 'error' | 'warning' | 'note';
  category: string;
  message: string;
  where: string;
  suggested_action: string;
  requirement_ids: string[];
  /** `writ`, `plan`, or `gate:<id>`. */
  source: string;
  disposition: 'open' | 'accepted' | 'declined' | 'resolved';
  reason: string;
  change: string;
  first_seen_at: string;
}

/** One requirement and what covers it, derived from the current graph. */
export interface Coverage {
  id: string;
  text: string;
  priority: 'must' | 'should' | 'may';
  declared: 'planned' | 'existing' | 'out-of-scope' | 'deferred';
  source: string;
  evidence: string;
  reason: string;
  tasks: string[];
  gates: string[];
  state: string;
  complete: number;
}

/** A gate's request for the plan to change, and what came of it. */
export interface Repair {
  id: string;
  gate: string;
  status: string;
  round: number;
  summary: string;
  findings: string[];
  applied_tasks: string[];
  /** Patches writ turned down for this request. */
  refusals: number;
  opened_at: string;
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
  /**
   * How a run that did not finish was classified. An infrastructure failure — a
   * provider timeout, a spawn that failed, a lock writ could not take — is not a
   * statement about the work: no reviewer read it. Null for a run that finished.
   */
  failure: RunFailure | null;
}

/** See writ/failures.py. */
export interface RunFailure {
  category:
    | 'infrastructure'
    | 'unavailable'
    | 'task'
    | 'rejection'
    | 'blocked'
    | 'internal';
  reason: string;
  /** Whether another attempt could succeed with nothing else changing. */
  retryable: boolean;
  exception?: string;
  where?: string[];
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
  plan: Plan;
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
  findings: Finding[];
  coverage: Coverage[];
  repairs: Repair[];
  generated_at: string;
}
