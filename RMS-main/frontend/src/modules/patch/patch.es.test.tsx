/**
 * Patch management in Spanish: the fleet page on each platform tab (tiles,
 * update and device lists, the approvals, restarts and compliance records, the
 * decision bars and their confirmations, the empty-tab explanations), the
 * policies page with its dialog, a device's own Patch tab, and the sentences
 * the module builds outside React. Update titles, identifiers and hostnames
 * read as recorded; the words around them are Spanish.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage, translate } from "../../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import { ApiError } from "../../lib/api";
import type { ApiClient, DevicePage, Me } from "../../lib/api";
import { DevicePatchTab } from "./DevicePatchTab";
import { FleetPatchPage } from "./FleetPatchPage";
import { PatchPolicyEditor } from "./PatchPolicyEditor";
import { appSourceSentences } from "./AppsPanel";
import { describeCron, cronProblem } from "./cron";
import {
  applyOutcomeMessage,
  bulkApprovedMessage,
  deviceLensWarning,
  hiddenBySeverityMessage,
  hostLensWarning,
  hostSelectionSummary,
  machinesNeedingUpdates,
  pageRangeMessage,
  ringReachMessage,
  scanQueuedMessage,
  selectAllHostsLabel,
  selectAllMatchingLabel,
  selectionCeilingNotice,
  selectionSummary,
} from "./patchCopy";
import { patchStateLabel, severityLabel } from "./patchState";
import { SEVERITY_PROVENANCE, familyMachineCount } from "./patchTabs";
import { describeMinutes, restartConfirmBody, restartLabel, restartSentence, restartWhen } from "./restartCopy";
import type {
  FleetReport,
  PatchApproval,
  PatchDeviceRollup,
  PatchPolicy,
  PatchRun,
  PatchUpdate,
  PatchUpdateFilters,
  PatchUpdateListResponse,
} from "./types";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({
  useApi: () => {
    const merged: Partial<ApiClient> = { ...mockDefaults(), ...mockUseApi() };
    const list = merged.listPatchUpdates;
    return {
      ...merged,
      // App updates have their own list; these tests are about the OS one.
      listPatchUpdates: (params?: PatchUpdateFilters) =>
        params?.kind === "apps" ? Promise.resolve(updatePage([])) : list?.(params) ?? Promise.resolve(updatePage([])),
    };
  },
}));

const SCANNED = { last_scan_at: "2026-01-05T09:00:00.000Z", devices_scanned: 1, devices_total: 1 };

function updatePage(items: PatchUpdate[], overrides: Partial<PatchUpdateListResponse> = {}): PatchUpdateListResponse {
  const severityCounts: Record<string, number> = {};
  for (const row of items) severityCounts[row.severity] = (severityCounts[row.severity] ?? 0) + 1;
  return { items, total: items.length, total_unfiltered: items.length, severity_counts: severityCounts, ...SCANNED, ...overrides };
}

function update(overrides: Partial<PatchUpdate> = {}): PatchUpdate {
  return {
    id: "p1",
    os_family: "windows",
    source: "wsus",
    identifier: "KB5031354",
    title: "Acumulativa de octubre",
    severity: "critical",
    kb: "KB5031354",
    cve: null,
    priority: 1,
    first_seen_at: "2026-01-01T00:00:00.000Z",
    devices_missing: 41,
    devices_approved: 3,
    devices_failed: 2,
    devices_installed: 0,
    devices_outstanding: 41,
    device_state: null,
    device_last_error: null,
    device_applied_at: null,
    cleared_rings: [],
    ...overrides,
  };
}

const UPDATES = [update(), update({ id: "p2", identifier: "KB5031355", title: "Definiciones de seguridad", severity: "moderate", devices_outstanding: 1, devices_approved: 0, devices_failed: 0, cleared_rings: ["test"] })];

const ROLLUP: PatchDeviceRollup = {
  device_id: "dev-1",
  hostname: "sentinel",
  os_family: "windows",
  outstanding: 41,
  failed: 4,
  worst_severity: "critical",
  counts_by_severity: { critical: 1, important: 40 },
  last_scan_at: "2026-01-05T09:00:00.000Z",
};

const RINGS = {
  items: [
    { ring_name: "test", ring_order: 0, device_count: 3 },
    { ring_name: "pilot", ring_order: 50, device_count: 1 },
    { ring_name: "broad", ring_order: 100, device_count: 220 },
  ],
  total: 3,
  device_total: 240,
};

const APPROVAL: PatchApproval = {
  id: "a1",
  patch_id: "p1",
  patch_identifier: "KB5031354",
  ring_name: "pilot",
  approved_by: null,
  approved_at: "2026-01-02T00:00:00.000Z",
  revoked: false,
  revoked_by: null,
  revoked_at: null,
};
const REVOKED: PatchApproval = { ...APPROVAL, id: "a2", ring_name: "broad", approved_by: "u9", revoked: true, revoked_at: "2026-01-03T00:00:00.000Z" };

const REPORT: FleetReport = {
  compliant_pct_by_os_family: { windows: 82.5, linux: 40, macos: 95 },
  compliant_pct_by_severity: { critical: 91, low: 55 },
  exposures: [],
  exposed_device_count: 0,
  critical_device_count: 0,
  ...SCANNED,
};

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "operator@example.com",
  display_name: "Operator",
  permissions: ["patch.read", "patch.approve", "patch.apply", "patch.scan", "patch.policy.manage", "devices.read"],
  timezone: "Europe/Madrid",
} as Me;

function devicePage(managed: number): DevicePage {
  return {
    items: [],
    total: managed,
    state_counts: { all: managed, online: 0, offline: 0, assets: 0, certs: 0 },
    fleet: { total: managed, managed, online: 0, cert_attention: 0 },
  };
}

const mockDefaults = (): Partial<ApiClient> => ({
  me: vi.fn().mockResolvedValue(ME),
  listPatchApprovals: vi.fn().mockResolvedValue({ items: [APPROVAL, REVOKED], total: 2 }),
  listPatchUpdates: vi.fn().mockResolvedValue(updatePage(UPDATES)),
  listPatchDeviceRollup: vi.fn().mockResolvedValue({ items: [ROLLUP], total: 1, ...SCANNED }),
  listPatchRuns: vi.fn().mockResolvedValue({ items: [] }),
  getPatchFleetReport: vi.fn().mockResolvedValue(REPORT),
  listPatchRings: vi.fn().mockResolvedValue(RINGS),
  bulkApprovePatches: vi.fn().mockResolvedValue({ items: [], total: 0, newly_approved: 2, already_in_force: 1 }),
  triggerPatchScan: vi.fn().mockResolvedValue({ job_ids: ["j1", "j2"] }),
  applyPatches: vi.fn().mockResolvedValue({ job_ids: ["j-apply-1"] }),
  exportPatchReport: vi.fn().mockResolvedValue(new Blob(["identifier,state\n"])),
  listDevices: vi.fn().mockResolvedValue(devicePage(60)),
  listPatchRestarts: vi.fn().mockResolvedValue({
    items: [
      {
        id: "r1",
        device_id: "dev-1",
        state: "scheduled",
        origin: "updates",
        scheduled_for: "2026-01-06T02:00:00.000Z",
        user_present: true,
        reason: null,
        created_at: "2026-01-05T09:00:00.000Z",
        closed_at: null,
        hostname: "sentinel",
      },
    ],
    total: 1,
  }),
  listPatchAppSources: vi.fn().mockResolvedValue({ items: [] }),
  listChanges: vi.fn().mockResolvedValue([{ id: "c1", title: "Ventana de octubre", state: "approved" }]),
  getCurrentOrg: vi.fn().mockResolvedValue({ id: "o1", name: "Org", slug: "org", status: "active", settings: {} }),
});

function renderPage(ui: ReactElement, path = "/patch") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderInSpanish(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Text recorded by the system or typed by a person, as it reads in any language. */
const STORED = ["sentinel", "KB5031354", "KB5031355", "Acumulativa de octubre", "Definiciones de seguridad", "wsus", "apt", "winget", "Homebrew", "pilot", "broad", "test"];

