import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
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

// `client_id` as `GET /changes` sends it (phase 2): the picker offers only
// the approval's client's changes (phase 3 final review D2).
const APPROVED = {
  id: "c1",
  client_id: "o1",
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

  // Phase 3 final review C, Important 4: the server accepts only a change of
  // the approval's client -- the internal client's for an approval of every
  // client -- and answers "no such change" for any other, even one on screen.
  describe("offers only the changes of the approval's client", () => {
    const INTERNAL: MeClient = { ...SCOPE_INTERNAL, permissions: ["patch.approve"] };
    const ACME: MeClient = { id: "c-acme", name: "Acme", kind: "customer", enabled_modules: [], permissions: ["patch.approve"] };
    const CHANGES = [
      APPROVED,
      { ...APPROVED, id: "c-acme-1", client_id: "c-acme", title: "Acme patch window" },
    ];

    /** The API, and its `bulkApprovePatches` for a test that sends. */
    function stub(): ReturnType<typeof vi.fn> {
      const bulkApprovePatches = vi
        .fn()
        .mockResolvedValue({ items: [], total: 1, newly_approved: 1, already_in_force: 0 });
      mockUseApi.mockReturnValue({
        me: vi.fn().mockResolvedValue({
          user_id: "u", org_id: "o1", email: "", display_name: "",
          permissions: ["patch.approve"], permissions_everywhere: ["patch.approve"],
        }),
        listChanges: vi.fn().mockResolvedValue(CHANGES),
        bulkApprovePatches,
      });
      return bulkApprovePatches;
    }

    function offered(): (string | null)[] {
      const picker = screen.getByRole("combobox", { name: /Change this approval implements/ });
      return within(picker).getAllByRole("option").map((option) => option.textContent);
    }

    it("the client chosen at the top's", async () => {
      selectClient("c-acme", [INTERNAL, ACME]);
      stub();
      renderBar();
      await screen.findByRole("combobox", { name: /Change this approval implements/ });
      expect(offered()).toEqual(["No change request", "Acme patch window"]);
    });

    it("the internal client's for an approval of every client", async () => {
      selectClient("c-acme", [INTERNAL, ACME]);
      stub();
      const user = userEvent.setup();
      renderBar();
      await user.click(await screen.findByRole("checkbox", { name: /For every client/ }));
      expect(offered()).toEqual(["No change request", "September patching"]);
    });

    it("none until the bar's Client field names the client, then that client's", async () => {
      selectClient(null, [INTERNAL, ACME]);
      stub();
      const user = userEvent.setup();
      renderBar();
      const field = await screen.findByLabelText("Client");
      expect(screen.queryByRole("combobox", { name: /Change this approval implements/ })).toBeNull();
      await user.selectOptions(field, "c-acme");
      expect(offered()).toEqual(["No change request", "Acme patch window"]);
    });

    it("drops a change chosen for another client, so it is never sent", async () => {
      selectClient(null, [INTERNAL, ACME]);
      const bulkApprovePatches = stub();
      const user = userEvent.setup();
      renderBar();
      await user.selectOptions(await screen.findByLabelText("Client"), "o1");
      await user.selectOptions(screen.getByRole("combobox", { name: /Change this approval implements/ }), "c1");
      await user.selectOptions(screen.getByLabelText("Client"), "c-acme");
      expect(screen.getByRole("combobox", { name: /Change this approval implements/ })).toHaveValue("");
      // ...and what is SENT, not only what the picker shows (phase 3 last fix
      // round, item 6): the approval goes to Acme with no `change_id`, though
      // the bar still holds the internal client's change it was given.
      await user.click(screen.getByRole("button", { name: /^Approve 1 update/ }));
      await user.click(await screen.findByRole("button", { name: "Approve" }));
      await waitFor(() => expect(bulkApprovePatches).toHaveBeenCalledTimes(1));
      expect(bulkApprovePatches.mock.calls[0]?.[0]).toEqual({
        patch_ids: ["p1"],
        ring_name: "broad",
        client_id: "c-acme",
      });
      expect(bulkApprovePatches.mock.calls[0]?.[0]).not.toHaveProperty("change_id");
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
    // known (`useNewRowClientField`); without one the body would be a global
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
