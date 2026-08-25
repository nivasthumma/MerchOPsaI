// One scannable line per audit event.
//
// Written from real payloads (src/test-fixtures/trace.json), not from what the
// events are assumed to contain — the last time this app guessed at a backend
// shape it rendered an object into JSX and took the page down. Anything absent
// falls through to no summary rather than to "undefined".

import type { TraceEvent } from "../api/types";

export type TraceGroup = "policy" | "action" | "verification" | "approval" | "system";

const GROUPS: Record<string, TraceGroup> = {
  policy_decision: "policy",
  policy_recheck: "policy",
  tool_rejected: "policy",
  action_executing: "action",
  action_recorded: "action",
  verification: "verification",
  approval_requested: "approval",
  awaiting_approval: "approval",
  approval_decided: "approval",
};

export function groupOf(event: string): TraceGroup {
  return GROUPS[event] ?? "system";
}

const ICONS: Record<TraceGroup, string> = {
  policy: "⚖",
  action: "→",
  verification: "✓",
  approval: "⏸",
  system: "·",
};

export function iconOf(event: string): string {
  return ICONS[groupOf(event)];
}

function str(v: unknown): string | null {
  return typeof v === "string" ? v : null;
}

/** A one-line gloss, or null when the payload has nothing worth promoting. */
export function summarise(e: TraceEvent): string | null {
  const p = (e.payload ?? {}) as Record<string, unknown>;
  switch (e.event) {
    case "task_created":
      return str(p.request);
    case "llm_turn": {
      const tools = Array.isArray(p.requested_tools) ? p.requested_tools.join(", ") : "";
      return `turn ${p.turn} · ${p.stop_reason}${tools ? ` · ${tools}` : ""}`;
    }
    case "policy_decision":
      return `${p.decision} · ${p.tool} · ${p.rule}`;
    case "policy_recheck":
      return `${p.decision} · ${p.rule}`;
    case "tool_call":
      return `${p.tool} · ${p.success ? "ok" : "failed"}${
        p.duration_ms != null ? ` · ${p.duration_ms} ms` : ""}`;
    case "approval_requested":
      return `${p.action} · ${p.approval_id}`;
    case "awaiting_approval":
      return str(p.approval_id);
    case "action_executing":
      return `${p.payment} · adapter ${p.adapter_mode}`;
    case "action_recorded":
      return `${p.status} · ${p.external_reference ?? "no reference"}`;
    case "verification": {
      const detail = p.detail as { reason?: string } | undefined;
      return `${p.state}${detail?.reason ? ` · ${detail.reason}` : ""}`;
    }
    case "task_completed":
      return `${p.status}${p.verification ? ` · verification ${p.verification}` : ""}`;
    default:
      return null;
  }
}