beforeEach(() => mockUseApi.mockReset());

// ------------------------------------------------------------- the fleet page

describe("the fleet patch page in Spanish", () => {
  it("words the head, the platform strip, the tiles, both lists and the records below", async () => {
    mockUseApi.mockReturnValue({});
    const { container } = renderPage(<FleetPatchPage />);
    expect(await screen.findByRole("heading", { name: "Gestión de parches" })).toBeInTheDocument();
    await screen.findByText("Acumulativa de octubre");
    await screen.findAllByText("sentinel");
    expect(screen.getByText("Actualizaciones de toda la flota, aprobadas en anillos de despliegue.")).toBeInTheDocument();
    for (const label of ["Políticas", "Exportar evidencias", "Escanear"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    expect(screen.getByRole("tab", { name: /Windows/ })).toBeInTheDocument();
    expect(screen.getByRole("tabpanel", { name: "Parches de Windows" })).toBeInTheDocument();
    const labels = [...document.querySelectorAll(".stat-label")].map((el) => el.textContent);
    expect(labels).toEqual(["Actualizaciones pendientes", "Críticas", "Importantes", "Equipos que necesitan actualizaciones"]);
    // The updates list.
    expect(screen.getByText("Actualizaciones pendientes de Windows")).toBeInTheDocument();
    const updates = screen.getByText("Acumulativa de octubre").closest("tr")!;
    expect(within(updates).getByText("41 dispositivos")).toBeInTheDocument();
    expect(within(updates).getByText("3 esperando una ventana, 2 fallidas")).toBeInTheDocument();
    expect(within(updates).getByText("Crítica")).toBeInTheDocument();
    expect(screen.getByText("No autorizada para ningún anillo")).toBeInTheDocument();
    expect(screen.getByLabelText("Buscar actualizaciones de Windows")).toBeInTheDocument();
    expect(screen.getByLabelText("Filtrar actualizaciones de Windows por gravedad")).toBeInTheDocument();
    expect(screen.getAllByRole("option", { name: "Solo críticas e importantes" }).length).toBeGreaterThan(0);
    expect(screen.getByText(/Mostrando 1–2 de 2 actualizaciones\./)).toBeInTheDocument();
    expect(screen.getByText(/Aquí la gravedad es la valoración MSRC de Microsoft/)).toBeInTheDocument();
    // The machines list.
    expect(screen.getByText("Equipos Windows que necesitan actualizaciones")).toBeInTheDocument();
    expect(screen.getByText(/1 de 60 equipos Windows con agente necesita al menos una actualización\./)).toBeInTheDocument();
    const machine = screen.getAllByText("sentinel")[0]!.closest("tr")!;
    expect(within(machine).getByText("4 fallidas")).toBeInTheDocument();
    expect(within(machine).getByRole("button", { name: "Mostrar las 41 actualizaciones pendientes de sentinel" })).toBeInTheDocument();
    // The records below the tabs.
    expect(screen.getByText("Aprobaciones")).toBeInTheDocument();
    expect(screen.getByText("Aprobación automática por política")).toBeInTheDocument();
    expect(screen.getByText("En vigor")).toBeInTheDocument();
    expect(screen.getByText("Revocada")).toBeInTheDocument();
    expect(screen.getByText("Reinicios")).toBeInTheDocument();
    expect(screen.getByText("Reinicio programado")).toBeInTheDocument();
    expect(screen.getByText(/se reinicia el /)).toBeInTheDocument();
    expect(screen.getByText("Cumplimiento")).toBeInTheDocument();
    expect(screen.getByText(/El color de la barra es la franja de cumplimiento/)).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: /Windows: cumplimiento, por plataforma, / })).toBeInTheDocument();
    expect(screen.getByText("Antes de poder aprobar actualizaciones")).toBeInTheDocument();
    expectNoEnglish(container, [...STORED, "KB5031354"]);
  });

  it("words the Apps list on a platform with an app manager", async () => {
    mockUseApi.mockReturnValue({
      listPatchAppSources: vi.fn().mockResolvedValue({
        items: [
          { source: "winget", state: "scanned", devices: 12, detail: null },
          { source: "winget", state: "failed", devices: 1, detail: "sin red" },
          { source: "winget", state: "unavailable", devices: 3, detail: null },
        ],
      }),
    });
    const { container } = renderPage(<FleetPatchPage />);
    expect(await screen.findByText("Aplicaciones")).toBeInTheDocument();
    expect(await screen.findByText("winget comprobado en 12 equipos.")).toBeInTheDocument();
    expect(screen.getByText("No se pudo comprobar winget en 1 equipo: sin red.")).toBeInTheDocument();
    expect(screen.getByText("winget no está disponible en 3 equipos.")).toBeInTheDocument();
    expect(screen.getByLabelText("Buscar actualizaciones de aplicaciones")).toBeInTheDocument();
    expect(screen.getByText("winget no publica ninguna gravedad, así que toda actualización de aplicación es «desconocida».")).toBeInTheDocument();
    expectNoEnglish(container, [...STORED, "sin red"]);
  });

  it("words the decision bar, the ring chooser and the approval confirmation", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue({});
    renderPage(<FleetPatchPage />);
    await screen.findByText("Acumulativa de octubre");
    expect(screen.getByRole("button", { name: "Seleccionar las 2 actualizaciones — actualizaciones de Windows" })).toBeInTheDocument();
    expect(screen.getByText("El anillo test abarca 3 de 240 dispositivos.")).toBeInTheDocument();
    const ring = screen.getByLabelText("Anillo en el que aprobar — actualizaciones de Windows");
    expect(within(ring).getByRole("option", { name: "pilot, 1 dispositivo" })).toBeInTheDocument();
    expect(within(ring).getByRole("option", { name: "broad, 220 dispositivos" })).toBeInTheDocument();
    await user.click(screen.getByLabelText("Seleccionar KB5031354"));
    expect(screen.getByText("1 actualización seleccionada de 2 coincidentes.")).toBeInTheDocument();
    expect(screen.getByLabelText("Cambio que implementa esta aprobación — actualizaciones de Windows")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Sin solicitud de cambio" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Aprobar 1 actualización en el anillo test/ }));
    const dialog = await screen.findByRole("dialog", { name: "Aprobar actualizaciones en un anillo de despliegue" });
    expect(within(dialog).getByText("1 actualización → anillo test")).toBeInTheDocument();
    expect(dialog).toHaveTextContent("Esta actualización pasa a poder instalarse en los 3 dispositivos del anillo «test» en su próxima ventana de mantenimiento.");
    expect(dialog).toHaveTextContent("Si no eliges ninguna solicitud de cambio, se registra una automáticamente para esta aprobación.");
    await user.click(within(dialog).getByRole("button", { name: "Aprobar" }));
    expect(await screen.findByText("2 actualizaciones aprobadas para el anillo test. 1 ya estaba en vigor.")).toBeInTheDocument();
  });

  it("says what a scan and an export did, and that an export failed", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue({ exportPatchReport: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    renderPage(<FleetPatchPage />);
    await screen.findByText("Acumulativa de octubre");
    await user.click(screen.getByRole("button", { name: "Escanear" }));
    expect(await screen.findByText("Escaneo en cola: se enviaron 2 tareas.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Exportar evidencias" }));
    expect(await screen.findAllByRole("alert")).not.toHaveLength(0);
  });

  it("words the Linux tab: the machine list first, the host bar and its confirmation", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue({
      listPatchUpdates: vi.fn().mockResolvedValue(updatePage([update({ os_family: "linux", source: "apt", identifier: "openssl-3.0.11" })])),
      listPatchDeviceRollup: vi.fn().mockResolvedValue({ items: [{ ...ROLLUP, os_family: "linux", hostname: "ubuntu-01" }], total: 1, ...SCANNED }),
    });
    const { container } = renderPage(<FleetPatchPage />, "/patch?os=linux");
    expect(await screen.findByText("Equipos Linux")).toBeInTheDocument();
    await screen.findByText("ubuntu-01");
    expect(screen.getByText("Lo que les falta a estos equipos")).toBeInTheDocument();
    expect(screen.getByText(/En apt, «importante» significa que la versión viene del repositorio de seguridad/)).toBeInTheDocument();
    expect(screen.getByText(/Las actualizaciones aprobadas se instalan solas en la próxima ventana de mantenimiento de cada equipo\./)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Seleccionar 1 equipo con actualizaciones pendientes — Linux" })).toBeInTheDocument();
    await user.click(screen.getByLabelText("Seleccionar ubuntu-01"));
    expect(screen.getByText("1 equipo seleccionado de 1.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Instalar las actualizaciones aprobadas en ubuntu-01" }));
    const dialog = await screen.findByRole("dialog", { name: "Instalar ahora las actualizaciones aprobadas" });
    expect(dialog).toHaveTextContent("ubuntu-01 está a punto de instalar.");
    expect(dialog).toHaveTextContent("Cada uno recibe las actualizaciones ya aprobadas para su anillo, y nada más.");
    await user.click(within(dialog).getByRole("button", { name: "Instalar" }));
    expect(await screen.findByText("Instalación en cola en 1 equipo.")).toBeInTheDocument();
    expectNoEnglish(container, [...STORED, "ubuntu-01", "openssl-3.0.11"]);
  });

  it("words the macOS caveats", async () => {
    mockUseApi.mockReturnValue({});
    const { container } = renderPage(<FleetPatchPage />, "/patch?os=macos");
    expect(await screen.findByText("Tres cosas que esta plataforma no puede hacer en macOS.")).toBeInTheDocument();
    expect(screen.getByText("Sin actualizaciones de versión mayor, a propósito.").tagName).toBe("STRONG");
    expect(screen.getByText(/Las actualizaciones del sistema en Apple Silicon necesitan las credenciales del propietario del volumen/)).toBeInTheDocument();
    expectNoEnglish(container, [...STORED, "Apple Silicon"]);
  });

  it("explains each reason a platform tab is empty", async () => {
    const empty = {
      listPatchUpdates: vi.fn().mockResolvedValue(updatePage([], { devices_scanned: 0 })),
      listPatchDeviceRollup: vi.fn().mockResolvedValue({ items: [], total: 0, ...SCANNED, devices_scanned: 0 }),
    };
    // Nothing enrolled.
    mockUseApi.mockReturnValue({ ...empty, listDevices: vi.fn().mockResolvedValue(devicePage(0)) });
    const first = renderPage(<FleetPatchPage />);
    expect(await screen.findByText("Ningún equipo Windows está inscrito")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Añadir dispositivos" })).toBeInTheDocument();
    expectNoEnglish(first.container, STORED);
    first.unmount();

    // A fleet nobody has scanned, and the operator may scan.
    mockUseApi.mockReturnValue({ ...empty, listDevices: vi.fn().mockResolvedValue(devicePage(1)) });
    const second = renderPage(<FleetPatchPage />);
    expect(await screen.findByText("Todavía no se ha completado ningún escaneo de parches")).toBeInTheDocument();
    expect(await screen.findByText(/1 equipo Windows tiene un agente y no ha terminado ningún escaneo de parches/)).toBeInTheDocument();
    expect(screen.getByText(/Escanear, en la parte superior de esta página, pone uno en cola\./)).toBeInTheDocument();
    expectNoEnglish(second.container, STORED);
    second.unmount();

    // A scanned fleet with nothing outstanding.
    mockUseApi.mockReturnValue({
      listPatchUpdates: vi.fn().mockResolvedValue(updatePage([])),
      listPatchDeviceRollup: vi.fn().mockResolvedValue({ items: [], total: 0, ...SCANNED }),
      listDevices: vi.fn().mockResolvedValue(devicePage(60)),
    });
    const third = renderPage(<FleetPatchPage />);
    expect(await screen.findByText("Todos los equipos Windows están al día")).toBeInTheDocument();
    expect(screen.getByText(/Los 60 equipos Windows con agente informaron en su último escaneo sin nada pendiente\./)).toBeInTheDocument();
    expectNoEnglish(third.container, STORED);
    third.unmount();

    // The console cannot count the fleet.
    mockUseApi.mockReturnValue({
      me: vi.fn().mockResolvedValue({ ...ME, permissions: ["patch.read"] }),
      listPatchUpdates: vi.fn().mockResolvedValue(updatePage([])),
      listPatchDeviceRollup: vi.fn().mockResolvedValue({ items: [], total: 0, ...SCANNED }),
    });
    const fourth = renderPage(<FleetPatchPage />);
    expect(await screen.findByText("No hay nada pendiente en ningún equipo Windows que haya informado")).toBeInTheDocument();
    expectNoEnglish(fourth.container, STORED);
  });

  it("opens a machine's own updates and words the drill-down", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue({});
    renderPage(<FleetPatchPage />);
    await screen.findAllByText("sentinel");
    await user.click(screen.getByRole("button", { name: "Mostrar las 41 actualizaciones pendientes de sentinel" }));
    expect(await screen.findByText(/sentinel: Mostrando 1–2 de 2 actualizaciones\./)).toBeInTheDocument();
    expect(screen.getByLabelText("Seleccionar todas las actualizaciones mostradas de sentinel")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Abrir sentinel" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ocultar las actualizaciones de sentinel" })).toBeInTheDocument();
  });

  it("words the revoke confirmation", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue({ revokePatchApproval: vi.fn().mockResolvedValue(undefined) });
    renderPage(<FleetPatchPage />);
    await screen.findByText("Aprobación automática por política");
    await user.click(screen.getByRole("button", { name: /KB5031354 → anillo pilot/ }));
    await user.click(await screen.findByRole("menuitem", { name: "Revocar" }));
    const dialog = await screen.findByRole("dialog", { name: "Revocar la aprobación del parche" });
    expect(dialog).toHaveTextContent("Los dispositivos del anillo «pilot» dejarán de instalar esta actualización");
    expect(within(dialog).getByRole("button", { name: "Revocar aprobación" })).toBeInTheDocument();
  });
});

