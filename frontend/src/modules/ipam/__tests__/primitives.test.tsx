import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../../../lib/api";
import type { ApiClient } from "../../../lib/api";
import { AddressDialog } from "../AddressDialog";
import { DriftPage } from "../DriftPage";
import { RangesPanel } from "../RangesPanel";
import { ScanAuthorizationsPage } from "../ScanAuthorizationsPage";
import { SweepPanel } from "../SweepPanel";
import type { DriftOut, GridEntryOut, ScopeOut, SubnetOut } from "../types";

// The two shared primitives, as they behave INSIDE this module.
//
// `lib/datetime.ts` and `components/form.tsx` have their own unit tests. What
// those cannot show is whether IPAM actually routes through them, and both
// defects they exist to close are invisible from the outside:
//
//   * A DATE FIELD IS A WALL CLOCK. Every assertion below about `toInstant`
//     pins a SPECIFIC INSTANT that differs from what `new Date(value)` would
//     have produced, because the old code's answer was a perfectly well-formed
//     ISO string — just the wrong moment. A test asserting "an ISO string was
//     sent" passes against the bug.
//   * AN EMPTY ERROR RENDERS AS NOTHING. `ApiError` carries the response body
//     as its message, and a 503 from a proxy has no body, so the module's old
//     `describeError` returned "" and every failure paragraph rendered blank.
//     So these assert an ANNOUNCED, NON-EMPTY sentence — `getByRole("alert")`
//     — never merely that the element exists.

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

// UTC-5 in August, and the operator's offset in both audit reports. Chosen so
// that a zone-losing implementation is off by a whole calendar day, not by an
// amount that could be mistaken for rounding.
const BOGOTA = "America/Bogota";

