// Accessibility, in a real browser — P1-12.
//
// The Vitest suite already pins the parts jsdom can see: that a status carries
// a shape and not only a colour, that a dialog keeps the promise `aria-modal`
// makes, that a stacked table labels every cell. None of that is checkable
// here and none of it is repeated.
//
// What jsdom CANNOT check is anything that needs layout or paint, and P1-12
// names one of those explicitly: **contrast**. jsdom has no colours, no
// computed styles worth the name, and no viewport. A real browser has all
// three, so this is where that requirement is actually tested — and where a
// palette change that makes a failure state unreadable gets caught before an
// operator squints at it during an incident.
//
// ## Both themes, deliberately
//
// The app follows the viewer's theme, and a token redefined under
// `prefers-color-scheme: dark` is a token nobody checked in light. Every screen
// is scanned twice.
//
// ## Why the rule set is narrowed rather than "everything axe knows"
//
// Same argument the ruff configuration makes: a gate that fires on things
// nobody agreed to is a gate people learn to bypass. These are the WCAG A and
// AA rules, which is the bar the plan implies by naming contrast, keyboard
// navigation, semantic controls and accessible state updates. Best-practice
// rules are excluded because they encode opinions this project has not adopted.

import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Browser, type Page } from "@playwright/test";

const TOKEN = process.env.E2E_TOKEN ?? "";

test.beforeEach(async ({ page }) => {
  await page.addInitScript((t) => {
    window.localStorage.setItem("merchantops.token", t);
  }, TOKEN);
});

/** Scan whatever is currently rendered, and report every violation rather than
 *  the first — a list of one is a list somebody fixes one at a time. */
async function scan(page: Page, label: string) {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();

  // axe's own `failureSummary` names the measured ratio and the two colours it
  // measured. Without it a contrast failure reports a selector and leaves the
  // reader to guess which of foreground, background or opacity moved — and the
  // usual answer is opacity, which is invisible in the palette.
  const summary = results.violations.map((v) =>
    `  ${v.id} (${v.impact}) — ${v.help}\n`
    + v.nodes.slice(0, 3).map((n) =>
        `      ${n.target.join(" ")}\n`
        + `        ${(n.failureSummary ?? "").split("\n").join("\n        ")}`,
      ).join("\n"),
  ).join("\n");

  expect(results.violations, `${label}\n${summary}`).toEqual([]);
}

/** The theme is the viewer's, and a token redefined for dark is a token nobody
 *  checked in light. Emulated rather than toggled through the UI so the scan
 *  does not depend on the toggle working. */
async function inBothThemes(page: Page, url: string, ready: RegExp | string) {
  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await page.goto(url);
    await expect(page.getByText(ready).first()).toBeVisible();
    await scan(page, `${url} (${scheme})`);
  }
}

test("the Command Center is accessible in both themes", async ({ page }) => {
  await inBothThemes(page, "/", "Needs attention");
});

test("the Action Center is accessible in both themes", async ({ page }) => {
  // The densest screen in the application, and the one an operator reads under
  // time pressure.
  await inBothThemes(page, "/actions", /Every financial action/);
});

test("the incident workspace is accessible in both themes", async ({ page, request }) => {
  const res = await request.get("http://127.0.0.1:8100/incidents", {
    headers: { Authorization: `Bearer ${TOKEN}` },
  });
  const { incidents } = await res.json();
  // Asserted, not skipped: `scripts/run_e2e.sh` runs detection during setup, so
  // an empty list means the fixture is broken rather than absent. A scan that
  // silently does not run is a scan nobody notices has stopped.
  expect(incidents.length, "run via scripts/run_e2e.sh, which runs detection")
    .toBeGreaterThan(0);

  await inBothThemes(page, `/incidents/${incidents[0].id}`, "What happened");
});

test("the payment lifecycle is accessible in both themes", async ({ page }) => {
  await inBothThemes(page, "/payments/SYN_PAY_0002", "The payment");
});

test("the recovery ledger is accessible in both themes", async ({ page }) => {
  await inBothThemes(page, "/recovery", "Exposure");
});

test("the access review is accessible in both themes", async ({ page }) => {
  // §66, and the one screen here that puts text on a tinted ground: the
  // permissions that move money are chipped in `--warn-ink` on `--warn-soft`.
  // That pairing is exactly the one the severity chips got wrong once, so it
  // is scanned rather than reasoned about. Runs as USR_A_OWNER, which the
  // endpoint requires.
  await inBothThemes(page, "/access-review", /Who holds what/);
});

test("a failure state stays readable", async ({ page }) => {
  // The one worth having most. A palette change that leaves an error banner
  // unreadable is discovered, otherwise, by somebody trying to read it during
  // an incident — which is the worst possible moment and the least likely to
  // be reported as a contrast bug.
  await page.route("**/api/**", (route) => route.abort());
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("/actions");
  // `.first()`: the message appears in the banner heading, in its detail line
  // and in the freshness bar, which is the LiveBar and ErrorBanner both doing
  // their job rather than a duplicate.
  await expect(page.getByText(/Cannot reach the API/).first()).toBeVisible();
  await scan(page, "the error state (dark)");
});

