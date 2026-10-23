/**
 * The real `Shell`: what the top-bar search finds, and what a requester sees.
 *
 * Both depend on `/me`'s permissions reaching the right place -- the record
 * lookups, the placeholder, the rail -- which only the assembled shell shows.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("./lib/auth", () => ({
  safeReturnTo: () => "/",
  signIn: () => Promise.resolve(),
  completeSignIn: () => Promise.resolve(null),
  isAuthenticated: () => true,
  initAuth: () => Promise.resolve(null),
  signOut: () => Promise.resolve(),
  accessToken: () => null,
  oidcAuthority: "https://itms.example/realms/it-suite",
  isSessionExpiring: () => false,
  onSessionExpiring: () => () => {},
  startAccountAction: () => Promise.resolve(),
  userManager: { events: { addUserLoaded: () => {}, addUserUnloaded: () => {} } },
}));

const apiRef = vi.hoisted(() => ({ current: undefined as unknown }));
vi.mock("./lib/ApiContext", () => ({
  useApi: () => apiRef.current,
  ApiProvider: ({ children }: { children: React.ReactNode }) => children,
}));

const { Landing, Shell, homeFor } = await import("./App");

const user = userEvent.setup();

function fakeApi(permissions: string[]) {
  return {
    me: vi.fn().mockResolvedValue({
      user_id: "u1",
      display_name: "Ada",
      email: "ada@example.test",
      org_id: "org-1",
      roles: [],
      permissions,
    }),
    getCurrentOrg: vi.fn().mockResolvedValue({ id: "org-1", name: "Toeshee" }),
    getHealthAlertSummary: vi.fn().mockResolvedValue({
      open: 0,
      by_state: { active: 0, acknowledged: 0, muted: 0 },
      by_severity: { info: 0, warning: 0, critical: 0 },
      worst_severity: null,
    }),
    getPlatformStatus: vi
      .fn()
      .mockResolvedValue({ overall: "ok", probes: [], processes: [], modules: [] }),
    // Both lists behave like their servers: `q` narrows, and the ticket `q`
    // looks at the subject only, never the number.
    listDevices: vi.fn().mockImplementation(({ q }: { q?: string }) => {
      const all = [
        { id: "dev-9", hostname: "por-ws-008", primary_ip: "10.0.0.8", os_version: "Windows 11" },
      ];
      const items = all.filter((d) => !q || d.hostname.includes(q));
      return Promise.resolve({ items, total: items.length });
    }),
    listTickets: vi.fn().mockImplementation(({ q }: { q?: string }) => {
      const all = [
        { id: "t-1", number: "INC-7", subject: "Printer jammed" },
        { id: "t-2", number: "INC-42", subject: "VPN drops" },
      ];
      const items = all.filter((t) => !q || t.subject.toLowerCase().includes(q.toLowerCase()));
      return Promise.resolve({ items, total: items.length });
    }),
    listPortalTickets: vi.fn().mockResolvedValue([
      { id: "p-1", number: "INC-3", subject: "Laptop will not boot" },
      { id: "p-2", number: "REQ-4", subject: "New monitor" },
    ]),
  };
}

function Where() {
  const location = useLocation();
  return <p data-testid="where">{location.pathname}</p>;
}

function renderShell(permissions: string[], path = "/devices") {
  const api = fakeApi(permissions);
  apiRef.current = api;
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route
            path="*"
            element={
              <Shell>
                <Where />
              </Shell>
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { api, client };
}

afterEach(() => cleanup());

const OPERATOR = ["devices.read", "ticket.read", "ai.use"];
const REQUESTER = ["portal.ticket.read", "portal.ticket.write"];

describe("the top-bar search finds records", () => {
  it("finds a device by name and opens its page", async () => {
    const { api } = renderShell(OPERATOR);
    await waitFor(() => expect(api.me).toHaveBeenCalled());
    const field = await screen.findByRole("combobox", { name: /find or ask/i });
    await waitFor(() => expect(field).toHaveAttribute("placeholder", expect.stringContaining("device")));

    await user.click(field);
    await user.keyboard("por-ws");

    const option = await screen.findByRole("option", { name: /por-ws-008/ });
    expect(api.listDevices).toHaveBeenCalledWith({ q: "por-ws", limit: 5 });
    await user.click(option);
    expect(screen.getByTestId("where")).toHaveTextContent("/devices/dev-9");
  });

  it("finds a ticket by number as well as by subject", async () => {
    const { api } = renderShell(OPERATOR);
    await waitFor(() => expect(api.me).toHaveBeenCalled());
    await user.click(await screen.findByRole("combobox", { name: /find or ask/i }));
    await user.keyboard("INC-42");

    expect(await screen.findByRole("option", { name: /INC-42 · VPN drops/ })).toBeInTheDocument();
    // The subject search could not have found it; the number match did.
    expect(screen.queryByRole("option", { name: /INC-7/ })).toBeNull();
  });

  it("waits for typing to settle before asking the server", async () => {
    const { api } = renderShell(OPERATOR);
    await waitFor(() => expect(api.me).toHaveBeenCalled());
    await user.click(await screen.findByRole("combobox", { name: /find or ask/i }));
    await user.keyboard("por-ws");
    await screen.findByRole("option", { name: /por-ws-008/ });
    // One lookup for the settled query, none for "po", "por", "por-" ...
    expect(api.listDevices).toHaveBeenCalledTimes(1);
  });

  it("queries only what the account can read", async () => {
    const { api } = renderShell(["devices.read"]);
    await waitFor(() => expect(api.me).toHaveBeenCalled());
    const field = await screen.findByRole("combobox", { name: /find or ask/i });
    await waitFor(() => expect(field).toHaveAttribute("placeholder", "Find a device, address or page"));
    await user.click(field);
    await user.keyboard("INC-42");
    await screen.findByText("No records match that");
    expect(api.listTickets).not.toHaveBeenCalled();
    expect(api.listPortalTickets).not.toHaveBeenCalled();
  });

  it("still finds pages, and still offers to ask", async () => {
    const { api } = renderShell(OPERATOR);
    await waitFor(() => expect(api.me).toHaveBeenCalled());
    await user.click(await screen.findByRole("combobox", { name: /find or ask/i }));
    // A page inside the Admin area, which the rail shows only once opened.
    await user.keyboard("enrollment");
    expect(await screen.findByRole("option", { name: /Enrollment/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Ask the Assistant/ })).toBeInTheDocument();
  });
});

describe("a requester's shell", () => {
  it("offers the portal's destinations and no operator rail", async () => {
    renderShell(REQUESTER, "/portal");
    const nav = await screen.findByRole("navigation", { name: "Main" });
    await waitFor(() => expect(within(nav).getByRole("link", { name: "My tickets" })).toBeInTheDocument());
    expect(within(nav).getByRole("link", { name: "New ticket" })).toBeInTheDocument();
    expect(within(nav).queryByRole("link", { name: "Dashboard" })).toBeNull();
    expect(within(nav).queryByRole("link", { name: /End-user Portal/ })).toBeNull();
  });

  it("promises only their own tickets in the search field, and finds them", async () => {
    const { api } = renderShell(REQUESTER, "/portal");
    const field = await screen.findByRole("combobox", { name: /find or ask/i });
    await waitFor(() => expect(field).toHaveAttribute("placeholder", "Find one of your tickets"));
    await user.click(field);
    await user.keyboard("monitor");
    await user.click(await screen.findByRole("option", { name: /REQ-4 · New monitor/ }));
    expect(screen.getByTestId("where")).toHaveTextContent("/portal/tickets/p-2");
    expect(api.listDevices).not.toHaveBeenCalled();
    expect(api.listTickets).not.toHaveBeenCalled();
  });

  it("is sent from an operator page to the portal", async () => {
    renderShell(REQUESTER, "/dashboard");
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/portal"));
    expect(screen.queryByText(/holds no module read permissions/)).toBeNull();
  });

  it("names the portal page in the crumb, with no operator section", async () => {
    renderShell(REQUESTER, "/portal/new");
    await waitFor(() => expect(document.title).toBe("New ticket · ITMS"));
    const crumb = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(within(crumb).queryByRole("link", { name: "Service" })).toBeNull();
  });
});

describe("where the root lands", () => {
  it("picks the account's home", () => {
    expect(homeFor(REQUESTER)).toBe("/portal");
    expect(homeFor(OPERATOR)).toBe("/devices");
    expect(homeFor(["ticket.read"])).toBe("/dashboard");
  });

  it("sends a requester to the portal, not to the fleet list", async () => {
    apiRef.current = fakeApi(REQUESTER);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/"]}>
          <Routes>
            <Route path="/" element={<Landing />} />
            <Route path="*" element={<Where />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(await screen.findByTestId("where")).toHaveTextContent("/portal");
  });
});
