/**
 * Push install in Spanish: the authorizations and credential-scopes pages (with
 * the readiness panel, the hazard notice and both forms), the push runs list and
 * a run's page in every state the lockout guard can leave it in, the install
 * flow for ticked machines, its pre-flight panel (SSH and Windows targets, a
 * refusal) and the "By subnet" flow. Addresses, hostnames, credential names and
 * the server's own outcome text read as recorded; the words around them are
 * Spanish.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import { ApiError } from "../../lib/api";
import type { ApiClient, Device } from "../../lib/api";
import { PushAuthorizationsPage } from "./PushAuthorizationsPage";
import { PushCredentialScopesPage } from "./PushCredentialScopesPage";
import { PushInstallChooser } from "./PushInstallChooser";
import { PushPreflightPanel } from "./PushPreflightPanel";
import { PushRunPage } from "./PushRunPage";
import { PushRunsPage } from "./PushRunsPage";
import type {
  HostKeyOut,
  PushAuthorizationOut,
  PushCredentialScopeOut,
  PushRunOut,
  PushRunPreviewOut,
  PushRunState,
  PushRunTargetOut,
} from "./types";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const PERMISSIONS = ["agent.push.scope.manage", "agent.push.run", "credentials.read", "site.read", "agent.release.read"];

// ------------------------------------------------------------------ fixtures

const inDays = (days: number): string => new Date(Date.now() + days * 86_400_000).toISOString();

function grant(overrides: Partial<PushAuthorizationOut> = {}): PushAuthorizationOut {
  return {
    id: "g1",
    client_id: "o1",
    prefix: "10.20.4.0/24",
    address_count: 256,
    note: "Planta 3 de Lisboa",
    authorized_by: "Marta Ruiz",
    authorized_at: "2026-09-10T09:00:00Z",
    expires_at: inDays(60),
    revoked_at: null,
    revoked_by: null,
    ...overrides,
  };
}

const GRANTS: PushAuthorizationOut[] = [
  grant(),
  grant({ id: "g2", prefix: "10.30.1.0/24", note: "", expires_at: null }),
  grant({ id: "g3", prefix: "10.20.9.0/24", note: "Piloto", expires_at: "2026-09-01T00:00:00Z" }),
  grant({ id: "g4", prefix: "10.40.0.0/16", address_count: 65536, note: "Antigua", revoked_at: "2026-09-15T09:00:00Z", revoked_by: "Marta Ruiz" }),
];

function scope(overrides: Partial<PushCredentialScopeOut> = {}): PushCredentialScopeOut {
  return {
    id: "s1",
    client_id: "o1",
    credential_id: "c1",
    credential_name: "Instalación del rack B4",
    prefix: "10.20.4.0/24",
    address_count: 256,
    note: "Administrador local dedicado",
    bound_by: "Marta Ruiz",
    bound_at: "2026-09-10T09:00:00Z",
    unbound_at: null,
    unbound_by: null,
    ...overrides,
  };
}

const SCOPES: PushCredentialScopeOut[] = [
  scope(),
  scope({ id: "s2", credential_id: "c2", credential_name: "", prefix: "10.30.1.1", address_count: 1, note: "" }),
  scope({ id: "s3", prefix: "10.20.9.0/24", unbound_at: "2026-09-20T09:00:00Z", unbound_by: "Marta Ruiz" }),
];

const CREDENTIALS = [
  { id: "c1", name: "Instalación del rack B4", type: "push_install", device_group_id: null, created_at: null, last_used_at: null, has_secret: true },
  { id: "c3", name: "Cuenta de dominio de Lisboa", type: "windows_domain", device_group_id: null, created_at: null, last_used_at: null, has_secret: true },
  { id: "c9", name: "Control remoto", type: "remote_control", device_group_id: null, created_at: null, last_used_at: null, has_secret: true },
];

const SITE_SERVERS = [
  {
    id: "ss-1",
    device_id: "dev-5",
    name: "rack-b4-relay",
    segments: ["10.20.4.0/24"],
    roles: ["wake"],
    enabled: true,
    notes: "",
    client_id: "o1",
    site_id: null,
    agent_status: "online",
    agent_last_seen_at: "2026-09-30T09:00:00Z",
  },
];

const RELEASES = ["windows", "linux", "darwin"].flatMap((os) =>
  ["amd64", "arm64"].map((arch) => ({
    id: `rel-${os}-${arch}`,
    version: "1.8.2",
    os,
    arch,
    size_bytes: 1024,
    sha256: "ab".repeat(32),
    signed: true,
    notes: "",
    published_at: "2026-09-01T00:00:00Z",
    has_artifact: true,
    channel: "stable",
    release_signed: true,
  })),
);

function hostKey(address: string, overrides: Partial<HostKeyOut> = {}): HostKeyOut {
  return {
    address,
    client_id: null,
    transport: "ssh",
    observed_fingerprint: "SHA256:q3Vd2mX0Jc8pZ1uYkT7rHs4nLw9aGf6eBi5oCxRtUvE",
    observed_key_type: "ssh-ed25519",
    observed_at: "2026-09-28T09:00:00Z",
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
  return hostKey(address, { transport: "winrm", observed_fingerprint: "winrm", observed_key_type: "", ...overrides });
}

function run(state: PushRunState = "running", over: Partial<PushRunOut> = {}): PushRunOut {
  return {
    id: "run-1",
    client_id: "o1",
    prefix: "10.20.4.0/24",
    credential_id: "c1",
    credential_name: "Instalación del rack B4",
    created_by: "u1",
    created_at: "2026-09-30T09:00:00Z",
    state,
    target_count: 6,
    concurrency: 50,
    list_digest: "ab".repeat(32),
    finished_at: null,
    counts: { waiting: 1, in_flight: 2, installed: 1, failed: 1, refused: 1, cancelled: 0 },
    stopped_at: state === "stopped_credential" ? "2026-09-30T09:05:00Z" : null,
    auth_failures: state === "stopped_credential" ? 3 : 0,
    auth_failure_limit: 3,
    signed_in: state !== "stopped_credential",
    ...over,
  };
}

const TARGETS: PushRunTargetOut[] = [
  { position: 0, device_id: "d1", address: "10.20.4.12", state: "installed", job_id: "j1", outcome: "installed", detail: "", reasons: [], updated_at: "2026-09-30T09:01:00Z" },
  { position: 1, device_id: "d2", address: "10.20.4.13", state: "failed", job_id: "j2", outcome: "auth_failed", detail: "Permiso denegado", reasons: [], updated_at: "2026-09-30T09:01:00Z" },
  { position: 2, device_id: "d3", address: "10.20.4.14", state: "refused", job_id: null, outcome: "", detail: "", reasons: ["host_key_changed", "already_enrolled"], updated_at: "2026-09-30T09:01:00Z" },
  { position: 3, device_id: "d4", address: "10.20.4.15", state: "in_flight", job_id: "j3", outcome: "", detail: "", reasons: [], updated_at: "2026-09-30T09:01:00Z" },
  { position: 4, device_id: "d5", address: "10.20.4.16", state: "waiting", job_id: null, outcome: "", detail: "", reasons: [], updated_at: "2026-09-30T09:01:00Z" },
  { position: 5, device_id: "d6", address: "10.20.4.17", state: "cancelled", job_id: null, outcome: "", detail: "", reasons: [], updated_at: "2026-09-30T09:01:00Z" },
  { position: 6, device_id: "d7", address: "10.20.4.18", state: "failed", job_id: "j7", outcome: "never_seen", detail: "", reasons: [], updated_at: "2026-09-30T09:01:00Z" },
];

function preview(over: Partial<PushRunPreviewOut> = {}): PushRunPreviewOut {
  return {
    prefix: "10.20.4.0/24",
    client_id: "o1",
    ambiguous_count: 0,
    credential_id: "c1",
    credential_name: "Instalación del rack B4",
    list_digest: "ab".repeat(32),
    ready_count: 2,
    blocked_count: 3,
    unvouched_count: 3,
    awaiting_review_count: 1,
    run_max: 1024,
    concurrency: 50,
    rows: [
      { device_id: "d1", address: "10.20.4.12", hostname: "lis-srv-12", os_family: "linux", ready: true, reasons: [] },
      { device_id: "d2", address: "10.20.4.13", hostname: "", os_family: "", ready: true, reasons: [] },
      { device_id: "d3", address: "10.20.4.40", hostname: "lis-srv-40", os_family: "linux", ready: false, reasons: ["host_key_unconfirmed"] },
      { device_id: "d4", address: "10.20.4.31", hostname: "lis-ws-31", os_family: "windows", ready: false, reasons: ["wrong_credential_kind", "no_dns_name"] },
      { device_id: "d5", address: "10.20.4.50", hostname: "lis-srv-50", os_family: "linux", ready: false, reasons: ["something_new"] },
    ],
    reason_help: { something_new: "algo que dice el servidor" },
    ...over,
  };
}

function device(overrides: Partial<Device> = {}): Device {
  return {
    id: "d-1",
    hostname: "lis-srv-12",
    fqdn: "lis-srv-12.corp.local",
    os_family: "linux",
    os_version: "Ubuntu 24.04",
    architecture: "amd64",
    device_group_id: null,
    status: "active",
    agent_status: null,
    last_seen_at: null,
    cert_not_after: null,
    cert_state: null,
    cert_revoked: false,
    lifecycle: "in_service",
    asset_type: "computer",
    source: "manual",
    provenance: "operator",
    description: null,
    primary_ip: "10.20.4.12",
    owner: null,
    location: null,
    review_state: "approved",
    link_joined_from: null,
    link_joined_at: null,
    ...overrides,
  };
}

function stubApi(overrides: Record<string, unknown> = {}, permissions: string[] = PERMISSIONS) {
  const api = {
    me: vi.fn().mockResolvedValue({ user_id: "u1", org_id: "o1", email: "a@b.c", display_name: "A", permissions }),
    listPushAuthorizations: vi.fn().mockResolvedValue(GRANTS),
    createPushAuthorization: vi.fn(),
    revokePushAuthorization: vi.fn().mockResolvedValue(undefined),
    listPushCredentialScopes: vi.fn().mockResolvedValue(SCOPES),
    bindPushCredentialScope: vi.fn(),
    unbindPushCredentialScope: vi.fn().mockResolvedValue(undefined),
    listCredentials: vi.fn().mockResolvedValue(CREDENTIALS),
    listSiteServers: vi.fn().mockResolvedValue(SITE_SERVERS),
    listAgentReleases: vi.fn().mockResolvedValue(RELEASES),
    listPushHostKeys: vi.fn().mockResolvedValue([hostKey("10.20.4.12", { confirmed_fingerprint: "x", installable: true }), winrmRow("10.20.4.31", { winrm_https: true, kdc_reachable: true }), winrmRow("10.20.4.32")]),
    requestPush: vi.fn().mockResolvedValue({ batch_id: "b-0001abcdef", kind: "probe", targets: [] }),
    confirmPushHostKey: vi.fn().mockResolvedValue(hostKey("10.20.4.12")),
    listPushRunPrefixes: vi.fn().mockResolvedValue([{ prefix: "10.20.4.0/24", address_count: 256, expires_at: null }]),
    previewPushRun: vi.fn().mockResolvedValue(preview()),
    createPushRun: vi.fn().mockResolvedValue({ id: "run-9" }),
    listPushRuns: vi.fn().mockResolvedValue([run("running"), run("completed", { id: "run-0", prefix: "10.30.1.0/24", counts: { waiting: 0, in_flight: 0, installed: 5, failed: 1, refused: 0, cancelled: 0 } })]),
    getPushRun: vi.fn().mockResolvedValue(run("running")),
    listPushRunTargets: vi.fn().mockResolvedValue({ total: TARGETS.length, items: TARGETS, reason_help: { host_key_changed: "la clave cambió" } }),
    pausePushRun: vi.fn().mockResolvedValue(run("paused")),
    resumePushRun: vi.fn().mockResolvedValue(run("running")),
    cancelPushRun: vi.fn().mockResolvedValue(run("cancelled")),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

function renderPage(node: ReactElement, path = "/") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderInSpanish(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

function renderRunRoutes(path: string) {
  return renderPage(
    <Routes>
      <Route path="/devices/push-runs" element={<PushRunsPage />} />
      <Route path="/devices/push-runs/:runId" element={<PushRunPage />} />
    </Routes>,
    path,
  );
}

beforeEach(() => mockUseApi.mockReset());

// --------------------------------------------------------------------- tests

describe("push install pages in Spanish", () => {
  it("words the authorizations page, its readiness panel and the grant form", async () => {
    stubApi();
    const { container } = renderPage(<PushAuthorizationsPage />);
    expect(await screen.findByRole("heading", { name: "Autorizaciones de instalación remota" })).toBeInTheDocument();
    expect(await screen.findByText(/WinRM respondió en 1 de 2 equipos Windows sondeados/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Ámbitos de credenciales" })).toBeInTheDocument();
    expect(screen.getAllByText("Revocar").length).toBeGreaterThan(0);
    expectNoEnglish(container);

    // The grant form: a narrow prefix with an expiry, a wide one, and a refusal.
    fireEvent.change(screen.getByLabelText("Prefijo"), { target: { value: "10.4.7.0/24" } });
    fireEvent.change(screen.getByLabelText("Caduca (opcional)"), { target: { value: "2026-12-01" } });
    expect(await screen.findByText(/Caduca al comienzo del/)).toBeInTheDocument();
    expectNoEnglish(container);
    fireEvent.change(screen.getByLabelText("Prefijo"), { target: { value: "10.0.0.0/8" } });
    fireEvent.change(screen.getByLabelText("Caduca (opcional)"), { target: { value: "" } });
    expect(await screen.findByText(/Es una autorización muy amplia/)).toBeInTheDocument();
    expect(screen.getByText(/Sin caducidad: esta autorización sigue vigente/)).toBeInTheDocument();
    expectNoEnglish(container);
    fireEvent.change(screen.getByLabelText("Prefijo"), { target: { value: "nope" } });
    expect(await screen.findByText(/Eso no es un prefijo/)).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("words the authorizations page for someone who cannot manage scopes, and an empty list", async () => {
    stubApi({ listPushAuthorizations: vi.fn().mockResolvedValue([]), listPushCredentialScopes: vi.fn().mockResolvedValue([]), listCredentials: vi.fn().mockResolvedValue([]) }, ["credentials.read"]);
    const { container } = renderPage(<PushAuthorizationsPage />);
    expect(await screen.findByText(/Ningún rango de direcciones está autorizado/)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText(/Comprobando/)).toBeNull());
    expectNoEnglish(container);
  });

  it("words the credential scopes page and its bind form", async () => {
    const user = userEvent.setup();
    stubApi();
    const { container } = renderPage(<PushCredentialScopesPage />);
    expect(await screen.findByRole("heading", { name: "Ámbitos de credenciales" })).toBeInTheDocument();
    expect(await screen.findByText(/WinRM respondió en 1 de 2 equipos Windows sondeados/)).toBeInTheDocument();
    expect(screen.getByText("(credencial eliminada)")).toBeInTheDocument();
    expect(screen.getByText("Desvinculado")).toBeInTheDocument();
    expect(screen.getByText("3 ámbitos")).toBeInTheDocument();
    expect(screen.getByText("1 dirección")).toBeInTheDocument();
    expectNoEnglish(container);

    const picker = await screen.findByLabelText("Credencial", { selector: "select" });
    await waitFor(() => expect(within(picker).getAllByRole("option")).toHaveLength(3));
    expect(within(picker).getByRole("option", { name: /Cuenta de dominio de Lisboa · Cuenta de dominio de Windows/ })).toBeInTheDocument();
    await user.selectOptions(picker, "c1");
    fireEvent.change(screen.getByLabelText("Prefijo"), { target: { value: "10.4.7.0/24" } });
    expect(await screen.findByText(/Vincular no autoriza nada por sí solo/)).toBeInTheDocument();
    expectNoEnglish(container);
    fireEvent.change(screen.getByLabelText("Prefijo"), { target: { value: "10.4.7.0/24/9" } });
    expect(await screen.findByText(/Eso no es un prefijo/)).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("explains an empty credential picker, and one the account may not read", async () => {
    stubApi({ listCredentials: vi.fn().mockResolvedValue([]), listPushCredentialScopes: vi.fn().mockResolvedValue([]) });
    const first = renderPage(<PushCredentialScopesPage />);
    expect(await screen.findByText(/Todavía no existe ninguna credencial de instalación remota/)).toBeInTheDocument();
    expect(screen.getByText(/Ninguna credencial está vinculada a un prefijo/)).toBeInTheDocument();
    expectNoEnglish(first.container);
    first.unmount();

    stubApi({ listCredentials: vi.fn().mockRejectedValue(new ApiError(403, JSON.stringify({ detail: "no" }))) });
    const second = renderPage(<PushCredentialScopesPage />);
    expect(await screen.findByText(/Esta cuenta no puede ver las credenciales, así que no se puede mostrar la lista/)).toBeInTheDocument();
    expectNoEnglish(second.container);
  });

  it("words the push runs list", async () => {
    stubApi();
    const { container } = renderRunRoutes("/devices/push-runs");
    expect(await screen.findByRole("heading", { name: "Ejecuciones de instalación remota" })).toBeInTheDocument();
    const table = await screen.findByRole("table", { name: "Ejecuciones de instalación remota" });
    expect(within(table).getByText("1 instalado · 1 fallido · 1 rechazado · 3 pendientes")).toBeInTheDocument();
    expect(within(table).getByText("En ejecución")).toBeInTheDocument();
    expect(within(table).getByText("Terminada")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("words an empty push runs list", async () => {
    stubApi({ listPushRuns: vi.fn().mockResolvedValue([]) });
    const { container } = renderRunRoutes("/devices/push-runs");
    expect(await screen.findByText("No se ha iniciado ninguna instalación remota por subred.")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("words a running push run: every machine state, why each was refused, and the cancel confirmation", async () => {
    const user = userEvent.setup();
    stubApi({ getPushRun: vi.fn().mockResolvedValue(run("running", { signed_in: false })) });
    const { container } = renderRunRoutes("/devices/push-runs/run-1");
    expect(await screen.findByRole("heading", { name: "Ejecución de instalación remota en 10.20.4.0/24" })).toBeInTheDocument();
    const table = await screen.findByRole("table", { name: "Equipos de esta ejecución" });
    expect(within(table).getAllByText("Instalado").length).toBeGreaterThan(0);
    expect(within(table).getByText("Esperando un hueco libre.")).toBeInTheDocument();
    expect(within(table).getByText(/La clave de host cambió|Su clave de host SSH cambió desde que se confirmó/)).toBeInTheDocument();
    expect(within(table).getByText(/Ya está ejecutando el agente\./)).toBeInTheDocument();
    expect(within(table).getByText("Se rechazó la credencial: Permiso denegado")).toBeInTheDocument();
    expect(within(table).getByText("Terminado")).toBeInTheDocument();
    expect(screen.getByText(/Un equipo cada vez hasta que un inicio de sesión tenga éxito/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Pausar" })).toBeInTheDocument();
    expectNoEnglish(container, ["Permiso denegado"]);

    await user.click(screen.getByRole("button", { name: "Cancelar ejecución" }));
    const dialog = await screen.findByRole("dialog", { name: "Cancelar esta ejecución de instalación remota" });
    expectNoEnglish(dialog);
  });

  it("words a paused run and a cancelled run", async () => {
    stubApi({ getPushRun: vi.fn().mockResolvedValue(run("paused")) });
    const paused = renderRunRoutes("/devices/push-runs/run-1");
    expect(await screen.findByText(/En pausa: no se instala nada nuevo/)).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Reanudar" })).toBeInTheDocument();
    await screen.findByRole("table", { name: "Equipos de esta ejecución" });
    expectNoEnglish(paused.container, ["Permiso denegado"]);
    paused.unmount();

    stubApi({ getPushRun: vi.fn().mockResolvedValue(run("cancelled")), listPushRunTargets: vi.fn().mockResolvedValue({ total: 0, items: [], reason_help: {} }) });
    const cancelled = renderRunRoutes("/devices/push-runs/run-1");
    expect(await screen.findByText(/Esta ejecución se canceló/)).toBeInTheDocument();
    expect(await screen.findByText("Esta ejecución no tiene equipos.")).toBeInTheDocument();
    expectNoEnglish(cancelled.container);
  });

  it("words a run the lockout guard stopped (stopped_credential)", async () => {
    stubApi({ getPushRun: vi.fn().mockResolvedValue(run("stopped_credential")) });
    const { container } = renderRunRoutes("/devices/push-runs/run-1");
    expect(await screen.findByText(/Detenida para proteger la credencial: la rechazaron 3 equipos/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reanudar" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Pausar" })).toBeNull();
    await screen.findByRole("table", { name: "Equipos de esta ejecución" });
    expectNoEnglish(container, ["Permiso denegado"]);
  });

  it("words the pre-flight panel: SSH and Windows targets, a confirmation, and a refusal", async () => {
    const user = userEvent.setup();
    const refusal = new ApiError(
      422,
      JSON.stringify({
        detail: {
          message: "refused",
          targets: [{ address: "10.0.4.2", reasons: ["host_key_unconfirmed", "no_authorization", "brand_new"] }],
          reason_help: { brand_new: "algo nuevo del servidor" },
        },
      }),
    );
    stubApi({
      listPushHostKeys: vi.fn().mockResolvedValue([
        hostKey("10.0.4.1", { confirmed_fingerprint: "SHA256:q3Vd2mX0Jc8pZ1uYkT7rHs4nLw9aGf6eBi5oCxRtUvE", installable: true }),
        hostKey("10.0.4.2"),
        hostKey("10.0.4.3", { confirmed_fingerprint: "SHA256:OLD", observed_fingerprint: "SHA256:NEW" }),
        winrmRow("10.0.4.5", { winrm_https: true, winrm_http: false, kdc_reachable: false }),
        winrmRow("10.0.4.6"),
        winrmRow("10.0.4.7", { winrm_http: true, kdc_reachable: true }),
      ]),
      requestPush: vi.fn().mockRejectedValue(refusal),
      confirmPushHostKey: vi.fn().mockRejectedValue(new ApiError(409, JSON.stringify({ detail: "esa no es la huella que observó el último sondeo" }))),
    });
    const targets = ["10.0.4.1", "10.0.4.2", "10.0.4.3", "10.0.4.4", "10.0.4.5", "10.0.4.6", "10.0.4.7", "10.0.4.8"];
    const { container } = renderPage(
      <PushPreflightPanel targets={targets} windowsTargets={["10.0.4.8"]} credential={{ id: "c1", name: "Cuenta de dominio de Lisboa", type: "windows_domain" }} />,
    );
    expect(await screen.findByRole("table", { name: "Destinos SSH" })).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Destinos Windows" })).toBeInTheDocument();
    expect(await screen.findByText("Destinos (8)")).toBeInTheDocument();
    expect(screen.getAllByText(/listo \(Kerberos\)/).length).toBeGreaterThan(0);
    expectNoEnglish(container, ["ssh-ed25519"]);

    await user.click(screen.getByRole("button", { name: /^Confirmar SHA256:q3Vd2mX0Jc8…/ }));
    expect(await screen.findByText(/No se confirmó la clave de host\./)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Sondear los destinos" }));
    expect(await screen.findByText("Se rechazó esta instalación remota. No se creó nada.")).toBeInTheDocument();
    expect(screen.getByText(/Algo nuevo del servidor\./)).toBeInTheDocument();
    expectNoEnglish(container, ["ssh-ed25519"]);
  });

  it("words the pre-flight panel for SSH only, ready, and the dispatched batch", async () => {
    const user = userEvent.setup();
    stubApi({
      listPushHostKeys: vi.fn().mockResolvedValue([hostKey("10.0.4.1", { confirmed_fingerprint: "SHA256:q3Vd2mX0Jc8pZ1uYkT7rHs4nLw9aGf6eBi5oCxRtUvE", installable: true })]),
      requestPush: vi.fn().mockResolvedValue({ batch_id: "b-0001abcdef", kind: "install", targets: [] }),
    });
    const { container } = renderPage(<PushPreflightPanel targets={["10.0.4.1"]} credential={{ id: "c1", name: "Instalación del rack B4", type: "push_install" }} />);
    const install = await screen.findByRole("button", { name: "Instalar de forma remota en 1 equipo" });
    await waitFor(() => expect(install).toBeEnabled());
    await user.click(install);
    expect(await screen.findByText("lote b-0001ab enviado")).toBeInTheDocument();
    expectNoEnglish(container, ["ssh-ed25519"]);
  });

  it("words the install flow for ticked machines: what stops a push, a mixed selection and the credential picker", async () => {
    const user = userEvent.setup();
    stubApi();
    const bare = device({ id: "d-2", hostname: "", primary_ip: null, os_family: null, architecture: "", fqdn: null });
    const blocked = renderPage(<PushInstallChooser machines={[device(), bare]} onRemove={() => {}} onClose={() => {}} />);
    expect(await screen.findByRole("table", { name: "Equipos donde instalar" })).toBeInTheDocument();
    expect(screen.getByText("Activo sin nombre")).toBeInTheDocument();
    expect(screen.getByText(/La instalación remota no puede ejecutarse hasta que todos los equipos de arriba estén listos/)).toBeInTheDocument();
    expect(screen.getAllByText("Sin registrar").length).toBeGreaterThan(0);
    expectNoEnglish(blocked.container);
    blocked.unmount();

    const windows = device({ id: "d-4", hostname: "lis-ws-32", primary_ip: "10.20.4.32", os_family: "windows", fqdn: "lis-ws-32.corp.local" });
    const mixed = renderPage(<PushInstallChooser machines={[device(), windows]} onRemove={() => {}} onClose={() => {}} />);
    expect(await screen.findByText(/Instala los equipos Windows por separado de los Linux y macOS/)).toBeInTheDocument();
    expectNoEnglish(mixed.container);
    mixed.unmount();

    const ready = renderPage(<PushInstallChooser machines={[device()]} onRemove={() => {}} onClose={() => {}} />);
    const picker = await screen.findByLabelText("Credencial");
    await waitFor(() => expect(within(picker).getAllByRole("option")).toHaveLength(2));
    expectNoEnglish(ready.container);
    await user.selectOptions(picker, "c1");
    expect(await screen.findByText("Confirmar esta instalación remota")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText(/Cargando/)).toBeNull());
    expectNoEnglish(ready.container, ["ssh-ed25519"]);
  });

  it("words the install flow when no credential exists, and when none can be read", async () => {
    stubApi({ listCredentials: vi.fn().mockResolvedValue([]) });
    const none = renderPage(<PushInstallChooser machines={[device()]} onRemove={() => {}} onClose={() => {}} />);
    expect(await screen.findByText(/No hay guardada ninguna credencial de tipo “Instalación remota”\./)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Añade una en Credenciales" })).toBeInTheDocument();
    expectNoEnglish(none.container);
    none.unmount();

    stubApi({ listCredentials: vi.fn().mockRejectedValue(new ApiError(403, JSON.stringify({ detail: "no" }))) });
    const denied = renderPage(<PushInstallChooser machines={[device()]} onRemove={() => {}} onClose={() => {}} />);
    expect(await screen.findByText("Esta cuenta no puede ver las credenciales, así que no se puede ofrecer ninguna.")).toBeInTheDocument();
    expectNoEnglish(denied.container);
  });

  it("words the By subnet flow: the list, what is not listed and why, and a changed list", async () => {
    const user = userEvent.setup();
    const api = stubApi({
      createPushRun: vi.fn().mockRejectedValue(new ApiError(409, JSON.stringify({ detail: { code: "list_changed", message: "changed" } }))),
    });
    const { container } = renderPage(
      <Routes>
        <Route path="/" element={<PushInstallChooser machines={[]} onRemove={() => {}} onClose={() => {}} />} />
      </Routes>,
    );
    // The credential first (MSP clients phase 2, Task 12): until then the
    // subnet list says so, in Spanish.
    expect(await screen.findByRole("option", { name: "Elige primero la credencial" })).toBeInTheDocument();
    expect(screen.getByText(/instalados 50 a la vez/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("option", { name: /Instalación del rack B4/ })).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText("Credencial"), "c1");
    await waitFor(() => expect(screen.getByRole("option", { name: /10\.20\.4\.0\/24/ })).toBeInTheDocument());
    expect(screen.getByRole("option", { name: "10.20.4.0/24 (256 direcciones)" })).toBeInTheDocument();
    await user.selectOptions(await screen.findByLabelText("Subred"), "10.20.4.0/24");
    const table = await screen.findByRole("table", { name: "Equipos de esta subred" });
    expect(within(table).getByText("Sin sondear")).toBeInTheDocument();
    expect(within(table).getByText("El tipo de credencial no coincide")).toBeInTheDocument();
    expect(within(table).getByText("Sin nombre DNS")).toBeInTheDocument();
    expect(within(table).getByText("No está listo")).toBeInTheDocument();
    expect(screen.getByText(/3 equipos de esta subred no aparecen en la lista porque nadie los ha avalado/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Reclámalos en Descubiertos" })).toBeInTheDocument();
    expect(screen.getByText(/1 equipo de esta subred no aparece en la lista porque está pendiente de revisión/)).toBeInTheDocument();
    expectNoEnglish(container);

    await user.click(await screen.findByRole("button", { name: "Iniciar la instalación remota en 2 equipos" }));
    expect(await screen.findByText(/Los equipos de esta subred cambiaron desde que se mostró la lista/)).toBeInTheDocument();
    await waitFor(() => expect(api.previewPushRun).toHaveBeenCalledTimes(2));
    expectNoEnglish(container);
  });

  it("words the By subnet flow with one unlisted machine, no authorized subnet, and a refused listing", async () => {
    const user = userEvent.setup();
    const pickCredential = async () => {
      await waitFor(() => expect(screen.getByRole("option", { name: /Instalación del rack B4/ })).toBeInTheDocument());
      await user.selectOptions(screen.getByLabelText("Credencial"), "c1");
    };
    stubApi({ listPushRunPrefixes: vi.fn().mockResolvedValue([]) });
    const none = renderPage(<PushInstallChooser machines={[]} onRemove={() => {}} onClose={() => {}} />);
    await pickCredential();
    expect(await screen.findByText(/Todavía no hay ninguna subred autorizada para la instalación remota del cliente de esta credencial/)).toBeInTheDocument();
    expectNoEnglish(none.container);
    none.unmount();

    stubApi({ previewPushRun: vi.fn().mockResolvedValue(preview({ unvouched_count: 1, awaiting_review_count: 0, ready_count: 1, ambiguous_count: 1 })) });
    const one = renderPage(<PushInstallChooser machines={[]} onRemove={() => {}} onClose={() => {}} />);
    await pickCredential();
    await waitFor(() => expect(screen.getByRole("option", { name: /10\.20\.4\.0\/24/ })).toBeInTheDocument());
    await user.selectOptions(await screen.findByLabelText("Subred"), "10.20.4.0/24");
    expect(await screen.findByText(/1 equipo de esta subred no aparece en la lista porque nadie lo ha avalado/)).toBeInTheDocument();
    expect(screen.getByText("1 dirección de esta subred no aparece en la lista porque no nombra un solo equipo.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Iniciar la instalación remota en 1 equipo" })).toBeInTheDocument();
    expectNoEnglish(one.container);
    one.unmount();

    stubApi({ listPushRunPrefixes: vi.fn().mockRejectedValue(new ApiError(403, JSON.stringify({ detail: "no" }))) });
    const refused = renderPage(<PushInstallChooser machines={[]} onRemove={() => {}} onClose={() => {}} />);
    await pickCredential();
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expectNoEnglish(refused.container);
  });
});

describe("a live language switch on the push install pages", () => {
  it("re-words a stopped run already on screen", async () => {
    stubApi({ getPushRun: vi.fn().mockResolvedValue(run("stopped_credential")) });
    renderRunRoutes("/devices/push-runs/run-1");
    expect(await screen.findByText(/Detenida para proteger la credencial: la rechazaron 3 equipos/)).toBeInTheDocument();
    await screen.findByText("Esperando un hueco libre.");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("heading", { name: "Push run on 10.20.4.0/24" })).toBeInTheDocument();
    expect(screen.getByText(/Stopped to protect the credential: it was refused by 3 machines/)).toBeInTheDocument();
    expect(screen.getByText("Waiting for a free slot.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resume" })).toBeInTheDocument();
  });

  it("re-words the authorizations page and its readiness panel already on screen", async () => {
    stubApi();
    renderPage(<PushAuthorizationsPage />);
    expect(await screen.findByText(/WinRM respondió en 1 de 2 equipos Windows sondeados/)).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("heading", { name: "Push authorizations" })).toBeInTheDocument();
    expect(screen.getByText(/WinRM answered on 1 of 2 Windows machines probed/)).toBeInTheDocument();
    expect(screen.getByText(/2 ranges are authorized\./)).toBeInTheDocument();
    // A grant's status label and its tooltip are catalogue lookups, not stored text.
    expect(screen.getAllByText("Expired").length).toBeGreaterThan(0);
  });
});
