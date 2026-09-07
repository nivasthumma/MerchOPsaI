// The five browser journeys — MerchantOps §22.
//
//   A  revenue degradation   detection → incident → investigation → impact
//   B  approval              candidate → policy → approve → execute → verify
//   C  rejection             candidate → approval → reject → ZERO external action
//   D  UNKNOWN               execute → inconclusive → reverify → confirm/escalate
//   E  replay                completed task → playback → ZERO external calls
//
// What these can catch that nothing else in this repository can: the seams.
// The scenario suite grades the agent, the integration tests exercise the API,
// Vitest renders components against captured fixtures. Every one of those can
// pass while a route renders nothing, a field is read under a name the server
// stopped sending, or a button posts somewhere that moved. That failure only
// appears when a real browser drives a real API over a real database.
//
// Two rules these tests follow, and they are the same rules the product does:
//
//   Nothing is asserted about money that the SERVER did not say. Where a test
//   checks a refund happened, it checks the verification state the API
//   returned — not a green tick, which is a rendering of a claim rather than
//   the claim.
//
//   C and E assert a NEGATIVE, and those are the two most valuable here. "No
//   external call was made" is exactly the property a UI bug can silently
//   violate, and exactly the one no amount of rendering looks wrong for.

import { expect, test, type Page, type APIRequestContext } from "@playwright/test";

const API = process.env.E2E_API ?? "http://127.0.0.1:8000";

/** The bearer token the app expects, minted the way `make token` does.
 *
 *  Read from the environment rather than derived here: deriving it would mean
 *  reimplementing the HMAC, and a test that mints its own credentials is a test
 *  that can pass against an auth scheme the application has stopped using. */
const TOKEN = process.env.E2E_TOKEN ?? "";

test.beforeAll(() => {
  if (!TOKEN) {
    throw new Error(
      "E2E_TOKEN is not set. These drive a real API and a real database:\n"
      + "  make seed\n"
      + "  make api &\n"
      + "  E2E_TOKEN=$(make token USER_ID=USR_A_OWNER) make e2e\n");
  }
});

/** Sign in the way the app does — the token goes in localStorage and nowhere
 *  else, so this is the whole of authentication from the browser's side. */
async function signIn(page: Page) {
  await page.addInitScript((t) => {
    window.localStorage.setItem("merchantops.token", t);
  }, TOKEN);
}

/** Ask the API directly. Used only to establish preconditions and to check
 *  facts the UI is not the authority on — never to assert what the UI showed. */
async function api(request: APIRequestContext, path: string, method = "GET") {
  const res = await request.fetch(`${API}${path}`, {
    method,
    headers: { Authorization: `Bearer ${TOKEN}` },
  });
  expect(res.ok(), `${method} ${path} → ${res.status()}`).toBeTruthy();
  return res.json();
}

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

// ---------------------------------------------------------------- journey A
test("A — a revenue degradation becomes an incident an operator can read", async ({
  page, request,
}) => {
  await api(request, "/incidents/detect", "POST");

  await page.goto("/");
  // The Command Center is the home screen (P0-05) and must answer "what needs
  // my attention" without navigation.
  await expect(page.getByRole("heading", { name: "Command Center" })).toBeVisible();
  await expect(page.getByText("Needs attention")).toBeVisible();

  await page.getByRole("link", { name: /Open incidents/ }).click();
  await expect(page).toHaveURL(/\/incidents/);

  const degradation = page.getByRole("link", { name: /payment degradation/i }).first();
  await expect(degradation).toBeVisible();
  await degradation.click();

  // P0-07: the page is ordered as the decision is made, not as the data model
  // is shaped. That ordering IS the deliverable, so it is what is asserted.
  const sections = page.getByRole("heading", { level: 3 });
  await expect(sections.nth(0)).toHaveText("What happened");
  await expect(sections.nth(1)).toHaveText("Why we believe it");
  await expect(sections.nth(2)).toHaveText("Business impact");
  await expect(sections.nth(3)).toHaveText("Recovery recommendation");

  // §12's metadata: the rule, and the numbers that tripped it. Scoped to the
  // section that owns them — "Baseline" also appears in the evidence rows and
  // in the recovery basis, and an unscoped match is a test that passes on the
  // wrong element.
  const whatHappened = page.locator("section.card").filter({
    has: page.getByRole("heading", { name: "What happened" }),
  });
  await expect(whatHappened.getByText("Baseline", { exact: true })).toBeVisible();
  await expect(whatHappened.getByText("Observed", { exact: true })).toBeVisible();
  await expect(whatHappened.getByText("Threshold", { exact: true })).toBeVisible();

  // And the impact is money, computed by the control plane.
  const impact = page.locator("section.card").filter({
    has: page.getByRole("heading", { name: "Business impact" }),
  });
  await expect(impact.getByText("Revenue at risk")).toBeVisible();
});

