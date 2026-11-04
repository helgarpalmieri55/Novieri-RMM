import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient, ClientSiteOut, MeClient } from "../../lib/api";
import { EnrollmentPage } from "./EnrollmentPage";

// MSP clients phase 3, design §3.2: an enrollment token is one site's, chosen
// in the form among the sites of the client chosen at the top, or of the
// form's Client field with every client.

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const mockClientScope = vi.fn();
vi.mock("../../lib/clientScope", () => ({ useClientScope: () => mockClientScope() }));

const listEnrollmentTokens = vi.fn();
const createEnrollmentToken = vi.fn();
vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api")>()),
  listEnrollmentTokens: (...a: unknown[]) => listEnrollmentTokens(...a),
  createEnrollmentToken: (...a: unknown[]) => createEnrollmentToken(...a),
}));

const CODES = ["enrollment.manage", "devices.read"];
const INTERNAL: MeClient = { id: "o", name: "Novieri", kind: "internal", enabled_modules: [], permissions: CODES };
const ACME: MeClient = { id: "c-acme", name: "Acme", kind: "customer", enabled_modules: [], permissions: CODES };

function selectClient(selected: string | null): void {
  mockClientScope.mockReturnValue({ selected, select: vi.fn(), clients: [INTERNAL, ACME], ready: true });
}

function site(id: string, clientId: string, name: string): ClientSiteOut {
  return { id, client_id: clientId, name, address: "", description: "", egress_networks: [], archived_at: null, device_count: 0 };
}

/**
 * `behind` puts history entries before the page, so it arrives on an entry
 * with a key of its own (the first entry's is always "default", and
 * `useAdoptJourneyClient` remembers the entries it has decided on).
 * `remount` starts the page again on the same URL, as a change of client does
 * (`lib/clientScope.tsx`).
 */
