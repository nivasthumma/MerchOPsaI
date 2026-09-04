// Typed access to the FastAPI surface.
//
// The token is the caller's whole identity as far as this app is concerned, and
// deliberately so: permissions are read from the database on every request
// server-side, never carried in the token or asserted by this client. Nothing
// here decides what the user may do — it asks, and renders the answer.

import type {
  AgentMessage,
  Dashboard,
  IncidentDetail,
  IncidentSummary,
  EscalatedAction, Health, LiveEventList, Metrics, ReconcileReport, ReplayResult,
  Scenario,
  Principal, ProviderChange, ScenarioResult, Task, TaskEvidence, TraceEvent,
} from "./types";

const BASE = "/api";
const TOKEN_KEY = "merchantops.token";
const REFRESH_KEY = "merchantops.refresh";

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

/** A demo credential baked in at build time, or "" when none was configured.
 *
 *  This exists so a public deployment can be opened and used without minting a
 *  token first. It is a real bearer token and anyone who loads the page has it,
 *  so it belongs only on a deployment where that is acceptable: synthetic data,
 *  the mock payment adapter, nothing that reaches a real financial system.
 *
 *  It is read from the environment rather than written here, so the credential
 *  never enters the repository and can be rotated by changing one Vercel
 *  variable and redeploying. Leave VITE_DEMO_TOKEN unset and the app behaves
 *  exactly as before: it asks for a token. */
const DEMO_TOKEN: string = import.meta.env.VITE_DEMO_TOKEN ?? "";

export function getToken(): string {
  try {
    // A token this browser was given wins over the shared demo one, so signing
    // in as somebody else is still possible and still sticks.
    return localStorage.getItem(TOKEN_KEY) ?? DEMO_TOKEN;
  } catch {
    // Private browsing and some embedded webviews throw on access rather than
    // returning null. An unreadable store is the same as an empty one here.
    return DEMO_TOKEN;
  }
}

/** True when the session is running on the shared demo credential rather than
 *  one this person supplied. The UI says so — a page that silently hands a
 *  visitor approval rights over somebody's audit log should admit it. */
export function isDemoSession(): boolean {
  try {
    return !localStorage.getItem(TOKEN_KEY) && !!DEMO_TOKEN;
  } catch {
    return !!DEMO_TOKEN;
  }
}

