/**
 * The shell's account chip names the signed-in role. English shows it as
 * stored ("org-admin", as it always has); Spanish words a seeded role
 * ("Administrador de la organización"), and re-words it on a live switch.
 * Set up like App.crumb.test.tsx, which renders the real `Shell`.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "./i18n/core";

vi.mock("./lib/auth", () => ({
  safeReturnTo: () => "/devices",
  signIn: () => Promise.resolve(),
  completeSignIn: () => Promise.resolve(null),
  isAuthenticated: () => true,
  initAuth: () => Promise.resolve(null),
  signOut: () => Promise.resolve(),
  accessToken: () => null,
  oidcAuthority: "https://itms.example/realms/it-suite",
  isSessionExpiring: () => false,
  onSessionExpiring: () => () => {},
  startAccountAction: () => Promise.resolve(),
  userManager: { events: { addUserLoaded: () => {}, addUserUnloaded: () => {} } },
}));

const apiRef = vi.hoisted(() => ({ current: undefined as unknown }));
vi.mock("./lib/ApiContext", () => ({
  useApi: () => apiRef.current,
  ApiProvider: ({ children }: { children: React.ReactNode }) => children,
}));

const { Shell } = await import("./App");

beforeEach(() => {
  apiRef.current = {
    me: vi.fn().mockResolvedValue({
      display_name: "Ada",
      email: "ada@example.test",
      org_id: "org-1",
      roles: ["org-admin"],
      permissions: ["devices.read"],
    }),
    getCurrentOrg: vi.fn().mockResolvedValue({ id: "org-1", name: "Toeshee" }),
    getHealthAlertSummary: vi.fn().mockResolvedValue({
      open: 0,
      by_state: { active: 0, acknowledged: 0, muted: 0 },
      by_severity: { info: 0, warning: 0, critical: 0 },
      worst_severity: null,
    }),
    getPlatformStatus: vi.fn().mockResolvedValue({ overall: "ok", probes: [], processes: [], modules: [] }),
  };
});

afterEach(() => {
  cleanup();
  act(() => setActiveLanguage("en"));
});

describe("the shell's account chip role", () => {
  it("is the stored role in English and the seeded role's Spanish name in Spanish, live", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/devices"]}>
          <Shell>
            <p>page body</p>
          </Shell>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(client.isFetching()).toBe(0));
    expect(document.querySelector(".profile-role")?.textContent).toBe("org-admin");
    act(() => setActiveLanguage("es"));
    expect(document.querySelector(".profile-role")?.textContent).toBe("Administrador de la organización");
    act(() => setActiveLanguage("en"));
    expect(document.querySelector(".profile-role")?.textContent).toBe("org-admin");
  });
});
