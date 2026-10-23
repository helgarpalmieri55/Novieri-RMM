import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../../lib/api";
import type { ApiClient, Me } from "../../../lib/api";
import { NO_DATE } from "../../../lib/datetime";
import { DeviceProvisioningTab } from "../DeviceProvisioningTab";
import { ImagesPage } from "../ImagesPage";
import { ProfilesPage } from "../ProfilesPage";
import { ProvisioningRunsPage } from "../ProvisioningRunsPage";
import { provisionRunStateVariant } from "../provisioningState";
import {
  IMAGE_PICKER_EMPTY_HINT,
  PROFILE_PICKER_EMPTY_HINT,
  imagePickerOptions,
  nameForOption,
  profilePickerOptions,
} from "../pickerOptions";
import type {
  FleetProvisioningReport,
  OsImage,
  ProvisionProfile,
  ProvisionRun,
} from "../types";

// The auth-backed API client is mocked so this is a pure component test: no
// real fetch. Provisioning data is plain TanStack Query state (no live-event
// stream) — mirrors modules/backup/__tests__/backup.test.tsx.
const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function renderWithProviders(ui: ReactElement, entry = "/provisioning") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

/**
 * THE ROUTE TABLE, READ FROM THE APPLICATION ITSELF.
 *
 * A tile linking to `/provisioning/image` (no `s`) is exactly as dead as one
 * linking nowhere, and it type-checks, renders, and satisfies any assertion
 * that only asks whether the href is non-empty. `App.tsx`'s own `path=` list
 * is the only thing that can tell the difference — the source-text technique
 * `App.routing.test.tsx` uses, for the same reason it uses it.
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

/**
 * Choose an option from an `EntityPicker`, the way a person does: focus the
 * combobox, then click the row. Deliberately NOT `user.type(...)` into the
 * field — typing only FILTERS the list; nothing is chosen until an option is
 * clicked, and a test that typed and asserted a value would be asserting a
 * behaviour the picker does not have.
 */
async function pickEntity(
  user: ReturnType<typeof userEvent.setup>,
  pickerLabel: string,
  optionName: RegExp,
) {
  await user.click(screen.getByLabelText(pickerLabel));
  await user.click(await screen.findByRole("option", { name: optionName }));
}

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "operator@example.com",
  display_name: "Operator",
  permissions: ["provisioning.read", "provisioning.run", "provisioning.manage"],
};

const RUNS: ProvisionRun[] = [
  {
    id: "run1",
    profile_id: "profile1",
    device_id: null,
    target_ref: "AA:BB:CC:DD:EE:FF",
    state: "awaiting_enrollment",
    answer_ref: null,
    started_at: "2026-01-02T00:00:00.000Z",
    finished_at: null,
    error: null,
  },
];

const PROFILES: ProvisionProfile[] = [
  {
    id: "profile1",
    name: "standard-workstation",
    image_id: "image1",
    post_config: {},
    target_scope: {},
    enabled: true,
    created_at: "2026-01-01T00:00:00.000Z",
    has_domain_join_credential: true,
    has_local_admin_credential: false,
  },
];

const IMAGE: OsImage = {
  id: "image1",
  os_family: "windows",
  name: "Windows 11 Standard",
  kind: "install",
  artifact_ref: "s3://images/win11",
  artifact_sha256: "deadbeef",
  params: {},
  state: "active",
  created_at: "2026-01-01T00:00:00.000Z",
};

// A fleet that has provisioned nothing: every state count zero, so
// `success_rate` is a fraction over no runs at all.
const EMPTY_REPORT: FleetProvisioningReport = {
  run_counts_by_state: {},
  success_rate: 0,
  machines_provisioned: 0,
  per_profile: {},
};

// A fleet that HAS run builds, half of which failed — a real 50%.
const HALF_FAILED_REPORT: FleetProvisioningReport = {
  run_counts_by_state: { complete: 1, failed: 1 },
  success_rate: 0.5,
  machines_provisioned: 1,
  per_profile: {},
};

// A profile with something to LOSE: a domain-join configuration, and
// `enabled: false` set deliberately by an operator.
const CONFIGURED_PROFILE: ProvisionProfile = {
  ...PROFILES[0]!,
  post_config: { domain_join: { domain: "corp.local", username: "svc-join" } },
  enabled: false,
};

