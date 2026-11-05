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
 *   - a session that ENDS while the person is here -> HIDDEN AND EMPTIED
 *     (operator decision 3, 2026-11-05, "Ocultar y vaciar"): the query cache
 *     is cleared, the shell and the page give way to the session-ended
 *     notice, and what the person had typed -- never a password -- is shown
 *     read-only so it can be copied. RE-POINTED from "keeps the page, says
 *     so": auditor A's HIGH finding ("the session drops mid-task ... total
 *     loss of context") is now answered by the kept text, not by leaving a
 *     tenant's data on an unattended screen (final review D1, Minor 1). It
 *     holds for every way the end is learnt: a 401, a `/ws` 4401, another
 *     person in the cookie (C3), and signing out;
 *   - a 403 at `/me` (a refusal, not an outage) -> the server's sentence and
 *     a way to sign out (D1, Minor 7);
 *   - an unknown path under a public prefix -> the sign-in page, not an
 *     endless "Redirecting" (D1, Minor 8).
 *
 * The open-redirect refusals moved with the filter they test, to
 * `lib/session.test.ts` (`safeNext`).
 */

import { QueryClient, QueryClientProvider, useMutation, useQuery } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode, useEffect, useState } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CLIENT_STORAGE_KEY, setSelectedClient } from "./lib/clientChoice";
import { navigation } from "./lib/session";
import { sessionEnded } from "./lib/sessionEnd";
import { CLOSE_SESSION_ENDED, useLiveEvents } from "./lib/ws";

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

/** Tenant data the page draws, from the query cache. */
const TENANT_ROW = "laptop-7 of Acme Ltd";
const TYPED = "Restarted the print spooler; ask Bob to try again.";
const PASSWORD = "hunter2-hunter2-hunter2";

/**
 * A page with tenant data in the cache, a form, and a mutation that sent a
 * password -- and, with `live`, the live socket.
 */
function TenantPage({ live = false }: { live?: boolean }) {
  const devices = useQuery({ queryKey: ["devices"], queryFn: async () => [TENANT_ROW], staleTime: Infinity });
  const save = useMutation({ mutationFn: async (_body: { password: string }) => "saved" });
  const [reply, setReply] = useState("");
  const [password, setPassword] = useState("");
  return (
    <div>
      {live ? <Live /> : null}
      <ul>
        {devices.data?.map((row) => (
          <li key={row}>{row}</li>
        ))}
      </ul>
      <label>
        <span>Reply to the requester</span>
        <textarea value={reply} onChange={(event) => setReply(event.target.value)} />
      </label>
      <label>
        <span>Their new password</span>
        <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} />
      </label>
      <button type="button" onClick={() => save.mutate({ password })}>
        Save the password
      </button>
    </div>
  );
}

function Live() {
  useLiveEvents(() => {});
  return null;
}

