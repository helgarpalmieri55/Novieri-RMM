import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../../lib/api";
import { BackupReadiness, OffsiteReadiness, PolicyReadiness } from "../BackupReadiness";
import type { BackupPolicy, OffsiteTarget, RecoveryKey, Repository } from "../types";

// The cross-module panels on the Backup pages
// (`docs/design/cross-module-guidance.md` §2 Backup). Asserted as an operator
// reads them: the missing piece with a link to where it is fixed, nothing at
// all once everything is in place, and "cannot be checked" -- never
// "missing" -- for an account that may not look.

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ALL = ["backup.read", "devices.read"];

const REPO: Repository = {
  id: "repo-1",
  name: "Primary",
  backend: "s3",
  state: "active",
} as unknown as Repository;

function policy(overrides: Partial<BackupPolicy> = {}): BackupPolicy {
  return {
    id: "pol-1",
    client_id: "o1",
    device_group_id: null,
    repository_id: "repo-1",
    sources: {},
    window_cron: null,
    retention_gfs: {},
    enabled: true,
    ...overrides,
  };
}

function fleet(managed: number) {
  return {
    items: [],
    total: managed,
    state_counts: { all: 0, online: 0, offline: 0, assets: 0, certs: 0 },
    fleet: { total: managed, managed, online: managed, cert_attention: 0 },
  };
}

