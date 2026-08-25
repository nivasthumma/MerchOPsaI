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
}

export interface ScenarioCheck {
  name: string;
  passed: boolean;
  detail: string;
}

export interface ScenarioResult {
  scenario_id: string;
  passed: boolean;
  checks: ScenarioCheck[];
  metrics?: Record<string, unknown>;
}

export interface EscalatedAction {
  id: string;
  task_id: string;
  merchant_id: string;
  action_type: string;
  verification_state: VerificationState | null;
  verify_attempts: number;
  external_reference: string | null;
  amount_minor: number | null;
  [k: string]: unknown;
}

export interface ReconcileReport {
  scanned: number;
  settled: number;
  escalated: number;
  still_unknown: number;
  [k: string]: unknown;
}

export interface ReplayResult {
  mode?: string;
  steps?: string[];
  external_calls: number;
  reasoning_diverged?: boolean;
  divergence_kind?: string | null;
  note?: string;
  [k: string]: unknown;
}