describe("ProvisioningRunsPage", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listProvisionRuns: vi.fn().mockResolvedValue(RUNS),
      listProvisionProfiles: vi.fn().mockResolvedValue(PROFILES),
      listOsImages: vi.fn().mockResolvedValue([IMAGE]),
      getProvisioningReport: vi.fn().mockResolvedValue(EMPTY_REPORT),
      startProvisionRun: vi.fn().mockResolvedValue({ run_id: "run2", state: "pending" }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  // The reading half of cycle 6: the Profile column was `run.profile_id`, a
  // raw UUID in the column an operator scans to find their build.
  it("names each run's profile rather than printing its UUID", async () => {
    renderWithProviders(<ProvisioningRunsPage />);

    expect(await screen.findByText("standard-workstation")).toBeInTheDocument();
    expect(screen.queryByText("profile1")).not.toBeInTheDocument();
  });

  // Auditor A (MEDIUM), on the identical cards: "'0%' is a computed rate over
  // zero runs — mathematically undefined, and displayed identically to a
  // genuine 0% success rate, which would be a catastrophic condition."
  it("says 'Never run' rather than 0% when no run has ever executed", async () => {
    renderWithProviders(<ProvisioningRunsPage />);

    expect(await screen.findByText("Never run")).toBeInTheDocument();
    expect(screen.queryByText("0%")).not.toBeInTheDocument();
  });

  it("shows a real percentage once runs exist", async () => {
    api.getProvisioningReport = vi.fn().mockResolvedValue(HALF_FAILED_REPORT);
    renderWithProviders(<ProvisioningRunsPage />);

    expect(await screen.findByText("50%")).toBeInTheDocument();
    expect(screen.queryByText("Never run")).not.toBeInTheDocument();
  });

  it("lists runs from the mocked api", async () => {
    renderWithProviders(<ProvisioningRunsPage />);

    expect(await screen.findByText("run1")).toBeInTheDocument();
    // WAS `getByText("awaiting_enrollment")` -- an assertion on the defect.
    // Raw enum values reaching the screen is a finding in both audits; the
    // badge now reads the humanised form by default (UX cycle 11).
    //
    // SCOPED TO THE ROW, because the state filter's <option> list speaks the
    // same humanised vocabulary on purpose (the tile links to
    // `?state=failed`, and a select that said `awaiting_enrollment` while the
    // badge said "Awaiting enrollment" would be two vocabularies for one
    // thing). An unscoped `getByText` now matches the option as well as the
    // badge -- and the badge in the run's own row is the stronger assertion
    // anyway.
    const row = screen.getByText("run1").closest("tr");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText("Awaiting enrollment")).toBeInTheDocument();
  });

  it("hides the Provision control when the caller lacks provisioning.run", async () => {
    api.me = vi.fn().mockResolvedValue({
      ...ME,
      permissions: ME.permissions.filter((p) => p !== "provisioning.run"),
    });
    renderWithProviders(<ProvisioningRunsPage />);

    await screen.findByText("run1");
    expect(screen.queryByRole("button", { name: "Provision" })).not.toBeInTheDocument();
  });

  // THE THREE STATES A MOCK NEVER HAS — mirrors DevicesPage.test.tsx.
  it("says it is loading rather than showing an empty table", async () => {
    api.listProvisionRuns = vi.fn(() => new Promise<never>(() => {}));
    renderWithProviders(<ProvisioningRunsPage />);
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the request FAILED rather than showing an empty table", async () => {
    api.listProvisionRuns = vi.fn().mockRejectedValue(new Error("boom"));
    renderWithProviders(<ProvisioningRunsPage />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText(/No provisioning runs/i)).not.toBeInTheDocument();
  });

  it("says there are no runs when there genuinely are none", async () => {
    api.listProvisionRuns = vi.fn().mockResolvedValue([]);
    renderWithProviders(<ProvisioningRunsPage />);
    expect(await screen.findByText("No provisioning runs yet.")).toBeInTheDocument();
  });
});

