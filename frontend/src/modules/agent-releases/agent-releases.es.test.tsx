/**
 * Agent packages in Spanish: the catalogue (empty for an installer and for a
 * publisher, then full with an older version folded away), the install-command
 * confirmation and dialog, the managed-deployment dialog, the install link with
 * its one-time reveal and the Group Policy MSI, the publish dialog (file
 * choice, the problems found, a run with its failures), the release channels,
 * and the self-update section (fleet versions, rollouts, a rollout's rings and
 * devices, and the new-rollout dialog). Versions, hostnames, group names and
 * the text the server itself writes (staging steps, blast-radius lines) read
 * as recorded; the words around them are Spanish.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import { ApiError } from "../../lib/api";
import type { AgentRelease, ApiClient, FleetVersions, UpdateRollout } from "../../lib/api";
import { AgentReleasesPage } from "./AgentReleasesPage";
import { PublishReleaseDialog } from "./PublishReleaseDialog";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
vi.mock("../../lib/download", () => ({ saveBlob: vi.fn() }));

const PUBLISHER = [
  "agent.release.manage",
  "agent.update.read",
  "agent.update.rollout",
  "enrollment.link.manage",
  "devices.read",
];

// ------------------------------------------------------------------ fixtures

const SHA = "8b3421e0c7a1f9d2e4b6a8c0d2f4e6a8b0c2d4e6f8a0b2c4d6e8f0a2b4c6d8e0";

function release(overrides: Partial<AgentRelease> = {}): AgentRelease {
  return {
    id: "rel-1",
    version: "1.8.2",
    os: "linux",
    arch: "amd64",
    size_bytes: 16_777_216,
    sha256: SHA,
    signed: false,
    notes: "",
    published_at: "2026-09-20T09:00:00Z",
    has_artifact: true,
    channel: "stable",
    release_signed: true,
    ...overrides,
  };
}

const RELEASES: AgentRelease[] = [
  release({ id: "rel-1" }),
  release({ id: "rel-2", os: "windows", signed: true }),
  release({ id: "rel-3", os: "darwin", arch: "arm64", size_bytes: 0, sha256: "", has_artifact: false, release_signed: false }),
  release({ id: "rel-4", version: "1.8.1", release_signed: false, channel: "pilot" }),
  release({ id: "rel-5", version: "1.8.2-rc1", os: "linux", arch: "arm64", channel: "pilot", release_signed: false }),
];

const FLEET: FleetVersions = {
  total: 3,
  versions: { "1.8.2": 1, "1.0.0": 1, unknown: 1 },
  latest_stable: "1.8.2",
  update_available: 2,
  needs_manual_update: [
    { device_id: "d-old", hostname: "portatil-antiguo", version: "", reason: "version_unknown" },
    { device_id: "d-pre", hostname: "equipo-previo", version: "1.0.0", reason: "no_agent_update" },
  ],
};

function rollout(overrides: Partial<UpdateRollout> = {}): UpdateRollout {
  return {
    id: "r1",
    target_version: "1.8.2",
    is_rollback: false,
    state: "running",
    batch_size: 10,
    failure_threshold_pct: 5,
    health_window_s: 600,
    reason: "",
    pause_reason: null,
    created_at: "2026-09-23T09:00:00Z",
    started_at: "2026-09-23T09:01:00Z",
    completed_at: null,
    rings: [
      { id: "g0", ordinal: 0, name: "Piloto", is_pilot: true, device_group_id: "grp", state: "done", activated_at: "2026-09-23T09:01:00Z", counts: { succeeded: 2, failed: 1 } },
      { id: "g1", ordinal: 1, name: "Todos los demás", is_pilot: false, device_group_id: null, state: "pending", activated_at: null, counts: { pending: 4 } },
    ],
    ...overrides,
  };
}

const TARGETS = ["pending", "dispatched", "verifying", "succeeded", "failed", "rolled_back", "refused", "lost", "skipped", "needs_manual_update"].map((state, at) => ({
  id: `t${at}`,
  ring_id: "g0",
  device_id: `d${at}`,
  hostname: at % 2 === 0 ? `equipo-${at}` : null,
  from_version: at === 3 ? "" : "1.8.1",
  state,
  reason: ["", "", "", "", "ring_closed", "cancelled", "", "", "motivo del servidor", ""][at]!,
  attempts: 1,
  job_id: null,
  updated_at: "2026-09-23T09:05:00Z",
}));

const LINK = {
  enabled: true,
  has_code: true,
  networks: ["10.20.0.0/16"],
  disable_at: "2026-10-30T12:00:00Z",
  hourly_limit: 50,
  enabled_at: "2026-09-29T09:00:00Z",
  joined_since_enabled: 1,
};
const LINK_URL = "https://consola.example/i/ABCDEFGHIJKLMNOPQRSTUVWXYZ012345";

const PACKAGE = {
  release: RELEASES[0],
  channel: "gpo",
  download_url: "https://consola.example/artifacts/its-agent-linux-amd64",
  download_expires_in_s: 900,
  sha256: SHA,
  staging: [
    { title: "Descargar el artefacto", detail: "Descárgalo una vez, en el equipo del administrador.", command: "curl -fsSL -o its-agent https://consola.example/a" },
    { title: "Copiar al recurso compartido", detail: "Cópialo al recurso compartido SYSVOL.", command: "" },
  ],
  install_command: "its-agent install --server consola.example",
  enrol_command: "its-agent enroll --server consola.example --token-from-env ITS_ENROLL_TOKEN",
  integrity: {
    sha256: SHA,
    verify_command: `echo "${SHA}  its-agent" | sha256sum -c`,
    verified_at_install: false,
    notes: ["El punto de distribución copia el archivo y lo ejecuta; no comprueba ningún resumen."],
  },
};

function stubApi(overrides: Record<string, unknown> = {}, permissions: string[] = PUBLISHER) {
  const api = {
    me: vi.fn().mockResolvedValue({ user_id: "u", org_id: "o", email: "op@example.com", display_name: "Operadora", permissions, timezone: "UTC" }),
    listAgentReleases: vi.fn().mockResolvedValue(RELEASES),
    generateAgentInstallCommand: vi.fn().mockResolvedValue({
      release: RELEASES[0],
      token: "ENR-ficticio-de-un-solo-uso",
      token_expires_at: "2026-10-01T13:00:00Z",
      download_url: "https://consola.example/artifacts/its-agent-linux-amd64",
      download_expires_in_s: 900,
      sha256: SHA,
      command: "curl -fsSL https://consola.example/a -o its-agent && sudo ./its-agent enroll --token ENR-ficticio-de-un-solo-uso",
    }),
    getAgentDeploymentPackage: vi.fn().mockResolvedValue(PACKAGE),
    mintAgentDeploymentToken: vi.fn().mockResolvedValue({
      token: "ENR-ficticio-de-flota",
      token_id: "tok-1",
      token_expires_at: "2026-10-04T09:00:00Z",
      max_uses: 50,
      channel: "gpo",
      blast_radius: ["Cualquiera de hasta 50 equipos que lean el punto de distribución puede inscribirse con él."],
    }),
    getInstallLink: vi.fn().mockResolvedValue(LINK),
    saveInstallLink: vi.fn().mockResolvedValue({ ...LINK, url: null }),
    regenerateInstallLink: vi.fn().mockResolvedValue({ ...LINK, url: LINK_URL }),
    downloadInstallLinkMsi: vi.fn().mockResolvedValue({ blob: new Blob(["msi"]), filename: "ITMS-Agent.msi", sha256: "9c".repeat(32) }),
    getFleetVersions: vi.fn().mockResolvedValue(FLEET),
    listUpdateRollouts: vi.fn().mockResolvedValue([rollout(), rollout({ id: "r2", state: "paused", is_rollback: true, pause_reason: "failure_threshold", target_version: "1.8.1" })]),
    listUpdateTargets: vi.fn().mockResolvedValue(TARGETS),
    listDeviceGroups: vi.fn().mockResolvedValue({
      items: [
        { id: "grp", name: "Equipos de prueba", parent_group_id: null, description: "", dynamic: false, device_count: 3, precedence: 1, archived_at: null },
        { id: "grp2", name: "Toda la flota", parent_group_id: null, description: "", dynamic: false, device_count: 200, precedence: 2, archived_at: null },
      ],
      total: 2,
      ungrouped_device_count: 0,
      sweep: null,
    }),
    listDevices: vi.fn().mockResolvedValue({ items: [], total: 3, state_counts: {}, fleet: { total: 3, managed: 2, online: 1, cert_attention: 0 } }),
    previewUpdateRollout: vi.fn().mockResolvedValue({
      target_version: "1.8.2",
      is_rollback: false,
      channel: "stable",
      rings: [
        { ordinal: 0, name: "Piloto", is_pilot: true, device_group_id: "grp", total: 3, eligible: 2, needs_manual_update: 1, no_artifact: 0, already_current: 0, other_skipped: 0 },
      ],
    }),
    createUpdateRollout: vi.fn().mockResolvedValue({ id: "r9" }),
    updateRolloutAction: vi.fn().mockResolvedValue(rollout()),
    closeUpdateRing: vi.fn().mockResolvedValue(rollout()),
    promoteAgentReleaseVersion: vi.fn().mockResolvedValue({ version: "1.8.1", channel: "stable", rows: 1 }),
    deleteAgentReleaseVersion: vi.fn().mockResolvedValue({ version: "1.8.1", removed: 1, files_not_removed: ["its-agent-linux-amd64"] }),
    createAgentRelease: vi.fn(),
    uploadAgentReleaseArtifact: vi.fn(),
    setAgentReleaseSignature: vi.fn(),
    deleteAgentRelease: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

function renderUi(ui: ReactElement) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderInSpanish(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const renderPage = () => renderUi(<AgentReleasesPage />);

/** The first enabled button of that name: the darwin row has no file, so its buttons are off. */
async function enabledButton(name: string): Promise<HTMLElement> {
  await screen.findAllByRole("button", { name });
  await waitFor(() => expect(screen.getAllByRole("button", { name }).some((b) => !(b as HTMLButtonElement).disabled)).toBe(true));
  return screen.getAllByRole("button", { name }).find((b) => !(b as HTMLButtonElement).disabled)!;
}

