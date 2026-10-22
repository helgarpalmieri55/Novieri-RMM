/**
 * Remote sessions' cross-module guidance (`docs/design/cross-module-guidance.md`
 * §2 Remote sessions): the page says when no agent is connected, and the
 * Start session dialog says -- before the server's 409 -- when the chosen
 * device's agent is too old to share its screen, with the way to update it.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient, Device } from "../../lib/api";
import { RemoteReadiness } from "./RemoteReadiness";
import { StartSessionDialog } from "./StartSessionDialog";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function fleet(managed: number, online: number) {
  return {
    items: [],
    total: managed,
    state_counts: { all: 0, online: 0, offline: 0, assets: 0, certs: 0 },
    fleet: { total: managed, managed, online, cert_attention: 0 },
  };
}

function device(overrides: Partial<Device> = {}): Device {
  return {
    id: "d1",
    hostname: "workstation-1",
    fqdn: null,
    os_family: "windows",
    os_version: "11",
    architecture: "x64",
    device_group_id: null,
    status: "active",
    agent_status: "online",
    last_seen_at: null,
    cert_not_after: null,
    cert_state: null,
    cert_revoked: false,
    lifecycle: "in_service",
    asset_type: "computer",
    source: "agent",
    provenance: "agent",
    description: null,
    primary_ip: null,
    owner: null,
    location: null,
    ...overrides,
  };
}

function stubApi(overrides: Record<string, unknown> & { permissions?: string[] } = {}) {
  const { permissions = ["devices.read", "remote.control"], ...rest } = overrides;
  const api = {
    me: vi.fn().mockResolvedValue({ user_id: "u1", org_id: "o1", email: "a@b.c", display_name: "A", permissions }),
    listDevices: vi.fn().mockResolvedValue(fleet(3, 1)),
    startRemoteSession: vi.fn(),
    ...rest,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

function renderUi(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
  return client;
}

const LABEL = "A computer with the agent is connected";

describe("RemoteReadiness", () => {
  it("sends a fleet with no agent to add a computer", async () => {
    stubApi({ listDevices: vi.fn().mockResolvedValue(fleet(0, 0)) });
    renderUi(<RemoteReadiness />);
    const item = (await screen.findByText(LABEL)).closest("li") as HTMLElement;
    expect(within(item).getByRole("link", { name: "Add a computer" })).toHaveAttribute("href", "/devices/onboard");
  });

  it("sends agents that are all offline to the offline list", async () => {
    stubApi({ listDevices: vi.fn().mockResolvedValue(fleet(3, 0)) });
    renderUi(<RemoteReadiness />);
    const item = (await screen.findByText(LABEL)).closest("li") as HTMLElement;
    expect(within(item).getByRole("link", { name: "See offline computers" })).toHaveAttribute(
      "href",
      "/devices?state=offline",
    );
  });

  it("is not drawn while an agent is connected", async () => {
    stubApi();
    const client = renderUi(<RemoteReadiness />);
    await waitFor(() => expect(client.isFetching()).toBe(0));
    await waitFor(() => expect(client.getQueryState(["me"])?.status).toBe("success"));
    expect(screen.queryByText("Before a session can start")).not.toBeInTheDocument();
  });

  it("cannot be checked without devices read, and asks nothing", async () => {
    const api = stubApi({ permissions: ["remote.control"] });
    renderUi(<RemoteReadiness />);
    expect((await screen.findByText(LABEL)).closest("li")).toHaveTextContent(
      "This cannot be checked from this account",
    );
    expect(api.listDevices).not.toHaveBeenCalled();
  });
});

describe("StartSessionDialog — an agent too old to share its screen", () => {
  it("says so beside the device and links to updating the agent", async () => {
    stubApi({
      listDevices: vi.fn().mockResolvedValue({ items: [device({ screen_capable: false })], total: 1 }),
    });
    const user = userEvent.setup();
    renderUi(<StartSessionDialog open onClose={() => {}} />);

    await user.click(await screen.findByRole("combobox", { name: "Device" }));
    const option = await screen.findByRole("option", { name: /workstation-1/ });
    expect(option).toHaveTextContent("agent too old to share its screen");
    await user.click(option);

    expect(await screen.findByText(/runs an agent too old to share its screen/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Update the agent" })).toHaveAttribute(
      "href",
      "/agent-packages#fleet-versions",
    );
  });

  it("says nothing of the kind for a capable agent, or a server that does not report it", async () => {
    stubApi({
      listDevices: vi.fn().mockResolvedValue({
        items: [device({ screen_capable: true }), device({ id: "d2", hostname: "workstation-2" })],
        total: 2,
      }),
    });
    const user = userEvent.setup();
    renderUi(<StartSessionDialog open onClose={() => {}} />);

    await user.click(await screen.findByRole("combobox", { name: "Device" }));
    await user.click(await screen.findByRole("option", { name: /workstation-1/ }));
    expect(screen.queryByText(/too old/)).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Update the agent" })).not.toBeInTheDocument();
  });

  it("links to adding a computer when there is none to pick", async () => {
    stubApi({ listDevices: vi.fn().mockResolvedValue({ items: [], total: 0 }) });
    renderUi(<StartSessionDialog open onClose={() => {}} />);
    expect(await screen.findByRole("link", { name: "Add a computer" })).toHaveAttribute("href", "/devices/onboard");
  });
});
