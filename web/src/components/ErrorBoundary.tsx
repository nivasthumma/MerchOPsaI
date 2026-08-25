import { isRouteErrorResponse, useRouteError } from "react-router-dom";

/** What a viewer sees when the app itself breaks.
 *
 * Without this, React Router's default page appears — stack frames, bundle
 * URLs, and a note addressed to the developer. That page has already been seen
 * once in this project, when `verification_detail` turned out to be a dict.
 *
 * The error is still shown rather than hidden: a control plane that swallows
 * its own failure teaches people to distrust everything else it says. What
 * changes is that it is framed, recoverable, and honest about what is not
 * affected — nothing here reaches the backend, so no financial state moved.
 */
export function AppErrorBoundary() {
  const error = useRouteError();
  const detail = isRouteErrorResponse(error)
    ? `${error.status} ${error.statusText}`
    : error instanceof Error
      ? error.message
      : String(error);

  return (
    <div className="shell">
      <div className="card" style={{ borderColor: "var(--danger-border)" }}>
        <h1 style={{ marginTop: 0, fontSize: 20 }}>This page failed to render</h1>
        <p className="sub">
          The failure is in the interface, not in the agent. Nothing on this screen
          reaches the backend, so no task, approval or financial state changed because of
          it.
        </p>
        <pre>{detail}</pre>
        {error instanceof Error && error.stack ? (
          <details>
            <summary>stack</summary>
            <pre>{error.stack}</pre>
          </details>
        ) : null}
        <div className="row" style={{ marginTop: 16 }}>
          <button className="primary" onClick={() => window.location.reload()}>Reload</button>
          <a href="/">Back to Investigate</a>
        </div>
      </div>
    </div>
  );
}
