/**
 * What the Keycloak console left in a tab's storage (final review D1, Minor
 * 10). `oidc-client-ts` kept the whole OIDC user -- access token, ID token,
 * possibly a refresh token -- in `sessionStorage["oidc.user:<authority>:
 * it-suite-web"]`, and its sign-in state under `oidc.*` keys. A tab left open
 * across the deploy reloads into the new console with all of it still there:
 * nothing reads it, and the ID token's personal data stays readable to any
 * script on the page. The console removes it at boot.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import { forgetOidcLeftovers } from "./oidcLeftovers";

afterEach(() => {
  // Mocks first: a test stubs the `localStorage` getter itself.
  vi.restoreAllMocks();
  window.localStorage.clear();
  window.sessionStorage.clear();
});

describe("forgetOidcLeftovers", () => {
  it("removes every oidc.* key from both storages and leaves the console's own keys", () => {
    window.sessionStorage.setItem("oidc.user:https://itms.example/realms/itms:it-suite-web", '{"access_token":"eyJ"}');
    window.sessionStorage.setItem("oidc.3f9a1c", '{"code_verifier":"x"}');
    window.localStorage.setItem("oidc.user:https://itms.example/realms/itms:it-suite-web", '{"id_token":"eyJ"}');
    window.localStorage.setItem("oidc.signin.state", "s");
    window.localStorage.setItem("itms.lang", "es");
    window.localStorage.setItem("itms.client", "all");
    window.sessionStorage.setItem("itms.fixtures", "1");

    forgetOidcLeftovers();

    expect(Object.keys(window.sessionStorage)).toEqual(["itms.fixtures"]);
    expect(Object.keys(window.localStorage).sort()).toEqual(["itms.client", "itms.lang"]);
  });

  it("does not throw where storage is blocked", () => {
    vi.spyOn(Storage.prototype, "key").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    window.localStorage.setItem("oidc.user:x", "y");
    expect(() => forgetOidcLeftovers()).not.toThrow();
  });

  it("does not throw, and still sweeps the other one, where reading localStorage itself throws (final re-review, M3)", () => {
    // Chromium with site data blocked throws SecurityError from the GETTER,
    // before any method is called. Thrown before `createRoot`, that was a
    // blank page instead of the sign-in page saying what is wrong.
    window.sessionStorage.setItem("oidc.user:x", "y");
    vi.spyOn(window, "localStorage", "get").mockImplementation(() => {
      throw new DOMException("The operation is insecure.", "SecurityError");
    });
    expect(() => forgetOidcLeftovers()).not.toThrow();
    expect(window.sessionStorage.getItem("oidc.user:x")).toBeNull();
  });

  it("runs at boot, before the console renders", () => {
    const main = readFileSync(resolve(import.meta.dirname, "../main.tsx"), "utf8");
    const call = main.indexOf("forgetOidcLeftovers()");
    expect(call).toBeGreaterThan(-1);
    expect(call).toBeLessThan(main.indexOf("createRoot("));
  });
});
