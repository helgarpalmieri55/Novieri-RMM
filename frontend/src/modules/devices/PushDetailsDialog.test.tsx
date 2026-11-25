/**
 * Push install details on a device record (asset-inventory spec §2): what a
 * push needs before the machine has an agent.
 *
 * NO WRITE WITHOUT A CHANGE (coordinator ruling 13, fix round 1 of wave O1): a
 * Save on details nobody changed closes and sends nothing, because a PATCH
 * with no change writes an audit entry for a change nobody made.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient, Device } from "../../lib/api";
import { PushDetailsDialog } from "./PushDetailsDialog";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const updateAsset = vi.fn();

beforeEach(() => {
  updateAsset.mockReset().mockResolvedValue({});
  mockUseApi.mockReturnValue({ updateAsset } as unknown as ApiClient);
});

function machine(overrides: Partial<Device> = {}): Device {
  return {
    id: "d-1",
    hostname: "ws-01",
    fqdn: "ws-01.corp.example.com",
    os_family: "windows",
    os_version: "11",
    architecture: "amd64",
    device_group_id: null,
    status: "active",
    agent_status: "online",
    last_seen_at: null,
    cert_not_after: null,
    cert_state: null,
    cert_revoked: false,
    lifecycle: "in_service",
    asset_type: "computer",
    source: "operator",
    provenance: "operator",
    description: null,
    primary_ip: "10.4.7.20",
    owner: null,
    location: null,
    ...overrides,
  } as Device;
}

function renderDialog(onClose = vi.fn()) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <PushDetailsDialog device={machine()} open onClose={onClose} />
    </QueryClientProvider>,
  );
  return { onClose };
}

describe("PushDetailsDialog", () => {
  it("closes on Save when nothing changed, and sends nothing (coordinator ruling 13)", async () => {
    const { onClose } = renderDialog();
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
    expect(updateAsset).not.toHaveBeenCalled();
  });

  it("sends only the field that changed", async () => {
    const { onClose } = renderDialog();
    const dns = screen.getByRole("textbox", { name: /DNS name/ });
    await userEvent.clear(dns);
    await userEvent.type(dns, "ws-01.branch.example.com");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(updateAsset).toHaveBeenCalledWith("d-1", { fqdn: "ws-01.branch.example.com" }));
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
  });
});
