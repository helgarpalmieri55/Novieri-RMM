/**
 * The three ways the console changes side -- sent to sign in, into the console
 * after signing in, signed out -- and the step the profile no longer starts.
 *
 * RE-POINTED FROM `auth.accountAction.test.ts` (own-auth Task 10). That file
 * held `lib/auth.ts` to its promises at Keycloak:
 *
 *   - `startAccountAction` sent exactly the `kc_action` asked for. THE
 *     CONSOLE STARTS NO STEP AT AN IDENTITY PROVIDER NOW: credentials are the
 *     platform's own and are changed on `/profile/security`. So the property
 *     becomes "no source file names `kc_action`" -- a reintroduced account
 *     action is a redirect to a product that is leaving.
 *   - `signIn()` recorded the page the person was on, and never the callback
 *     route. That is `goToSignIn`: the page travels as `?next=`, and a page
 *     reached without a session is never recorded and never starts a
 *     navigation (plan decision D20) -- the loop the callback exclusion
 *     prevented has the same shape.
 *   - `ui_locales` carried the console's language to Keycloak's pages. The
 *     sign-in page is the console's own and reads the language this browser
 *     stored (`itms.lang`), so the address carries none.
 *
 * `navigation` is spied on rather than `window.location`, which jsdom will
 * neither implement nor let a test replace.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "./api";
import { enterConsole, goToSignIn, navigation, signOut } from "./session";

let assign: ReturnType<typeof vi.spyOn>;
let replace: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  assign = vi.spyOn(navigation, "assign").mockImplementation(() => {});
  replace = vi.spyOn(navigation, "replace").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
  window.history.pushState({}, "", "/");
});

describe("goToSignIn", () => {
  it("records the page the person was on, so signing in returns them to it", () => {
    goToSignIn("/changes/new?category=incident");
    expect(assign).toHaveBeenCalledWith("/sign-in?next=%2Fchanges%2Fnew%3Fcategory%3Dincident");
  });

  it("carries no language and no other parameter: the sign-in page reads the stored one", () => {
    goToSignIn("/devices");
    const [url] = assign.mock.calls[0] as [string];
    expect([...new URL(url, "https://itms.example").searchParams.keys()]).toEqual(["next"]);
  });

  it("does nothing from a page reached without a session (D20)", () => {
    for (const page of ["/sign-in", "/sign-in/factor", "/forgot-password", "/reset-password/t", "/invite/t"]) {
      window.history.pushState({}, "", page);
      goToSignIn("/devices");
    }
    expect(assign).not.toHaveBeenCalled();
  });
});

describe("enterConsole", () => {
  it("replaces the sign-in step with the page asked for, filtered", () => {
    enterConsole("/settings?tab=users");
    enterConsole("//evil.example");
    expect(replace.mock.calls).toEqual([["/settings?tab=users"], ["/"]]);
    expect(assign).not.toHaveBeenCalled();
  });
});

describe("signOut", () => {
  it("asks the server to end the session, then leaves for the sign-in page", async () => {
    const logout = vi.fn().mockResolvedValue(undefined);
    await signOut({ logout });
    expect(logout).toHaveBeenCalledOnce();
    expect(assign).toHaveBeenCalledWith("/sign-in");
  });

  it("counts a 401 as signed out: the session had already ended", async () => {
    await signOut({ logout: vi.fn().mockRejectedValue(new ApiError(401, "")) });
    expect(assign).toHaveBeenCalledWith("/sign-in");
  });

  it("stays, and rejects, when the server never heard -- the session is still open", async () => {
    await expect(signOut({ logout: vi.fn().mockRejectedValue(new TypeError("Failed to fetch")) })).rejects.toThrow(
      "Failed to fetch",
    );
    await expect(signOut({ logout: vi.fn().mockRejectedValue(new ApiError(503, "")) })).rejects.toBeInstanceOf(
      ApiError,
    );
    expect(assign).not.toHaveBeenCalled();
  });
});
