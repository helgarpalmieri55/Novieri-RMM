/**
 * Managed deployment — GPO and Intune.
 *
 * FOUR CLAIMS, and every one is a claim about a security property rather than
 * about layout:
 *
 *   1. **Opening the package mints nothing.** The package is what gets staged
 *      on a share the whole domain reads; if opening this dialog produced a
 *      credential, the separation the backend enforces would be undone by the
 *      console.
 *   2. **The credential is a second, confirmed act**, and the confirmation
 *      states the reach in machines and in hours. Those two numbers are the
 *      entire decision.
 *   3. **The blast radius is shown when the token is.** Not in a tooltip, not
 *      on a help page: at the moment the operator is holding a live fleet
 *      credential.
 *   4. **The missing integrity check is named.** GPO and Intune verify nothing
 *      at install, and a panel that showed the one-machine reassurance here
 *      would be asserting a check that does not happen.
 *
 * Mocks the auth-backed API client, mirroring `AgentReleasesPage.test.tsx`.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { AgentDeploymentPackage, AgentRelease, ApiClient } from "../../lib/api";
import { ManagedDeploymentDialog } from "./ManagedDeploymentDialog";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const RELEASE: AgentRelease = {
  id: "11111111-1111-1111-1111-111111111111",
  version: "873d666",
  os: "windows",
  arch: "amd64",
  size_bytes: 21_331_968,
  sha256: "b".repeat(64),
  signed: false,
  notes: "",
  published_at: "2026-08-26T09:00:00Z",
  has_artifact: true,
};

function pkg(over: Partial<AgentDeploymentPackage> = {}): AgentDeploymentPackage {
  return {
    release: RELEASE,
    channel: "gpo",
    download_url: "https://minio.test/agent-releases/873d666/its-agent-windows-amd64.exe?sig=x",
    download_expires_in_s: 900,
    sha256: "b".repeat(64),
    staging: [
      { title: "Download the artefact once", detail: "On your own workstation.", command: "curl -fL -o its-agent.exe '…'" },
      { title: "Stage it on a distribution point", detail: "Readable by machine accounts.", command: "" },
    ],
    install_command: "powershell.exe -File \\\\dfs\\its-agent\\install.ps1 -NoStart",
    enrol_command:
      "$env:ITS_AGENT_ENROLL_TOKEN = <read from your secret store>; .\\its-agent-enroll.ps1 -Url https://console",
    integrity: {
      sha256: "b".repeat(64),
      verify_command: "if ((Get-FileHash 'its-agent.exe' -Algorithm SHA256).Hash -ne '…') { throw }",
      verified_at_install: false,
      notes: ["The endpoint does not verify anything.", "This binary is unsigned."],
    },
    ...over,
  };
}

function renderDialog({
  getAgentDeploymentPackage = () => Promise.resolve(pkg()),
  mintAgentDeploymentToken = vi.fn(() =>
    Promise.resolve({
      token: "tok-11111111.secret",
      token_id: "22222222-2222-2222-2222-222222222222",
      token_expires_at: "2026-08-29T09:00:00Z",
      max_uses: 50,
      channel: "gpo",
      blast_radius: [
        "Anyone who can run code as SYSTEM on any domain-joined machine in scope can read this token.",
        "A holder can enrol up to 50 machines into this organization.",
      ],
    }),
  ),
}: Record<string, unknown> = {}) {
  mockUseApi.mockReturnValue({
    getAgentDeploymentPackage,
    mintAgentDeploymentToken,
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui: ReactElement = (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ManagedDeploymentDialog release={RELEASE} onClose={() => {}} />
      </MemoryRouter>
    </QueryClientProvider>
  );
  return { ...render(ui), mintAgentDeploymentToken };
}

beforeEach(() => {
  mockUseApi.mockReset();
});

describe("the package is inert", () => {
  it("mints no credential just by being opened", async () => {
    const { mintAgentDeploymentToken } = renderDialog();
    await screen.findByText("Staging");
    expect(mintAgentDeploymentToken).not.toHaveBeenCalled();
    // And the operator can see that the thing they are about to stage carries
    // nothing: it is the first sentence, not a footnote.
    expect(screen.getByText(/no enrollment token and nothing tenant-specific/i)).toBeTruthy();
  });

  it("serves an install command that names no token, and a separate enrol step that does", async () => {
    renderDialog();
    await screen.findByText("Staging");
    const install = screen.getByText(/registers the service, does not enrol/i);
    const enrol = screen.getByText(/a separate step/i);
    expect(install).toBeTruthy();
    expect(enrol).toBeTruthy();
    // The install invocation must not carry the credential idiom at all --
    // it would be run by every machine in the domain.
    expect(screen.getByText(/-File .*install\.ps1 -NoStart/)).toBeTruthy();
  });
});

describe("the credential is a second act", () => {
  it("confirms before minting, naming the machines and the hours", async () => {
    const { mintAgentDeploymentToken } = renderDialog();
    await screen.findByText("Staging");
    await userEvent.click(screen.getByRole("button", { name: /Create a deployment token/i }));

    // Not minted yet. The click asks; the confirmation acts.
    expect(mintAgentDeploymentToken).not.toHaveBeenCalled();
    const confirm = await screen.findByRole("dialog", { name: /Create a deployment token/i });
    expect(within(confirm).getByText(/up to 50 machines/i)).toBeTruthy();
    expect(within(confirm).getByText(/next 72 hours/i)).toBeTruthy();
    // The channel-specific reach, at the moment of the decision.
    expect(within(confirm).getByText(/any local administrator of any machine in scope/i)).toBeTruthy();

    await userEvent.click(within(confirm).getByRole("button", { name: "Create token" }));
    expect(mintAgentDeploymentToken).toHaveBeenCalledTimes(1);
  });

  it("shows the blast radius alongside the live token", async () => {
    renderDialog();
    await screen.findByText("Staging");
    await userEvent.click(screen.getByRole("button", { name: /Create a deployment token/i }));
    const confirm = await screen.findByRole("dialog", { name: /Create a deployment token/i });
    await userEvent.click(within(confirm).getByRole("button", { name: "Create token" }));

    expect(await screen.findByText("tok-11111111.secret")).toBeTruthy();
    expect(screen.getByText(/enrol up to 50 machines into this organization/i)).toBeTruthy();
    expect(screen.getByText(/50 enrolments/)).toBeTruthy();
  });
});

describe("the integrity accounting", () => {
  it("says the endpoint verifies nothing on a pushed channel", async () => {
    renderDialog();
    expect(await screen.findByText(/The endpoint verifies nothing on this path/i)).toBeTruthy();
    expect(screen.getByText(/This binary is unsigned\./)).toBeTruthy();
    // And it still gives the comparison that DOES happen, so "no check" is not
    // read as "no digest".
    expect(screen.getByText(/before it reaches a share/i)).toBeTruthy();
  });

  it("says the opposite when the channel earns it", async () => {
    // Non-vacuity: the badge is about the CHANNEL, not a constant.
    renderDialog({
      getAgentDeploymentPackage: () =>
        Promise.resolve(
          pkg({
            channel: "manual",
            integrity: {
              sha256: "b".repeat(64),
              verify_command: "echo '…' | sha256sum -c -",
              verified_at_install: true,
              notes: ["A person runs this command on the machine."],
            },
          }),
        ),
    });
    expect(await screen.findByText(/The endpoint verifies before installing/i)).toBeTruthy();
  });
});