describe("ProfilesPage", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listProvisionProfiles: vi.fn().mockResolvedValue(PROFILES),
      listOsImages: vi.fn().mockResolvedValue([]),
      createProvisionProfile: vi.fn(),
      updateProvisionProfile: vi.fn(),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("hides the New profile control when the caller lacks provisioning.manage", async () => {
    api.me = vi.fn().mockResolvedValue({
      ...ME,
      permissions: ME.permissions.filter((p) => p !== "provisioning.manage"),
    });
    renderWithProviders(<ProfilesPage />);

    await screen.findByText("standard-workstation");
    expect(screen.queryByRole("button", { name: "New profile" })).not.toBeInTheDocument();
  });

  it("shows a credential-set indicator for a profile with has_domain_join_credential, never a secret value", async () => {
    renderWithProviders(<ProfilesPage />);

    await screen.findByText("standard-workstation");
    expect(screen.getByText(/credential set/i)).toBeInTheDocument();
    // No secret value, sealed ref, token, or password field ever appears in
    // the list view — the editor dialog (with its write-only password
    // inputs) is closed, and even the closed page has no password element.
    expect(screen.queryByText(/__sealed__/)).not.toBeInTheDocument();
    expect(document.querySelector('input[type="password"]')).not.toBeInTheDocument();
  });

  it("says it is loading rather than showing an empty table", async () => {
    api.listProvisionProfiles = vi.fn(() => new Promise<never>(() => {}));
    renderWithProviders(<ProfilesPage />);
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the request FAILED rather than showing an empty table", async () => {
    api.listProvisionProfiles = vi.fn().mockRejectedValue(new Error("boom"));
    renderWithProviders(<ProfilesPage />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  it("says there are no profiles when there genuinely are none", async () => {
    api.listProvisionProfiles = vi.fn().mockResolvedValue([]);
    renderWithProviders(<ProfilesPage />);
    expect(await screen.findByText("No provisioning profiles configured.")).toBeInTheDocument();
  });
});

describe("ImagesPage", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listOsImages: vi.fn().mockResolvedValue([
        {
          id: "img1",
          os_family: "windows",
          name: "Windows 11 Standard",
          kind: "install",
          artifact_ref: "s3://images/win11",
          artifact_sha256: "deadbeef",
          params: {},
          state: "active",
          created_at: "2026-01-01T00:00:00.000Z",
        },
      ]),
      createOsImage: vi.fn(),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("lists images from the mocked api", async () => {
    renderWithProviders(<ImagesPage />);
    expect(await screen.findByText("Windows 11 Standard")).toBeInTheDocument();
  });

  // REVIEW WAVE, round 1, /provisioning/images: `kind` reached the screen as
  // the stored token, underscore and all ("answer_file"), in a column whose
  // neighbours are prose -- and Platform wore the info-circle severity pill
  // that the deploy catalog wore, for a value that is not a severity.
  it("humanises the image kind and draws the platform as a platform", async () => {
    api.listOsImages = vi.fn().mockResolvedValue([
      {
        id: "img2",
        os_family: "windows",
        name: "Windows 11 24H2 answer file",
        kind: "answer_file",
        artifact_ref: null,
        artifact_sha256: null,
        params: {},
        state: "active",
        created_at: "2026-01-01T00:00:00.000Z",
      },
    ]);
    renderWithProviders(<ImagesPage />);

    await screen.findByText("Windows 11 24H2 answer file");
    const row = screen.getByText("Windows 11 24H2 answer file").closest("tr") as HTMLElement;
    expect(within(row).getByText("Answer file")).toBeInTheDocument();
    expect(within(row).queryByText("answer_file")).not.toBeInTheDocument();
    expect(within(row).getByText("Windows")).toBeInTheDocument();
    expect(within(row).getByRole("img", { name: "Windows" })).toBeInTheDocument();
  });

  it("hides the New image control when the caller lacks provisioning.manage", async () => {
    api.me = vi.fn().mockResolvedValue({
      ...ME,
      permissions: ME.permissions.filter((p) => p !== "provisioning.manage"),
    });
    renderWithProviders(<ImagesPage />);
    await screen.findByText("Windows 11 Standard");
    expect(screen.queryByRole("button", { name: "New image" })).not.toBeInTheDocument();
  });

  it("says it is loading rather than showing an empty table", async () => {
    api.listOsImages = vi.fn(() => new Promise<never>(() => {}));
    renderWithProviders(<ImagesPage />);
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the request FAILED rather than showing an empty table", async () => {
    api.listOsImages = vi.fn().mockRejectedValue(new Error("boom"));
    renderWithProviders(<ImagesPage />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  it("says there are no images when there genuinely are none", async () => {
    api.listOsImages = vi.fn().mockResolvedValue([]);
    renderWithProviders(<ImagesPage />);
    expect(await screen.findByText("No OS images registered.")).toBeInTheDocument();
  });
});

describe("provisioning state vocabulary", () => {
  it("maps a real backend provision_run state to a non-fallback badge variant", () => {
    // provision_run.state vocabulary is sourced from the backend
    // (reporting.py:52 RUN_STATES) — "enrolled" is a real, non-terminal state.
    expect(provisionRunStateVariant("enrolled")).not.toBe("muted");
  });

  it("falls back to muted for an unknown/invented state name", () => {
    // Guards against the Phase-5 regression: no invented state name may
    // resolve to a real variant.
    expect(provisionRunStateVariant("not_a_real_state")).toBe("muted");
  });
});

// --------------------------------------------------------------------------
// The "Provision a device" modal — the form kernel
// --------------------------------------------------------------------------

describe("the Provision a device modal", () => {
  // Auditor A (MEDIUM): "'Provision a device' is the same shared modal —
  // fourth confirmation": `{"form":false,...}`, no `required` on either
  // control, and a Start disabled until a profile was picked that never said
  // so. Each test asserts what that evidence lacked — a submitted form, and a
  // click that produces a sentence rather than nothing.
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listProvisionRuns: vi.fn().mockResolvedValue(RUNS),
      listProvisionProfiles: vi.fn().mockResolvedValue(PROFILES),
      listOsImages: vi.fn().mockResolvedValue([IMAGE]),
      getProvisioningReport: vi.fn().mockResolvedValue(EMPTY_REPORT),
      startProvisionRun: vi.fn().mockResolvedValue({ run_id: "run2", state: "pending" }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  async function openDialog() {
    const user = userEvent.setup();
    renderWithProviders(<ProvisioningRunsPage />);
    await user.click(await screen.findByRole("button", { name: "Provision" }));
    return user;
  }

  /** Get past the cycle-8 confirmation, which every start now goes through. */
  async function confirmStart(user: ReturnType<typeof userEvent.setup>) {
    await user.click(await screen.findByRole("button", { name: "Start build" }));
  }

  it("names the unpicked Profile on submit rather than leaving Start disabled", async () => {
    const user = await openDialog();
    const start = screen.getByRole("button", { name: "Start" });

    expect(start).not.toBeDisabled();
    await user.click(start);

    expect(await screen.findByRole("alert")).toHaveTextContent("Profile is required");
    expect(api.startProvisionRun).not.toHaveBeenCalled();
  });

  it("starts the run on Enter, which the modal could not do before it was a form", async () => {
    const user = await openDialog();

    await pickEntity(user, "Profile", /standard-workstation/);
    // The implicit-submit Enter fires the confirmation dialog's mount as a
    // side effect of user-event's keypress plugin rather than as its own
    // awaited interaction, so without this explicit `act` the dialog's
    // focus-stealing effect runs in the same tick and leaves React's act
    // queue unflushed -- the same shape as
    // settings/__tests__/organization.test.tsx's rename-on-Enter case.
    await act(async () => {
      await user.type(screen.getByRole("textbox"), "AA:BB:CC:00:11:22{Enter}");
    });
    await confirmStart(user);

    await waitFor(() =>
      expect(api.startProvisionRun).toHaveBeenCalledWith("profile1", {
        target_ref: "AA:BB:CC:00:11:22",
      }),
    );
  });

  // CYCLE 6. Auditor A's BLOCKER evidence was a `<select>`:
  // `{"lab":"Profile","type":"select-one","opts":["Select a profile…"]}`.
  it("offers the Profile as a searchable list, not a select, and distinguishes options", async () => {
    const user = await openDialog();

    const field = screen.getByLabelText("Profile");
    expect(field.tagName).toBe("INPUT");
    expect(field).toHaveAttribute("role", "combobox");

    await user.click(field);
    // The sublabel is what tells two profiles apart: the image it lays down
    // and whether the profile is still in service.
    expect(
      await screen.findByRole("option", {
        name: /standard-workstation\s*Windows 11 Standard · enabled/,
      }),
    ).toBeInTheDocument();
  });

  it("says what would fill the Profile list, and links to where, when nothing exists", async () => {
    // Auditor A's BLOCKER: "With no profile selectable, the 'Start' action
    // cannot be completed ... Nothing on the page or in the dialog explains
    // that profiles must be created first, and there is no link to the
    // 'Profiles' tab that sits just above."
    api.listProvisionProfiles = vi.fn().mockResolvedValue([]);
    const user = await openDialog();

    expect(await screen.findByText(/No provisioning profiles exist yet/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start" })).toBeDisabled();

    // The link is the half of the fix the disabled reason cannot carry.
    expect(screen.getByRole("link", { name: /Create a provisioning profile/ })).toHaveAttribute(
      "href",
      "/provisioning/profiles",
    );

    // ...and the hint is IN the list, where somebody who opened it is looking.
    await user.click(screen.getByLabelText("Profile"));
    expect(await screen.findByText(/No provisioning profiles exist\./)).toBeInTheDocument();
  });

  // CYCLE 8. A run reimages the endpoint and there is no cancel route in
  // `backend/app/modules/provisioning/api.py`, so the click before it is the
  // last place anybody can stop it.
  it("asks before starting a run, naming the profile, and does not start on Cancel", async () => {
    const user = await openDialog();
    await pickEntity(user, "Profile", /standard-workstation/);
    await user.click(screen.getByRole("button", { name: "Start" }));

    const confirm = await screen.findByRole("dialog", { name: "Start provisioning run" });
    expect(within(confirm).getByText("standard-workstation")).toBeInTheDocument();
    expect(within(confirm).getByText(/replaces the endpoint's disk contents/)).toBeInTheDocument();
    expect(api.startProvisionRun).not.toHaveBeenCalled();

    await user.click(within(confirm).getByRole("button", { name: "Cancel" }));
    expect(api.startProvisionRun).not.toHaveBeenCalled();
  });

  it("starts the run once the confirmation is accepted", async () => {
    const user = await openDialog();
    await pickEntity(user, "Profile", /standard-workstation/);
    await user.click(screen.getByRole("button", { name: "Start" }));
    await confirmStart(user);

    await waitFor(() =>
      expect(api.startProvisionRun).toHaveBeenCalledWith("profile1", { target_ref: null }),
    );
  });

  it("renders a bodyless 503 as a sentence instead of swallowing it", async () => {
    // `new ApiError(503, "")` stringified to "ApiError: " through the old
    // `String(start.error)`, so a failed start read as an empty red line.
    api.startProvisionRun = vi.fn().mockRejectedValue(new ApiError(503, ""));
    const user = await openDialog();

    await pickEntity(user, "Profile", /standard-workstation/);
    await user.click(screen.getByRole("button", { name: "Start" }));
    await confirmStart(user);

    expect(await screen.findByRole("alert")).toHaveTextContent(/unavailable right now/);
  });

  it("does not carry the previous attempt's failure into the next open dialog", async () => {
    // The dialog is never unmounted, so without a reset the 503 above is still
    // on screen when it reopens — a run that was never started, reported as
    // failed.
    api.startProvisionRun = vi.fn().mockRejectedValue(new ApiError(503, ""));
    const user = await openDialog();
    await pickEntity(user, "Profile", /standard-workstation/);
    await user.click(screen.getByRole("button", { name: "Start" }));
    await confirmStart(user);
    await screen.findByRole("alert");

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.click(screen.getByRole("button", { name: "Provision" }));

    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  });
});

// --------------------------------------------------------------------------
// Cycle 5 (lib/datetime) and cycle 4 (describeSubmitError) in this module
// --------------------------------------------------------------------------

// The operator's profile zone, and deliberately NOT the zone this suite runs
// in (vite.config.ts pins no TZ; the runner resolves to UTC). Every assertion
// below is written so that the browser-zone rendering and the profile-zone
// rendering are DIFFERENT STRINGS — otherwise the test passes whether or not
// `me.timezone` is ever read, which is the shape of test this project has
// been bitten by before.
const ME_IN_NEW_YORK: Me = { ...ME, timezone: "America/New_York" };

describe("provisioning renders every date in the profile's zone, in one format", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_IN_NEW_YORK),
      listProvisionRuns: vi.fn().mockResolvedValue(RUNS),
      listProvisionProfiles: vi.fn().mockResolvedValue(PROFILES),
      listOsImages: vi.fn().mockResolvedValue([
        {
          id: "img1",
          os_family: "windows",
          name: "Windows 11 Standard",
          kind: "install",
          artifact_ref: "s3://images/win11",
          artifact_sha256: "deadbeef",
          params: {},
          state: "active",
          // Midnight UTC — five hours EARLIER in New York, which puts it on
          // the PREVIOUS DAY. A date shown in the wrong zone is off by a
          // whole day here, not by five hours, and that is harder to notice.
          created_at: "2026-01-01T00:00:00.000Z",
        },
      ]),
      getDeviceProvisioning: vi.fn().mockResolvedValue({
        device_id: "dev1",
        profile_id: "profile1",
        image_id: "img1",
        enrolled_at: "2026-01-02T00:00:00.000Z",
        latest_run_state: "complete",
      }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("renders a run's started_at in the profile zone, not the browser's", async () => {
    renderWithProviders(<ProvisioningRunsPage />);

    // 2026-01-02T00:00Z is 19:00 on 01 Jan in America/New_York.
    expect(await screen.findByText("01 Jan 2026, 19:00")).toBeInTheDocument();
    expect(screen.queryByText("02 Jan 2026, 00:00")).not.toBeInTheDocument();
  });

  it("renders NO_DATE, never Invalid Date, for a run that has not finished", async () => {
    renderWithProviders(<ProvisioningRunsPage />);

    await screen.findByText("run1");
    // RUNS[0].finished_at is null. The old `new Date(null)` guard was a
    // ternary at the call site; the helper owns it now.
    expect(screen.queryByText(/Invalid Date/)).not.toBeInTheDocument();
    // The Finished column specifically (Target with the id beneath it,
    // Profile, State, Started, Finished, Error), so this cannot pass on some
    // other empty cell.
    const row = screen.getByText("run1").closest("tr") as HTMLElement;
    expect(within(row).getAllByRole("cell")[4]).toHaveTextContent(NO_DATE);
  });

  it("renders an image's created_at in the profile zone, on the profile's day", async () => {
    renderWithProviders(<ImagesPage />);

    await screen.findByText("Windows 11 Standard");
    expect(screen.getByText("31 Dec 2025, 19:00")).toBeInTheDocument();
    expect(screen.queryByText("01 Jan 2026, 00:00")).not.toBeInTheDocument();
  });

  it("renders a device's enrolled_at in the profile zone", async () => {
    renderWithProviders(<DeviceProvisioningTab deviceId="dev1" />);

    expect(await screen.findByText("01 Jan 2026, 19:00")).toBeInTheDocument();
  });

  it("renders NO_DATE for a device that has never enrolled", async () => {
    api.getDeviceProvisioning = vi.fn().mockResolvedValue({
      device_id: "dev1",
      profile_id: null,
      image_id: null,
      enrolled_at: null,
      latest_run_state: null,
    });
    renderWithProviders(<DeviceProvisioningTab deviceId="dev1" />);

    await waitFor(() => expect(api.getDeviceProvisioning).toHaveBeenCalled());
    expect(screen.queryByText(/Invalid Date/)).not.toBeInTheDocument();
    const enrolled = screen.getByText("Enrolled").closest("div") as HTMLElement;
    expect(within(enrolled).getByText(NO_DATE)).toBeInTheDocument();
  });
});

describe("provisioning failure paths say what went wrong", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listOsImages: vi.fn().mockResolvedValue([]),
      listProvisionProfiles: vi.fn().mockResolvedValue(PROFILES),
      createOsImage: vi.fn(),
      updateProvisionProfile: vi.fn(),
      createProvisionProfile: vi.fn(),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("announces a bodyless 503 from New image instead of rendering an empty red line", async () => {
    // `ApiError` is built as `new ApiError(status, await response.text())`, so
    // a proxy's bodyless 503 carries `message === ""` and the old
    // `String(create.error)` rendered the literal "ApiError: ".
    api.createOsImage = vi.fn().mockRejectedValue(new ApiError(503, ""));
    const user = userEvent.setup();
    renderWithProviders(<ImagesPage />);

    await user.click(await screen.findByRole("button", { name: "New image" }));
    await user.type(screen.getByLabelText("Name"), "Windows 11 Standard");
    await user.click(screen.getByRole("button", { name: "Create" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/unavailable right now/);
    expect(alert).not.toHaveTextContent(/ApiError/);
  });

  it("translates a 422 from Save profile and never echoes the password back to the screen", async () => {
    // The real FastAPI 422 shape. `input` echoes what was submitted — here a
    // credential — and `describeSubmitError` deliberately never reads it, so
    // this asserts invariant 3 through the validation-error door as well as
    // asserting the raw payload is not printed.
    const body = JSON.stringify({
      detail: [
        {
          type: "string_too_short",
          loc: ["body", "post_config", "domain_join", "password"],
          msg: "String should have at least 12 characters",
          input: "hunter2-in-the-clear",
          ctx: { min_length: 12 },
        },
      ],
    });
    api.listOsImages = vi.fn().mockResolvedValue([IMAGE]);
    api.updateProvisionProfile = vi.fn().mockRejectedValue(new ApiError(422, body));
    const user = userEvent.setup();
    renderWithProviders(<ProfilesPage />);

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    // Two fieldsets carry this same label (domain join, local admin); the
    // first is the domain-join password the 422 below is about.
    await user.type(
      screen.getAllByLabelText("Password (leave blank to keep existing)")[0] as HTMLElement,
      "hunter2-in-the-clear",
    );
    await user.click(screen.getByRole("button", { name: "Save" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Password: String should have at least 12 characters");
    expect(alert).not.toHaveTextContent("hunter2-in-the-clear");
    expect(alert).not.toHaveTextContent("string_too_short");
    expect(document.body.textContent).not.toContain("hunter2-in-the-clear");
  });
});

// --------------------------------------------------------------------------
// The Edit dialog opened blank, and its full-body upsert then destroyed data
// --------------------------------------------------------------------------

describe("the profile editor opens on the profile being edited", () => {
  // `ProfileEditor` is mounted once by `ProfilesPage`, while `editing` is
  // still null, and seeds every field with `useState(existing?.… ?? default)`.
  // React does not re-run an initializer for a component that stays mounted,
  // so Edit opened a BLANK editor for a populated profile — and because
  // `PUT /provisioning/profiles/{id}` is a full-body upsert whose submit
  // deletes empty sub-fields and posts `enabled` from state, saving that
  // blank form dropped the domain-join config and re-enabled a disabled
  // profile, reporting success as it closed.
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listProvisionProfiles: vi.fn().mockResolvedValue([CONFIGURED_PROFILE]),
      listOsImages: vi.fn().mockResolvedValue([IMAGE]),
      updateProvisionProfile: vi.fn().mockResolvedValue(CONFIGURED_PROFILE),
      createProvisionProfile: vi.fn(),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("prefills the fields from the profile rather than opening empty", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ProfilesPage />);

    await user.click(await screen.findByRole("button", { name: "Edit" }));

    expect(screen.getByLabelText("Name")).toHaveValue("standard-workstation");
    expect(screen.getByLabelText("Domain")).toHaveValue("corp.local");
    expect(screen.getByLabelText("Enabled")).not.toBeChecked();
  });

  it("saves the profile's own domain and enabled flag, not the defaults of a blank form", async () => {
    // The consequence, asserted on the wire: an operator who opens Edit and
    // presses Save must not silently strip `domain_join` or flip `enabled`.
    const user = userEvent.setup();
    renderWithProviders(<ProfilesPage />);

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(api.updateProvisionProfile).toHaveBeenCalled());
    const [, sent] = (api.updateProvisionProfile as ReturnType<typeof vi.fn>).mock.calls[0]!;
    expect(sent.enabled).toBe(false);
    expect(sent.post_config).toEqual({
      domain_join: { domain: "corp.local", username: "svc-join" },
    });
  });

  it("opens New profile empty even after an existing profile was edited", async () => {
    // The other side of keying: reusing one instance would now carry the last
    // edited profile's values into a NEW profile.
    const user = userEvent.setup();
    renderWithProviders(<ProfilesPage />);

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    expect(screen.getByLabelText("Name")).toHaveValue("standard-workstation");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    await user.click(screen.getByRole("button", { name: "New profile" }));
    expect(screen.getByLabelText("Name")).toHaveValue("");
    expect(screen.getByLabelText("Domain")).toHaveValue("");
  });
});

// --------------------------------------------------------------------------
// Cycle 6/7 in the profile editor and the image dialog
// --------------------------------------------------------------------------

describe("the profile editor picks an image instead of listing one in a select", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listProvisionProfiles: vi.fn().mockResolvedValue(PROFILES),
      listOsImages: vi.fn().mockResolvedValue([IMAGE]),
      createProvisionProfile: vi.fn().mockResolvedValue(PROFILES[0]),
      updateProvisionProfile: vi.fn(),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("offers images as a searchable list whose options carry family, kind and state", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ProfilesPage />);

    await user.click(await screen.findByRole("button", { name: "New profile" }));
    const field = screen.getByLabelText("Image");
    expect(field).toHaveAttribute("role", "combobox");

    await user.click(field);
    expect(
      await screen.findByRole("option", {
        name: /Windows 11 Standard\s*windows · install · active/,
      }),
    ).toBeInTheDocument();
  });

  it("posts the id of the image that was picked, never a typed string", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ProfilesPage />);

    await user.click(await screen.findByRole("button", { name: "New profile" }));
    await user.type(screen.getByLabelText("Name"), "kiosk");
    await pickEntity(user, "Image", /Windows 11 Standard/);
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(api.createProvisionProfile).toHaveBeenCalled());
    const [sent] = (api.createProvisionProfile as ReturnType<typeof vi.fn>).mock.calls[0]!;
    expect(sent.image_id).toBe("image1");
  });

  it("says what would fill the image list, and links to where, when none exist", async () => {
    api.listOsImages = vi.fn().mockResolvedValue([]);
    const user = userEvent.setup();
    renderWithProviders(<ProfilesPage />);

    await user.click(await screen.findByRole("button", { name: "New profile" }));
    expect(screen.getByRole("link", { name: /Register an OS image/ })).toHaveAttribute(
      "href",
      "/provisioning/images",
    );

    await user.click(screen.getByLabelText("Image"));
    expect(await screen.findByText(/No OS images are registered\./)).toBeInTheDocument();
  });

  // CYCLE 3. Save read `disabled={mutation.isPending || !name || !imageId}`:
  // the click moved nothing and named nothing.
  it("names the missing Name and Image on submit instead of disabling Save", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ProfilesPage />);

    await user.click(await screen.findByRole("button", { name: "New profile" }));
    const save = screen.getByRole("button", { name: "Save" });
    expect(save).not.toBeDisabled();
    await user.click(save);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Name is required");
    expect(alert).toHaveTextContent("Image is required");
    expect(api.createProvisionProfile).not.toHaveBeenCalled();
  });

  it("refuses a whitespace-only profile name rather than posting it", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ProfilesPage />);

    await user.click(await screen.findByRole("button", { name: "New profile" }));
    await user.type(screen.getByLabelText("Name"), "   ");
    await pickEntity(user, "Image", /Windows 11 Standard/);
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Name is required");
    expect(api.createProvisionProfile).not.toHaveBeenCalled();
  });
});

