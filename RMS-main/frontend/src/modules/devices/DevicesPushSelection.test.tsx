/**
 * Starting a push install from Devices (spec-push-install §10.4; invariant
 * 2a(f); gap 2 of the Windows push PR).
 *
 * WHAT THESE TESTS HOLD THE SCREEN TO, and each is a bound on what one mistaken
 * click can do rather than a nicety:
 *
 *   - ONLY MACHINES A HUMAN TICKED, ONE BY ONE. There is no "select all", no
 *     "select all matching", no group, no filter and no subnet: a push names
 *     explicit addresses (2a(f)).
 *   - NEVER MORE THAN THE BATCH CEILING. The 26th tick is refused here, and the
 *     server refuses a 26-address request whatever the console does.
 *   - ONLY FOR SOMEONE WHO MAY RUN A PUSH. The selection column and the action
 *     render for holders of `agent.push.run`; the server checks it (and
 *     `credentials.checkout`) on every request regardless (invariant 6).
 *   - ONLY MACHINES A PUSH COULD BE FOR: no agent yet, an address, and a
 *     person stood behind the row (a sweep row is not selectable).
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient, Device, DevicePage, Me } from "../../lib/api";
import { DevicesPage } from "./DevicesPage";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
vi.mock("../../lib/ws", () => ({ useLiveEvents: () => undefined }));

function machine(n: number, overrides: Partial<Device> = {}): Device {
  return {
    id: `dev-${n}`,
    hostname: `pc${n}`,
    fqdn: `pc${n}.corp.example.com`,
    os_family: "windows",
    os_version: "",
    architecture: "amd64",
    device_group_id: null,
    status: "active",
    agent_status: null,
    last_seen_at: null,
    cert_not_after: null,
    cert_state: null,
    cert_revoked: false,
    lifecycle: "in_service",
    asset_type: "computer",
    source: "manual",
    provenance: "operator",
    description: null,
    primary_ip: `10.20.4.${n}`,
    owner: null,
    location: null,
    ...overrides,
  } as Device;
}

function page(items: Device[]): DevicePage {
  return {
    items,
    total: items.length,
    state_counts: { all: items.length, online: 0, offline: 0, assets: items.length, certs: 0, review: 0 },
    fleet: { total: items.length, managed: 0, online: 0, cert_attention: 0 },
  } as unknown as DevicePage;
}

const PUSH_RUN = "agent.push.run";

function renderPage(items: Device[], permissions: string[] = [PUSH_RUN], extra: Partial<ApiClient> = {}) {
  const api = {
    listDevices: vi.fn().mockResolvedValue(page(items)),
    me: vi.fn().mockResolvedValue({ permissions } as Me),
    listCredentials: vi.fn().mockResolvedValue([
      { id: "c-win", name: "corp-push", type: "windows_domain", has_secret: true },
      { id: "c-ssh", name: "branch-installer", type: "push_install", has_secret: true },
    ]),
    listPushHostKeys: vi.fn().mockResolvedValue([]),
    requestPush: vi.fn(),
    confirmPushHostKey: vi.fn(),
    ...extra,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/devices"]}>
        <Routes>
          <Route path="/devices" element={<DevicesPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return api;
}

const tick = (label: string) => screen.getByRole("checkbox", { name: `Select ${label} for push install` });

beforeEach(() => mockUseApi.mockReset());

describe("selecting machines for a push install", () => {
  it("offers no selection and no push action without permission to run a push", async () => {
    renderPage([machine(1)], ["devices.read"]);
    await screen.findByText("pc1");
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(screen.queryByRole("button", { name: /push install the agent/i })).toBeNull();
  });

  it("offers a tick only on a machine a push could be for", async () => {
    renderPage([
      machine(1),
      machine(2, { agent_status: "online", source: "agent", provenance: "agent" }),
      machine(3, { provenance: "sweep", source: "discovery", hostname: "sweep-3" }),
      machine(4, { primary_ip: null, hostname: "boxed-4" }),
      machine(5, { lifecycle: "unmanageable", hostname: "switch-5" }),
      machine(6, { asset_type: "printer", hostname: "prn-6" }),
    ]);
    await screen.findByText("pc1");
    expect(tick("pc1")).toBeInTheDocument();
    for (const name of ["pc2", "sweep-3", "boxed-4", "switch-5", "prn-6"]) {
      expect(screen.queryByRole("checkbox", { name: `Select ${name} for push install` })).toBeNull();
    }
  });

  it("has no select-all control of any kind (2a(f))", async () => {
    renderPage([machine(1), machine(2)]);
    await screen.findByText("pc1");
    expect(screen.queryByRole("checkbox", { name: /select all|all matching|every/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /select all/i })).toBeNull();
    expect(screen.getAllByRole("checkbox")).toHaveLength(2);
  });

  it("shows the action only once a machine is ticked, naming how many", async () => {
    renderPage([machine(1), machine(2)]);
    await screen.findByText("pc1");
    // The page head's "Push install the agent…" (design 2026-09-30 §1.1) is
    // always offered, because By subnet needs no tick. The TICKED-machines
    // action, which names its count, appears only once a machine is ticked.
    expect(screen.getByRole("button", { name: /^push install the agent…$/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /push install the agent… \(/i })).toBeNull();
    await userEvent.click(tick("pc1"));
    await userEvent.click(tick("pc2"));
    expect(screen.getByRole("button", { name: /push install the agent… \(2\)/i })).toBeInTheDocument();
  });

  it("refuses the 26th tick: never more than the batch ceiling", async () => {
    const many = Array.from({ length: 26 }, (_, i) => machine(i + 1));
    renderPage(many);
    await screen.findByText("pc1");
    for (let n = 1; n <= 25; n += 1) await userEvent.click(tick(`pc${n}`));
    expect(tick("pc26")).toBeDisabled();
    expect(screen.getByText(/25 of at most 25 machines selected/i)).toBeInTheDocument();
    // Unticking one frees a place.
    await userEvent.click(tick("pc1"));
    expect(tick("pc26")).toBeEnabled();
  }, 30_000);
});

describe("the push install flow", () => {
  it("opens the pre-flight for exactly the ticked machines, with a credential of the right kind", async () => {
    const api = renderPage([machine(1), machine(2), machine(3)]);
    await screen.findByText("pc1");
    await userEvent.click(tick("pc1"));
    await userEvent.click(tick("pc3"));
    await userEvent.click(screen.getByRole("button", { name: /push install the agent… \(/i }));

    const flow = await screen.findByRole("region", { name: /push install the agent/i });
    expect(within(flow).getByText("10.20.4.1")).toBeInTheDocument();
    expect(within(flow).getByText("10.20.4.3")).toBeInTheDocument();
    expect(within(flow).queryByText("10.20.4.2")).toBeNull();

    // Windows machines: only a Windows domain account is offered.
    const picker = within(flow).getByLabelText(/credential/i);
    expect(within(picker).getByRole("option", { name: "corp-push" })).toBeInTheDocument();
    expect(within(picker).queryByRole("option", { name: "branch-installer" })).toBeNull();
    await userEvent.selectOptions(picker, "c-win");

    await userEvent.click(await within(flow).findByRole("button", { name: /^probe the targets$/i }));
    await waitFor(() =>
      expect(api.requestPush).toHaveBeenCalledWith({
        mode: "probe",
        credential_id: "c-win",
        targets: ["10.20.4.1", "10.20.4.3"],
      }),
    );
  });

  it("stops a Windows machine with no DNS name before any request, and says how to fix it", async () => {
    const api = renderPage([machine(1, { fqdn: null })]);
    await screen.findByText("pc1");
    await userEvent.click(tick("pc1"));
    await userEvent.click(screen.getByRole("button", { name: /push install the agent… \(/i }));
    const flow = await screen.findByRole("region", { name: /push install the agent/i });
    expect(within(flow).getByText(/No DNS name: Windows push needs the computer's full name/)).toBeInTheDocument();
    expect(within(flow).getByRole("link", { name: /set push install details for pc1/i })).toHaveAttribute(
      "href",
      "/devices/dev-1",
    );
    expect(within(flow).queryByRole("button", { name: /probe the targets/i })).toBeNull();
    expect(api.requestPush).not.toHaveBeenCalled();
  });

  it("says a machine with no OS or processor recorded cannot be pushed yet", async () => {
    renderPage([machine(1, { os_family: "linux", architecture: "" })]);
    await screen.findByText("pc1");
    await userEvent.click(tick("pc1"));
    await userEvent.click(screen.getByRole("button", { name: /push install the agent… \(/i }));
    const flow = await screen.findByRole("region", { name: /push install the agent/i });
    expect(within(flow).getByText(/No processor recorded/)).toBeInTheDocument();
  });

  it("keeps Windows and Linux/macOS machines in separate pushes", async () => {
    renderPage([machine(1), machine(2, { os_family: "linux", fqdn: null })]);
    await screen.findByText("pc1");
    await userEvent.click(tick("pc1"));
    await userEvent.click(tick("pc2"));
    await userEvent.click(screen.getByRole("button", { name: /push install the agent… \(/i }));
    const flow = await screen.findByRole("region", { name: /push install the agent/i });
    expect(within(flow).getByText(/Push Windows machines separately/)).toBeInTheDocument();
    expect(within(flow).queryByRole("button", { name: /probe the targets/i })).toBeNull();
  });
});
