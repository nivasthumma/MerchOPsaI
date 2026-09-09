// Shapes returned by app/api/main.py. Kept narrow on purpose: every field here
// exists in a response the backend actually sends.

/** Every value `app/models.py::TaskStatus` can hold. `PENDING` (the column
 *  default) and `DENIED` (set when policy refuses at approval time) were missing
 *  here — a task in either state fell through every narrowing on this union.
 *  Found by `contract.ts` comparing this file against the generated schema,
 *  which is the reason that check exists. */
export type TaskStatus =
  | "PENDING"
  // Accepted and not yet started, when the server queued it for a worker rather
  // than running it inline (ADR-0045). Added here after `contract.ts` caught its
  // absence — the same check that caught PENDING and DENIED.
  | "QUEUED"
  | "RUNNING"
  | "AWAITING_APPROVAL"
  | "COMPLETED"
  | "DENIED"
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
  /** Absent, an object, or explicitly null — the server sends all three, and
   *  omitting `null` here was the mirror narrowing what it does not control. */
  expected?: Record<string, unknown> | null;
  actual?: Record<string, unknown> | null;
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

/** MerchantOps §56. Every failure carries more than a code: a code says what
 *  broke, not whether trying again is sensible. */
export interface FailureClass {
  error_code: string;
  category: string;
  retryability: "NEVER" | "BOUNDED_BACKOFF" | "RECONCILE" | "ESCALATE";
  owning_subsystem: string;
  recommended_next_action: string;
  correlation_id: string | null;
  is_classified: boolean;
}

/** MerchantOps §41 — what it takes to reproduce a run. */
export interface RunVersions {
  agent: string;
  model_provider: string | null;
  /** Nullable on the server: the columns are nullable, and a task recorded
   *  before a version was pinned has none. Asserting non-null here was the
   *  mirror being more confident than the thing it mirrors. */
  model: string | null;
  prompt: string | null;
  tool_registry: string | null;
  policy: string | null;
  workflow: string | null;
}

/** One step of the agent's operational progress — plan P0-08.
 *
 *  `state` is the claim being made: `done` happened and worked, `failed`
 *  happened and did not, `blocked` waits on a person, `running` is in flight,
 *  `pending` was expected and not reached. */
export interface ActivityStep {
  key: string;
  label: string;
  state: "done" | "failed" | "blocked" | "running" | "pending";
  /** Optional because the server omits it: several steps are derived from a
   *  state rather than from an event, and those have no honest timestamp to
   *  give. Invented ones would be worse than absent. */
  at?: string | null;
  detail: string;
}

export interface Task {
  id: string;
  tenant_id: string | null;
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
  /** Nullable for the same reason as `RunVersions.model`: the columns are
   *  nullable server-side. */
  agent_version: string;
  model_version: string | null;
  prompt_version: string | null;
  is_replay: boolean;
  replayed_from: string | null;
  approvals: Approval[];
  actions: AgentAction[];
  /** Operational progress — plan P0-08. Built server-side from recorded rows
   *  (tool calls, policy decisions, approvals, actions), never from
   *  model-authored text, so a step exists because something happened. */
  activity: ActivityStep[];

  /** MerchantOps §37. The model's own typed output. `agent_confidence` is
   *  displayed and consulted by nothing; `requires_human` is the OR of policy
   *  and the model, because the model may raise the bar and never lower it. */
  intent: string | null;
  recommendation: { type: string; detail: string | null } | null;
  agent_confidence: number | null;
  requires_human: boolean;
  model_requires_human: boolean;
  versions: RunVersions;
  failure: FailureClass | null;
}