function renderWithProviders(node: React.ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/ipam"]}>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

function stubApi(overrides: Partial<ApiClient>, timezone: string | null = BOGOTA): ApiClient {
  const api = {
    me: vi.fn().mockResolvedValue({
      user_id: "u1",
      org_id: "o1",
      email: "a@b.c",
      display_name: "A",
      timezone,
      permissions: [
        "ipam.read",
        "ipam.assign",
        "ipam.manage",
        "ipam.sweep.run",
        "ipam.sweep.manage",
        "ipam.scope.manage",
      ],
    }),
    listIpamVrfs: vi.fn().mockResolvedValue([]),
    listClientSites: vi.fn().mockResolvedValue([]),
    listIpamVlans: vi.fn().mockResolvedValue([]),
    listIpamSubnets: vi.fn().mockResolvedValue([]),
    listIpamSubnetAddresses: vi.fn().mockResolvedValue([]),
    listIpamRanges: vi.fn().mockResolvedValue([]),
    listIpamDrift: vi.fn().mockResolvedValue([]),
    listIpamOrphans: vi.fn().mockResolvedValue([]),
    listIpamScanAuthorizations: vi.fn().mockResolvedValue([]),
    listIpamSweepRuns: vi.fn().mockResolvedValue([]),
    getIpamAddressHistory: vi.fn().mockResolvedValue([]),
    ...overrides,
  } as unknown as ApiClient;
  mockUseApi.mockReturnValue(api);
  return api;
}

/** The grant form lives in the dialog the head's primary action opens. */
async function openGrantForm(): Promise<void> {
  await userEvent.click(await screen.findByRole("button", { name: "Authorize a subnet" }));
}

function subnet(overrides: Partial<SubnetOut> = {}): SubnetOut {
  return {
    client_id: "o1",
    id: "s1",
    vrf_id: "v1",
    prefix: "10.4.7.0/24",
    kind: "assignable",
    name: "Core",
    description: "",
    site_id: null,
    vlan_id: null,
    gateway: null,
    dns_servers: [],
    parent_id: null,
    sweep_enabled: false,
    sweep_device_id: null,
    sweep_interval_hours: null,
    last_swept_at: null,
    ...overrides,
  };
}

function drift(overrides: Partial<DriftOut> = {}): DriftOut {
  return {
    client_id: "o1",
    id: "d1",
    vrf_id: "v1",
    subnet_id: "s1",
    address: "10.4.7.9",
    drift_class: "documented_absent",
    severity: "medium",
    confidence: "medium",
    detail: { last_seen_days: 30 },
    device_id: null,
    first_detected_at: "2026-07-01T00:00:00.000Z",
    last_evaluated_at: "2026-07-31T00:00:00.000Z",
    consecutive_detections: 3,
    resolved_at: null,
    acknowledged_at: null,
    suppressed_until: null,
    note: "",
    ...overrides,
  };
}

function scope(overrides: Partial<ScopeOut> = {}): ScopeOut {
  return {
    client_id: "o1",
    id: "sc1",
    vrf_id: "v1",
    prefix: "10.4.7.0/24",
    note: "",
    authorized_by: "u1",
    authorized_at: "2026-07-01T00:00:00.000Z",
    expires_at: null,
    revoked_at: null,
    ...overrides,
  };
}

function gridEntry(overrides: Partial<GridEntryOut> = {}): GridEntryOut {
  return {
    address: "10.4.7.9",
    status: "in_use",
    state: "assigned",
    address_id: "a1",
    device_id: "dev1",
    asset_label: "",
    mac: "a4:bb:6d:11:22:33",
    hostname: "WS-01",
    last_seen_at: null,
    allocation_method: "static",
    allocation_method_source: "documented",
    range_kinds: [],
    ...overrides,
  };
}

/** A 503 from a proxy: a status and NO BODY. This is the shape that rendered blank. */
function bodyless503(): ApiError {
  return new ApiError(503, "");
}

// --------------------------------------------------------------------------
// A. Dates in — the five-hour (here, whole-day) bug
// --------------------------------------------------------------------------

describe("a date field is a wall clock, and gets the operator's zone attached", () => {
  it("suppresses drift until midnight in the operator's zone, not midnight UTC", async () => {
    const suppressIpamDrift = vi.fn().mockResolvedValue(drift());
    stubApi({ listIpamDrift: vi.fn().mockResolvedValue([drift()]), suppressIpamDrift });
    renderWithProviders(<DriftPage />);

    await userEvent.click(await screen.findByRole("button", { name: "Handle" }));
    await userEvent.type(screen.getByLabelText("Note"), "vendor confirmed");
    await userEvent.type(screen.getByLabelText("Suppress until"), "2026-08-25");
    await userEvent.click(screen.getByRole("button", { name: "Suppress" }));

    await waitFor(() => expect(suppressIpamDrift).toHaveBeenCalled());
    // 00:00 on the 25th in Bogota is 05:00Z on the 25th. The old code sent
    // 2026-08-25T00:00:00.000Z — which is 19:00 on the 24th where the operator
    // sits, so the suppression lapsed before the day they chose began.
    expect(suppressIpamDrift).toHaveBeenCalledWith(
      "d1",
      "2026-08-25T05:00:00.000Z",
      "vendor confirmed",
    );
  });

  it("expires a scan authorization at midnight in the operator's zone", async () => {
    const createIpamScanAuthorization = vi.fn().mockResolvedValue(scope());
    stubApi({ createIpamScanAuthorization, listIpamSubnets: vi.fn().mockResolvedValue([subnet()]) });
    renderWithProviders(<ScanAuthorizationsPage />);
    await openGrantForm();

    await userEvent.type(await screen.findByLabelText("Prefix"), "10.4.7.0/24");
    await userEvent.type(screen.getByLabelText("Note (why)"), "pen test window");
    await userEvent.type(screen.getByLabelText("Expires (optional)"), "2026-09-01");
    await userEvent.click(screen.getByRole("button", { name: "Authorize" }));

    await waitFor(() => expect(createIpamScanAuthorization).toHaveBeenCalled());
    expect(createIpamScanAuthorization).toHaveBeenCalledWith(
      expect.objectContaining({ expires_at: "2026-09-01T05:00:00.000Z" }),
    );
  });

  it("still sends null — never the epoch, never an empty instant — for no expiry", async () => {
    const createIpamScanAuthorization = vi.fn().mockResolvedValue(scope());
    stubApi({ createIpamScanAuthorization, listIpamSubnets: vi.fn().mockResolvedValue([subnet()]) });
    renderWithProviders(<ScanAuthorizationsPage />);
    await openGrantForm();

    await userEvent.type(await screen.findByLabelText("Prefix"), "10.4.7.0/24");
    await userEvent.click(screen.getByRole("button", { name: "Authorize" }));

    await waitFor(() => expect(createIpamScanAuthorization).toHaveBeenCalled());
    expect(createIpamScanAuthorization).toHaveBeenCalledWith(
      expect.objectContaining({ expires_at: null }),
    );
  });

  it("reads the zone from the PROFILE, so two operators do not get one answer", async () => {
    // The Bogota cases above cannot tell "read `me.timezone`" apart from
    // "hardcoded UTC-5". Tokyo is UTC+9, on the other side of both UTC and the
    // date line from Bogota, so only an implementation that actually consults
    // the profile lands here.
    const createIpamScanAuthorization = vi.fn().mockResolvedValue(scope());
    stubApi(
      { createIpamScanAuthorization, listIpamSubnets: vi.fn().mockResolvedValue([subnet()]) },
      "Asia/Tokyo",
    );
    renderWithProviders(<ScanAuthorizationsPage />);
    await openGrantForm();

    await userEvent.type(await screen.findByLabelText("Prefix"), "10.4.7.0/24");
    await userEvent.type(screen.getByLabelText("Expires (optional)"), "2026-09-01");
    await userEvent.click(screen.getByRole("button", { name: "Authorize" }));

    await waitFor(() => expect(createIpamScanAuthorization).toHaveBeenCalled());
    // Midnight on 1 September in Tokyo is 15:00Z on 31 AUGUST — a different
    // calendar day from the one an unqualified `new Date("2026-09-01")` records.
    expect(createIpamScanAuthorization).toHaveBeenCalledWith(
      expect.objectContaining({ expires_at: "2026-08-31T15:00:00.000Z" }),
    );
  });

  it("produces a real instant, not an empty string, when the profile has no timezone", async () => {
    // `timezone` is nullable on /me. `toInstant` resolves the browser zone for a
    // null — that branch is `lib/datetime.test.ts`'s subject, and this runner is
    // pinned to UTC so it cannot be distinguished here. What IS this module's
    // business is that a null does not become "" (which the API would 422) or
    // the epoch.
    const createIpamScanAuthorization = vi.fn().mockResolvedValue(scope());
    stubApi(
      { createIpamScanAuthorization, listIpamSubnets: vi.fn().mockResolvedValue([subnet()]) },
      null,
    );
    renderWithProviders(<ScanAuthorizationsPage />);
    await openGrantForm();

    await userEvent.type(await screen.findByLabelText("Prefix"), "10.4.7.0/24");
    await userEvent.type(screen.getByLabelText("Expires (optional)"), "2026-09-01");
    await userEvent.click(screen.getByRole("button", { name: "Authorize" }));

    await waitFor(() => expect(createIpamScanAuthorization).toHaveBeenCalled());
    const sent = createIpamScanAuthorization.mock.calls[0]?.[0] as { expires_at: string };
    expect(sent.expires_at).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/);
    expect(new Date(sent.expires_at).getFullYear()).toBe(2026);
  });
});

