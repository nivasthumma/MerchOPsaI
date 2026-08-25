import type { Task } from "../api/types";

/** Where a task stopped, along the loop the README describes. Reading a status
 *  string tells you the state; this tells you how far it got and what is left. */
export function Stepper({ task }: { task: Task }) {
  const halted = task.status === "AWAITING_APPROVAL";
  const rejected = task.status === "REJECTED";
  const acted = task.actions.length > 0;
  const verified = task.actions.some((a) => a.verification_state != null);
  const needsApproval = task.approvals.length > 0;

  const steps: { label: string; state: "done" | "here" | "blocked" | "" }[] = [
    { label: "Investigate", state: "done" },
    { label: "Recommend", state: needsApproval || acted ? "done" : "" },
    {
      label: "Approval",
      state: halted ? "blocked" : rejected ? "blocked" : needsApproval ? "done" : "",
    },
    { label: "Execute", state: acted ? "done" : halted ? "here" : "" },
    { label: "Verify", state: verified ? "done" : "" },
    { label: "Audit", state: "done" },
  ];

  return (
    <div className="stepper" aria-label="Task progress">
      {steps.map((s) => (
        <span key={s.label} className={`step ${s.state}`}>
          {s.state === "done" ? "✓" : s.state === "blocked" ? "⏸" : "·"} {s.label}
        </span>
      ))}
    </div>
  );
}
