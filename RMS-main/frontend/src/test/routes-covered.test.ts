/**
 * EVERY ROUTED PAGE IS WALKED BY THE HARMONY GATE, AND THIS IS WHAT SAYS SO.
 *
 * `scripts/harmony.mjs` visits `scripts/routes.mjs` in a real browser after
 * every push (v3 port spec §5). A route that `App.tsx` mounts but the list
 * does not name is a page the gate never sees: no screenshot, no stray-token
 * diff, no overflow probe, no axe. The list is deliberately a LITERAL rather
 * than derived from `App.tsx` at run time, because "the gate covers every
 * page" is a claim somebody should have to make on purpose when a page is
 * added -- and this test is what makes them.
 *
 * Both directions are checked. A route in the list that `App.tsx` no longer
 * mounts falls through to `*` and screenshots the fleet page under the wrong
 * name, which is a review finding about a page that does not exist.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { EXCLUDED, PARAMS, ROUTES, fillPath } from "../../scripts/routes.mjs";

const APP = readFileSync(resolve(import.meta.dirname, "../App.tsx"), "utf8");

/** Every `path="..."` that `App.tsx` mounts, patterns as written. */
function appPatterns(): string[] {
  const found = new Set<string>();
  for (const match of APP.matchAll(/<Route\b[^>]*?\bpath="([^"]+)"/gs)) {
    found.add(match[1] as string);
  }
  return [...found];
}

describe("scripts/routes.mjs covers App.tsx", () => {
  const patterns = appPatterns();

  it("read a real route table out of App.tsx", () => {
    // NON-VACUITY: an empty set satisfies both subset checks below.
    expect(patterns.length).toBeGreaterThan(50);
    expect(patterns).toContain("/devices");
    expect(patterns).toContain("/devices/:deviceId");
  });

  it("excludes exactly the routes the gate cannot or must not walk", () => {
    expect([...EXCLUDED].sort()).toEqual([
      "*",
      "/__preview",
      "/auth/callback",
      "/settings/email-intake/oauth/callback",
    ]);
    for (const excluded of EXCLUDED) expect(patterns).toContain(excluded);
  });

  it("fills every param App.tsx declares with a sample id", () => {
    const declared = new Set<string>();
    for (const pattern of patterns) {
      for (const match of pattern.matchAll(/:([A-Za-z]+)/g)) declared.add(match[1] as string);
    }
    for (const name of declared) {
      expect(PARAMS, `no sample id for :${name}`).toHaveProperty(name);
    }
    for (const route of ROUTES) expect(route).not.toMatch(/:[A-Za-z]/);
  });

  it("lists every route App.tsx mounts", () => {
    const expected = patterns.filter((p) => !EXCLUDED.has(p)).map(fillPath);
    const missing = expected.filter((route) => !ROUTES.includes(route));
    expect(missing, "routes App.tsx mounts that the gate never walks").toEqual([]);
  });

  it("lists no route App.tsx lacks", () => {
    const expected = new Set(patterns.filter((p) => !EXCLUDED.has(p)).map(fillPath));
    // A query variant (`/settings?tab=ai-connections`) is the route before
    // its `?`: the page is App.tsx's, the query only picks a state of it.
    const stale = ROUTES.filter((route) => !expected.has(route.split("?")[0] as string));
    expect(stale, "routes in the list that App.tsx no longer mounts").toEqual([]);
  });

  it("walks a query variant only of a route it also walks plainly", () => {
    for (const route of ROUTES.filter((r) => r.includes("?"))) {
      expect(ROUTES, `${route} has no plain route beside it`).toContain(route.split("?")[0]);
    }
  });

  it("walks Settings -> AI connections, which /settings alone never opens", () => {
    expect(ROUTES).toContain("/settings?tab=ai-connections");
  });

  it("has no duplicates", () => {
    expect(new Set(ROUTES).size).toBe(ROUTES.length);
  });
});
