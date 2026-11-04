import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../../lib/api";
import { DecisionBar } from "../DecisionBar";
import type { MeClient } from "../../../lib/api";
import { ClientNotChosen } from "../../../lib/clientSelection";

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const mockClientScope = vi.fn();
vi.mock("../../../lib/clientScope", () => ({ useClientScope: () => mockClientScope() }));

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

function renderBar() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <DecisionBar
          selectedIds={["p1"]}
          onSelectionChange={() => undefined}
          matchingTotal={1}
          resolveMatching={() => Promise.resolve({ ids: ["p1"], matching: 1, truncated: false })}
          ringOptions={[{ name: "broad", deviceCount: 12, clientCounts: { o1: 12 } }]}
          fleetDeviceTotal={12}
          scopeName="Windows updates"
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The approval is filed under the client chosen at the top: here the internal one, `o1`. */
const me = () =>
  vi.fn().mockResolvedValue({ user_id: "u", org_id: "o1", email: "", display_name: "", permissions: [] });

const APPROVED = {
  id: "c1",
  type: "normal",
  title: "September patching",
  description: "",
  risk: "medium",
  affected: [],
  window_start: null,
  window_end: null,
  rollback_plan: null,
  state: "approved",
  created_by: "u",
  created_at: "2026-09-20T00:00:00Z",
};

describe("linking an approval to a change", () => {
  it("offers only approved changes and sends the one chosen", async () => {
    const user = userEvent.setup();
    const bulkApprovePatches = vi
      .fn()
      .mockResolvedValue({ items: [], total: 1, newly_approved: 1, already_in_force: 0 });
    mockUseApi.mockReturnValue({
      me: me(),
      listChanges: vi.fn().mockResolvedValue([APPROVED, { ...APPROVED, id: "c2", title: "Draft", state: "draft" }]),
      bulkApprovePatches,
    });
    renderBar();
    const picker = await screen.findByRole("combobox", { name: /Change this approval implements/ });
    expect(screen.queryByRole("option", { name: "Draft" })).toBeNull();
    await user.selectOptions(picker, "c1");
    await user.click(screen.getByRole("button", { name: /^Approve 1 update/ }));
    await user.click(await screen.findByRole("button", { name: "Approve" }));
    await waitFor(() => expect(bulkApprovePatches).toHaveBeenCalled());
    expect(bulkApprovePatches.mock.calls[0]?.[0]).toEqual({
      patch_ids: ["p1"],
      ring_name: "broad",
      change_id: "c1",
      client_id: "o1",
    });
  });

  it("shows the server's refusal when a gate needs a change", async () => {
    const user = userEvent.setup();
    const refusal =
      "Approving updates for the broad ring reaches the Servers group, which needs an approved change. Choose an approved change to link, or have one approved first.";
    mockUseApi.mockReturnValue({
      me: me(),
      listChanges: vi.fn().mockRejectedValue(new ApiError(403, "")),
      bulkApprovePatches: vi.fn().mockRejectedValue(new ApiError(409, refusal)),
    });
    renderBar();
    await user.click(await screen.findByRole("button", { name: /^Approve 1 update/ }));
    await user.click(await screen.findByRole("button", { name: "Approve" }));
    expect(await screen.findByText(refusal)).toBeTruthy();
    expect(screen.queryByRole("combobox", { name: /Change this approval implements/ })).toBeNull();
  });

  it("sends nothing before /me has answered, and says why", async () => {
    // Until `/me` has answered the selector is not ready and no client is
    // known (`useNewRowClient`); without one the body would be a global
    // approval nobody chose (MSP clients plan 2026-10-26 Task 14).
    mockClientScope.mockReturnValue({ selected: SCOPE_INTERNAL.id, select: vi.fn(), clients: [], ready: false });
    const user = userEvent.setup();
    const bulkApprovePatches = vi.fn();
    mockUseApi.mockReturnValue({
      me: vi.fn().mockReturnValue(new Promise(() => undefined)),
      listChanges: vi.fn().mockResolvedValue([]),
      bulkApprovePatches,
    });
    renderBar();
    await user.click(await screen.findByRole("button", { name: /^Approve 1 update/ }));
    await user.click(await screen.findByRole("button", { name: "Approve" }));
    // The shared sentence, exactly (phase 3 final review D, Minor 4).
    expect(await screen.findByRole("alert")).toHaveTextContent(new ClientNotChosen().message);
    expect(bulkApprovePatches).not.toHaveBeenCalled();
  });
});
