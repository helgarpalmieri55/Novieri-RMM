/**
 * What a visitor with no session sees, and what a session that ends does.
 *
 * RE-POINTED (own-auth Task 10). This file held the guard to its Keycloak
 * promises: an unauthenticated visitor was redirected to the identity
 * provider at once, a failed redirect named the authority it tried, and the
 * OIDC callback honoured a filtered `returnTo`. The console signs in on its
 * own pages now, and the same promises hold on the new path:
 *
 *   - no session -> the sign-in page, once, carrying the page asked for;
 *   - a server that cannot say -> a screen that says so, with the reason,
 *     instead of a blank page or a redirect that blames the session;
 *   - a session -> the page, with no redirect (non-vacuity for the above);
 *   - a session that ENDS while the person is here -> the page stays, and the
 *     shell says so (auditor A, HIGH: "the session drops mid-task ... total
 *     loss of context").
 *
 * The open-redirect refusals moved with the filter they test, to
 * `lib/session.test.ts` (`safeNext`).
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode, useEffect } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setSelectedClient } from "./lib/clientChoice";
import { navigation } from "./lib/session";
import { sessionEnded } from "./lib/sessionEnd";

const { ApiProvider } = await import("./lib/ApiContext");
const { RequireAuth } = await import("./App");

const ME = {
  user_id: "u1",
  display_name: "Ada Lovelace",
  email: "ada@example.test",
  org_id: "org-1",
  roles: ["org-admin"],
  permissions: ["devices.read"],
};

type Answer = { status: number; body?: unknown } | Error;

/** `fetch`: `/api/v1/me` answers `me`, signing out answers 204, every other path 404. */
function stubServer(me: () => Answer): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const answer: Answer = url.endsWith("/api/v1/me")
      ? me()
      : url.endsWith("/api/v1/auth/logout")
        ? { status: 204 }
        : { status: 404, body: {} };
    if (answer instanceof Error) throw answer;
    return new Response(answer.body === undefined ? null : JSON.stringify(answer.body), { status: answer.status });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function guarded(path = "/devices", child: React.ReactNode = <p>secret</p>) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui = (
    <QueryClientProvider client={queryClient}>
      <ApiProvider>
        <MemoryRouter initialEntries={[path]}>
          <RequireAuth>{child}</RequireAuth>
        </MemoryRouter>
      </ApiProvider>
    </QueryClientProvider>
  );
  return { queryClient, ui };
}

let assign: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  assign = vi.spyOn(navigation, "assign").mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("a visitor with no session", () => {
  it("is sent to the sign-in page with the page they asked for, rather than shown it", async () => {
    stubServer(() => ({ status: 401, body: { code: "auth_required" } }));
    render(guarded("/devices?x=1").ui);
    await waitFor(() => expect(assign).toHaveBeenCalledTimes(1));
    expect(assign).toHaveBeenCalledWith("/sign-in?next=%2Fdevices%3Fx%3D1");
    expect(screen.queryByText("secret")).not.toBeInTheDocument();
  });

  it("is TOLD when the server cannot say whether there is a session, and why", async () => {
    stubServer(() => new TypeError("Failed to fetch"));
    render(guarded().ui);
    expect(await screen.findByText(/sign-in is unavailable/i)).toBeInTheDocument();
    expect(screen.getByText(/Failed to fetch/)).toBeInTheDocument();
    // A server that is down is not a session that ended: nobody is sent away.
    expect(assign).not.toHaveBeenCalled();
  });

  it("is sent once, not on every render", async () => {
    // The navigation replaces the document; firing it twice is two loads.
    // The session is already marked ended BEFORE the first render, so
    // `mustSignIn` is true on mount and StrictMode's double-run of the
    // effect would navigate twice -- without the `leaving` ref.
    stubServer(() => ({ status: 401 }));
    sessionEnded();
    const { ui } = guarded();
    const { rerender } = render(<StrictMode>{ui}</StrictMode>);
    await waitFor(() => expect(assign).toHaveBeenCalledTimes(1));
    rerender(<StrictMode>{ui}</StrictMode>);
    expect(assign).toHaveBeenCalledTimes(1);
  });
});

describe("a signed-in person", () => {
  it("gets the page, with no redirect", async () => {
    // Non-vacuity for all three above: a guard that redirected
    // unconditionally would pass them and lock every signed-in operator out.
    stubServer(() => ({ status: 200, body: ME }));
    render(guarded().ui);
    expect(await screen.findByText("secret")).toBeInTheDocument();
    expect(assign).not.toHaveBeenCalled();
  });

  it("starts the page again, on an empty cache, when the client changes (phase 3 design §1.4)", async () => {
    stubServer(() => ({ status: 200, body: ME }));
    let mounts = 0;
    function Secret() {
      useEffect(() => {
        mounts += 1;
      }, []);
      return <p>secret</p>;
    }
    const { queryClient, ui } = guarded("/devices", <Secret />);
    render(ui);
    expect(await screen.findByText("secret")).toBeInTheDocument();
    queryClient.setQueryData(["devices"], ["a device of every client"]);

    act(() => setSelectedClient("6b1f2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"));

    await waitFor(() => expect(mounts).toBe(2));
    expect(queryClient.getQueryData(["devices"])).toBeUndefined();
  });
});

describe("a session that ends while the person is here", () => {
  it("keeps the page, says so, and leaves only when they choose to", async () => {
    stubServer(() => ({ status: 200, body: ME }));
    const user = userEvent.setup();
    render(guarded("/settings?tab=users").ui);
    expect(await screen.findByText("secret")).toBeInTheDocument();

    act(() => sessionEnded());

    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent(/session has expired/i);
    expect(screen.getByText("secret")).toBeInTheDocument();
    expect(assign).not.toHaveBeenCalled();

    await user.click(within(notice).getByRole("button", { name: /sign in again/i }));
    expect(assign).toHaveBeenCalledWith("/sign-in?next=%2Fsettings%3Ftab%3Dusers");
  });
});

describe("signing out", () => {
  it("asks the server, then leaves for the sign-in page", async () => {
    const fetchMock = stubServer(() => ({ status: 200, body: ME }));
    const user = userEvent.setup();
    render(guarded().ui);
    await screen.findByText("secret");

    await user.click(screen.getByRole("button", { name: /Ada Lovelace/ }));
    await user.click(screen.getByRole("menuitem", { name: "Sign out" }));

    await waitFor(() => expect(assign).toHaveBeenCalledWith("/sign-in"));
    const logout = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/api/v1/auth/logout"));
    expect(logout).toBeDefined();
    expect((logout as [string, RequestInit])[1].method).toBe("POST");
  });

  it("says the session is still open when the server never heard, and stays", async () => {
    stubServer(() => ({ status: 200, body: ME }));
    const user = userEvent.setup();
    render(guarded().ui);
    await screen.findByText("secret");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).endsWith("/api/v1/auth/logout")) throw new TypeError("Failed to fetch");
        return new Response(JSON.stringify(ME), { status: 200 });
      }),
    );

    await user.click(screen.getByRole("button", { name: /Ada Lovelace/ }));
    await user.click(screen.getByRole("menuitem", { name: "Sign out" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/your session is still open/i);
    expect(assign).not.toHaveBeenCalled();
  });
});
