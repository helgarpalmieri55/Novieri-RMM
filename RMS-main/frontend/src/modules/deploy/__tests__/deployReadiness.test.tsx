import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../../lib/api";
import { DeployReadiness, needsUpload } from "../DeployReadiness";
import type { Package } from "../types";

// "Before software can be deployed" (`docs/design/cross-module-guidance.md`
// §2 Software): the agent, the uploaded installer execute refuses without,
// and the change gate that refuses a deployment with 409.

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ALL = ["deploy.read", "devices.read", "change.read"];

function pkg(overrides: Partial<Package> = {}): Package {
  return {
    id: "p1",
    name: "7-Zip",
    version: "24.08",
    os_family: "windows",
    installer_type: "msi",
    artifact_ref: "artifacts/p1",
    artifact_sha256: "ab",
    detection_rule: {},
    uninstall_rule: {},
    install_args: null,
    created_at: "2026-09-01T00:00:00Z",
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
    listPackages: vi.fn().mockResolvedValue([pkg()]),
    listDevices: vi.fn().mockResolvedValue(fleet(2)),
    listChangeGates: vi.fn().mockResolvedValue([]),
    listChanges: vi.fn().mockResolvedValue([]),
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

async function settled(client: QueryClient) {
  await waitFor(() => expect(client.isFetching()).toBe(0));
  await waitFor(() => expect(client.getQueryState(["me"])?.status).toBe("success"));
}

async function step(label: string) {
  const item = (await screen.findByText(label)).closest("li");
  if (!item) throw new Error(`no step for ${label}`);
  return item;
}

describe("needsUpload mirrors the server's refusal (deploy/artifacts.py)", () => {
  it("is true for a file-backed package with no stored file or no hash", () => {
    expect(needsUpload(pkg({ artifact_ref: null }))).toBe(true);
    expect(needsUpload(pkg({ artifact_sha256: null }))).toBe(true);
    expect(needsUpload(pkg())).toBe(false);
  });

  it("is false for a package-manager install, which ships no file", () => {
    expect(needsUpload(pkg({ installer_type: "winget", artifact_ref: null, artifact_sha256: null }))).toBe(false);
  });
});

describe("DeployReadiness", () => {
  it("names the package with no installer and links to uploading it", async () => {
    stubApi({ listPackages: vi.fn().mockResolvedValue([pkg(), pkg({ id: "p2", name: "Zoom", version: "6.2", artifact_ref: null })]) });
    renderPanel(<DeployReadiness />);

    const upload = await step("Every package has its installer uploaded");
    expect(upload).toHaveTextContent("Zoom 6.2 has no installer uploaded");
    expect(within(upload).getByRole("link", { name: "Upload the installer" })).toHaveAttribute(
      "href",
      "/deploy/packages",
    );
  });

  it("warns about a deployment gate before the 409", async () => {
    stubApi({
      listChangeGates: vi.fn().mockResolvedValue([
        { id: "g1", device_group_id: null, device_group_name: null, applies_to_patches: false, applies_to_deployments: true, created_at: "" },
      ]),
    });
    renderPanel(<DeployReadiness />);

    const gated = await step("An approved change covers gated deployments");
    expect(gated).toHaveTextContent("Deployments to every device need an approved change");
    expect(within(gated).getByRole("link", { name: "Raise a change for this deployment" })).toHaveAttribute(
      "href",
      "/changes",
    );
  });

  it("links to adding a computer when none runs the agent", async () => {
    stubApi({ listDevices: vi.fn().mockResolvedValue(fleet(0)) });
    renderPanel(<DeployReadiness />);
    expect(within(await step("A computer runs the agent")).getByRole("link", { name: "Add a computer" })).toHaveAttribute(
      "href",
      "/devices/onboard",
    );
  });

  it("is not drawn once everything is in place", async () => {
    stubApi();
    const client = renderPanel(<DeployReadiness />);
    await settled(client);
    expect(screen.queryByText("Before software can be deployed")).not.toBeInTheDocument();
  });

  it("says the gate cannot be checked without change read", async () => {
    const api = stubApi({ permissions: ["deploy.read", "devices.read"] });
    renderPanel(<DeployReadiness />);
    expect(await step("An approved change covers gated deployments")).toHaveTextContent(
      "This cannot be checked from this account",
    );
    expect(api.listChangeGates).not.toHaveBeenCalled();
  });
});