// --------------------------------------------------------------------------
// B. Failures out — the BLOCKER: a 503 that said nothing
// --------------------------------------------------------------------------

describe("a failure the user must be told about is announced, and is a sentence", () => {
  it("names a bodyless 503 when REVOKING a scan authorization", async () => {
    // Revocation is the security-critical direction: an operator who believes a
    // grant is gone stops watching a prefix that is still sweepable.
    stubApi({
      listIpamScanAuthorizations: vi.fn().mockResolvedValue([scope()]),
      revokeIpamScanAuthorization: vi.fn().mockRejectedValue(bodyless503()),
    });
    renderWithProviders(<ScanAuthorizationsPage />);

    await userEvent.click(await screen.findByRole("button", { name: /^More actions for / }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Revoke" }));
    // Revoking asks first.
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Revoke" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/unavailable right now/i);
    expect(alert).toHaveTextContent(/Nothing was changed/i);
  });

  it("names a bodyless 503 when RELEASING an address", async () => {
    stubApi({ releaseIpamAddress: vi.fn().mockRejectedValue(bodyless503()) });
    const onClose = vi.fn();
    renderWithProviders(
      <AddressDialog
        entry={gridEntry()}
        subnetPrefix="10.4.7.0/24"
        canAssign
        onClose={onClose}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Release" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/unavailable right now/i);
    // And the dialog must not behave as though the release happened.
    expect(onClose).not.toHaveBeenCalled();
  });

  it("translates a Pydantic 422 into fields and messages rather than printing the payload", async () => {
    stubApi({
      listIpamScanAuthorizations: vi.fn().mockResolvedValue([]),
      createIpamScanAuthorization: vi.fn().mockRejectedValue(
        new ApiError(
          422,
          JSON.stringify({
            detail: [
              { type: "value_error", loc: ["body", "prefix"], msg: "not a valid CIDR", input: "10.4.7.0/24" },
              { type: "value_error", loc: ["body", "note"], msg: "field required", input: "" },
            ],
          }),
        ),
      ),
    });
    renderWithProviders(<ScanAuthorizationsPage />);
    await openGrantForm();

    await userEvent.type(await screen.findByLabelText("Prefix"), "10.4.7.0/24");
    await userEvent.click(screen.getByRole("button", { name: "Authorize" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Prefix: not a valid CIDR");
    // EVERY problem, so a two-field form is not two round trips.
    expect(alert).toHaveTextContent("Note: field required");
    // And none of the machine parts, including the echoed `input` — a
    // validation echo is credential disclosure through a third door.
    expect(alert.textContent).not.toContain("value_error");
    expect(alert.textContent).not.toContain("detail");
  });

  it("names a bodyless 503 when DISPATCHING a sweep", async () => {
    stubApi({
      listIpamScanAuthorizations: vi.fn().mockResolvedValue([scope()]),
      dispatchIpamSweep: vi.fn().mockRejectedValue(bodyless503()),
    });
    renderWithProviders(<SweepPanel subnet={subnet()} canRun canManage={false} />);

    await userEvent.click(await screen.findByRole("button", { name: "Sweep now" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/unavailable right now/i);
  });

  it("believes a server that explained itself in plain text", async () => {
    // The other half of the rule: `describeSubmitError` must not flatten a real
    // sentence into the generic one, or the module trades a blank for a shrug.
    stubApi({
      listIpamScanAuthorizations: vi.fn().mockResolvedValue([scope()]),
      revokeIpamScanAuthorization: vi
        .fn()
        .mockRejectedValue(new ApiError(409, "that grant was already revoked")),
    });
    renderWithProviders(<ScanAuthorizationsPage />);

    await userEvent.click(await screen.findByRole("button", { name: /^More actions for / }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Revoke" }));
    // Revoking asks first.
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Revoke" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("that grant was already revoked");
  });
});

describe("destructive network actions ask first", () => {
  it("does not revoke a scan authorization until the operator confirms", async () => {
    const revoke = vi.fn().mockResolvedValue(undefined);
    stubApi({
      listIpamScanAuthorizations: vi.fn().mockResolvedValue([scope()]),
      revokeIpamScanAuthorization: revoke,
    });
    renderWithProviders(<ScanAuthorizationsPage />);

    await userEvent.click(await screen.findByRole("button", { name: /^More actions for / }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Revoke" }));
    const dialog = await screen.findByRole("dialog");
    expect(revoke).not.toHaveBeenCalled();
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(revoke).not.toHaveBeenCalled();
  });

  it("does not delete a range until the operator confirms", async () => {
    const remove = vi.fn().mockResolvedValue(undefined);
    stubApi({
      listIpamRanges: vi.fn().mockResolvedValue([
        { id: "r1", subnet_id: "s1", kind: "dhcp_pool", start: "10.0.0.100", end: "10.0.0.200", name: "Pool" },
      ]),
      deleteIpamRange: remove,
    });
    renderWithProviders(<RangesPanel subnetId="s1" canManage />);

    await userEvent.click(await screen.findByRole("button", { name: /^More actions for / }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog");
    expect(remove).not.toHaveBeenCalled();
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete range" }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith("s1", "r1"));
  });
});

