/**
 * The journey, rendered.
 *
 * `journey.test.ts` holds the state machine; this holds the three claims that
 * are only true of the DOM:
 *
 *   1. **Every step is on screen, whatever the operator holds.** The failure
 *      this page exists to fix is a prerequisite nobody could discover, so a
 *      step the caller may not perform must still be visible, with the
 *      permission named. Hiding it reproduces the original bug in a new place.
 *   2. **It links out and does not reimplement.** In particular the
 *      scan-authorization grant -- which decides where this platform emits
 *      packets -- must be a link to the admin-tier screen and not a form here.
 *   3. **It never claims the platform will install anything.** Invariant 2 is
 *      the reason step 3 can fail with `no_agent` at all, and an operator who
 *      does not know it reads that state as a bug.
 *
 * Mocks the auth-backed API client, mirroring `AgentReleasesPage.test.tsx`.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../lib/api";
import type { ScopeOut, SubnetOut, SweepRunOut } from "../ipam/types";
import { OnboardPage } from "./OnboardPage";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const subnet = (over: Partial<SubnetOut> = {}): SubnetOut => ({
  client_id: "o1",
  id: "s1",
  vrf_id: "v1",
  prefix: "10.0.4.0/24",
  kind: "lan",
  name: "office",
  description: "",
  site_id: null,
  vlan_id: null,
  gateway: null,
  dns_servers: [],
  parent_id: null,
  sweep_enabled: true,
  sweep_device_id: null,
  sweep_interval_hours: 168,
  last_swept_at: null,
  ...over,
});

const scope = (): ScopeOut => ({
  client_id: "o1",
  id: "sc1",
  vrf_id: "v1",
  prefix: "10.0.0.0/16",
  note: "",
  authorized_by: "someone",
  authorized_at: "2026-08-01T00:00:00Z",
  expires_at: null,
  revoked_at: null,
});

function renderPage({
  permissions = [
    "devices.read",
    "ipam.manage",
    "ipam.scope.manage",
    "ipam.sweep.run",
    "agent.release.read",
  ],
  subnets = [] as SubnetOut[],
  scopes = [] as ScopeOut[],
  runs = [] as SweepRunOut[],
  candidates = { items: [], total: 0, truncated: false, unsweepable_subnets: 0 },
  releases = [] as unknown[],
}: Record<string, unknown> = {}) {
  mockUseApi.mockReturnValue({
    me: () =>
      Promise.resolve({
        user_id: "u",
        org_id: "o",
        email: "op@example.com",
        display_name: "Operator",
        permissions,
        timezone: null,
      }),
    listIpamSubnets: () => Promise.resolve(subnets),
    listIpamScanAuthorizations: () => Promise.resolve(scopes),
    listIpamSweepRuns: () => Promise.resolve(runs),
    listAssetCandidates: () => Promise.resolve(candidates),
    listAgentReleases: () => Promise.resolve(releases),
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui: ReactElement = (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <OnboardPage />
      </MemoryRouter>
    </QueryClientProvider>
  );
  return render(ui);
}

beforeEach(() => {
  mockUseApi.mockReset();
});

describe("the five steps are one page", () => {
  it("names every step, in order, on a completely empty deployment", async () => {
    renderPage();
    for (const title of [
      "Register the network",
      "Authorize scanning here",
      "Sweep for what is out there",
      "Review what answered",
      "Get the agent onto them",
    ]) {
      expect(await screen.findByText(title)).toBeTruthy();
    }
  });

  it("marks where the operator is", async () => {
    renderPage({ subnets: [subnet()] });
    // Wait for the LAST query to land before reading the marker. The five
    // steps resolve from four independent requests, and asserting mid-flight
    // would be asserting on the loading order rather than on the state.
    await screen.findByText(/No registered network is authorized/);
    // Step 1 is satisfied, so the marker sits on the authorization -- the
    // prerequisite nobody discovers on their own.
    const here = screen.getAllByText("You are here");
    expect(here).toHaveLength(1);
    expect(here[0]?.closest("li")?.textContent).toContain("Authorize scanning here");
  });

  it("draws two steps in the same state with the same gutter", async () => {
    // THE BAR REPORTS THE STATE, NOT THE CURSOR. Two subnets, both
    // authorized, one swept -- so step 3 is partly done AND the current step,
    // and step 4 is partly done and is not. They wear the same pill, so they
    // must wear the same rung; step 3 used to take the accent bar and step 4
    // no bar at all, which reads as a difference in state that does not exist.
    renderPage({
      subnets: [subnet(), subnet({ id: "s2", prefix: "10.0.5.0/24", last_swept_at: "2026-08-26T11:00:00Z" })],
      scopes: [scope()],
      candidates: { items: [], total: 4, truncated: false, unsweepable_subnets: 0 },
      releases: [{ has_artifact: true }],
    });
    await screen.findByText(/1 of 2 authorized networks have been swept/);
    const partly = screen
      .getAllByText("Partly done")
      .map((pill) => pill.closest("li") as HTMLElement);
    expect(partly).toHaveLength(2);
    for (const step of partly) expect(step.className).toContain("warn");

    // And where the operator is stayed said, in the two ways a colour never
    // said it: the ARIA state and the words.
    const current = partly.find((step) => step.getAttribute("aria-current") === "step");
    expect(current?.textContent).toContain("Sweep for what is out there");
    expect(current?.textContent).toContain("You are here");
  });

  it("leaves the rung blank for a step nothing has answered for", async () => {
    // `unknown` is a query in flight, not a verdict, so it takes no severity
    // colour -- the same reason its pill is the muted one.
    renderPage();
    const loading = (await screen.findByText(/Loading your registered networks…/)).closest(
      "li",
    ) as HTMLElement;
    expect(loading.className).not.toContain("warn");
    expect(loading.className).not.toContain("crit");
  });
});

describe("a step the operator may not perform", () => {
  it("stays visible and names the permission", async () => {
    renderPage({ permissions: ["devices.read"] });
    expect(await screen.findByText("Register the network")).toBeTruthy();
    // The sentence is broken by a <code>, so it is matched on the element's
    // own text rather than on a text node.
    expect(
      await screen.findByText(
        (_content, element) =>
          element?.tagName === "P" &&
          /an administrator must do this/i.test(element.textContent ?? "") &&
          (element.textContent ?? "").includes("permission to manage the IP address plan") &&
          !(element.textContent ?? "").includes("ipam.manage"),
      ),
    ).toBeTruthy();
  });

  it("still offers the link, because the refusal is the server's to make", async () => {
    // Invariant 6: the frontend never gates security. Hiding the destination
    // would only decide WHERE the operator learns they cannot do this.
    renderPage({ permissions: ["devices.read"] });
    const link = await screen.findByRole("link", { name: /Open IP Address Management/i });
    expect(link.getAttribute("href")).toBe("/ipam");
  });
});

describe("it links out rather than reimplementing", () => {
  it("sends the scan authorization to the admin-tier screen and offers no form here", async () => {
    renderPage({ subnets: [subnet()] });
    const link = await screen.findByRole("link", { name: /Open scan authorizations/i });
    expect(link.getAttribute("href")).toBe("/ipam/scan-authorizations");
    // THE GRANT IS NOT MADE HERE. It decides where this platform puts packets
    // on somebody's wire; a form on a page designed to feel easy would be the
    // same act behind a friendlier gate.
    expect(screen.queryByRole("button", { name: /authorize/i })).toBeNull();
  });

  it("points every step at a page that already owns it", async () => {
    renderPage({
      subnets: [subnet({ last_swept_at: "2026-08-26T11:00:00Z" })],
      scopes: [scope()],
      releases: [{ has_artifact: true }],
      candidates: { items: [], total: 4, truncated: false, unsweepable_subnets: 0 },
    });
    const hrefs = (await screen.findAllByRole("link")).map((a) => a.getAttribute("href"));
    for (const to of ["/ipam", "/ipam/scan-authorizations", "/discovered", "/agent-packages"]) {
      expect(hrefs).toContain(to);
    }
  });
});

describe("the honesty rules", () => {
  it("says the platform never connects to a machine, before any step", async () => {
    renderPage();
    expect(await screen.findByText(/never connects to a machine/i)).toBeTruthy();
  });

  it("distinguishes 'nothing answered' from 'nothing could look'", async () => {
    const { unmount } = renderPage({
      candidates: { items: [], total: 0, truncated: false, unsweepable_subnets: 0 },
    });
    expect(await screen.findByText(/Nothing is waiting for review on Discovered\./)).toBeTruthy();
    expect(screen.queryByText(/could not be swept at all/)).toBeNull();
    unmount();

    renderPage({
      candidates: { items: [], total: 0, truncated: false, unsweepable_subnets: 2 },
    });
    expect(await screen.findByText(/could not be swept at all/)).toBeTruthy();
  });

  it("renders no count before the queries have answered", async () => {
    // A zero printed while a request is in flight is the console asserting a
    // fact it has not established.
    renderPage();
    expect(await screen.findAllByText(/^Checking…$|Loading/)).toBeTruthy();
  });
});
