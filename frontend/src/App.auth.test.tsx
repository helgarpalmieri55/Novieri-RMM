/**
 * What an unauthenticated visitor sees.
 *
 * THE LAUNCHER WAS ACCIDENTALLY COVERING TWO FAILURES. A page with a button
 * renders whether or not Keycloak is reachable; an automatic redirect has
 * nothing to render if the redirect itself fails. That is not hypothetical --
 * it is the defect recorded in `lib/auth.ts`'s header, which cost a live
 * afternoon on this deployment: a login button that did nothing, repeated
 * 0-byte discovery fetches, and no error anywhere, because no request ever
 * left the laptop.
 *
 * So both of these are required tests rather than niceties
 * (`docs/modules/console-auth/spec.md` §5.5 and §5.6).
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import { useEffect } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { setSelectedClient } from "./lib/clientChoice";

const signIn = vi.fn();
const completeSignIn = vi.fn();
const isAuthenticated = vi.fn();

// `safeReturnTo` IS DELIBERATELY NOT MOCKED. It is the open-redirect filter
// itself, so a stub of it would make every refusal below assert that a fake
// said no. `importActual` pulls the real function through the mock; the rest of
// the module stays stubbed, so no `UserManager` of ours ever talks to a
// network.
vi.mock("./lib/auth", async () => ({
  // `await import(...)` here would resolve to THIS mock and recurse;
  // `importActual` is the only form that reaches the real module.
  safeReturnTo: (await vi.importActual<typeof import("./lib/auth")>("./lib/auth"))
    .safeReturnTo,
  signIn: () => signIn(),
  completeSignIn: () => completeSignIn(),
  isAuthenticated: () => isAuthenticated(),
  initAuth: () => Promise.resolve(null),
  signOut: () => Promise.resolve(),
  accessToken: () => null,
  // The value the error state must NAME. A blank page tells an operator
  // nothing; the authority is the single fact that identifies the problem.
  oidcAuthority: "https://itms.example/realms/it-suite",
  // The session-expiry notice's two exports. `Shell` subscribes on mount, so
  // a mock missing them fails at render rather than at import -- which reads
  // like a routing bug and is not one.
  isSessionExpiring: () => false,
  onSessionExpiring: () => () => {},
  userManager: { events: { addUserLoaded: () => {}, addUserUnloaded: () => {} } },
}));

const { ApiProvider } = await import("./lib/ApiContext");
const { RequireAuth, CallbackPage } = await import("./App");

beforeEach(() => {
  signIn.mockReset().mockResolvedValue(undefined);
  completeSignIn.mockReset().mockRejectedValue(new Error("state mismatch"));
  isAuthenticated.mockReset().mockReturnValue(false);
});

describe("an unauthenticated visitor", () => {
  it("is sent to Keycloak rather than shown a launcher", async () => {
    render(
      <MemoryRouter>
        <RequireAuth>
          <p>secret</p>
        </RequireAuth>
      </MemoryRouter>,
    );
    await waitFor(() => expect(signIn).toHaveBeenCalledTimes(1));
    expect(screen.queryByText("secret")).not.toBeInTheDocument();
  });

  it("is TOLD when sign-in is unavailable, and which authority failed", async () => {
    signIn.mockRejectedValue(new Error("Failed to fetch openid-configuration"));
    render(
      <MemoryRouter>
        <RequireAuth>
          <p>secret</p>
        </RequireAuth>
      </MemoryRouter>,
    );
    expect(await screen.findByText(/sign-in is unavailable/i)).toBeInTheDocument();
    expect(screen.getByText(/realms\/it-suite/)).toBeInTheDocument();
  });

  it("redirects once, not on every render", async () => {
    // `signinRedirect()` navigates the whole page; firing it twice starts two
    // authorization requests, and the second overwrites the first's PKCE state
    // in sessionStorage. The symptom is a callback that fails with "state
    // mismatch" after a login that looked fine -- and React strict mode runs
    // effects twice on purpose, so this is the default outcome, not an edge.
    const ui = (
      <MemoryRouter>
        <RequireAuth>
          <p>secret</p>
        </RequireAuth>
      </MemoryRouter>
    );
    const { rerender } = render(ui);
    await waitFor(() => expect(signIn).toHaveBeenCalledTimes(1));
    rerender(ui);
    expect(signIn).toHaveBeenCalledTimes(1);
  });

  it("renders the page once authenticated, with no redirect", async () => {
    // Non-vacuity for all three above: a guard that redirected unconditionally
    // would pass them and lock every signed-in operator out of the console.
    //
    // The providers are here because an authenticated render reaches `Shell`,
    // which asks the API who the user is -- that is the path being asserted,
    // so it is exercised rather than stubbed away.
    isAuthenticated.mockReturnValue(true);
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <ApiProvider>
          <MemoryRouter>
            <RequireAuth>
              <p>secret</p>
            </RequireAuth>
          </MemoryRouter>
        </ApiProvider>
      </QueryClientProvider>,
    );
    expect(await screen.findByText("secret")).toBeInTheDocument();
    expect(signIn).not.toHaveBeenCalled();
  });

  it("starts the page again, on an empty cache, when the client changes (phase 3 design §1.4)", async () => {
    isAuthenticated.mockReturnValue(true);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let mounts = 0;
    function Secret() {
      useEffect(() => {
        mounts += 1;
      }, []);
      return <p>secret</p>;
    }
    render(
      <QueryClientProvider client={queryClient}>
        <ApiProvider>
          <MemoryRouter>
            <RequireAuth>
              <Secret />
            </RequireAuth>
          </MemoryRouter>
        </ApiProvider>
      </QueryClientProvider>,
    );
    expect(await screen.findByText("secret")).toBeInTheDocument();
    queryClient.setQueryData(["devices"], ["a device of every client"]);

    act(() => setSelectedClient("6b1f2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"));

    await waitFor(() => expect(mounts).toBe(2));
    expect(queryClient.getQueryData(["devices"])).toBeUndefined();
  });
});

describe("a failed callback", () => {
  it("shows the error instead of re-entering signIn", async () => {
    // A loop here is indistinguishable from a hung page: the browser bounces
    // between the console and Keycloak, and nothing on screen ever says why.
    render(
      <MemoryRouter initialEntries={["/auth/callback"]}>
        <CallbackPage />
      </MemoryRouter>,
    );
    expect(await screen.findByText(/sign-in failed/i)).toBeInTheDocument();
    expect(signIn).not.toHaveBeenCalled();
  });
});

describe("coming back from a credential step", () => {
  /**
   * THE PROMISE THE PROFILE PANEL MAKES. `startAccountAction` records the page
   * that started the action in the OIDC `state`, and the whole point of an
   * application-initiated action -- rather than a link to a second product --
   * is that the user lands back where they were. The callback used to navigate
   * to a fixed route, so changing a password from Settings dropped the user on
   * the device list.
   *
   * AND `state` COMES BACK FROM THE IDENTITY PROVIDER, so it is untrusted
   * input. `navigate("//evil.example")` is a genuine open redirect: the
   * history API resolves a protocol-relative path against the current scheme
   * and leaves the origin. So the refusals below are not paranoia about our own
   * code -- they are the reason this value is filtered at all.
   */
  function Probe() {
    const loc = useLocation();
    return <p data-testid="landed">{loc.pathname + loc.search}</p>;
  }

  async function landAfter(state: unknown): Promise<string> {
    completeSignIn.mockResolvedValue({ state });
    render(
      <MemoryRouter initialEntries={["/auth/callback"]}>
        <Routes>
          <Route path="/auth/callback" element={<CallbackPage />} />
          <Route path="*" element={<Probe />} />
        </Routes>
      </MemoryRouter>,
    );
    const landed = await screen.findByTestId("landed");
    return landed.textContent ?? "";
  }

  it("returns to the page that started the action", async () => {
    expect(await landAfter({ returnTo: "/settings?tab=profile" })).toBe("/settings?tab=profile");
  });

  it("falls back to the default page when the provider returns no state", async () => {
    // Non-vacuity for the refusals below: they would all pass on an
    // implementation that ignored `returnTo` entirely, which is the bug being
    // fixed. This pins the fallback, and the test above pins the honoured path.
    expect(await landAfter(undefined)).toBe("/");
  });

  it.each([
    ["//evil.example", "protocol-relative: history resolves it to another ORIGIN"],
    ["https://evil.example", "absolute URL"],
    ["javascript:alert(1)", "scheme that is not http at all"],
    ["/\\evil.example", "backslash form browsers normalise to //"],
    ["/\tevil", "control character browsers strip before resolving"],
    ["not-a-path", "relative, so it would resolve against the callback route"],
    ["", "empty, which is falsy and must not become a navigate('')"],
  ])("refuses %j and lands on the default page", async (hostile) => {
    expect(await landAfter({ returnTo: hostile })).toBe("/");
  });

  it("refuses a returnTo that is not a string", async () => {
    // `state` is JSON from the wire; nothing guarantees the shape ITMS wrote.
    expect(await landAfter({ returnTo: { toString: () => "/settings" } })).toBe("/");
    expect(await landAfter("/settings")).toBe("/");
  });
});
