/**
 * A client's contact in the console (own-auth phase 2, Task 10, decisions
 * 1-2, 6-7): the console's door answers 403 `portal_only`, and the guard draws
 * the portal's own frame on a portal page, sends them to the portal from any
 * other, and ends their session like anybody's. That 403 is asked once, and
 * the portal forgets a client the console stored on this computer.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { selectedClient, setSelectedClient } from "./lib/clientChoice";
import { navigation } from "./lib/session";
import { sessionEnded } from "./lib/sessionEnd";
import { usePortalTickets } from "./modules/portal/api";
import { MyTicketsPage } from "./modules/portal/MyTicketsPage";

const { ApiProvider } = await import("./lib/ApiContext");
const { RequireAuth } = await import("./App");

/**
 * The portal page's stand-in: it asks `/portal/tickets`, a request that is
 * NOT global and so carries `X-Client-Id` when a client is chosen -- the one
 * the stale choice broke (decision 7). `/portal/me` is global and never
 * carries it, so it proves nothing about the fix (f2-3 M2).
 */
function Tickets() {
  usePortalTickets();
  return <p>my tickets</p>;
}

const PORTAL_ONLY = { status: 403, body: { detail: "this account can only use the portal", code: "portal_only" } };
const PORTAL_ME = {
  user_id: "contact-1",
  kind: "contact",
  email: "ana@acme.test",
  display_name: "Ana Ruiz",
  timezone: null,
  permissions: ["portal.ticket.read", "portal.ticket.write"],
};

function stubServer(
  me: { status: number; body: unknown },
  portalMe: () => { status: number; body: unknown } = () => ({ status: 200, body: PORTAL_ME }),
): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
    const url = String(input);
    const answer = url.endsWith("/api/v1/me")
      ? me
      : url.endsWith("/api/v1/portal/me")
        ? portalMe()
        : url.endsWith("/api/v1/portal/tickets")
          ? { status: 200, body: [] }
          : url.endsWith("/api/v1/auth/logout")
            ? { status: 204, body: null }
            : { status: 404, body: {} };
    return new Response(answer.body === null ? null : JSON.stringify(answer.body), { status: answer.status });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function calls(fetchMock: ReturnType<typeof vi.fn>, path: string): RequestInit[] {
  return fetchMock.mock.calls
    .filter(([input]) => String(input).endsWith(path))
    .map(([, init]) => (init ?? {}) as RequestInit);
}

/** `retry`: the console's own default (`App.tsx`), for decision 6. */
function app(path: string, retry: number | false = false): QueryClient {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry } } });
  render(
    <QueryClientProvider client={queryClient}>
      <ApiProvider>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/portal" element={<RequireAuth><Tickets /></RequireAuth>} />
            <Route path="/devices" element={<RequireAuth><p>the fleet</p></RequireAuth>} />
          </Routes>
        </MemoryRouter>
      </ApiProvider>
    </QueryClientProvider>,
  );
  return queryClient;
}

let assign: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  assign = vi.spyOn(navigation, "assign").mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  setSelectedClient(null);
  window.localStorage.clear();
});

describe("a client's contact", () => {
  it("gets the portal's own frame on a portal page, with their name", async () => {
    stubServer(PORTAL_ONLY);
    app("/portal");
    expect(await screen.findByText("my tickets")).toBeInTheDocument();
    expect(await screen.findByText("Ana Ruiz")).toBeInTheDocument();
    expect(screen.getByTestId("portal-shell")).toBeInTheDocument();
  });

  it("is taken to the portal from a console page, which never draws", async () => {
    stubServer(PORTAL_ONLY);
    app("/devices");
    expect(await screen.findByText("my tickets")).toBeInTheDocument();
    expect(screen.queryByText("the fleet")).not.toBeInTheDocument();
  });

  it("whose session ends is sent to sign in again on the portal's page", async () => {
    stubServer(PORTAL_ONLY);
    const user = userEvent.setup();
    app("/portal");
    await screen.findByText("Ana Ruiz");
    act(() => sessionEnded());
    expect(await screen.findByRole("heading", { name: "Your session has ended" })).toBeInTheDocument();
    expect(screen.queryByText("my tickets")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /sign in again/i }));
    expect(assign).toHaveBeenCalledExactlyOnceWith("/portal/sign-in?next=%2Fportal");
  });

  it("is asked the console's 403 once, not twice (decision 6, A2-65)", async () => {
    const fetchMock = stubServer(PORTAL_ONLY);
    app("/portal", 1);
    await screen.findByText("Ana Ruiz");
    expect(calls(fetchMock, "/api/v1/me")).toHaveLength(1);
  });

  it("forgets a client the console stored on this computer (decision 7, A2-66)", async () => {
    setSelectedClient("0b7d6a43-6f43-4f5e-9d39-2f8c1c3f9a10");
    const fetchMock = stubServer(PORTAL_ONLY);
    app("/portal");
    await screen.findByText("Ana Ruiz");
    expect(selectedClient()).toBeNull();
    await waitFor(() => expect(calls(fetchMock, "/api/v1/portal/tickets").length).toBeGreaterThan(0));
    for (const init of calls(fetchMock, "/api/v1/portal/tickets")) {
      expect(new Headers(init.headers).has("X-Client-Id")).toBe(false);
    }
  });
});

describe("a technician on a portal page", () => {
  it("keeps the console's frame", async () => {
    stubServer({
      status: 200,
      body: { user_id: "u1", org_id: "o1", email: "t@x.test", display_name: "Tess", roles: [], permissions: ["portal.ticket.read"] },
    });
    app("/portal");
    expect(await screen.findByText("my tickets")).toBeInTheDocument();
    expect(screen.queryByTestId("portal-shell")).not.toBeInTheDocument();
  });
});

describe("a contact on the real /portal page (fix round 1)", () => {
  function appReal(path: string): void {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: 1 } } });
    render(
      <QueryClientProvider client={queryClient}>
        <ApiProvider>
          <MemoryRouter initialEntries={[path]}>
            <Routes>
              <Route path="/portal" element={<RequireAuth><MyTicketsPage /></RequireAuth>} />
            </Routes>
          </MemoryRouter>
        </ApiProvider>
      </QueryClientProvider>,
    );
  }

  it("asks the console's /me once and keeps the portal frame drawn", async () => {
    const fetchMock = stubServer(PORTAL_ONLY);
    appReal("/portal");
    await screen.findByText("Ana Ruiz");
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 600));
    });
    expect(calls(fetchMock, "/api/v1/me")).toHaveLength(1);
    expect(screen.getByTestId("portal-shell")).toBeInTheDocument();
  });

  it("signs out to the portal's sign-in page", async () => {
    stubServer(PORTAL_ONLY);
    const user = userEvent.setup();
    app("/portal");
    await screen.findByText("Ana Ruiz");
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/portal/sign-in"));
  });

  it("says so, with a retry, when /portal/me fails", async () => {
    let fail = true;
    stubServer(PORTAL_ONLY, () => (fail ? { status: 500, body: { detail: "boom" } } : { status: 200, body: PORTAL_ME }));
    const user = userEvent.setup();
    app("/portal");
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    fail = false;
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Ana Ruiz")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("has one navigation landmark named Portal, the page's own", async () => {
    stubServer(PORTAL_ONLY);
    appReal("/portal");
    await screen.findByText("Ana Ruiz");
    await waitFor(() => expect(screen.getAllByRole("navigation", { name: "Portal" })).toHaveLength(1));
  });
});