describe("the New OS image dialog names what it is missing", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listOsImages: vi.fn().mockResolvedValue([]),
      createOsImage: vi.fn().mockResolvedValue(IMAGE),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("names the empty Name on submit instead of disabling Create", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ImagesPage />);

    await user.click(await screen.findByRole("button", { name: "New image" }));
    const create = screen.getByRole("button", { name: "Create" });
    expect(create).not.toBeDisabled();
    await user.click(create);

    expect(await screen.findByRole("alert")).toHaveTextContent("Name is required");
    expect(api.createOsImage).not.toHaveBeenCalled();
  });

  it("names the emptied Kind, which nothing checked before", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ImagesPage />);

    await user.click(await screen.findByRole("button", { name: "New image" }));
    await user.type(screen.getByLabelText("Name"), "Windows 11 Standard");
    await user.clear(screen.getByLabelText("Kind"));
    await user.click(screen.getByRole("button", { name: "Create" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Kind is required");
    expect(api.createOsImage).not.toHaveBeenCalled();
  });

  // A real <form>: `ui.tsx`'s Button sets no type, so Enter did nothing here.
  it("creates the image on Enter", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ImagesPage />);

    await user.click(await screen.findByRole("button", { name: "New image" }));
    await user.type(screen.getByLabelText("Name"), "Windows 11 Standard{Enter}");

    await waitFor(() =>
      expect(api.createOsImage).toHaveBeenCalledWith({
        os_family: "windows",
        name: "Windows 11 Standard",
        kind: "install",
      }),
    );
  });
});