// ----------------------------------------------------------- the policies page

const POLICY: PatchPolicy = {
  id: "pol1",
  device_group_id: null,
  ring_name: "broad",
  ring_order: 100,
  maintenance_window: "0 2-5 * * 6",
  auto_approve_security: true,
  reboot: "if_required",
  reboot_deferral_hours: 24,
  scan_interval_hours: 24,
};

describe("the patch policies page in Spanish", () => {
  it("words the list, with the window read back in words", async () => {
    mockUseApi.mockReturnValue({
      listPatchPolicies: vi.fn().mockResolvedValue([POLICY, { ...POLICY, id: "pol2", ring_name: "pilot", maintenance_window: null, auto_approve_security: false, reboot: "forced" }]),
    });
    const { container } = renderPage(<PatchPolicyEditor />, "/patch/policies");
    expect(await screen.findByRole("heading", { name: "Políticas de parches" })).toBeInTheDocument();
    await screen.findByText("sábado, de 02:00 a 06:00".replace(/^s/, "S"));
    expect(screen.getByText("siempre abierta")).toBeInTheDocument();
    expect(screen.getByText("Reiniciar cuando las actualizaciones lo necesiten")).toBeInTheDocument();
    expect(screen.getByText("Reiniciar siempre tras las actualizaciones")).toBeInTheDocument();
    for (const header of ["Anillo", "Orden", "Ventana (UTC)", "Aprobar automáticamente las de seguridad", "Reinicio", "Intervalo de escaneo (h)"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(screen.getByRole("button", { name: "Editar la política broad" })).toBeInTheDocument();
    expect(screen.getAllByText("Valor predeterminado de la organización").length).toBeGreaterThan(0);
    expectNoEnglish(container, ["broad", "pilot", "0 2-5 * * 6"]);
  });

  it("words the dialog, a refusal, and the overwrite confirmation", async () => {
    const user = userEvent.setup();
    const upsert = vi.fn().mockResolvedValue(POLICY);
    mockUseApi.mockReturnValue({
      listPatchPolicies: vi.fn().mockResolvedValue([POLICY]),
      upsertPatchPolicy: upsert,
      listDeviceGroups: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    });
    const { baseElement } = renderPage(<PatchPolicyEditor />, "/patch/policies");
    await user.click(await screen.findByRole("button", { name: "Nueva política" }));
    const dialog = await screen.findByRole("dialog", { name: "Nueva política de parches" });
    for (const label of ["Se aplica a", "Nombre del anillo", "Orden del anillo", "Reinicio", "Avisar a un usuario con sesión iniciada durante (horas)", "Intervalo de escaneo (horas)"]) {
      expect(within(dialog).getByLabelText(label)).toBeInTheDocument();
    }
    expect(within(dialog).getByText("Ventana de mantenimiento (UTC, opcional)")).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: "Todos los dispositivos (valor predeterminado de la organización)" })).toBeInTheDocument();
    expect(within(dialog).getByText(/Los anillos por grupo necesitan un grupo de dispositivos/)).toBeInTheDocument();
    await user.clear(within(dialog).getByLabelText("Nombre del anillo"));
    await user.clear(within(dialog).getByLabelText("Intervalo de escaneo (horas)"));
    await user.type(within(dialog).getByLabelText("Intervalo de escaneo (horas)"), "0");
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("Nombre del anillo");
    expect(alert).toHaveTextContent("debe estar entre 1 y 8760");
    expect(upsert).not.toHaveBeenCalled();
    expectNoEnglish(baseElement, ["broad"]);

    await user.type(within(dialog).getByLabelText("Nombre del anillo"), "pilot");
    await user.clear(within(dialog).getByLabelText("Intervalo de escaneo (horas)"));
    await user.type(within(dialog).getByLabelText("Intervalo de escaneo (horas)"), "12");
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    const confirm = await screen.findByRole("dialog", { name: "Reemplazar la política de parches de toda la organización" });
    expect(confirm).toHaveTextContent("pasan al anillo «pilot» y su reinicio queda así: reiniciar cuando las actualizaciones lo necesiten.");
    expect(within(confirm).getByRole("button", { name: "Reemplazar política" })).toBeInTheDocument();
  });

  it("re-words a refused policy form already on screen", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue({
      listPatchPolicies: vi.fn().mockResolvedValue([]),
      upsertPatchPolicy: vi.fn(),
      listDeviceGroups: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    });
    renderPage(<PatchPolicyEditor />, "/patch/policies");
    await user.click(await screen.findByRole("button", { name: "Nueva política" }));
    const dialog = await screen.findByRole("dialog", { name: "Nueva política de parches" });
    await user.clear(within(dialog).getByLabelText("Nombre del anillo"));
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Nombre del anillo");
    act(() => setActiveLanguage("en"));
    expect(within(screen.getByRole("dialog", { name: "New patch policy" })).getByRole("alert")).toHaveTextContent("Ring name is required");
  });

  it("words a window the painter cannot draw, and a mistyped one", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue({
      listPatchPolicies: vi.fn().mockResolvedValue([{ ...POLICY, maintenance_window: "0 */4 * * 6" }]),
      upsertPatchPolicy: vi.fn(),
      listDeviceGroups: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    });
    renderPage(<PatchPolicyEditor />, "/patch/policies");
    expect(await screen.findByText("Expresión personalizada")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Editar la política broad" }));
    expect(await screen.findByRole("dialog", { name: "Editar política de parches" })).toBeInTheDocument();
    expect(screen.getByText("Se aplica a: Valor predeterminado de la organización.")).toBeInTheDocument();
  });
});

