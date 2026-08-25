// Typed access to the FastAPI surface.
//
// The token is the caller's whole identity as far as this app is concerned, and
// deliberately so: permissions are read from the database on every request
// server-side, never carried in the token or asserted by this client. Nothing
// here decides what the user may do — it asks, and renders the answer.

import type {
  EscalatedAction, Health, ReconcileReport, ReplayResult, Scenario,
  ScenarioResult, Task, TaskEvidence, TraceEvent,
} from "./types";

const BASE = "/api";
const TOKEN_KEY = "merchantops.token";

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
  try {
    res = await fetch(`${BASE}${path}`, { ...init, headers });
  } catch (e) {
    // A network-level failure is almost always "the API is not running", which
    // is worth saying plainly rather than surfacing "Failed to fetch".
    throw new ApiError(0, `Cannot reach the API. Is it running on :8000? (${String(e)})`);
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

  reconcile: () => request<ReconcileReport>("/actions/reconcile", { method: "POST" }),

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
