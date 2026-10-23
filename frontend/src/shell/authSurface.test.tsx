/**
 * The two screens a failed sign-in leaves the operator on, and whether anyone
 * who is not watching the pixels is told.
 *
 * THE FINDING. Auditor A, HIGH: "After the IdP session was destroyed, clicking
 * 'Sign in with SSO' navigates to Keycloak ... and the browser ends up back at
 * https://itms.toeshee.com/login showing the identical card. NO ERROR TEXT, NO
 * BANNER, NO TOAST, NO REASON. Repeating the click reproduces the loop ... the
 * user cannot tell whether they are unauthorised, whether the IdP is down,
 * whether their session expired, or whether the button is broken -- the three
 * states are visually identical." Their fix names the mechanism: "render them
 * in a `role="alert"` on the login card".
 *
 * Earlier cycles gave both surfaces the TEXT (`App.auth.test.tsx` pins the
 * wording and the named authority). What was still missing is the
 * announcement. Both screens are reached by a redirect the browser performed
 * on its own: focus is nowhere useful, nothing was clicked, and the page
 * simply changed. An unannounced paragraph at the end of a redirect chain is
 * the same silence the audit measured, for anyone using a screen reader.
 *
 * WHY THIS FILE LIVES UNDER `shell/`. It asserts on `App.tsx`, whose own test
 * files belong to a different slice of this remediation programme. The
 * pre-shell auth screens are chrome by any reasonable reading, and the
 * alternative -- an assertion nobody may add -- is a fix with no guard.
 */

import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const signIn = vi.fn();
const completeSignIn = vi.fn();
const isAuthenticated = vi.fn();

// The same seam `App.auth.test.tsx` uses, and for the same reason: `App.tsx`
// resolves these through the module binding, so mocking `./lib/auth` is what
// keeps a real `UserManager` from reaching for a network during a unit test.
vi.mock("../lib/auth", async () => ({
  safeReturnTo: (await vi.importActual<typeof import("../lib/auth")>("../lib/auth"))
    .safeReturnTo,
  signIn: () => signIn(),
  completeSignIn: () => completeSignIn(),
  isAuthenticated: () => isAuthenticated(),
  initAuth: () => Promise.resolve(null),
  signOut: () => Promise.resolve(),
  accessToken: () => null,
  oidcAuthority: "https://itms.example/realms/it-suite",
  isSessionExpiring: () => false,
  onSessionExpiring: () => () => {},
  userManager: { events: { addUserLoaded: () => {}, addUserUnloaded: () => {} } },
}));

const { RequireAuth, CallbackPage } = await import("../App");

beforeEach(() => {
  signIn.mockReset().mockResolvedValue(undefined);
  completeSignIn.mockReset().mockRejectedValue(new Error("state mismatch"));
  isAuthenticated.mockReset().mockReturnValue(false);
});

describe("a sign-in that failed says so out loud", () => {
  it("announces the callback failure, rather than only printing it", async () => {
    render(
      <MemoryRouter initialEntries={["/auth/callback"]}>
        <CallbackPage />
      </MemoryRouter>,
    );

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/sign-in failed/i);
    // The reason travels with it. "Something went wrong" and "state mismatch"
    // send an operator to two different places.
    expect(alert).toHaveTextContent(/state mismatch/);
  });

  it("announces an unreachable identity provider, and names it inside the alert", async () => {
    signIn.mockRejectedValue(new Error("Failed to fetch openid-configuration"));

    render(
      <MemoryRouter>
        <RequireAuth>
          <p>secret</p>
        </RequireAuth>
      </MemoryRouter>,
    );

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/sign-in is unavailable/i);
    // INSIDE the alert, not merely somewhere on the page: an alert that
    // announces the headline and leaves the one identifying fact outside it
    // reproduces the finding for exactly the users it was raised for.
    expect(alert).toHaveTextContent(/realms\/it-suite/);
  });

  it("stays silent while the redirect is still in flight", async () => {
    // NON-VACUITY, and the reason the role is `alert` rather than a banner
    // that is always mounted: the ordinary path is a redirect that leaves the
    // page, and announcing "Redirecting to sign-in…" on the way out is noise
    // every operator would learn to ignore.
    render(
      <MemoryRouter>
        <RequireAuth>
          <p>secret</p>
        </RequireAuth>
      </MemoryRouter>,
    );

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText(/redirecting to sign-in/i)).toBeInTheDocument();
  });
});