describe("the device Provisioning tab names its profile and image", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listProvisionProfiles: vi.fn().mockResolvedValue(PROFILES),
      listOsImages: vi.fn().mockResolvedValue([IMAGE]),
      getDeviceProvisioning: vi.fn().mockResolvedValue({
        device_id: "dev1",
        profile_id: "profile1",
        image_id: "image1",
        enrolled_at: "2026-01-02T00:00:00.000Z",
        latest_run_state: "awaiting_enrollment",
      }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("renders the profile and image names, not their UUIDs", async () => {
    renderWithProviders(<DeviceProvisioningTab deviceId="dev1" />);

    expect(await screen.findByText("standard-workstation")).toBeInTheDocument();
    expect(screen.getByText("Windows 11 Standard")).toBeInTheDocument();
    expect(screen.queryByText("profile1")).not.toBeInTheDocument();
    expect(screen.queryByText("image1")).not.toBeInTheDocument();
  });

  it("humanises the run state rather than showing the raw enum", async () => {
    renderWithProviders(<DeviceProvisioningTab deviceId="dev1" />);

    expect(await screen.findByText("Awaiting enrollment")).toBeInTheDocument();
    expect(screen.queryByText("awaiting_enrollment")).not.toBeInTheDocument();
  });

  it("falls back to the id rather than to blank when the profile list is unreadable", async () => {
    // `nameForOption` never returns "—" for an id that exists: a device that
    // HAS a profile must not render as one that does not, even when the
    // caller cannot list profiles.
    api.listProvisionProfiles = vi.fn().mockRejectedValue(new ApiError(403, ""));
    renderWithProviders(<DeviceProvisioningTab deviceId="dev1" />);

    expect(await screen.findByText("profile1")).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// The picker options themselves — the sublabel is the safety, so it is
// asserted here rather than inspected in a JSX tree.
// --------------------------------------------------------------------------

describe("provisioning picker options", () => {
  it("distinguishes a profile by the image it lays down and whether it is enabled", () => {
    const [option] = profilePickerOptions(PROFILES, [IMAGE]);
    expect(option).toEqual({
      id: "profile1",
      label: "standard-workstation",
      sublabel: "Windows 11 Standard · enabled",
    });
  });

  it("says a profile is disabled, because starting a run from one is a mistake", () => {
    const [option] = profilePickerOptions([CONFIGURED_PROFILE], [IMAGE]);
    expect(option?.sublabel).toBe("Windows 11 Standard · disabled");
  });

  it("falls back to the image id rather than an empty separator when the image is unknown", () => {
    const [option] = profilePickerOptions(PROFILES, []);
    expect(option?.sublabel).toBe("image1 · enabled");
  });

  it("distinguishes an image by family, kind and state", () => {
    expect(imagePickerOptions([IMAGE])[0]?.sublabel).toBe("windows · install · active");
  });

  it("resolves an id to a name, and an unknown id to itself rather than to blank", () => {
    const options = profilePickerOptions(PROFILES, [IMAGE]);
    expect(nameForOption(options, "profile1")).toBe("standard-workstation");
    expect(nameForOption(options, "profile-gone")).toBe("profile-gone");
    expect(nameForOption(options, null)).toBeNull();
  });

  it("names both the surface that fills a list and the permission that may hide it", () => {
    // Cycle 7: a hint that asserts "there are none" is a lie to a reader who
    // simply cannot list them.
    expect(PROFILE_PICKER_EMPTY_HINT).toMatch(/Profiles tab/);
    expect(PROFILE_PICKER_EMPTY_HINT).toMatch(/permission/);
    expect(IMAGE_PICKER_EMPTY_HINT).toMatch(/Images tab/);
    expect(IMAGE_PICKER_EMPTY_HINT).toMatch(/permission/);
  });
});


// ==========================================================================
// NO DEAD TILES. See the same block in modules/backup for the full reasoning;
// in short, `StatCard`'s required `to` makes the compiler catch a MISSING
// link, and nothing but a test catches a link that goes nowhere useful — a
// mistyped path, or a filter the destination does not read.
// ==========================================================================

describe("every provisioning tile opens the rows it counted", () => {
  let api: Partial<ApiClient>;

  const FAILED_RUN: ProvisionRun = {
    ...(RUNS[0] as ProvisionRun),
    id: "run-failed",
    state: "failed",
    target_ref: "11:22:33:44:55:66",
    finished_at: "2026-01-02T00:30:00.000Z",
    error: "image checksum mismatch",
  };

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listProvisionRuns: vi.fn().mockResolvedValue([RUNS[0] as ProvisionRun, FAILED_RUN]),
      listProvisionProfiles: vi.fn().mockResolvedValue(PROFILES),
      listOsImages: vi.fn().mockResolvedValue([IMAGE]),
      getProvisioningReport: vi.fn().mockResolvedValue(HALF_FAILED_REPORT),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("links every runs tile at a route the application has", async () => {
    renderWithProviders(<ProvisioningRunsPage />);
    await screen.findByText("50%");

    expectLiveTiles([
      "/provisioning?state=complete",
      "/devices",
      "/provisioning?state=failed",
      "/provisioning",
    ]);
  });

  // THE OTHER HALF OF "NOT DEAD": arriving at the href has to change what the
  // page shows, and the state name in the link has to be the backend's own
  // (`reporting.py` RUN_STATES) or it matches nothing.
  it("narrows the runs table to the failed runs the tile counted", async () => {
    renderWithProviders(<ProvisioningRunsPage />, "/provisioning?state=failed");

    expect(await screen.findByText("run-failed")).toBeInTheDocument();
    expect(screen.queryByText("run1")).not.toBeInTheDocument();
  });

  it("shows every run when the filter is absent", async () => {
    renderWithProviders(<ProvisioningRunsPage />);

    expect(await screen.findByText("run1")).toBeInTheDocument();
    expect(screen.getByText("run-failed")).toBeInTheDocument();
  });

  it("distinguishes a filter that matched nothing from having no runs", async () => {
    renderWithProviders(<ProvisioningRunsPage />, "/provisioning?state=enrolled");

    expect(await screen.findByText("No run matches that filter.")).toBeInTheDocument();
    expect(screen.queryByText("No provisioning runs yet.")).not.toBeInTheDocument();
  });

  it("links every images tile at a route the application has", async () => {
    api.listOsImages = vi.fn().mockResolvedValue([IMAGE]);
    renderWithProviders(<ImagesPage />, "/provisioning/images");
    await screen.findByText("Windows 11 Standard");

    // Three tiles since the review wave's round 2: "Not active" is the third
    // and opens the rows the filter below it also reaches.
    expectLiveTiles([
      "/provisioning/images",
      "/provisioning/images?state=active",
      "/provisioning/images?state=inactive",
    ]);
  });

  it("narrows the images table to the active images the tile counted", async () => {
    api.listOsImages = vi
      .fn()
      .mockResolvedValue([
        IMAGE,
        { ...IMAGE, id: "image2", name: "Windows 10 Legacy", state: "archived" },
      ]);
    renderWithProviders(<ImagesPage />, "/provisioning/images?state=active");

    expect(await screen.findByText("Windows 11 Standard")).toBeInTheDocument();
    expect(screen.queryByText("Windows 10 Legacy")).not.toBeInTheDocument();
  });

  it("links every profiles tile at a route the application has", async () => {
    renderWithProviders(<ProfilesPage />, "/provisioning/profiles");
    await screen.findByText("standard-workstation");

    expectLiveTiles([
      "/provisioning/profiles",
      "/provisioning/profiles?enabled=true",
      "/provisioning/profiles?enabled=false",
    ]);
  });

  it("narrows the profiles table to the enabled profiles the tile counted", async () => {
    api.listProvisionProfiles = vi
      .fn()
      .mockResolvedValue([
        PROFILES[0] as ProvisionProfile,
        { ...(PROFILES[0] as ProvisionProfile), id: "profile2", name: "lab-bench", enabled: false },
      ]);
    renderWithProviders(<ProfilesPage />, "/provisioning/profiles?enabled=true");

    expect(await screen.findByText("standard-workstation")).toBeInTheDocument();
    expect(screen.queryByText("lab-bench")).not.toBeInTheDocument();
  });

  // ZERO IS A MEASUREMENT, NOT AN ABSENCE.
  it("says WHICH zero a zero failure count is", async () => {
    api.getProvisioningReport = vi.fn().mockResolvedValue(EMPTY_REPORT);
    renderWithProviders(<ProvisioningRunsPage />);

    await screen.findByText("Never run");
    expect(screen.getByText("No build has ever run")).toBeInTheDocument();
    expect(screen.queryByText("No build failed")).not.toBeInTheDocument();
  });

  it("says the other zero when builds have happened and none failed", async () => {
    api.getProvisioningReport = vi.fn().mockResolvedValue({
      ...HALF_FAILED_REPORT,
      run_counts_by_state: { complete: 2 },
      success_rate: 1,
    });
    renderWithProviders(<ProvisioningRunsPage />);

    await screen.findByText("100%");
    expect(screen.getByText("No build failed")).toBeInTheDocument();
  });
});
