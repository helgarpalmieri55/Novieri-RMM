import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../../lib/api";
import { IpamReadiness, siteServerServes } from "../IpamReadiness";
import type { ScopeOut, SubnetOut } from "../types";

// "Before subnets can be swept" (cross-module guidance, IP addresses rows
// 1-4). The per-subnet versions of rows 1 and 2 are on the sweep panel
// (ipam.test.tsx).

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ALL = ["ipam.read", "devices.read", "site.read", "notification.read"];

function subnet(overrides: Partial<SubnetOut> = {}): SubnetOut {
  return {
    client_id: "o1",
    id: "s1",
    vrf_id: "v1",
    prefix: "10.4.7.0/24",
    kind: "assignable",
    name: "Core",
    description: "",
    site_id: null,
    vlan_id: null,
    gateway: null,
    dns_servers: [],
    parent_id: null,
    sweep_enabled: false,
    sweep_device_id: null,
    sweep_interval_hours: null,
    last_swept_at: null,
    ...overrides,
  };
}

function scope(overrides: Partial<ScopeOut> = {}): ScopeOut {
  return {
    client_id: "o1",
    id: "sc1",
    vrf_id: "v1",
    prefix: "10.4.7.0/24",
    note: "",
    authorized_by: "u1",
    authorized_at: "2026-07-01T00:00:00.000Z",
    expires_at: null,
    revoked_at: null,
    ...overrides,
  };
}

function server(segments: string[], enabled = true) {
  return {
    id: "ss1",
    device_id: "d1",
    name: "branch-ss",
    segments,
    roles: [],
    enabled,
    notes: "",
    client_id: "o1",
    site_id: null,
    agent_status: "online",
    agent_last_seen_at: null,
  };
}

function stubApi(overrides: Partial<ApiClient> & { permissions?: string[] } = {}): ApiClient {
  const { permissions = ALL, ...rest } = overrides;
  const api = {
    me: vi.fn().mockResolvedValue({ user_id: "u1", org_id: "o1", email: "a@b.c", display_name: "A", permissions }),
    listIpamSubnets: vi.fn().mockResolvedValue([subnet()]),
    listIpamScanAuthorizations: vi.fn().mockResolvedValue([scope()]),
    listAssetCandidates: vi.fn().mockResolvedValue({ items: [], total: 0, truncated: false, unsweepable_subnets: 0 }),
    listSiteServers: vi.fn().mockResolvedValue([server(["10.4.0.0/16"])]),
    listNotificationRules: vi.fn().mockResolvedValue([{ id: "r1", event_type: "ipam.drift_detected", active: true }]),
    ...rest,
  } as unknown as ApiClient;
  mockUseApi.mockReturnValue(api);
  return api;
}

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <IpamReadiness />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("siteServerServes", () => {
  it("needs one enabled segment CONTAINING the subnet, the rule the sweeper applies", () => {
    expect(siteServerServes(subnet(), [server(["10.4.0.0/16"])])).toBe(true);
    expect(siteServerServes(subnet(), [server(["10.4.0.0/16"], false)])).toBe(false);
    // Two halves that cover it between them are not a segment containing it.
    expect(siteServerServes(subnet(), [server(["10.4.7.0/25", "10.4.7.128/25"])])).toBe(false);
    expect(siteServerServes(subnet(), [server(["192.168.0.0/16"])])).toBe(false);
  });
});

describe("IpamReadiness", () => {
  it("renders nothing when a subnet is authorized, agents sit on it, a site server serves it and drift is routed", async () => {
    const api = stubApi();
    renderPanel();
    await waitFor(() => expect(api.listSiteServers).toHaveBeenCalled());
    await waitFor(() => expect(api.listNotificationRules).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText("Before subnets can be swept")).toBeNull();
  });

  it("renders nothing before any subnet is registered: the page's first-run guidance owns that", async () => {
    const api = stubApi({ listIpamSubnets: vi.fn().mockResolvedValue([]), listIpamScanAuthorizations: vi.fn().mockResolvedValue([]) });
    renderPanel();
    await waitFor(() => expect(api.listIpamSubnets).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText("Before subnets can be swept")).toBeNull();
  });

  it("links an unauthorized estate to scan authorizations, and waits the rows that depend on it", async () => {
    stubApi({ listIpamScanAuthorizations: vi.fn().mockResolvedValue([]) });
    renderPanel();
    expect(await screen.findByRole("link", { name: "Authorize scanning" })).toHaveAttribute(
      "href",
      "/ipam/scan-authorizations",
    );
    expect(screen.getAllByText("Authorize a subnet for scanning first.")).toHaveLength(2);
  });

  it("does not count an expired or revoked authorization", async () => {
    stubApi({
      listIpamScanAuthorizations: vi.fn().mockResolvedValue([
        scope({ id: "a", revoked_at: "2026-08-01T00:00:00.000Z" }),
        scope({ id: "b", expires_at: "2020-01-01T00:00:00.000Z" }),
      ]),
    });
    renderPanel();
    expect(await screen.findByRole("link", { name: "Authorize scanning" })).toBeInTheDocument();
  });

  it("sends a segment with no agent to the agent package", async () => {
    stubApi({
      listAssetCandidates: vi.fn().mockResolvedValue({ items: [], total: 0, truncated: false, unsweepable_subnets: 2 }),
    });
    renderPanel();
    expect(await screen.findByText(/latest sweep of 2 subnets found no enrolled agent/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Install the agent on one machine there" })).toHaveAttribute(
      "href",
      "/agent-packages",
    );
  });

  it("names the subnet no site server serves, and says a site server is preferred, not required", async () => {
    stubApi({ listSiteServers: vi.fn().mockResolvedValue([server(["192.168.0.0/16"])]) });
    renderPanel();
    expect(await screen.findByText(/contains 10\.4\.7\.0\/24/)).toBeInTheDocument();
    expect(screen.getByText(/preferred, not required/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Promote a site server" })).toHaveAttribute("href", "/site-servers");
  });

  it("links unrouted drift to notification rules", async () => {
    stubApi({ listNotificationRules: vi.fn().mockResolvedValue([]) });
    renderPanel();
    expect(await screen.findByRole("link", { name: "Tell someone about drift" })).toHaveAttribute(
      "href",
      "/notifications#rules",
    );
  });

  it("says the agent and site-server checks cannot be made without their permissions, and never asks", async () => {
    const api = stubApi({ permissions: ["ipam.read", "notification.read"] });
    renderPanel();
    expect(await screen.findAllByText(/cannot be checked from this account/)).toHaveLength(2);
    expect(api.listAssetCandidates).not.toHaveBeenCalled();
    expect(api.listSiteServers).not.toHaveBeenCalled();
  });
});