/** The clipboard jsdom does not provide. */
function stubClipboard(writeText: () => Promise<void>) {
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
}

beforeEach(() => mockUseApi.mockReset());
afterEach(() => {
  Reflect.deleteProperty(navigator, "clipboard");
});

// --------------------------------------------------------------------- tests

describe("agent packages in Spanish", () => {
  it("words the empty catalogue for an installer, who cannot publish", async () => {
    stubApi({ listAgentReleases: vi.fn().mockResolvedValue([]) }, []);
    const { container } = renderPage();
    expect(await screen.findByText("Aún no hay paquetes del agente publicados.")).toBeInTheDocument();
    expect(screen.getByText(/Publicar no es algo que tu cuenta pueda hacer/)).toBeInTheDocument();
    expect(screen.getByText("Aún no se ha publicado nada")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("words the empty catalogue for a publisher", async () => {
    stubApi({ listAgentReleases: vi.fn().mockResolvedValue([]) });
    const { container } = renderPage();
    expect(await screen.findByText(/Tu cuenta puede publicar paquetes del agente/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Publicar una versión…" })).toBeInTheDocument();
    await screen.findByText(/No hay agentes inscritos|Versiones de la flota/);
    expectNoEnglish(container);
  });

  it("words the catalogue: tiles, rows, the untested caveat, and older versions folded away", async () => {
    const user = userEvent.setup();
    stubApi();
    const { container } = renderPage();
    expect(await screen.findByRole("heading", { name: "Paquetes del agente" })).toBeInTheDocument();
    expect(await screen.findByText("Versión 1.8.2, 3 paquetes. Las versiones anteriores están plegadas más abajo.")).toBeInTheDocument();
    expect(screen.getAllByText(/Aún sin probar en un equipo Windows real/).length).toBeGreaterThan(0);
    expect(screen.getByText("No se ha subido ningún archivo")).toBeInTheDocument();
    await screen.findByText("3 agentes · última estable 1.8.2 · 2 pueden actualizarse");
    expectNoEnglish(container);
    await user.click(screen.getByRole("button", { name: "Mostrar las versiones anteriores (1.8.2-rc1, 1.8.1)" }));
    expect(screen.getByRole("button", { name: "Ocultar las versiones anteriores" })).toBeInTheDocument();
    expect(screen.getByText("5 paquetes en 3 plataformas")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("words the prerequisites panel when the groups are too big, and when nothing is marked stable", async () => {
    stubApi({
      getFleetVersions: vi.fn().mockResolvedValue({ ...FLEET, latest_stable: null }),
      listDeviceGroups: vi.fn().mockResolvedValue({
        items: [{ id: "grp2", name: "Toda la flota", parent_group_id: null, description: "", dynamic: false, device_count: 200, precedence: 2, archived_at: null }],
        total: 1,
        ungrouped_device_count: 0,
        sweep: null,
      }),
    });
    const { container } = renderPage();
    expect(await screen.findByText(/todos los grupos de dispositivos con equipos tienen más de 25/)).toBeInTheDocument();
    expect(await screen.findByText(/Ninguna versión está marcada como estable/)).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("words the install command: the confirmation, the command, copying, and a refused copy", async () => {
    const user = userEvent.setup();
    stubClipboard(() => Promise.resolve());
    stubApi();
    renderPage();
    await user.click(await enabledButton("Comando de instalación…"));
    const confirm = await screen.findByRole("dialog", { name: "Generar el comando de instalación" });
    expect(within(confirm).getByText(/Caduca en 4 horas/)).toBeInTheDocument();
    expectNoEnglish(confirm);
    await user.click(within(confirm).getByRole("button", { name: "Generar comando" }));
    const dialog = await screen.findByRole("dialog", { name: "Comando de instalación" });
    expect(within(dialog).getByText(/de un solo uso/)).toBeInTheDocument();
    expect(within(dialog).getByText(/caduca en 15 minutos/)).toBeInTheDocument();
    expectNoEnglish(dialog);
    await user.click(within(dialog).getByRole("button", { name: "Copiar comando" }));
    expect(await within(dialog).findByText("Copiado al portapapeles.")).toBeInTheDocument();
    stubClipboard(() => Promise.reject(new Error("denied")));
    await user.click(within(dialog).getByRole("button", { name: /Copiado|Copiar comando/ }));
    expect(await within(dialog).findByText(/Este navegador rechazó el acceso al portapapeles/)).toBeInTheDocument();
    expectNoEnglish(dialog);
  });

  it("words a refused install command", async () => {
    const user = userEvent.setup();
    stubApi({ generateAgentInstallCommand: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    const { container } = renderPage();
    await user.click(await enabledButton("Comando de instalación…"));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Generar comando" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("words the managed deployment dialog, the channels and the token confirmation", async () => {
    const user = userEvent.setup();
    stubApi();
    renderPage();
    await user.click(await enabledButton("Despliegue administrado…"));
    const dialog = await screen.findByRole("dialog", { name: "Despliegue administrado" });
    expect(await within(dialog).findByText("Descargar el artefacto")).toBeInTheDocument();
    expect(within(dialog).getByText("El equipo no verifica nada por esta vía")).toBeInTheDocument();
    expect(within(dialog).getByText(/Un script de inicio del equipo desde un recurso compartido SYSVOL o DFS/)).toBeInTheDocument();
    // "server" and "install" are the `--server` flag and the verb of the install command the server wrote;
    // "from" is the middle of its `--token-from-env` flag.
    expectNoEnglish(dialog, ["server", "install", "from"]);
    await user.selectOptions(within(dialog).getByLabelText("Canal de distribución"), "intune");
    expect(within(dialog).getByText(/Una aplicación Win32/)).toBeInTheDocument();
    await user.selectOptions(within(dialog).getByLabelText("Canal de distribución"), "manual");
    // ("from" again: the middle of the command's `--token-from-env` flag.)
    expectNoEnglish(dialog, ["server", "install", "from"]);
    await user.click(within(dialog).getByRole("button", { name: "Crear un token de despliegue…" }));
    const confirm = await screen.findByRole("dialog", { name: "Crear un token de despliegue" });
    expect(within(confirm).getByText(/Inscribe hasta 50 equipos en esta organización durante las próximas 72 horas/)).toBeInTheDocument();
    expectNoEnglish(confirm);
    await user.click(within(confirm).getByRole("button", { name: "Crear token" }));
    expect(await within(dialog).findByText(/Se muestra una sola vez\. Caduca/)).toBeInTheDocument();
    expectNoEnglish(dialog, ["server", "install", "from"]);
  });

  it("words the install link: status, the reveal after regenerating, and the Group Policy MSI", async () => {
    const user = userEvent.setup();
    stubClipboard(() => Promise.resolve());
    stubApi();
    const { container } = renderPage();
    expect(await screen.findByText(/Activo desde .* · 1 equipo se ha unido desde entonces · se desactiva solo/)).toBeInTheDocument();
    expect(screen.getByText(/El enlace se mostró una vez, al generarlo/)).toBeInTheDocument();
    const panel = screen.getByRole("heading", { name: "Enlace de instalación" }).closest(".panel") as HTMLElement;
    expect(panel).not.toBeNull();
    expectNoEnglish(panel);

    // The MSI: a malformed link, then the right one, then a download.
    const input = within(panel).getByLabelText("Enlace de instalación");
    await user.type(input, "no es un enlace");
    expect(await within(panel).findByText(/Eso no es un enlace de instalación/)).toBeInTheDocument();
    expectNoEnglish(panel);
    await user.clear(input);
    await user.type(input, LINK_URL);
    await user.click(within(panel).getByRole("button", { name: "Descargar el MSI para la directiva de grupo (Intel o AMD de 64 bits)" }));
    expect(await within(panel).findByText(/Guardado como/)).toBeInTheDocument();
    // "Agent" is the stored file name the build gave the MSI.
    expectNoEnglish(panel, ["Agent"]);

    // Regenerating shows the new link once.
    await user.click(within(panel).getByRole("button", { name: "Generar un enlace nuevo…" }));
    const confirm = await screen.findByRole("dialog", { name: "Generar un enlace de instalación nuevo" });
    expectNoEnglish(confirm);
    await user.click(within(confirm).getByRole("button", { name: "Generar enlace nuevo" }));
    const region = await screen.findByRole("region", { name: "El nuevo enlace de instalación" });
    expect(within(region).getByText("Copia este enlace ahora.")).toBeInTheDocument();
    await user.click(within(region).getByRole("button", { name: "Copiar enlace" }));
    expect(await within(region).findByRole("button", { name: "Copiado" })).toBeInTheDocument();
    expectNoEnglish(container, ["Agent"]);
  });

  it("words the install link when it is off, refused for want of a network, and not saved", async () => {
    const user = userEvent.setup();
    stubApi({
      getInstallLink: vi.fn().mockResolvedValue({ ...LINK, enabled: false, has_code: false, networks: [], disable_at: null }),
      saveInstallLink: vi.fn().mockRejectedValue(new ApiError(422, JSON.stringify({ detail: "se necesita al menos una red" }))),
    });
    const { container } = renderPage();
    expect(await screen.findByText("Desactivado. Aún no se ha generado ningún enlace.")).toBeInTheDocument();
    expect(screen.getByText("Activa el enlace para descargar el MSI: los equipos se unen a través de él.")).toBeInTheDocument();
    await user.click(screen.getByRole("switch", { name: "Aceptar equipos nuevos a través del enlace" }));
    expect(await screen.findByText("Indica al menos una red antes de activar el enlace.")).toBeInTheDocument();
    expectNoEnglish(container);
    await user.type(screen.getByLabelText("Redes a las que responde el enlace"), "10.20.0.0/16");
    await user.click(screen.getByRole("button", { name: "Guardar el enlace de instalación" }));
    expect(await screen.findByText(/No se cambió el enlace de instalación\./)).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("words the release channels, their confirmations, and a removal that left files behind", async () => {
    const user = userEvent.setup();
    const api = stubApi();
    const { container } = renderPage();
    const channels = (await screen.findByRole("table", { name: "Canales de versiones" })).closest(".panel") as HTMLElement;
    expect(within(channels).getAllByText("Falta en 1 de 1").length).toBe(2);
    expect(within(channels).getAllByText("Piloto").length).toBeGreaterThan(0);
    expectNoEnglish(channels);

    const row181 = within(channels).getByText("1.8.1").closest("tr") as HTMLElement;
    await user.click(within(row181).getByRole("button", { name: "Marcar como estable…" }));
    const stable = await screen.findByRole("dialog", { name: "Marcar como estable" });
    expect(within(stable).getByText(/Esto se aplica a 1 paquete de 1\.8\.1\./)).toBeInTheDocument();
    expect(within(stable).getByText(/no tiene registrada la firma de la versión/)).toBeInTheDocument();
    expectNoEnglish(stable);
    await user.click(within(stable).getByRole("button", { name: "Cancelar" }));

    const row182 = within(channels).getByText("1.8.2").closest("tr") as HTMLElement;
    await user.click(within(row182).getByRole("button", { name: "Devolver a piloto…" }));
    const pilot = await screen.findByRole("dialog", { name: "Devolver a piloto" });
    expect(within(pilot).getByText(/Esto se aplica a 3 paquetes de 1\.8\.2\./)).toBeInTheDocument();
    expectNoEnglish(pilot);
    await user.click(within(pilot).getByRole("button", { name: "Cancelar" }));

    await user.click(within(row181).getByRole("button", { name: "Quitar…" }));
    const remove = await screen.findByRole("dialog", { name: "Quitar esta versión" });
    expectNoEnglish(remove);
    await user.click(within(remove).getByRole("button", { name: "Quitar la versión" }));
    expect(await screen.findByText(/ya no está en el catálogo, pero no se pudo eliminar 1 archivo guardado/)).toBeInTheDocument();
    expect(api.deleteAgentReleaseVersion).toHaveBeenCalledWith("1.8.1");
    expectNoEnglish(container);
  });

  it("words a failed removal of a version, and a refused promotion", async () => {
    const user = userEvent.setup();
    stubApi({
      deleteAgentReleaseVersion: vi.fn().mockRejectedValue(new ApiError(409, JSON.stringify({ detail: "un despliegue activo aún la usa" }))),
      promoteAgentReleaseVersion: vi.fn().mockRejectedValue(new ApiError(503, "")),
    });
    const { container } = renderPage();
    const channels = (await screen.findByRole("table", { name: "Canales de versiones" })).closest(".panel") as HTMLElement;
    const row181 = within(channels).getByText("1.8.1").closest("tr") as HTMLElement;
    await user.click(within(row181).getByRole("button", { name: "Quitar…" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Quitar la versión" }));
    expect(await screen.findByText(/No se quitó nada\./)).toBeInTheDocument();
    expectNoEnglish(container);
    await user.click(within(row181).getByRole("button", { name: "Marcar como estable…" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Marcar como estable" }));
    await waitFor(() => expect(screen.getAllByRole("alert").length).toBeGreaterThan(0));
    expectNoEnglish(container);
  });

  it("words the self-update section: fleet versions, rollouts, and a rollout's rings and devices", async () => {
    const user = userEvent.setup();
    stubApi();
    const { container } = renderPage();
    expect(await screen.findByText("3 agentes · última estable 1.8.2 · 2 pueden actualizarse")).toBeInTheDocument();
    expect(screen.getAllByText("Requiere actualización manual").length).toBeGreaterThan(0);
    expect(screen.getByText(/Su agente no puede ejecutar agent\.update/)).toBeInTheDocument();
    expect(screen.getByText("umbral de fallos superado")).toBeInTheDocument();
    expect(screen.getByText("1.8.1 (reversión)")).toBeInTheDocument();
    expect(screen.getAllByText("1/2 hechos").length).toBeGreaterThan(0);
    expectNoEnglish(container);

    await user.click(screen.getAllByRole("button", { name: "Detalles" })[0]!);
    expect(await screen.findByText("Despliegue de 1.8.2")).toBeInTheDocument();
    expect(await screen.findByRole("table", { name: "Dispositivos de este despliegue" })).toBeInTheDocument();
    expect(screen.getByText(/10 dispositivos a la vez · se pausa si falla más del 5 % · cada dispositivo tiene/)).toBeInTheDocument();
    expect(screen.getByText("Anillo 0: Piloto")).toBeInTheDocument();
    expect(screen.getAllByText("(el primero en actualizarse)", { exact: false }).length).toBeGreaterThan(0);
    expect(screen.getByText("anillo cerrado")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("words every control of a running, a paused and a draft rollout", async () => {
    const user = userEvent.setup();
    const running = rollout({
      rings: [
        { id: "g0", ordinal: 0, name: "Piloto", is_pilot: true, device_group_id: "grp", state: "active", activated_at: "2026-09-23T09:01:00Z", counts: { dispatched: 1, succeeded: 2 } },
      ],
    });
    stubApi({ listUpdateRollouts: vi.fn().mockResolvedValue([running]), updateRolloutAction: vi.fn().mockRejectedValue(new ApiError(409, JSON.stringify({ detail: "otro despliegue está activo" }))) });
    const first = renderPage();
    await user.click((await screen.findAllByRole("button", { name: "Detalles" }))[0]!);
    const detail = (await screen.findByText("Despliegue de 1.8.2")).closest(".panel") as HTMLElement;
    for (const name of ["Pausar", "Cerrar anillo", "Cancelar"]) {
      expect(within(detail).getByRole("button", { name })).toBeInTheDocument();
    }
    await user.click(within(detail).getByRole("button", { name: "Pausar" }));
    expect(await screen.findAllByRole("alert")).not.toHaveLength(0);
    expectNoEnglish(first.container);
    first.unmount();

    stubApi({ listUpdateRollouts: vi.fn().mockResolvedValue([rollout({ state: "paused", pause_reason: "failure_threshold" })]) });
    const second = renderPage();
    await user.click((await screen.findAllByRole("button", { name: "Detalles" }))[0]!);
    const paused = (await screen.findByText("Despliegue de 1.8.2")).closest(".panel") as HTMLElement;
    expect(within(paused).getByRole("button", { name: "Reanudar" })).toBeInTheDocument();
    expect(within(paused).queryByRole("button", { name: "Pausar" })).toBeNull();
    expectNoEnglish(second.container);
    second.unmount();

    stubApi({ listUpdateRollouts: vi.fn().mockResolvedValue([rollout({ state: "draft" })]) });
    const third = renderPage();
    await user.click((await screen.findAllByRole("button", { name: "Detalles" }))[0]!);
    const draft = (await screen.findByText("Despliegue de 1.8.2")).closest(".panel") as HTMLElement;
    expect(within(draft).getByRole("button", { name: "Iniciar" })).toBeInTheDocument();
    expectNoEnglish(third.container);
  });

  it("words the new rollout dialog and its preview", async () => {
    const user = userEvent.setup();
    const api = stubApi();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Nuevo despliegue…" }));
    const dialog = await screen.findByRole("dialog", { name: "Nuevo despliegue de actualización del agente" });
    await waitFor(() => expect(within(dialog).getByRole("option", { name: "1.8.2 (estable)" })).toBeInTheDocument());
    expect(within(dialog).getByRole("option", { name: "1.8.1 (piloto)" })).toBeInTheDocument();
    expectNoEnglish(dialog);
    await user.selectOptions(within(dialog).getByLabelText("Versión"), "1.8.2");
    await user.selectOptions(within(dialog).getByLabelText("Anillo piloto: grupo de dispositivos (25 dispositivos como máximo)"), "grp");
    await user.click(within(dialog).getByRole("button", { name: "Vista previa" }));
    expect(await within(dialog).findByRole("table", { name: "Vista previa" })).toBeInTheDocument();
    expectNoEnglish(dialog);
    await user.click(within(dialog).getByRole("button", { name: "Crear e iniciar" }));
    expect(api.createUpdateRollout).toHaveBeenCalledWith(
      expect.objectContaining({ rings: [expect.objectContaining({ name: "Pilot" }), expect.objectContaining({ name: "Everyone else" })] }),
    );
  });

  it("words a fleet of one agent in the singular", async () => {
    stubApi({ getFleetVersions: vi.fn().mockResolvedValue({ ...FLEET, total: 1, update_available: 1, needs_manual_update: [] }) });
    renderPage();
    expect(await screen.findByText("1 agente · última estable 1.8.2 · 1 puede actualizarse")).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("1 agents · latest stable 1.8.2 · 1 can update")).toBeInTheDocument();
  });

  it("shows the two default ring names in Spanish while storing the wire names", async () => {
    const user = userEvent.setup();
    const api = stubApi({
      listUpdateRollouts: vi.fn().mockResolvedValue([
        rollout({
          rings: [
            { id: "g0", ordinal: 0, name: "Pilot", is_pilot: true, device_group_id: "grp", state: "done", activated_at: null, counts: {} },
            { id: "g1", ordinal: 1, name: "Everyone else", is_pilot: false, device_group_id: null, state: "pending", activated_at: null, counts: {} },
          ],
        }),
      ]),
      listUpdateTargets: vi.fn().mockResolvedValue([{ ...TARGETS[0]!, ring_id: "g1" }]),
      previewUpdateRollout: vi.fn().mockResolvedValue({
        target_version: "1.8.2",
        is_rollback: false,
        channel: "stable",
        rings: [
          { ordinal: 0, name: "Pilot", is_pilot: true, device_group_id: "grp", total: 3, eligible: 2, needs_manual_update: 1, no_artifact: 0, already_current: 0, other_skipped: 0 },
          { ordinal: 1, name: "Everyone else", is_pilot: false, device_group_id: null, total: 9, eligible: 9, needs_manual_update: 0, no_artifact: 0, already_current: 0, other_skipped: 0 },
        ],
      }),
    });
    renderPage();
    await user.click((await screen.findAllByRole("button", { name: "Detalles" }))[0]!);
    expect(await screen.findByText("Anillo 0: Piloto")).toBeInTheDocument();
    expect(screen.getByText("Anillo 1: Todos los demás")).toBeInTheDocument();
    const targets = await screen.findByRole("table", { name: "Dispositivos de este despliegue" });
    expect(within(targets).getByText("Todos los demás")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Nuevo despliegue…" }));
    const dialog = await screen.findByRole("dialog", { name: "Nuevo despliegue de actualización del agente" });
    await waitFor(() => expect(within(dialog).getByRole("option", { name: "1.8.2 (estable)" })).toBeInTheDocument());
    await user.selectOptions(within(dialog).getByLabelText("Versión"), "1.8.2");
    await user.selectOptions(within(dialog).getByLabelText("Anillo piloto: grupo de dispositivos (25 dispositivos como máximo)"), "grp");
    await user.click(within(dialog).getByRole("button", { name: "Vista previa" }));
    const preview = await within(dialog).findByRole("table", { name: "Vista previa" });
    expect(within(preview).getByText("Piloto")).toBeInTheDocument();
    expect(within(preview).getByText("Todos los demás")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Crear e iniciar" }));
    expect(api.createUpdateRollout).toHaveBeenCalledWith(
      expect.objectContaining({ rings: [expect.objectContaining({ name: "Pilot" }), expect.objectContaining({ name: "Everyone else" })] }),
    );
  });

  it("words the publish dialog: the files chosen, the problems found, and the published result", async () => {
    const user = userEvent.setup();
    const sigLinux = `${"L".repeat(86)}==`;
    const shaLinux = "1".repeat(64);
    const signatures =
      `${shaLinux}  its-agent-linux-amd64  linux/amd64  ${sigLinux}\n` +
      "una línea ilegible\n" +
      `${"2".repeat(64)}  a  windows/amd64  ${"W".repeat(86)}==\n` +
      `${"3".repeat(64)}  b  windows/amd64  ${"V".repeat(86)}==\n`;
    const created = new Map<string, AgentRelease>();
    const api = stubApi({
      createAgentRelease: vi.fn().mockImplementation((body: { version: string; os: string; arch: string }) => {
        const row = release({ id: `id-${body.os}-${body.arch}`, version: body.version, os: body.os, arch: body.arch, sha256: "", has_artifact: false });
        created.set(row.id, row);
        return Promise.resolve(row);
      }),
      uploadAgentReleaseArtifact: vi.fn().mockImplementation((id: string) =>
        Promise.resolve({ ...created.get(id)!, sha256: id.includes("linux") ? shaLinux : "f".repeat(64), has_artifact: true }),
      ),
      setAgentReleaseSignature: vi.fn().mockResolvedValue(undefined),
    });
    const { container } = renderUi(<PublishReleaseDialog onClose={() => {}} />);
    const dialog = await screen.findByRole("dialog", { name: "Publicar una versión" });
    expectNoEnglish(dialog);

    // Nothing chosen yet: the form says what is missing.
    await user.click(within(dialog).getByRole("button", { name: "Publicar" }));
    expect(await within(dialog).findByText("Escribe la versión que compiló el flujo de publicación.")).toBeInTheDocument();
    expect(within(dialog).getByText("Elige al menos un binario del agente.")).toBeInTheDocument();
    expectNoEnglish(dialog);

    await user.type(within(dialog).getByLabelText("Versión"), "v1.4");
    const files = [
      new File(["x"], "its-agent-linux-amd64"),
      new File(["x"], "its-agent-windows-amd64.exe"),
      new File(["x"], "descarga (1)"),
      new File(["x"], "SHA256SUMS"),
      new File([signatures], "SIGNATURES"),
    ];
    await user.upload(within(dialog).getByLabelText("Binarios del agente"), files);
    expect(await within(dialog).findByText(/Archivo de firmas/)).toBeInTheDocument();
    expect(within(dialog).getByText("La línea 2 del archivo de firmas no tiene el formato esperado.")).toBeInTheDocument();
    expect(within(dialog).getByText(/más de una línea para Windows \(Intel\/AMD de 64 bits\)/)).toBeInTheDocument();
    expect(within(dialog).getByText("Encontrada; comprobada con lo subido")).toBeInTheDocument();
    expect(within(dialog).getAllByText("Sin línea para esta plataforma").length).toBeGreaterThan(0);
    await user.click(within(dialog).getByRole("button", { name: "Publicar" }));
    expect(await within(dialog).findByText("La versión debe ser un número de versión simple, como 1.4.0, sin una v delante.")).toBeInTheDocument();
    expect(within(dialog).getByText("Elige la plataforma de descarga (1).")).toBeInTheDocument();
    expectNoEnglish(dialog, ["SHA256SUMS", "SIGNATURES"]);

    await user.clear(within(dialog).getByLabelText("Versión"));
    await user.type(within(dialog).getByLabelText("Versión"), "1.4.0");
    await user.selectOptions(within(dialog).getByLabelText("Sistema operativo de descarga (1)"), "darwin");
    await user.selectOptions(within(dialog).getByLabelText("Procesador de descarga (1)"), "arm64");
    expectNoEnglish(dialog, ["SHA256SUMS", "SIGNATURES"]);
    await user.click(within(dialog).getByRole("button", { name: "Publicar" }));
    expect(await within(dialog).findByText(/de 3 archivos publicados\./)).toBeInTheDocument();
    expect(within(dialog).getByText("Publicado, firma de la versión registrada")).toBeInTheDocument();
    expect(within(dialog).getAllByText("Publicado sin firma de la versión").length).toBeGreaterThan(0);
    expect(within(dialog).getAllByText(/Los agentes se negarán a actualizarse a ella\./).length).toBeGreaterThan(0);
    expect(api.setAgentReleaseSignature).toHaveBeenCalledTimes(1);
    expectNoEnglish(dialog, ["SHA256SUMS", "SIGNATURES"]);
    expect(container).toBeTruthy();
  });

  it("words the failures of a publish run: a checksum mismatch, a refused upload and a refused signature", async () => {
    const user = userEvent.setup();
    const sig = `${"L".repeat(86)}==`;
    const signatures =
      `${"1".repeat(64)}  its-agent-linux-amd64  linux/amd64  ${sig}\n` +
      `${"2".repeat(64)}  its-agent-linux-arm64  linux/arm64  ${sig}\n` +
      `${"3".repeat(64)}  its-agent-windows-amd64.exe  windows/amd64  ${sig}\n`;
    const rows = new Map<string, AgentRelease>();
    stubApi({
      createAgentRelease: vi.fn().mockImplementation((body: { version: string; os: string; arch: string }) => {
        if (body.os === "windows") return Promise.reject(new ApiError(409, JSON.stringify({ detail: "ya existe una versión para este sistema y arquitectura" })));
        const row = release({ id: `id-${body.os}-${body.arch}`, version: body.version, os: body.os, arch: body.arch, sha256: "", has_artifact: false });
        rows.set(row.id, row);
        return Promise.resolve(row);
      }),
      uploadAgentReleaseArtifact: vi.fn().mockImplementation((id: string) => {
        if (id.includes("arm64")) return Promise.reject(new ApiError(413, JSON.stringify({ detail: "el archivo es demasiado grande" })));
        return Promise.resolve({ ...rows.get(id)!, sha256: "9".repeat(64), has_artifact: true });
      }),
      deleteAgentRelease: vi.fn().mockImplementation((id: string) => (id.includes("arm64") ? Promise.reject(new Error("x")) : Promise.resolve())),
    });
    renderUi(<PublishReleaseDialog onClose={() => {}} />);
    const dialog = await screen.findByRole("dialog", { name: "Publicar una versión" });
    await user.type(within(dialog).getByLabelText("Versión"), "1.4.0");
    await user.upload(within(dialog).getByLabelText("Binarios del agente"), [
      new File(["x"], "its-agent-linux-amd64"),
      new File(["x"], "its-agent-linux-arm64"),
      new File(["x"], "its-agent-windows-amd64.exe"),
      new File([signatures], "SIGNATURES"),
    ]);
    await user.click(within(dialog).getByRole("button", { name: "Publicar" }));
    expect(await within(dialog).findByText(/Este no es el archivo para el que se escribió el archivo de firmas/)).toBeInTheDocument();
    expect(within(dialog).getByText(/Se creó la entrada de este archivo pero no tiene archivo, y no se pudo quitar\./)).toBeInTheDocument();
    expect(within(dialog).getByText(/ya existe una versión para este sistema y arquitectura/)).toBeInTheDocument();
    expect(within(dialog).getAllByText("Fallido").length).toBe(3);
    expectNoEnglish(dialog, ["SIGNATURES", "ya existe una versión para este sistema y arquitectura"]);
  });
});

describe("a live language switch on the agent packages page", () => {
  it("re-words a publish failure already on screen", async () => {
    const user = userEvent.setup();
    const sig = `${"L".repeat(86)}==`;
    const signatures = `${"1".repeat(64)}  its-agent-linux-amd64  linux/amd64  ${sig}\nbasura\n`;
    stubApi({
      createAgentRelease: vi.fn().mockImplementation((body: { version: string; os: string; arch: string }) =>
        Promise.resolve(release({ id: "id-1", version: body.version, os: body.os, arch: body.arch, sha256: "", has_artifact: false })),
      ),
      uploadAgentReleaseArtifact: vi.fn().mockImplementation(() => Promise.resolve(release({ id: "id-1", sha256: "9".repeat(64) }))),
    });
    renderUi(<PublishReleaseDialog onClose={() => {}} />);
    const dialog = await screen.findByRole("dialog", { name: "Publicar una versión" });
    await user.type(within(dialog).getByLabelText("Versión"), "1.4.0");
    await user.upload(within(dialog).getByLabelText("Binarios del agente"), [new File(["x"], "its-agent-linux-amd64"), new File([signatures], "SIGNATURES")]);
    expect(await within(dialog).findByText("La línea 2 del archivo de firmas no tiene el formato esperado.")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Publicar" }));
    expect(await within(dialog).findByText(/Este no es el archivo para el que se escribió el archivo de firmas/)).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    const english = screen.getByRole("dialog", { name: "Publish a release" });
    expect(within(english).getByText(/This is not the file the signatures file was written for/)).toBeInTheDocument();
    expect(within(english).getByText("Line 2 of the signatures file is not in the expected form.")).toBeInTheDocument();
    expect(within(english).getByText("Failed")).toBeInTheDocument();
  });

  it("re-words the catalogue, a rollout's states and the install link status already on screen", async () => {
    const user = userEvent.setup();
    stubApi();
    renderPage();
    await user.click((await screen.findAllByRole("button", { name: "Detalles" }))[0]!);
    expect(await screen.findByText("Despliegue de 1.8.2")).toBeInTheDocument();
    await screen.findByRole("table", { name: "Dispositivos de este despliegue" });
    expect(await screen.findByText(/Activo desde/)).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("heading", { name: "Agent packages" })).toBeInTheDocument();
    expect(screen.getByText("Rollout of 1.8.2")).toBeInTheDocument();
    expect(screen.getByText(/On since/)).toBeInTheDocument();
    expect(screen.getByText("3 agents · latest stable 1.8.2 · 2 can update")).toBeInTheDocument();
    // Target states are catalogue lookups, not stored text.
    expect(screen.getAllByText("Rolled back").length).toBeGreaterThan(0);
    expect(screen.getByText("1.8.1 (rollback)")).toBeInTheDocument();
    // A folded version's button word follows too.
    expect(screen.getByRole("button", { name: /Show older versions/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Show older versions/ }));
    expect(screen.getByRole("button", { name: "Hide older versions" })).toBeInTheDocument();
  });
});