class FakeSocket {
  static instances: FakeSocket[] = [];
  onopen: (() => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  closed = false;
  constructor(readonly url: string) {
    FakeSocket.instances.push(this);
  }
  close(): void {
    this.closed = true;
  }
}

/** Every query that holds an answer, by key. */
function cachedData(queryClient: QueryClient): unknown[] {
  return queryClient
    .getQueryCache()
    .getAll()
    .filter((query) => query.state.data !== undefined)
    .map((query) => query.queryKey);
}

function cacheText(queryClient: QueryClient): string {
  return JSON.stringify([
    queryClient.getQueryCache().getAll().map((query) => query.state.data),
    queryClient.getMutationCache().getAll().map((mutation) => [mutation.state.variables, mutation.state.data]),
  ]);
}

/** The document as drawn, with the value of every box in it. */
function everythingShown(): string {
  const values = Array.from(document.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>("input, textarea")).map(
    (field) => field.value,
  );
  return [document.body.innerHTML, ...values].join("\n");
}

/** The notice, and nothing of the tenant's: no page, no shell, no cache. */
async function expectEnded(queryClient: QueryClient): Promise<void> {
  expect(await screen.findByRole("heading", { name: "Your session has ended" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /sign in again/i })).toBeInTheDocument();
  expect(screen.queryByText(TENANT_ROW)).not.toBeInTheDocument();
  expect(screen.queryByText(/Ada Lovelace/)).not.toBeInTheDocument();
  expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Save the password" })).not.toBeInTheDocument();
  expect(cachedData(queryClient)).toEqual([]);
  expect(queryClient.getMutationCache().getAll()).toEqual([]);
}

let assign: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  assign = vi.spyOn(navigation, "assign").mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.localStorage.clear();
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
  it("hides the shell and the page, empties the cache, and keeps what they typed to copy -- never a password", async () => {
    // RE-POINTED from "keeps the page, says so, and leaves only when they
    // choose to" (operator decision 3). The end is learnt the real way here:
    // `/me` is asked again and the server answers 401.
    let me: Answer = { status: 200, body: ME };
    stubServer(() => me);
    const user = userEvent.setup();
    const { queryClient, ui } = guarded("/settings?tab=users", <TenantPage />);
    render(ui);
    expect(await screen.findByText(TENANT_ROW)).toBeInTheDocument();
    await user.type(screen.getByLabelText("Reply to the requester"), TYPED);
    await user.type(screen.getByLabelText("Their new password"), PASSWORD);
    await user.click(screen.getByRole("button", { name: "Save the password" }));
    await waitFor(() => expect(queryClient.getMutationCache().getAll()).toHaveLength(1));

    me = { status: 401, body: { code: "auth_required" } };
    await act(() => queryClient.invalidateQueries({ queryKey: ["me"] }));

    await expectEnded(queryClient);
    // What they typed, read-only, to copy.
    const kept = within(screen.getByRole("region", { name: "What you were typing" })).getByRole("textbox", {
      name: "Reply to the requester",
    });
    expect(kept).toHaveValue(TYPED);
    expect(kept).toHaveAttribute("readonly");
    // NEVER A PASSWORD: not on screen, not in a box, not in the cache.
    expect(everythingShown()).not.toContain(PASSWORD);
    expect(cacheText(queryClient)).not.toContain(PASSWORD);
    expect(assign).not.toHaveBeenCalled();
  });

  it("leaves only when they choose to: no redirect of its own, then one sign-in carrying the page (D20)", async () => {
    let me: Answer = { status: 200, body: ME };
    stubServer(() => me);
    const user = userEvent.setup();
    const { queryClient, ui } = guarded("/settings?tab=users", <TenantPage />);
    const { rerender } = render(<StrictMode>{ui}</StrictMode>);
    expect(await screen.findByText(TENANT_ROW)).toBeInTheDocument();

    me = { status: 401, body: { code: "auth_required" } };
    await act(() => queryClient.invalidateQueries({ queryKey: ["me"] }));
    await expectEnded(queryClient);

    // An emptied `/me` is not "a visitor with no session": nobody is sent away.
    rerender(<StrictMode>{ui}</StrictMode>);
    await act(() => queryClient.invalidateQueries());
    expect(assign).not.toHaveBeenCalled();
    expect(screen.queryByRole("region", { name: "What you were typing" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /sign in again/i }));
    expect(assign).toHaveBeenCalledExactlyOnceWith("/sign-in?next=%2Fsettings%3Ftab%3Dusers");
  });

  it("does the same when the live socket closes 4401 and `/me` then says the session ended", async () => {
    let me: Answer = { status: 200, body: ME };
    stubServer(() => me);
    vi.stubGlobal("WebSocket", FakeSocket);
    FakeSocket.instances = [];
    const { queryClient, ui } = guarded("/jobs", <TenantPage live />);
    render(ui);
    expect(await screen.findByText(TENANT_ROW)).toBeInTheDocument();
    const socket = await waitFor(() => {
      expect(FakeSocket.instances).toHaveLength(1);
      return FakeSocket.instances[0]!;
    });
    act(() => socket.onopen?.());

    // An administrator ended the session: the server's re-check closes 4401.
    me = { status: 401, body: { code: "auth_required" } };
    await act(async () => socket.onclose?.({ code: CLOSE_SESSION_ENDED }));

    await expectEnded(queryClient);
    // No socket opens again, at once or after a backoff.
    await act(() => new Promise((resolve) => setTimeout(resolve, 2_500)));
    expect(FakeSocket.instances).toHaveLength(1);
    expect(assign).not.toHaveBeenCalled();
  });

