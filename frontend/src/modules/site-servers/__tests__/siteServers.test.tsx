// Component tests against RENDERED OUTPUT (docs/modules/site-server/plan.md
// Task 10).
//
// EVERY ASSERTION BELOW IS ABOUT A DISTINCTION, because in each case the wrong
// rendering is a specific operational lie:
//
//   1. a declared-but-unobserved segment against a matched one — §0.8's designed
//      condition, not an error and not "fine";
//   2. `evidence: "unavailable"` against "no mismatches" — opposite answers that
//      would otherwise render identically, since both produce three empty lists;
//   3. `enabled: false` against a live row — a demoted-without-delete server
//      that looks live is a page that gets trusted;
//   4. the promote dialog offering `wake` and NOT `cache`/`pxe` — the service
//      refuses those with a 400;
//   5. a device with no agent shown-and-disabled against silently absent.
//
// NOT WRITTEN HERE, deliberately: any test that asserts `Object.keys()` over a
// fixture defined in the same file. That test cannot fail, and this project has
// shipped one.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient, Device, Job, Me, Page } from "../../../lib/api";
import { PromoteDialog } from "../PromoteDialog";
import { SegmentCheck } from "../SegmentCheck";
import { SiteServerDetail } from "../SiteServerDetail";
import { SiteServerList } from "../SiteServerList";
import { SiteServersPage } from "../SiteServersPage";
import { WakeAction, wakeStateOf } from "../WakeAction";
import type { SegmentCheckOut, SiteServerOut } from "../types";

function renderWithRouter(node: React.ReactNode) {
  return render(<MemoryRouter initialEntries={["/site-servers"]}>{node}</MemoryRouter>);
}

// Only SiteServersPage (the container, migrated onto the design per the UI
// port spec §6) needs the auth-backed API client — everything above this
// point is a pure presentational-component test with no query layer.
const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function renderPage(entry = "/site-servers") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>
        <SiteServersPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/**
 * THE ROUTE TABLE, READ FROM THE APPLICATION ITSELF.
 *
 * This module is the one that shipped ~1,300 lines nobody could reach, so the
 * lesson is already written at the top of this file: "reachable" has to be
 * asserted, not assumed. A tile pointing at `/site-server` (no `s`) is the
 * same defect one level down — a working anchor to nothing — and only
 * `App.tsx`'s own `path=` list can catch it.
 */
const APP_SOURCE = Object.values(
  import.meta.glob("../../../App.tsx", { query: "?raw", import: "default", eager: true }),
)[0] as string;

const ROUTED_PATHS = new Set(
  [...APP_SOURCE.matchAll(/path="([^"]+)"/g)].map((match) => match[1] as string),
);

function expectLiveTiles(expected: readonly string[]) {
  const tiles = [...document.querySelectorAll(".stat-card")];
  expect(tiles.map((tile) => tile.getAttribute("href"))).toEqual([...expected]);
  for (const tile of tiles) {
    expect(tile.tagName).toBe("A");
    const path = (tile.getAttribute("href") ?? "").split("?")[0];
    expect(ROUTED_PATHS.has(path as string), `${path} is not a route in App.tsx`).toBe(true);
  }
}

const PAGE_ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "operator@example.com",
  display_name: "Operator",
  permissions: ["site.read", "site.manage", "site.wake"],
};

function pageServer(overrides: Partial<SiteServerOut> = {}): SiteServerOut {
  return {
    id: "ss-1",
    device_id: "dev-1",
    name: "branch-manila-01",
    segments: ["10.20.0.0/16"],
    roles: ["wake"],
    enabled: true,
    notes: "",
    client_id: "o1",
    site_id: null,
    agent_status: "online",
    agent_last_seen_at: "2026-08-06T11:55:00Z",
    ...overrides,
  };
}