// ------------------------------------------------------------ a device's tab

describe("a device's Patch tab in Spanish", () => {
  const RUN: PatchRun = {
    id: "run-1",
    job_id: "job-1",
    device_id: "d1",
    installed: 2,
    failed: 1,
    reboot_required: true,
    started_at: "2026-09-01T09:00:00.000Z",
    finished_at: "2026-09-01T09:04:00.000Z",
    failures: [{ source: "wsus", identifier: "KB5041585", error: "0x8024402c" }],
  };

  it("words the restart, state and severity cards, the last apply and the outstanding list", async () => {
    mockUseApi.mockReturnValue({
      getDevicePatchStatus: vi.fn().mockResolvedValue({
        device_id: "d1",
        counts_by_state: { failed: 1, missing: 1, not_applicable: 2 },
        counts_by_severity: { critical: 1, unknown: 1 },
        pending_reboot: true,
        last_scan_at: "2026-09-01T08:00:00.000Z",
        app_sources: [{ source: "winget", state: "scanned", detail: null }],
      }),
      listPatchRuns: vi.fn().mockResolvedValue({ items: [RUN] }),
      listPatchUpdates: vi.fn().mockResolvedValue(
        updatePage([
          update({ device_state: "failed", device_last_error: "0x80070005" }),
          update({ id: "p2", identifier: "KB2", title: "Otra", device_state: "approved" }),
        ]),
      ),
    });
    const { container } = renderPage(<DevicePatchTab deviceId="d1" />, "/devices/d1");
    expect(await screen.findByText("Última instalación")).toBeInTheDocument();
    expect(screen.getByText("Terminó con fallos")).toBeInTheDocument();
    expect(screen.getByText("Hace falta un reinicio para que surtan efecto.")).toBeInTheDocument();
    expect(screen.getByText("Reinicio pendiente")).toBeInTheDocument();
    expect(screen.getByText(/Último escaneo /)).toBeInTheDocument();
    expect(screen.getByText("Por estado")).toBeInTheDocument();
    expect(screen.getByText("Por gravedad")).toBeInTheDocument();
    expect(screen.getByText("Faltante")).toBeInTheDocument();
    expect(screen.getByText("No aplica")).toBeInTheDocument();
    expect(await screen.findByText("Actualizaciones pendientes")).toBeInTheDocument();
    expect((await screen.findAllByText("Fallida")).length).toBeGreaterThan(0);
    expect(screen.getByText("Esperando la ventana")).toBeInTheDocument();
    expect(screen.getByText("Las aplicaciones de winget se comprobaron en el último escaneo.")).toBeInTheDocument();
    expectNoEnglish(container, [...STORED, "KB5041585", "KB2", "Otra", "0x8024402c", "0x80070005"]);
  });

  it("words a running apply and a device that was never scanned", async () => {
    mockUseApi.mockReturnValue({
      getDevicePatchStatus: vi.fn().mockResolvedValue({ device_id: "d1", counts_by_state: {}, counts_by_severity: {}, pending_reboot: false, last_scan_at: null }),
      listPatchRuns: vi.fn().mockResolvedValue({ items: [{ ...RUN, finished_at: null, failures: [] }] }),
      listPatchUpdates: vi.fn().mockResolvedValue(updatePage([], { devices_scanned: 0 })),
    });
    const { container } = renderPage(<DevicePatchTab deviceId="d1" />, "/devices/d1");
    expect(await screen.findByText("En ejecución")).toBeInTheDocument();
    expect(screen.getByText(/El agente aún no ha respondido\./)).toBeInTheDocument();
    expect(screen.getByText("Nunca escaneado")).toBeInTheDocument();
    expect(screen.getByText("Sin reinicio pendiente")).toBeInTheDocument();
    expect(screen.getByText("Este dispositivo nunca ha completado un escaneo de parches, así que no se sabe qué le falta.")).toBeInTheDocument();
    expectNoEnglish(container, STORED);
  });
});