// ---------------------------------------------------------------- journey B
test("B — policy gates a refund, a human approves, the provider is read back", async ({
  page, request,
}) => {
  const created = await request.post(`${API}/tasks`, {
    headers: { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json" },
    data: { request: "Refund the duplicate payment SYN_PAY_0002 amount 499900." },
  });
  expect(created.ok()).toBeTruthy();
  const task = await created.json();

  // The gate is the point: nothing has reached the provider yet.
  expect(task.status).toBe("AWAITING_APPROVAL");
  expect(task.actions).toHaveLength(0);

  await page.goto("/actions?section=awaiting_approval");
  await expect(page.getByRole("heading", { name: /Awaiting approval/ })).toBeVisible();
  await expect(page.getByText(task.approvals[0].id)).toBeVisible();

  // P0-03: approving is not offered from the queue. It happens next to the
  // evidence, which is a product decision worth pinning.
  await expect(page.getByRole("button", { name: /^Approve$/ })).toHaveCount(0);
  await page.getByRole("link", { name: /Review evidence/ }).first().click();
  await expect(page).toHaveURL(new RegExp(`/tasks/${task.id}`));

  // P0-08: what ran, from recorded rows.
  await expect(page.getByText("Investigation started")).toBeVisible();
  await expect(page.getByText("Waiting for approval")).toBeVisible();

  // Approving is TWO clicks, and that is the product being right rather than
  // the test being awkward: the first arms the button and relabels it
  // "Confirm — this moves money", the second commits. A single-click approve
  // on a page like this is how somebody refunds by reflex.
  const approve = page.getByRole("button", { name: "Approve and execute" });
  await approve.click();
  await expect(
    page.getByRole("button", { name: /Confirm — this moves money/ })
  ).toBeVisible();
  await page.getByRole("button", { name: /Confirm — this moves money/ }).click();

  // The server is the authority on whether money moved — not the tick.
  await expect
    .poll(async () => (await api(request, `/tasks/${task.id}`)).actions[0]
      ?.verification_state, { timeout: 20_000 })
    .toBe("SUCCESS");

  await page.goto(`/payments/SYN_PAY_0002`);
  await expect(page.getByRole("heading", { name: /Payment SYN_PAY_0002/ })).toBeVisible();
  // §7: the whole chain, including the independent read-back.
  await expect(page.getByText("Verification: SUCCESS")).toBeVisible();
  await expect(page.getByText(/Refund sent to provider/)).toBeVisible();
});

// ---------------------------------------------------------------- journey C
test("C — a rejected candidate makes ZERO external calls", async ({ page, request }) => {
  const before = await api(request, "/metrics");

  const created = await request.post(`${API}/tasks`, {
    headers: { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json" },
    data: { request: "Refund the duplicate payment SYN_PAY_0011 amount 129900." },
  });
  const task = await created.json();
  // Not skipped if it fails to gate: a rejection journey against a task that
  // was never gated proves nothing, so an ungated task is a failure of the
  // fixture and should say so.
  expect(task.status, "this payment must reach the policy gate")
    .toBe("AWAITING_APPROVAL");

  await page.goto(`/tasks/${task.id}`);
  await page.getByRole("button", { name: /Reject/ }).first().click();

  // Polled, not read once. A click fires an asynchronous POST, and asserting
  // on the next line is a race that passes whenever the machine is fast enough
  // — which is most of the time, and is why this was green twice before it was
  // red. A flaky end-to-end test is worse than none: it teaches people to
  // re-run rather than to look.
  await expect
    .poll(async () => (await api(request, `/tasks/${task.id}`)).status,
          { timeout: 15_000 })
    .toBe("REJECTED");

  // The assertion that matters, and the one only a negative can make: no
  // action row exists, so nothing was ever sent.
  const after = await api(request, `/tasks/${task.id}`);
  expect(after.actions).toHaveLength(0);

  const metrics = await api(request, "/metrics");
  expect(metrics.moved_minor).toBe(before.moved_minor);
});

// ---------------------------------------------------------------- journey D
test("D — an UNKNOWN outcome is presented as unresolved work, not as a failure",
  async ({ page, request }) => {
  const center = await api(request, "/actions");
  // Planted by `scripts/run_e2e.sh` through the real execution path with the
  // timeout injector. Asserted rather than skipped: a journey that quietly
  // skips is a journey nobody notices has stopped running.
  expect(center.counts.unknown, "run via scripts/run_e2e.sh, which plants one")
    .toBeGreaterThan(0);

  await page.goto("/actions?section=unknown");

  // P0-04: the queue is WORK, so the row carries what is needed to act.
  await expect(page.getByRole("columnheader", { name: "Attempts" })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "Next retry" })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "Last check" })).toBeVisible();

  // UNKNOWN never reads as a failure, and never as a success.
  const pill = page.locator(".pill.unknown").first();
  await expect(pill).toBeVisible();
  await expect(page.locator(".pill.ok")).toHaveCount(0);

  // And re-verifying reports what it FOUND (P1-14), not that it ran.
  await page.getByRole("button", { name: "Reverify" }).first().click();
  await expect(page.getByText(/Still UNKNOWN|Verified/)).toBeVisible();
});