describe("SiteServersPage", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(PAGE_ME),
      listSiteServers: vi.fn().mockResolvedValue([pageServer()]),
      listDevices: vi.fn().mockResolvedValue({ items: [], total: 0 } satisfies Page<Device>),
      promoteSiteServer: vi.fn(),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("shows the loading state distinctly from an empty fleet", async () => {
    api.listSiteServers = vi.fn(() => new Promise<never>(() => {}));
    renderPage();
    expect(await screen.findByText("Loading site servers…")).toBeInTheDocument();
  });

  it("shows the request-failed state distinctly from an empty fleet", async () => {
    api.listSiteServers = vi.fn().mockRejectedValue(new Error("boom"));
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not load/i);
  });

  it("shows the empty state distinctly from a failed request", async () => {
    api.listSiteServers = vi.fn().mockResolvedValue([]);
    renderPage();
    expect(await screen.findByTestId("no-site-servers")).toBeInTheDocument();
  });

  it("counts registered servers and served segments from the data fetched", async () => {
    api.listSiteServers = vi.fn().mockResolvedValue([
      pageServer({ id: "a", name: "site-a", segments: ["10.20.0.0/16"] }),
      pageServer({ id: "b", name: "site-b", segments: ["10.30.0.0/16"], enabled: false }),
    ]);
    renderPage();
    await screen.findByText("site-a");
    const stats = document.querySelectorAll(".stat-card");
    const values = [...stats].map((card) => card.querySelector(".stat-value")?.textContent);
    expect(values).toEqual(["2", "1", "2"]);
  });

  it("opens the promote dialog and submits the real promote mutation", async () => {
    const user = userEvent.setup();
    api.listDevices = vi.fn().mockResolvedValue({
      items: [{ id: "dev-9", hostname: "fresh-box", agent_status: "online" } as Device],
      total: 1,
    } satisfies Page<Device>);
    renderPage();

    await screen.findByText("branch-manila-01");
    await user.click(screen.getByRole("button", { name: "Promote device" }));

    const dialog = await screen.findByRole("dialog");
    await user.selectOptions(
      screen.getByLabelText("Device"),
      screen.getByTestId("device-option-dev-9"),
    );
    await user.type(screen.getByLabelText("Segments (comma separated CIDRs)"), "10.50.0.0/16");
    await user.click(within(dialog).getByRole("button", { name: "Promote" }));

    await waitFor(() =>
      expect(api.promoteSiteServer).toHaveBeenCalledWith({
        device_id: "dev-9",
        segments: ["10.50.0.0/16"],
        roles: ["wake"],
      }),
    );
  });

  // Auditor A (HIGH): "Internal spec reference shipped in production UI copy"
  // — the description read "cache and PXE delivery are specified but not yet
  // built (site-server spec §4.4)".
  it("gives the fleet table a name of its own", async () => {
    // IT HAD ONE BEFORE THE PORT and lost it: `<table aria-label="Site
    // servers">` became `<Table>`, which accepted no such prop. The wrapper is
    // a tab stop, so a keyboard user lands on a scrollable region announced as
    // nothing. `Table` takes `label` now; this holds the call site to passing
    // it, since the component cannot require what most tables do not need.
    renderPage();
    expect(await screen.findByRole("table", { name: "Site servers" })).toBeInTheDocument();
  });

  it("does not cite an internal spec section in the page description", async () => {
    renderPage();
    await screen.findByText("branch-manila-01");

    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/spec §/);
    expect(text).not.toMatch(/site-server spec/);
    expect(text).not.toMatch(/specified but not yet built/);
    // The limitation is still disclosed — in words a buyer can act on.
    expect(screen.getByText(/not available in this release/)).toBeInTheDocument();
  });

  it("does not offer Promote device without site.manage", async () => {
    api.me = vi.fn().mockResolvedValue({ ...PAGE_ME, permissions: ["site.read"] });
    renderPage();
    await screen.findByText("branch-manila-01");
    expect(screen.queryByRole("button", { name: "Promote device" })).not.toBeInTheDocument();
  });
});

