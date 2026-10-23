import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../../lib/api";
import type { Me } from "../../../lib/api";
import { BackupPointsPage } from "../BackupPointsPage";
import { CoveragePage } from "../CoveragePage";
import { OffsitePage, RecoveryKeyWizard } from "../OffsitePage";
import type { BackupPoint, OffsiteTarget, RecoveryKey, Repository } from "../types";

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function renderWithProviders(ui: ReactElement, entry = "/backup/offsite") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const ADMIN: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "admin@example.com",
  display_name: "Admin",
  permissions: ["backup.read", "backup.offsite.manage", "backup.recovery.manage"],
};

const KEY: RecoveryKey = {
  id: "k1",
  fingerprint: "0123456789abcdef0123456789abcdef",
  public_key: "cHVibGljLWtleS1ieXRlcy0wMTIzNDU2Nzg5YWJjZGVm",
  proven_at: null,
  challenge_expires_at: "2026-09-24T12:00:00Z",
  created_at: "2026-09-23T12:00:00Z",
  retired_at: null,
};

const TARGET: OffsiteTarget = {
  id: "t1",
  name: "Wasabi EU",
  endpoint: "https://s3.eu-central-1.wasabisys.com",
  region: "eu-central-1",
  bucket: "acme-offsite",
  prefix: "itms",
  addressing: "path",
  lock_mode: "compliance",
  immutable_days: 30,
  retention_gfs: { daily: 14 },
  max_mbps: null,
  state: "failing",
  test_requested_at: null,
  tested_at: "2026-09-23T12:00:00Z",
  test_steps: [
    {
      step: "object_lock",
      ok: false,
      detail: "This bucket does not have Object Lock turned on.",
    },
  ],
  last_ok_at: null,
  last_error: "This bucket does not have Object Lock turned on.",
};

const REPO: Repository = {
  id: "r1",
  name: "primary",
  storage_ref: "o1/backup/r1",
  state: "active",
  dedup_params: {},
  created_at: "2026-09-01T00:00:00Z",
  offsite_target_id: null,
};

function api(over: Record<string, unknown> = {}) {
  return {
    me: vi.fn().mockResolvedValue(ADMIN),
    listRecoveryKeys: vi.fn().mockResolvedValue([]),
    listOffsiteTargets: vi.fn().mockResolvedValue([TARGET]),
    listBackupRepositories: vi.fn().mockResolvedValue([REPO]),
    registerRecoveryKey: vi.fn().mockResolvedValue({ key: KEY, challenge: "Q0hBTExFTkdF" }),
    proveRecoveryKey: vi.fn().mockResolvedValue({ ...KEY, proven_at: "2026-09-23T12:05:00Z" }),
    newRecoveryKeyChallenge: vi.fn(),
    setRepositoryOffsite: vi.fn(),
    testOffsiteTarget: vi.fn().mockResolvedValue({ ...TARGET, state: "testing" }),
    runReport: vi.fn(),
    ...over,
  };
}

describe("recovery key wizard", () => {
  beforeEach(() => mockUseApi.mockReset());

  it("will not take the public key until the private key is confirmed stored", async () => {
    const client = api();
    mockUseApi.mockReturnValue(client);
    const user = userEvent.setup();
    renderWithProviders(<RecoveryKeyWizard open onClose={() => {}} />);

    expect(screen.getByText(/itms_recover.py keygen --out recovery.key/)).toBeInTheDocument();
    await user.type(screen.getByLabelText(/Public key/), "itms-recovery-public:QUJD");
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText(/two safe places first/)).toBeInTheDocument();
    expect(client.registerRecoveryKey).not.toHaveBeenCalled();
  });

  it("registers the key, shows the check, and confirms the answer", async () => {
    const client = api();
    mockUseApi.mockReturnValue(client);
    const user = userEvent.setup();
    renderWithProviders(<RecoveryKeyWizard open onClose={() => {}} />);

    await user.type(screen.getByLabelText(/Public key/), "itms-recovery-public:QUJD");
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "Next" }));

    expect(client.registerRecoveryKey).toHaveBeenCalledWith("itms-recovery-public:QUJD");
    expect(await screen.findByText(/prove --key recovery.key --challenge 'Q0hBTExFTkdF'/))
      .toBeInTheDocument();
    await user.type(screen.getByLabelText("Answer"), "YW5zd2Vy");
    await user.click(screen.getByRole("button", { name: "Confirm" }));
    expect(client.proveRecoveryKey).toHaveBeenCalledWith("k1", "YW5zd2Vy");
    expect(await screen.findByText(/recovery key is set up/)).toBeInTheDocument();
  });

  it("shows the server's reason when the answer does not match", async () => {
    const client = api({
      proveRecoveryKey: vi
        .fn()
        .mockRejectedValue(
          new ApiError(422, "That answer does not match. Check you used the right key."),
        ),
    });
    mockUseApi.mockReturnValue(client);
    const user = userEvent.setup();
    renderWithProviders(<RecoveryKeyWizard open onClose={() => {}} />);
    await user.type(screen.getByLabelText(/Public key/), "itms-recovery-public:QUJD");
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.type(await screen.findByLabelText("Answer"), "d3Jvbmc=");
    await user.click(screen.getByRole("button", { name: "Confirm" }));
    expect(await screen.findByText(/does not match/)).toBeInTheDocument();
  });
});

