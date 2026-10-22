/**
 * Test helpers for the per-page "Before this page can do its job" panels
 * (`components/prerequisites.tsx`). Each panel test mocks `useApi` itself --
 * `vi.mock` is hoisted per file -- and uses these to render and to read the
 * three things every panel test asserts: the CTA's destination when a
 * prerequisite is missing, the panel's absence when everything is in place,
 * and "cannot be checked from this account" when the permission is not held.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { expect, vi } from "vitest";

/** `api.me` answering with these codes. */
export function meWith(permissions: readonly string[]) {
  return vi.fn().mockResolvedValue({
    user_id: "u1",
    org_id: "o1",
    email: "a@b.c",
    display_name: "A",
    permissions: [...permissions],
  });
}

export function renderPanel(node: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The CTA link named `name` points at `href`. */
export async function expectCta(name: string, href: string) {
  const link = await screen.findByRole("link", { name });
  expect(link).toHaveAttribute("href", href);
}

/**
 * The panel titled `title` is not on screen once `settled` holds and the
 * answers have rendered. A panel is also hidden while its checks are still
 * loading, so asserting absence straight after the request would pass on a
 * panel that never reads its answer; the pause lets the answers land first.
 */
export async function expectNoPanel(title: string, settled: () => void) {
  await waitFor(settled);
  await new Promise((resolve) => setTimeout(resolve, 30));
  expect(screen.queryByText(title)).toBeNull();
}
