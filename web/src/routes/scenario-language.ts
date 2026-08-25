// Turning the `expect` block into sentences.
//
// A reader deciding whether "106/106" means anything needs to see what each
// scenario asserts. Dumping the JSON would technically show it; this makes it
// readable. Anything unrecognised falls through to `key: value` rather than
// being dropped — a silently omitted assertion would overstate what is checked.

const LIST = (v: unknown) => (Array.isArray(v) ? v.join(", ") : String(v));

const PHRASE: Record<string, (v: unknown) => string> = {
  tool_sequence: (v) => `calls exactly: ${LIST(v)}`,
  tools_include: (v) => `calls: ${LIST(v)}`,
  tools_forbidden: (v) => `never calls: ${LIST(v)}`,
  policy_decision: (v) => `policy decides ${v}`,
  policy_rule: (v) => `under rule ${v}`,
  approval_required: (v) => (v ? "requires human approval" : "requires no approval"),
  final_status: (v) => `ends ${v}`,
  verification_state: (v) => `verification settles ${v}`,
  failure_code: (v) => `fails with ${v}`,
  external_calls: (v) => `makes ${v} external call${v === 1 ? "" : "s"}`,
  no_financial_effect: () => "moves no money",
  refund_delta: (v) => `refund rows change by exactly ${v}`,
  action_status: (v) => `action ends ${v}`,
  approval_decision: (v) => `approval recorded as ${v}`,
  audit_events: (v) => `audit records: ${LIST(v)}`,
  audit_excludes_secrets: () => "no secret survives into the audit trail",
  answer_contains: (v) => `answer mentions: ${LIST(v)}`,
  answer_excludes: (v) => `answer never mentions: ${LIST(v)}`,
  min_grounding_rate: (v) => `grounding rate at least ${v}`,
};

const SETUP: Record<string, (v: unknown) => string> = {
  fault: (v) => {
    const f = v as { fault?: string; on_operation?: string };
    return `injects ${f.fault ?? "a fault"}${f.on_operation ? ` on ${f.on_operation}` : ""}`;
  },
  approve: (v) => (v ? "the human approves" : "the human rejects"),
  approve_as: (v) => `approved by ${v}`,
  expire_approval: () => "the approval is back-dated past its TTL",
  reverify: () => "re-verification is run",
  reconcile: () => "the reconciliation sweep is run",
  repeat_request: () => "the same request is issued twice",
  allowed_tools: (v) => `only these tools are offered: ${LIST(v)}`,
  budget: (v) => `budget overridden: ${JSON.stringify(v)}`,
  initial_state: (v) => `state: ${Object.keys(v as object).join(", ")}`,
};

function render(map: Record<string, (v: unknown) => string>, obj: Record<string, unknown>) {
  return Object.entries(obj)
    .filter(([, v]) => v !== null && v !== undefined && v !== false)
    .map(([k, v]) => (map[k] ? map[k](v) : `${k}: ${JSON.stringify(v)}`));
}

export const assertions = (expect: Record<string, unknown>) => render(PHRASE, expect);
export const conditions = (setup: Record<string, unknown>) => render(SETUP, setup);
