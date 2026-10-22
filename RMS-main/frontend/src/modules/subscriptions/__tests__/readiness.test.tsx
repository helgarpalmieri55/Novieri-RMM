import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../../lib/api";
import { SubscriptionReadiness } from "../SubscriptionReadiness";

// "Before subscriptions are fully tracked" (cross-module guidance,
// Subscriptions rows 3 and 4). Row 2's "Add a user" link is asserted in
// subscriptions.test.tsx, beside the owner picker it belongs to.

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ALL = ["subscriptions.read", "people.read", "notification.read"];

const RULE = { id: "r1", event_type: "subscription.renewal_approaching", active: true };
const UNMATCHED_NONE = { seats: [], devices: [], allocations: [] };
const SEAT = {
  seat_id: "s1",
  subscription_id: "sub1",
  service_name: "Figma",
  assignee_name: "Pat",
  assignee_email: "pat@example.com",
};

function stubApi(overrides: Partial<ApiClient> & { permissions?: string[] } = {}): ApiClient {
  const { permissions = ALL, ...rest } = overrides;
  const api = {
    me: vi.fn().mockResolvedValue({ user_id: "u1", org_id: "o1", email: "a@b.c", display_name: "A", permissions }),
    listUnmatched: vi.fn().mockResolvedValue(UNMATCHED_NONE),
    listNotificationRules: vi.fn().mockResolvedValue([RULE]),
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
        <SubscriptionReadiness />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("SubscriptionReadiness", () => {
  it("renders nothing when every seat is matched and a renewal rule is active", async () => {
    const api = stubApi();
    renderPanel();
    await waitFor(() => expect(api.listNotificationRules).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText("Before subscriptions are fully tracked")).toBeNull();
  });

  it("links unmatched seat holders to the matching page", async () => {
    stubApi({ listUnmatched: vi.fn().mockResolvedValue({ ...UNMATCHED_NONE, seats: [SEAT] }) });
    renderPanel();
    expect(await screen.findByText(/1 seat is not matched to a person/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Match seat holders" })).toHaveAttribute("href", "/people/unmatched");
  });

  it("links missing renewal reminders to notification rules", async () => {
    // A rule for another module does not count, and neither does a paused one.
    stubApi({
      listNotificationRules: vi.fn().mockResolvedValue([
        { id: "r2", event_type: "health.alert_raised", active: true },
        { ...RULE, active: false },
      ]),
    });
    renderPanel();
    expect(await screen.findByRole("link", { name: "Get renewal reminders" })).toHaveAttribute(
      "href",
      "/notifications#rules",
    );
  });

  it("says seat matching cannot be checked without people read access, and never asks", async () => {
    const api = stubApi({ permissions: ["subscriptions.read", "notification.read"] });
    renderPanel();
    expect(await screen.findByText(/cannot be checked from this account/)).toBeInTheDocument();
    expect(api.listUnmatched).not.toHaveBeenCalled();
  });
});
