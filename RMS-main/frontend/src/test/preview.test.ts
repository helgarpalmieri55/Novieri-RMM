/**
 * The dev-only preview harness must not reach production.
 *
 * `App.tsx` mounts `/__preview` behind `import.meta.env.DEV`, which Vite
 * replaces with the literal `false` in a production build, so rollup drops the
 * branch and tree-shakes the module away. That is the INTENT. This test checks
 * the ARTIFACT, because "guarded by a flag" and "absent from the bundle" are
 * different claims and only the second is a guarantee.
 *
 * Why it matters more than it looks: the harness renders a full sidebar from a
 * hardcoded list of every permission code, over fixture devices with invented
 * counts (286 / 272 / 18 / 94%). Shipped, it would be a route that shows an
 * unauthenticated visitor a convincing, entirely fictional console.
 *
 * The test builds the app itself rather than trusting a previous `npm run
 * build`, so it cannot pass against a stale `dist/`.
 */

import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import { beforeAll, describe, expect, it } from "vitest";

const FRONTEND = resolve(import.meta.dirname, "../..");
// ITS OWN output directory, never the shared `dist/`. This test builds the
// app, and anything else building concurrently -- another test, a developer,
// a parallel agent -- would be rewriting the very files it is reading. That
// race made this test fail against a bundle that was in fact correct, which
// is a worse outcome than not having the test: a flaky guard gets deleted.
const DIST = resolve(FRONTEND, "dist-preview-check");

/** Text of every built JS asset, concatenated. */
function bundleText(): string {
  const assets = resolve(DIST, "assets");
  return readdirSync(assets)
    .filter((name) => name.endsWith(".js"))
    .map((name) => readFileSync(resolve(assets, name), "utf8"))
    .join("\n");
}

