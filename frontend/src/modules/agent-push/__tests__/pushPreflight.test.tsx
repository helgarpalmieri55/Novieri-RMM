import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../../lib/api";
import { ApiError } from "../../../lib/api";
import { PushPreflightPanel } from "../PushPreflightPanel";
import type { HostKeyOut } from "../types";

// **THE PRE-FLIGHT PANEL IS THE LAST SCREEN BEFORE A CREDENTIALED PUSH.** Each
// assertion is about a claim it makes to an operator about to present
// administrative credentials to unmanaged machines -- not about wording. Since
// 2026-09-29 it talks to the server through the shared `ApiClient`, so the
// client is mocked here like everywhere else.

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

type Stubbed = "listPushHostKeys" | "requestPush" | "confirmPushHostKey";

function stubApi(hostKeys: HostKeyOut[], overrides: Partial<Record<Stubbed, ReturnType<typeof vi.fn>>> = {}) {
  const api = {
    listPushHostKeys: vi.fn().mockResolvedValue(hostKeys),
    requestPush: vi.fn().mockResolvedValue({ batch_id: "b-0001", kind: "probe", targets: [] }),
    confirmPushHostKey: vi.fn().mockResolvedValue(hostKeys[0]),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

function renderPanel(
  targets: string[],
  props: { windowsTargets?: string[]; credentialType?: string; client?: string | null } = {},
) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <PushPreflightPanel
        targets={targets}
        windowsTargets={props.windowsTargets}
        credential={{
          id: "c1",
          name: "push-svc",
          type: props.credentialType ?? "push_install",
          client_id: props.client ?? null,
        }}
      />
    </QueryClientProvider>,
  );
}

beforeEach(() => mockUseApi.mockReset());

function hostKey(address: string, overrides: Partial<HostKeyOut> = {}): HostKeyOut {
  return {
    address,
    client_id: null,
    transport: "ssh",
    observed_fingerprint: "SHA256:AAAA",
    observed_key_type: "ssh-ed25519",
    observed_at: "2026-08-26T00:00:00Z",
    confirmed_fingerprint: null,
    confirmed_at: null,
    confirmed_by: null,
    installable: false,
    winrm_https: false,
    winrm_http: false,
    kdc_reachable: false,
    ...overrides,
  };
}

function winrmRow(address: string, overrides: Partial<HostKeyOut> = {}): HostKeyOut {
  return hostKey(address, {
    transport: "winrm",
    observed_fingerprint: "winrm",
    observed_key_type: "",
    ...overrides,
  });
}

