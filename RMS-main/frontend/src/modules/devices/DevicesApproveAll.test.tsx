/**
 * Approve all (GPO MSI spec 2026-09-30 §2.5).
 *
 * What is pinned: the button is on Waiting for review, for a holder of
 * `devices.review`, and nowhere else; the dialog names the waiting machines
 * (paged) and leaves out a rejected row; it sends exactly the ids it showed
 * with their digest -- the server's digest, byte for byte; the result
 * sentence says how many were approved and how many still wait.
 */

import { QueryClient, QueryClientProvider, focusManager, onlineManager } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient, Device, DeviceListParams, DevicePage, Me } from "../../lib/api";
import { approveAllMessage, pendingForReview, reviewListDigest } from "./ApproveAllDialog";
import { DevicesPage } from "./DevicesPage";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
vi.mock("../../lib/ws", () => ({ useLiveEvents: () => undefined }));

function device(i: number, overrides: Partial<Device> = {}): Device {
  return {
    id: `00000000-0000-0000-0000-${String(i).padStart(12, "0")}`, hostname: `DESKTOP-${i}`, fqdn: null,
    os_family: "windows", os_version: "11 24H2", architecture: "amd64", device_group_id: null, status: "active",
    agent_status: "online", last_seen_at: "2026-09-30T09:00:00Z", cert_not_after: null, cert_state: "ok",
    cert_revoked: false, lifecycle: "in_service", asset_type: "computer", source: "agent", provenance: "agent",
    description: null, primary_ip: null, owner: null, location: null, review_state: "pending",
    link_joined_from: "10.50.0.9", link_joined_at: "2026-09-30T08:00:00Z",
    ...overrides,
  };
}

function listFrom(review: Device[]) {
  return vi.fn(async (params: DeviceListParams = {}): Promise<DevicePage> => {
    const items = params.state === "review" ? review : [];
    const offset = params.offset ?? 0;
    const limit = params.limit ?? 50;
    return {
      items: items.slice(offset, offset + limit), total: items.length,
      state_counts: { all: review.length, online: review.length, offline: 0, assets: 0, certs: 0, review: review.length },
      fleet: { total: review.length, managed: review.length, online: review.length, cert_attention: 0 },
    };
  });
}

let lastQueryClient: QueryClient;

