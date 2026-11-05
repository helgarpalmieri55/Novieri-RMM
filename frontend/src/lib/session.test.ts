/**
 * Where the console signs in, and what it will land on afterwards.
 *
 * RE-POINTED FROM `auth.test.ts` (own-auth Task 10). That file held
 * `resolveAuthority` to one defect: the console fetched Keycloak's discovery
 * document from `http://localhost:8080` on every deployment, because a
 * build-time variable was set nowhere and the fallback shipped in every image.
 * The console no longer talks to an identity provider at all; the property
 * that file protected -- the sign-in address can never point off the
 * deployment's own origin -- is now a property of `signInPath`, which builds a
 * PATH and nothing else, and of the package list, which no longer carries an
 * OIDC client.
 *
 * And the open-redirect refusals `App.auth.test.tsx` ran through the old
 * callback (`safeReturnTo` over the OIDC `state`) run here, against the
 * `?next=` filter that replaced it: the value is in a link anyone can write.
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it, vi } from "vitest";

import {
  DEFAULT_AFTER_SIGN_IN,
  PUBLIC_PATHS,
  SIGN_IN_PATH,
  isPublicPath,
  safeNext,
  signInPath,
} from "./session";
import { isSessionEnded, resetSessionEnd, sessionEnded, subscribeSessionEnd } from "./sessionEnd";

describe("the sign-in address", () => {
  it("is a path on the console's own origin, never a host and never a port", () => {
    for (const next of [undefined, "/", "/devices", "/settings?tab=users"]) {
      const path = signInPath(next);
      expect(path.startsWith(`${SIGN_IN_PATH}`)).toBe(true);
      expect(path).not.toMatch(/^[a-z]+:|^\/\//i);
      expect(path).not.toContain("localhost");
      expect(path).not.toContain(":8080");
    }
  });

  it("carries the page asked for, encoded, and nothing for the root", () => {
    expect(signInPath("/devices?x=1")).toBe("/sign-in?next=%2Fdevices%3Fx%3D1");
    expect(signInPath("/")).toBe("/sign-in");
    expect(signInPath(undefined)).toBe("/sign-in");
  });

  it("is not built from anything a build or a page can configure", () => {
    // The defect `auth.test.ts` was written for came from a build-time
    // variable nobody set. No such variable is read any more.
    const source = readFileSync(resolve(import.meta.dirname, "session.ts"), "utf8");
    expect(source).not.toMatch(/import\.meta\.env/);
  });

  it("needs no OIDC client: the package is gone and nothing imports it", () => {
    const pkg = JSON.parse(readFileSync(resolve(import.meta.dirname, "../../package.json"), "utf8")) as {
      dependencies?: Record<string, string>;
      devDependencies?: Record<string, string>;
    };
    expect({ ...pkg.dependencies, ...pkg.devDependencies }).not.toHaveProperty("oidc-client-ts");
  });
});

describe("the pages reached without a session", () => {
  it("are the sign-in steps, the two password links and the invitation", () => {
    expect([...PUBLIC_PATHS].sort()).toEqual(["/forgot-password", "/invite", "/reset-password", "/sign-in"]);
  });

  it("match by whole segment, not by string prefix", () => {
    expect(isPublicPath("/sign-in")).toBe(true);
    expect(isPublicPath("/sign-in/factor")).toBe(true);
    expect(isPublicPath("/invite/abc")).toBe(true);
    expect(isPublicPath("/sign-inside")).toBe(false);
    expect(isPublicPath("/invitations")).toBe(false);
    expect(isPublicPath("/devices")).toBe(false);
  });
});

describe("?next=, which anyone can write into a link", () => {
  it("returns the page asked for", () => {
    expect(safeNext("/settings?tab=profile")).toBe("/settings?tab=profile");
    expect(safeNext("/devices/dev-1#inventory")).toBe("/devices/dev-1#inventory");
    // An encoded character in a console path is still a console page.
    expect(safeNext("/docs/a%20b?x=1#h")).toBe("/docs/a%20b?x=1#h");
  });

  it("falls back to the root when there is none", () => {
    // Non-vacuity for the refusals below: they would all pass on a filter
    // that ignored `next` entirely.
    expect(safeNext(null)).toBe(DEFAULT_AFTER_SIGN_IN);
    expect(safeNext(undefined)).toBe(DEFAULT_AFTER_SIGN_IN);
  });

  it.each([
    ["//evil.example", "protocol-relative: the browser resolves it to another ORIGIN"],
    ["https://evil.example", "absolute URL"],
    ["javascript:alert(1)", "a scheme that is not http at all"],
    ["/\\evil.example", "backslash form browsers normalise to //"],
    ["/\tevil", "control character browsers strip before resolving"],
    ["/\t/evil.example", "stripped, it becomes //evil.example"],
    ["not-a-path", "relative, so it would resolve against the sign-in page"],
    ["", "empty"],
    ["/sign-in", "the sign-in page itself: signing in would ask again"],
    ["/sign-in/factor?next=%2Fdevices", "a sign-in step"],
    ["/invite/abc", "a page reached without a session"],
    ["/x/../sign-in", "normalises to the sign-in page"],
    ["/%2e%2e/sign-in", "encoded dot-dot normalises to the sign-in page"],
    ["/i/abc123/windows", "an agent-installer download, not a console page"],
    ["/api/v1/me", "the API"],
    ["/enroll", "agent enrolment"],
    ["/mcp", "the MCP endpoint"],
    ["/.well-known/openid-configuration", "edge discovery"],
    ["/realms/itms", "the identity provider's path"],
    ["/resources/x", "the identity provider's assets"],
    ["/js/x", "the identity provider's scripts"],
    ["/x/../i/abc/windows", "normalises into an edge prefix"],
    ["/%69/abc/windows", "a percent-encoded letter the edge decodes into /i/"],
    ["/%2Fevil.example/x", "an encoded slash"],
    ["/%E0%A4%A", "a percent sequence that does not decode"],
    ["/sign%2Din", "an encoded hyphen spelling a public page"],
  ])("refuses %j and lands on the root (%s)", (hostile) => {
    expect(safeNext(hostile)).toBe(DEFAULT_AFTER_SIGN_IN);
  });

  it("refuses a value that is not a string", () => {
    expect(safeNext({ toString: () => "/settings" })).toBe(DEFAULT_AFTER_SIGN_IN);
    expect(safeNext(["/settings"])).toBe(DEFAULT_AFTER_SIGN_IN);
  });
});

describe("the session-ended flag", () => {
  it("is set by a 401, heard once, and stays set for the life of the page", () => {
    const heard = vi.fn();
    const stop = subscribeSessionEnd(heard);
    expect(isSessionEnded()).toBe(false);
    sessionEnded();
    sessionEnded();
    expect(isSessionEnded()).toBe(true);
    expect(heard).toHaveBeenCalledTimes(1);
    stop();
    resetSessionEnd();
    expect(isSessionEnded()).toBe(false);
  });
});
