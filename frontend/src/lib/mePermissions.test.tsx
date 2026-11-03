/**
 * The two permission lists /me returns (phase 3 design §1.3): `permissions`,
 * the codes held in at least one client of the request's scope, and
 * `permissions_everywhere`, the codes held on every client -- what a screen
 * of configuration every client shares offers its writes on (§2).
 *
 * THREE STATES, NOT TWO, for both: neither `has` nor `lacks` is true until /me
 * has answered. And an API older than phase 3 sends no
 * `permissions_everywhere`: its `permissions` WAS that set.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient, Me } from "./api";
import { useEverywherePermissions, useMePermissions } from "./mePermissions";

const mockUseApi = vi.fn();
vi.mock("./ApiContext", () => ({ useApi: () => mockUseApi() }));

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "tech@example.com",
  display_name: "Tech",
  permissions: ["sla.read", "sla.admin"],
  permissions_everywhere: ["sla.read"],
};

function hook<T>(use: () => T, me: Promise<Me>) {
  mockUseApi.mockReturnValue({ me: vi.fn().mockReturnValue(me) } as unknown as ApiClient);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
  return renderHook(use, { wrapper });
}

describe("useEverywherePermissions", () => {
  it("reads the codes held on every client, not those held in some", async () => {
    const { result } = hook(() => useEverywherePermissions(), Promise.resolve(ME));
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.has("sla.read")).toBe(true);
    expect(result.current.has("sla.admin")).toBe(false);
    expect(result.current.lacks("sla.admin")).toBe(true);
  });

  it("is neither yes nor no before /me answers", () => {
    const { result } = hook(() => useEverywherePermissions(), new Promise<Me>(() => {}));
    expect(result.current.status).toBe("loading");
    expect(result.current.has("sla.read")).toBe(false);
    expect(result.current.lacks("sla.read")).toBe(false);
  });

  it("reads `permissions` from an API that sends no permissions_everywhere", async () => {
    const older: Me = { ...ME, permissions_everywhere: undefined };
    const { result } = hook(() => useEverywherePermissions(), Promise.resolve(older));
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.has("sla.admin")).toBe(true);
  });

  it("leaves useMePermissions on the codes held in some client", async () => {
    const { result } = hook(() => useMePermissions(), Promise.resolve(ME));
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.has("sla.admin")).toBe(true);
  });
});
