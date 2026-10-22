/**
 * WHY A REDIRECT AND NOT A LINK. The account console is a SECOND APPLICATION:
 * different chrome, different navigation, and it says the vendor's name on it.
 * An application-initiated action runs ONE step — set a password, enrol a key —
 * inside the same themed pages the user signs in on, then returns to ITMS.
 *
 * The `kc_action` parameter is what selects that step. If it is dropped, the
 * redirect degrades into an ordinary re-authentication: the user is bounced to
 * sign-in, comes back, and NOTHING HAPPENS. That is the failure this file
 * exists for, and it is silent — no error, no console message, just a button
 * that does not work.
 *
 * HOW THIS IS MOCKED, AND WHY NOT THE OBVIOUS WAY. The plan's sketch mocked
 * `./auth` itself and replaced its `userManager` export. That cannot work:
 * `startAccountAction` resolves `userManager` through the module-local
 * binding, not through the export object a mock would swap, so the spy would
 * never be called and the test would pass on an implementation that redirected
 * nowhere. The seam that actually exists is the `UserManager` CONSTRUCTOR, so
 * `oidc-client-ts` is what gets mocked and `auth.ts` runs for real.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

const { signinRedirect } = vi.hoisted(() => ({ signinRedirect: vi.fn() }));

vi.mock("oidc-client-ts", () => {
  class UserManager {
    // A STRICT NAMESPACE: every event `lib/auth.ts` subscribes to must be
    // here or the module throws at import. `addSilentRenewError` joined the
    // list when the session-expiry notice landed.
    events = {
      addUserLoaded: vi.fn(),
      addUserUnloaded: vi.fn(),
      addSilentRenewError: vi.fn(),
    };
    signinRedirect = signinRedirect;
    getUser = vi.fn().mockResolvedValue(null);
  }
  class WebStorageStateStore {}
  class User {}
  return { User, UserManager, WebStorageStateStore };
});

beforeEach(() => {
  signinRedirect.mockReset();
  signinRedirect.mockResolvedValue(undefined);
});

describe("startAccountAction", () => {
  it("asks the identity provider for exactly the requested action", async () => {
    const { startAccountAction } = await import("./auth");

    await startAccountAction("UPDATE_PASSWORD");

    expect(signinRedirect).toHaveBeenCalledWith(
      expect.objectContaining({ extraQueryParams: { kc_action: "UPDATE_PASSWORD" } }),
    );
  });

  it("carries each of the three steps through unmodified", async () => {
    // A mapping layer between the button and the wire is one more place for
    // these to drift. The values ITMS ships ARE the protocol's values.
    const { startAccountAction } = await import("./auth");

    for (const action of [
      "UPDATE_PASSWORD",
      "CONFIGURE_TOTP",
      "webauthn-register-passwordless",
    ] as const) {
      signinRedirect.mockClear();
      await startAccountAction(action);
      expect(signinRedirect).toHaveBeenCalledWith(
        expect.objectContaining({ extraQueryParams: { kc_action: action } }),
      );
    }
  });

  it("comes back to the page that started it, not to the app root", async () => {
    const { startAccountAction } = await import("./auth");

    await startAccountAction("CONFIGURE_TOTP");

    const [args] = signinRedirect.mock.calls[0] as [{ state?: { returnTo?: string } }];
    expect(args.state?.returnTo).toBe(window.location.pathname + window.location.search);
  });

  it("refuses an action it does not know", async () => {
    const { startAccountAction } = await import("./auth");
    // @ts-expect-error deliberately outside the union
    await expect(startAccountAction("DELETE_ACCOUNT")).rejects.toThrow(/unsupported/i);
    expect(signinRedirect).not.toHaveBeenCalled();
  });
});

/**
 * Signing in returns you to where you were (UX remediation cycle 2 remnant).
 *
 * TWO FINDINGS, ONE CAUSE. Auditor A, MEDIUM: "Navigating directly to
 * /dashboard while unauthenticated redirects to /login. After signing in, the
 * app lands on /devices — not the route originally requested. Any bookmarked
 * or shared deep link silently dumps the user on an unrelated page." And
 * auditor A, HIGH: "The session drops mid-task during continuous active use,
 * with no warning, no expiry notice, and total loss of context."
 *
 * They are the same defect seen from two ends. `signIn()` recorded nothing, so
 * `safeReturnTo(undefined)` fell through to the default route -- which is
 * merely annoying when you followed a link, and is "total loss of context"
 * when you were seven fields into a change request and your token expired.
 *
 * `startAccountAction` already carried `returnTo`; sign-in did not. It does
 * now, through the same filtered path, so the open-redirect refusals in
 * `safeReturnTo` cover both.
 */
describe("signIn", () => {
  it("records the page the user was on, so re-authentication returns them to it", async () => {
    const { signIn } = await import("./auth");
    window.history.pushState({}, "", "/changes/new?category=incident");

    await signIn();

    expect(signinRedirect).toHaveBeenCalledWith(
      expect.objectContaining({
        state: { returnTo: "/changes/new?category=incident" },
      }),
    );
  });

  it("does not send the callback route back to itself", async () => {
    // Signing in FROM the callback would loop: complete, navigate to
    // /auth/callback, complete again with no code, fail. The one path that
    // must never be recorded.
    const { signIn } = await import("./auth");
    window.history.pushState({}, "", "/auth/callback?code=abc");

    await signIn();

    const passed = signinRedirect.mock.calls[0]?.[0] as
      | { state?: { returnTo?: string } }
      | undefined;
    expect(passed?.state?.returnTo).toBeUndefined();
  });
});

/**
 * THE SIGN-IN PAGE OPENS IN THE CONSOLE'S LANGUAGE before Keycloak knows who is
 * signing in. Until a person is identified there is no `locale` attribute to
 * read, so the only signals are what the request carries: OIDC `ui_locales`
 * (Keycloak's client-selected locale) and the browser's Accept-Language. The
 * console's own resolved language (saved account value, then this browser's
 * last choice, then the browser's language) is better than the last one, and
 * `ui_locales` is how to say it. It is a top-level request argument, not
 * `extraQueryParams`, so `kc_action` stays exactly what it was.
 */
describe("ui_locales", () => {
  it("sign-in asks for the console's current language", async () => {
    const { setActiveLanguage } = await import("../i18n/core");
    const { signIn } = await import("./auth");
    window.history.pushState({}, "", "/");

    setActiveLanguage("es");
    await signIn();
    expect(signinRedirect).toHaveBeenLastCalledWith(expect.objectContaining({ ui_locales: "es" }));

    setActiveLanguage("en");
    await signIn();
    expect(signinRedirect).toHaveBeenLastCalledWith(expect.objectContaining({ ui_locales: "en" }));
  });

  it("an account action asks for it too, beside kc_action", async () => {
    const { setActiveLanguage } = await import("../i18n/core");
    const { startAccountAction } = await import("./auth");

    setActiveLanguage("es");
    await startAccountAction("UPDATE_PASSWORD");

    expect(signinRedirect).toHaveBeenLastCalledWith(
      expect.objectContaining({
        ui_locales: "es",
        extraQueryParams: { kc_action: "UPDATE_PASSWORD" },
      }),
    );
    setActiveLanguage("en");
  });
});