function stubApi(overrides: Record<string, unknown> & { permissions?: string[] } = {}) {
  const { permissions = ALL, ...rest } = overrides;
  const api = {
    me: vi.fn().mockResolvedValue({ user_id: "u1", org_id: "o1", email: "a@b.c", display_name: "A", permissions }),
    listBackupRepositories: vi.fn().mockResolvedValue([REPO]),
    listBackupPolicies: vi.fn().mockResolvedValue([policy()]),
    listDevices: vi.fn().mockResolvedValue(fleet(3)),
    listDeviceGroups: vi.fn().mockResolvedValue({ items: [], total: 1, ungrouped_device_count: 0, last_evaluated_at: null }),
    listRecoveryKeys: vi.fn().mockResolvedValue([]),
    listOffsiteTargets: vi.fn().mockResolvedValue([]),
    ...rest,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

function renderPanel(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
  return client;
}

/**
 * Every query answered. The panel is ALSO hidden while its rows are still
 * checking, so "not drawn" is only a claim about the done state once nothing
 * is in flight.
 */
async function settled(client: QueryClient) {
  await waitFor(() => expect(client.isFetching()).toBe(0));
  await waitFor(() => expect(client.getQueryState(["me"])?.status).toBe("success"));
}

/** The step (`li`) whose statement is `label`. */
async function step(label: string) {
  const heading = await screen.findByText(label);
  const item = heading.closest("li");
  if (!item) throw new Error(`no step for ${label}`);
  return item;
}

describe("BackupReadiness — before backups can run", () => {
  it("sends an org with no repository to create one, and holds the policy step until then", async () => {
    stubApi({ listBackupRepositories: vi.fn().mockResolvedValue([]), listBackupPolicies: vi.fn().mockResolvedValue([]) });
    renderPanel(<BackupReadiness />);

    const repo = await step("A backup repository exists");
    expect(within(repo).getByRole("link", { name: "Create a repository" })).toHaveAttribute(
      "href",
      "/backup/repositories",
    );
    const pol = await step("An enabled backup policy covers devices");
    expect(pol).toHaveTextContent("Create a repository first.");
    expect(within(pol).queryByRole("link")).not.toBeInTheDocument();
  });

  it("names the silent no-op: no enabled policy means Run backup creates no job", async () => {
    stubApi({ listBackupPolicies: vi.fn().mockResolvedValue([]) });
    renderPanel(<BackupReadiness />);

    const pol = await step("An enabled backup policy covers devices");
    expect(pol).toHaveTextContent(/Run backup creates no job/);
    expect(within(pol).getByRole("link", { name: "Create a backup policy" })).toHaveAttribute(
      "href",
      "/backup/policies",
    );
  });

  it("sends a disabled policy to the disabled list, not to create another", async () => {
    stubApi({ listBackupPolicies: vi.fn().mockResolvedValue([policy({ enabled: false })]) });
    renderPanel(<BackupReadiness />);

    const pol = await step("An enabled backup policy covers devices");
    expect(pol).toHaveTextContent(/none is enabled/);
    expect(within(pol).getByRole("link", { name: "Enable a policy" })).toHaveAttribute(
      "href",
      "/backup/policies?enabled=false",
    );
  });

  it("sends a fleet with no agent to add a computer", async () => {
    stubApi({ listDevices: vi.fn().mockResolvedValue(fleet(0)) });
    renderPanel(<BackupReadiness />);

    const agent = await step("A computer runs the agent");
    expect(within(agent).getByRole("link", { name: "Add a computer" })).toHaveAttribute("href", "/devices/onboard");
  });

  it("is not drawn once everything is in place", async () => {
    const api = stubApi();
    const client = renderPanel(<BackupReadiness />);

    await waitFor(() => expect(api.listDevices).toHaveBeenCalled());
    await settled(client);
    expect(screen.queryByText("Before backups can run")).not.toBeInTheDocument();
  });

  it("says an account without backup read cannot check, and asks nothing", async () => {
    const api = stubApi({ permissions: ["devices.read"] });
    renderPanel(<BackupReadiness />);

    const repo = await step("A backup repository exists");
    expect(repo).toHaveTextContent("This cannot be checked from this account");
    expect(within(repo).queryByRole("link")).not.toBeInTheDocument();
    expect(api.listBackupRepositories).not.toHaveBeenCalled();
    expect(api.listBackupPolicies).not.toHaveBeenCalled();
  });
});

describe("PolicyReadiness — before a policy can back anything up", () => {
  it("links to creating a device group when none exists", async () => {
    stubApi({
      listDeviceGroups: vi.fn().mockResolvedValue({ items: [], total: 0, ungrouped_device_count: 0, last_evaluated_at: null }),
    });
    renderPanel(<PolicyReadiness />);

    const groups = await step("A device group exists for a narrower policy");
    expect(within(groups).getByRole("link", { name: "Create a device group" })).toHaveAttribute(
      "href",
      "/device-groups",
    );
  });

  it("is not drawn with a repository and a group", async () => {
    const api = stubApi();
    const client = renderPanel(<PolicyReadiness />);
    await waitFor(() => expect(api.listDeviceGroups).toHaveBeenCalled());
    await settled(client);
    expect(screen.queryByText("Before a policy can back anything up")).not.toBeInTheDocument();
  });
});

describe("OffsiteReadiness — before repositories are copied off-site", () => {
  const provenKey: RecoveryKey = {
    id: "k1",
    fingerprint: "ab:cd",
    public_key: "age1…",
    proven_at: "2026-09-01T00:00:00Z",
    challenge_expires_at: null,
    created_at: "2026-09-01T00:00:00Z",
    retired_at: null,
  };
  const okTarget = { id: "t1", state: "ok" } as unknown as OffsiteTarget;

  it("sends an unconfirmed key and untested storage to the off-site page", async () => {
    stubApi({
      listRecoveryKeys: vi.fn().mockResolvedValue([{ ...provenKey, proven_at: null }]),
      listOffsiteTargets: vi.fn().mockResolvedValue([{ ...okTarget, state: "failing" }]),
    });
    renderPanel(<OffsiteReadiness />);

    const key = await step("A recovery key is set up and confirmed");
    expect(key).toHaveTextContent(/not confirmed yet/);
    expect(within(key).getByRole("link", { name: "Set up a recovery key" })).toHaveAttribute("href", "/backup/offsite");
    const storage = await step("Off-site storage has passed its test");
    expect(within(storage).getByRole("link", { name: "Test off-site storage" })).toHaveAttribute(
      "href",
      "/backup/offsite",
    );
  });

  it("is not drawn with a confirmed key and tested storage", async () => {
    const api = stubApi({
      listRecoveryKeys: vi.fn().mockResolvedValue([provenKey]),
      listOffsiteTargets: vi.fn().mockResolvedValue([okTarget]),
    });
    const client = renderPanel(<OffsiteReadiness />);
    await waitFor(() => expect(api.listOffsiteTargets).toHaveBeenCalled());
    await settled(client);
    expect(screen.queryByText("Before repositories are copied off-site")).not.toBeInTheDocument();
  });

  it("cannot be checked without backup read", async () => {
    stubApi({ permissions: [] });
    renderPanel(<OffsiteReadiness />);
    expect(await step("A recovery key is set up and confirmed")).toHaveTextContent(
      "This cannot be checked from this account",
    );
  });
});