/* ------------------------------------------------------- signed out
 *
 * The two pages every visitor sees first, and the two this file could not
 * reach: `beforeEach` above puts a token in localStorage for every test, so
 * the landing page and the sign-in page had no accessibility coverage at all
 * from the moment they were written.
 *
 * They need their own browser context rather than a cleared one. The token is
 * installed by an init script on the shared context, and an init script runs
 * on every navigation — clearing storage after `goto` would be racing the
 * thing that put it there.
 */
test.describe("signed out", () => {
  async function scanAnonymously(browser: Browser, url: string, ready: RegExp) {
    for (const scheme of ["light", "dark"] as const) {
      const ctx = await browser.newContext({ colorScheme: scheme });
      const page = await ctx.newPage();
      try {
        await page.goto(url);
        await expect(page.getByText(ready).first()).toBeVisible();
        const results = await new AxeBuilder({ page })
          .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
          .analyze();
        const summary = results.violations.map((v) =>
          `  ${v.id} (${v.impact}) — ${v.help}\n`
          + v.nodes.slice(0, 3).map((n) =>
              `      ${n.target.join(" ")}\n`
              + `        ${(n.failureSummary ?? "").split("\n").join("\n        ")}`,
            ).join("\n"),
        ).join("\n");
        expect(results.violations, `${url} signed out (${scheme})\n${summary}`)
          .toEqual([]);
      } finally {
        await ctx.close();
      }
    }
  }

  test("the landing page is accessible in both themes", async ({ browser }) => {
    // The dark hero and the light body are two different grounds, and an
    // accent legible on one can fail on the other -- which is exactly what a
    // contrast check is for and exactly what no test was doing.
    await scanAnonymously(browser, "/", /An HTTP 200 is not/);
  });

  test("the sign-in page is accessible in both themes", async ({ browser }) => {
    // Its left half is a fixed dark palette that does NOT follow the theme, so
    // it is checked under both settings to prove that is deliberate rather
    // than a token that failed to switch.
    await scanAnonymously(browser, "/signin", /A token identifies you/);
  });

  test("following the landing nav shows exactly one section, clear of the header",
       async ({ browser }) => {
    // Two ways this broke, both of which look like the page simply lost a
    // heading:
    //
    //   - the sticky header is 69px tall and `scroll-margin-top` was a
    //     constant, so at a width where the pill row wrapped the bar was 107px
    //     and the target landed 18px behind it;
    //   - sections wait for an IntersectionObserver holding a 14px offset, so
    //     the scroll aimed at where the page was before the observers fired
    //     and the target rose by the sum of every offset above it.
    //
    // Checked at more than one width because the first failure only appears at
    // one, and on the section's own <header> rather than the section box --
    // that is the element a reader loses.
    for (const [width, height] of [[1440, 900], [1180, 720]] as const) {
      const ctx = await browser.newContext({ viewport: { width, height } });
      const page = await ctx.newPage();
      try {
        await page.goto("/");
        const nav = page.getByRole("navigation", { name: "On this page" });
        await expect(nav).toBeVisible();

        for (const link of await nav.getByRole("link").all()) {
          const label = (await link.textContent())!.trim();
          await link.click();
          // The scroll settles asynchronously; poll rather than sleep.
          await expect.poll(async () => page.evaluate(() => {
            const header = document.querySelector(".lp-topwrap")!.getBoundingClientRect();
            const section = document.getElementById(location.hash.slice(1))!;
            const head = section.querySelector(".lp-sec-head") ?? section;
            return Math.round(head.getBoundingClientRect().top - header.bottom);
          }), `"${label}" at ${width}px sits behind the header`).toBeGreaterThan(0);

          // And the header agrees with where the reader landed. The spy used
          // to watch for a section crossing the middle of the viewport, so
          // arriving at "How it works" lit "The four states" -- the nav and
          // the heading directly under it saying different things.
          await expect.poll(async () =>
            page.locator(".lp-nav a.on").textContent(),
          `"${label}" at ${width}px is not the entry the header lit`,
          ).toBe(label);

          // And it is the ONLY section heading in view. Sections are a screen
          // tall so that following a nav entry hands the reader that section
          // and nothing else; before that, arriving at one section showed the
          // next one's heading in the same frame.
          await expect.poll(async () => page.evaluate(() => {
            const bottom = document.querySelector(".lp-topwrap")!
              .getBoundingClientRect().bottom;
            return [...document.querySelectorAll(".landing section .lp-sec-head")]
              .filter((e) => {
                const r = e.getBoundingClientRect();
                return r.bottom > bottom && r.top < window.innerHeight - 2;
              }).length;
          }), `"${label}" at ${width}px shares the screen with another section`,
          ).toBe(1);
        }
      } finally {
        await ctx.close();
      }
    }
  });
});
