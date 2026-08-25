/** Recently opened tasks, for navigation only.
 *
 *  There is no "list my tasks" endpoint, by design: a task belongs to the
 *  merchant, not to a browser. This list is a convenience for getting back to
 *  something you just looked at. It is not the record — the record is the
 *  server-side audit trail, and nothing here should ever be presented as it.
 *
 *  It lives in a module rather than in a component because two places need it
 *  now: Investigate writes to it, and the task rail in the app shell reads it.
 *  Subscribers are notified on write so the rail updates without a reload,
 *  mirroring the `activity` store in api/client.
 */

export interface RecentTask {
  id: string;
  request: string;
  status?: string;
  at?: number;
}

const KEY = "merchantops.recent";
const LIMIT = 8;

const listeners = new Set<(items: RecentTask[]) => void>();

export function readRecent(): RecentTask[] {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) ?? "[]");
    return Array.isArray(raw) ? raw : [];
  } catch {
    return [];
  }
}

export function remember(task: { id: string; request: string; status?: string }) {
  try {
    const next = [
      { id: task.id, request: task.request, status: task.status, at: Date.now() },
      ...readRecent().filter((r) => r.id !== task.id),
    ].slice(0, LIMIT);
    localStorage.setItem(KEY, JSON.stringify(next));
    listeners.forEach((fn) => fn(next));
  } catch {
    /* a browser refusing storage costs navigation convenience, nothing more */
  }
}

/** Drop one entry — used when the server says that task no longer exists.
 *  The list is local and the record is server-side, so the two can drift; a
 *  dead link that cannot be cleared is a rail that gets worse over time. */
export function forgetOne(id: string) {
  try {
    localStorage.setItem(KEY, JSON.stringify(readRecent().filter((r) => r.id !== id)));
  } catch {
    /* nothing to do */
  }
  listeners.forEach((fn) => fn(readRecent()));
}

export function forgetRecent() {
  try {
    localStorage.removeItem(KEY);
  } catch {
    /* nothing to do */
  }
  listeners.forEach((fn) => fn([]));
}

export function subscribeRecent(fn: (items: RecentTask[]) => void) {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}
