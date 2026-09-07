// Agent activity as operational progress — plan P0-08.
//
// The plan's reason for this is exact, and it is the design constraint:
//
//   This demonstrates genuine AI without exposing private reasoning.
//
// So this component renders a list the *server* built from rows the application
// wrote — tool calls, policy decisions, approvals, actions, verification. It
// does not read `final_answer`, `findings`, or any other model-authored text,
// and it does not classify anything itself: a client deciding a step was `done`
// would be a client with an opinion about whether a financial action completed.
//
// It sits alongside `Stepper`, which answers a different question. The stepper
// says how far along the loop a task got, in six fixed stages; this says what
// actually ran, in the order it ran. One is a position, the other is a history,
// and collapsing them would lose whichever question the reader had.

import type { ActivityStep } from "../api/types";

const GLYPH: Record<ActivityStep["state"], string> = {
  done: "✓",
  failed: "✕",
  blocked: "⏸",
  running: "▶",
  pending: "·",
};

// What each state asserts, for the accessible name. Same discipline as
// `components/Status`: a red mark meaning "it failed" and one meaning "we are
// waiting on you" are different claims and must not sound alike.
const MEANING: Record<ActivityStep["state"], string> = {
  done: "completed",
  failed: "did not complete",
  blocked: "waiting on a person",
  running: "in progress",
  pending: "not reached",
};

export function AgentActivity({ steps }: { steps: ActivityStep[] }) {
  if (steps.length === 0) {
    return (
      <p className="sub">
        Nothing has run yet. This list is built from what the system recorded
        doing — it stays empty until something does.
      </p>
    );
  }

  return (
    <ol className="activity" aria-label="Agent activity">
      {steps.map((s) => (
        <li key={s.key} data-state={s.state}>
          <span className="act-glyph" aria-hidden="true">{GLYPH[s.state]}</span>
          <span className="act-label">
            {s.label}
            <span className="sr-only"> — {MEANING[s.state]}</span>
          </span>
          {s.detail ? <span className="act-detail muted">{s.detail}</span> : null}
          {s.at ? (
            <time className="act-at mono" dateTime={s.at}>
              {new Date(s.at).toLocaleTimeString()}
            </time>
          ) : null}
        </li>
      ))}
    </ol>
  );
}
