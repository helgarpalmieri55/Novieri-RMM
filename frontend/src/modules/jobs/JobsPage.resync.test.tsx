/**
 * After a gap in the live socket the jobs page re-reads its jobs (final
 * review W1). It refreshes only on live events, so a `job_status` lost while
 * the socket was down would leave a finished job shown as running until
 * something else happened to touch it.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../lib/api";
import type { UiEvent } from "../../lib/ws";
import { JobsPage } from "./JobsPage";

const live = vi.hoisted(() => ({ handlers: [] as ((event: UiEvent) => void)[] }));
vi.mock("../../lib/ws", () => ({
  useLiveEvents: (handler: (event: UiEvent) => void) => {
    live.handlers.push(handler);
  },
}));
const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

/** Every API call stays pending: this test is about what is invalidated. */
const PENDING = new Proxy({}, { get: () => () => new Promise(() => undefined) });

describe("JobsPage on a resync", () => {
  it("invalidates the jobs", () => {
    mockUseApi.mockReturnValue(PENDING as ApiClient);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidate = vi.spyOn(client, "invalidateQueries");
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/jobs"]}>
          <JobsPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(invalidate).not.toHaveBeenCalled();
    act(() => live.handlers.at(-1)!({ kind: "resync" }));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["jobs"] });
  });
});
