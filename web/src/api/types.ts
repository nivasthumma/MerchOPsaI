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

export interface Principal {
  user_id: string;
  merchant_id: string;
  role: string;
  permissions: string[];
}

export interface ProviderChange {
  llm_provider: string;
  llm_provider_source: string;
  llm_model: string;
  changed_from: string;
}

/** The operations strip. Counts are scoped server-side to the caller's
 *  merchant — a count is still merchant data. */
export interface Metrics {
  window_hours: number;
  gated: number;
  approved: number;
  rejected: number;
  moved_minor: number;
  tool_calls: number;
  tool_errors: number;
  /** null, not 0, when nothing ran: a rate over zero calls is unknown. */
  tool_error_rate: number | null;
  p50_duration_ms: number | null;
  signing_secret_is_development_default: boolean;
}

export interface Health {
  status: string;
  llm_provider: string;
  llm_credential_source: string | null;
  llm_provider_is_explicit: boolean;
  /** `runtime` means it was switched in this process and will not survive a
   *  restart — and that a published metric was not measured under it. */
  llm_provider_source: string;
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
  /** The task the scenario produced. A verdict with no route to the trace is
   *  a verdict nobody can act on. */
  task_id: string | null;
  provider: string;
  model: string;
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
  /** Why it is unsettled. A queue of identifiers is a lookup exercise; the
   *  reason belongs on the row. */
  verification_detail: VerificationDetail | null;
}

/** One line of the sweep's working: what it re-read, and what changed. */
export interface ReconcileDetail {
  action_id: string;
  /** The task the action belongs to, so a swept row can be followed up. */
  task_id: string | null;
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

/** MerchantOps §49. Six figures that nest, in one unit — see `basis`. */
export interface RecoveryLedger {
  merchant_id: string;
  at_risk_minor: number;
  recoverable_minor: number;
  attempted_minor: number;
  recovered_minor: number;
  failed_minor: number;
  unknown_minor: number;
  outstanding_minor: number;
  by_incident: {
    incident_id: string; incident_type: string; severity: string; status: string;
    title: string; revenue_at_risk_minor: number;
    recoverable_minor: number; recovered_minor: number;
  }[];
  by_method: {
    method: string; recoverable_minor: number; recovered_minor: number;
    candidates: number;
  }[];
  /** Empty when the §49 orderings hold. Rendered when it is not: a reporting
   *  defect has to be visible, not swallowed. */
  invariants_broken: string[];
  basis: string;
}

/** MerchantOps §50. */
export interface Dashboard {
  recovery: RecoveryLedger;
  incidents: { by_status: Record<string, number>; open: number; resolved: number };
  agent_activity: {
    investigations: number; tool_calls: number; recommendations: number;
    awaiting_approval: number; escalations: number;
  };
}

/** MerchantOps §13 / §51. */
export interface IncidentSummary {
  id: string; merchant_id: string; type: string; severity: string; status: string;
  title: string; summary: string; revenue_at_risk_minor: number;
  detection_rule: string; detection_version: string; correlation_id: string;
  started_at: string; detected_at: string; resolved_at: string | null;
}

export interface IncidentDetail extends IncidentSummary {
  signals: Record<string, unknown>;
  evidence: { id: string; key: string; value: unknown; source: string; untrusted: boolean }[];
  tasks: { id: string; status: string; final_answer: string | null;
           tool_calls: number; duration_ms: number | null }[];
  legal_transitions: string[];
  recovery: RecoveryPlanView | null;
  timeline: { at: string; event: string; task_id: string | null;
              detail: Record<string, unknown> }[];
}

export interface RecoveryPlanView {
  id: string; incident_id: string; status: string; intervention: string;
  revenue_at_risk_minor: number; eligible_recovery_minor: number;
  expected_recovery_minor: number; expected_recovery_basis: string;
  budget: { max_recovery_minor: number; max_actions: number;
            max_attempts_per_customer: number; max_duration_seconds: number };
  stop_rule: string | null; stop_reason: string | null;
  candidates?: { id: string; rank: number; payment_id: string; customer_id: string;
                 amount_minor: number; attributed_amount_minor: number;
                 intervention: string; status: string; ineligible_reason: string | null;
                 expected_recovery_minor: number; actual_recovery_minor: number;
                 executable: boolean; attempts: number; task_id: string | null }[];
}
