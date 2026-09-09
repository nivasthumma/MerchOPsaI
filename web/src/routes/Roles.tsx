import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { RoleList } from "../api/types";
import {
  Busy, Empty, ErrorBanner, SectionHead, Skeleton, StatStrip,
} from "../components/Bits";
import { useToast } from "../components/Toast";

/** What each role grants — §43.
 *
 *  Permissions became rows in ADR-0047 and the catalogue is derived from the
 *  tool registry, so this page cannot drift from what the tools actually check:
 *  a permission appears here because a tool declares it.
 *
 *  ## The checkbox that moves money
 *
 *  `action:` permissions are the ones that let a role act rather than read.
 *  Granting one to a role held by nine people grants it to nine people, and the
 *  page says so on the control rather than in a heading somewhere above it.
 *
 *  Derived from the prefix rather than a list of names, for the same reason as
 *  the access review: the catalogue grows when a tool is added, and a hardcoded
 *  list would keep looking authoritative while quietly going stale.
 *
 *  ## Changes are staged, not live
 *
 *  Ticking a box does not send anything. `PUT /roles/{name}/permissions` takes
 *  the whole set, so a click-to-save control would make every intermediate
 *  state a real grant — including the half-second where somebody has removed
 *  read access and not yet added it back.
 */
export default function Roles() {
  const [data, setData] = useState<RoleList | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [refusal, setRefusal] = useState<ApiError | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  async function load() {
    try {
      setData(await api.roles());
      setError(null);
    } catch (e) {
      setError(e as ApiError);
    }
  }

  useEffect(() => { void load(); }, []);

  if (error?.status === 403) {
    return (
      <div className="card">
        <SectionHead title="Roles" />
        <p className="sub">
          Administering roles requires the <span className="mono">owner</span>{" "}
          role. A permission change reaches everybody holding the role, so it is
          owner-only on purpose.
        </p>
      </div>
    );
  }
  if (error) return <div className="card"><ErrorBanner error={error} /></div>;
  if (!data) {
    return <div className="card"><SectionHead title="Roles" /><Skeleton rows={4} /></div>;
  }

  const acting = data.roles.filter((r) => r.permissions.some(isAction));

  function startEditing(name: string, permissions: string[]) {
    setEditing(name);
    setDraft(new Set(permissions));
    setRefusal(null);
  }

  async function save(name: string) {
    setBusy(true);
    setRefusal(null);
    try {
      const change = await api.setRolePermissions(name, [...draft].sort());
      const moved = change.granted.length + change.revoked.length;
      toast({
        tone: change.granted.some(isAction) ? "warn" : "ok",
        title: moved === 0 ? "No change" : `${name} updated`,
        body: moved === 0
          ? "The permission set was already what you saved."
          : [change.granted.length ? `granted ${change.granted.join(", ")}` : "",
             change.revoked.length ? `revoked ${change.revoked.join(", ")}` : ""]
            .filter(Boolean).join(" · "),
      });
      setEditing(null);
      await load();
    } catch (e) {
      const err = e as ApiError;
      if (err.status === 409 || err.status === 400) setRefusal(err);
      else setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="card">
        <SectionHead title="Roles" count={data.roles.length} />
        <p className="sub">
          What granting a role actually means. Somebody approving “make them an
          approver” is approving this list, not the word.
        </p>
        <StatStrip items={[
          ["Roles", data.roles.length],
          ["Can move money", acting.length],
          ["Permissions", data.catalogue.length],
        ]} />
        {refusal ? <ErrorBanner error={refusal} /> : null}
      </div>

      {data.roles.length === 0 ? (
        <div className="card"><Empty>This tenant has no roles.</Empty></div>
      ) : data.roles.map((r) => {
        const open = editing === r.name;
        const shown = open ? [...draft] : r.permissions;
        return (
          <div className="card" key={r.name}>
            <SectionHead title={r.name} count={`held by ${r.held_by || "nobody"}`}>
              {open ? (
                <>
                  <button type="button" onClick={() => setEditing(null)}>Cancel</button>
                  <button type="button" className="primary" disabled={busy}
                          onClick={() => void save(r.name)}>
                    {busy ? <Busy>saving</Busy> : "Save permissions"}
                  </button>
                </>
              ) : (
                <button type="button"
                        onClick={() => startEditing(r.name, r.permissions)}>
                  Edit permissions
                </button>
              )}
            </SectionHead>

            <p className="sub">{r.description}</p>

            {/* Stated on the role, where the decision is being made. A role
                that can act is the one worth pausing over. */}
            {shown.some(isAction) ? (
              <p className="sub">
                <span className="pill warn">can move money</span>{" "}
                {r.held_by > 0
                  ? `${r.held_by} ${r.held_by === 1 ? "person holds" : "people hold"} this role.`
                  : "Nobody holds this role."}
              </p>
            ) : null}

            <ul className="perm-grid">
              {data.catalogue.map((p) => {
                const on = open ? draft.has(p.name) : r.permissions.includes(p.name);
                return (
                  <li key={p.name} className={isAction(p.name) ? "perm-row act" : "perm-row"}>
                    <label>
                      <input
                        type="checkbox"
                        checked={on}
                        disabled={!open}
                        onChange={(e) => {
                          const next = new Set(draft);
                          if (e.target.checked) next.add(p.name);
                          else next.delete(p.name);
                          setDraft(next);
                        }}
                      />
                      <code className={isAction(p.name) ? "perm act" : "perm"}>
                        {p.name}
                      </code>
                    </label>
                    <span className="muted">{p.description}</span>
                  </li>
                );
              })}
            </ul>
          </div>
        );
      })}
    </>
  );
}

/** A write permission. Prefix, not a list — the catalogue comes from the tool
 *  registry and grows when a tool is added. */
function isAction(p: string): boolean {
  return p.startsWith("action:");
}
