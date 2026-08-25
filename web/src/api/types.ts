// Shapes returned by app/api/main.py. Kept narrow on purpose: every field here
// exists in a response the backend actually sends.

export type TaskStatus =
  | "RUNNING"
  | "AWAITING_APPROVAL"
  | "COMPLETED"
  | "REJECTED"
  | "FAILED"
  | "ABORTED_BUDGET";

export type VerificationState = "SUCCESS" | "FAILED" | "PARTIAL" | "UNKNOWN";

export interface Finding {
  claim: string;
  kind: "OBSERVED" | "INFERRED" | "RECOMMENDATION" | string;
  evidence_refs?: string[];
  /** Present on measured findings. `value` is whatever the tool produced — a
   *  number, a formatted string, or a list (e.g. the worst-performing hours) —
   *  so it is never rendered without going through a formatter. */
  metric?: string | null;
  value?: unknown;
}

export interface Approval {
  id: string;
  decision: string;
  action_type: string;
  action_payload: Record<string, unknown>;
  risk_level: string;
  expires_at: string;
  decided_by: string | null;
}

/** `agent_actions.verification_detail` is a JSON column (`Mapped[dict | None]`),
 *  not a string. It carries the verdict, the sentence explaining it, and the
 *  expected-vs-actual evidence the verdict was computed from. */
export interface VerificationDetail {
  state: VerificationState;
  reason: string;
  expected?: Record<string, unknown>;
  actual?: Record<string, unknown>;
  external_reference?: string | null;
}

export interface AgentAction {
  id: string;
  action_type: string;
  status: string;
  target_payment_id: string | null;
  external_payment_id: string | null;
  amount_minor: number | null;
  external_reference: string | null;
  verification_state: VerificationState | null;
  verification_detail: VerificationDetail | null;
  verify_attempts: number;
}

export interface Task {
  id: string;
  merchant_id: string;
  user_id: string;
  request: string;
  status: TaskStatus;
  final_answer: string | null;
  failure_code: string | null;
  findings: Finding[] | null;
  tool_calls: number | null;
  llm_turns: number | null;
  duration_ms: number | null;
  agent_version: string;
  model_version: string;
  prompt_version: string;
  is_replay: boolean;
  replayed_from: string | null;
  approvals: Approval[];
  actions: AgentAction[];
}

export interface TraceEvent {
  id: number;
  at: string;
  event: string;
  payload: Record<string, unknown>;
}

export interface Health {
  status: string;
  llm_provider: string;
  llm_credential_source: string | null;
  llm_provider_is_explicit: boolean;
  llm_model: string;
  payment_adapter: string;
  razorpay_execution_is_real: boolean;
  auth: string;
  auth_secret_is_development_default: boolean;
}

export interface Scenario {
  id: string;
  category: string;
  critical: boolean;
  description: string;
  /** The request that drives it, and who runs it — an `analyst` scenario means
   *  something different from an `owner` one, and the description does not
   *  always say so. */
  request: string;
  principal: string;
  /** What the scenario asserts. The description is prose; this is the contract. */
  expect: Record<string, unknown>;
  /** Setup that changes what the scenario means: an injected fault, a
   *  back-dated approval, a second identical request. */
  setup: Record<string, unknown>;
}

export interface ScenarioCheck {
  name: string;
  passed: boolean;
  detail: string;
}

export interface ScenarioMetrics {
  category: string;
  critical: boolean;
  tool_calls: number;
  llm_turns: number;
  duration_ms: number;
  final_status: string;
  failure_code: string | null;
  grounding_rate: number;
  tools_used: string[];
  /** The one that matters most: did this scenario move money externally? */
  external_actions: number;
  verification_states: string[];
}

export interface ScenarioResult {
  scenario_id: string;
  passed: boolean;
  checks: ScenarioCheck[];
  metrics: ScenarioMetrics;
}

/** Exactly the columns `escalated_actions()` selects. It does *not* return
 *  `action_type` — an earlier version of this type claimed it did, so the UI
 *  rendered an always-empty column and nothing complained. */
export interface EscalatedAction {
  id: string;
  task_id: string;
  merchant_id: string;
  target_payment_id: string | null;
  external_payment_id: string | null;
  amount_minor: number | null;
  external_reference: string | null;
  verification_state: VerificationState | null;
  verify_attempts: number;
  updated_at: string;
}

/** One line of the sweep's working: what it re-read, and what changed. */
export interface ReconcileDetail {
  action_id: string;
  from: string | null;
  to: string | null;
  attempt?: number;
  external_reference?: string | null;
  escalated?: boolean;
  error?: string;
}

export interface ReconcileReport {
  scanned: number;
  settled: number;
  still_unsettled: number;
  escalated: number;
  skipped_too_recent: number;
  details: ReconcileDetail[];
}

/** The two replay modes return genuinely different shapes, and both count
 *  external calls in `external_calls_made` — not `external_calls`. Typing that
 *  field wrongly made the UI read `undefined`, fail its `=== 0` check, and
 *  report a clean replay as a defect. */
export interface PlaybackStep {
  seq: number;
  tool: string;
  arguments: Record<string, unknown>;
  success: boolean;
  risk_level: string;
  policy_decision: string;
  duration_ms: number;
  error_code: string | null;
}

export interface PlaybackResult {
  mode: "PLAYBACK";
  task_id: string;
  request: string;
  status: string;
  final_answer: string | null;
  steps: PlaybackStep[];
  trace: unknown[];
  external_calls_made: number;
  note?: string;
}

export interface ReReasonResult {
  mode: "RE_REASON";
  replayed_from: string;
  replay_task_id: string;
  diverged: boolean;
  reasoning_diverged: boolean;
  policy_diverged: boolean;
  policy_divergence_cause: string | null;
  diff: Record<string, unknown>;
  original_tool_sequence: string[];
  replay_tool_sequence: string[];
  final_answer: string | null;
  external_calls_made: number;
  original_actions_unchanged: boolean;
  note?: string;
}

export type ReplayResult = PlaybackResult | ReReasonResult;


/** One fact a tool returned. `untrusted` marks merchant or customer free text —
 *  the injection surface. A client that renders it as ordinary system text is
 *  doing the one thing CONTRACT §36 exists to prevent. */
export interface EvidenceItem {
  key: string;
  value: unknown;
  source: string;
  untrusted: boolean;
}

export interface EvidenceToolCall {
  id: string;
  seq: number;
  tool: string;
  arguments: Record<string, unknown>;
  success: boolean;
  error_code: string | null;
  risk_level: string | null;
  policy_decision: string | null;
  duration_ms: number;
  evidence: EvidenceItem[];
  data: Record<string, unknown>;
}

export interface TaskEvidence {
  task_id: string;
  tool_calls: EvidenceToolCall[];
}
