/**
 * FIXTURE MODE: the console rendering every route with no backend behind it.
 *
 * Switched on by `?__fixtures` in the URL (remembered in `sessionStorage`
 * under `itms.fixtures`, so client-side navigation keeps it for the tab and
 * a fresh tab starts clean) and read by `lib/ApiContext.tsx`, which then
 * hands every page a typed proxy answering from `src/fixtures/core.ts` and
 * the per-module `fixtures.ts` files instead of an `ApiClient`.
 *
 * DEV-ONLY, AND THE GUARD IS IN THE BUNDLE, NOT IN A RUNTIME CHECK. The flag
 * is `import.meta.env.DEV && ...`, which Vite replaces with `false && ...` in
 * a production build; rollup folds it to `false`, drops `detect()` and with
 * it the only mention of `__fixtures`, and `src/test/preview.test.ts` asserts
 * that absence in the built artifact. The frontend never gates security
 * (invariant 6) -- a signed-in-looking console over invented data is a
 * feature of the review harness and would be a defect anywhere else.
 *
 * Everything touching `location` and `sessionStorage` is in a try/catch:
 * either can throw under a restrictive storage policy or in a non-browser
 * test environment, and "the flag is off" is the right answer to both.
 */

const STORAGE_KEY = "itms.fixtures";

function detect(): boolean {
  try {
    if (new URLSearchParams(window.location.search).has("__fixtures")) {
      window.sessionStorage.setItem(STORAGE_KEY, "1");
      return true;
    }
    return window.sessionStorage.getItem(STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

export const FIXTURE_MODE: boolean = import.meta.env.DEV && detect();
