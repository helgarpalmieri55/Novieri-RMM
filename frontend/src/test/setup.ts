// The vitest setup for the whole suite: jest-dom matchers, render cleanup,
// and the unexpected-`console.error`-fails-the-test gate, combined into one
// `setupFiles` entry loaded by every test file.
//
// THE GATE USED TO BE SCOPED to `src/components/field.test.tsx` and
// `src/modules/settings/**` only, via a second `vitest` project
// (`console-error-gated`) with its own `setupFiles`
// (`src/test/setup.gated.ts`), while this file stayed jest-dom-plus-cleanup
// only. That scoping was a holding measure, not a decision: turning the gate
// on for the whole suite in one commit surfaced ten pre-existing, previously-
// invisible `console.error` calls in `bcp`, `ipam`, `provisioning` and
// `dashboard`, and rather than block the finding that motivated the gate
// (`Field`'s help-wiring diagnostic, `src/components/ui.tsx`) on fixing all
// ten, the gate was scoped down to the surface the finding actually named
// and the ten were tracked and fixed separately (commits `69b1e3a` and
// `d628124`). With all eleven fixed, the split has no remaining reason to
// exist, so the gate lives here, globally, and `vite.config.ts` no longer
// declares `test.projects` at all -- a single top-level `setupFiles` entry
// naming this file is enough.
//
// NOT `vi.spyOn`. A test that wants to assert on an expected call
// (`field.test.tsx`'s "when the describedby cannot reach the control" is the
// pattern) installs its OWN `vi.spyOn(console, "error")` -- and `vi.spyOn` on
// a property that is ALREADY a vi mock does not layer a new one, it returns
// the SAME mock and only swaps its implementation (confirmed empirically:
// two `vi.spyOn(console, "error")` calls in a row return `===` objects, and
// `mockRestore()` does not clear that shared `.mock.calls`). A root-level
// `vi.spyOn` here, with a local `vi.spyOn` on top of it in a test, would
// therefore collapse into ONE spy with no way to tell "the test asserted on
// this" from "nothing did" -- which broke `field.test.tsx`'s own
// console.error tests the first time this was tried.
//
// A PLAIN REASSIGNMENT SIDESTEPS THE DEDUP ENTIRELY, because `vi.spyOn` only
// recognises and reuses an EXISTING *vi* mock -- `vi.isMockFunction` says no
// to a plain function, so a local `vi.spyOn(console, "error")` on top of
// this wrapper creates a genuine independent mock, wrapping this function as
// ITS `original`. While that local mock is installed, `console.error` points
// at it, not at this wrapper, so a call a test captures and asserts on never
// reaches `calls` below at all -- there is nothing to distinguish, because
// this wrapper is simply not in the call path for that test. Whatever a
// local spy leaves `console.error` pointing at when the test ends (restored
// to this wrapper, restored to native, or never restored at all), the
// unconditional reset below always puts the true original back before the
// next test's `beforeEach` runs.
//
// ONE `afterEach`, NOT TWO -- and this is not stylistic. Two same-level
// `afterEach` hooks (cleanup in one, the gate's throw in another) drop the
// second silently when the first throws: confirmed empirically (two probe
// suites), Vitest does not run the sibling hooks and collect the errors when
// one same-level `afterEach` throws -- it stops the phase outright,
// REGARDLESS of registration order (a benign hook registered before a
// throwing one was never observed to run either). Two hooks would therefore
// leave `cleanup()` silently skipped on any test whose own console.error
// check failed, and the NEXT test would inherit its unmounted DOM. One
// function, `cleanup()` first as a plain statement, throw second: the throw
// cannot pre-empt a statement that already ran.
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach } from "vitest";
import { setActiveLanguage } from "../i18n/core";

let calls: unknown[][] = [];
let original: typeof console.error;

beforeEach(() => {
  // Every test starts in English, so one test's Spanish can never leak into
  // the next. In the existing hook, not a second one (see above).
  setActiveLanguage("en");
  calls = [];
  original = console.error;
  console.error = (...args: unknown[]) => {
    calls.push(args);
  };
});

afterEach(() => {
  cleanup();
  console.error = original;
  if (calls.length > 0) {
    const said = calls.map((call) => call.map((arg) => String(arg)).join(" ")).join("\n");
    throw new Error(
      `console.error was called ${calls.length} time(s) during this test, and nothing in ` +
        `the test expected it. If the call is expected, spy on console.error locally in the ` +
        `test (see field.test.tsx's "when the describedby cannot reach the control") so it ` +
        `is asserted on rather than merely tolerated:\n${said}`,
    );
  }
});
