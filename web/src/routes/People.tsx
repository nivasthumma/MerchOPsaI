import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { MerchantView, RoleList, UserCreated, UserSummary } from "../api/types";
import {
  Busy, CopyId, Empty, ErrorBanner, SectionHead, Skeleton, StatStrip,
} from "../components/Bits";
import { ShownOnce } from "../components/ShownOnce";
import { useToast } from "../components/Toast";

/** Joiner, mover, leaver — §43.
 *
 *  ADR-0048 built all three over the API and nothing called them, so bringing
 *  somebody onto a merchant meant a bearer token and curl. This is the screen.
 *
 *  ## The refusals are the feature
 *
 *  The server guards the last active owner: it will not let the final one be
 *  demoted or disabled, because a merchant nobody can administer is a merchant
 *  that needs a database console to recover. Those refusals arrive as 409s
 *  with a code, and they are shown as **refusals** rather than errors — the
 *  control working, not the software failing.
 *
 *  ## Sign out is not disable
 *
 *  Two different acts, next to each other, and easy to confuse:
 *
 *    sign out   ends every live session; the account still works
 *    disable    ends the account; ADR-0048 makes their tokens stop resolving
 *
 *  The first is what you do when a laptop is lost. The second is what you do
 *  when somebody leaves. Labelling them the same way is how the wrong one gets
 *  pressed on a Friday afternoon.
 */
