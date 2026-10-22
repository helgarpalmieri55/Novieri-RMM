import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../../lib/api";
import { NO_DATE } from "../../../lib/datetime";
import type { ApiClient, Device, DeviceGroup, Me, Page } from "../../../lib/api";
import { formatBytes } from "../../../lib/format";
import {
  DEVICE_PICKER_EMPTY_HINT,
  REPOSITORY_PICKER_EMPTY_HINT,
  devicePickerOptions,
  nameForOption,
  repositoryPickerOptions,
} from "../pickerOptions";
import {
  backupPointStateVariant,
  backupRunStateVariant,
  repositoryStateVariant,
  restoreJobStateVariant,
} from "../backupState";
import { BackupPointsPage } from "../BackupPointsPage";
import { BackupPolicyEditor } from "../BackupPolicyEditor";
import { DeviceBackupTab } from "../DeviceBackupTab";
import { RepositoriesPage } from "../RepositoriesPage";
import type {
  BackupManifest,
  BackupPoint,
  BackupPolicy,
  FleetBackupReport,
  DeviceBackupStatus,
  Repository,
} from "../types";

// The auth-backed API client is mocked so this is a pure component test: no
// real fetch. Backup data is plain TanStack Query state (no live-event
// stream), so freshness comes from invalidating the backup query keys after
// mutations succeed (mirrors modules/deploy/__tests__/deploy.test.tsx).
const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

/**
 * Choose an option from an `EntityPicker`, the way a person does: focus the
 * combobox, then click the row. Deliberately NOT `user.type(...)` — typing
 * only FILTERS the list; nothing is chosen until an option is clicked, which
 * is exactly why a hand-typed UUID can no longer reach the server from here.
 */
async function pickEntity(
  user: ReturnType<typeof userEvent.setup>,
  pickerLabel: string,
  optionName: RegExp,
) {
  await user.click(screen.getByLabelText(pickerLabel));
  await user.click(await screen.findByRole("option", { name: optionName }));
}

function renderWithProviders(ui: ReactElement, entry = "/backup") {
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
 * A tile that links to `/backup/repository` (no `s`) is exactly as dead as a
 * tile that links nowhere, and it type-checks, renders, and passes every test
 * that only asserts "the href is non-empty". So the hrefs below are checked
 * against `App.tsx`'s actual `path=` list — the same source-text technique
 * `App.routing.test.tsx` uses, and for the same reason: the property that
 * matters to an operator is "can you get there", and nothing else asserts it.
 */
const APP_SOURCE = Object.values(
  import.meta.glob("../../../App.tsx", { query: "?raw", import: "default", eager: true }),
)[0] as string;

const ROUTED_PATHS = new Set(
  [...APP_SOURCE.matchAll(/path="([^"]+)"/g)].map((match) => match[1] as string),
);

/**
 * Every tile on screen is a link, to a route this application actually has.
 *
 * `expected` is the ordered list of hrefs, so a tile that silently changes
 * destination is a failing test rather than a quiet redirection of the
 * operator.
 */
function expectLiveTiles(expected: readonly string[]) {
  const tiles = [...document.querySelectorAll(".stat-card")];
  expect(tiles.map((tile) => tile.getAttribute("href"))).toEqual([...expected]);
  for (const tile of tiles) {
    expect(tile.tagName).toBe("A");
    const path = (tile.getAttribute("href") ?? "").split("?")[0];
    expect(ROUTED_PATHS.has(path as string), `${path} is not a route in App.tsx`).toBe(true);
  }
}

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "operator@example.com",
  display_name: "Operator",
  permissions: ["backup.read", "backup.run", "backup.restore", "backup.policy.manage"],
};

// The same operator, with the profile timezone `/me` carries (cycle 2) set.
// Every datetime assertion below uses this rather than the browser zone: a
// test that reads the machine's own zone passes on the machine that wrote it
// and says nothing about the bug — the five hours are lost between a zone the
// user chose and a zone nobody chose.
const ME_NEW_YORK: Me = { ...ME, timezone: "America/New_York" };
const ME_TOKYO: Me = { ...ME, timezone: "Asia/Tokyo" };

// A real device UUID: the Run control posts to `/backup/devices/{id}/run`,
// whose path parameter is typed `UUID` server-side.
const DEVICE_UUID = "06a85c19-55e2-7f3c-8000-831c7bdba357";
const OFFLINE_UUID = "06a85c19-55e2-7f3c-8000-000000000002";

function device(over: Partial<Device> & { id: string }): Device {
  return {
    hostname: "WKS-042",
    fqdn: null,
    os_family: "windows",
    os_version: "11",
    architecture: "x86_64",
    device_group_id: null,
    status: "active",
    agent_status: "online",
    last_seen_at: "2026-01-02T00:00:00.000Z",
    cert_not_after: null,
    cert_state: "ok",
    cert_revoked: false,
    lifecycle: "in_service",
    asset_type: "computer",
    source: "agent",
    // An enrolled machine proved its own identity at enrolment; nobody had
    // to vouch for the row (asset-inventory spec §3).
    provenance: "agent",
    description: null,
    primary_ip: null,
    owner: null,
    location: null,
    ...over,
  };
}

// Three endpoints that a backup targets differently: one an agent can answer
// on, one whose agent is offline, and one with no agent row at all — the case
// where a signed job can never arrive.
const DEVICES: Page<Device> = {
  items: [
    device({ id: DEVICE_UUID }),
    device({ id: OFFLINE_UUID, hostname: "BUILD-01", os_family: "linux", agent_status: "offline" }),
    device({
      id: "06a85c19-55e2-7f3c-8000-000000000003",
      hostname: "SW-CORE-1",
      os_family: null,
      agent_status: null,
      asset_type: "network",
      source: "manual",
    }),
  ],
  total: 3,
};

const REPOSITORIES: Repository[] = [
  {
    id: "repo1",
    name: "primary-vault",
    storage_ref: "s3://backups/primary",
    state: "active",
    dedup_params: {},
    created_at: "2026-01-01T00:00:00.000Z",
  },
];

// A fleet that has never taken a backup: no runs in any state, one device
// covered by nothing. `success_rate` here is a fraction over zero runs.
const EMPTY_REPORT: FleetBackupReport = {
  devices_total: 1,
  devices_covered: 0,
  coverage_rate: 0,
  run_counts_by_state: {},
  success_rate: 0,
  points_needing_verification: 0,
  dedup: { bytes_protected_total: 0, bytes_stored_total: 0, dedup_ratio: 1, savings_pct: 0 },
};

// A fleet that HAS run backups — one succeeded, one failed — and covers
// every device it knows about.
const RUN_REPORT: FleetBackupReport = {
  ...EMPTY_REPORT,
  devices_covered: 1,
  coverage_rate: 1,
  run_counts_by_state: { success: 1, failed: 1 },
  success_rate: 0.5,
};

const POINTS: BackupPoint[] = [
  {
    id: "pt1",
    device_id: DEVICE_UUID,
    repository_id: "repo1",
    backup_run_id: "run1",
    taken_at: "2026-01-02T00:00:00.000Z",
    size: 1024,
    state: "complete",
    verified_at: null,
    restore_tested_at: null,
  },
];

/**
 * Opening the run form.
 *
 * REVIEW WAVE, round 1, /backup (harmony): the form used to sit in the page
 * head, and it was the only head in its group carrying a field label -- the
 * picker's own "Device" -- which put the submit 16px below the field it
 * belongs to. It is a dialog now, opened by the head's one primary, the way
 * /deploy's New deployment and /provisioning's Provision open theirs. The
 * form inside it is the same form: same picker, same rule, same refusal,
 * same queued sentence, and every assertion below still makes it.
 */
async function openRun(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Run backup" }));
  return screen.findByRole("dialog", { name: "Run backup" });
}

