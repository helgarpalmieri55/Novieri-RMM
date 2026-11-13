/**
 * `/oauth/authorize` needs a session and draws no console around it
 * (`RequireAuth bare`, plan 2026-11-06 Task 4). With no session the person is
 * sent to sign in and brought back to the same request, query and all.
 *
 * A CLIENT'S CONTACT NEVER REACHES `bare` (pre-flight T4-2): the console's
 * door answers them 403 `portal_only`, and the guard's contact branch sends
 * them to the portal before the consent page is drawn or asks the server
 * anything. A contact cannot connect AI apps, and the server's consent routes
 * refuse them anyway.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { isPublicPath, navigation } from "../../../lib/session";
import { ConsentPage } from "../ConsentPage";

const { ApiProvider } = await import("../../../lib/ApiContext");
const { RequireAuth } = await import("../../../App");

const ME = {
  user_id: "u1",
  display_name: "Ada Lovelace",
  email: "ada@example.test",
  org_id: "org-1",
  roles: ["org-admin"],
  permissions: ["devices.read", "mcp.use"],
};
const PAGE = "/oauth/authorize?client_id=itms_x&state=s";

function answerMe(status: number, body: unknown): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) =>
      String(input).endsWith("/api/v1/me")
        ? new Response(JSON.stringify(body), { status })
        : new Response("{}", { status: 404 }),
    ),
  );
}

/** Every address the page asked, as text. */
function asked(): string[] {
  return vi.mocked(fetch).mock.calls.map(([input]) => String(input));
}

function bare() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={queryClient}>
      <ApiProvider>
        <MemoryRouter initialEntries={[PAGE]}>
          <RequireAuth bare>
            <p>the consent card</p>
          </RequireAuth>
        </MemoryRouter>
      </ApiProvider>
    </QueryClientProvider>
  );
}

/** The consent page as `App.tsx` mounts it, beside a stand-in for the portal. */
function routed() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={queryClient}>
      <ApiProvider>
        <MemoryRouter initialEntries={[PAGE]}>
          <Routes>
            <Route
              path="/oauth/authorize"
              element={
                <RequireAuth bare>
                  <ConsentPage />
                </RequireAuth>
              }
            />
            <Route path="/portal" element={<p>the portal</p>} />
          </Routes>
        </MemoryRouter>
      </ApiProvider>
    </QueryClientProvider>
  );
}

let assign: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  assign = vi.spyOn(navigation, "assign").mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("the consent page's guard", () => {
  it("draws only the page for a signed-in person: no rail, no top bar", async () => {
    answerMe(200, ME);
    render(bare());
    expect(await screen.findByText("the consent card")).toBeInTheDocument();
    expect(screen.queryByRole("navigation")).toBeNull();
    expect(screen.queryByRole("banner")).toBeNull();
  });

  it("sends a visitor to sign in and back to the same request, query and all", async () => {
    answerMe(401, { code: "auth_required" });
    render(bare());
    await waitFor(() => expect(assign).toHaveBeenCalledTimes(1));
    expect(assign).toHaveBeenCalledWith(`/sign-in?next=${encodeURIComponent(PAGE)}`);
    expect(screen.queryByText("the consent card")).toBeNull();
  });

  it("sends a client's contact to the portal, before the page asks the server anything", async () => {
    answerMe(403, { detail: "this account can only use the portal", code: "portal_only" });
    render(routed());
    expect(await screen.findByText("the portal")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /Connect the app/ })).toBeNull();
    // Non-vacuity: the guard did ask `/me`, and nothing after it.
    expect(asked().some((url) => url.endsWith("/api/v1/me"))).toBe(true);
    expect(asked().filter((url) => url.includes("/oauth/"))).toEqual([]);
    expect(assign).not.toHaveBeenCalled();
  });

  it("is mounted in App.tsx inside that guard, and is no page reached without a session", () => {
    const app = readFileSync(resolve(import.meta.dirname, "../../../App.tsx"), "utf8");
    expect(app).toContain(
      '<Route path="/oauth/authorize" element={<RequireAuth bare><ConsentPage /></RequireAuth>} />',
    );
    expect(isPublicPath("/oauth/authorize")).toBe(false);
  });
});
