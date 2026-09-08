// The frontend's lint rules — the same argument `pyproject.toml` makes for ruff.
//
// The backend has had a curated ruff configuration since early on, and its
// first run found two methods silently redefining each other in the live
// payments adapter. The frontend had `tsc --noEmit` and nothing else, which is
// a different check entirely: the compiler proves types line up, and says
// nothing about a `useEffect` that reads a value it never declared.
//
// That gap is not hypothetical here. Three screens poll through
// `useLiveRefresh`, and the whole point of its `deps` option is that a filter
// change refetches immediately rather than at the next interval. A dependency
// array that omits the value it closes over produces exactly the failure the
// hook exists to prevent — one filter's rows under another filter's heading —
// silently, in a financial queue, with a "live" indicator beside it.
//
// ## Why this is not "everything eslint knows"
//
// Same reason the ruff set is curated: a gate that fires on style nobody
// agreed to is a gate people learn to bypass, and the first thing bypassed is
// the rule that would have caught something.
//
// ## What was evaluated and NOT adopted, and why
//
// `eslint-plugin-react-hooks` v7's recommended set is largely the React
// Compiler rule set. Turned on wholesale it reports ten findings here, and
// every one was read before this decision was made:
//
//   set-state-in-effect (6)  guarded `if (!token) { setMe(null); return; }`
//                            clearing derived state when its input goes away
//   purity (2)               `Date.now()` during render, in the relative-time
//                            component and in an approval-expiry check
//   refs (1)                 `fetcherRef.current = fetcher` during render —
//                            the standard latest-callback ref, held precisely
//                            so the polling effect does not restart on every
//                            parent re-render
//
// None is a defect. They are deviations from rules that exist to let the React
// Compiler memoize aggressively, and this app does not use the compiler.
// Satisfying them means restructuring ten sites of working, tested UI for no
// behavioural gain — and the `refs` one cannot be fixed by moving the write
// into an effect without leaving the ref a render stale on first commit, which
// for a polling hook is a real change.
//
// So they are off, by name, with the count recorded. Off with a reason is a
// decision; on-as-warning is noise that trains people to ignore lint output,
// and on-as-error is a refactor nobody asked for. If this app ever adopts the
// React Compiler, this block is the list of what has to be fixed first.

import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import tseslint from "typescript-eslint";

export default tseslint.config(
  {
    ignores: [
      "dist/**", "node_modules/**", "coverage/**",
      // Generated from `docs/openapi.json` by `npm run gen:api`. It is the
      // contract, not source; editing it to satisfy a linter would be editing
      // the wrong end of the pipeline.
      "src/api/schema.d.ts",
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    plugins: { "react-hooks": reactHooks },
    rules: {
      // The two this configuration exists for.
      "react-hooks/rules-of-hooks": "error",
      // Promoted from the plugin's "warn" to an error. An effect that reads a
      // value it does not declare is a screen that updates on the wrong
      // schedule, and on an operations console the wrong schedule means stale
      // money on display.
      "react-hooks/exhaustive-deps": "error",

      // Evaluated and not adopted — see the header. Named individually rather
      // than by skipping the recommended set, so that a rule ADDED to the
      // plugin later arrives switched on and gets read, instead of being
      // silently excluded by a config that never mentioned it.
      "react-hooks/set-state-in-effect": "off",
      "react-hooks/purity": "off",
      "react-hooks/refs": "off",

      // An unused variable is usually a rename that did not finish. The
      // underscore prefix is the escape hatch, because a deliberately ignored
      // argument is a real thing — `(_, index) =>` should not need a comment.
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_",
          caughtErrorsIgnorePattern: "^_" },
      ],

      // Off, deliberately. The API surface is generated from OpenAPI and the
      // fixtures are captured JSON; `any` appears at those boundaries where a
      // response is narrowed by hand, and the narrowing is the code worth
      // reading. Banning it there produces a wall of `unknown` casts that say
      // less than the `any` did.
      "@typescript-eslint/no-explicit-any": "off",
    },
  },
);