describe("the preview harness", () => {
  // The build's failure, if it failed. A THROW in `beforeAll` makes vitest
  // report these tests as SKIPPED, and a skipped guard reads as green in
  // every summary line and every CI grep for "failed". That is how a check
  // stops existing without anyone deciding to remove it -- observed here:
  // three consecutive full-suite runs reported "3 skipped" twice, while the
  // property under test was never actually checked.
  let buildError: string | null = null;

  beforeAll(() => {
    // 120s: a cold production build. Slow, and the only honest way to assert
    // something about the artifact.
    try {
    execFileSync(
      "npx",
      ["vite", "build", "--mode", "production", "--outDir", "dist-preview-check", "--emptyOutDir"],
      {
        cwd: FRONTEND,
        timeout: 180_000,
        stdio: "pipe",
        // NODE_ENV=production EXPLICITLY. vitest sets NODE_ENV=test and
        // execFileSync inherits it, so without this the build is not a
        // production build at all: `import.meta.env.DEV` stays true, the
        // output still contains `jsxDEV`, and the preview route and its
        // fixtures are bundled. The first run of this test failed for exactly
        // that reason -- against an application that was in fact correct,
        // since `npm run build` in a clean environment produced a clean
        // bundle. A guard that tests a different artifact than the one you
        // ship is worse than no guard.
        env: { ...process.env, NODE_ENV: "production" },
      },
    );
    } catch (error) {
      // Captured, not thrown: an assertion below turns it into a FAILURE.
      const err = error as { stderr?: Buffer; message?: string };
      buildError = err.stderr?.toString() || err.message || String(error);
    }
  }, 200_000);

  it("built successfully, so the assertions below actually ran", () => {
    expect(buildError, `the production build failed:\n${buildError}`).toBeNull();
  });

  it("produced a bundle to inspect", () => {
    // NON-VACUITY: every assertion below is an absence, and all of them pass
    // against a directory that does not exist.
    expect(existsSync(resolve(DIST, "assets"))).toBe(true);
    const text = bundleText();
    expect(text.length).toBeGreaterThan(10_000);
    // Something we KNOW ships, proving the search can find things at all.
    expect(text).toContain("Add a computer");
  });

  it("does not ship the preview route", () => {
    expect(bundleText()).not.toContain("__preview");
  });

  /**
   * FIXTURE MODE IS THE SAME CLASS OF THING AS THE PREVIEW HARNESS, and gets
   * the same guard. `?__fixtures` (v3 port spec §5) makes `RequireAuth` treat
   * the session as signed in and hands every page a proxy answering from
   * `src/fixtures/`. In DEV that is the review harness; shipped, it would be
   * a query string that renders a convincing console to anyone. The flag is
   * `import.meta.env.DEV && ...` and the three strings below are each the
   * only place their concept is spelled: the URL switch (`fixtures/mode.ts`),
   * the error class (`fixtures/client.ts`) and the registry path named in
   * that error's message (which is how `fixtures/core.ts` is referenced from
   * the one module that imports it).
   */
  it("does not ship fixture mode", () => {
    const text = bundleText();
    for (const marker of ["__fixtures", "FixtureMissing", "fixtures/core"]) {
      expect(text, `${marker} reached the production bundle`).not.toContain(marker);
    }
  });

  it("does not ship the fixture data", () => {
    const text = bundleText();
    // The worked example in `fixtures/core.ts`: a hostname from the seed
    // table, the org's support address from an object literal, and the
    // suffix of a serial built by a template string -- three constructs, for
    // the reason the harness list below gives. (The suffix, not the whole
    // serial: the template's output never appears in source, so asserting
    // its absence would be asserting nothing.)
    for (const fixture of ["lis-ws-041", "it@toeshee.example", "-7K2Q"]) {
      expect(text, `${fixture} reached the production bundle`).not.toContain(fixture);
    }
  });

  /**
   * THE GUARD WAS CHECKING THE ONE FIXTURE FAMILY THAT WAS ALREADY FINE.
   *
   * `ws-alpha.corp.local` and `srv-dc01.dc.local` live in `FIXTURE_DEVICES` --
   * a plain array nothing calls, which rollup shakes without being asked. Every
   * OTHER fixture in the harness reached the production bundle and this test
   * stayed green: `KB5041585`, `openssl=3.0.13-0ubuntu3.4`, `srv-dc01`,
   * `Alex Morgan`, and later a live XSS payload string.
   *
   * The cause was module scope. Rollup drops the `/__preview` route and the
   * component under `import.meta.env.DEV`, but it cannot drop a CALL whose body
   * might have a side effect -- and `const X = xPreviewClient()`,
   * `[...].join("\n")` and `"c".repeat(64)` are all calls. Each one anchored
   * every fixture it touched. The harness now builds its clients inside the
   * component and writes its strings out literally.
   *
   * So the list below deliberately spans BOTH families and every construct that
   * was anchoring data, because a guard that samples one of them is how this
   * went unnoticed for a release.
   */
  it("does not ship the harness's fixture data", () => {
    const text = bundleText();
    for (const fixture of [
      // Devices (shaken all along -- kept so a regression there still fails).
      "ws-alpha.corp.local",
      "srv-dc01.dc.local",
      // Patch: anchored by `const PATCH_PREVIEW_CLIENT = patchPreviewClient()`.
      "KB5041585",
      "openssl=3.0.13-0ubuntu3.4",
      "Alex Morgan",
      // Documents: anchored by the same call, plus `.join()` and `.repeat()`.
      "core-switch-diagram",
      "Acme support contract",
      // AND THE ONE THAT MATTERS MOST. The harness's document fixture is a live
      // three-vector XSS payload, there so `scripts/a11y-browser.mjs` can prove
      // in a browser that the viewer renders it as characters. It is inert in a
      // bundle whose only reference was dropped -- and it has no business being
      // in a production artifact regardless, and would be the first thing a
      // scanner flagged.
      "__ITMS_XSS_EXECUTED",
    ]) {
      expect(text, `${fixture} reached the production bundle`).not.toContain(fixture);
    }
  });
});