function aServer(overrides: Partial<SiteServerOut> = {}): SiteServerOut {
  return {
    id: "ss-1",
    device_id: "dev-1",
    name: "branch-manila-01",
    segments: ["10.20.0.0/16"],
    roles: ["wake"],
    enabled: true,
    notes: "",
    client_id: "o1",
    site_id: null,
    agent_status: "online",
    agent_last_seen_at: "2026-08-06T11:55:00Z",
    ...overrides,
  };
}

function aDevice(overrides: Partial<Device> = {}): Device {
  return {
    id: "dev-1",
    hostname: "branch-manila-01",
    fqdn: null,
    os_family: "linux",
    os_version: "",
    architecture: "",
    device_group_id: null,
    status: "active",
    agent_status: "online",
    last_seen_at: null,
    ...overrides,
  } as Device;
}

// ---------------------------------------------------------------------------
// 1. A declared-but-unobserved segment is its own state
// ---------------------------------------------------------------------------

describe("SegmentCheck", () => {
  it("renders a declared-but-unobserved segment separately from a matched one", () => {
    // BOTH lists are populated, so the test observes WHICH segment landed
    // where. With only one segment the rendering could fold them together and
    // still pass.
    const check: SegmentCheckOut = {
      site_server_id: "ss-1",
      device_id: "dev-1",
      evidence: "ipam",
      matched: ["10.20.0.0/16"],
      declared_unobserved: ["10.30.0.0/16"],
      observed_undeclared: [],
    };
    render(<SegmentCheck check={check} />);

    const matched = screen.getByTestId("matched");
    const unobserved = screen.getByTestId("declared-unobserved");
    expect(matched).toHaveTextContent("10.20.0.0/16");
    expect(matched).not.toHaveTextContent("10.30.0.0/16");
    expect(unobserved).toHaveTextContent("10.30.0.0/16");
    expect(unobserved).not.toHaveTextContent("10.20.0.0/16");
    // Not rendered as an error: it is a designed condition.
    expect(screen.queryByRole("alert")).toBeNull();
  });

  // -------------------------------------------------------------------------
  // 2. "cannot verify" is not "no mismatches"
  // -------------------------------------------------------------------------

  it("does not say the label back in the value for an unobserved segment", () => {
    // The row read "10.20.9.0/24: declared, not yet observed. No sweep
    // preference here until inventory places this machine on the segment." —
    // the first clause is the `<dt>` directly above it, repeated once per
    // segment. The prefixes are the value; the consequence is said once.
    const check: SegmentCheckOut = {
      site_server_id: "ss-1",
      device_id: "dev-1",
      evidence: "ipam",
      matched: [],
      declared_unobserved: ["10.20.9.0/24", "10.20.11.0/24"],
      observed_undeclared: [],
    };
    render(<SegmentCheck check={check} />);

    const unobserved = screen.getByTestId("declared-unobserved");
    expect(unobserved).toHaveTextContent("10.20.9.0/24");
    expect(unobserved).toHaveTextContent("10.20.11.0/24");
    expect(unobserved).not.toHaveTextContent(/declared, not yet observed/i);
    // Said once for the list, not once per segment.
    expect(screen.getAllByText(/No sweep preference/i)).toHaveLength(1);
  });

  it("renders unavailable evidence as 'cannot verify', not as a clean result", () => {
    const unavailable: SegmentCheckOut = {
      site_server_id: "ss-1",
      device_id: "dev-1",
      evidence: "unavailable",
      matched: [],
      declared_unobserved: [],
      observed_undeclared: [],
    };
    render(<SegmentCheck check={unavailable} />);
    const marker = screen.getByTestId("segment-check-evidence");
    expect(marker).toHaveTextContent(/cannot verify/i);
    // The clean-result copy must NOT appear.
    expect(screen.queryByTestId("declared-unobserved-empty")).toBeNull();
    expect(screen.queryByTestId("observed-undeclared-empty")).toBeNull();
  });

  it("renders a genuinely clean result differently from an unverifiable one", () => {
    // THE DISCRIMINATING PAIR. Both bodies carry three empty lists; only
    // `evidence` differs, and the two must not read the same. Asserting either
    // one alone would pass against a component that ignored `evidence`.
    const clean: SegmentCheckOut = {
      site_server_id: "ss-1",
      device_id: "dev-1",
      evidence: "ipam",
      matched: [],
      declared_unobserved: [],
      observed_undeclared: [],
    };
    const { unmount } = render(<SegmentCheck check={clean} />);
    expect(screen.getByTestId("segment-check-evidence")).not.toHaveTextContent(
      /cannot verify/i,
    );
    expect(screen.getByTestId("observed-undeclared-empty")).toBeInTheDocument();
    unmount();

    render(<SegmentCheck check={{ ...clean, evidence: "unavailable" }} />);
    expect(screen.getByTestId("segment-check-evidence")).toHaveTextContent(
      /cannot verify/i,
    );
    expect(screen.queryByTestId("observed-undeclared-empty")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 3. enabled: false renders distinctly
// ---------------------------------------------------------------------------

describe("SiteServerList", () => {
  it("renders a disabled site server distinctly from a live one", () => {
    // Both rows present in one render, so the test observes a DIFFERENCE rather
    // than the presence of one string.
    const servers = [
      aServer({ id: "live", name: "live-one" }),
      aServer({ id: "off", name: "off-one", enabled: false }),
    ];
    renderWithRouter(<SiteServerList servers={servers} />);

    const live = screen.getByTestId("state-live");
    const off = screen.getByTestId("state-off");
    expect(live.textContent).not.toEqual(off.textContent);
    expect(off).toHaveTextContent(/not serving/i);
    expect(live).toHaveTextContent(/enabled/i);
  });

  it("distinguishes an empty list from a disabled row", () => {
    // "No site servers" and "a site server that is switched off" are different
    // facts; a page that rendered a disabled row as absent would hide a
    // privileged host from the person responsible for it.
    renderWithRouter(<SiteServerList servers={[]} />);
    expect(screen.getByTestId("no-site-servers")).toBeInTheDocument();
    // Without a site server a segment still gets sweeps -- any computer with
    // the agent there runs them (`ipam/sweep.py`); it gets no wakes and no
    // push installs. The old sentence said it got no sweeps.
    expect(screen.getByTestId("no-site-servers")).toHaveTextContent(/no wakes and no push installs/);
    expect(screen.getByTestId("no-site-servers")).not.toHaveTextContent(/no sweeps/);

    renderWithRouter(<SiteServerList servers={[aServer({ id: "off", enabled: false })]} />);
    expect(screen.getByTestId("row-off")).toBeInTheDocument();
  });

  it("names a role in words, never in the wire token", () => {
    // "wake" is the API's word. An operator's word for what that server does
    // is Wake-on-LAN, and the cell was printing the token in mono.
    renderWithRouter(<SiteServerList servers={[aServer({ id: "live" })]} />);
    const roles = screen.getByTestId("roles-live");
    expect(roles).toHaveTextContent("Wake-on-LAN");
    expect(roles.textContent).not.toBe("wake");
  });

  it("renders each declared segment as its own badge", () => {
    renderWithRouter(
      <SiteServerList
        servers={[aServer({ id: "multi", segments: ["10.20.0.0/16", "10.20.7.0/24"] })]}
      />,
    );
    const badges = screen.getAllByTestId("segment-multi");
    expect(badges.map((node) => node.textContent)).toEqual([
      "10.20.0.0/16",
      "10.20.7.0/24",
    ]);
  });

  it("keeps the separator attached to the prefix before it", () => {
    // At 390px the sub-line broke on the space to the LEFT of the bullet and
    // stranded it on a line of its own between two prefixes. The separator is
    // punctuation belonging to the prefix it follows, so the space before it is
    // non-breaking and only the one after it is a break opportunity.
    renderWithRouter(
      <SiteServerList
        servers={[aServer({ id: "multi", segments: ["10.20.4.0/24", "10.20.9.0/24"] })]}
      />,
    );
    const [first] = screen.getAllByTestId("segment-multi");
    const line = first?.parentElement as HTMLElement;
    expect(line.textContent).toBe("10.20.4.0/24\u00a0\u00b7 10.20.9.0/24");
    // The defect itself: an ordinary space on the left of the bullet.
    expect(line.textContent).not.toMatch(/ \u00b7/);
  });
});

// ---------------------------------------------------------------------------
// 4 & 5. The promote dialog
// ---------------------------------------------------------------------------

describe("PromoteDialog", () => {
  it("offers only the wake role", () => {
    // `cache` and `pxe` are refused by the service with a 400 naming §4.4. An
    // option the server rejects is a bug report waiting to be filed.
    render(<PromoteDialog devices={[aDevice()]} onSubmit={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.getByLabelText("Enable Wake-on-LAN")).toBeInTheDocument();
    expect(screen.queryByLabelText("cache")).toBeNull();
    expect(screen.queryByLabelText("pxe")).toBeNull();
  });

  it("shows a device with no agent, disabled, with the reason", () => {
    // SHOWN AND DISABLED, not silently absent: an operator looking for a
    // machine they can see in the device list needs to be told why it is not
    // offerable, or they conclude the page is broken.
    const devices = [
      aDevice({ id: "has-agent", hostname: "has-agent", agent_status: "online" }),
      aDevice({ id: "no-agent", hostname: "no-agent", agent_status: null }),
    ];
    render(<PromoteDialog devices={devices} onSubmit={vi.fn()} onCancel={vi.fn()} />);

    const offerable = screen.getByTestId("device-option-has-agent");
    const blocked = screen.getByTestId("device-option-no-agent");
    // BOTH present -- the pair is what makes "disabled" an observation rather
    // than an artefact of an empty list.
    expect(offerable).not.toBeDisabled();
    expect(blocked).toBeDisabled();
    expect(blocked).toHaveTextContent(/no enrolled agent/i);
  });

  it("marks an already-promoted device as such rather than dropping it", () => {
    const devices = [
      aDevice({ id: "fresh", hostname: "fresh" }),
      aDevice({ id: "promoted", hostname: "promoted" }),
    ];
    render(
      <PromoteDialog
        devices={devices}
        promotedDeviceIds={["promoted"]}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.getByTestId("device-option-fresh")).not.toBeDisabled();
    const already = screen.getByTestId("device-option-promoted");
    expect(already).toBeDisabled();
    expect(already).toHaveTextContent(/already a site server/i);
  });

  // ------------------------------------------------------------------------
  // Auditor A (MEDIUM): "The 'Promote a device' dialog is a different variant
  // — it has a real <form> but its controls are unlabeled and it has no
  // Cancel." Evidence: `[{"lab":null,"type":"select-one",...},
  // {"lab":null,"type":"text",...},{"lab":"wake","type":"checkbox"}]`,
  // `"btns":["(icon)","Promote"]`.
  // ------------------------------------------------------------------------

  it("labels both of its primary controls, which a screen reader had nothing for", () => {
    render(<PromoteDialog devices={[aDevice()]} onSubmit={vi.fn()} onCancel={vi.fn()} />);

    // `lab: null` on both in the audit's probe: an unlabeled combo box and an
    // unlabeled text field in a dialog that promotes a device to
    // infrastructure. `getByLabelText` throws if the association is missing,
    // so deleting either `htmlFor`/`id` pair fails this.
    expect(screen.getByLabelText("Device").tagName).toBe("SELECT");
    expect(screen.getByLabelText("Segments (comma separated CIDRs)").tagName).toBe("INPUT");
  });

  it("says what ticking the role does rather than printing the wire value", () => {
    render(<PromoteDialog devices={[aDevice()]} onSubmit={vi.fn()} onCancel={vi.fn()} />);

    const checkbox = screen.getByLabelText("Enable Wake-on-LAN");
    // The label is a sentence; the POSTed value is still the enum the service
    // validates, so humanising the screen did not change the wire.
    expect(checkbox).toHaveAttribute("value", "wake");
    // The raw lowercase enum is no longer the accessible name of anything.
    expect(screen.queryByLabelText("wake")).toBeNull();
  });

  it("offers a Cancel that backs out instead of promoting the device", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    const onCancel = vi.fn();
    render(<PromoteDialog devices={[aDevice()]} onSubmit={onSubmit} onCancel={onCancel} />);

    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onCancel).toHaveBeenCalledTimes(1);
    // The trap the audit's "no Cancel" invites is a Cancel that submits: an
    // untyped <button> inside a <form> is type="submit" by default, so a
    // Cancel added without `type="button"` would promote the device.
    expect(onSubmit).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// The wake action
// ---------------------------------------------------------------------------

describe("WakeAction", () => {
  // The 202's payload. It describes a SIGNED JOB, not a datagram: `job_id`,
  // the server-computed broadcast, and the five-minute expiry.
  const aWakeResult = {
    job_id: "job-1",
    device_id: "dev-1",
    broadcast: "10.20.255.255",
    target_mac: "a4:bb:6d:11:22:33",
    expires_at: "2026-08-06T12:05:00Z",
  };

  function aWakeJob(overrides: Partial<Job> = {}): Job {
    return {
      id: "job-1",
      type: "site.wake",
      device_id: "dev-1",
      status: "queued",
      attempts: 0,
      params: { broadcast: "10.20.255.255", target_mac: "a4:bb:6d:11:22:33" },
      result: null,
      issued_at: "2026-08-06T12:00:00Z",
      expires_at: "2026-08-06T12:05:00Z",
      started_at: null,
      finished_at: null,
      ...overrides,
    };
  }

  it("does not claim a packet was sent when all it has is a 202", () => {
    // **THE DEFECT.** This rendered "Packet sent to 10.20.255.255" on a response
    // that had only QUEUED a job. `SiteWakeOut` makes no such claim and cannot:
    // at that moment the job is `queued`, and if the site server is offline it
    // stays queued until it reconnects or expires. Same shape as the false
    // caveat fixed in `HealthReportPanel` — a sentence asserting as fact
    // something the data does not support.
    render(<WakeAction server={aServer()} onWake={vi.fn()} result={aWakeResult} />);

    const status = screen.getByTestId("wake-result");
    expect(status).toHaveAttribute("data-wake-state", "queued");
    expect(status).toHaveTextContent(/queued/i);
    expect(status).toHaveTextContent(/nothing has been sent yet/i);
    // The falsified claims, pinned so neither can come back quietly.
    expect(status).not.toHaveTextContent(/packet sent|was sent to/i);
    expect(status).not.toHaveTextContent(/woke|is up|awake/i);
    // The SERVER-COMPUTED broadcast is still shown back, which is how an
    // operator notices they named the wrong wire.
    expect(status).toHaveTextContent("10.20.255.255");
  });

  it("says a packet was sent only once the site server has reported one", () => {
    // The earliest moment "sent" is true: the agent returned a
    // `SiteWakeResult`, `handle_wake_result` checked its `broadcast` against the
    // SIGNED params, and the job succeeded. Still the agent's REPORT, and still
    // not "the machine woke" — §0.9 leaves that to the agent connecting.
    render(
      <WakeAction
        server={aServer()}
        onWake={vi.fn()}
        result={aWakeResult}
        job={aWakeJob({
          status: "succeeded",
          result: {
            sent: true,
            broadcast: "10.20.255.255",
            sent_at: "2026-08-06T12:00:04Z",
          },
        })}
      />,
    );

    const status = screen.getByTestId("wake-result");
    expect(status).toHaveAttribute("data-wake-state", "reported");
    expect(status).toHaveTextContent(/reports a packet sent to 10\.20\.255\.255/i);
    // In the operator's zone, as a date a person reads, not the wire timestamp.
    expect(status).toHaveTextContent(/Aug 2026/);
    expect(status).not.toHaveTextContent("2026-08-06T12:00:04Z");
    expect(status).toHaveTextContent(/not that the machine came up/i);
    expect(status).not.toHaveTextContent(/woke|is up|awake/i);
  });

  it("renders queued, dispatched and reported as three different sentences", () => {
    // A two-state UI ("asked" / "done") puts `queued` and `reported` in the same
    // box, which is the original defect with more steps. Asserted as a tuple of
    // (state attribute, whether the text claims a packet) across all five
    // statuses, so a branch that silently falls through to the optimistic copy
    // is named rather than merely "not raising".
    const cases: Array<[Job | undefined, string]> = [
      [undefined, "queued"],
      [aWakeJob({ status: "queued" }), "queued"],
      [aWakeJob({ status: "dispatched" }), "dispatched"],
      [aWakeJob({ status: "running" }), "dispatched"],
      [aWakeJob({ status: "succeeded" }), "reported"],
      [aWakeJob({ status: "failed" }), "failed"],
      [aWakeJob({ status: "refused" }), "failed"],
      [aWakeJob({ status: "expired" }), "expired"],
    ];

    const observed = cases.map(([job]) => {
      const { unmount } = render(
        <WakeAction server={aServer()} onWake={vi.fn()} result={aWakeResult} job={job} />,
      );
      const status = screen.getByTestId("wake-result");
      const claimsSent = /reports a packet sent/i.test(status.textContent ?? "");
      const state = status.getAttribute("data-wake-state");
      unmount();
      return [state, claimsSent];
    });

    expect(observed).toEqual([
      ["queued", false],
      ["queued", false],
      ["dispatched", false],
      ["dispatched", false],
      ["reported", true],
      ["failed", false],
      ["failed", false],
      ["expired", false],
    ]);
  });

  it("treats an unknown job status as not-yet-sent", () => {
    // `wakeStateOf`'s default arm. An unrecognised status must fall to the
    // CAUTIOUS state: defaulting to "reported" is how the claim this component
    // exists to avoid returns through the fallback branch.
    expect(wakeStateOf(undefined)).toBe("queued");
    expect(wakeStateOf(null)).toBe("queued");
    expect(wakeStateOf({ ...aWakeJob(), status: "invented" as Job["status"] })).toBe(
      "queued",
    );
  });

  it("refuses to offer a wake through a disabled site server", () => {
    render(<WakeAction server={aServer({ enabled: false })} onWake={vi.fn()} />);
    expect(screen.getByTestId("wake-disabled")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /send wake/i })).toBeNull();
  });

  it("requires a segment only when the server declares more than one", () => {
    const { unmount } = render(
      <WakeAction server={aServer({ segments: ["10.20.7.0/24"] })} onWake={vi.fn()} />,
    );
    expect(screen.getByLabelText(/segment \(optional\)/i)).toBeInTheDocument();
    unmount();

    render(
      <WakeAction
        server={aServer({ segments: ["10.20.0.0/16", "10.30.0.0/16"] })}
        onWake={vi.fn()}
      />,
    );
    expect(screen.getByLabelText(/declares several/i)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// The detail pane
// ---------------------------------------------------------------------------

describe("SiteServerDetail", () => {
  it("hides the wake form without site.wake, and the server still gates it", () => {
    // The UI may hide the affordance; INVARIANT #6 says it gates nothing. The
    // server-side 403 is asserted in tests/test_site_server_api.py, and this
    // asserts rendering only.
    const { unmount } = render(
      <SiteServerDetail
        server={aServer()}
        check={undefined}
        canWake={false}
        onWake={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: /send wake/i })).toBeNull();
    unmount();

    render(
      <SiteServerDetail server={aServer()} check={undefined} canWake onWake={vi.fn()} />,
    );
    expect(screen.getByRole("button", { name: /send wake/i })).toBeInTheDocument();
  });

  it("renders the declaration and the evidence together", () => {
    const check: SegmentCheckOut = {
      site_server_id: "ss-1",
      device_id: "dev-1",
      evidence: "ipam",
      matched: [],
      declared_unobserved: ["10.20.0.0/16"],
      observed_undeclared: ["192.168.50.4"],
    };
    render(
      <SiteServerDetail server={aServer()} check={check} canWake={false} onWake={vi.fn()} />,
    );
    expect(screen.getByTestId("detail-segments")).toHaveTextContent("10.20.0.0/16");
    expect(screen.getByTestId("declared-unobserved")).toHaveTextContent("10.20.0.0/16");
    expect(screen.getByTestId("observed-undeclared")).toHaveTextContent("192.168.50.4");
  });
});


// ==========================================================================
// NO DEAD TILES.
//
// `StatCard` requires a `to`, so a MISSING link is a compile error. What the
// compiler cannot see is a link to a path that does not exist, or one whose
// filter the destination page ignores — both render as a perfectly good
// anchor and both waste the click. Hence: the href exists, the path is routed,
// and the filter actually narrows the list.
// ==========================================================================

describe("every site-server tile opens the rows it counted", () => {
  let api: Partial<ApiClient>;

  const TWO: SiteServerOut[] = [
    pageServer({ id: "a", name: "site-a", segments: ["10.20.0.0/16"] }),
    pageServer({ id: "b", name: "site-b", segments: ["10.30.0.0/16"], enabled: false }),
  ];

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(PAGE_ME),
      listSiteServers: vi.fn().mockResolvedValue(TWO),
      listDevices: vi.fn().mockResolvedValue({ items: [], total: 0 } satisfies Page<Device>),
      promoteSiteServer: vi.fn(),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("links every tile at a route the application has", async () => {
    renderPage();
    await screen.findByText("site-a");

    expectLiveTiles(["/site-servers", "/site-servers?state=enabled", "/site-servers"]);
  });

  // THE OTHER HALF OF "NOT DEAD": the destination has to narrow. `site-b` is
  // demoted-but-not-deleted — the distinction this whole list exists to make.
  it("narrows the list to the enabled servers the tile counted", async () => {
    renderPage("/site-servers?state=enabled");

    expect(await screen.findByText("site-a")).toBeInTheDocument();
    expect(screen.queryByText("site-b")).not.toBeInTheDocument();
  });

  it("shows both servers when the filter is absent", async () => {
    renderPage();

    expect(await screen.findByText("site-a")).toBeInTheDocument();
    expect(screen.getByText("site-b")).toBeInTheDocument();
  });

  // "No site servers. Promote a device to add one." told to an operator who
  // has two of them is the same class of lie as a loading table rendering as
  // an empty one: it says the estate is empty when only the filter is.
  it("distinguishes a filter that matched nothing from an empty estate", async () => {
    api.listSiteServers = vi.fn().mockResolvedValue([TWO[0] as SiteServerOut]);
    renderPage("/site-servers?state=disabled");

    expect(await screen.findByText("No site server matches that filter.")).toBeInTheDocument();
    expect(screen.queryByTestId("no-site-servers")).not.toBeInTheDocument();
  });

  it("still says the estate is empty when it genuinely is", async () => {
    api.listSiteServers = vi.fn().mockResolvedValue([]);
    renderPage("/site-servers?state=enabled");

    expect(await screen.findByTestId("no-site-servers")).toBeInTheDocument();
  });

  // ZERO IS A MEASUREMENT, NOT AN ABSENCE: "no server is enabled" and "no
  // server exists" are the same 0 and opposite operational facts.
  it("says WHICH zero a zero enabled count is", async () => {
    api.listSiteServers = vi
      .fn()
      .mockResolvedValue([pageServer({ id: "c", name: "site-c", enabled: false })]);
    renderPage();

    await screen.findByText("site-c");
    expect(
      screen.getByText("Every site server is switched off, so none is serving"),
    ).toBeInTheDocument();
    expect(screen.queryByText("No site server exists to enable")).not.toBeInTheDocument();
  });
});
