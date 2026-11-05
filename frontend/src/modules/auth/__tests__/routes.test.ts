/**
 * The pages reached without a session, as `App.tsx` mounts them (own-auth
 * Task 11).
 *
 * THREE LISTS MUST AGREE, AND NONE CAN SEE THE OTHERS:
 *   - `App.tsx`'s routes for these pages, which must sit OUTSIDE
 *     `RequireAuth` -- inside it, a person with no session would be sent to
 *     sign in from the page where they sign in;
 *   - `lib/session.ts::PUBLIC_PATHS`, which keeps `goToSignIn` from starting
 *     a navigation from one of them (plan decision D20) and refuses them as a
 *     `?next=`;
 *   - `scripts/routes.mjs`, which the harmony gate walks.
 * And each must start with a prefix the edge sends to the console, not to the
 * api (plan decision D19).
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { ROUTES, fillPath } from "../../../../scripts/routes.mjs";
import { PUBLIC_PATHS, isPublicPath } from "../../../lib/session";

const APP = readFileSync(resolve(import.meta.dirname, "../../../App.tsx"), "utf8");

/** Every `path="..."` App.tsx mounts (the pattern `routes-covered.test.ts` reads). */
function allPaths(): string[] {
  return [...APP.matchAll(/<Route\b[^>]*?\bpath="([^"]+)"/gs)].map((match) => match[1] as string);
}

/** `path -> element source` for every `<Route>` App.tsx declares on one line. */
function oneLineRoutes(): Map<string, string> {
  return new Map(
    [...APP.matchAll(/<Route\s+path="([^"]+)"\s+element=\{(.*?)\}\s*\/>/g)].map(
      (match) => [match[1] as string, match[2] as string] as const,
    ),
  );
}

const PAGES = [
  "/sign-in",
  "/sign-in/factor",
  "/sign-in/enroll",
  "/sign-in/recovery-codes",
  "/forgot-password",
  "/reset-password",
  "/invite",
];

describe("the pages reached without a session", () => {
  it("are mounted, each outside RequireAuth", () => {
    const mounted = oneLineRoutes();
    for (const page of PAGES) {
      expect(mounted.has(page), `${page} is not a one-line <Route> in App.tsx`).toBe(true);
      expect(mounted.get(page)).not.toContain("RequireAuth");
    }
  });

  it("are exactly the public paths, and every other route is not one", () => {
    for (const page of PAGES) expect(isPublicPath(page), page).toBe(true);
    const paths = allPaths();
    expect(paths.length).toBeGreaterThan(50);
    for (const path of paths) {
      if (!PAGES.includes(path) && path !== "*") expect(isPublicPath(path), path).toBe(false);
    }
    expect(new Set(PAGES.map((page) => `/${page.split("/")[1]}`))).toEqual(new Set(PUBLIC_PATHS));
  });

  it("are walked by the harmony gate", () => {
    for (const page of PAGES) expect(ROUTES).toContain(fillPath(page));
  });

  it("start with no prefix the edge sends to the api", () => {
    for (const page of PAGES) {
      expect(page).not.toMatch(/^\/(api|enroll|i|mcp|\.well-known|realms|resources|js)(\/|$)/);
    }
  });
});
