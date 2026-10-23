import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../lib/api";
import { useReviewCount } from "./useReviewCount";

const mockUseApi = vi.fn();
vi.mock("../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => mockUseApi.mockReset());

describe("useReviewCount", () => {
  it("is the server's review count for someone who can act on it", async () => {
    const listDevices = vi.fn().mockResolvedValue({
      items: [], total: 3,
      state_counts: { all: 40, online: 30, offline: 10, assets: 0, certs: 0, review: 3 },
      fleet: { total: 40, managed: 40, online: 30, cert_attention: 0 },
    });
    mockUseApi.mockReturnValue({ listDevices } as unknown as ApiClient);
    const { result } = renderHook(() => useReviewCount(["devices.read", "devices.review"]), { wrapper });
    await waitFor(() => expect(result.current).toBe(3));
    expect(listDevices).toHaveBeenCalledWith({ state: "review", limit: 1 });
  });

  it("asks nothing and shows nothing for anyone else", () => {
    const listDevices = vi.fn();
    mockUseApi.mockReturnValue({ listDevices } as unknown as ApiClient);
    const { result } = renderHook(() => useReviewCount(["devices.read"]), { wrapper });
    expect(result.current).toBeNull();
    expect(listDevices).not.toHaveBeenCalled();
  });
});