export default function People() {
  const [users, setUsers] = useState<UserSummary[] | null>(null);
  const [roles, setRoles] = useState<RoleList | null>(null);
  const [merchants, setMerchants] = useState<MerchantView[] | null>(null);
  const [merchantName, setMerchantName] = useState("");
  const [error, setError] = useState<ApiError | null>(null);
  const [refusal, setRefusal] = useState<ApiError | null>(null);
  const [created, setCreated] = useState<UserCreated | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("analyst");
  const toast = useToast();

  async function load() {
    try {
      const [u, r, m] = await Promise.all([
        api.users(true), api.roles(), api.merchants()]);
      setUsers(u.users);
      setRoles(r);
      setMerchants(m.merchants);
      setError(null);
    } catch (e) {
      setError(e as ApiError);
    }
  }

  useEffect(() => { void load(); }, []);

  /** Every mutation goes through here, so the refusal path is written once. */
  async function act(key: string, run: () => Promise<unknown>, done: string) {
    setBusy(key);
    setRefusal(null);
    try {
      await run();
      toast({ tone: "ok", title: done });
      await load();
    } catch (e) {
      const err = e as ApiError;
      // A guard firing is not a fault. 409 is the last-owner protection and
      // the duplicate-email check; both are the system doing its job.
      if (err.status === 409 || err.status === 400) setRefusal(err);
      else setError(err);
    } finally {
      setBusy(null);
    }
  }

  if (error?.status === 403) {
    return (
      <div className="card">
        <SectionHead title="People" />
        <p className="sub">
          Administering people requires the <span className="mono">owner</span>{" "}
          role. Creating an account mints a credential and changing a role can
          grant the ability to move money, so it is owner-only on purpose.
        </p>
      </div>
    );
  }
  if (error) return <div className="card"><ErrorBanner error={error} /></div>;
  if (!users || !roles || !merchants) {
    return <div className="card"><SectionHead title="People" /><Skeleton rows={4} /></div>;
  }

  const active = users.filter((u) => u.status === "ACTIVE");
  const owners = active.filter((u) => u.role === "owner");

  return (
    <>
      <div className="card">
        <SectionHead title="People" count={users.length} />
        <p className="sub">
          Who can sign in to this merchant, and what their role lets them do.
          Disabled accounts stay listed — an account missing from this page is
          indistinguishable from one that never existed.
        </p>
        <StatStrip items={[
          ["Active", active.length],
          ["Disabled", users.length - active.length],
          // The number the last-owner guard exists to protect. At one, the
          // server will refuse to demote or disable the remaining owner.
          ["Owners", owners.length],
        ]} />
      </div>

      <div className="card">
        <SectionHead title="Merchants" count={merchants.length} />
        <p className="sub">
          A tenant owns one or more merchants (§11). Adding one here puts it in
          your own tenant — the tenant is taken from your session, never from
          the request.
        </p>
        {/* Stated plainly rather than left to be discovered. Somebody looking
            for "add a customer" needs to know it is not missing, it is
            somewhere else and for a reason. */}
        <p className="sub">
          A new <em>customer</em> — a whole tenant with its own first owner — is
          created by an operator running{" "}
          <span className="mono">scripts/onboard_tenant.py</span>. It mints the
          first owner of a tenant nobody administers yet, and no role in this
          system can authorise that.
        </p>
        <form
          className="row-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (!merchantName.trim()) return;
            void act("merchant", async () => {
              await api.createMerchant(merchantName.trim());
              setMerchantName("");
            }, "Merchant created");
          }}
        >
          <label>
            <span>Merchant name</span>
            <input
              value={merchantName}
              required
              placeholder="Kettle Espresso"
              onChange={(e) => setMerchantName(e.target.value)}
            />
          </label>
          <button type="submit" className="primary" disabled={busy === "merchant"}>
            {busy === "merchant" ? <Busy>creating</Busy> : "Add merchant"}
          </button>
        </form>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Merchant</th><th>Name</th><th>Currency</th></tr></thead>
            <tbody>
              {merchants.map((m) => (
                <tr key={m.merchant_id}>
                  <td><CopyId value={m.merchant_id} label="merchant id" /></td>
                  <td>{m.name}</td>
                  <td className="mono">{m.currency}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card">
        <SectionHead title="Add someone" />
        <p className="sub">
          Creates the account and mints its first token. The token is returned
          once and is not retrievable afterwards.
        </p>
        {refusal ? <ErrorBanner error={refusal} /> : null}
        {created ? (
          <ShownOnce
            label={`Token for ${created.email}`}
            value={created.token}
            what="this account's token"
            onDismiss={() => setCreated(null)}
          />
        ) : null}
        <form
          className="row-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (!email.trim()) return;
            void act("create", async () => {
              const c = await api.createUser(email.trim(), role);
              setCreated(c);
              setEmail("");
            }, "Account created");
          }}
        >
          <label>
            <span>Email</span>
            <input
              type="email"
              value={email}
              required
              placeholder="someone@example.com"
              onChange={(e) => setEmail(e.target.value)}
            />
          </label>
          <label>
            <span>Role</span>
            <select value={role} onChange={(e) => setRole(e.target.value)}>
              {roles.roles.map((r) => (
                <option key={r.name} value={r.name}>{r.name}</option>
              ))}
            </select>
          </label>
          <button type="submit" className="primary" disabled={busy === "create"}>
            {busy === "create" ? <Busy>creating</Busy> : "Create account"}
          </button>
        </form>
      </div>

      <div className="card">
        <SectionHead title="Accounts" count={users.length} />
        {users.length === 0 ? (
          <Empty>Nobody has an account on this merchant.</Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>User</th><th>Email</th><th>Role</th><th>Status</th><th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {users.map((u) => {
                  const isActive = u.status === "ACTIVE";
                  // Surfaced BEFORE the server refuses, so the disabled control
                  // carries the reason rather than the click producing one.
                  const lastOwner = isActive && u.role === "owner" && owners.length === 1;
                  return (
                    <tr key={u.user_id}>
                      <td><CopyId value={u.user_id} label="user id" /></td>
                      <td className="mono">{u.email}</td>
                      <td>
                        <select
                          value={u.role}
                          // Named for the person it changes. Without this a
                          // screen reader announces "combobox" on every row and
                          // the table becomes a list of identical controls --
                          // caught by the axe scan, which is the only reader
                          // here that was ever going to notice.
                          aria-label={`Role for ${u.email}`}
                          disabled={!isActive || lastOwner || busy === u.user_id}
                          title={lastOwner
                            ? "The last active owner cannot be demoted"
                            : undefined}
                          onChange={(e) => void act(
                            u.user_id,
                            () => api.updateUser(u.user_id, { role: e.target.value }),
                            `${u.email} is now ${e.target.value}`)}
                        >
                          {roles.roles.map((r) => (
                            <option key={r.name} value={r.name}>{r.name}</option>
                          ))}
                        </select>
                      </td>
                      <td>
                        {isActive
                          ? <span className="pill ok">active</span>
                          : <span className="pill neutral">disabled</span>}
                      </td>
                      <td className="row-actions">
                        {isActive ? (
                          <>
                            {/* Ends sessions, keeps the account. What you do
                                when a laptop goes missing. */}
                            <button
                              type="button"
                              disabled={busy === u.user_id}
                              onClick={() => void act(
                                u.user_id,
                                () => api.signOutUser(u.user_id),
                                `Signed ${u.email} out everywhere`)}
                            >
                              Sign out
                            </button>
                            <button
                              type="button"
                              className="danger"
                              disabled={lastOwner || busy === u.user_id}
                              title={lastOwner
                                ? "The last active owner cannot be disabled"
                                : undefined}
                              onClick={() => {
                                if (!confirm(
                                  `Disable ${u.email}? Their tokens stop working `
                                  + `immediately. This is the leaver step, not a `
                                  + `sign-out.`)) return;
                                void act(u.user_id,
                                  () => api.updateUser(u.user_id, { status: "DISABLED" }),
                                  `${u.email} disabled`);
                              }}
                            >
                              Disable
                            </button>
                          </>
                        ) : (
                          <button
                            type="button"
                            disabled={busy === u.user_id}
                            onClick={() => void act(
                              u.user_id,
                              () => api.updateUser(u.user_id, { status: "ACTIVE" }),
                              `${u.email} re-enabled`)}
                          >
                            Re-enable
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}
