// EVERY FEATURE MODULE IS REACHABLE FROM THE APP SHELL.
//
// On 2026-08-14 `src/modules/site-servers/` held seven files and ~1,300 lines
// -- a full list page, a detail pane, a wake action, a segment check, typed
// hooks for all of it -- and `App.tsx` imported none of them. It had no route.
// No operator could reach it. Its backend routes, permission codes, tests and
// migrations were all in place.
//
// NOTHING CAUGHT IT, and that is the interesting part. The module had a folder,
// so the roadmap status test was satisfied. Its components had unit tests, so
// vitest was satisfied. `tsc --noEmit` was clean, because unreferenced modules
// still type-check. The backend suite was green. Every signal this project
// measures said "shipped"; the one property that matters to a user -- can you
// get to it -- was asserted by nothing.
//
// This file asserts it. It is a structural test over source TEXT rather than a
// render test, deliberately: it must fail for a module NOBODY imports, and a
// render test can only exercise routes that already exist.
//
// VITE'S `import.meta.glob`, NOT `node:fs`. Originally because this project's
// tsconfig had no `@types/node`, so an fs-based version compiled under vitest
// and failed `tsc --noEmit`. That reason EXPIRED on 2026-08-16 -- the UI port
// added `@types/node` so the design-token guard could read the prototype off
// disk -- but the choice stands on its own merits: `import.meta.glob` resolves
// against Vite's module graph, which is the same graph the bundler uses, so
// this test sees exactly the files the application would.

import { describe, expect, it } from "vitest";

// Every file one level inside `modules/`, eager: false -- only the KEYS are
// read, so nothing is bundled or executed by this test.
const MODULE_FILES = import.meta.glob("./modules/*/*");

// The shell's own source, as text.
const APP_SOURCE = Object.values(
  import.meta.glob("./App.tsx", { query: "?raw", import: "default", eager: true }),
)[0] as string;

/**
 * Modules deliberately not routed at the top level, each with its reason.
 *
 * EMPTY, and it should stay that way unless a real case appears. A module that
 * genuinely renders only inside another (a shared widget, say) belongs in
 * `components/` rather than `modules/`, so an entry here is a smell worth
 * arguing about in review rather than a routine exemption.
 */
const DELIBERATELY_UNROUTED: readonly string[] = [];

function moduleFolders(): string[] {
  const names = new Set<string>();
  for (const path of Object.keys(MODULE_FILES)) {
    // "./modules/<name>/<file>"
    const name = path.split("/")[2];
    if (name) names.add(name);
  }
  return [...names].sort();
}

describe("app shell reachability", () => {
  it("imports every feature module", () => {
    const unreferenced = moduleFolders().filter(
      (m) => !DELIBERATELY_UNROUTED.includes(m) && !APP_SOURCE.includes(`./modules/${m}/`),
    );
    expect(
      unreferenced,
      "these modules exist but App.tsx imports nothing from them, so no user " +
        "can reach them. Route them, or move them out of modules/ if they are " +
        `not features: ${unreferenced.join(", ")}`,
    ).toEqual([]);
  });

  it("finds a plausible number of modules, so the check is not vacuous", () => {
    // The guard against the failure this whole file exists to prevent, applied
    // to itself: if the glob ever returned nothing -- a moved directory, a
    // changed pattern -- the assertion above would pass over an empty list and
    // report a clean bill of health.
    expect(moduleFolders().length).toBeGreaterThan(20);
    expect(APP_SOURCE.length).toBeGreaterThan(1000);
  });

  it("routes site-servers specifically", () => {
    // The regression that prompted this file, pinned by name. The general
    // assertion above would catch it again, but naming it means a future reader
    // who deletes these routes learns WHY they are load-bearing.
    expect(APP_SOURCE).toContain("SiteServersPage");
    expect(APP_SOURCE).toContain('path="/site-servers"');
    expect(APP_SOURCE).toContain('path="/site-servers/:siteServerId"');
  });
});