// ----------------------------------------------- sentences built outside React

describe("the patch sentences built outside React, in Spanish", () => {
  beforeEach(() => setActiveLanguage("es"));

  it("agrees updates, devices and machines with their counts", () => {
    expect(selectionSummary(1, 40)).toBe("1 actualización seleccionada de 40 coincidentes.");
    expect(selectionSummary(12, 300)).toBe("12 actualizaciones seleccionadas de 300 coincidentes.");
    expect(pageRangeMessage(1, 0, 1)).toBe("Mostrando 1–1 de 1 actualización.");
    expect(pageRangeMessage(25, 25, 300)).toBe("Mostrando 26–50 de 300 actualizaciones.");
    expect(hiddenBySeverityMessage(1, 2)).toBe("1 actualización más queda fuera del filtro de gravedad.");
    expect(hiddenBySeverityMessage(1, 9)).toBe("8 actualizaciones más quedan fuera del filtro de gravedad.");
    expect(machinesNeedingUpdates(1, null, null)).toBe("1 equipo necesita al menos una actualización.");
    expect(machinesNeedingUpdates(4, null, null)).toBe("4 equipos necesitan al menos una actualización.");
    expect(machinesNeedingUpdates(3, 60, "Windows")).toBe("3 de 60 equipos Windows con agente necesitan al menos una actualización.");
    expect(machinesNeedingUpdates(1, 60, null)).toBe("1 de 60 equipos con agente necesita al menos una actualización.");
    expect(selectAllMatchingLabel(1, 500)).toBe("Seleccionar 1 actualización");
    expect(selectAllMatchingLabel(12, 500)).toBe("Seleccionar las 12 actualizaciones");
    expect(selectAllMatchingLabel(812, 500)).toBe("Seleccionar las primeras 500 de 812");
    expect(selectAllHostsLabel(1, 500)).toBe("Seleccionar 1 equipo");
    expect(selectAllHostsLabel(9, 500)).toBe("Seleccionar los 9 equipos");
    expect(hostSelectionSummary(1, 12)).toBe("1 equipo seleccionado de 12.");
    expect(hostSelectionSummary(3, 12)).toBe("3 equipos seleccionados de 12.");
    expect(selectionCeilingNotice(812, 500)).toContain("las 312 restantes");
    expect(scanQueuedMessage({ job_ids: ["a"] })).toBe("Escaneo en cola: se envió 1 tarea.");
    expect(scanQueuedMessage({ job_ids: [] })).toContain("ningún dispositivo era apto");
  });

  it("agrees the filtered-empty sentence with the number outstanding", () => {
    expect(translate("patch.updates.emptyFiltered", { count: 1 })).toBe("Ninguna actualización coincide con estos filtros. Hay 1 pendiente en la flota.");
    expect(translate("patch.updates.emptyFiltered", { count: 5 })).toBe("Ninguna actualización coincide con estos filtros. Hay 5 pendientes en la flota.");
    setActiveLanguage("en");
    expect(translate("patch.updates.emptyFiltered", { count: 1 })).toBe("No update matches these filters. 1 are outstanding across the fleet.");
  });

  it("agrees the approval and install outcomes with what the server did", () => {
    expect(bulkApprovedMessage({ newly_approved: 1, already_in_force: 0 }, "test")).toBe("1 actualización aprobada para el anillo test.");
    expect(bulkApprovedMessage({ newly_approved: 3, already_in_force: 1 }, "test")).toBe("3 actualizaciones aprobadas para el anillo test. 1 ya estaba en vigor.");
    expect(bulkApprovedMessage({ newly_approved: 0, already_in_force: 4 }, "test")).toBe("0 actualizaciones aprobadas para el anillo test. 4 ya estaban en vigor.");
    expect(applyOutcomeMessage([], 1)).toContain("El equipo seleccionado no tenía ninguna actualización aprobada");
    expect(applyOutcomeMessage([], 3)).toContain("Ninguno de los 3 equipos seleccionados");
    expect(applyOutcomeMessage(["j"], 1)).toBe("Instalación en cola en 1 equipo.");
    expect(applyOutcomeMessage(["j"], 2)).toBe("Instalación en cola en 1 equipo. Se omitió 1 equipo: o no tiene nada aprobado para su anillo, o su ventana de mantenimiento está cerrada ahora mismo.");
    expect(applyOutcomeMessage(["j"], 4)).toContain("Se omitieron 3 equipos: o no tienen nada aprobado para su anillo");
  });

  it("words the blast radius for one device, many and none", () => {
    expect(ringReachMessage("test", null, 240)).toBeNull();
    expect(ringReachMessage("test", 0, 240)).toBe("Ningún dispositivo pertenece todavía al anillo test, así que aprobar aquí no autoriza nada hoy.");
    expect(ringReachMessage("test", 1, null)).toBe("El anillo test abarca 1 dispositivo.");
    expect(ringReachMessage("test", 3, null)).toBe("El anillo test abarca 3 dispositivos.");
    expect(deviceLensWarning("srv", "test", 1)).toBe("Estás eligiendo estas actualizaciones desde srv, pero una aprobación abarca el único dispositivo del anillo test, no solo srv.");
    expect(deviceLensWarning("srv", "test", 12)).toContain("abarca los 12 dispositivos del anillo test");
    expect(deviceLensWarning("srv", "test", null)).toContain("abarca todos los dispositivos del anillo test");
    expect(hostLensWarning(["a", "b"], "test", 5)).toBe("Estás eligiendo estas actualizaciones desde a y b, pero una aprobación abarca los 5 dispositivos del anillo test, no solo esos equipos.");
    expect(hostLensWarning(["a"], "test", 5)).toContain("no solo a.");
    expect(hostLensWarning(["a", "b", "c"], "test", 5)).toContain("desde 3 equipos seleccionados");
  });

  it("words restarts, durations and the maintenance window", () => {
    expect(restartLabel("scheduled")).toEqual({ label: "Reinicio programado", kind: "info" });
    expect(restartLabel("pending").label).toBe("Esperando la ventana");
    expect(restartSentence({ state: "pending", origin: "operator", scheduled_for: null, user_present: null, reason: null, closed_at: null }, "UTC")).toBe(
      "Se pidió un reinicio. Se enviará en la próxima ventana de mantenimiento.",
    );
    expect(restartSentence({ state: "completed", scheduled_for: null, user_present: null, reason: null, closed_at: null }, "UTC")).toBe("Reiniciado.");
    expect(describeMinutes(1)).toBe("1 minuto");
    expect(describeMinutes(15)).toBe("15 minutos");
    expect(describeMinutes(120)).toBe("2 horas");
    expect(describeMinutes(1440)).toBe("1 día");
    expect(describeMinutes(2880)).toBe("2 días");
    expect(restartWhen(null, "UTC")).toBe("Se reinicia en su próxima ventana de mantenimiento.");
    expect(restartWhen({ open_now: true, opens_at: null, user_warning_minutes: 5 } as never, "UTC")).toContain("se envía en los próximos 15 minutos");
    expect(restartConfirmBody(null, "UTC")).toContain("Si el equipo está sin conexión, espera hasta que vuelva.");
    expect(restartConfirmBody({ open_now: false, opens_at: null, user_warning_minutes: 60 } as never, "UTC")).toContain("recibe un aviso de 1 hora antes");
  });

  it("words the maintenance-window checks and the window read back", () => {
    expect(describeCron("0 2-5 * * 6")).toBe("Sábado, de 02:00 a 06:00");
    expect(describeCron("0 22-23 * * 1-5")).toBe("De lunes a viernes, de 22:00 a 24:00");
    expect(describeCron("0 3 * * 0,6")).toBe("Sábado, domingo, de 03:00 a 04:00");
    expect(describeCron("0 2-3,22 * * 6")).toBe("Sábado, de 02:00 a 04:00 y de 22:00 a 23:00");
    expect(cronProblem("0 2 *")).toBe("La ventana debe ser una expresión cron de 5 campos: minuto hora día-del-mes mes día-de-la-semana, p. ej. 0 2 * * 6");
    expect(cronProblem("0 2 * * SAT")).toContain("El campo de día de la semana de la ventana solo admite números");
    expect(cronProblem("61 2 * * 6")).toBe("El campo de minuto de la ventana debe estar entre 0 y 59.");
    expect(cronProblem("0 25 * * 6")).toBe("El campo de hora de la ventana debe estar entre 0 y 23.");
    expect(cronProblem("0,,3 2 * * 6")).toBe("El campo de minuto de la ventana tiene un valor vacío en su lista.");
    expect(cronProblem("0 2 * * 6")).toBeNull();
  });

  it("words the app-manager sentences and the label tables", () => {
    expect(appSourceSentences("winget", [{ source: "winget", state: "scanned", devices: 1, detail: null }])).toEqual(["winget comprobado en 1 equipo."]);
    expect(patchStateLabel("not_applicable")).toBe("No aplica");
    expect(patchStateLabel("desconocido")).toBe("desconocido");
    expect(severityLabel("important")).toBe("Importante");
    expect(severityLabel("informational")).toBe("Informativa");
    expect(severityLabel("novedad")).toBe("Novedad");
    expect(familyMachineCount("linux", 1)).toBe("1 equipo Linux");
    expect(familyMachineCount("macos", 2)).toBe("2 equipos macOS");
  });
});

