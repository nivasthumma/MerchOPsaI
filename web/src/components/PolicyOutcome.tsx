// What policy decided, stated where it can be seen.
//
// One implementation for both pages. The task page has the whole trace and was
// showing this only as a row inside a filterable list; the investigate page had
// to fetch the trace specifically to say it. A refusal is the most important
// thing that can happen to a request, and it should not read differently
// depending on which screen you are standing on.

import type { TraceEvent } from "../api/types";

export interface PolicyDecision {
  tool: string;
  decision: string;
  rule?: string;
  reason?: string;
}

/** Every policy outcome that was not a plain ALLOW. */
export function policyDecisions(trace: TraceEvent[]): PolicyDecision[] {
  return trace
    .filter((e) => e.event === "policy_decision")
    .map((e) => e.payload as unknown as PolicyDecision)
    .filter((d) => d && d.decision && d.decision !== "ALLOW");
}

export function PolicyOutcome({ decisions }: { decisions: PolicyDecision[] }) {
  if (!decisions.length) return null;
  return (
    <>
      {decisions.map((d, i) => (
        <div key={i} className={`banner ${d.decision === "DENY" ? "danger" : "warn"}`}>
          <strong>
            {d.decision === "DENY" ? "Refused" : "Held for approval"}: {d.tool}
          </strong>{" "}
          {d.reason ?? ""}
          {d.rule ? <> <code>{d.rule}</code></> : null}
          <div className="muted" style={{ fontSize: 12.5, marginTop: 4 }}>
            The decision was made outside the model, and no external call was made.
          </div>
        </div>
      ))}
    </>
  );
}