function renderDevices(api: Partial<ApiClient>, permissions: string[], path = "/devices?state=review") {
  mockUseApi.mockReturnValue({ me: vi.fn().mockResolvedValue({ permissions } as Me), ...api } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  lastQueryClient = queryClient;
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/devices" element={<DevicesPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => mockUseApi.mockReset());

describe("reviewListDigest", () => {
  it("is the server's review_list_digest, byte for byte", async () => {
    const one = "00000000-0000-0000-0000-000000000001";
    const two = "00000000-0000-0000-0000-000000000002";
    // tests/test_device_review_approve_all.py computes this same value.
    const expected = "7a70c782c5d30f61a8f57b905eaa11c41ec8ffeafaa3a0e99c4fca60044a28d4";
    expect(await reviewListDigest([two, one, two])).toBe(expected);
  });
});

describe("pendingForReview", () => {
  it("reads every page of the review tab and leaves out a rejected row", async () => {
    const rows = [
      ...Array.from({ length: 620 }, (_, i) => device(i + 1)),
      device(9999, { review_state: "rejected", rejection_outcome: "agent_did_not_confirm" }),
    ];
    const listDevices = listFrom(rows);
    const { devices, more } = await pendingForReview({ listDevices } as unknown as ApiClient);
    expect(devices).toHaveLength(620);
    expect(more).toBe(false);
    expect(listDevices).toHaveBeenCalledWith({ state: "review", limit: 500, offset: 0 });
    expect(listDevices).toHaveBeenCalledWith({ state: "review", limit: 500, offset: 500 });
  });

  it("counts a machine once when it appears on two pages", async () => {
    // Paging is by offset over a hostname-ordered list while machines keep
    // joining: a join ahead of the offset shifts a machine onto the next page.
    const rows = Array.from({ length: 600 }, (_, i) => device(i + 1));
    const listDevices = vi.fn(async (params: DeviceListParams = {}): Promise<DevicePage> => {
      const offset = params.offset ?? 0;
      // The second page starts three rows early: 498, 499 and 500 repeat.
      const start = offset === 0 ? 0 : offset - 3;
      const items = rows.slice(start, offset + (params.limit ?? 50));
      return {
        items, total: rows.length,
        state_counts: { all: 600, online: 600, offline: 0, assets: 0, certs: 0, review: 600 },
        fleet: { total: 600, managed: 600, online: 600, cert_attention: 0 },
      };
    });
    const { devices } = await pendingForReview({ listDevices } as unknown as ApiClient);
    expect(devices).toHaveLength(600);
    expect(new Set(devices.map((d) => d.id)).size).toBe(600);
  });

  it("stops at 1,000 and says there are more", async () => {
    const listDevices = listFrom(Array.from({ length: 1200 }, (_, i) => device(i + 1)));
    const { devices, more } = await pendingForReview({ listDevices } as unknown as ApiClient);
    expect(devices).toHaveLength(1000);
    expect(more).toBe(true);
  });
});

describe("approveAllMessage", () => {
  it("says what was approved and what still waits", () => {
    expect(approveAllMessage({ bulk_id: "b", approved: 3, not_pending: 0, still_waiting: 0 })).toBe(
      "Approved 3 machines.",
    );
    expect(approveAllMessage({ bulk_id: "b", approved: 1, not_pending: 1, still_waiting: 2 })).toBe(
      "Approved 1 machine. 1 machine on the list was no longer waiting. " +
        "2 machines still wait for review: they joined after the list was shown.",
    );
  });

  // Codex on #92: past 1,000 the dialog approves only the first 1,000, so the
  // rest were already waiting, not newcomers.
  it("says to run it again when the list was capped at 1,000", () => {
    expect(
      approveAllMessage({ bulk_id: "b", approved: 1000, not_pending: 0, still_waiting: 240 }, { capped: true }),
    ).toBe(
      "Approved 1000 machines. 240 machines still wait for review: Approve all takes at most 1,000 at a time, " +
        "so run it again for the rest.",
    );
  });
});

describe("Approve all on Devices", () => {
  it("is offered on Waiting for review to a reviewer, and nowhere else", async () => {
    const review = [device(1)];
    renderDevices({ listDevices: listFrom(review) }, ["devices.read", "devices.review"]);
    expect(await screen.findByRole("button", { name: "Approve all…" })).toBeInTheDocument();
  });

  it("is not offered without devices.review", async () => {
    renderDevices({ listDevices: listFrom([device(1)]) }, ["devices.read"]);
    expect(await screen.findByText("DESKTOP-1")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve all…" })).not.toBeInTheDocument();
  });

  it("names the machines, sends exactly that list with its digest, and reports the result", async () => {
    const review = Array.from({ length: 25 }, (_, i) => device(i + 1));
    const approveAllDevices = vi.fn().mockResolvedValue({ bulk_id: "b", approved: 25, not_pending: 0, still_waiting: 1 });
    renderDevices({ listDevices: listFrom(review), approveAllDevices }, ["devices.read", "devices.review"]);
    await userEvent.click(await screen.findByRole("button", { name: "Approve all…" }));

    const dialog = await screen.findByRole("dialog", { name: "Approve all waiting machines" });
    const names = await within(dialog).findByRole("list", { name: "Machines to approve" });
    expect(within(names).getAllByRole("listitem")).toHaveLength(20);
    expect(within(dialog).getByText("Showing 1–20 of 25.")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Next page of the machines to approve" }));
    expect(within(names).getAllByRole("listitem")).toHaveLength(5);

    await userEvent.click(within(dialog).getByRole("button", { name: "Approve 25 machines" }));
    const ids = review.map((d) => d.id);
    expect(approveAllDevices).toHaveBeenCalledWith({ device_ids: ids, list_digest: await reviewListDigest(ids) });
    expect(
      await screen.findByText("Approved 25 machines. 1 machine still waits for review: it joined after the list was shown."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  async function openDialog(review: Device[], extra: Partial<ApiClient> = {}) {
    const listDevices = listFrom(review);
    renderDevices({ listDevices, ...extra }, ["devices.read", "devices.review"]);
    await userEvent.click(await screen.findByRole("button", { name: "Approve all…" }));
    const dialog = await screen.findByRole("dialog", { name: "Approve all waiting machines" });
    await within(dialog).findByRole("list", { name: "Machines to approve" });
    const reviewCalls = () => listDevices.mock.calls.filter(([params]) => params?.state === "review" && params.limit === 500).length;
    return { dialog, listDevices, reviewCalls };
  }

  it("does not refetch the confirmed list on window focus or reconnect, and it never goes stale", async () => {
    const { reviewCalls } = await openDialog([device(1), device(2)]);
    expect(reviewCalls()).toBe(1);
    const query = lastQueryClient.getQueryCache().find({ queryKey: ["review-approve-all"] });
    expect(query?.isStale()).toBe(false);

    act(() => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
      onlineManager.setOnline(false);
      onlineManager.setOnline(true);
    });
    await act(async () => {
      await lastQueryClient.refetchQueries({ stale: true });
    });
    focusManager.setFocused(undefined);
    expect(reviewCalls()).toBe(1);
  });

  it("forgets the list on close, so opening again shows a fresh one", async () => {
    const review = [device(1), device(2)];
    const { dialog, listDevices } = await openDialog(review);
    expect(within(dialog).getByRole("button", { name: "Approve 2 machines" })).toBeInTheDocument();

    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(lastQueryClient.getQueryData(["review-approve-all"])).toBeUndefined();

    review.push(device(3));
    listDevices.mockClear();
    await userEvent.click(screen.getByRole("button", { name: "Approve all…" }));
    const again = await screen.findByRole("dialog", { name: "Approve all waiting machines" });
    expect(await within(again).findByRole("button", { name: "Approve 3 machines" })).toBeInTheDocument();
    expect(listDevices).toHaveBeenCalledWith({ state: "review", limit: 500, offset: 0 });
  });

  it("cannot be confirmed while the list is being read", async () => {
    const review = [device(1), device(2)];
    const { dialog, listDevices } = await openDialog(review);
    const confirm = within(dialog).getByRole("button", { name: "Approve 2 machines" });
    expect(confirm).toBeEnabled();

    let release: () => void = () => undefined;
    listDevices.mockImplementationOnce(
      (params: DeviceListParams = {}) =>
        new Promise<DevicePage>((resolve) => {
          release = () => void listFrom(review)(params).then(resolve);
        }),
    );
    act(() => {
      void lastQueryClient.refetchQueries({ queryKey: ["review-approve-all"] });
    });
    await waitFor(() => expect(confirm).toBeDisabled());
    await act(async () => release());
    await waitFor(() => expect(confirm).toBeEnabled());
  });

  it("counts and shows a machine once when the pages overlap", async () => {
    const review = [device(1), device(2), device(3)];
    const listDevices = vi.fn(async (params: DeviceListParams = {}): Promise<DevicePage> => ({
      // The dialog's read (limit 500) sees a machine twice, as an offset page
      // does when a join shifts it: counted, shown and sent once. The page's
      // own table is served the plain list.
      items: params.state !== "review" ? [] : params.limit === 500 ? [...review, device(2)] : review,
      total: params.limit === 500 ? 4 : 3,
      state_counts: { all: 3, online: 3, offline: 0, assets: 0, certs: 0, review: 3 },
      fleet: { total: 3, managed: 3, online: 3, cert_attention: 0 },
    }));
    const approveAllDevices = vi.fn().mockResolvedValue({ bulk_id: "b", approved: 3, not_pending: 0, still_waiting: 0 });
    renderDevices({ listDevices, approveAllDevices }, ["devices.read", "devices.review"]);
    await userEvent.click(await screen.findByRole("button", { name: "Approve all…" }));
    const dialog = await screen.findByRole("dialog", { name: "Approve all waiting machines" });
    const names = await within(dialog).findByRole("list", { name: "Machines to approve" });
    expect(within(names).getAllByRole("listitem")).toHaveLength(3);
    expect(within(dialog).getByText(/^3 machines are waiting for review\./)).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Approve 3 machines" }));
    const ids = review.map((d) => d.id);
    expect(approveAllDevices).toHaveBeenCalledWith({ device_ids: ids, list_digest: await reviewListDigest(ids) });
  });
});