function renderPage(
  path = "/enrollment",
  behind: string[] = [],
  acmeSites: ClientSiteOut[] = [site("site-a1", "c-acme", "Acme HQ"), site("site-a2", "c-acme", "Acme Lab")],
) {
  listEnrollmentTokens.mockResolvedValue({ items: [], total: 0, revoked_count: 0, unlimited_count: 0 });
  createEnrollmentToken.mockResolvedValue({
    id: "t1", name: "lab", token: "plain", expires_at: "2099-01-01T00:00:00Z", max_uses: 1,
  });
  mockUseApi.mockReturnValue({
    me: () =>
      Promise.resolve({ user_id: "u", org_id: "o", email: "a@b.c", display_name: "A", permissions: CODES, timezone: null, clients: [INTERNAL, ACME] }),
    listDevices: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    listClientSites: vi.fn((clientId: string) =>
      Promise.resolve(
        clientId === "c-acme" ? acmeSites : [site("site-o", "o", "Principal")],
      ),
    ),
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui = (key: string) => (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[...behind, path]} initialIndex={behind.length}>
        <EnrollmentPage key={key} />
      </MemoryRouter>
    </QueryClientProvider>
  );
  const view = render(ui("first"));
  return { ...view, remount: (key: string) => view.rerender(ui(key)) };
}

beforeEach(() => {
  listEnrollmentTokens.mockReset();
  createEnrollmentToken.mockReset();
  selectClient("o");
});

describe("an enrollment token's site", () => {
  it("with every client chosen, asks for the client and the site, and sends that site", async () => {
    selectClient(null);
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Create token" }));
    await user.selectOptions(await screen.findByLabelText("Client"), "c-acme");
    const picker = await screen.findByLabelText("Site");
    await within(picker).findByRole("option", { name: "Acme Lab" });
    expect(within(picker).queryByRole("option", { name: "Principal" })).toBeNull();
    await user.selectOptions(picker, "site-a2");
    await user.type(screen.getByLabelText("Token name"), "lab");
    await user.click(screen.getByRole("button", { name: "Issue token…" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Issue token" }));
    await waitFor(() =>
      expect(createEnrollmentToken).toHaveBeenCalledWith(expect.anything(), {
        name: "lab", expires_in_hours: 72, max_uses: 1, site_id: "site-a2",
      }),
    );
  });

  it("uses the only site of the client chosen at the top without asking for a client", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Create token" }));
    expect(screen.queryByLabelText("Client")).toBeNull();
    await waitFor(() => expect((screen.getByLabelText("Site") as HTMLSelectElement).value).toBe("site-o"));
  });

  it("uses the only site of a client chosen at the top that is not the organisation's own", async () => {
    // `org_id` is "o": the phase 1-2 rule took the organisation's own client
    // whoever was chosen, so only a client other than "o" tells the two rules
    // apart (phase 3 final review D, Important 1).
    selectClient("c-acme");
    const user = userEvent.setup();
    renderPage("/enrollment", [], [site("site-a1", "c-acme", "Acme HQ")]);
    await user.click(await screen.findByRole("button", { name: "Create token" }));
    expect(screen.queryByLabelText("Client")).toBeNull();
    await waitFor(() => expect((screen.getByLabelText("Site") as HTMLSelectElement).value).toBe("site-a1"));
    expect(within(screen.getByLabelText("Site")).queryByRole("option", { name: "Principal" })).toBeNull();
    await user.type(screen.getByLabelText("Token name"), "lab");
    await user.click(screen.getByRole("button", { name: "Issue token…" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Issue token" }));
    await waitFor(() =>
      expect(createEnrollmentToken).toHaveBeenCalledWith(expect.anything(), {
        name: "lab", expires_in_hours: 72, max_uses: 1, site_id: "site-a1",
      }),
    );
  });

  it("refuses to open the confirmation without a site, and names the field", async () => {
    selectClient(null);
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Create token" }));
    await screen.findByLabelText("Client");
    await user.type(screen.getByLabelText("Token name"), "lab");
    await user.click(screen.getByRole("button", { name: "Issue token…" }));
    expect(await screen.findByText(/Site is required/)).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(createEnrollmentToken).not.toHaveBeenCalled();
  });

  it("starts on the journey's site when opened from the add-a-computer journey (design §4.4)", async () => {
    selectClient("c-acme");
    const user = userEvent.setup();
    renderPage("/enrollment?client=c-acme&site=site-a2");
    await user.click(await screen.findByRole("button", { name: "Create token" }));
    await waitFor(() => expect((screen.getByLabelText("Site") as HTMLSelectElement).value).toBe("site-a2"));
    await user.type(screen.getByLabelText("Token name"), "lab");
    await user.click(screen.getByRole("button", { name: "Issue token…" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Issue token" }));
    await waitFor(() =>
      expect(createEnrollmentToken).toHaveBeenCalledWith(expect.anything(), {
        name: "lab", expires_in_hours: 72, max_uses: 1, site_id: "site-a2",
      }),
    );
  });
});

describe("a tab clicked on a page opened from the journey (design §4.4)", () => {
  // A tab is pushed into the URL (`?tab=`) with the journey's `client=` kept:
  // the same arrival, so the client the person chose since is not undone.
  const select = vi.fn();
  function scopeOn(selected: string | null): void {
    mockClientScope.mockReturnValue({ selected, select, clients: [INTERNAL, ACME], ready: true });
  }
  beforeEach(() => select.mockReset());

  it("keeps «Todos mis clientes» chosen after the journey's client was adopted", async () => {
    scopeOn(null);
    const user = userEvent.setup();
    const view = renderPage("/enrollment?client=c-acme&site=site-a2", ["/devices/onboard"]);
    expect(select).toHaveBeenCalledWith("c-acme");
    // The console works on Acme: the page starts again.
    scopeOn("c-acme");
    view.remount("on-acme");
    // The person picks «Todos mis clientes» above: it starts again, and stays.
    scopeOn(null);
    view.remount("every-client");
    await user.click(await screen.findByRole("tab", { name: "Certificates" }));
    expect(screen.getByRole("tab", { name: "Certificates" })).toHaveAttribute("aria-selected", "true");
    await user.click(screen.getByRole("tab", { name: "Enrollment flow" }));
    expect(screen.getByRole("tab", { name: "Enrollment flow" })).toHaveAttribute("aria-selected", "true");
    expect(select).toHaveBeenCalledTimes(1);
  });

  it("keeps «Todos mis clientes» chosen after arriving on the journey's client", async () => {
    scopeOn("c-acme");
    const user = userEvent.setup();
    const view = renderPage("/enrollment?client=c-acme&site=site-a2", ["/devices/onboard"]);
    scopeOn(null);
    view.remount("every-client");
    await user.click(await screen.findByRole("tab", { name: "Certificates" }));
    expect(screen.getByRole("tab", { name: "Certificates" })).toHaveAttribute("aria-selected", "true");
    expect(select).not.toHaveBeenCalled();
  });
});
