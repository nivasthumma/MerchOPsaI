#!/usr/bin/env python3
"""Mutation test for the FRONTEND suite — does it actually catch regressions?

`scripts/mutation_test.py` asks that question of 88 controls under `app/` and
answers it 88/88. Nothing asked it of `web/`. The repository publishes "every
control has a test that fails when it breaks" beside a figure measured entirely
in Python, while 308 Vitest tests had never been shown to fail when anything
broke.

They had reason to be doubted. Three tests found by hand in one afternoon
asserted less than their own names promised:

  * "shows the matched total, not the length of the page" checked the sentence
    under the table and the strip beside it, but never the count in the
    heading — which was the page size, and wrong.
  * "never draws a later stage wider than an earlier one" carried the comment
    "Recovered is forced above at-risk here" over data that nested perfectly,
    so it asserted that a correct funnel draws correctly.
  * one I wrote myself, asserting a formatting bound I could not construct an
    input to violate. Deleted.

None of those is findable by grepping: every test in the suite has assertions,
and none of them are tautologies. The only thing that finds a test asserting
less than it claims is breaking the code and seeing whether it notices.

## Scope

Deliberately smaller than the backend's 88. Each mutant here is a control where
a wrong frontend misleads an operator about money or about what the system did:
a count, a money figure, a verification state, a policy hold, a gate. Rendering
and layout are not controls and are not mutated.

    python scripts/mutation_test_web.py            # all of them
    python scripts/mutation_test_web.py money      # only labels matching

A full run is one `vitest run` per mutant plus a baseline — minutes, not hours,
which is why this one prints a table at the end AND a line as it goes.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
LOCK = ROOT / ".mutation-web-in-progress"

# (label, file relative to the repository root, find, replace)
#
# Every `find` is anchored on a line the control actually turns on, so a
# mutation that cannot be applied is reported rather than passed over: an
# anchor that no longer matches means the code moved, and a control with no
# test looks identical to one that passed.
MUTATIONS: list[tuple[str, str, str, str]] = [
    # --- counts that must come from the server's total, not the page --------
    (
        "incidents: count the page instead of the match",
        "web/src/routes/Incidents.tsx",
        "  const matched = live.data?.matched ?? rows?.length ?? 0;",
        "  const matched = rows?.length ?? 0;  // MUTANT",
    ),
    (
        "actions: count the page instead of the section total",
        "web/src/routes/Actions.tsx",
        "  const total = d.counts[k];",
        "  const total = rows.length;  // MUTANT",
    ),
    # --- money -------------------------------------------------------------
    (
        "money: render minor units as rupees",
        "web/src/components/Bits.tsx",
        '      ₹{(minor / 100).toLocaleString("en-IN",',
        '      ₹{(minor / 1).toLocaleString("en-IN",  // MUTANT',
    ),
    (
        "money: render a missing amount as zero",
        "web/src/components/Bits.tsx",
        '  if (minor == null) return <span className="muted">—</span>;',
        "  if (false) return <span />;  // MUTANT",
    ),
    # --- the funnel's geometry ---------------------------------------------
    (
        "funnel: let a bar exceed the track it is drawn in",
        "web/src/routes/CommandCenter.tsx",
        "            const share = top > 0 ? Math.min(s.amount_minor / top, 1) : 0;",
        "            const share = top > 0 ? s.amount_minor / top : 0;  // MUTANT",
    ),
    (
        "funnel: scale by whichever stage came first",
        "web/src/routes/CommandCenter.tsx",
        '  const top = stages.find((s) => s.stage === "AT_RISK")?.amount_minor ?? 0;',
        "  const top = stages[0]?.amount_minor ?? 0;  // MUTANT",
    ),
    # --- verification state, where "unknown" must never read as "fine" ------
    (
        "status: colour UNKNOWN as a success",
        "web/src/components/Bits.tsx",
        '    : state === "UNKNOWN" ? "unknown"',
        '    : state === "UNKNOWN" ? "ok"  // MUTANT',
    ),
    (
        "activity: name a blocked step as completed",
        "web/src/components/AgentActivity.tsx",
        '  blocked: "waiting on a person",',
        '  blocked: "completed",  // MUTANT',
    ),
    (
        "activity: draw a failed step with the done glyph",
        "web/src/components/AgentActivity.tsx",
        '  failed: "✕",',
        '  failed: "✓",  // MUTANT',
    ),
    # --- what a failure implies about whether anything happened -------------
    (
        "effect: call a failed write a read",
        "web/src/api/client.ts",
        '    if (this.method === "GET") return "read-only";',
        '    return "read-only";  // MUTANT',
    ),
    (
        "effect: call an unknown outcome a refusal",
        "web/src/api/client.ts",
        "    return \"unknown\";",
        '    return "refused";  // MUTANT',
    ),

    (
        "schedule: read a null next-check as no retry at all",
        "web/src/routes/Actions.tsx",
        '                    : <span className="muted">due now</span>}',
        '                    : <span className="muted">no retry</span>}  // MUTANT',
    ),

    # --- the token, and what carries it ------------------------------------
    (
        "auth: send a request with no token rather than refusing",
        "web/src/api/client.ts",
        "  if (!token) {",
        "  if (false) {  // MUTANT",
    ),
    (
        "signin: show the token in plain text",
        "web/src/App.tsx",
        '              id="tok" type="password" value={draft}',
        '              id="tok" type="text" value={draft}  // MUTANT',
    ),
    # --- the landing page's disclosure -------------------------------------
    (
        "landing: drop the mocked-execution disclosure",
        "web/src/App.tsx",
        "            ? <>Execution is <strong>live</strong> against Razorpay test mode.</>",
        "            ? <>Execution is <strong>live</strong>.</>  // MUTANT",
    ),
]

SELF = "scripts/mutation_test_web.py"


def _hms(seconds: float) -> str:
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    if s >= 60:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s}s"


def run_suite() -> tuple[bool, str]:
    """Run Vitest once. Returns (everything passed, the summary line).

    `--silent` because a mutant makes tests fail loudly and eighty failures of
    console noise per mutant buries the one line that matters.
    """
    r = subprocess.run(
        ["npx", "vitest", "run", "--silent"],
        cwd=WEB, capture_output=True, text=True,
        env={**os.environ, "CI": "1"},
    )
    line = ""
    for out in (r.stdout, r.stderr):
        for ln in out.splitlines():
            if "Tests " in ln and ("passed" in ln or "failed" in ln):
                line = " ".join(ln.split())
    return r.returncode == 0, line or "(no summary line)"


def main() -> int:
    selectors = [a for a in sys.argv[1:] if not a.startswith("-")]
    mutations = [m for m in MUTATIONS
                 if not selectors or any(s.lower() in m[0].lower() for s in selectors)]
    if not mutations:
        print(f"No mutation label matches {selectors}.")
        return 1

    print("=" * 78)
    print("Frontend mutation test — breaking each control to prove Vitest catches it")
    print("=" * 78)
    print()
    if selectors:
        print(f"!! FILTERED RUN: {len(mutations)}/{len(MUTATIONS)} mutants.")
        print("!! A filtered run is not a substitute for the full one.")
        print()
    print("!! Source files under web/src/ are REWRITTEN while this runs.")
    print("!! Do not commit, branch, or stash until it finishes.")
    print(f"!! Lock file: {LOCK.name}")
    print(flush=True)

    if LOCK.exists():
        print("A frontend mutation run is already in progress (or one was killed).")
        print()
        print(LOCK.read_text().rstrip())
        return 1

    # Anchors first. A mutation that cannot be applied is a control with no
    # test, not a control that passed -- and finding that out mutant by mutant
    # turns a five-minute run into a five-minute run that ends in a surprise.
    stale, ambiguous = [], []
    for label, rel, find, _ in mutations:
        n = (ROOT / rel).read_text().count(find)
        if n == 0:
            stale.append((label, rel))
        elif n > 1:
            # `replace(find, replace, 1)` takes the FIRST match. An anchor that
            # appears twice therefore mutates whichever site happens to come
            # first in the file, which is not the control the label names — so
            # the run would report on something nobody chose.
            ambiguous.append((label, rel, n))
    if stale:
        print("ANCHORS NO LONGER MATCH THE SOURCE — the code moved under them:")
        for label, rel in stale:
            print(f"  {label}\n    in {rel}")
    if ambiguous:
        print("ANCHORS MATCH MORE THAN ONE PLACE — the first one would be "
              "mutated, which is not necessarily the control named:")
        for label, rel, n in ambiguous:
            print(f"  {label}\n    {n} matches in {rel}")
    if stale or ambiguous:
        return 1

    LOCK.write_text("Frontend mutation test in progress. web/src is being rewritten.\n")
    started = time.monotonic()
    try:
        ok, line = run_suite()
        print(f"baseline: {line}", flush=True)
        if not ok:
            print("  baseline is not clean; aborting.")
            return 1

        rows: list[tuple[str, str, str]] = []
        for n, (label, rel, find, replace) in enumerate(mutations, 1):
            path = ROOT / rel
            original = path.read_text()
            try:
                path.write_text(original.replace(find, replace, 1))
                caught, line = run_suite()
                status = "CAUGHT" if not caught else "SURVIVED"
                rows.append((label, status, line))
            finally:
                path.write_text(original)
            done = time.monotonic() - started
            eta = (f"  eta {_hms((done / n) * (len(mutations) - n))}"
                   if n >= 2 else "")
            print(f"[{n:>2}/{len(mutations)}] {label:<52} {status:<9} {line}"
                  f"  ({_hms(done)}{eta})", flush=True)
    finally:
        LOCK.unlink(missing_ok=True)

    print()
    print(f"{'mutation':<52} {'result':<10} {'suite'}")
    print("-" * 78)
    for label, status, line in rows:
        print(f"{label:<52} {status:<10} {line}")

    survivors = [label for label, status, _ in rows if status == "SURVIVED"]
    print()
    print(f"RESULT: {len(rows) - len(survivors)}/{len(rows)} mutations caught")
    if survivors:
        print("\nSURVIVING MUTATIONS — these are gaps in the frontend suite:")
        for s in survivors:
            print(f"  - {s}")
        return 1
    print("Every injected defect was detected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