  it("does the same when the cookie now holds ANOTHER person's session: someone signed in in another tab (C3)", async () => {
    let me: Answer = { status: 200, body: ME };
    const fetchMock = stubServer(() => me);
    const user = userEvent.setup();
    const { queryClient, ui } = guarded("/devices", <TenantPage />);
    render(ui);
    expect(await screen.findByText(TENANT_ROW)).toBeInTheDocument();
    await user.type(screen.getByLabelText("Reply to the requester"), TYPED);

    me = { status: 200, body: { ...ME, user_id: "u2", display_name: "Grace Hopper", email: "grace@example.test" } };
    await act(() => queryClient.invalidateQueries({ queryKey: ["me"] }));

    await expectEnded(queryClient);
    // Neither person's name, and the page is not asked again under the new cookie.
    expect(screen.queryByText(/Grace Hopper/)).not.toBeInTheDocument();
    const asked = fetchMock.mock.calls.length;
    await act(() => queryClient.invalidateQueries());
    expect(fetchMock.mock.calls.length).toBe(asked);
    // RE-POINTED (final re-review, M4): the person at the browser now is the
    // one who just signed in, not the one who typed. Their text is not
    // theirs to read.
    expect(screen.queryByRole("region", { name: "What you were typing" })).not.toBeInTheDocument();
    expect(everythingShown()).not.toContain(TYPED);
    expect(assign).not.toHaveBeenCalled();
  });

  it("forgets the client chosen in the top bar at an end that is not a sign-out, too (final re-review, M5)", async () => {
    let me: Answer = { status: 200, body: ME };
    stubServer(() => me);
    const { queryClient, ui } = guarded("/devices", <TenantPage />);
    render(ui);
    expect(await screen.findByText(TENANT_ROW)).toBeInTheDocument();
    window.localStorage.setItem(CLIENT_STORAGE_KEY, "6b1f2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d");

    me = { status: 401, body: { code: "auth_required" } };
    await act(() => queryClient.invalidateQueries({ queryKey: ["me"] }));

    await expectEnded(queryClient);
    expect(window.localStorage.getItem(CLIENT_STORAGE_KEY)).toBeNull();
  });

  it("leaves the client alone when ANOTHER person signed in: it is theirs now (fix round 1 re-review, R1-I1)", async () => {
    // The new person chose this client in their own tab, in the same storage.
    // Removing it would fire a `storage` event there, switch them to "All my
    // clients" and remount their page under them.
    let me: Answer = { status: 200, body: ME };
    stubServer(() => me);
    const { queryClient, ui } = guarded("/devices", <TenantPage />);
    render(ui);
    expect(await screen.findByText(TENANT_ROW)).toBeInTheDocument();
    window.localStorage.setItem(CLIENT_STORAGE_KEY, "6b1f2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d");

    me = { status: 200, body: { ...ME, user_id: "u2", display_name: "Grace Hopper", email: "grace@example.test" } };
    await act(() => queryClient.invalidateQueries({ queryKey: ["me"] }));

    await expectEnded(queryClient);
    expect(window.localStorage.getItem(CLIENT_STORAGE_KEY)).toBe("6b1f2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d");
  });
});

describe("one stale 401 (final re-review, I1)", () => {
  it("leaves the page drawn when /me still answers for the same person", async () => {
    // The reviewer's probe: a request that left with the cookie a password
    // change just rotated is answered 401; the session lives on.
    let orgs = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/me")) return new Response(JSON.stringify(ME), { status: 200 });
        if (url.endsWith("/api/v1/orgs/current")) {
          orgs += 1;
          return new Response(JSON.stringify({ code: "auth_required" }), { status: 401 });
        }
        return new Response("{}", { status: 404 });
      }),
    );
    const { queryClient, ui } = guarded("/devices", <TenantPage />);
    render(ui);
    expect(await screen.findByText(TENANT_ROW)).toBeInTheDocument();
    await waitFor(() => expect(orgs).toBeGreaterThan(0));
    await act(() => new Promise((resolve) => setTimeout(resolve, 50)));

    expect(screen.queryByRole("heading", { name: "Your session has ended" })).not.toBeInTheDocument();
    expect(screen.getByText(TENANT_ROW)).toBeInTheDocument();
    expect(cachedData(queryClient)).toContainEqual(["devices"]);
  });

  it("ends the session when /me answers 401 too", async () => {
    let me: Answer = { status: 200, body: ME };
    stubServer(() => me);
    const { queryClient, ui } = guarded("/devices", <TenantPage />);
    render(ui);
    expect(await screen.findByText(TENANT_ROW)).toBeInTheDocument();
    me = { status: 401, body: { code: "auth_required" } };
    await act(() => queryClient.invalidateQueries({ queryKey: ["me"] }));
    await expectEnded(queryClient);
  });
});

