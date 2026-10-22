import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { GatesPanel, gateCoverage } from "../GatesPanel";

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function renderPanel(canManage: boolean) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <GatesPanel canManage={canManage} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const GATE = {
  id: "g1",
  device_group_id: "grp",
  device_group_name: "Servers",
  applies_to_patches: true,
  applies_to_deployments: false,
  created_at: "2026-09-23T00:00:00Z",
};

describe("change gates", () => {
  it("names what each gate holds back", () => {
    expect(gateCoverage({ applies_to_patches: true, applies_to_deployments: true })).toBe(
      "Patch approvals and deployments",
    );
    expect(gateCoverage({ applies_to_patches: false, applies_to_deployments: true })).toBe(
      "Deployments",
    );
  });

  it("lists gates read-only for someone who cannot manage them", async () => {
    mockUseApi.mockReturnValue({ listChangeGates: vi.fn().mockResolvedValue([GATE]) });
    renderPanel(false);
    expect(await screen.findByText("Servers")).toBeTruthy();
    expect(screen.getByText("Patch approvals")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Require a change" })).toBeNull();
  });

  it("adds a gate for every device", async () => {
    const user = userEvent.setup();
    const setChangeGate = vi.fn().mockResolvedValue(GATE);
    mockUseApi.mockReturnValue({
      listChangeGates: vi.fn().mockResolvedValue([]),
      listDeviceGroups: vi.fn().mockResolvedValue({ items: [{ id: "grp", name: "Servers" }], total: 1 }),
      setChangeGate,
    });
    renderPanel(true);
    expect(await screen.findByText("Nothing requires an approved change yet.")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Require a change" }));
    await waitFor(() => expect(setChangeGate).toHaveBeenCalled());
    expect(setChangeGate.mock.calls[0]?.[0]).toEqual({
      device_group_id: null,
      applies_to_patches: true,
      applies_to_deployments: true,
    });
  });
});
