import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../../lib/api";
import { AlertReadiness } from "../AlertReadiness";
import { HealthSettingsPanel } from "../HealthSettingsPanel";

// "Before alerts can reach anyone" (`docs/design/cross-module-guidance.md`
// §2 Alerts). The row that matters most is the profile: with none assigned
// the fleet is silently unmonitored and the alert list reads as healthy.

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ALL = ["health.read", "devices.read", "notification.read"];

function fleet(managed: number) {
  return {
    items: [],
    total: managed,
    state_counts: { all: 0, online: 0, offline: 0, assets: 0, certs: 0 },
    fleet: { total: managed, managed, online: managed, cert_attention: 0 },
  };
}

const ASSIGNMENT = { id: "a1", profile_id: "p1", scope: "org", device_group_id: null, device_id: null };
const RULE = { id: "r1", event_type: "health.alert_raised", active: true };

function stubApi(overrides: Record<string, unknown> & { permissions?: string[] } = {}) {
  const { permissions = ALL, ...rest } = overrides;
  const api = {
    me: vi.fn().mockResolvedValue({ user_id: "u1", org_id: "o1", email: "a@b.c", display_name: "A", permissions }),
    listDevices: vi.fn().mockResolvedValue(fleet(4)),
    listHealthAssignments: vi.fn().mockResolvedValue([ASSIGNMENT]),
    listNotificationRules: vi.fn().mockResolvedValue([RULE]),
    ...rest,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

function renderPanel(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
  return client;
}

async function settled(client: QueryClient) {
  await waitFor(() => expect(client.isFetching()).toBe(0));
  await waitFor(() => expect(client.getQueryState(["me"])?.status).toBe("success"));
}

async function step(label: string) {
  const item = (await screen.findByText(label)).closest("li");
  if (!item) throw new Error(`no step for ${label}`);
  return item;
}

describe("AlertReadiness", () => {
  it("says an unassigned fleet is unmonitored, and links to the profiles", async () => {
    stubApi({ listHealthAssignments: vi.fn().mockResolvedValue([]) });
    renderPanel(<AlertReadiness />);

    const profile = await step("A health profile is assigned");
    expect(profile).toHaveTextContent(/does not mean a healthy fleet/);
    expect(within(profile).getByRole("link", { name: "Assign a health profile" })).toHaveAttribute(
      "href",
      "/health#profiles",
    );
  });

  it("links to adding a computer when none runs the agent", async () => {
    stubApi({ listDevices: vi.fn().mockResolvedValue(fleet(0)) });
    renderPanel(<AlertReadiness />);

    const agent = await step("A computer runs the agent");
    expect(within(agent).getByRole("link", { name: "Add a computer" })).toHaveAttribute("href", "/devices/onboard");
  });

  it("links to notification rules when nobody is told about alerts", async () => {
    stubApi({ listNotificationRules: vi.fn().mockResolvedValue([{ ...RULE, event_type: "ticket.assigned" }]) });
    renderPanel(<AlertReadiness />);

    const rules = await step("Someone is told about alerts");
    expect(within(rules).getByRole("link", { name: "Notify someone about alerts" })).toHaveAttribute(
      "href",
      "/notifications#rules",
    );
  });

  it("is not drawn once everything is in place", async () => {
    stubApi();
    const client = renderPanel(<AlertReadiness />);
    await settled(client);
    expect(screen.queryByText("Before alerts can reach anyone")).not.toBeInTheDocument();
  });

  it("says the profile cannot be checked without health read, and asks nothing", async () => {
    const api = stubApi({ permissions: ["devices.read", "notification.read"] });
    renderPanel(<AlertReadiness />);

    expect(await step("A health profile is assigned")).toHaveTextContent("This cannot be checked from this account");
    expect(api.listHealthAssignments).not.toHaveBeenCalled();
  });
});

describe("HealthSettingsPanel — the alert-ticket warning is a link now", () => {
  it("links to Settings → Health when alerts cannot open tickets", async () => {
    stubApi({
      getHealthSettings: vi.fn().mockResolvedValue({
        alert_tickets_enabled: false,
        auto_resolve_alert_tickets: false,
        alert_ticket_queue_id: null,
        alert_ticket_category_id: null,
      }),
    });
    renderPanel(<HealthSettingsPanel canAdmin={false} />);

    expect(await screen.findByRole("link", { name: "Choose where alert tickets go" })).toHaveAttribute(
      "href",
      "/settings?tab=health",
    );
  });
});