describe("BackupPointsPage", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listBackupPoints: vi.fn().mockResolvedValue(POINTS),
      listDevices: vi.fn().mockResolvedValue(DEVICES),
      listBackupRepositories: vi.fn().mockResolvedValue(REPOSITORIES),
      getBackupReport: vi.fn().mockResolvedValue(EMPTY_REPORT),
      runBackup: vi.fn().mockResolvedValue({ job_ids: ["j1"] }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  // THE THREE STATES A MOCK NEVER HAS. Loading, empty and a failed request
  // must render distinguishably — mirrors DevicesPage.test.tsx.
  it("says it is loading rather than showing an empty table", async () => {
    api.listBackupPoints = vi.fn(() => new Promise<never>(() => {}));
    renderWithProviders(<BackupPointsPage />);
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the request FAILED rather than showing an empty table", async () => {
    api.listBackupPoints = vi.fn().mockRejectedValue(new Error("boom"));
    renderWithProviders(<BackupPointsPage />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText(/No backup points recorded/i)).not.toBeInTheDocument();
  });

  it("says there are no backup points when there genuinely are none", async () => {
    api.listBackupPoints = vi.fn().mockResolvedValue([]);
    renderWithProviders(<BackupPointsPage />);
    expect(await screen.findByText("No backup points recorded yet.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  // REVIEW WAVE, round 1, /backup (harmony): the run form stood in the page
  // head, the only head in its group with a field label in it. The control
  // is the same control and the form is the same form -- it is one click
  // behind the head's primary now, as it is on /deploy and /provisioning.
  it("keeps the run form in a dialog rather than in the page head", async () => {
    const user = userEvent.setup();
    renderWithProviders(<BackupPointsPage />);

    await screen.findByText("pt1");
    expect(screen.queryByLabelText("Device")).not.toBeInTheDocument();
    const dialog = await openRun(user);
    expect(within(dialog).getByLabelText("Device")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Run" })).toBeEnabled();
  });

  it("lists points from the mocked api", async () => {
    renderWithProviders(<BackupPointsPage />);

    expect(await screen.findByText("pt1")).toBeInTheDocument();
    // Cycle 11's rule, applied to this module's own enums: the badge reads
    // "Complete", not the wire value.
    expect(screen.getByText("Complete")).toBeInTheDocument();
  });

  // ------------------------------------------------------------------------
  // CYCLE 6, and Auditor A's HIGH: "A fifth hand-typed raw ID field — 'Device
  // ID' is the only way to target a backup ... The stat card directly beside
  // it reads 'Protected endpoints 0 / 1', so the system knows there is
  // exactly one device — but will not offer it."
  // ------------------------------------------------------------------------

  it("offers devices as a searchable list rather than a raw ID box", async () => {
    const user = userEvent.setup();
    renderWithProviders(<BackupPointsPage />);

    await screen.findByText("pt1");
    await openRun(user);
    // The old field: `{"al":"Device ID","tag":"INPUT","type":"text"}`.
    expect(screen.queryByLabelText("Device ID")).not.toBeInTheDocument();
    const field = screen.getByLabelText("Device");
    expect(field).toHaveAttribute("role", "combobox");

    await user.click(field);
    // The sublabel is what stops a backup being aimed at a machine that
    // cannot answer.
    expect(
      await screen.findByRole("option", { name: /WKS-042\s*windows · online/ }),
    ).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /BUILD-01\s*linux · offline/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /SW-CORE-1\s*no agent/ })).toBeInTheDocument();
  });

  it("calls the run mutation with the id of the device that was picked", async () => {
    const user = userEvent.setup();
    renderWithProviders(<BackupPointsPage />);

    await screen.findByText("pt1");
    await openRun(user);
    await pickEntity(user, "Device", /WKS-042/);
    await user.click(screen.getByRole("button", { name: "Run" }));

    expect(api.runBackup).toHaveBeenCalledWith(DEVICE_UUID);
  });

  // The mistyped-UUID case is now unreachable rather than validated: typing
  // into the picker filters the list and sets no value, so a string a person
  // invented cannot be submitted at all.
  it("does not run a backup against text typed into the picker", async () => {
    const user = userEvent.setup();
    renderWithProviders(<BackupPointsPage />);

    await screen.findByText("pt1");
    await openRun(user);
    await user.type(screen.getByLabelText("Device"), "dev9");
    await user.click(screen.getByRole("button", { name: "Run" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Device is required");
    expect(api.runBackup).not.toHaveBeenCalled();
  });

  it("says what would fill the device list when it is empty", async () => {
    // Cycle 7: an empty picker that says nothing is the blocked-flow finding
    // wearing a nicer control.
    api.listDevices = vi.fn().mockResolvedValue({ items: [], total: 0 });
    const user = userEvent.setup();
    renderWithProviders(<BackupPointsPage />);

    await screen.findByText("pt1");
    await openRun(user);
    await user.click(screen.getByLabelText("Device"));
    expect(await screen.findByText(/No devices to choose from\./)).toBeInTheDocument();
  });

  // ------------------------------------------------------------------------
  // THE SILENT NO-OP, auditor A (HIGH), /backup -> Device ID + Run now, "the
  // eighth occurrence". `disabled={run.isPending || !deviceId}` swallowed the
  // click with the field empty: nothing moved, and no field was named.
  // ------------------------------------------------------------------------

  it("names the unpicked Device on submit instead of disabling Run", async () => {
    const user = userEvent.setup();
    renderWithProviders(<BackupPointsPage />);

    await screen.findByText("pt1");
    await openRun(user);
    const runButton = screen.getByRole("button", { name: "Run" });
    expect(runButton).toBeEnabled();
    await user.click(runButton);

    expect(await screen.findByRole("alert")).toHaveTextContent("Device is required");
    expect(api.runBackup).not.toHaveBeenCalled();
  });

  // There was no <form> on this route at all and `ui.tsx`'s Button sets no
  // type, so Enter did nothing anywhere on it.
  it("runs the backup when Enter is pressed in the run form", async () => {
    const user = userEvent.setup();
    renderWithProviders(<BackupPointsPage />);

    await screen.findByText("pt1");
    await openRun(user);
    await pickEntity(user, "Device", /WKS-042/);
    await user.type(screen.getByLabelText("Device"), "{Enter}");

    await waitFor(() => expect(api.runBackup).toHaveBeenCalledWith(DEVICE_UUID));
  });

  // The success side of the same silence: the click cleared the field and
  // said nothing, and the points table does not show the run, so the operator
  // had no evidence anything had happened.
  it("says what the run dispatched instead of clearing the field in silence", async () => {
    const user = userEvent.setup();
    renderWithProviders(<BackupPointsPage />);

    await screen.findByText("pt1");
    await openRun(user);
    await pickEntity(user, "Device", /WKS-042/);
    await user.click(screen.getByRole("button", { name: "Run" }));

    expect(await screen.findByRole("status")).toHaveTextContent("Backup queued — 1 job dispatched.");
  });

  // A run that dispatched no job must not read as a started backup -- the
  // module's own docstring is explicit that backup.run has never completed
  // end to end.
  it("says nothing was dispatched when the server queued no job", async () => {
    const user = userEvent.setup();
    api.runBackup = vi.fn().mockResolvedValue({ job_ids: [] });
    renderWithProviders(<BackupPointsPage />);

    await screen.findByText("pt1");
    await openRun(user);
    await pickEntity(user, "Device", /WKS-042/);
    await user.click(screen.getByRole("button", { name: "Run" }));

    expect(await screen.findByRole("status")).toHaveTextContent(/Nothing was dispatched/);
  });

  // The BLOCKER: `ApiError` is built as `new ApiError(status, await
  // response.text())`, so a bodyless 503 carries `message === ""` and
  // `String(run.error)` rendered "Error: " into a <p> nobody announced.
  it("renders a sentence for a bodyless 503 instead of an empty error", async () => {
    const user = userEvent.setup();
    api.runBackup = vi.fn().mockRejectedValue(new ApiError(503, ""));
    renderWithProviders(<BackupPointsPage />);

    await screen.findByText("pt1");
    await openRun(user);
    await pickEntity(user, "Device", /WKS-042/);
    await user.click(screen.getByRole("button", { name: "Run" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/unavailable right now/);
    expect(alert).not.toHaveTextContent(/^Error:\s*$/);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("hides the Run control when the caller lacks backup.run", async () => {
    api.me = vi.fn().mockResolvedValue({
      ...ME,
      permissions: ME.permissions.filter((p) => p !== "backup.run"),
    });
    renderWithProviders(<BackupPointsPage />);

    await screen.findByText("pt1");
    expect(screen.queryByRole("button", { name: "Run backup" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Run" })).not.toBeInTheDocument();
  });

  it("hides the Restore control when the caller lacks backup.restore", async () => {
    api.me = vi.fn().mockResolvedValue({
      ...ME,
      permissions: ME.permissions.filter((p) => p !== "backup.restore"),
    });
    renderWithProviders(<BackupPointsPage />);

    await screen.findByText("pt1");
    expect(screen.queryByRole("button", { name: "Restore" })).not.toBeInTheDocument();
  });

  // A failed or pruned point has nothing a restore job could read from: a
  // failed run never finished writing a manifest, and a pruned point has
  // aged out of the repository. The control stays visible, but disabled and
  // with a reason -- an operator scanning the row should not have to click
  // it to learn that.
  it("disables Restore, with a reason, on a failed backup point", async () => {
    api.listBackupPoints = vi.fn().mockResolvedValue([{ ...(POINTS[0] as BackupPoint), state: "failed" }]);
    renderWithProviders(<BackupPointsPage />);

    await screen.findByText("pt1");
    const restoreButton = screen.getByRole("button", { name: "Restore" });
    expect(restoreButton).toBeDisabled();
    expect(restoreButton).toHaveAttribute("title", expect.stringMatching(/failed/i));
  });

  it("disables Restore, with a reason, on a pruned backup point", async () => {
    api.listBackupPoints = vi.fn().mockResolvedValue([{ ...(POINTS[0] as BackupPoint), state: "pruned" }]);
    renderWithProviders(<BackupPointsPage />);

    await screen.findByText("pt1");
    const restoreButton = screen.getByRole("button", { name: "Restore" });
    expect(restoreButton).toBeDisabled();
    expect(restoreButton).toHaveAttribute("title", expect.stringMatching(/retention/i));
  });

  it("keeps Restore enabled on a complete backup point", async () => {
    renderWithProviders(<BackupPointsPage />);

    await screen.findByText("pt1");
    expect(screen.getByRole("button", { name: "Restore" })).toBeEnabled();
  });

  // The two columns read as near-synonyms and were routinely confused for
  // each other; they measure different things (data integrity vs. an actual
  // restore drill) and cannot be merged without losing one of them, so they
  // are renamed instead.
  it("names the two verification columns so they cannot be confused for each other", async () => {
    renderWithProviders(<BackupPointsPage />);

    await screen.findByText("pt1");
    expect(screen.getByRole("columnheader", { name: "Data checked" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Restore tested" })).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Verification" })).not.toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Restore test" })).not.toBeInTheDocument();
  });
});

const MANIFEST: BackupManifest = {
  schema_version: 1,
  streams: [
    {
      type: "files",
      entries: [
        {
          path: "/etc/app/config.yaml",
          kind: "file",
          mode: 0o644,
          size: 512,
          mtime: 0,
          chunk_ids: ["c1"],
          target: null,
        },
      ],
    },
  ],
};

describe("RestoreDialog", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listBackupPoints: vi.fn().mockResolvedValue(POINTS),
      listDevices: vi.fn().mockResolvedValue(DEVICES),
      listBackupRepositories: vi.fn().mockResolvedValue(REPOSITORIES),
      getBackupReport: vi.fn().mockResolvedValue(EMPTY_REPORT),
      getBackupManifest: vi.fn().mockResolvedValue(MANIFEST),
      restoreBackupPoint: vi.fn().mockResolvedValue({ job_ids: ["r1"] }),
      verifyBackupPoint: vi.fn().mockResolvedValue({ job_ids: ["v1"] }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  // The page has a row-level "Restore" button (opens the dialog) and, once
  // open, the dialog has its own "Restore" submit button — both share the
  // accessible name "Restore", so the dialog's is disambiguated as the last
  // one in DOM order (RestoreDialog is mounted after the points table).
  function dialogRestoreButton(): HTMLElement {
    const buttons = screen.getAllByRole("button", { name: "Restore" });
    const last = buttons.at(-1);
    if (!last) throw new Error("expected a Restore button in the dialog");
    return last;
  }

  async function openDialogAndSelectPath(user: ReturnType<typeof userEvent.setup>) {
    renderWithProviders(<BackupPointsPage />);
    await screen.findByText("pt1");
    await user.click(screen.getByRole("button", { name: "Restore" }));
    await screen.findByText("/etc/app/config.yaml");
    await user.click(screen.getByRole("checkbox"));
  }

  /** Get past the cycle-8 confirmation, which every restore now goes through. */
  async function confirmRestore(user: ReturnType<typeof userEvent.setup>) {
    await user.click(await screen.findByRole("button", { name: "Restore files" }));
  }

  it("defaults to Original location and restores with target: { location: 'original' } and no path required", async () => {
    const user = userEvent.setup();
    await openDialogAndSelectPath(user);

    // The overwrite warning is said on the option itself now, so the radio's
    // accessible name carries it too -- matched by prefix rather than pinned
    // to the whole sentence.
    expect(screen.getByRole("radio", { name: /^Original location/ })).toBeChecked();
    expect(screen.queryByLabelText("Alternate target path")).not.toBeInTheDocument();

    const restoreButton = dialogRestoreButton();
    expect(restoreButton).toBeEnabled();
    await user.click(restoreButton);
    await confirmRestore(user);

    expect(api.restoreBackupPoint).toHaveBeenCalledWith("pt1", {
      selection: ["/etc/app/config.yaml"],
      target: { location: "original" },
    });
  });

  // CYCLE 9. Original overwrites files nothing else can bring back, which is
  // what the extra step is for; Alternate writes to a path the operator just
  // typed, so the submit itself is the confirmation and no dialog stacks on
  // top of it.
  it("restores to an alternate location on submit, with no extra confirmation", async () => {
    const user = userEvent.setup();
    await openDialogAndSelectPath(user);

    await user.click(screen.getByRole("radio", { name: "Alternate location" }));
    await user.type(screen.getByLabelText("Alternate target path"), "/mnt/restore");

    const restoreButton = dialogRestoreButton();
    expect(restoreButton).toBeEnabled();
    await user.click(restoreButton);

    expect(screen.queryByRole("dialog", { name: "Restore files onto this device" })).not.toBeInTheDocument();
    expect(api.restoreBackupPoint).toHaveBeenCalledWith("pt1", {
      selection: ["/etc/app/config.yaml"],
      target: { location: "alternate", path: "/mnt/restore" },
    });
  });

  // ------------------------------------------------------------------------
  // CYCLE 3 in this dialog. Restore read `disabled={… || !canSubmitRestore}`:
  // with nothing ticked, or Alternate chosen and no path typed, the control
  // sat dead and named neither cause.
  // ------------------------------------------------------------------------

  it("names the missing alternate path on submit instead of disabling Restore", async () => {
    const user = userEvent.setup();
    await openDialogAndSelectPath(user);

    await user.click(screen.getByRole("radio", { name: "Alternate location" }));
    const restoreButton = dialogRestoreButton();
    expect(restoreButton).toBeEnabled();
    await user.click(restoreButton);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Alternate target path is required",
    );
    expect(api.restoreBackupPoint).not.toHaveBeenCalled();
  });

  it("refuses a whitespace-only alternate path rather than posting it", async () => {
    const user = userEvent.setup();
    await openDialogAndSelectPath(user);

    await user.click(screen.getByRole("radio", { name: "Alternate location" }));
    await user.type(screen.getByLabelText("Alternate target path"), "   ");
    await user.click(dialogRestoreButton());

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Alternate target path is required",
    );
    expect(api.restoreBackupPoint).not.toHaveBeenCalled();
  });

  it("names an empty selection rather than sitting disabled with nothing ticked", async () => {
    const user = userEvent.setup();
    renderWithProviders(<BackupPointsPage />);
    await screen.findByText("pt1");
    await user.click(screen.getByRole("button", { name: "Restore" }));
    await screen.findByText("/etc/app/config.yaml");

    await user.click(dialogRestoreButton());

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Select at least one file or folder to restore",
    );
    expect(api.restoreBackupPoint).not.toHaveBeenCalled();
  });

  // ------------------------------------------------------------------------
  // CYCLE 8. A restore to the original location overwrites what is on the
  // endpoint now, and nothing in this product undoes it.
  // ------------------------------------------------------------------------

  it("asks before restoring, names the device, and does not restore on Cancel", async () => {
    const user = userEvent.setup();
    await openDialogAndSelectPath(user);
    await user.click(dialogRestoreButton());

    const confirm = await screen.findByRole("dialog", {
      name: "Restore files onto this device",
    });
    // The SUBJECT is the device by name — what lets somebody notice they have
    // the wrong row — never its UUID.
    expect(within(confirm).getByText("WKS-042")).toBeInTheDocument();
    expect(within(confirm).queryByText(DEVICE_UUID)).not.toBeInTheDocument();
    expect(within(confirm).getByText(/cannot be recovered afterwards/)).toBeInTheDocument();
    expect(api.restoreBackupPoint).not.toHaveBeenCalled();

    await user.click(within(confirm).getByRole("button", { name: "Cancel" }));
    expect(api.restoreBackupPoint).not.toHaveBeenCalled();
  });

  // CYCLE 9. The overwrite warning is on the Original option itself, not
  // only two clicks away in the confirmation.
  it("says plainly, on the option, that Original location overwrites current files", async () => {
    const user = userEvent.setup();
    await openDialogAndSelectPath(user);

    expect(
      screen.getByText("— overwrites the files currently on this device"),
    ).toBeInTheDocument();
  });

  // Verify reads a point; it does not write to the endpoint, so it is NOT
  // behind the confirmation. Over-confirming is how people learn to click
  // through the dialog that matters.
  it("verifies without a confirmation, because verify writes nothing to the device", async () => {
    const user = userEvent.setup();
    await openDialogAndSelectPath(user);

    await user.click(screen.getByRole("button", { name: "Verify" }));

    await waitFor(() => expect(api.verifyBackupPoint).toHaveBeenCalledWith("pt1"));
  });
});

describe("RepositoriesPage", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listBackupRepositories: vi.fn().mockResolvedValue(REPOSITORIES),
      createBackupRepository: vi.fn(),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("lists repositories from the mocked api", async () => {
    renderWithProviders(<RepositoriesPage />);

    expect(await screen.findByText("primary-vault")).toBeInTheDocument();
    // The state is a kit pill now, which humanises the wire word; scoped to
    // the row because the tile above the list carries the same word.
    const row = screen.getByText("primary-vault").closest("tr") as HTMLElement;
    expect(within(row).getByText("Active")).toBeInTheDocument();
  });

  it("hides the New repository control when the caller lacks backup.policy.manage", async () => {
    api.me = vi.fn().mockResolvedValue({
      ...ME,
      permissions: ME.permissions.filter((p) => p !== "backup.policy.manage"),
    });
    renderWithProviders(<RepositoriesPage />);

    await screen.findByText("primary-vault");
    expect(screen.queryByRole("button", { name: "New repository" })).not.toBeInTheDocument();
  });

  it("says it is loading rather than showing an empty table", async () => {
    api.listBackupRepositories = vi.fn(() => new Promise<never>(() => {}));
    renderWithProviders(<RepositoriesPage />);
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the request FAILED rather than showing an empty table", async () => {
    api.listBackupRepositories = vi.fn().mockRejectedValue(new Error("boom"));
    renderWithProviders(<RepositoriesPage />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText(/No repositories registered/i)).not.toBeInTheDocument();
  });

  it("says there are no repositories when there genuinely are none", async () => {
    api.listBackupRepositories = vi.fn().mockResolvedValue([]);
    renderWithProviders(<RepositoriesPage />);
    expect(await screen.findByText("No repositories registered.")).toBeInTheDocument();
  });
});

const POLICIES: BackupPolicy[] = [
  {
    id: "pol1",
    device_group_id: null,
    repository_id: "repo1",
    sources: {},
    window_cron: "0 2 * * *",
    retention_gfs: {},
    enabled: true,
  },
];

const DEVICE_GROUPS: DeviceGroup[] = [
  {
    id: "grp-build",
    name: "Build runners",
    parent_group_id: null,
    description: "",
    dynamic: false,
    device_count: 3,
    precedence: 0,
    archived_at: null,
  },
];

const DEVICE_GROUP_PAGE = {
  items: DEVICE_GROUPS,
  total: DEVICE_GROUPS.length,
  ungrouped_device_count: 0,
  evaluation: null,
};

describe("BackupPolicyEditor", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listBackupPolicies: vi.fn().mockResolvedValue(POLICIES),
      listBackupRepositories: vi.fn().mockResolvedValue(REPOSITORIES),
      listDeviceGroups: vi.fn().mockResolvedValue(DEVICE_GROUP_PAGE),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  // The reading half of the group picker: a policy scoped to a group named
  // its raw id, in mono, beside every other row's plain-language scope.
  it("lists a scoped policy's group by name rather than its id", async () => {
    api.listBackupPolicies = vi.fn().mockResolvedValue([
      { ...(POLICIES[0] as BackupPolicy), device_group_id: "grp-build" },
    ]);
    renderWithProviders(<BackupPolicyEditor />);

    expect(await screen.findByText("Build runners")).toBeInTheDocument();
    expect(screen.queryByText("grp-build")).not.toBeInTheDocument();
  });

  it("names an org-wide policy's scope as 'All devices'", async () => {
    renderWithProviders(<BackupPolicyEditor />);

    expect(await screen.findByText("All devices")).toBeInTheDocument();
  });

  // CYCLE 9. The pasted "Device group ID (blank = org-wide)" field is a
  // picker now, fed by the same endpoint the Device groups page lists from,
  // with the blank scope offered as an explicit choice rather than an empty
  // box.
  it("offers device groups from a picker, with All devices as an explicit choice", async () => {
    const user = userEvent.setup();
    renderWithProviders(<BackupPolicyEditor />);

    await user.click(await screen.findByRole("button", { name: "New policy" }));
    const field = screen.getByLabelText("Applies to");
    expect(field).toHaveAttribute("role", "combobox");
    expect(field).toHaveValue("All devices");

    await user.click(field);
    expect(await screen.findByRole("option", { name: /^All devices/ })).toBeInTheDocument();
    expect(await screen.findByRole("option", { name: /Build runners/ })).toBeInTheDocument();
  });

  it("posts the id of the device group that was picked", async () => {
    api.upsertBackupPolicy = vi.fn().mockResolvedValue(POLICIES[0]);
    const user = userEvent.setup();
    renderWithProviders(<BackupPolicyEditor />);

    await user.click(await screen.findByRole("button", { name: "New policy" }));
    await pickEntity(user, "Applies to", /Build runners/);
    await pickEntity(user, "Repository", /primary-vault/);
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(api.upsertBackupPolicy).toHaveBeenCalled());
    const [sent] = (api.upsertBackupPolicy as ReturnType<typeof vi.fn>).mock.calls[0]!;
    expect(sent.device_group_id).toBe("grp-build");
  });

  it("posts null for the device group when All devices is left chosen", async () => {
    api.upsertBackupPolicy = vi.fn().mockResolvedValue(POLICIES[0]);
    const user = userEvent.setup();
    renderWithProviders(<BackupPolicyEditor />);

    await user.click(await screen.findByRole("button", { name: "New policy" }));
    await pickEntity(user, "Repository", /primary-vault/);
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(api.upsertBackupPolicy).toHaveBeenCalled());
    const [sent] = (api.upsertBackupPolicy as ReturnType<typeof vi.fn>).mock.calls[0]!;
    expect(sent.device_group_id).toBeNull();
  });

  // The reading half of cycle 6: the Repository column was `repository_id`,
  // a raw UUID in the one column that says where a policy writes.
  it("lists policies naming their repository rather than its UUID", async () => {
    renderWithProviders(<BackupPolicyEditor />);
    expect(await screen.findByText("primary-vault")).toBeInTheDocument();
    expect(screen.queryByText("repo1")).not.toBeInTheDocument();
    // The state is a kit pill now, which humanises the wire word; scoped to
    // the row because the tile above the list carries the same word.
    const row = screen.getByText("primary-vault").closest("tr") as HTMLElement;
    expect(within(row).getByText("Enabled")).toBeInTheDocument();
  });

  // REVIEW WAVE, round 1, /backup/policies (copy): the Window column printed
  // "0 1-4 * * *" beside the plain-language "Always open", so one column
  // spoke two languages and one of them was crontab. The editor paints the
  // same value on a 7x24 grid, so the list was the only place a reader met
  // the expression at all.
  it("writes the window in the words the painter uses, not in crontab", async () => {
    renderWithProviders(<BackupPolicyEditor />);

    await screen.findByText("primary-vault");
    const row = screen.getByText("primary-vault").closest("tr") as HTMLElement;
    expect(within(row).getByText("Every day, 02:00 to 03:00")).toBeInTheDocument();
    expect(within(row).queryByText("0 2 * * *")).not.toBeInTheDocument();
  });

  it("hides the New policy control when the caller lacks backup.policy.manage", async () => {
    api.me = vi.fn().mockResolvedValue({
      ...ME,
      permissions: ME.permissions.filter((p) => p !== "backup.policy.manage"),
    });
    renderWithProviders(<BackupPolicyEditor />);
    await screen.findByText("primary-vault");
    expect(screen.queryByRole("button", { name: "New policy" })).not.toBeInTheDocument();
  });

  // ------------------------------------------------------------------------
  // CYCLES 6 AND 7 in the policy dialog. On a fresh install the Repository
  // `<select>` held only its own placeholder, so a policy could not be saved
  // and nothing said why — the /provisioning BLOCKER's shape, on the module
  // where an unsaveable policy means nothing is being backed up.
  // ------------------------------------------------------------------------

  it("offers repositories as a searchable list carrying their storage ref and state", async () => {
    const user = userEvent.setup();
    renderWithProviders(<BackupPolicyEditor />);

    await user.click(await screen.findByRole("button", { name: "New policy" }));
    const field = screen.getByLabelText("Repository");
    expect(field).toHaveAttribute("role", "combobox");

    await user.click(field);
    expect(
      await screen.findByRole("option", {
        name: /primary-vault\s*s3:\/\/backups\/primary · active/,
      }),
    ).toBeInTheDocument();
  });

  it("says what would fill the repository list, and links to where, when none exist", async () => {
    api.listBackupRepositories = vi.fn().mockResolvedValue([]);
    const user = userEvent.setup();
    renderWithProviders(<BackupPolicyEditor />);

    await user.click(await screen.findByRole("button", { name: "New policy" }));
    expect(screen.getByRole("link", { name: /Create a backup repository/ })).toHaveAttribute(
      "href",
      "/backup/repositories",
    );

    await user.click(screen.getByLabelText("Repository"));
    expect(await screen.findByText(/No backup repositories exist\./)).toBeInTheDocument();
  });

  it("posts the id of the repository that was picked", async () => {
    api.upsertBackupPolicy = vi.fn().mockResolvedValue(POLICIES[0]);
    const user = userEvent.setup();
    renderWithProviders(<BackupPolicyEditor />);

    await user.click(await screen.findByRole("button", { name: "New policy" }));
    await pickEntity(user, "Repository", /primary-vault/);
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(api.upsertBackupPolicy).toHaveBeenCalled());
    const [sent] = (api.upsertBackupPolicy as ReturnType<typeof vi.fn>).mock.calls[0]!;
    expect(sent.repository_id).toBe("repo1");
  });

  it("says it is loading rather than showing an empty table", async () => {
    api.listBackupPolicies = vi.fn(() => new Promise<never>(() => {}));
    renderWithProviders(<BackupPolicyEditor />);
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
  });

  it("says the request FAILED rather than showing an empty table", async () => {
    api.listBackupPolicies = vi.fn().mockRejectedValue(new Error("boom"));
    renderWithProviders(<BackupPolicyEditor />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  it("says there are no policies when there genuinely are none", async () => {
    api.listBackupPolicies = vi.fn().mockResolvedValue([]);
    renderWithProviders(<BackupPolicyEditor />);
    expect(await screen.findByText("No backup policies configured.")).toBeInTheDocument();
  });
});

describe("backup state vocabulary", () => {
  it("maps real backend backup_runs states to non-default badge variants", () => {
    expect(backupRunStateVariant("success")).toBe("success");
    expect(backupRunStateVariant("failed")).toBe("destructive");
    expect(backupRunStateVariant("running")).toBe("info");
    expect(backupRunStateVariant("pending")).toBe("muted");
    // Invented vocabulary must not resolve specially — this is the Phase-5
    // regression this test guards against.
    expect(backupRunStateVariant("not_a_real_state")).toBe("muted");
  });

  it("maps real backend backup_points states to non-default badge variants", () => {
    expect(backupPointStateVariant("complete")).toBe("success");
    expect(backupPointStateVariant("failed")).toBe("destructive");
    expect(backupPointStateVariant("pruned")).toBe("muted");
    expect(backupPointStateVariant("not_a_real_state")).toBe("muted");
  });

  it("maps real backend restore_jobs states to non-default badge variants", () => {
    expect(restoreJobStateVariant("success")).toBe("success");
    expect(restoreJobStateVariant("failed")).toBe("destructive");
    expect(restoreJobStateVariant("running")).toBe("info");
    expect(restoreJobStateVariant("pending")).toBe("muted");
    expect(restoreJobStateVariant("not_a_real_state")).toBe("muted");
  });

  it("maps real backend backup_repositories states to non-default badge variants", () => {
    expect(repositoryStateVariant("active")).toBe("success");
    expect(repositoryStateVariant("disabled")).toBe("muted");
    expect(repositoryStateVariant("not_a_real_state")).toBe("muted");
  });
});


// ==========================================================================
// CYCLE 5 — one datetime format, in the operator's zone.
//
// Every site below rendered `new Date(x).toLocaleString()`: the browser's
// zone (never the profile's) and the browser's format. Auditor B filed the
// consequence as a BLOCKER — "every datetime in the product is displayed 5
// hours earlier than it was entered" — and auditor A the milder half, one
// device reporting two different times in two different formats on two
// screens.
//
// The assertions are exact strings on purpose. A test that only checked "some
// date is present" passes against `toLocaleString()` and is therefore worse
// than none.
// ==========================================================================

describe("backup datetimes render in the operator's profile zone", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_NEW_YORK),
      listBackupPoints: vi.fn().mockResolvedValue(POINTS),
      listBackupRepositories: vi.fn().mockResolvedValue(REPOSITORIES),
      runBackup: vi.fn().mockResolvedValue({ job_ids: ["j1"] }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  // 2026-01-02T00:00:00Z is 2026-01-01 19:00 in New York — the five hours,
  // in the direction the operator actually notices.
  it("shows a backup point's taken_at in the profile zone, not the browser's", async () => {
    renderWithProviders(<BackupPointsPage />);
    expect(await screen.findByText("01 Jan 2026, 19:00")).toBeInTheDocument();
  });

  it("moves that same instant when the profile zone changes", async () => {
    api.me = vi.fn().mockResolvedValue(ME_TOKYO);
    renderWithProviders(<BackupPointsPage />);
    expect(await screen.findByText("02 Jan 2026, 09:00")).toBeInTheDocument();
  });

  it("shows a repository's created_at in the profile zone", async () => {
    renderWithProviders(<RepositoriesPage />);
    // 2026-01-01T00:00:00Z is the previous YEAR in New York. A date rendered
    // in the wrong zone is off by a whole day, which is harder to notice than
    // five hours — this is the case that makes it visible.
    expect(await screen.findByText("31 Dec 2025, 19:00")).toBeInTheDocument();
  });
});

// ==========================================================================
// CYCLE 4 — every failure that reaches a user is a sentence in a role="alert".
//
// Three dialogs rendered `{String(error)}` into an unannounced <p>. `ApiError`
// is built as `new ApiError(status, await response.text())`, so a bodyless
// 503 — which a proxy's 503 generally is — carries `message === ""` and
// `String(error)` produced the bare string "Error: ". That is the BLOCKER
// auditor A found: a failed destructive call that the UI reports as nothing.
// ==========================================================================

describe("CreateRepositoryDialog failure and validation", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_NEW_YORK),
      listBackupRepositories: vi.fn().mockResolvedValue(REPOSITORIES),
      createBackupRepository: vi.fn().mockResolvedValue(REPOSITORIES[0]),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  async function openCreateDialog(user: ReturnType<typeof userEvent.setup>) {
    renderWithProviders(<RepositoriesPage />);
    await screen.findByText("primary-vault");
    await user.click(screen.getByRole("button", { name: "New repository" }));
    await screen.findByRole("dialog", { name: "New repository" });
  }

  it("renders a sentence for a bodyless 503 instead of the string \"Error: \"", async () => {
    const user = userEvent.setup();
    api.createBackupRepository = vi.fn().mockRejectedValue(new ApiError(503, ""));
    await openCreateDialog(user);

    await user.type(screen.getByLabelText("Name"), "cold-vault");
    await user.click(screen.getByRole("button", { name: "Create" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/unavailable right now/);
    expect(alert.textContent).not.toContain("Error:");
  });

  it("translates a FastAPI 422 rather than printing its JSON", async () => {
    const user = userEvent.setup();
    api.createBackupRepository = vi.fn().mockRejectedValue(
      new ApiError(
        422,
        JSON.stringify({
          detail: [{ type: "string_too_short", loc: ["body", "name"], msg: "String should have at least 1 character", input: "" }],
        }),
      ),
    );
    await openCreateDialog(user);

    await user.type(screen.getByLabelText("Name"), "x");
    await user.click(screen.getByRole("button", { name: "Create" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Name: String should have at least 1 character");
    expect(alert.textContent).not.toContain("string_too_short");
  });

  // The silent no-op: `disabled={create.isPending || !name}` swallowed the
  // click and named no field.
  it("names the empty Name on submit instead of disabling Create", async () => {
    const user = userEvent.setup();
    await openCreateDialog(user);

    const createButton = screen.getByRole("button", { name: "Create" });
    expect(createButton).toBeEnabled();
    await user.click(createButton);

    expect(await screen.findByRole("alert")).toHaveTextContent("Name is required");
    expect(api.createBackupRepository).not.toHaveBeenCalled();
  });

  it("does not create the repository when Cancel is pressed inside the form", async () => {
    // `ui.tsx`'s Button sets no type, so an untyped button inside the new
    // <form> would submit it — Cancel would create the repository.
    const user = userEvent.setup();
    await openCreateDialog(user);

    await user.type(screen.getByLabelText("Name"), "cold-vault");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(api.createBackupRepository).not.toHaveBeenCalled();
  });

  it("does not carry a previous failure into the next opening of the dialog", async () => {
    const user = userEvent.setup();
    api.createBackupRepository = vi.fn().mockRejectedValue(new ApiError(503, ""));
    await openCreateDialog(user);

    await user.type(screen.getByLabelText("Name"), "cold-vault");
    await user.click(screen.getByRole("button", { name: "Create" }));
    await screen.findByRole("alert");

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.click(screen.getByRole("button", { name: "New repository" }));
    await screen.findByRole("dialog", { name: "New repository" });

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("PolicyDialog failure, validation and the edit it used to blank", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_NEW_YORK),
      listBackupPolicies: vi.fn().mockResolvedValue(POLICIES),
      listBackupRepositories: vi.fn().mockResolvedValue(REPOSITORIES),
      upsertBackupPolicy: vi.fn().mockResolvedValue(POLICIES[0]),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("renders a sentence for a bodyless 503 instead of the string \"Error: \"", async () => {
    const user = userEvent.setup();
    api.upsertBackupPolicy = vi.fn().mockRejectedValue(new ApiError(503, ""));
    renderWithProviders(<BackupPolicyEditor />);

    await screen.findByText("primary-vault");
    await user.click(screen.getByRole("button", { name: "New policy" }));
    await pickEntity(user, "Repository", /primary-vault/);
    await user.click(screen.getByRole("button", { name: "Save" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/unavailable right now/);
    expect(alert.textContent).not.toContain("Error:");
  });

  it("names the missing Repository on submit instead of disabling Save", async () => {
    const user = userEvent.setup();
    renderWithProviders(<BackupPolicyEditor />);

    await screen.findByText("primary-vault");
    await user.click(screen.getByRole("button", { name: "New policy" }));

    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeEnabled();
    await user.click(save);

    expect(await screen.findByRole("alert")).toHaveTextContent("Repository is required");
    expect(api.upsertBackupPolicy).not.toHaveBeenCalled();
  });

  // The dialog was never unmounted, so its `useState(existing?.…)` initialisers
  // ran ONCE with `policy === null`. Clicking Edit therefore opened a blank
  // form over a real policy, and saving wrote `window_cron: null` over it.
  it("opens Edit with the policy's own values rather than a blank form", async () => {
    const user = userEvent.setup();
    renderWithProviders(<BackupPolicyEditor />);

    await screen.findByText("primary-vault");
    await user.click(screen.getByRole("button", { name: "Edit" }));

    // The window is painted on a grid now; the expression itself lives in
    // the painter's "Cron expression" field, which is what carries the value.
    expect(await screen.findByLabelText("Cron expression")).toHaveValue("0 2 * * *");
    // The picker shows the repository's NAME for a stored id -- never the id
    // itself. `EntityPicker` deliberately does not fall back to the raw value:
    // "a value nobody can see is a value nobody chose".
    expect(screen.getByLabelText("Repository")).toHaveValue("primary-vault");
  });

  it("saves the edited policy's window rather than nulling it", async () => {
    const user = userEvent.setup();
    renderWithProviders(<BackupPolicyEditor />);

    await screen.findByText("primary-vault");
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await screen.findByLabelText("Cron expression");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(api.upsertBackupPolicy).toHaveBeenCalledWith(
      expect.objectContaining({ repository_id: "repo1", window_cron: "0 2 * * *" }),
    );
  });
});

describe("RestoreDialog failure reporting", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_NEW_YORK),
      listBackupPoints: vi.fn().mockResolvedValue(POINTS),
      listDevices: vi.fn().mockResolvedValue(DEVICES),
      listBackupRepositories: vi.fn().mockResolvedValue(REPOSITORIES),
      getBackupReport: vi.fn().mockResolvedValue(EMPTY_REPORT),
      getBackupManifest: vi.fn().mockResolvedValue(MANIFEST),
      restoreBackupPoint: vi.fn().mockResolvedValue({ job_ids: ["r1"] }),
      verifyBackupPoint: vi.fn().mockResolvedValue({ job_ids: ["v1"] }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  function dialogRestoreButton(): HTMLElement {
    const buttons = screen.getAllByRole("button", { name: "Restore" });
    const last = buttons.at(-1);
    if (!last) throw new Error("expected a Restore button in the dialog");
    return last;
  }

  async function openAndSelect(user: ReturnType<typeof userEvent.setup>) {
    renderWithProviders(<BackupPointsPage />);
    await screen.findByText("pt1");
    await user.click(screen.getByRole("button", { name: "Restore" }));
    await screen.findByText("/etc/app/config.yaml");
    await user.click(screen.getByRole("checkbox"));
  }

  // AUDITOR A'S BLOCKER, on the exact call it was found on: a destructive
  // request whose 503 the UI swallowed.
  it("announces a bodyless 503 from a restore instead of rendering \"Error: \"", async () => {
    const user = userEvent.setup();
    api.restoreBackupPoint = vi.fn().mockRejectedValue(new ApiError(503, ""));
    await openAndSelect(user);

    await user.click(dialogRestoreButton());
    await user.click(await screen.findByRole("button", { name: "Restore files" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/unavailable right now/);
    expect(alert.textContent).not.toContain("Error:");
  });

  it("announces a 403 from a verify as a permission sentence", async () => {
    const user = userEvent.setup();
    api.verifyBackupPoint = vi.fn().mockRejectedValue(new ApiError(403, ""));
    await openAndSelect(user);

    await user.click(screen.getByRole("button", { name: "Verify" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "You do not have permission to do that.",
    );
  });

  // Both actions render through ONE element, and only the losing one is
  // reset. A failed Verify therefore sat on screen while a Restore was in
  // flight, so the operator read a refusal that belonged to a request they
  // had already moved on from. The restore below never settles, which is
  // exactly the window in which the stale sentence used to be visible.
  it("clears the failed Verify's sentence while the next Restore is in flight", async () => {
    const user = userEvent.setup();
    api.verifyBackupPoint = vi.fn().mockRejectedValue(new ApiError(403, ""));
    api.restoreBackupPoint = vi.fn(() => new Promise<never>(() => {}));
    await openAndSelect(user);

    await user.click(screen.getByRole("button", { name: "Verify" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/permission/);

    await user.click(dialogRestoreButton());
    await user.click(await screen.findByRole("button", { name: "Restore files" }));

    await screen.findByRole("button", { name: "Restoring…" });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

// ==========================================================================
// DeviceBackupTab — six ad-hoc dates, a swallowed failure, and a success that
// said nothing at all.
// ==========================================================================

const DEVICE_STATUS: DeviceBackupStatus = {
  device_id: "dev1",
  last_good_point: {
    backup_point_id: "pt1",
    taken_at: "2026-01-02T00:00:00.000Z",
    size: 1024,
    repository_id: "repo1",
  },
  last_run_state: "success",
  last_run_finished_at: "2026-01-02T00:30:00.000Z",
  verified_at: null,
  restore_tested_at: null,
  repository: { id: "repo1", name: "primary-vault" },
};

describe("DeviceBackupTab", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME_NEW_YORK),
      getDeviceBackup: vi.fn().mockResolvedValue(DEVICE_STATUS),
      runBackup: vi.fn().mockResolvedValue({ job_ids: ["j1"] }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("renders every date in the profile zone and one format", async () => {
    renderWithProviders(<DeviceBackupTab deviceId="dev1" />);

    expect(await screen.findByText("01 Jan 2026, 19:30")).toBeInTheDocument();
    expect(screen.getByText("01 Jan 2026, 19:00")).toBeInTheDocument();
  });

  it("renders an absent date as an em dash, never \"Invalid Date\"", async () => {
    api.getDeviceBackup = vi.fn().mockResolvedValue({
      ...DEVICE_STATUS,
      last_run_finished_at: null,
      last_good_point: null,
    });
    renderWithProviders(<DeviceBackupTab deviceId="dev1" />);

    await screen.findByText("primary-vault");
    expect(screen.queryByText(/Invalid Date/)).not.toBeInTheDocument();
    expect(screen.getAllByText(NO_DATE).length).toBeGreaterThanOrEqual(2);
  });

  it("says what the run dispatched instead of reporting nothing", async () => {
    const user = userEvent.setup();
    renderWithProviders(<DeviceBackupTab deviceId="dev1" />);

    await screen.findByText("primary-vault");
    await user.click(screen.getByRole("button", { name: "Run now" }));

    expect(await screen.findByRole("status")).toHaveTextContent("Backup queued — 1 job dispatched.");
  });

  it("does not round a run that dispatched no job up to a started backup", async () => {
    const user = userEvent.setup();
    api.runBackup = vi.fn().mockResolvedValue({ job_ids: [] });
    renderWithProviders(<DeviceBackupTab deviceId="dev1" />);

    await screen.findByText("primary-vault");
    await user.click(screen.getByRole("button", { name: "Run now" }));

    expect(await screen.findByRole("status")).toHaveTextContent(/Nothing was dispatched/);
  });

  it("announces a bodyless 503 from Run now instead of rendering \"Error: \"", async () => {
    const user = userEvent.setup();
    api.runBackup = vi.fn().mockRejectedValue(new ApiError(503, ""));
    renderWithProviders(<DeviceBackupTab deviceId="dev1" />);

    await screen.findByText("primary-vault");
    await user.click(screen.getByRole("button", { name: "Run now" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/unavailable right now/);
    expect(alert.textContent).not.toContain("Error:");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("hides Run now when the caller lacks backup.run", async () => {
    api.me = vi.fn().mockResolvedValue({
      ...ME_NEW_YORK,
      permissions: ME.permissions.filter((p) => p !== "backup.run"),
    });
    renderWithProviders(<DeviceBackupTab deviceId="dev1" />);

    await screen.findByText("primary-vault");
    expect(screen.queryByRole("button", { name: "Run now" })).not.toBeInTheDocument();
  });
});

// ==========================================================================
// The stat cards, the page copy, and the columns an operator reads
// ==========================================================================

describe("the backup overview reports what it knows and no more", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listBackupPoints: vi.fn().mockResolvedValue(POINTS),
      listDevices: vi.fn().mockResolvedValue(DEVICES),
      listBackupRepositories: vi.fn().mockResolvedValue(REPOSITORIES),
      getBackupReport: vi.fn().mockResolvedValue(EMPTY_REPORT),
      runBackup: vi.fn().mockResolvedValue({ job_ids: ["j1"] }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  // Auditor A (MEDIUM): "'0%' is a computed rate over zero runs —
  // mathematically undefined, and displayed identically to a genuine 0%
  // success rate, which would be a catastrophic condition."
  it("says 'Never run' rather than 0% before any backup has executed", async () => {
    renderWithProviders(<BackupPointsPage />);

    expect(await screen.findByText("Never run")).toBeInTheDocument();
    expect(screen.queryByText("0%")).not.toBeInTheDocument();
  });

  it("shows the real success rate once runs exist", async () => {
    api.getBackupReport = vi.fn().mockResolvedValue(RUN_REPORT);
    renderWithProviders(<BackupPointsPage />);

    expect(await screen.findByText("50%")).toBeInTheDocument();
    expect(screen.queryByText("Never run")).not.toBeInTheDocument();
  });

  // The OTHER half of the same MEDIUM: "surface '0 of 1 endpoints protected'
  // as an alert state rather than a neutral statistic". "Never run" fixes the
  // undefined rate; this fixes the card beside it, which reported an entirely
  // unprotected fleet in the same neutral treatment as a fully covered one.
  // THE TREATMENT MOVED, THE FINDING DID NOT. `gold-accent` and the `gold`
  // boolean are gone; a tile now carries its category as `tone-*` and its
  // urgency as `urgent`. Coverage is a gold-category number whether or not
  // the gap is open — the hue says what the number is about — so `urgent` is
  // the class that has to appear and disappear here, and it is what the audit
  // actually asked for: an unprotected fleet reading as an alert rather than
  // a neutral statistic.
  it("flags an unprotected fleet rather than reporting it as a neutral count", async () => {
    renderWithProviders(<BackupPointsPage />);

    // devices_covered 0 of devices_total 1 — the audit's exact card. Wait for
    // the report to settle first; before it does every card reads "—".
    await screen.findByText("Never run");
    const card = screen.getByText("Protected endpoints").closest("a");
    expect(card).toHaveTextContent("0 / 1");
    expect(card).toHaveClass("tone-gold");
    expect(card).toHaveClass("urgent");
  });

  it("does not flag a fleet whose every endpoint is covered", async () => {
    // The pair is what makes the flag an observation rather than a class that
    // is always on: RUN_REPORT covers 1 of 1.
    api.getBackupReport = vi.fn().mockResolvedValue(RUN_REPORT);
    renderWithProviders(<BackupPointsPage />);

    await screen.findByText("50%");
    const card = screen.getByText("Protected endpoints").closest("a");
    expect(card).toHaveTextContent("1 / 1");
    expect(card).not.toHaveClass("urgent");
  });

  // Auditor A (HIGH): "Production page copy tells the user to read the page
  // source to find out what works."
  it("does not tell the reader to open the page source", async () => {
    renderWithProviders(<BackupPointsPage />);

    await screen.findByText("pt1");
    expect(document.body.textContent).not.toMatch(/module notes in this page's source/);
    expect(document.body.textContent).not.toMatch(/page's source/);
  });

  it("names the device and repository on each point instead of printing UUIDs", async () => {
    renderWithProviders(<BackupPointsPage />);

    await screen.findByText("pt1");
    expect(screen.getByText("WKS-042")).toBeInTheDocument();
    expect(screen.getByText("primary-vault")).toBeInTheDocument();
    expect(screen.queryByText(DEVICE_UUID)).not.toBeInTheDocument();
    expect(screen.queryByText("repo1")).not.toBeInTheDocument();
  });

  it("renders a point's size as a readable quantity, not a raw byte count", async () => {
    renderWithProviders(<BackupPointsPage />);

    await screen.findByText("pt1");
    expect(screen.getByText("1.0 KiB")).toBeInTheDocument();
    expect(screen.queryByText("1024")).not.toBeInTheDocument();
  });
});

describe("byte sizes", () => {
  // THIS MODULE NO LONGER OWNS THE FORMATTER. It briefly shipped its own
  // `bytes.ts` while the shared one was landing in `lib/format.ts`, which is
  // the exact defect that motivated the shared one: the audit found a stat
  // card rendering "15.0 GiB" beside a table printing the same field raw, and
  // concluded "the formatter exists and simply is not applied here". Two
  // formatters that round differently is that finding with an extra step.
  //
  // Two contract changes came with the merge, both deliberate. Sub-KiB reads
  // "512 B" rather than "512 bytes", so a column does not mix a word with five
  // symbols. And an unmeasurable size returns "" rather than an em dash: the
  // CALLER decides how absence looks, because only the caller knows what is
  // missing. BackupPointsPage renders <NoValue> with that sentence.

  it("scales into 1024-based units and labels them unambiguously", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1024)).toBe("1.0 KiB");
    expect(formatBytes(1536)).toBe("1.5 KiB");
    expect(formatBytes(42949672960)).toBe("40.0 GiB");
  });

  it("agrees with the stat card the audit measured", () => {
    // The whole reason the boundary sits at 100 rather than 10.
    expect(formatBytes(16093573120)).toBe("15.0 GiB");
    expect(formatBytes(256022937600)).toBe("238 GiB");
  });

  it("keeps zero as a measurement rather than rendering it as absent", () => {
    // An empty backup point is a finding, not a missing value.
    expect(formatBytes(0)).toBe("0 B");
  });

  it("returns nothing for a size that was never measured", () => {
    expect(formatBytes(null)).toBe("");
    expect(formatBytes(undefined)).toBe("");
    expect(formatBytes(-1)).toBe("");
    expect(formatBytes(Number.NaN)).toBe("");
  });
});

describe("backup picker options", () => {
  it("distinguishes devices by os family and whether an agent can answer", () => {
    const options = devicePickerOptions(DEVICES.items);
    expect(options[0]).toEqual({
      id: DEVICE_UUID,
      label: "WKS-042",
      sublabel: "windows · online",
    });
    expect(options[1]?.sublabel).toBe("linux · offline");
  });

  it("says 'no agent' rather than leaving an empty separator for an asset with none", () => {
    // A switch or a machine still in a box: nothing signed can ever reach it,
    // which is the fact a person aiming a backup needs.
    expect(devicePickerOptions(DEVICES.items)[2]?.sublabel).toBe("no agent");
  });

  it("distinguishes repositories by the storage they write to and their state", () => {
    expect(repositoryPickerOptions(REPOSITORIES)[0]).toEqual({
      id: "repo1",
      label: "primary-vault",
      sublabel: "s3://backups/primary · active",
    });
  });

  it("resolves an id to a name, and an unknown id to itself rather than to blank", () => {
    const options = devicePickerOptions(DEVICES.items);
    expect(nameForOption(options, DEVICE_UUID)).toBe("WKS-042");
    expect(nameForOption(options, "gone")).toBe("gone");
    expect(nameForOption(options, null)).toBeNull();
  });

  it("names both the surface that fills a list and the permission that may hide it", () => {
    // Cycle 7: a hint that asserts "there are none" lies to a reader who
    // simply may not list them.
    expect(DEVICE_PICKER_EMPTY_HINT).toMatch(/Devices → Add a computer/);
    expect(DEVICE_PICKER_EMPTY_HINT).toMatch(/permission/);
    expect(REPOSITORY_PICKER_EMPTY_HINT).toMatch(/Repositories tab/);
    expect(REPOSITORY_PICKER_EMPTY_HINT).toMatch(/permission/);
  });
});

describe("the device Backup tab reads the same as the points table", () => {
  let api: Partial<ApiClient>;

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      getDeviceBackup: vi.fn().mockResolvedValue({
        ...DEVICE_STATUS,
        last_run_state: "running",
      }),
      runBackup: vi.fn().mockResolvedValue({ job_ids: ["j1"] }),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  // The raw wire value here and a humanised one on /backup is how one module
  // comes to contradict itself between two views — the shape of Auditor A's
  // "six tabs render 'no data' six different ways".
  it("humanises the last run state rather than printing the wire value", async () => {
    renderWithProviders(<DeviceBackupTab deviceId="dev1" />);

    expect(await screen.findByText("Running")).toBeInTheDocument();
    expect(screen.queryByText("running")).not.toBeInTheDocument();
  });
});


// ==========================================================================
// NO DEAD TILES.
//
// The operator's instruction was "all tiles and all numbers or kpis need to be
// clickable and direct the user to the specific menus, i dont want dead tiles
// everywhere". `StatCard` requires a `to`, so the compiler catches the missing
// link; what it CANNOT catch is a link that goes nowhere useful — a path with
// a typo in it, or a filter the destination page does not read. Both render
// as a working anchor and both are dead on arrival.
//
// So these assert three separable things:
//   1. the tile is an <a> with an href (keyboard-reachable, middle-clickable,
//      pasteable into an incident channel);
//   2. the path exists in App.tsx's route table;
//   3. where the href carries a filter this cluster owns, the page it lands
//      on actually narrows to the rows the tile counted.
// ==========================================================================

describe("every backup tile opens the rows it counted", () => {
  let api: Partial<ApiClient>;

  // Two points: one complete-but-unverified (what "Needs verification"
  // counts, per reporting.py's `state == complete AND verified_at IS NULL`)
  // and one already verified.
  const VERIFY_POINTS: BackupPoint[] = [
    POINTS[0] as BackupPoint,
    {
      ...(POINTS[0] as BackupPoint),
      id: "pt2",
      verified_at: "2026-01-03T00:00:00.000Z",
    },
  ];

  beforeEach(() => {
    api = {
      me: vi.fn().mockResolvedValue(ME),
      listBackupPoints: vi.fn().mockResolvedValue(VERIFY_POINTS),
      listDevices: vi.fn().mockResolvedValue(DEVICES),
      listBackupRepositories: vi.fn().mockResolvedValue(REPOSITORIES),
      getBackupReport: vi.fn().mockResolvedValue(RUN_REPORT),
      listBackupPolicies: vi.fn().mockResolvedValue(POLICIES),
    };
    mockUseApi.mockReturnValue(api as ApiClient);
  });

  it("links every overview tile at a route the application has", async () => {
    renderWithProviders(<BackupPointsPage />);
    await screen.findByText("50%");

    expectLiveTiles([
      "/jobs?q=backup",
      "/devices",
      "/jobs?status=failed&q=backup",
      "/backup?verify=pending",
    ]);
  });

  // THE OTHER HALF OF "NOT DEAD": the destination has to do something.
  it("narrows the points table to the unverified points the tile counted", async () => {
    renderWithProviders(<BackupPointsPage />, "/backup?verify=pending");

    expect(await screen.findByText("pt1")).toBeInTheDocument();
    expect(screen.queryByText("pt2")).not.toBeInTheDocument();
  });

  it("shows every point when the filter is absent", async () => {
    renderWithProviders(<BackupPointsPage />);

    expect(await screen.findByText("pt1")).toBeInTheDocument();
    expect(screen.getByText("pt2")).toBeInTheDocument();
  });

  // A filter that matches none of several is NOT an empty estate, and saying
  // "No backup points recorded yet" to an operator who has fifty would be the
  // same class of lie as a loading table rendering as an empty one.
  it("distinguishes a filter that matched nothing from having no points", async () => {
    api.listBackupPoints = vi.fn().mockResolvedValue([VERIFY_POINTS[1] as BackupPoint]);
    renderWithProviders(<BackupPointsPage />, "/backup?verify=pending");

    expect(
      await screen.findByText("No backup point is awaiting verification."),
    ).toBeInTheDocument();
  });

  it("links every repositories tile at a route the application has", async () => {
    renderWithProviders(<RepositoriesPage />, "/backup/repositories");
    await screen.findByText("primary-vault");

    expectLiveTiles([
      "/backup/repositories",
      "/backup/repositories?state=active",
      "/backup/repositories?state=disabled",
    ]);
  });

  it("narrows the repository table to the state the tile counted", async () => {
    api.listBackupRepositories = vi.fn().mockResolvedValue([
      REPOSITORIES[0] as Repository,
      { ...(REPOSITORIES[0] as Repository), id: "repo2", name: "cold-vault", state: "disabled" },
    ]);
    renderWithProviders(<RepositoriesPage />, "/backup/repositories?state=disabled");

    expect(await screen.findByText("cold-vault")).toBeInTheDocument();
    expect(screen.queryByText("primary-vault")).not.toBeInTheDocument();
  });

  it("links every policies tile at a route the application has", async () => {
    renderWithProviders(<BackupPolicyEditor />, "/backup/policies");
    await screen.findByText("primary-vault");

    // Three tiles since the review wave's round 2: the disabled slice is the
    // third, and it opens the rows the filter below it also reaches.
    expectLiveTiles([
      "/backup/policies",
      "/backup/policies?enabled=true",
      "/backup/policies?enabled=false",
    ]);
  });

  it("narrows the policy table to the enabled policies the tile counted", async () => {
    api.listBackupPolicies = vi.fn().mockResolvedValue([
      POLICIES[0] as BackupPolicy,
      { ...(POLICIES[0] as BackupPolicy), id: "pol2", window_cron: "0 5 * * *", enabled: false },
    ]);
    renderWithProviders(<BackupPolicyEditor />, "/backup/policies?enabled=true");

    // The window column speaks the painter's language now, so the rows are
    // identified by the sentence the painter would write for them.
    expect(await screen.findByText("Every day, 02:00 to 03:00")).toBeInTheDocument();
    expect(screen.queryByText("Every day, 05:00 to 06:00")).not.toBeInTheDocument();
  });

  // ZERO IS A MEASUREMENT, NOT AN ABSENCE. "0 failed" over a fleet that has
  // run backups and "0 failed" over a fleet that has never run one are the
  // same glyph and opposite facts; the audit found nine tiles reading 0 on a
  // page where nothing had been measured.
  it("says WHICH zero a zero failure count is", async () => {
    api.getBackupReport = vi.fn().mockResolvedValue(EMPTY_REPORT);
    renderWithProviders(<BackupPointsPage />);

    await screen.findByText("Never run");
    expect(screen.getByText("No backup run has ever executed")).toBeInTheDocument();
    expect(screen.queryByText("No backup run failed")).not.toBeInTheDocument();
  });

  it("says the other zero when runs have happened and none failed", async () => {
    api.getBackupReport = vi.fn().mockResolvedValue({
      ...RUN_REPORT,
      run_counts_by_state: { success: 2 },
      success_rate: 1,
    });
    renderWithProviders(<BackupPointsPage />);

    await screen.findByText("100%");
    expect(screen.getByText("No backup run failed")).toBeInTheDocument();
  });
});
