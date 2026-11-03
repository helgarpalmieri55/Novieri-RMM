/**
 * A device's site, and moving it inside its client (MSP clients phase 3,
 * design §4.3). `SiteField` is the Task 9 picker, replaced here by a plain
 * select: what is pinned is the request this sends and the refusal it shows.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../lib/api";
import type { ApiClient, Device } from "../../lib/api";
import { DeviceSiteDialog } from "./DeviceSite";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
// Task 9's contract: `value` is the site chosen or `""`, and so is `onChange`'s.
vi.mock("../../components/SiteField", () => ({
  SiteField: ({ value, onChange }: { value: string; onChange: (id: string) => void }) => (
    <select aria-label="Site" value={value} onChange={(event) => onChange(event.target.value)}>
      <option value="" />
      <option value="s-main">Principal</option>
      <option value="s-north">Bodega norte</option>
    </select>
  ),
}));

const DEVICE = { id: "d1", hostname: "andes-01", client_id: "c-andes", site_id: "s-main" } as Device;

function renderDialog(changeDeviceSite: ReturnType<typeof vi.fn>, onClose = vi.fn()) {
  mockUseApi.mockReturnValue({ changeDeviceSite } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <DeviceSiteDialog device={DEVICE} onClose={onClose} />
    </QueryClientProvider>,
  );
  return onClose;
}

beforeEach(() => mockUseApi.mockReset());

describe("moving a device to another site of its client", () => {
  it("sends the chosen site and closes", async () => {
    const changeDeviceSite = vi.fn().mockResolvedValue({ ...DEVICE, site_id: "s-north" });
    const onClose = renderDialog(changeDeviceSite);
    const user = userEvent.setup();
    const dialog = await screen.findByRole("dialog", { name: "Move to another site" });
    expect(within(dialog).getByRole("button", { name: "Move" })).toBeDisabled();
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Site" }), "s-north");
    await user.click(within(dialog).getByRole("button", { name: "Move" }));
    await waitFor(() => expect(changeDeviceSite).toHaveBeenCalledWith("d1", "s-north"));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it("says the move did not happen, in the server's words", async () => {
    const changeDeviceSite = vi.fn().mockRejectedValue(
      new ApiError(409, JSON.stringify({ detail: "that site is archived" })),
    );
    renderDialog(changeDeviceSite);
    const user = userEvent.setup();
    const dialog = await screen.findByRole("dialog", { name: "Move to another site" });
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Site" }), "s-north");
    await user.click(within(dialog).getByRole("button", { name: "Move" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "The device was not moved. that site is archived",
    );
  });

  it("offers no move while no other site is chosen", async () => {
    const changeDeviceSite = vi.fn();
    renderDialog(changeDeviceSite);
    const user = userEvent.setup();
    const dialog = await screen.findByRole("dialog", { name: "Move to another site" });
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Site" }), "");
    expect(within(dialog).getByRole("button", { name: "Move" })).toBeDisabled();
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Site" }), "s-main");
    expect(within(dialog).getByRole("button", { name: "Move" })).toBeDisabled();
    expect(changeDeviceSite).not.toHaveBeenCalled();
  });
});
