/**
 * The screen a failed sign-in check leaves the operator on, and whether anyone
 * who is not watching the pixels is told.
 *
 * THE FINDING. Auditor A, HIGH: "After the IdP session was destroyed, clicking
 * 'Sign in with SSO' navigates to Keycloak ... and the browser ends up back at
 * https://itms.toeshee.com/login showing the identical card. NO ERROR TEXT, NO
 * BANNER, NO TOAST, NO REASON. ... the user cannot tell whether they are
 * unauthorised, whether the IdP is down, whether their session expired, or
 * whether the button is broken". Their fix names the mechanism: "render them
 * in a `role="alert"`".
 *
 * RE-POINTED (own-auth Task 10). The two screens this held were the OIDC
 * callback's failure and an unreachable identity provider; both went with
 * `oidc-client-ts`. The finding did not: the guard can still fail to learn
 * whether there is a session -- the server down, a proxy's 502 -- and that
 * screen replaces a page the person expected, with nothing clicked and focus
 * nowhere useful. So it is an alert, its reason is inside it, and the
 * ordinary path (no session: off to sign in) announces nothing.
 *
 * WHY THIS FILE LIVES UNDER `shell/`. It asserts on `App.tsx`, whose own test
 * files belong to a different slice of this remediation programme. The
 * pre-shell auth screens are chrome by any reasonable reading.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { navigation } from "../lib/session";

const { ApiProvider } = await import("../lib/ApiContext");
const { RequireAuth } = await import("../App");

function guarded() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={queryClient}>
      <ApiProvider>
        <MemoryRouter>
          <RequireAuth>
            <p>secret</p>
          </RequireAuth>
        </MemoryRouter>
      </ApiProvider>
    </QueryClientProvider>
  );
}

function answerMe(answer: () => Promise<Response>): void {
  vi.stubGlobal("fetch", vi.fn(() => answer()));
}

beforeEach(() => {
  vi.spyOn(navigation, "assign").mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("a sign-in check that failed says so out loud", () => {
  it("announces a server that could not be reached, with the reason inside the alert", async () => {
    answerMe(() => Promise.reject(new TypeError("Failed to fetch")));
    render(guarded());

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/sign-in is unavailable/i);
    // INSIDE the alert, not merely somewhere on the page: an alert that
    // announces the headline and leaves the identifying fact outside it
    // reproduces the finding for exactly the users it was raised for.
    expect(alert).toHaveTextContent(/could not ask the server/i);
    expect(alert).toHaveTextContent(/Failed to fetch/);
  });

  it("announces a server that answered with an error, and words it", async () => {
    answerMe(() => Promise.resolve(new Response("", { status: 503 })));
    render(guarded());

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/sign-in is unavailable/i);
    expect(screen.queryByText("secret")).not.toBeInTheDocument();
  });

  it("stays silent while the visitor is on their way to sign in", async () => {
    // NON-VACUITY, and the reason the role is `alert` rather than a banner
    // that is always mounted: the ordinary path is a navigation that leaves
    // the page, and announcing it on the way out is noise every operator
    // would learn to ignore.
    answerMe(() => Promise.resolve(new Response('{"code":"auth_required"}', { status: 401 })));
    render(guarded());

    expect(await screen.findByText(/redirecting to sign-in/i)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
