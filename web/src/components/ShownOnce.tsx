import { useState } from "react";

/** A credential the server will never show again.
 *
 *  Two endpoints return one: `POST /users` hands back the new account's token,
 *  and `POST /scim/tokens` hands back a provisioning token. Neither is stored
 *  in a form anybody can read back — the database keeps a digest — so the
 *  moment this renders is the only moment the value exists outside the
 *  response.
 *
 *  ## Why it does not look like the rest of the page
 *
 *  Everything else here can be re-read by reloading. This cannot, and a panel
 *  styled like every other panel invites the habit that works everywhere else:
 *  glance, move on, come back later. Coming back later means the account has to
 *  be recreated or the token reissued.
 *
 *  So it holds its own space, states the consequence in words rather than
 *  implying it, and the dismiss control says what dismissing costs. The copy
 *  button is the primary action because copying is the thing that has to
 *  happen before anything else does.
 */
export function ShownOnce(
  { label, value, what, onDismiss }:
  { label: string; value: string; what: string; onDismiss: () => void },
) {
  const [copied, setCopied] = useState(false);

  return (
    <div className="once" role="alert">
      <div className="once-head">
        <strong>{label}</strong>
        <span className="pill warn">shown once</span>
      </div>

      <p className="once-why">
        This is the only time {what} is readable. The database keeps a digest,
        not the value, so it cannot be shown again — copy it now, or it has to
        be reissued.
      </p>

      <div className="once-value">
        <code className="mono">{value}</code>
        <button
          type="button"
          className="primary"
          onClick={() => {
            void navigator.clipboard?.writeText(value).then(
              () => { setCopied(true); setTimeout(() => setCopied(false), 2000); },
              () => { /* clipboard blocked — the value is selectable on screen */ },
            );
          }}
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </div>

      {/* Named for what it costs, not "Close". A button labelled "Close" beside
          a value that cannot be recovered is a button people press to tidy up. */}
      <button type="button" className="once-dismiss" onClick={onDismiss}>
        I have copied it — hide
      </button>
    </div>
  );
}