// ------------------------------------------------------------ live switching

describe("a live language switch on the patch pages", () => {
  it("re-words a mounted fleet page, its record tables and its decision bar", async () => {
    mockUseApi.mockReturnValue({});
    renderPage(<FleetPatchPage />);
    expect(await screen.findByRole("heading", { name: "Gestión de parches" })).toBeInTheDocument();
    await screen.findByText("Acumulativa de octubre");
    expect(screen.getByText(/Mostrando 1–2 de 2 actualizaciones\./)).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("heading", { name: "Patch management" })).toBeInTheDocument();
    expect(screen.getByText("Showing 1–2 of 2 updates.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Select all 2 updates — Windows updates" })).toBeInTheDocument();
    expect(screen.getByText("The test ring covers 3 of 240 devices.")).toBeInTheDocument();
    expect(screen.getByText("Policy auto-approval")).toBeInTheDocument();
    expect(screen.getByText("Restart booked")).toBeInTheDocument();
    expect(screen.getByText(/Severity here is Microsoft's MSRC rating/)).toBeInTheDocument();
  });

  it("re-words the label table that was read before the switch", () => {
    setActiveLanguage("es");
    const before = SEVERITY_PROVENANCE.macos;
    expect(before).toMatch(/^Apple no publica ninguna gravedad/);
    setActiveLanguage("en");
    expect(SEVERITY_PROVENANCE.macos).toMatch(/^Apple publishes no severity/);
    expect(Object.keys(SEVERITY_PROVENANCE)).toEqual(["windows", "linux", "macos"]);
  });

  it("re-words the scan notice already on screen", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue({});
    renderPage(<FleetPatchPage />);
    await screen.findByText("Acumulativa de octubre");
    await user.click(screen.getByRole("button", { name: "Escanear" }));
    expect(await screen.findByText("Escaneo en cola: se enviaron 2 tareas.")).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("Scan queued — 2 jobs dispatched.")).toBeInTheDocument();
  });

  it("re-words an export failure already on screen", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue({ exportPatchReport: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    renderPage(<FleetPatchPage />);
    await screen.findByText("Acumulativa de octubre");
    await user.click(screen.getByRole("button", { name: "Exportar evidencias" }));
    const before = (await screen.findAllByRole("alert")).map((el) => el.textContent);
    act(() => setActiveLanguage("en"));
    const after = screen.getAllByRole("alert").map((el) => el.textContent);
    expect(after).not.toEqual(before);
    expect(translate("patch.fleet.exportEvidence")).toBe("Export evidence");
  });

  it("re-words the empty-tab explanation and the approval outcome", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue({});
    renderPage(<FleetPatchPage />);
    await screen.findByText("Acumulativa de octubre");
    await user.click(screen.getByLabelText("Seleccionar KB5031354"));
    await user.click(screen.getByRole("button", { name: /Aprobar 1 actualización en el anillo test/ }));
    const dialog = await screen.findByRole("dialog", { name: "Aprobar actualizaciones en un anillo de despliegue" });
    await user.click(within(dialog).getByRole("button", { name: "Aprobar" }));
    expect(await screen.findByText("2 actualizaciones aprobadas para el anillo test. 1 ya estaba en vigor.")).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("2 updates approved for the test ring. 1 was already in force.")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });
});
