import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { RoleList, ScimTokenCreated, ScimTokenSummary, SsoConfig } from "../api/types";
import {
  Busy, CopyId, Empty, ErrorBanner, SectionHead, Skeleton, When,
} from "../components/Bits";
import { ShownOnce } from "../components/ShownOnce";
import { useToast } from "../components/Toast";

/** Where accounts come from, and who is allowed to create them — §43.
 *
 *  Two halves of one question, which is why they share a page:
 *
 *    SSO (ADR-0050)    how a person proves who they are at sign-in
 *    SCIM (ADR-0051)   how their account arrives and leaves without anybody
 *                      remembering to do it
 *
 *  ## What is deliberately not here
 *
 *  The client secret. `GET /sso` does not return it, and this page does not
 *  ask for it back to re-save the rest — the form sends only what it shows.
 *  It is encrypted at rest (ADR-0052), and a screen that round-trips a
 *  credential through a browser to change an unrelated field is a screen that
 *  puts it in a form, a history entry and a heap dump for no reason.
 *
 *  ## A revoked provisioning token is kept, not deleted
 *
 *  `DELETE /scim/tokens/{id}` marks it revoked. The row stays because "this
 *  token was used until the 4th and then withdrawn" is the question an auditor
 *  asks, and a deleted row answers it with silence.
 */
export default function Identity() {
  const [sso, setSso] = useState<SsoConfig | null>(null);
  const [tokens, setTokens] = useState<ScimTokenSummary[] | null>(null);
  const [roles, setRoles] = useState<RoleList | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [refusal, setRefusal] = useState<ApiError | null>(null);
  const [minted, setMinted] = useState<ScimTokenCreated | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [tokenRole, setTokenRole] = useState("analyst");
  const toast = useToast();

  async function load() {
    try {
      const [s, t, r] = await Promise.all([api.sso(), api.scimTokens(), api.roles()]);
      setSso(s);
      setTokens(t.tokens);
      setRoles(r);
      setError(null);
    } catch (e) {
      setError(e as ApiError);
    }
  }

  useEffect(() => { void load(); }, []);

  async function act(key: string, run: () => Promise<unknown>, done: string) {
    setBusy(key);
    setRefusal(null);
    try {
      await run();
      toast({ tone: "ok", title: done });
      await load();
    } catch (e) {
      const err = e as ApiError;
      if (err.status === 409 || err.status === 400) setRefusal(err);
      else setError(err);
    } finally {
      setBusy(null);
    }
  }

  if (error?.status === 403) {
    return (
      <div className="card">
        <SectionHead title="Identity" />
        <p className="sub">
          Identity settings require the <span className="mono">owner</span>{" "}
          role. A provisioning token creates accounts, so it is owner-only on
          purpose.
        </p>
      </div>
    );
  }
  if (error) return <div className="card"><ErrorBanner error={error} /></div>;
  if (!sso || !tokens || !roles) {
    return <div className="card"><SectionHead title="Identity" /><Skeleton rows={4} /></div>;
  }

  const live = tokens.filter((t) => !t.revoked);

  return (
    <>
      <div className="card">
        <SectionHead title="Single sign-on" count={sso.configured ? "configured" : "not set up"} />
        {sso.configured ? (
          <>
            <p className="sub">
              People in these domains sign in through your identity provider.
              The client secret is not shown here and is encrypted at rest.
            </p>
            {/* Direct dt/dd children: `.kv` is a two-column grid and wrapping
                each pair in a div would collapse it into one column. */}
            <dl className="kv">
              <dt>Issuer</dt><dd>{sso.issuer}</dd>
              <dt>Client ID</dt><dd>{sso.client_id}</dd>
              <dt>Email domains</dt>
              <dd>
                {sso.email_domains.length === 0
                  ? <span className="muted">none</span>
                  : <span className="perms">
                      {sso.email_domains.map((d) => (
                        <code className="perm" key={d}>{d}</code>))}
                    </span>}
              </dd>
              <dt>New accounts get</dt>
              <dd>{sso.default_role ?? <span className="muted">no default</span>}</dd>
              <dt>Status</dt>
              <dd>{sso.enabled
                ? <span className="pill ok">enabled</span>
                : <span className="pill neutral">disabled</span>}</dd>
            </dl>
          </>
        ) : (
          <>
            <p className="sub">
              Nobody signs in through an identity provider yet — every account
              uses a bearer token this system issued.
            </p>
            {/* Deliberately not a form. Configuring SSO needs the client secret,
                and the honest place to hand a credential to a server is not a
                field on a settings page nobody has secured against a browser
                extension. */}
            <Empty>
              Set it up with <span className="mono">PUT /sso</span>, which takes
              the issuer, client ID and secret. The secret is written once and
              never read back, so it is not editable from this page by design.
            </Empty>
          </>
        )}
      </div>

      <div className="card">
        <SectionHead title="Provisioning tokens" count={`${live.length} live`} />
        <p className="sub">
          What your identity provider presents when it creates, updates or
          deactivates an account over SCIM. Revoked tokens stay listed — when a
          token stopped being trusted is part of the record.
        </p>
        {refusal ? <ErrorBanner error={refusal} /> : null}
        {minted ? (
          <ShownOnce
            label={`Provisioning token “${minted.name}”`}
            value={minted.token}
            what="this provisioning token"
            onDismiss={() => setMinted(null)}
          />
        ) : null}

        <form
          className="row-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (!name.trim()) return;
            void act("mint", async () => {
              setMinted(await api.createScimToken(name.trim(), tokenRole, ""));
              setName("");
            }, "Provisioning token created");
          }}
        >
          <label>
            <span>Label</span>
            <input
              value={name}
              required
              placeholder="Okta production"
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label>
            <span>Accounts it creates get</span>
            <select value={tokenRole} onChange={(e) => setTokenRole(e.target.value)}>
              {roles.roles.map((r) => (
                <option key={r.name} value={r.name}>{r.name}</option>
              ))}
            </select>
          </label>
          <button type="submit" className="primary" disabled={busy === "mint"}>
            {busy === "mint" ? <Busy>creating</Busy> : "Create token"}
          </button>
        </form>

        {tokens.length === 0 ? (
          <Empty>No provisioning tokens. Accounts are created by hand.</Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Token</th><th>Label</th><th>Creates</th>
                  <th>Last used</th><th>Status</th><th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {tokens.map((t) => (
                  <tr key={t.id}>
                    <td><CopyId value={t.id} label="token id" /></td>
                    <td>{t.name}</td>
                    <td>{t.default_role}</td>
                    <td>
                      {/* Never used is a finding: a provisioning token nobody
                          presented is either misconfigured at the IdP or was
                          minted and forgotten. Both want noticing. */}
                      {t.last_used_at
                        ? <When iso={t.last_used_at} />
                        : <span className="muted">never used</span>}
                    </td>
                    <td>
                      {t.revoked
                        ? <span className="pill neutral">revoked</span>
                        : <span className="pill ok">live</span>}
                    </td>
                    <td className="row-actions">
                      {t.revoked ? null : (
                        <button
                          type="button"
                          className="danger"
                          disabled={busy === t.id}
                          onClick={() => {
                            if (!confirm(
                              `Revoke “${t.name}”? Your identity provider stops `
                              + `being able to create or deactivate accounts with `
                              + `it immediately.`)) return;
                            void act(t.id, () => api.revokeScimToken(t.id),
                                     `“${t.name}” revoked`);
                          }}
                        >
                          Revoke
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}
