/**
 * The install link's settings (install-link spec 2026-09-28 §1.1, §1.6).
 *
 * What is pinned: the link cannot be switched on with no network; the full
 * link is shown ONCE, from the response that minted it, and is gone when the
 * operator says they have it; regenerating asks first; the panel says how long
 * the link has been on and how many computers joined through it.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../lib/api";
import type { ApiClient, InstallLinkSaved, InstallLinkSettings, MeClient } from "../../lib/api";
import { InstallLinkPanel, fromLocalInput, parseNetworks, toLocalInput } from "./InstallLinkPanel";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const mockClientScope = vi.fn();
vi.mock("../../lib/clientScope", () => ({ useClientScope: () => mockClientScope() }));

/** The internal client every test here works in unless it chooses otherwise. */
const SCOPE_INTERNAL: MeClient = {
  id: "o",
  name: "Novieri",
  kind: "internal",
  enabled_modules: [],
  permissions: [],
};

/** The selector (Task 7): `selected` is the client chosen at the top, `null` for every client. */
function selectClient(selected: string | null, clients: MeClient[] = [SCOPE_INTERNAL]): void {
  mockClientScope.mockReturnValue({ selected, select: vi.fn(), clients, ready: true });
}

beforeEach(() => selectClient(SCOPE_INTERNAL.id));

const OFF: InstallLinkSettings = {
  enabled: false, has_code: false, networks: [], disable_at: null,
  hourly_limit: 50, enabled_at: null, joined_since_enabled: 0,
};
const LINK = "https://itms.example.com/i/AbCdEfGhIjKlMnOpQrStUvWxYz012_-9";
/**
 * The site whose link this panel manages (MSP clients, phase 1: one link per
 * site, at `/sites/{site_id}/install-link`). The page passes the internal
 * client's "Principal" site, whose id is the organisation's.
 */
const SITE = "00000000-0000-4000-8000-0000000000aa";

function renderPanel(api: Partial<ApiClient>) {
  mockUseApi.mockReturnValue(api as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <InstallLinkPanel zone="Europe/Lisbon" siteId={SITE} />
    </QueryClientProvider>,
  );
}

beforeEach(() => mockUseApi.mockReset());