// ---------------------------------------------------------------- journey E
test("E — replaying a completed task makes ZERO external calls", async ({
  page, request,
}) => {
  const created = await request.post(`${API}/tasks`, {
    headers: { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json" },
    data: { request: "Why did revenue drop this week?" },
  });
  const task = await created.json();

  await page.goto(`/tasks/${task.id}`);
  // Replay is a pane, not a page-level button — reached the way an operator
  // reaches it. `role="tab"` is what the page declares, and asking for the
  // role it actually uses is the difference between testing the product and
  // testing an assumption about it.
  await page.getByRole("tab", { name: "Replay" }).click();
  await expect(page.getByText(/must produce zero external calls/)).toBeVisible();
  await page.getByRole("button", { name: "PLAYBACK", exact: true }).click();

  // The whole claim of PLAYBACK: recorded results are served, the provider is
  // never contacted. A number rendered as zero is not the assertion — the
  // server's own count is.
  await expect
    .poll(async () => {
      const r = await request.post(`${API}/tasks/${task.id}/replay?mode=PLAYBACK`, {
        headers: { Authorization: `Bearer ${TOKEN}` },
      });
      // `external_calls_made`, which is what the contract says. Reading it
      // under a name the server does not send is the exact failure this suite
      // exists to catch — it caught it here in the test rather than in the
      // product, which is the same lesson either way.
      return (await r.json()).external_calls_made;
    }, { timeout: 20_000 })
    .toBe(0);

  // And the page says so too. `external_calls_made` is the server's count; the
  // toast renders it verbatim rather than the page deciding what zero means.
  await expect(page.getByText(/0 external calls/).first()).toBeVisible();
});
