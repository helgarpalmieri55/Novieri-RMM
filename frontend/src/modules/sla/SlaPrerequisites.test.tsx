import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient, MeClient } from "../../lib/api";
import { ApiError } from "../../lib/api";
import { expectCta, expectNoPanel, meWith, renderPanel } from "../../test/prerequisiteHarness";
import { SlaPrerequisites } from "./SlaPrerequisites";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const mockClientScope = vi.fn();
vi.mock("../../lib/clientScope", () => ({ useClientScope: () => mockClientScope() }));

/** The internal client every test here works in unless it chooses otherwise. */
const SCOPE_INTERNAL: MeClient = {
  id: "o1",
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

const TITLE = "Before a target can match a queue";

function stub(overrides: Record<string, unknown> = {}, permissions = ["sla.read", "ticket.read"]) {
  const api = {
    me: meWith(permissions),
    listTicketQueues: vi.fn().mockResolvedValue([{ id: "q1" }]),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

describe("SlaPrerequisites", () => {
  it("sends a desk with no queue to the service desk settings", async () => {
    stub({ listTicketQueues: vi.fn().mockResolvedValue([]) });
    renderPanel(<SlaPrerequisites />);
    await expectCta("Set up queues", "/service-desk/admin");
  });

  it("is absent when a queue exists", async () => {
    const api = stub();
    renderPanel(<SlaPrerequisites />);
    await expectNoPanel(TITLE, () => expect(api.listTicketQueues).toHaveBeenCalled());
  });

  it("says it cannot be checked from an account without the ticket permission", async () => {
    stub({ listTicketQueues: vi.fn().mockRejectedValue(new ApiError(403, "{}")) }, ["sla.read"]);
    renderPanel(<SlaPrerequisites />);
    expect(await screen.findByText(/cannot be checked from this account/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Set up queues" })).toBeNull();
  });
});