describe("InstallLinkPanel", () => {
  it("will not switch the link on until a network is listed", async () => {
    const saveInstallLink = vi.fn();
    renderPanel({ getInstallLink: vi.fn().mockResolvedValue(OFF), saveInstallLink });
    expect(await screen.findByText(/No link has been generated yet/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("switch", { name: "Accept new computers through the link" }));
    // Save stays enabled and the missing network is said on submit
    // (coordinator clarification 8a: a form of several inputs reports on
    // submit); the attempt sends nothing.
    const save = screen.getByRole("button", { name: "Save install link" });
    expect(save).toBeEnabled();
    await userEvent.click(save);
    expect(screen.getByRole("alert")).toHaveTextContent("List at least one network");
    expect(saveInstallLink).not.toHaveBeenCalled();
  });

  it("switching on sends the networks and shows the new link exactly once", async () => {
    const saved: InstallLinkSaved = {
      ...OFF, enabled: true, has_code: true, networks: ["10.20.0.0/16", "fd00::/8"],
      enabled_at: "2026-09-28T08:00:00Z", url: LINK,
    };
    const saveInstallLink = vi.fn().mockResolvedValue(saved);
    const getInstallLink = vi.fn().mockResolvedValue(OFF);
    renderPanel({ getInstallLink, saveInstallLink });
    await userEvent.type(
      await screen.findByLabelText("Networks the link answers"),
      "10.20.0.0/16{enter}{enter}fd00::/8",
    );
    await userEvent.click(screen.getByRole("switch", { name: "Accept new computers through the link" }));
    await userEvent.click(screen.getByRole("button", { name: "Save install link" }));

    // Read and written on the SITE's link.
    expect(getInstallLink).toHaveBeenCalledWith(SITE);
    expect(saveInstallLink).toHaveBeenCalledWith(SITE, {
      enabled: true, networks: ["10.20.0.0/16", "fd00::/8"], disable_at: null, hourly_limit: 50,
    });
    const reveal = await screen.findByRole("region", { name: "The new install link" });
    expect(within(reveal).getByText(LINK)).toBeInTheDocument();
    expect(reveal).toHaveTextContent("shown once");

    await userEvent.click(within(reveal).getByRole("button", { name: "I have copied it" }));
    expect(screen.queryByText(LINK)).not.toBeInTheDocument();
  });

  it("says how long the link has been on and how many computers joined", async () => {
    renderPanel({
      getInstallLink: vi.fn().mockResolvedValue({
        ...OFF, enabled: true, has_code: true, networks: ["10.20.0.0/16"],
        enabled_at: "2026-09-27T08:00:00Z", joined_since_enabled: 3,
        disable_at: "2026-10-03T18:00:00Z",
      }),
    });
    const status = await screen.findByText(/On since/);
    expect(status).toHaveTextContent("3 computers joined since then");
    expect(status).toHaveTextContent("switches itself off");
    expect(screen.getByText(/shown once, when it was generated/)).toBeInTheDocument();
  });

  it("asks before generating a new link, then shows it once", async () => {
    const regenerateInstallLink = vi.fn().mockResolvedValue({
      ...OFF, enabled: true, has_code: true, networks: ["10.20.0.0/16"],
      enabled_at: "2026-09-27T08:00:00Z", url: LINK,
    });
    renderPanel({
      getInstallLink: vi.fn().mockResolvedValue({
        ...OFF, enabled: true, has_code: true, networks: ["10.20.0.0/16"], enabled_at: "2026-09-27T08:00:00Z",
      }),
      regenerateInstallLink,
    });
    await userEvent.click(await screen.findByRole("button", { name: "Generate a new link…" }));
    const dialog = screen.getByRole("dialog", { name: "Generate a new install link" });
    expect(dialog).toHaveTextContent("stops working at once");
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(regenerateInstallLink).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Generate a new link…" }));
    await userEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Generate new link" }),
    );
    expect(regenerateInstallLink).toHaveBeenCalledOnce();
    expect(regenerateInstallLink).toHaveBeenCalledWith(SITE);
    expect(await screen.findByText(LINK)).toBeInTheDocument();
  });

  it("a refused save says why, in the server's words", async () => {
    renderPanel({
      getInstallLink: vi.fn().mockResolvedValue(OFF),
      saveInstallLink: vi.fn().mockRejectedValue(
        new ApiError(422, JSON.stringify({ detail: "'10.0.0.1/24' is not a network (has host bits set)" })),
      ),
    });
    await userEvent.type(await screen.findByLabelText("Networks the link answers"), "10.0.0.1/24");
    await userEvent.click(screen.getByRole("button", { name: "Save install link" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("is not a network");
  });
});

describe("the form's conversions", () => {
  it("reads one network per line or comma and drops blanks", () => {
    expect(parseNetworks(" 10.0.0.0/8 \n\n fd00::/8, 192.168.1.0/24 ")).toEqual([
      "10.0.0.0/8", "fd00::/8", "192.168.1.0/24",
    ]);
  });

  it("round-trips the switch-off time through the browser's local input", () => {
    const iso = "2026-10-03T18:30:00.000Z";
    expect(fromLocalInput(toLocalInput(iso))).toBe(iso);
    expect(toLocalInput(null)).toBe("");
    expect(fromLocalInput("")).toBeNull();
  });
});

/*
 * THE PANEL IS SAFE ON ITS OWN WHEN ITS SITE CHANGES (coordinator ruling 12,
 * fix round 2 of wave O1; invariant 3 hygiene). A client's Sites tab mounts
 * this panel without a key and switches its site in place, so nothing of one
 * site's panel -- an unsaved draft, a link shown once -- may survive into
 * another's. Re-rendered here with another `siteId` and no key, as that
 * consumer does.
 */
describe("InstallLinkPanel, re-rendered for another site", () => {
  const SITE_B = "00000000-0000-4000-8000-0000000000bb";
  const ON_A: InstallLinkSettings = { ...OFF, enabled: true, has_code: true, networks: ["10.1.0.0/16"], enabled_at: "2026-09-28T08:00:00Z" };
  const ON_B: InstallLinkSettings = { ...OFF, enabled: true, has_code: true, networks: ["10.2.0.0/16"], enabled_at: "2026-09-28T08:00:00Z" };

  function renderFor(siteId: string, api: Partial<ApiClient>) {
    mockUseApi.mockReturnValue(api as ApiClient);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = render(
      <QueryClientProvider client={queryClient}>
        <InstallLinkPanel zone="Europe/Lisbon" siteId={siteId} />
      </QueryClientProvider>,
    );
    const switchTo = (next: string) =>
      view.rerender(
        <QueryClientProvider client={queryClient}>
          <InstallLinkPanel zone="Europe/Lisbon" siteId={next} />
        </QueryClientProvider>,
      );
    return { switchTo };
  }

  it("shows site B's own settings, and Save sends nothing of site A's unsaved edit", async () => {
    const saveInstallLink = vi.fn();
    const getInstallLink = vi.fn((siteId: string) => Promise.resolve(siteId === SITE ? ON_A : ON_B));
    const { switchTo } = renderFor(SITE, { getInstallLink, saveInstallLink });
    const networks = await screen.findByLabelText("Networks the link answers");
    expect(networks).toHaveValue("10.1.0.0/16");
    await userEvent.type(networks, "{enter}192.168.9.0/24");

    switchTo(SITE_B);

    expect(await screen.findByDisplayValue("10.2.0.0/16")).toBeInTheDocument();
    expect(screen.queryByDisplayValue(/192\.168\.9\.0\/24/)).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Save install link" }));
    // B's settings as saved: nothing to send (ruling 13), and above all nothing of A's.
    expect(saveInstallLink).not.toHaveBeenCalled();
  });

  it("does not carry site A's link, shown once, into site B's panel", async () => {
    const saveInstallLink = vi.fn().mockResolvedValue({ ...ON_A, url: LINK });
    const getInstallLink = vi.fn((siteId: string) => Promise.resolve(siteId === SITE ? OFF : ON_B));
    const { switchTo } = renderFor(SITE, { getInstallLink, saveInstallLink });
    await userEvent.type(await screen.findByLabelText("Networks the link answers"), "10.1.0.0/16");
    await userEvent.click(screen.getByRole("switch", { name: "Accept new computers through the link" }));
    await userEvent.click(screen.getByRole("button", { name: "Save install link" }));
    expect(await screen.findByText(LINK)).toBeInTheDocument();

    switchTo(SITE_B);

    expect(await screen.findByDisplayValue("10.2.0.0/16")).toBeInTheDocument();
    expect(screen.queryByText(LINK)).toBeNull();
  });
});