describe("a refusal at `/me` (D1, Minor 7)", () => {
  it("shows the server's sentence and a way to sign out, rather than calling it an outage", async () => {
    stubServer(() => ({ status: 403, body: { detail: "This address may not sign in to the console." } }));
    const user = userEvent.setup();
    render(guarded().ui);

    expect(await screen.findByText("This address may not sign in to the console.")).toBeInTheDocument();
    expect(screen.queryByText(/could not ask the server/i)).not.toBeInTheDocument();
    expect(screen.queryByText("secret")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(assign).toHaveBeenCalledExactlyOnceWith("/sign-in"));
  });

  it("says the session is still open when that sign-out never reached the server", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).endsWith("/api/v1/auth/logout")) throw new TypeError("Failed to fetch");
        return new Response(JSON.stringify({ detail: "This address may not sign in to the console." }), { status: 403 });
      }),
    );
    const user = userEvent.setup();
    render(guarded().ui);
    await user.click(await screen.findByRole("button", { name: "Sign out" }));

    expect(await screen.findByText(/your session is still open/i)).toBeInTheDocument();
    expect(assign).not.toHaveBeenCalled();
  });
});

describe("an unknown path under a public prefix (D1, Minor 8)", () => {
  it.each(["/sign-in/x", "/invite/a-token-in-the-path"])("goes to the sign-in page from %s, once", async (path) => {
    // The document is at that path too, as it is in a browser: `goToSignIn`
    // reads `window.location` and refuses to leave a public path (D20).
    window.history.pushState({}, "", path);
    stubServer(() => ({ status: 401, body: { code: "auth_required" } }));
    const { ui } = guarded(path);
    const { rerender } = render(<StrictMode>{ui}</StrictMode>);
    await waitFor(() => expect(assign).toHaveBeenCalledExactlyOnceWith("/sign-in"));
    rerender(<StrictMode>{ui}</StrictMode>);
    expect(assign).toHaveBeenCalledTimes(1);
    window.history.pushState({}, "", "/");
  });
});

describe("signing out", () => {
  it("empties the cache and hides the page before the sign-in page loads, keeping nothing", async () => {
    stubServer(() => ({ status: 200, body: ME }));
    const user = userEvent.setup();
    const { queryClient, ui } = guarded("/devices", <TenantPage />);
    render(ui);
    expect(await screen.findByText(TENANT_ROW)).toBeInTheDocument();
    await user.type(screen.getByLabelText("Reply to the requester"), TYPED);

    await user.click(screen.getByRole("button", { name: /Ada Lovelace/ }));
    await user.click(screen.getByRole("menuitem", { name: "Sign out" }));

    await waitFor(() => expect(assign).toHaveBeenCalledExactlyOnceWith("/sign-in"));
    expect(await screen.findByText("Signing out…")).toBeInTheDocument();
    expect(screen.queryByText(TENANT_ROW)).not.toBeInTheDocument();
    expect(screen.queryByText(/Ada Lovelace/)).not.toBeInTheDocument();
    expect(cachedData(queryClient)).toEqual([]);
    expect(queryClient.getMutationCache().getAll()).toEqual([]);
    // They chose to leave: nothing is kept, and the guard sends nobody anywhere else.
    expect(everythingShown()).not.toContain(TYPED);
    expect(assign).toHaveBeenCalledTimes(1);
  });

  it("forgets the client chosen in the top bar, for the next person on a shared computer (D1, Minor 9)", async () => {
    stubServer(() => ({ status: 200, body: ME }));
    const user = userEvent.setup();
    render(guarded().ui);
    await screen.findByText("secret");
    window.localStorage.setItem(CLIENT_STORAGE_KEY, "6b1f2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d");

    await user.click(screen.getByRole("button", { name: /Ada Lovelace/ }));
    await user.click(screen.getByRole("menuitem", { name: "Sign out" }));

    await waitFor(() => expect(assign).toHaveBeenCalledWith("/sign-in"));
    expect(window.localStorage.getItem(CLIENT_STORAGE_KEY)).toBeNull();
  });

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
