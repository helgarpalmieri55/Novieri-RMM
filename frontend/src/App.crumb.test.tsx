/**
 * WHAT THE BREADCRUMB ACTUALLY RENDERS, from the real `Shell`.
 *
 * `nav.test.ts` holds `crumbFor` to the right ANSWER; `AppShell.test.tsx`
 * holds the shell to drawing a section as a link when it is given one. Neither
 * of them asserts that the two are wired together, and until this file existed
 * nothing did: no test in this project imported `App.tsx`'s `Shell`, so the
 * whole /software - /discovered - /agent-push crumb fix -- three review
 * findings filed by three groups -- was verified by prose only.
 *
 * THE HREF IS THE HALF THAT DEGRADES IN SILENCE. Drop `sectionTo` from the
 * `AppShell` call below, or rename a `NAV_GROUPS` label, and the section is
 * still rendered with the right words; it simply stops being clickable, which
 * is precisely the dead crumb the findings were about. So every case here
 * asserts the destination, not just the text.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The identity provider, stubbed at the module boundary `App.auth.test.tsx`
// already uses. `Shell` subscribes to the session-expiry notice on mount, so
// a mock missing those exports fails at render.
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

// The API, likewise. `Shell` calls `me()` and `getCurrentOrg()` itself, and
// `useHealthReading` calls the two status endpoints; nothing else here has a
// page mounted under it to make a request.
const apiRef = vi.hoisted(() => ({ current: undefined as unknown }));
vi.mock("./lib/ApiContext", () => ({
  useApi: () => apiRef.current,
  ApiProvider: ({ children }: { children: React.ReactNode }) => children,
}));

const { Shell } = await import("./App");

/** An operator holding everything, so no crumb below is hidden by permissions. */
const ALL_PERMISSIONS = [
  "devices.read",
  "jobs.read",
  "health.read",
  "platform.status.read",
  "agent.push.scope.manage",
];

beforeEach(() => {
  apiRef.current = {
    me: vi.fn().mockResolvedValue({
      display_name: "Ada",
      email: "ada@example.test",
      org_id: "org-1",
      roles: ["org-admin"],
      permissions: ALL_PERMISSIONS,
    }),
    getCurrentOrg: vi.fn().mockResolvedValue({ id: "org-1", name: "Toeshee" }),
    getHealthAlertSummary: vi.fn().mockResolvedValue({
      open: 0,
      by_state: { active: 0, acknowledged: 0, muted: 0 },
      by_severity: { info: 0, warning: 0, critical: 0 },
      worst_severity: null,
    }),
    getPlatformStatus: vi
      .fn()
      .mockResolvedValue({ overall: "ok", probes: [], processes: [], modules: [] }),
  };
});

afterEach(() => {
  cleanup();
});

/** The crumb at one route, once the shell has settled. */
async function crumbAt(pathname: string): Promise<HTMLElement> {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[pathname]}>
        <Shell>
          <p>page body</p>
        </Shell>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  // `me()` decides the rail and the crumb renders beside it; waiting for the
  // queries to land keeps these assertions off a half-mounted shell.
  await waitFor(() => expect(client.isFetching()).toBe(0));
  return screen.getByRole("navigation", { name: "Breadcrumb" });
}

describe("the shell's breadcrumb, at a route the rail lists", () => {
  it("names the rail entry the page lives under, and links it", async () => {
    const crumb = await crumbAt("/devices");
    const section = within(crumb).getByRole("link", { name: "Devices" });
    expect(section).toHaveAttribute("href", "/devices");
    expect(document.title).toBe("Devices · ITMS");
  });

  it("names an area's page after the area, linked to its landing page", async () => {
    const crumb = await crumbAt("/jobs");
    expect(within(crumb).getByRole("link", { name: "Admin" })).toHaveAttribute("href", "/admin");
    expect(document.title).toBe("Job history · ITMS");
  });
});

/* THE PAGES NO ITEM NAMES, asserted at the DOM. Each once read "ITMS >
   Overview" -- a section that does not contain the page -- as dead text. */
describe("the shell's breadcrumb, at a route the rail does not list", () => {
  it.each([
    ["/devices/onboard", "Devices", "/devices", "Add a computer · ITMS"],
    ["/software", "Software", "/deploy", "Installed software · ITMS"],
    ["/discovered", "Devices", "/devices", "Discovered · ITMS"],
    ["/agent-push", "Admin", "/admin", "Push install · ITMS"],
    ["/agent-push/credential-scopes", "Admin", "/admin", "Push install · ITMS"],
  ])("%s sits under %s, linked to %s", async (path, section, href, title) => {
    const crumb = await crumbAt(path);
    const link = within(crumb).getByRole("link", { name: section });
    expect(link).toHaveAttribute("href", href);
    expect(document.title).toBe(title);
    // NOT "Overview", which is what all three said before `NAV_UNLISTED`, and
    // what they would say again if the entry were deleted.
    expect(within(crumb).queryByText("Overview")).toBeNull();
  });
});

describe("the shell's breadcrumb, at a route in no section at all", () => {
  it("names /profile after itself and gives it nothing to click", async () => {
    const crumb = await crumbAt("/profile");
    // Present as text...
    expect(within(crumb).getByText("Your account")).toBeInTheDocument();
    // ...and deliberately not a link: there is no page behind "Your account",
    // and a crumb that goes nowhere is the false affordance the audit filed.
    expect(within(crumb).queryByRole("link", { name: "Your account" })).toBeNull();
    // ITMS is still the way home.
    expect(within(crumb).getByRole("link", { name: "ITMS" })).toHaveAttribute("href", "/");
  });
});
