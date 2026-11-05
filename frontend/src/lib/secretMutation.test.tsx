/**
 * A mutation that carries a secret leaves none in TanStack's mutation cache
 * (operator decision 4, 2026-11-05, "Barrer todas ahora"; final review D2,
 * Minor 1), while the page keeps what came of it.
 *
 * Measured as the harness measures it (`state.variables` / `state.data` of
 * every mutation in the cache), and as a script on the page would read it.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useSecretMutation } from "./secretMutation";

const SECRET = "hunter2-relay-password";

let queryClient: QueryClient;

function wrapper({ children }: { children: ReactNode }) {
  return createElement(QueryClientProvider, { client: queryClient }, children);
}

function cacheText(): string {
  return JSON.stringify(
    queryClient
      .getMutationCache()
      .getAll()
      .map((mutation) => ({ data: mutation.state.data, variables: mutation.state.variables })),
  );
}

beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
});

describe("useSecretMutation, a secret SENT", () => {
  it("drops the body once it settles, and keeps the success", async () => {
    const send = vi.fn(async (_body: { password: string }) => ({ saved: true }));
    const onSuccess = vi.fn();
    const { result } = renderHook(() => useSecretMutation({ mutationFn: send, onSuccess }), { wrapper });

    act(() => result.current.mutate({ password: SECRET }));
    expect(result.current.isPending).toBe(true);
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(send.mock.calls[0]?.[0]).toEqual({ password: SECRET });
    expect(onSuccess).toHaveBeenCalledTimes(1);
    expect(result.current.isPending).toBe(false);
    expect(result.current.error).toBeNull();
    await waitFor(() => expect(cacheText()).not.toContain(SECRET));
    expect(queryClient.getMutationCache().getAll()).toEqual([]);
  });

  it("drops the body once it fails, and keeps the refusal", async () => {
    const refusal = new Error("the relay refused the password");
    const { result } = renderHook(
      () => useSecretMutation({ mutationFn: async (_body: { password: string }) => Promise.reject(refusal) }),
      { wrapper },
    );

    act(() => result.current.mutate({ password: SECRET }));
    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(result.current.error).toBe(refusal);
    expect(result.current.isSuccess).toBe(false);
    await waitFor(() => expect(cacheText()).not.toContain(SECRET));
  });

  it("calls the page's own callbacks first, with what they need", async () => {
    const { result } = renderHook(
      () => useSecretMutation({ mutationFn: async (body: { password: string }) => body.password.length }),
      { wrapper },
    );
    const seen: unknown[] = [];
    act(() =>
      result.current.mutate(
        { password: SECRET },
        {
          onSuccess: (length) => seen.push(["success", length]),
          onSettled: (length, error) => seen.push(["settled", length, error]),
        },
      ),
    );
    await waitFor(() => expect(seen).toHaveLength(2));
    expect(seen).toEqual([
      ["success", SECRET.length],
      ["settled", SECRET.length, null],
    ]);
  });

  it("drops the body even when the page that sent it is gone before the answer", async () => {
    let answer: ((value: string) => void) | null = null;
    const { result, unmount } = renderHook(
      () => useSecretMutation({ mutationFn: (_body: { password: string }) => new Promise<string>((resolve) => (answer = resolve)) }),
      { wrapper },
    );
    act(() => result.current.mutate({ password: SECRET }));
    await waitFor(() => expect(answer).not.toBeNull());
    unmount();
    expect(cacheText()).toContain(SECRET);
    await act(async () => answer?.("saved"));
    await waitFor(() => expect(queryClient.getMutationCache().getAll()).toEqual([]));
  });
});

describe("useSecretMutation, a secret ANSWERED", () => {
  it("hands the value to the page once, and keeps none of it", async () => {
    const { result } = renderHook(
      () => useSecretMutation({ mutationFn: async (_id: string) => ({ token: SECRET }) }),
      { wrapper },
    );
    let shown: string | null = null;
    act(() => result.current.mutate("token-1", { onSuccess: (out) => (shown = out.token) }));
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(shown).toBe(SECRET);
    expect(result.current).not.toHaveProperty("data");
    await waitFor(() => expect(cacheText()).not.toContain(SECRET));
  });

  it("does the same through mutateAsync, whose caller gets the answer", async () => {
    const { result } = renderHook(
      () => useSecretMutation({ mutationFn: async (_id: string) => ({ token: SECRET }) }),
      { wrapper },
    );
    let out: { token: string } | undefined;
    await act(async () => {
      out = await result.current.mutateAsync("token-1");
    });
    expect(out?.token).toBe(SECRET);
    expect(result.current.isSuccess).toBe(true);
    await waitFor(() => expect(cacheText()).not.toContain(SECRET));
  });

  it("rejects mutateAsync with the refusal, and keeps it", async () => {
    const refusal = new Error("not allowed");
    const { result } = renderHook(
      () => useSecretMutation({ mutationFn: async (_id: string) => Promise.reject<{ token: string }>(refusal) }),
      { wrapper },
    );
    await act(async () => {
      await expect(result.current.mutateAsync("token-1")).rejects.toBe(refusal);
    });
    expect(result.current.error).toBe(refusal);
  });
});

describe("useSecretMutation.reset", () => {
  it("forgets the outcome too", async () => {
    const { result } = renderHook(
      () => useSecretMutation({ mutationFn: async (_body: { password: string }) => Promise.reject(new Error("no")) }),
      { wrapper },
    );
    act(() => result.current.mutate({ password: SECRET }));
    await waitFor(() => expect(result.current.isError).toBe(true));
    act(() => result.current.reset());
    expect(result.current.isError).toBe(false);
    expect(result.current.error).toBeNull();
    expect(result.current.isIdle).toBe(true);
  });
});