export function getRefreshToken(): string {
  try {
    return localStorage.getItem(REFRESH_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setRefreshToken(token: string): void {
  try {
    if (token) localStorage.setItem(REFRESH_KEY, token);
    else localStorage.removeItem(REFRESH_KEY);
  } catch {
    /* private browsing; the session simply will not survive a reload */
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

/** Exchange the stored refresh token for a new pair. True if it worked.
 *
 *  Both tokens are replaced: refresh is single use, so the response carries a
 *  new one and keeping the old would guarantee the next attempt is treated as a
 *  replay -- which signs the account out of everything (ADR-0049). */
async function tryRefresh(): Promise<boolean> {
  const refresh = getRefreshToken();
  if (!refresh) return false;
  try {
    const res = await fetch(`${BASE}/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ refresh_token: refresh }),
    });
    if (!res.ok) {
      // Expired, revoked, or replayed. All of them mean this session is over,
      // and keeping a dead refresh token only causes the next request to try
      // again.
      setRefreshToken("");
      return false;
    }
    const body = (await res.json()) as { access_token: string; refresh_token: string };
    setToken(body.access_token);
    setRefreshToken(body.refresh_token);
    return true;
  } catch {
    return false;
  }
}

async function request<T>(
  path: string,
  init: RequestInit = {},
  { auth = true, retried = false }: { auth?: boolean; retried?: boolean } = {},
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
    // An access token now expires (ADR-0049), so a 401 on a request that
    // carried one is ordinarily "it aged out" rather than "sign in again". One
    // refresh, then one retry: if the refresh also fails the session is
    // genuinely over and the 401 is the honest answer.
    //
    // Once, deliberately. A retry loop around an endpoint that mints
    // credentials is how a client turns an expired session into a flood.
    if (res.status === 401 && auth && !retried && getRefreshToken()) {
      if (await tryRefresh()) {
        setPending(pending - 1);
        return request<T>(path, init, { auth, retried: true });
      }
    }
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

  /** MerchantOps §50. Deliberately a different endpoint from `metrics`:
   *  one counts operations, the other reports money. */
  dashboard: () => request<Dashboard>("/dashboard"),

  incidents: () =>
    request<{ incidents: IncidentSummary[]; total_revenue_at_risk_minor: number }>(
      "/incidents"),

  /** Idempotent: a second sweep over the same window reports `already_known`
   *  rather than raising a second incident for one anomaly. */
  detect: () =>
    request<{ merchant_id: string; anomalies_found: number; incidents_created: number;
              already_known: number; duration_ms: number }>(
      "/incidents/detect", { method: "POST" }),

  getIncident: (id: string) =>
    request<IncidentDetail>(`/incidents/${encodeURIComponent(id)}`),

  /** MerchantOps v2 §62, §65 — the live event stream, read as a cursor.
   *
   *  Polled rather than streamed, and that is not a shortcut. The server also
   *  exposes `/events/stream` as `text/event-stream`, but the browser's
   *  `EventSource` cannot set request headers, so it cannot present this app's
   *  bearer token. The alternatives are putting a credential in a query string
   *  — where it lands in access logs and browser history — or moving the whole
   *  app to cookie auth for one screen. Neither is worth it, and the server's
   *  own docstring already says the cursor is the real mechanism and the
   *  stream is sugar over it.
   *
   *  `after` is an event id rather than a timestamp: two frames written in one
   *  transaction share a timestamp to the microsecond, so a time cursor would
   *  either replay one or skip one. */
  events: (after?: string | null, limit = 100) => {
    const q = new URLSearchParams({ limit: String(limit) });
    if (after) q.set("after", after);
    return request<LiveEventList>(`/events?${q.toString()}`);
  },

  /** Deliver pending frames to their consumers. Exposed because this
   *  deployment has no worker; the claim is `FOR UPDATE SKIP LOCKED`, so
   *  pressing it twice costs a wasted query rather than a double delivery. */
  drainEvents: () =>
    request<{ claimed: number; published: number; failed: number }>(
      "/events/drain", { method: "POST" }),

  /** Selects among providers the server can already reach. It never carries a
   *  credential — CONTRACT §37 keeps those in the environment. */
  setProvider: (provider: "auto" | "deterministic" | "anthropic") =>
    request<ProviderChange>("/config/llm-provider",
                            { method: "POST", body: JSON.stringify({ provider }) }),

  createTask: (req: string) =>
    request<Task>("/tasks", { method: "POST", body: JSON.stringify({ request: req }) }),

  getTask: (id: string) => request<Task>(`/tasks/${encodeURIComponent(id)}`),

  /** Poll a task until it stops moving.
   *
   *  The server may run a task inline and hand it back finished, or accept it
   *  with 202 and let a worker run it (ADR-0045). The client cannot know which,
   *  and should not have to: this resolves either way. A task that comes back
   *  already terminal never issues a request.
   *
   *  Terminal is "not QUEUED and not RUNNING" rather than a list of finished
   *  states, so a status added later stops the poll instead of hanging on it.
   *  AWAITING_APPROVAL is terminal here on purpose -- it is waiting for a
   *  person, which can be a long time, and the page has something to show. */
  awaitTask: async (task: Task, opts?: { timeoutMs?: number; intervalMs?: number;
                                         signal?: AbortSignal }): Promise<Task> => {
    const interval = opts?.intervalMs ?? 800;
    // Bounded, because an unbounded poll against a queue nobody is draining is
    // a spinner that never stops and never says why. The caller surfaces the
    // task in whatever state it reached.
    const deadline = Date.now() + (opts?.timeoutMs ?? 120_000);
    let current = task;
    while (current.status === "QUEUED" || current.status === "RUNNING") {
      if (opts?.signal?.aborted || Date.now() > deadline) return current;
      await new Promise((r) => setTimeout(r, interval));
      if (opts?.signal?.aborted) return current;
      current = await request<Task>(`/tasks/${encodeURIComponent(current.id)}`);
    }
    return current;
  },

  /** CONTRACT §21: the evidence the human reviews before approving. */
  getEvidence: (id: string) =>
    request<TaskEvidence>(`/tasks/${encodeURIComponent(id)}/evidence`),

  /** MerchantOps §66. Distinct from the trace: the trace is what the
   *  application DID, this is what the model was looking at when it decided. */
  getMessages: (id: string) =>
    request<{ task_id: string; messages: AgentMessage[]; total_chars: number }>(
      `/tasks/${encodeURIComponent(id)}/messages`),

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
