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

import type { ActivityStep, AiMode } from "../api/types";

/** How a run was produced, in words — the four modes `app/agent/provenance.py`
 *  records.
 *
 *  The label is the claim, so it has to be exact: a fallback run was produced
 *  by the deterministic planner, which applies rules and arithmetic and does
 *  not reason. Calling that a model result would have a merchant read
 *  arithmetic as judgement. `fallback` is what decides whether the notice
 *  below is shown; the tone only colours a badge whose words already say it. */
export interface ModeSpec {
  label: string;
  tone: "warn" | "neutral";
  meaning: string;
  fallback: boolean;
}

export const AI_MODE: Record<string, ModeSpec> = {
  AI_SUCCESS: {
    label: "Model result", tone: "neutral", fallback: false,
    meaning: "The configured model produced every turn of this run.",
  },
  AI_FAILED_FALLBACK: {
    label: "Deterministic fallback — model failed", tone: "warn", fallback: true,
    meaning: "The model was reached and failed partway; the deterministic "
             + "planner finished the run.",
  },
  AI_UNAVAILABLE_FALLBACK: {
    label: "Deterministic fallback — model unavailable", tone: "warn", fallback: true,
    meaning: "A model was configured and could not be reached; the "
             + "deterministic planner ran the whole run.",
  },
  DETERMINISTIC_ONLY: {
    label: "Deterministic planner (no model configured)", tone: "neutral",
    fallback: false,
    meaning: "No model is configured, so the planner ran by design. Its output "
             + "is rules and arithmetic, not a model's judgement.",
  },
};

export function aiModeSpec(mode: AiMode | null | undefined): ModeSpec {
  if (!mode) {
    // Not "model" and not "planner": a run recorded before the mode was
    // carries nothing that says which produced it, and guessing would be a
    // claim about provenance this client has no evidence for.
    return { label: "Mode not recorded", tone: "neutral", fallback: false,
             meaning: "This run was recorded before the system noted how runs "
                      + "were produced." };
  }
  return AI_MODE[mode] ?? {
    label: mode.replace(/_/g, " "), tone: "neutral", fallback: false,
    meaning: `An unrecognised mode (${mode}). Rendered as received.`,
  };
}

/** The mode as a badge. Text, not colour: the words carry the claim and the
 *  tone is only a second cue (P1-08). */
export function AiModeBadge({ mode }: { mode: AiMode | null | undefined }) {
  const spec = aiModeSpec(mode);
  return (
    <span className={`pill ${spec.tone}`} title={spec.meaning}>
      <span className="sr-only">How this run was produced: </span>
      {spec.label}
    </span>
  );
}

/** Said once, plainly, above everything the run produced — and only for the
 *  two fallback modes.
 *
 *  Not an alarm. A fallback is the system doing what it was configured to do
 *  when a model is unreachable, and nothing about money depends on it. What it
 *  must not do is let the findings and recommendation below read as a model's
 *  conclusions, because they are not. */
export function FallbackNotice({ mode }: { mode: AiMode | null | undefined }) {
  if (!aiModeSpec(mode).fallback) return null;
  const failed = mode === "AI_FAILED_FALLBACK";
  return (
    <div className="banner info" role="note" aria-label="How this run was produced">
      <strong>
        {failed ? "Finished by the deterministic planner." : "Run by the deterministic planner."}
      </strong>{" "}
      {failed
        ? "The configured model was reached and then failed partway through, so "
          + "the rest of this run — including what it concluded — came from fixed rules."
        : "The configured model could not be reached, so the deterministic planner "
          + "ran this whole run on fixed rules."}{" "}
      <strong>This is not a model result.</strong> Policy, approval and
      verification never depend on the model, and ran as they always do.
    </div>
  );
}

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
