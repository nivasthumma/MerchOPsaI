import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api/client";
import type { AccessReview as Review, AccessReviewEntry } from "../api/types";
import {
  CopyId, Empty, ErrorBanner, SectionHead, Skeleton, StatStrip, When,
} from "../components/Bits";

/** §66 — who holds what, as something a person can sign off.
 *
 *  The artefact a SOC 2 access review asks for quarterly. The endpoint has
 *  existed and been tested since ADR-0048; nothing called it, so producing the
 *  review still meant a bearer token and curl. This is the screen.
 *
 *  ## What a reviewer is actually doing
 *
 *  Not reading a list. They are answering three questions in order — who can
 *  move money, who should no longer be here, and does each role still mean
 *  what its name suggests — and the page is arranged as those three questions
 *  rather than as one table of everything.
 *
 *  ## Why `action:` rather than a list of permission names
 *
 *  "Can move money" is derived from the `action:` prefix, not from a hardcoded
 *  set of `action:refund` / `action:recover`. The permission catalogue is
 *  generated from the tool registry, so it grows when a tool is added — and a
 *  list written out here would silently stop being the answer the first time
 *  that happened, while still looking authoritative. The prefix is the
 *  convention the catalogue already uses.
 */
export default function AccessReview() {
  const [review, setReview] = useState<Review | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [filter, setFilter] = useState<Filter>("all");

  useEffect(() => {
    let live = true;
    api.accessReview().then(
      (r) => { if (live) { setReview(r); setError(null); } },
      (e) => { if (live) { setError(e as ApiError); setReview(null); } },
    );
    return () => { live = false; };
  }, []);

  const rows = useMemo(
    () => (review ? review.users.filter((u) => MATCHES[filter](u)) : []),
    [review, filter]);

  // Owner-only, and the server says which. A reviewer who lands here without
  // the role should read why, not a stack of red — being refused is the
  // control working.
  if (error?.status === 403) {
    return (
      <div className="card">
        <SectionHead title="Access review" />
        <p className="sub">
          Reviewing access requires the <span className="mono">owner</span> role.
          The list of who can move money is exactly the reconnaissance a
          read-only token would want, so it is owner-only on purpose.
        </p>
      </div>
    );
  }
  if (error) return <div className="card"><ErrorBanner error={error} /></div>;
  if (!review) {
    return <div className="card"><SectionHead title="Access review" /><Skeleton rows={5} /></div>;
  }

  const active = review.users.filter((u) => u.status === "ACTIVE");
  const offboarded = review.users.filter((u) => u.status !== "ACTIVE");
  const canAct = active.filter(privileged);

  return (
    <>
      <div className="card">
        <SectionHead title="Access review" count={review.tenant_id} />
        <p className="sub">
          Who holds what in this tenant, and what each role grants. Offboarded
          accounts are listed rather than hidden — an account missing from a
          review is indistinguishable from one that never existed.
        </p>
        <StatStrip items={[
          // The as-of is first and is not decoration: a review quoted without
          // one is a review somebody attests to a week after it stopped being
          // true.
          ["As of", <When iso={review.generated_at} />],
          ["People", review.users.length],
          ["Can move money", canAct.length],
          ["Offboarded", offboarded.length],
          ["Roles", review.roles.length],
        ]} />
      </div>

      <div className="card">
        <SectionHead title="People" count={`${rows.length} of ${review.users.length}`}>
          <div className="filters" style={{ margin: 0 }}>
            {(Object.keys(LABELS) as Filter[]).map((f) => (
              <button
                key={f}
                type="button"
                aria-pressed={filter === f}
                onClick={() => setFilter(f)}
              >
                {LABELS[f]}
              </button>
            ))}
          </div>
        </SectionHead>

        {rows.length === 0 ? (
          <Empty>{EMPTY[filter]}</Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>User</th><th>Email</th><th>Merchant</th><th>Role</th>
                  <th>Permissions</th><th>Status</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((u) => (
                  <tr key={u.user_id}>
                    <td><CopyId value={u.user_id} label="user id" /></td>
                    <td className="mono">{u.email}</td>
                    <td className="mono">{u.merchant_id}</td>
                    <td>
                      {u.role}
                      {privileged(u)
                        ? <span className="pill warn" style={{ marginLeft: 6 }}>can act</span>
                        : null}
                    </td>
                    <td style={{ maxWidth: 340 }}>
                      <Perms names={u.permissions} />
                    </td>
                    <td>
                      {u.status === "ACTIVE"
                        ? <span className="pill ok">active</span>
                        : (
                          <>
                            <span className="pill neutral">{u.status.toLowerCase()}</span>
                            {/* When access was removed is half the answer. A
                                disabled row without a date cannot be reviewed:
                                "still disabled" and "disabled this morning"
                                are different findings. */}
                            {u.deactivated_at ? (
                              <div className="muted" style={{ fontSize: 11 }}>
                                <When iso={u.deactivated_at} />
                              </div>
                            ) : null}
                          </>
                        )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="card">
        <SectionHead title="Roles" count={review.roles.length} />
        <p className="sub">
          What granting a role actually means. A reviewer approving “make them
          an approver” is approving this list, not the word.
        </p>
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Role</th><th>Grants</th><th>Held by</th></tr>
            </thead>
            <tbody>
              {review.roles.map((r) => {
                const held = review.users.filter(
                  (u) => u.role === r.name && u.status === "ACTIVE").length;
                return (
                  <tr key={r.name}>
                    <td>
                      {r.name}
                      {r.permissions.some(isAction)
                        ? <span className="pill warn" style={{ marginLeft: 6 }}>can act</span>
                        : null}
                    </td>
                    <td><Perms names={r.permissions} /></td>
                    {/* Active holders only. A role held by nobody is a role to
                        question; counting disabled accounts would hide that. */}
                    <td>{held === 0
                      ? <span className="muted">nobody</span>
                      : held}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}

/** A write permission. The catalogue is generated from the tool registry, so
 *  this is a prefix rule rather than a list that would go quietly stale the
 *  first time a tool was added. */
function isAction(p: string): boolean {
  return p.startsWith("action:");
}

function privileged(u: AccessReviewEntry): boolean {
  return u.permissions.some(isAction);
}

/** Permissions read as a set, not a sentence, and the ones that move money are
 *  what the eye should land on. */
function Perms({ names }: { names: string[] }) {
  if (names.length === 0) return <span className="muted">none</span>;
  return (
    <span className="perms">
      {names.map((p) => (
        <code key={p} className={isAction(p) ? "perm act" : "perm"}>{p}</code>
      ))}
    </span>
  );
}

type Filter = "all" | "acting" | "offboarded";

const LABELS: Record<Filter, string> = {
  all: "Everyone",
  acting: "Can move money",
  offboarded: "Offboarded",
};

const MATCHES: Record<Filter, (u: AccessReviewEntry) => boolean> = {
  all: () => true,
  acting: (u) => u.status === "ACTIVE" && privileged(u),
  offboarded: (u) => u.status !== "ACTIVE",
};

const EMPTY: Record<Filter, string> = {
  all: "This tenant has no users.",
  acting: "Nobody active holds a permission that moves money.",
  offboarded: "Nobody has been offboarded.",
};
