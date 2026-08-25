// Typed access to the FastAPI surface.
//
// The token is the caller's whole identity as far as this app is concerned, and
// deliberately so: permissions are read from the database on every request
// server-side, never carried in the token or asserted by this client. Nothing
// here decides what the user may do — it asks, and renders the answer.

import type {
  EscalatedAction, Health, Metrics, ReconcileReport, ReplayResult, Scenario,
  Principal, ProviderChange, ScenarioResult, Task, TaskEvidence, TraceEvent,
} from "./types";

const BASE = "/api";
const TOKEN_KEY = "merchantops.token";

// In-flight request count, so the progress indicator reflects real work rather
// than a timer pretending to be one. A bar that finishes before the request
// does is worse than no bar: it teaches people the app is done when it is not.
let pending = 0;
const listeners = new Set<(n: number) => void>();

function setPending(n: number) {
  pending = n;
  for (const l of listeners) l(pending);
}

export const activity = {
  get pending() { return pending; },
  subscribe(fn: (n: number) => void) {
    listeners.add(fn);
    fn(pending);
    return () => { listeners.delete(fn); };
  },
};

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    // Private browsing and some embedded webviews throw on access rather than
    // returning null. An unreadable store is the same as an empty one here.
    return "";
  }
}

export function setToken(token: string): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* nothing to do — the session simply will not survive a reload */
  }
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly code?: string,
    readonly body?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }

  /** 401 means the token is missing, invalid, or its subject is gone. */
  get isAuth(): boolean {
    return this.status === 401;
  }

  /** 409 is the approval state machine refusing — expected, not a bug. */
  get isConflict(): boolean {
    return this.status === 409;
  }
}

async function request<T>(
  path: string,
  init: RequestInit = {},
  { auth = true }: { auth?: boolean } = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body) headers.set("Content-Type", "application/json");
  if (auth) {
    const token = getToken();
    if (!token) throw new ApiError(401, "No token. Mint one with scripts/issue_token.py.");
    headers.set("Authorization", `Bearer ${token}`);
  }

  let res: Response;
  setPending(pending + 1);
  try {
    res = await fetch(`${BASE}${path}`, { ...init, headers });
  } catch (e) {
    // A network-level failure is almost always "the API is not running", which
    // is worth saying plainly rather than surfacing "Failed to fetch".
    throw new ApiError(0, `Cannot reach the API. Is it running on :8000? (${String(e)})`);
  } finally {
    // One decrement, on both paths. Reading the body below is fast enough that
    // counting it would only make the indicator linger after the work is done.
    setPending(pending - 1);
  }

  const text = await res.text();
  const body = text ? safeJson(text) : null;

  if (!res.ok) {
    // FastAPI puts the message in `detail`, which may itself be an object when
    // the route raised HTTPException(409, {"error": ..., "code": ...}).
    const detail = (body as { detail?: unknown } | null)?.detail;
    if (detail && typeof detail === "object") {
      const d = detail as { error?: string; code?: string };
      throw new ApiError(res.status, d.error ?? res.statusText, d.code, body);
    }
    throw new ApiError(res.status, typeof detail === "string" ? detail : res.statusText,
                       undefined, body);
  }
  return body as T;
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return { detail: text };
  }
}

export const api = {
  health: () => request<Health>("/health", {}, { auth: false }),

  me: () => request<Principal>("/me"),

  /** Counts for the operations strip. Authenticated: see types.Metrics. */
  metrics: () => request<Metrics>("/metrics"),

  /** Selects among providers the server can already reach. It never carries a
   *  credential — CONTRACT §37 keeps those in the environment. */
  setProvider: (provider: "auto" | "deterministic" | "anthropic") =>
    request<ProviderChange>("/config/llm-provider",
                            { method: "POST", body: JSON.stringify({ provider }) }),

  createTask: (req: string) =>
    request<Task>("/tasks", { method: "POST", body: JSON.stringify({ request: req }) }),

  getTask: (id: string) => request<Task>(`/tasks/${encodeURIComponent(id)}`),

  /** CONTRACT §21: the evidence the human reviews before approving. */
  getEvidence: (id: string) =>
    request<TaskEvidence>(`/tasks/${encodeURIComponent(id)}/evidence`),

  getTrace: (id: string) =>
    request<{ task_id: string; trace: TraceEvent[] }>(`/tasks/${encodeURIComponent(id)}/trace`),

  approve: (id: string) =>
    request<Task>(`/tasks/${encodeURIComponent(id)}/approve`, { method: "POST" }),

  reject: (id: string) =>
    request<Task>(`/tasks/${encodeURIComponent(id)}/reject`, { method: "POST" }),

  reverify: (id: string) =>
    request<{ task: Task; verification: Record<string, unknown> }>(
      `/tasks/${encodeURIComponent(id)}/reverify`, { method: "POST" }),

  replay: (id: string, mode: "PLAYBACK" | "RE_REASON") =>
    request<ReplayResult>(
      `/tasks/${encodeURIComponent(id)}/replay?mode=${mode}`, { method: "POST" }),

  /** `minAgeSeconds` guards against racing the request that created an action;
   *  `maxAttempts` is the escalation line. Both are the endpoint's own
   *  defaults unless a caller deliberately overrides them. */
  reconcile: (opts: { minAgeSeconds?: number; maxAttempts?: number } = {}) => {
    const q = new URLSearchParams();
    if (opts.minAgeSeconds !== undefined) q.set("min_age_seconds", String(opts.minAgeSeconds));
    if (opts.maxAttempts !== undefined) q.set("max_attempts", String(opts.maxAttempts));
    const suffix = q.toString() ? `?${q}` : "";
    return request<ReconcileReport>(`/actions/reconcile${suffix}`, { method: "POST" });
  },

  /** `maxAttempts` is the endpoint's own threshold: rows are unsettled actions
   *  with at least that many verify attempts. The default of 5 is the
   *  escalation line; 0 returns everything still unsettled, including the
   *  actions the sweep is actively working. */
  escalated: (maxAttempts?: number) =>
    request<EscalatedAction[]>(
      `/actions/escalated${maxAttempts === undefined ? "" : `?max_attempts=${maxAttempts}`}`),

  scenarios: () => request<Scenario[]>("/scenarios", {}, { auth: false }),

  runScenario: (id: string) =>
    request<ScenarioResult>(`/scenarios/${encodeURIComponent(id)}/run`, { method: "POST" },
                            { auth: false }),
};
