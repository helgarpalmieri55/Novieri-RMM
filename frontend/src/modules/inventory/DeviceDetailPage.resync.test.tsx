/**
 * After a gap in the live socket a device's page re-reads what the socket
 * keeps fresh (final review W1): its inventory, its changes and its jobs.
 * They refresh only on live events, so one lost in the gap stayed lost.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../lib/api";
import type { UiEvent } from "../../lib/ws";
import { DeviceDetailPage } from "./DeviceDetailPage";

const live = vi.hoisted(() => ({ handlers: [] as ((event: UiEvent) => void)[] }));
vi.mock("../../lib/ws", () => ({
  useLiveEvents: (handler: (event: UiEvent) => void) => {
    live.handlers.push(handler);
  },
}));
const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const PENDING = new Proxy({}, { get: () => () => new Promise(() => undefined) });
const DEVICE = "11111111-1111-4111-8111-111111111111";

describe("DeviceDetailPage on a resync", () => {
  it("invalidates the device's inventory, its changes and the jobs", () => {
    mockUseApi.mockReturnValue(PENDING as ApiClient);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidate = vi.spyOn(client, "invalidateQueries");
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/devices/${DEVICE}`]}>
          <Routes>
            <Route path="/devices/:deviceId" element={<DeviceDetailPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(invalidate).not.toHaveBeenCalled();
    act(() => live.handlers.at(-1)!({ kind: "resync" }));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["inventory", DEVICE] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["inventory-changes", DEVICE] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["jobs"] });
  });
});