describe("off-site page", () => {
  beforeEach(() => mockUseApi.mockReset());

  it("says in plain words why a bucket is not usable, and offers no untested storage", async () => {
    mockUseApi.mockReturnValue(api());
    renderWithProviders(<OffsitePage />);

    expect(await screen.findByText("Not usable")).toBeInTheDocument();
    expect(screen.getAllByText(/does not have Object Lock/).length).toBeGreaterThan(0);
    expect(screen.getByText(/Object Lock is on/)).toBeInTheDocument();
    const select = await screen.findByLabelText("Off-site copy for primary");
    // Only storage that passed its test can be chosen.
    expect(within(select).queryByText("Wasabi EU")).toBeNull();
    expect(screen.getByText(/Off-site copies cannot be turned on until/)).toBeInTheDocument();
  });

  it("shows the server's refusal when off-site cannot be turned on", async () => {
    const client = api({
      listOffsiteTargets: vi.fn().mockResolvedValue([{ ...TARGET, state: "ok", last_error: null }]),
      setRepositoryOffsite: vi
        .fn()
        .mockRejectedValue(
          new ApiError(409, "Set up a recovery key and confirm it before turning on off-site copies."),
        ),
    });
    mockUseApi.mockReturnValue(client);
    const user = userEvent.setup();
    renderWithProviders(<OffsitePage />);

    const select = await screen.findByLabelText("Off-site copy for primary");
    await waitFor(() => expect(within(select).getByText("Wasabi EU")).toBeInTheDocument());
    await user.selectOptions(select, "t1");
    expect(client.setRepositoryOffsite).toHaveBeenCalledWith("r1", "t1");
    expect(await screen.findByRole("alert")).toHaveTextContent(/recovery key/);
  });

  it("never shows a storage secret field value from the server", async () => {
    mockUseApi.mockReturnValue(api());
    renderWithProviders(<OffsitePage />);
    await screen.findByText("Wasabi EU");
    expect(document.body.textContent).not.toMatch(/secret_access_key|access_key_id/);
  });
});

describe("backup points from format-2 agents", () => {
  beforeEach(() => mockUseApi.mockReset());

  it("says when a point is not crash-consistent, lists its warnings, and marks a sampled test", async () => {
    const point: BackupPoint = {
      id: "p1",
      device_id: "d1",
      repository_id: "r1",
      backup_run_id: "run1",
      taken_at: "2026-09-23T02:05:00Z",
      size: 1024,
      state: "complete",
      verified_at: null,
      restore_tested_at: "2026-09-23T03:00:00Z",
      restore_test_level: "read",
      restore_test_scope: "sampled",
      consistency_level: "live",
      crash_consistent: false,
      warnings: ["files_changed_during_read"],
    };
    mockUseApi.mockReturnValue(
      api({
        me: vi.fn().mockResolvedValue({ ...ADMIN, permissions: ["backup.read"] }),
        listBackupPoints: vi.fn().mockResolvedValue([point]),
        listDevices: vi.fn().mockResolvedValue({ items: [], total: 0, limit: 50, offset: 0 }),
        getBackupReport: vi.fn().mockResolvedValue({
          devices_total: 1,
          devices_covered: 1,
          coverage_rate: 1,
          run_counts_by_state: {},
          success_rate: 1,
          points_needing_verification: 1,
          dedup: { bytes_protected_total: 0, bytes_stored_total: 0, dedup_ratio: 0, savings_pct: 0 },
        }),
      }),
    );
    renderWithProviders(<BackupPointsPage />, "/backup");
    expect(
      await screen.findByText("Not crash-consistent: files were read while in use"),
    ).toBeInTheDocument();
    expect(screen.getByText("Some files changed while they were being copied.")).toBeInTheDocument();
    expect(screen.getByText("Tested (sample)")).toBeInTheDocument();
  });
});

describe("coverage page", () => {
  beforeEach(() => mockUseApi.mockReset());

  const PAGE = {
    meta: {},
    totals: { devices: 2, covered: 1, stale: 0, failing: 0, unprotected: 1 },
    rows: [
      {
        device_id: "d1",
        hostname: "FS-01",
        repository_name: "primary",
        coverage: "covered",
        offsite: "current",
        last_point_taken_at: "2026-09-23T02:05:00Z",
        last_offsite_at: "2026-09-23T03:00:00Z",
        last_restore_tested_at: null,
      },
      {
        device_id: "d2",
        hostname: "WEB-02",
        repository_name: null,
        coverage: "unprotected",
        offsite: "off",
        last_point_taken_at: null,
        last_offsite_at: null,
        last_restore_tested_at: null,
      },
    ],
  };

  it("names which devices are not protected", async () => {
    const client = api({ runReport: vi.fn().mockResolvedValue(PAGE) });
    mockUseApi.mockReturnValue(client);
    renderWithProviders(<CoveragePage />, "/backup/coverage");

    const row = (await screen.findByText("WEB-02")).closest("tr") as HTMLElement;
    expect(within(row).getByText("Not protected")).toBeInTheDocument();
    expect(within(row).getByText("No backup policy")).toBeInTheDocument();
    expect(client.runReport).toHaveBeenCalledWith("backup-coverage", { limit: "500" });
  });

  it("filters to one verdict from the address", async () => {
    mockUseApi.mockReturnValue(api({ runReport: vi.fn().mockResolvedValue(PAGE) }));
    renderWithProviders(<CoveragePage />, "/backup/coverage?coverage=unprotected");
    expect(await screen.findByText("WEB-02")).toBeInTheDocument();
    expect(screen.queryByText("FS-01")).toBeNull();
  });
});