describe("PushPreflightPanel", () => {
  it("lists EVERY target address, never 'and N more' (§10.4)", async () => {
    stubApi([]);
    const targets = ["10.0.4.1", "10.0.4.2", "10.0.4.3"];
    renderPanel(targets);
    for (const t of targets) {
      expect(await screen.findAllByText(t)).not.toHaveLength(0);
    }
    expect(screen.queryByText(/and \d+ more/i)).toBeNull();
  });

  it("states the sentence and the lockout warning in plain text (§10.4 items 7, 8)", async () => {
    stubApi([]);
    renderPanel(["10.0.4.1"]);
    expect(await screen.findAllByText(/cannot be verified by it/i)).not.toHaveLength(0);
    expect(screen.getByText(/lock that account/i)).toBeInTheDocument();
  });

  it("keeps the install DISABLED until every target has a confirmed host key (§7.3)", async () => {
    stubApi([
      hostKey("10.0.4.1", { confirmed_fingerprint: "SHA256:AAAA", installable: true }),
      hostKey("10.0.4.2"),
    ]);
    renderPanel(["10.0.4.1", "10.0.4.2"]);
    const installBtn = await screen.findByRole("button", { name: /push install/i });
    expect(installBtn).toBeDisabled();
    expect(screen.getByText(/every target must be ready first/i)).toBeInTheDocument();
  });

  it("reads each address's key in the credential's client, never another client's", async () => {
    // Two clients' machines may share one address (MSP clients phase 1), each
    // with its own key. A push is made in its credential's client (phase 2,
    // Task 12), so X's confirmed key does not make Y's machine installable;
    // Y's own row speaks for it, and without one the global row would.
    stubApi([
      hostKey("10.0.4.1", { client_id: "x", confirmed_fingerprint: "SHA256:AAAA", installable: true }),
      hostKey("10.0.4.1", { client_id: "y" }),
    ]);
    const y = renderPanel(["10.0.4.1"], { client: "y" });
    expect((await screen.findAllByText("awaiting confirmation")).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: /push install/i })).toBeDisabled();
    y.unmount();

    renderPanel(["10.0.4.1"], { client: "x" });
    const installBtn = await screen.findByRole("button", { name: /push install/i });
    await waitFor(() => expect(installBtn).toBeEnabled());
  });

  it("falls back to the global key when the credential's client has none, and ignores another client's", async () => {
    stubApi([
      hostKey("10.0.4.1", { client_id: "x", confirmed_fingerprint: "SHA256:AAAA", installable: true }),
      hostKey("10.0.4.2", { client_id: null, confirmed_fingerprint: "SHA256:BBBB", installable: true }),
    ]);
    renderPanel(["10.0.4.1", "10.0.4.2"], { client: "y" });
    // 10.0.4.1 is X's key only: for Y it has none.
    expect((await screen.findAllByText("no host key")).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: /push install/i })).toBeDisabled();
  });

  it("enables the install once all targets are installable", async () => {
    stubApi([hostKey("10.0.4.1", { confirmed_fingerprint: "SHA256:AAAA", installable: true })]);
    renderPanel(["10.0.4.1"]);
    const installBtn = await screen.findByRole("button", { name: /push install/i });
    await waitFor(() => expect(installBtn).toBeEnabled());
  });

  it("shows a changed key as a refusal to re-confirm, not a warning to click through", async () => {
    stubApi([
      hostKey("10.0.4.1", {
        confirmed_fingerprint: "SHA256:OLD",
        observed_fingerprint: "SHA256:NEW",
        installable: false,
      }),
    ]);
    renderPanel(["10.0.4.1"]);
    expect(await screen.findAllByText(/key changed/i)).not.toHaveLength(0);
    expect(screen.getByRole("button", { name: /push install/i })).toBeDisabled();
  });

  it("confirms exactly the fingerprint the probe observed", async () => {
    const api = stubApi([hostKey("10.0.4.1", { observed_fingerprint: "SHA256:OBSERVED" })]);
    renderPanel(["10.0.4.1"]);
    await userEvent.click(await screen.findByRole("button", { name: /confirm sha256:observed/i }));
    await waitFor(() =>
      expect(api.confirmPushHostKey).toHaveBeenCalledWith({ address: "10.0.4.1", fingerprint: "SHA256:OBSERVED" }),
    );
  });

  it("probes with mode=probe and NO credential secret ever crosses the boundary (§4.4)", async () => {
    const api = stubApi([]);
    renderPanel(["10.0.4.1"]);
    await userEvent.click(await screen.findByRole("button", { name: /^probe the targets$/i }));
    await waitFor(() => expect(api.requestPush).toHaveBeenCalledTimes(1));
    const body = api.requestPush.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(body).toEqual({ mode: "probe", credential_id: "c1", targets: ["10.0.4.1"] });
    expect(JSON.stringify(body)).not.toMatch(/secret|password/i);
  });

  it("sends the install for exactly the named targets, once", async () => {
    const api = stubApi([hostKey("10.0.4.1", { confirmed_fingerprint: "SHA256:AAAA", installable: true })]);
    renderPanel(["10.0.4.1"]);
    const installBtn = await screen.findByRole("button", { name: /push install to 1 machine/i });
    await waitFor(() => expect(installBtn).toBeEnabled());
    await userEvent.click(installBtn);
    await waitFor(() =>
      expect(api.requestPush).toHaveBeenCalledWith({ mode: "install", credential_id: "c1", targets: ["10.0.4.1"] }),
    );
    expect(await screen.findByText(/dispatched/i)).toBeInTheDocument();
  });
});

