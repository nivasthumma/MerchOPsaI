// Browser E2E — MerchantOps §22.
//
// §24's pipeline puts browser E2E before deploy, and §22 names the five
// journeys. They exist because everything else in this repository tests a
// LAYER: the scenario suite grades the agent, the integration tests exercise
// the API, Vitest renders components against fixtures. None of them can catch
// the failure where each layer is correct and the seams between them are not —
// a route that renders nothing, a field the client reads under a name the
// server stopped sending, a button wired to an endpoint that moved.
//
// ## Why these run against a real API and a real database
//
// A mocked E2E is a slower unit test. The point is the seam, so the fixture is
// the whole stack: `make seed`, the FastAPI app, the built SPA, one browser.
//
// ## Where these run
//
// In CI, as the `browser` job, and locally through `make e2e`. Both go through
// `scripts/run_e2e.sh`, which owns the whole fixture — its own database, its
// own port, the planted UNKNOWN action journey D needs — so the two are the
// same run on different machines rather than two fixtures to keep in step.
//
// They are deliberately NOT in `make ci`. That target is the one somebody runs
// before pushing, and adding a browser download plus a second Postgres
// database to it would make the fast check slow enough to be skipped. §24 puts
// browser E2E before deploy, not before every commit, and that is the line
// taken here. See ADR-0034, which records this reversing the CI half of
// ADR-0015.
import { defineConfig, devices } from "@playwright/test";

const PORT = Number(process.env.E2E_PORT ?? 5199);

export default defineConfig({
  testDir: "./e2e",
  // One worker: every test drives the same seeded database, and two of them
  // approving refunds at once would be testing concurrency by accident.
  workers: 1,
  fullyParallel: false,
  // A financial console that takes ten seconds to answer is a defect, but a
  // slow CI box is not — so the timeout is generous and the assertions are
  // about content rather than speed.
  timeout: 30_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI ? "list" : [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "retain-on-failure",
    // Deterministic viewport: P1-11's stacked tables switch at 760px, and a
    // test that straddles the breakpoint by accident is a flaky test.
    viewport: { width: 1280, height: 900 },
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    // `vite preview` serves the built bundle and proxies /api, so this
    // exercises what actually ships rather than the dev server.
    command: `npm run build && npx vite preview --port ${PORT} --strictPort`,
    url: `http://127.0.0.1:${PORT}`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