/** MerchantOps §66 — the conversation the model actually saw. */
export interface AgentMessage {
  seq: number;
  turn: number;
  role: "user" | "assistant";
  content: unknown[];
  contains_untrusted: boolean;
  char_count: number;
  at: string;
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

/** Exactly the columns `unsettled_queue()` selects — plan P0-04.
 *
 *  It grew: an earlier version claimed `action_type` it did not return, so the
 *  UI rendered an always-empty column and nothing complained. The queue now
 *  carries every field the plan requires of an UNKNOWN row, and each one is
 *  selected server-side rather than joined in a browser. */
export interface EscalatedAction {
  id: string;
  task_id: string;
  merchant_id: string;
  action_type: string | null;
  status: string | null;
  target_payment_id: string | null;
  external_payment_id: string | null;
  amount_minor: number | null;
  external_reference: string | null;
  verification_state: VerificationState | null;
  verify_attempts: number;
  created_at: string;
  updated_at: string;
  /** Why it is unsettled. A queue of identifiers is a lookup exercise; the
   *  reason belongs on the row. */
  verification_detail: VerificationDetail | null;
  /** Escalation is a recorded decision, not a comparison this client
   *  re-derives from `verify_attempts`. */
  escalated: boolean;
  escalated_at: string | null;
  last_verified_at: string | null;
  /** When the sweep will look again.
   *
   *  Null does NOT mean "never". `unsettled_queue` matches
   *  `next_verify_at IS NULL OR next_verify_at <= now`, so for a row the sweep
   *  can still reach — unsettled, not escalated, attempts remaining — null
   *  means **due now**: never scheduled, which is what a just-claimed action
   *  carries and what every row written before the schedule existed carries.
   *
   *  It reads as "never" only for a row the sweep's other filters already
   *  exclude: settled, escalated, or out of attempts. `Actions.tsx` renders
   *  "due now" for the first case and an em dash for an escalated row, which
   *  is right — this comment used to say the opposite, and a comment that
   *  contradicts the query is how somebody "fixes" correct code. */
  next_verify_at: string | null;
  provider: string | null;
  environment: string | null;
  incident_id: string | null;
  owner: string | null;
}

// --------------------------------------------------------------- P0-03/P0-05
/** One action in the Action Center. One shape for every section that lists
 *  actions: a per-section type is how `amount_minor` comes to mean the
 *  requested amount in one column and the verified amount in another. */
export interface ActionRow {
  id: string;
  task_id: string;
  merchant_id: string;
  action_type: string;
  status: string;
  target_payment_id: string | null;
  external_payment_id: string | null;
  external_reference: string | null;
  amount_minor: number | null;
  verification_state: VerificationState | null;
  verify_attempts: number;
  escalated: boolean;
  escalated_at: string | null;
  last_verified_at: string | null;
  next_verify_at: string | null;
  approval_id: string | null;
  recovery_candidate_id: string | null;
  created_at: string;
  updated_at: string;
  provider_latency_ms: number | null;
  verification_latency_ms: number | null;
  customer_id: string | null;
  payment_method: string | null;
  provider: string | null;
  environment: string | null;
  incident_id: string | null;
  owner: string | null;
  task_request: string | null;
  task_status: string | null;
  approval_decision: string | null;
  risk_level: string | null;
  expires_at: string | null;
  required_signatures: number | null;
}

/** An approval no action exists for yet — the money has not moved and will not
 *  until a person decides. Deliberately not an `ActionRow` with nulls: that
 *  would tell an operator an action exists. */
export interface PendingApprovalRow {
  approval_id: string;
  task_id: string;
  action_type: string;
  action_payload: Record<string, unknown>;
  risk_level: string;
  decision: string;
  expires_at: string;
  required_signatures: number;
  created_at: string;
  evidence: unknown[];
  incident_id: string | null;
  owner: string | null;
  task_request: string | null;
  signatures: number;
  /** Decided by the database against the database's clock, never by this
   *  browser against its own. */
  expired: boolean;
}

export interface ActionCenterCounts {
  awaiting_approval: number;
  executing: number;
  unknown: number;
  escalated: number;
  recently_completed: number;
}

export interface ActionCenter {
  generated_at: string;
  merchant_id: string;
  awaiting_approval: PendingApprovalRow[];
  executing: ActionRow[];
  unknown: ActionRow[];
  escalated: ActionRow[];
  recently_completed: ActionRow[];
  /** TRUE totals, counted in SQL — not the length of the page. The two
   *  disagreed once, and the smaller number was on the screen an operator
   *  acts from. */
  counts: ActionCenterCounts;
  /** How many rows each section actually returned. Render `counts` beside a
   *  shorter list and you are showing a number you cannot substantiate. */
  shown: ActionCenterCounts;
  limit: number;
  /** The system's own stopping rule, so the UI renders it rather than keeping
   *  a second copy that can drift. */
  reconciliation_policy: { max_attempts: number; on_exhaustion: string };
  sections: string[];
}

/** One stage of the recovery funnel — P1-03. Ordered and labelled server-side
 *  so at-risk can never be arranged into reading as recovered. */
export interface FunnelStage {
  stage: "AT_RISK" | "RECOVERABLE" | "ATTEMPTED" | "RECOVERED";
  label: string;
  amount_minor: number;
}

export interface CommandCenter {
  generated_at: string;
  merchant_id: string;
  revenue: {
    at_risk_minor: number; recoverable_minor: number; attempted_minor: number;
    recovered_minor: number; failed_minor: number; unknown_minor: number;
    outstanding_minor: number; invariants_broken: string[];
  };
  funnel: FunnelStage[];
  attention: {
    approvals_pending: number; approvals_expired: number;
    unknown_actions: number; escalated_actions: number;
    open_incidents: number; critical_incidents: number; running_tasks: number;
  };
  by_incident: Record<string, unknown>[];
  by_method: Record<string, unknown>[];
  activity: { event_type: string; correlation_id: string | null;
              task_id: string | null; incident_id: string | null;
              created_at: string; payload: Record<string, unknown> }[];
}

/** One link in §7's chain. Every entry is a row that exists, with its own
 *  timestamp — never an inferred step. */
export interface LifecycleEvent {
  stage: string;
  at: string | null;
  id: string;
  label: string;
  detail: string;
  correlation_id: string | null;
}

/** MerchantOps §7 — a payment traceable through its complete lifecycle.
 *
 *  Distinct from `/trace/{correlation_id}`, which answers "everything one
 *  OPERATION touched". A payment's life spans several operations, which is why
 *  `correlation_ids` is a list. */
export interface PaymentLifecycle {
  payment: {
    id: string; merchant_id: string; order_id: string | null;
    customer_id: string | null; customer_name: string | null;
    amount_minor: number; currency: string; method: string; status: string;
    error_reason: string | null; amount_refunded_minor: number;
    refund_status: string | null; created_at: string | null;
  };
  external_payment_id: string | null;
  provider: string | null;
  environment: string | null;
  events: LifecycleEvent[];
  stages: string[];
  correlation_ids: string[];
  incident_ids: string[];
  task_ids: string[];
  action_ids: string[];
  generated_at: string;
}

export interface SearchHit {
  kind: string;
  id: string;
  label: string | null;
  detail: string | null;
  created_at: string | null;
  /** Where to go. Built server-side so the client is not maintaining a second
   *  map from entity kind to route. */
  route: string;
}

export interface SearchResults {
  query: string;
  results: SearchHit[];
  truncated: boolean;
}

/** One dependency's verdict — §11. `not_configured` is deliberately distinct
 *  from `down`: no webhook secret is a posture, not an outage.
 *
 *  The four declared fields are what an unauthenticated probe receives. The
 *  index signature covers the operational detail — coverage counts, the
 *  reconciliation backlog, drifted payment ids — which the server serves only
 *  to an authenticated caller, so a consumer must treat them as optional. */
export interface ComponentHealth {
  status: "healthy" | "degraded" | "down" | "not_configured";
  detail: string;
  required: boolean;
  latency_ms: number;
  [extra: string]: unknown;
}

export interface Readiness {
  status: "ready" | "degraded" | "not_ready";
  checked_at: string;
  components: Record<string, ComponentHealth>;
  blocking: string[];
  degraded: string[];
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

/** One saved view — plan P1-05. Declared server-side and served with the list,
 *  so "My attention" cannot mean one thing in a pasted link and another in the
 *  sidebar, and five counts come from one read rather than five requests. */
export interface SavedView {
  key: string;
  label: string;
  hint: string;
  filter: Record<string, unknown>;
  count: number;
}

export interface IncidentList {
  incidents: IncidentSummary[];
  /** Summed over the WHOLE match in SQL, never across the returned page. */
  total_revenue_at_risk_minor: number;
  /** How many matched, and how many are in `incidents`. Showing the total
   *  beside a shorter list is showing a number you cannot substantiate. */
  matched: number;
  shown: number;
  views: SavedView[];
  applied_view: string | null;
}

/** The eleven filters P1-05 names. Every one is applied in SQL server-side. */
export interface IncidentQuery {
  view?: string;
  severity?: string[];
  status?: string[];
  incident_type?: string[];
  payment_method?: string[];
  min_amount_minor?: number;
  max_age_hours?: number;
  unresolved?: boolean;
  approval_required?: boolean;
  has_unknown?: boolean;
  escalated?: boolean;
  include_closed?: boolean;
}

export interface IncidentDetail extends IncidentSummary {
  signals: Record<string, unknown>;
  evidence: { id: string; key: string; value: unknown; source: string; untrusted: boolean }[];
  tasks: { id: string; status: string; final_answer: string | null;
           tool_calls: number; duration_ms: number | null }[];
  legal_transitions: string[];
  /** The financial actions this incident produced — plan P0-07's last four
   *  stages. The same row shape the Action Center serves, so the two screens
   *  cannot disagree about the state of an action. */
  actions: ActionRow[];
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

/** One frame of the live event stream — MerchantOps v2 §11's field list,
 *  §62's names.
 *
 *  `event` is one of §62's fifteen and the set is closed server-side: the
 *  backend refuses to publish a name outside it, so a frame arriving here with
 *  an unfamiliar type means the vocabulary grew and this app has not caught up
 *  — which the timeline renders as itself rather than dropping. */
export interface LiveEvent {
  id: string;
  event: string;
  schema_version: string;
  occurred_at: string;
  payload_hash: string;
  payload: Record<string, unknown>;
  /** Optional, not merely nullable. The endpoint is served with
   *  `response_model_exclude_unset=True`, so a field the view did not set is
   *  ABSENT from the JSON rather than present as null. Declaring these
   *  `string | null` failed `contract.ts` against the generated schema — which
   *  is the whole reason that check exists, and it caught this before a
   *  browser did. */
  tenant_id?: string | null;
  merchant_id?: string | null;
  entity_id?: string | null;
  provider?: string | null;
  incident_id?: string | null;
  task_id?: string | null;
  correlation_id?: string | null;
}

export interface LiveEventList {
  events: LiveEvent[];
  /** Pass back as `after` to continue. Null when nothing was returned — a
   *  cursor of "nothing" is the cursor you already had. Optional for the same
   *  `exclude_unset` reason as the fields above. */
  next_cursor?: string | null;
  /** Frames written but not yet delivered to consumers. A number that only
   *  grows means the drain has stopped, which is invisible from the frames
   *  themselves: the timeline just stops moving, which looks like a quiet
   *  system rather than a broken one. */
  pending: number;
}

// ------------------------------------------------------- access review (§66)

/** What a role grants, so a reviewer signing off on "make them an approver"
 *  can see what that sentence actually means. */
export interface AccessReviewRole {
  name: string;
  permissions: string[];
}

/** One person's access. Shaped from a captured `/access-review` response, not
 *  from the endpoint's docstring: the operator queue's type once claimed a
 *  column the query never selected and the UI rendered an always-empty cell. */
export interface AccessReviewEntry {
  user_id: string;
  email: string;
  merchant_id: string;
  role: string;
  permissions: string[];
  /** ACTIVE | DISABLED. Offboarded accounts are listed deliberately — "whose
   *  access was removed, and when" is half of what a review asks, and an
   *  account missing from the list is indistinguishable from one that never
   *  existed. */
  status: string;
  deactivated_at?: string | null;
}

export interface AccessReview {
  tenant_id: string;
  /** Computed per read. A review quoted without an as-of is one somebody
   *  attests to a week after it stopped being true. */
  generated_at: string;
  roles: AccessReviewRole[];
  users: AccessReviewEntry[];
}

// ------------------------------------------------------ administration (§43)

export interface UserSummary {
  user_id: string;
  email: string;
  role: string;
  /** ACTIVE | DISABLED. */
  status: string;
  permissions: string[];
}

export interface UserList { users: UserSummary[] }

/** The one response carrying a credential. The token is returned once, at
 *  creation, and is not retrievable afterwards — the screen has to say so. */
export interface UserCreated {
  user_id: string;
  email: string;
  role: string;
  token: string;
}

export interface UserChange {
  user_id: string;
  role?: string | null;
  status?: string | null;
  changed?: boolean | null;
}

export interface PermissionView { name: string; description: string }

export interface RoleSummary {
  name: string;
  description: string;
  permissions: string[];
  /** Active holders. A role held by nobody is a role to question. */
  held_by: number;
}

export interface RoleList {
  roles: RoleSummary[];
  /** Derived from the tool registry, so it grows when a tool does. */
  catalogue: PermissionView[];
}

export interface RoleChange {
  name: string;
  permissions: string[];
  granted: string[];
  revoked: string[];
}

export interface SsoConfig {
  configured: boolean;
  issuer?: string | null;
  client_id?: string | null;
  email_domains: string[];
  default_role?: string | null;
  default_merchant_id?: string | null;
  enabled?: boolean | null;
}

export interface ScimTokenSummary {
  id: string;
  name: string;
  default_merchant_id: string;
  default_role: string;
  created_at: string;
  last_used_at?: string | null;
  revoked: boolean;
}

export interface ScimTokenList { tokens: ScimTokenSummary[] }

/** As with `UserCreated`: shown once, never again. */
export interface ScimTokenCreated { id: string; token: string; name: string }