describe("a Windows target (WinRM, Kerberos only) — never a host key to confirm", () => {
  it("shows WinRM, HTTPS, HTTP and domain controller readiness, and no Confirm", async () => {
    stubApi([winrmRow("10.0.5.1", { winrm_https: true, winrm_http: false, kdc_reachable: true })]);
    renderPanel(["10.0.5.1"], { windowsTargets: ["10.0.5.1"], credentialType: "windows_domain" });
    const table = await screen.findByRole("table", { name: /windows targets/i });
    const row = within(table).getByText("10.0.5.1").closest("tr")!;
    expect(within(row).getByText(/HTTPS \(5986\): answered/)).toBeInTheDocument();
    expect(within(row).getByText(/HTTP \(5985\): no answer/)).toBeInTheDocument();
    expect(within(row).getByText(/Domain controller: reachable/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^confirm/i })).toBeNull();
    // The marker is not a key and is never shown as one.
    expect(screen.queryByText("winrm")).toBeNull();
  });

  it("treats a row carrying the Kerberos marker as WinRM, not as a key", async () => {
    stubApi([hostKey("10.0.5.2", { observed_fingerprint: "kerberos", winrm_http: true, kdc_reachable: true })]);
    renderPanel(["10.0.5.2"], { credentialType: "windows_domain" });
    await screen.findByRole("table", { name: /windows targets/i });
    expect(screen.queryByRole("button", { name: /^confirm/i })).toBeNull();
    expect(screen.queryByText("kerberos")).toBeNull();
  });

  it("unlocks the install when WinRM answered and a domain controller is reachable", async () => {
    stubApi([winrmRow("10.0.5.1", { winrm_http: true, kdc_reachable: true })]);
    renderPanel(["10.0.5.1"], { windowsTargets: ["10.0.5.1"], credentialType: "windows_domain" });
    const installBtn = await screen.findByRole("button", { name: /push install/i });
    await waitFor(() => expect(installBtn).toBeEnabled());
  });

  it("says in plain words that WinRM did not answer, and keeps the install locked", async () => {
    stubApi([winrmRow("10.0.5.1", { winrm_https: false, winrm_http: false, kdc_reachable: true })]);
    renderPanel(["10.0.5.1"], { windowsTargets: ["10.0.5.1"], credentialType: "windows_domain" });
    expect(await screen.findAllByText(/WinRM did not answer/)).not.toHaveLength(0);
    expect(screen.getByRole("button", { name: /push install/i })).toBeDisabled();
  });

  it("warns that no domain controller was found via DNS, and still allows the install", async () => {
    // Review I2: advisory. The credential may name its domain controller, in
    // which case the DNS lookup the probe made is never used by the push.
    stubApi([winrmRow("10.0.5.1", { winrm_https: true, kdc_reachable: false })]);
    renderPanel(["10.0.5.1"], { windowsTargets: ["10.0.5.1"], credentialType: "windows_domain" });
    const table = await screen.findByRole("table", { name: /windows targets/i });
    const row = within(table).getByText("10.0.5.1").closest("tr")!;
    expect(
      within(row).getByText(/not found via DNS — fine if the credential names a domain controller/),
    ).toBeInTheDocument();
    const installBtn = screen.getByRole("button", { name: /push install/i });
    await waitFor(() => expect(installBtn).toBeEnabled());
  });

  it("a named Windows target that has not been probed waits for a probe, not a key", async () => {
    stubApi([]);
    renderPanel(["10.0.5.1"], { windowsTargets: ["10.0.5.1"], credentialType: "windows_domain" });
    expect(await screen.findAllByText(/not probed yet/i)).not.toHaveLength(0);
    expect(screen.queryByText(/awaiting confirmation/i)).toBeNull();
    expect(screen.getByRole("button", { name: /push install/i })).toBeDisabled();
  });

  it("carries the standing Domain Admin warning for a Windows domain account", async () => {
    stubApi([]);
    renderPanel(["10.0.5.1"], { windowsTargets: ["10.0.5.1"], credentialType: "windows_domain" });
    expect(await screen.findByText(/compromised domain/i)).toBeInTheDocument();
  });
});

describe("what the operator is told when the server refuses", () => {
  const refusal = (targets: { address: string; reasons: string[] }[]) =>
    new ApiError(
      422,
      JSON.stringify({
        detail: {
          message: "this push was refused and nothing was created.",
          targets,
          reason_help: {
            no_site_server: "no enabled site server serves this address's segment",
            some_new_reason: "a reason this console was never taught",
          },
        },
      }),
    );

  it("names every failing bound per address, in plain language", async () => {
    stubApi([], {
      requestPush: vi.fn().mockRejectedValue(
        refusal([
          { address: "10.0.4.1", reasons: ["no_asset", "no_authorization"] },
          { address: "10.0.4.2", reasons: ["no_site_server", "credential_not_scoped"] },
        ]),
      ),
    });
    renderPanel(["10.0.4.1", "10.0.4.2"]);
    await userEvent.click(await screen.findByRole("button", { name: /^probe the targets$/i }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/refused\. Nothing was created/i);
    expect(alert).toHaveTextContent("Nobody has vouched for this machine");
    expect(alert).toHaveTextContent("not in a range authorized for push install");
    expect(alert).toHaveTextContent("No site server serves this machine's network segment");
    expect(alert).toHaveTextContent("not allowed on this address");
    // Codes are wire names: never the thing an operator reads.
    expect(alert.textContent).not.toMatch(/no_asset|no_site_server|credential_not_scoped/);
  });

  it("falls back to the server's own remedy for a reason it was not taught", async () => {
    stubApi([], {
      requestPush: vi.fn().mockRejectedValue(refusal([{ address: "10.0.4.1", reasons: ["some_new_reason"] }])),
    });
    renderPanel(["10.0.4.1"]);
    await userEvent.click(await screen.findByRole("button", { name: /^probe the targets$/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("A reason this console was never taught.");
  });

  it("shows a refusal that is not per-address (no DNS name, too many, no permission) as a sentence", async () => {
    stubApi([], {
      requestPush: vi.fn().mockRejectedValue(
        new ApiError(
          400,
          JSON.stringify({
            detail:
              "the Windows target at d1 has no DNS name on its asset row. Set the asset's DNS name",
          }),
        ),
      ),
    });
    renderPanel(["10.0.5.1"], { windowsTargets: ["10.0.5.1"], credentialType: "windows_domain" });
    await userEvent.click(await screen.findByRole("button", { name: /^probe the targets$/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/has no DNS name on its asset row/);
  });
});
