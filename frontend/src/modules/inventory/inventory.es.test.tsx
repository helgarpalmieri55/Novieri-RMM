/**
 * Inventory in Spanish: the device page (the header chips, the facts strip,
 * the incident actions and their confirmations, the Overview and every tab it
 * owns, and the agentless-asset notice) and the fleet software list. Hostnames,
 * software names, serial numbers and job types read as recorded; the words
 * around them are Spanish.
 *
 * The Health and Deploy tabs and the two dialogs the incident actions open
 * (Run a script, Connect remotely) belong to the health, deploy and remote
 * modules; they are opened here too, because this page is where they live. The
 * Backup and Imaging tabs belong to modules that are not translated yet and are
 * not opened. Where the Overview borrows a label that has not been translated
 * yet (a ticket's status), the fixture is chosen so the borrowed word is not
 * one this catalogue also owns.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import { ApiError } from "../../lib/api";
import type { ApiClient, Device, DeviceInventory, Me } from "../../lib/api";
import { DeviceDetailPage } from "./DeviceDetailPage";
import { alertLine, certificateLine, updatesLine } from "./deviceSummary";
import { factBadge } from "./factBadges";
import { presentAttributes } from "./hardware";
import { SoftwarePage } from "./SoftwarePage";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
vi.mock("../../lib/ws", () => ({ useLiveEvents: () => undefined }));

const EVERYTHING = [
  "health.read",
  "patch.read",
  "patch.apply",
  "backup.read",
  "backup.run",
  "deploy.read",
  "deploy.script.run",
  "jobs.read",
  "jobs.dispatch",
  "ticket.read",
  "ticket.write",
  "remote.control",
  "assets.manage",
];

const DEVICE = {
  id: "d1",
  hostname: "ws-alpha",
  os_family: "windows",
  agent_status: "online",
  last_seen_at: "2026-09-23T11:55:00Z",
  lifecycle: "in_service",
  source: "agent",
  provenance: "agent",
  cert_state: "expiring",
  cert_revoked: false,
  cert_not_after: "2026-09-26T12:00:00Z",
  primary_ip: "10.20.4.31",
};

const INVENTORY: DeviceInventory = {
  summary: { hostname: "ws-alpha", os_family: "windows", os_version: "11 23H2", cpu: "Intel i7", ram_bytes: 17179869184, serial: "SN-001" },
  hardware: [
    { type: "disk", identifier: "C:", attributes: { size_bytes: 549755813888, model: "NVMe" } },
    { type: "nic", identifier: "3c:a8:2a:15:6b:11", attributes: { name: "eno1", speed_mbps: 1000, addresses: [{ address: "10.20.4.31", family: "ipv4" }] } },
    { type: "nic", identifier: "aa:bb:cc:dd:ee:01", attributes: { name: "veth12ab", addresses: [{ address: "fe80::1", origin: "link_local" }] } },
    { type: "battery", identifier: "BAT0", attributes: {} },
  ],
  software: [
    { name: "Paquete de ejemplo", version: "140.0", publisher: "Editor SA", install_date: "2026-08-01" },
    { name: "Otra herramienta", version: "2.1", publisher: "Taller SL", install_date: "2026-08-02" },
  ],
  facts: { disk_encryption: true, firewall_enabled: false, domain_joined: false },
  snapshot_taken_at: "2026-09-23T09:00:00Z",
};

const alert = (id: string, severity: string, check_type: string, opened_at: string) => ({
  id,
  device_id: "d1",
  device_hostname: "ws-alpha",
  check_type,
  selector: "",
  severity,
  state: "active",
  opened_at,
});

function renderDevice(permissions: string[] = EVERYTHING, overrides: Partial<ApiClient> = {}, entry = "/devices/d1", device: unknown = DEVICE) {
  const api: Partial<ApiClient> = {
    me: vi.fn().mockResolvedValue({ user_id: "u1", permissions, timezone: "UTC" } as unknown as Me),
    getDevice: vi.fn().mockResolvedValue(device),
    getDeviceInventory: vi.fn().mockResolvedValue(INVENTORY),
    getDeviceHealth: vi.fn().mockResolvedValue({
      open_alerts: [alert("a1", "info", "cpu.busy_pct", "2026-09-23T10:00:00Z"), alert("a2", "info", "disk.free_pct", "2026-09-23T09:00:00Z")],
      checks: [],
    }),
    getDevicePatchStatus: vi.fn().mockResolvedValue({
      device_id: "d1",
      counts_by_state: { missing: 4, installed: 20 },
      counts_by_severity: {},
      pending_reboot: false,
      last_scan_at: "2026-09-23T06:00:00Z",
      restart: { id: "r1", device_id: "d1", state: "scheduled", origin: "updates", scheduled_for: "2026-09-24T02:00:00Z", user_present: false, reason: null, created_at: "2026-09-23T06:00:00Z", closed_at: null },
      restart_window: null,
    }),
    getDeviceBackup: vi.fn().mockResolvedValue({
      device_id: "d1",
      last_good_point: { backup_point_id: "p1", taken_at: "2026-09-21T12:00:00Z", size: 1, repository_id: "r1" },
      last_run_state: "succeeded",
      last_run_finished_at: "2026-09-21T12:00:00Z",
      verified_at: null,
      restore_tested_at: null,
      repository: { id: "r1", name: "NAS de Lisboa" },
    }),
    listJobs: vi.fn().mockResolvedValue({
      items: [
        { id: "j1", type: "inventory.collect", status: "succeeded", issued_at: "2026-09-23T09:00:00Z", device_id: "d1" },
        { id: "j2", type: "inventory.collect", status: "queued", issued_at: "2026-09-19T09:00:00Z", device_id: "d1" },
      ],
      total: 2,
    }),
    listTickets: vi.fn().mockResolvedValue({
      items: [{ id: "t1", number: "INC-7", subject: "Lento al iniciar", status: "new", device_id: "d1" }],
      total: 1,
    }),
    getDeviceChanges: vi.fn().mockResolvedValue({
      items: [
        { id: "c1", ts: "2026-09-23T09:00:00Z", change_type: "software_installed", subject: "Paquete de ejemplo", from_value: null, to_value: "140.0" },
        { id: "c2", ts: "2026-09-22T09:00:00Z", change_type: "software_removed", subject: "Otra herramienta", from_value: "2.1", to_value: null },
        { id: "c3", ts: "2026-09-21T09:00:00Z", change_type: "fact_changed", subject: "disk_encryption", from_value: "false", to_value: "true" },
      ],
      total: 3,
    }),
    listArticles: vi.fn().mockResolvedValue({ items: [{ id: "ar1", title: "Guía de arranque", category: "Guías" }], total: 1 }),
    listDocuments: vi.fn().mockResolvedValue({
      items: [
        { id: "do1", title: "Contrato", category: "Legal", current_version: 0 },
        { id: "do2", title: "Plano", category: "Legal", current_version: 3 },
      ],
      total: 2,
    }),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as ApiClient);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = renderInSpanish(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/devices/:deviceId" element={<DeviceDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, api };
}

/** Text recorded by the system or typed by a person, as it reads in any language. */
const STORED = ["ws-alpha", "SN-001", "Intel i7", "NVMe", "eno1", "veth12ab", "NAS de Lisboa", "Paquete de ejemplo", "Otra herramienta", "Windows", "BAT0", "10.20.4.31"];

beforeEach(() => {
  mockUseApi.mockReset();
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(new Date("2026-09-23T12:00:00Z"));
});
afterEach(() => {
  vi.useRealTimers();
});

const userAction = () => userEvent.setup({ advanceTimers: vi.advanceTimersByTime });

// ------------------------------------------------------- the device page

describe("the device page in Spanish", () => {
  it("words the header, the facts strip, the actions and the Overview", async () => {
    const { container } = renderDevice();
    expect(await screen.findByRole("heading", { name: "ws-alpha" })).toBeInTheDocument();
    const status = await screen.findByRole("group", { name: "Estado del dispositivo" });
    expect(await within(status).findByText("2 alertas abiertas")).toBeInTheDocument();
    expect(within(status).getByText("Certificado por caducar")).toBeInTheDocument();
    expect(await within(status).findByText("Faltan 4 actualizaciones")).toBeInTheDocument();
    expect(within(status).getByText("Inscrito por el agente")).toBeInTheDocument();
    expect(within(status).getByText("Cifrado")).toBeInTheDocument();
    expect(within(status).getByText("Cortafuegos desactivado")).toBeInTheDocument();
    expect(within(status).getByText("No unido al dominio")).toBeInTheDocument();
    expect((await within(status).findByText(/Copia hecha/)).textContent).toBe("Copia hecha hace 2 días");
    expect(document.querySelector(".sub")).toHaveTextContent("último contacto hace 5 minutos");
    for (const label of ["Conectar en remoto", "Ejecutar un script", "Instalar actualizaciones", "Reiniciar", "Hacer copia ahora", "Abrir ticket", "Ejecutar tarea"]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }
    expect(screen.getByRole("link", { name: "Todos los dispositivos" })).toBeInTheDocument();
    // The facts strip.
    expect(screen.getByText("Agente visto por última vez", { exact: false })).toBeInTheDocument();
    expect(screen.getByText(/Válido hasta/)).toBeInTheDocument();
    expect(screen.getByText("N.º de serie")).toBeInTheDocument();
    expect(screen.getByText("Último barrido")).toBeInTheDocument();
    // The tab strip.
    for (const tab of ["Resumen", "Salud", "Actualizaciones", "Copia de seguridad", "Software", "Despliegues", "Hardware", "Tareas", "Cambios", "Documentación"]) {
      expect(screen.getByRole("tab", { name: tab })).toBeInTheDocument();
    }
    // The Overview's panels.
    const panel = screen.getByRole("tabpanel", { name: "Resumen" });
    await within(panel).findByText("2 abiertas");
    expect(within(panel).getByText("Estado de salud")).toBeInTheDocument();
    expect(within(panel).getByText("Todas las comprobaciones")).toBeInTheDocument();
    // An alert's check reads as the health module names it (`enums.HealthCheckType`).
    expect(within(panel).getByText(/CPU ocupada/)).toBeInTheDocument();
    expect(within(panel).getByText(/Disco libre/)).toBeInTheDocument();
    expect(await within(panel).findByText("Revisar actualizaciones")).toBeInTheDocument();
    expect(within(panel).getByText(/Reinicio programado, en su ventana de mantenimiento/)).toBeInTheDocument();
    expect(within(panel).getByText("Última comprobación")).toBeInTheDocument();
    expect(await within(panel).findByText("Detalles de la copia de seguridad")).toBeInTheDocument();
    expect(within(panel).getByText("Última copia correcta")).toBeInTheDocument();
    expect(within(panel).getByText("Guardada en")).toBeInTheDocument();
    expect(within(panel).getByText("Tareas recientes")).toBeInTheDocument();
    expect(await within(panel).findByText("Tickets abiertos")).toBeInTheDocument();
    expect(within(panel).getByText("Todos los tickets")).toBeInTheDocument();
    expectNoEnglish(container, [...STORED, "INC-7", "Lento al iniciar", "Lisboa"]);
  });

  it("words what the Overview says when there is nothing to show", async () => {
    renderDevice(EVERYTHING, {
      getDeviceHealth: vi.fn().mockResolvedValue({ open_alerts: [], checks: [] }),
      getDevicePatchStatus: vi.fn().mockResolvedValue({ device_id: "d1", counts_by_state: {}, counts_by_severity: {}, pending_reboot: true, last_scan_at: null, restart: null }),
      getDeviceBackup: vi.fn().mockResolvedValue({ device_id: "d1", last_good_point: null, last_run_state: null, repository: null }),
      listJobs: vi.fn().mockResolvedValue({ items: [], total: 0 }),
      listTickets: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    });
    const panel = await screen.findByRole("tabpanel", { name: "Resumen" });
    expect(await within(panel).findByText("No hay alertas abiertas en este dispositivo.")).toBeInTheDocument();
    expect(await within(panel).findByText("Pendiente, en su ventana de mantenimiento")).toBeInTheDocument();
    expect(await within(panel).findByText("Nunca se ha copiado")).toBeInTheDocument();
    expect(within(panel).getByText("No se ha ejecutado ninguna copia")).toBeInTheDocument();
    expect(within(panel).getByText("Sin repositorio")).toBeInTheDocument();
    expect(await within(panel).findByText("No se ha enviado ninguna tarea a este dispositivo recientemente.")).toBeInTheDocument();
    expect(await within(panel).findByText("No hay tickets abiertos para este dispositivo.")).toBeInTheDocument();
  });

  it("words the Hardware tab: units, the virtual-interface toggle and the raw view", async () => {
    const user = userAction();
    const { container } = renderDevice(EVERYTHING, {}, "/devices/d1?tab=hardware");
    const table = await screen.findByRole("table");
    for (const header of ["Tipo", "Interfaz / identificador", "Atributos"]) {
      expect(within(table).getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(within(table).getByText("Almacenamiento")).toBeInTheDocument();
    expect(within(table).getByText("Interfaz de red")).toBeInTheDocument();
    expect(within(table).getByText("Batería")).toBeInTheDocument();
    expect(within(table).getByText("Tamaño")).toBeInTheDocument();
    expect(within(table).getByText("Velocidad del enlace")).toBeInTheDocument();
    expect(within(table).getAllByText("Ver datos sin procesar").length).toBeGreaterThan(0);
    expect(within(table).queryByText("veth12ab")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Mostrar interfaces virtuales (1)" }));
    expect(screen.getByRole("button", { name: "Ocultar interfaces virtuales (1)" })).toBeInTheDocument();
    // "Name" and "Addresses" are the agent's own attribute keys (`attributes.name`,
    // `addresses`) as the shared attributes list captions them; the keys are recorded
    // data, not this module's words.
    // "link" is a word of the recorded address origin `link_local`, which the attributes list
    // shows raw; the docs module's English "{count} link" is a catalogue fragment since 10b.
    expectNoEnglish(container, [...STORED, "Name", "Addresses", "size_bytes", "model", "address", "family", "addresses", "origin", "link_local", "ipv4", "fe80::1", "link"]);
  });

  it("words the Software tab, its filter and its empty answers", async () => {
    const user = userAction();
    const { container } = renderDevice(EVERYTHING, {}, "/devices/d1?tab=software");
    const table = await screen.findByRole("table");
    for (const header of ["Nombre", "Versión", "Editor", "Instalado"]) {
      expect(within(table).getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(screen.getByLabelText("Filtrar software")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Filtrar software…")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Filtrar software"), "zzzz");
    expect(await screen.findByText("Ningún software coincide con ese filtro.")).toBeInTheDocument();
    expectNoEnglish(container, [...STORED, "Editor SA", "Taller SL"]);
  });

  it("words the Changes tab with a label for each kind of change", async () => {
    const { container } = renderDevice(EVERYTHING, {}, "/devices/d1?tab=changes");
    const table = await screen.findByRole("table");
    for (const header of ["Cuándo", "Cambio", "Elemento", "Antes", "Después"]) {
      expect(within(table).getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(within(table).getByText("Software instalado")).toBeInTheDocument();
    expect(within(table).getByText("Software eliminado")).toBeInTheDocument();
    expect(within(table).getByText("Dato modificado")).toBeInTheDocument();
    expect(within(table).getAllByText("Sin valor anterior").length).toBeGreaterThan(0);
    expect(within(table).getAllByText("Sin valor nuevo").length).toBeGreaterThan(0);
    expectNoEnglish(container, [...STORED, "disk_encryption", "disk"]);
  });

  it("words the Docs tab, with a document that has no file yet", async () => {
    const { container } = renderDevice(EVERYTHING, {}, "/devices/d1?tab=docs");
    expect(await screen.findByText("Artículos")).toBeInTheDocument();
    expect(screen.getByText("Documentos")).toBeInTheDocument();
    expect(await screen.findByText(/aún sin archivo/)).toBeInTheDocument();
    expect(screen.getByText(/· v3/)).toBeInTheDocument();
    expectNoEnglish(container, [...STORED, "Guía de arranque", "Guías", "Contrato", "Plano", "Legal"]);
  });

  it("words the empty Docs tab", async () => {
    renderDevice(EVERYTHING, { listArticles: vi.fn().mockResolvedValue({ items: [], total: 0 }), listDocuments: vi.fn().mockResolvedValue({ items: [], total: 0 }) }, "/devices/d1?tab=docs");
    expect(await screen.findByText("No hay artículos vinculados a este dispositivo.")).toBeInTheDocument();
    expect(await screen.findByText("No hay documentos vinculados a este dispositivo.")).toBeInTheDocument();
  });

  it("words the Jobs tab and flags a job that has waited too long", async () => {
    const { container } = renderDevice(EVERYTHING, {}, "/devices/d1?tab=jobs");
    const table = await screen.findByRole("table");
    for (const header of ["Tipo", "Estado", "Emitida", "Acciones"]) {
      expect(within(table).getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(await within(table).findByText("Sigue esperando")).toBeInTheDocument();
    expect(within(table).getAllByRole("link", { name: /^Inspeccionar la tarea .* emitida / }).length).toBe(2);
    expect(within(table).getAllByText("Inspeccionar").length).toBe(2);
    expectNoEnglish(container, [...STORED, "inventory.collect"]);
  });

  it("words the Health tab: the headline, the four bands, the checks and the open alerts", async () => {
    const health = {
      device_id: "d1",
      hostname: "ws-alpha",
      os_family: "windows",
      has_profile: true,
      profile: { id: "p1", name: "Base de oficina", policy_version: 3 },
      assignment: { id: "as1", device_group_id: "g1" },
      resolution_level: "group",
      acknowledged_policy_version: 2,
      policy_drift: true,
      checks: [
        { check_id: "c1", check_type: "disk.free_pct", selector: "C:", comparator: "lt", threshold_display: "10", severity: "critical", for_ticks: 1, for_seconds: 300, evaluated_by: "agent", state: "breaching", last_value: "3", policy_version: 3, reported_at: "2026-09-23T11:50:00Z" },
        { check_id: "c2", check_type: "posture.firewall_enabled", selector: "", comparator: "is", threshold_display: "true", severity: "warning", for_ticks: 1, for_seconds: 300, evaluated_by: "agent", state: "unavailable", last_value: "", policy_version: 3, reported_at: null },
      ],
      open_alerts: [{ ...alert("a1", "critical", "disk.free_pct", "2026-09-23T10:00:00Z"), observed_value: "3", threshold_display: "lt 10" }],
      agent_last_seen_at: "2026-09-23T11:55:00Z",
      agent_online: true,
      offline_after_s: 900,
    };
    const { container } = renderDevice(EVERYTHING, { getDeviceHealth: vi.fn().mockResolvedValue(health) }, "/devices/d1?tab=health");
    expect(await screen.findByText("Salud de ws-alpha")).toBeInTheDocument();
    expect(screen.getAllByText("1 alerta abierta").length).toBeGreaterThan(0);
    expect(screen.getByText("agente informando")).toBeInTheDocument();
    expect(screen.getByText("una asignación de grupo (g1)")).toBeInTheDocument();
    expect(screen.getByText("el agente tiene v2")).toBeInTheDocument();
    expect(screen.getByText("0 dentro del umbral")).toBeInTheDocument();
    expect(screen.getByText("1 incumpliendo")).toBeInTheDocument();
    expect(screen.getByText("1 no disponible")).toBeInTheDocument();
    const table = screen.getByRole("table");
    for (const header of ["Comprobación", "Estado", "Último valor", "Umbral", "Informado"]) {
      expect(within(table).getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(within(table).getByText("Incumpliendo")).toBeInTheDocument();
    expect(within(table).getByText("No disponible")).toBeInTheDocument();
    expect(within(table).getByText("por debajo de 10")).toBeInTheDocument();
    expect(within(table).getByText("se esperaba true")).toBeInTheDocument();
    expect(within(table).getByText("nunca")).toBeInTheDocument();
    expect(screen.getByText("Alertas abiertas")).toBeInTheDocument();
    expect(screen.getByText("Crítica")).toBeInTheDocument();
    // "enabled" is the wire word inside the recorded check type "posture.firewall_enabled".
    expectNoEnglish(container, [...STORED, "disk.free_pct", "posture.firewall_enabled", "enabled", "C:", "true", "p1", "g1"]);
  });

  it("words the Deploy tab", async () => {
    const history = [
      { deployment_id: "dep-a", package_id: "pkg-1", action: "install", state: "installed", exit_code: 0, log_ref: null, finished_at: "2026-09-23T09:00:00Z", created_at: "2026-09-23T08:00:00Z" },
      { deployment_id: "dep-b", package_id: "pkg-1", action: "uninstall", state: "failed", exit_code: null, log_ref: null, finished_at: null, created_at: "2026-09-22T08:00:00Z" },
    ];
    const { container } = renderDevice(EVERYTHING, { getDeviceDeployments: vi.fn().mockResolvedValue(history) }, "/devices/d1?tab=deploy");
    const table = await screen.findByRole("table");
    for (const header of ["Despliegue", "Paquete", "Acción", "Estado", "Código de salida", "Finalizado"]) {
      expect(within(table).getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(within(table).getByText("Instalado")).toBeInTheDocument();
    expect(within(table).getByText("Fallido")).toBeInTheDocument();
    expect(within(table).getByText("Sin código de salida registrado")).toBeInTheDocument();
    expectNoEnglish(container, [...STORED, "dep-a", "dep-b", "pkg-1"]);
  });

  it("words the Imaging tab the provisioning module supplies", async () => {
    const { container } = renderDevice(
      [...EVERYTHING, "provisioning.read"],
      {
        getDeviceProvisioning: vi.fn().mockResolvedValue({
          device_id: "d1",
          profile_id: "pr1",
          image_id: "im1",
          enrolled_at: "2026-09-22T10:00:00Z",
          latest_run_state: "awaiting_enrollment",
        }),
        listProvisionProfiles: vi.fn().mockResolvedValue([
          { id: "pr1", name: "Estación estándar", image_id: "im1", post_config: {}, target_scope: {}, enabled: true, created_at: "2026-09-01T00:00:00Z", has_domain_join_credential: false, has_local_admin_credential: false },
        ]),
        listOsImages: vi.fn().mockResolvedValue([
          { id: "im1", os_family: "windows", name: "Imagen base de oficina", kind: "install", artifact_ref: null, artifact_sha256: null, params: {}, state: "active", created_at: "2026-09-01T00:00:00Z" },
        ]),
      } as unknown as Partial<ApiClient>,
      "/devices/d1?tab=provisioning",
    );
    expect(await screen.findByText("Estado del aprovisionamiento")).toBeInTheDocument();
    expect(await screen.findByText("Estación estándar")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Imágenes de disco" })).toBeInTheDocument();
    expect(screen.getByText("Estado de la última ejecución")).toBeInTheDocument();
    expect(screen.getByText("Esperando inscripción")).toBeInTheDocument();
    expect(screen.getByText("Imagen base de oficina")).toBeInTheDocument();
    expectNoEnglish(container, STORED);
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("Provisioning status")).toBeInTheDocument();
    expect(screen.getByText("Awaiting enrollment")).toBeInTheDocument();
  });

  it("words the Backup tab the backup module supplies", async () => {
    const { container } = renderDevice(EVERYTHING, {}, "/devices/d1?tab=backup");
    expect(await screen.findByText("Estado de la copia de seguridad")).toBeInTheDocument();
    for (const label of ["Estado de la última ejecución", "Última ejecución finalizada", "Repositorio", "Último punto correcto", "Verificado", "Restauración probada"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    expect(screen.getByRole("button", { name: "Ejecutar ahora" })).toBeInTheDocument();
    // The fixture's run state is a word the backup module has no label for ("succeeded"),
    // so it reads through the shared status vocabulary, in Spanish.
    expect(await screen.findByText("Correcto")).toBeInTheDocument();
    expect(screen.getByText("no verificado")).toBeInTheDocument();
    expect(screen.getByText("no probada")).toBeInTheDocument();
    expectNoEnglish(container, STORED);
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("Backup status")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run now" })).toBeInTheDocument();
  });

  it("words the Run a script dialog the device actions open", async () => {
    const user = userAction();
    renderDevice(EVERYTHING, {
      listScripts: vi.fn().mockResolvedValue([
        { id: "s1", name: "limpiar-temporales", os_family: "windows", interpreter: "powershell", version: 1, created_by: null, created_at: "2026-09-01T00:00:00Z" },
      ]),
    });
    await user.click(await screen.findByRole("button", { name: "Ejecutar un script" }));
    const dialog = await screen.findByRole("dialog", { name: "Ejecutar un script en ws-alpha" });
    await user.click(within(dialog).getByRole("button", { name: "Ejecutar script" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("El campo Script es obligatorio");
    expectNoEnglish(dialog, STORED);
  });

  it("words the Connect remotely dialog the device actions open", async () => {
    const user = userAction();
    renderDevice(EVERYTHING, { listDevices: vi.fn().mockResolvedValue({ items: [DEVICE], total: 1 }) });
    await user.click(await screen.findByRole("button", { name: "Conectar en remoto" }));
    const dialog = await screen.findByRole("dialog", { name: "Iniciar sesión remota" });
    await user.click(within(dialog).getByRole("button", { name: "Iniciar sesión remota" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("El campo Motivo es obligatorio");
    expect(within(dialog).getByRole("option", { name: "Asistida (primero se pregunta a la persona que está en el dispositivo)" })).toBeInTheDocument();
    expectNoEnglish(dialog, STORED);
  });

  it("words the notice for an asset with no agent, and its push-install details", async () => {
    const user = userAction();
    const asset: Partial<Device> = { id: "d1", hostname: "core-sw-01", agent_status: null, lifecycle: "in_service", source: "manual", provenance: "operator", fqdn: null, os_family: null, architecture: "" };
    const { container } = renderDevice(EVERYTHING, { getDeviceInventory: vi.fn().mockResolvedValue({ summary: {}, hardware: [], software: [], facts: {}, snapshot_taken_at: null }) }, "/devices/d1", asset);
    expect(await screen.findByText("Este activo no tiene agente")).toBeInTheDocument();
    expect(screen.getByText("Las actualizaciones, los despliegues, la copia de seguridad y la salud necesitan un agente.")).toBeInTheDocument();
    expect(screen.getByText("Las pestañas de abajo se rellenan cuando un agente ha informado.")).toBeInTheDocument();
    expect(screen.getByText("Nombre DNS: sin registrar · Sistema operativo: sin registrar · Procesador: sin registrar")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Paquetes del agente" })).toBeInTheDocument();
    expect(screen.getByText("Sin agente inscrito")).toBeInTheDocument();
    expect(screen.getByText("Introducido a mano")).toBeInTheDocument();
    expect(screen.getByText("Sin certificado: este activo no tiene agente")).toBeInTheDocument();
    expect(screen.getByText("El agente no informó del número de serie de este equipo.")).toBeInTheDocument();
    expectNoEnglish(container, ["core-sw-01"]);
    await user.click(screen.getByRole("button", { name: "Datos de la instalación remota" }));
    expect(await screen.findByRole("dialog", { name: "Datos de la instalación remota" })).toBeInTheDocument();
  });

  it("words the asset whose agent can never run", async () => {
    renderDevice(EVERYTHING, {}, "/devices/d1", { id: "d1", hostname: "core-sw-01", agent_status: null, lifecycle: "unmanageable", source: "manual", provenance: "sweep" });
    expect(await screen.findByText("Marcado como incapaz de ejecutar un agente.")).toBeInTheDocument();
    expect(screen.getByText("Nada de lo que hay debajo se rellenará nunca para este registro.")).toBeInTheDocument();
    expect(screen.getByText("Encontrado por un barrido")).toBeInTheDocument();
  });

  it("words the install-updates and restart confirmations and what they did", async () => {
    const user = userAction();
    const applyPatches = vi.fn().mockResolvedValue({ job_ids: ["j-apply"] });
    const requestDeviceRestart = vi.fn().mockResolvedValue({ already_requested: true, window: null });
    renderDevice(EVERYTHING, { applyPatches, requestDeviceRestart, listPatchRings: vi.fn().mockResolvedValue({ items: [], total: 0, device_total: 0 }) });
    await user.click(await screen.findByRole("button", { name: "Instalar actualizaciones" }));
    const dialog = await screen.findByRole("dialog", { name: "Instalar las actualizaciones aprobadas" });
    expect(dialog).toHaveTextContent("Instala las actualizaciones ya aprobadas para el anillo de este equipo");
    await user.click(within(dialog).getByRole("button", { name: "Instalar actualizaciones" }));
    expect(await screen.findByText(/Instalación en cola en 1 equipo\./)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Ver sus actualizaciones" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Reiniciar" }));
    const restart = await screen.findByRole("dialog", { name: "Reiniciar en la ventana de mantenimiento" });
    expect(within(restart).getByRole("button", { name: "Programar reinicio" })).toBeInTheDocument();
  });
});

// -------------------------------------------------------- the software list

describe("the software page in Spanish", () => {
  const software = (overrides: Partial<ApiClient> = {}) => {
    mockUseApi.mockReturnValue({
      searchSoftware: vi.fn().mockResolvedValue({
        items: [
          { name: "Paquete de ejemplo", version: "140.0", publisher: "Editor SA", device_count: 214 },
          { name: "Otra herramienta", version: "2.1", publisher: "Taller SL", device_count: 1 },
        ],
        total: 2,
      }),
      softwareDevices: vi.fn().mockResolvedValue([{ device_id: "d1", hostname: "ws-alpha", version: "140.0" }]),
      exportInventory: vi.fn().mockResolvedValue(new Blob(["x"])),
      ...overrides,
    } as unknown as ApiClient);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return renderInSpanish(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <SoftwarePage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
  };

  it("words the list, the count, the selection prompt and a selected title", async () => {
    const user = userAction();
    const { container } = software();
    expect(await screen.findByRole("heading", { name: "Software" })).toBeInTheDocument();
    expect(screen.getByText("Lo que está instalado en toda la flota, según el inventario.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Exportar software" })).toBeInTheDocument();
    expect(await screen.findByText("Paquete de ejemplo")).toBeInTheDocument();
    expect(screen.getByLabelText("Buscar software")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Editor, producto, versión")).toBeInTheDocument();
    expect(await screen.findByTestId("software-count")).toHaveTextContent("2 títulos");
    for (const header of ["Nombre", "Versión", "Instalaciones"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(screen.getByRole("heading", { name: "Ningún software seleccionado" })).toBeInTheDocument();
    expect(screen.getByText("Elige un título del inventario de software.")).toBeInTheDocument();
    expect(screen.getByText("Instalado en")).toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "Paquete de ejemplo" }));
    expect(await screen.findByText("Dispositivos que informan de este título")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Dispositivo" })).toBeInTheDocument();
    expectNoEnglish(container, [...STORED, "Editor SA", "Taller SL"]);
  });

  it("agrees the title count and words the empty answers", async () => {
    const one = software({ searchSoftware: vi.fn().mockResolvedValue({ items: [{ name: "Solo uno", version: "1", publisher: "X", device_count: 1 }], total: 1 }) });
    expect(await screen.findByText("Solo uno")).toBeInTheDocument();
    expect(await screen.findByTestId("software-count")).toHaveTextContent("1 título");
    one.unmount();

    const none = software({ searchSoftware: vi.fn().mockResolvedValue({ items: [], total: 0 }) });
    expect(await screen.findByText("Todavía no se ha descubierto ningún software.")).toBeInTheDocument();
    none.unmount();

    const user = userAction();
    software({ searchSoftware: vi.fn().mockResolvedValue({ items: [], total: 0 }) });
    await screen.findByText("Todavía no se ha descubierto ningún software.");
    await user.type(screen.getByLabelText("Buscar software"), "zz");
    expect(await screen.findByText("Ningún software coincide con esa búsqueda.")).toBeInTheDocument();
  });

  it("words a selected title with no devices", async () => {
    const user = userAction();
    software({ softwareDevices: vi.fn().mockResolvedValue([]) });
    await user.click(await screen.findByRole("link", { name: "Paquete de ejemplo" }));
    expect(await screen.findByText("Ningún dispositivo informa de este software.")).toBeInTheDocument();
  });

  it("words a failed export and re-words it on a live switch", async () => {
    const user = userAction();
    software({ exportInventory: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    await user.click(await screen.findByRole("button", { name: "Exportar software" }));
    const alertBefore = await screen.findByRole("alert");
    expect(alertBefore).toHaveTextContent("La exportación falló.");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent("The export failed.");
  });
});

// ----------------------------------------- sentences built outside React

describe("the inventory sentences built outside React, in Spanish", () => {
  beforeEach(() => setActiveLanguage("es"));

  it("agrees the alert and update lines with their counts", () => {
    expect(alertLine([])).toEqual({ text: "Sin alertas abiertas", kind: "ok" });
    expect(alertLine([{ severity: "critical" } as never])).toEqual({ text: "1 alerta abierta", kind: "crit" });
    expect(alertLine([{ severity: "warning" } as never, { severity: "info" } as never]).text).toBe("2 alertas abiertas");
    const status = (counts: Record<string, number>, extra = {}) => ({ last_scan_at: "2026-09-01T00:00:00Z", counts_by_state: counts, pending_reboot: false, restart: null, ...extra }) as never;
    expect(updatesLine(status({ failed: 1 }))?.text).toBe("1 actualización fallida");
    expect(updatesLine(status({ failed: 3 }))?.text).toBe("3 actualizaciones fallidas");
    expect(updatesLine(status({ missing: 1 }))?.text).toBe("Falta 1 actualización");
    expect(updatesLine(status({ missing: 2 }))?.text).toBe("Faltan 2 actualizaciones");
    expect(updatesLine(status({}, { pending_reboot: true }))?.text).toBe("Reinicio pendiente");
    expect(updatesLine(status({}))?.text).toBe("Al día");
    expect(updatesLine(status({}, { last_scan_at: null }))?.text).toBe("Actualizaciones nunca comprobadas");
  });

  it("words the certificate line, the fact badges and the unit attributes", () => {
    const agent = { agent_status: "online", cert_revoked: false } as never;
    expect(certificateLine({ ...(agent as object), cert_revoked: true } as never)?.text).toBe("Certificado revocado");
    expect(certificateLine({ ...(agent as object), cert_state: "expired" } as never)?.text).toBe("Certificado caducado");
    expect(certificateLine({ ...(agent as object), cert_state: "renewing" } as never)?.text).toBe("Certificado en renovación");
    expect(certificateLine({ ...(agent as object), cert_state: "ok" } as never)?.text).toBe("Certificado válido");
    expect(factBadge("disk_encryption", false)).toEqual({ label: "Sin cifrar", variant: "warning" });
    expect(factBadge("firewall_enabled", true)).toEqual({ label: "Cortafuegos activado", variant: "success" });
    expect(factBadge("domain_joined", true)?.label).toBe("Unido al dominio");
    expect(presentAttributes({ size_bytes: 1024, speed_mbps: 2000, vram_bytes: 0, note: "x" })).toEqual({
      Tamaño: "1.0 KiB",
      "Velocidad del enlace": "2 Gbit/s",
      "Memoria de vídeo": "0 B",
      note: "x",
    });
  });
});

// ------------------------------------------------------------ live switching

describe("a live language switch on the device page", () => {
  it("re-words the header, the facts, the tab strip and the Overview", async () => {
    renderDevice();
    const status = await screen.findByRole("group", { name: "Estado del dispositivo" });
    expect(await within(status).findByText("2 alertas abiertas")).toBeInTheDocument();
    expect(await within(status).findByText("Faltan 4 actualizaciones")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Resumen" })).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    const english = screen.getByRole("group", { name: "Device status" });
    expect(within(english).getByText("2 open alerts")).toBeInTheDocument();
    expect(within(english).getByText("4 updates missing")).toBeInTheDocument();
    expect(within(english).getByText("Certificate expiring")).toBeInTheDocument();
    expect(within(english).getByText("Enrolled by agent")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Overview" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run job" })).toBeInTheDocument();
  });

  it("re-words an action outcome already on screen", async () => {
    const user = userAction();
    renderDevice(EVERYTHING, { applyPatches: vi.fn().mockResolvedValue({ job_ids: ["j-apply"] }), listPatchRings: vi.fn().mockResolvedValue({ items: [], total: 0, device_total: 0 }) });
    await user.click(await screen.findByRole("button", { name: "Instalar actualizaciones" }));
    const dialog = await screen.findByRole("dialog", { name: "Instalar las actualizaciones aprobadas" });
    await user.click(within(dialog).getByRole("button", { name: "Instalar actualizaciones" }));
    expect(await screen.findByText(/Instalación en cola en 1 equipo\./)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Ver sus actualizaciones" })).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByText(/Install queued on 1 host\./)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "See its updates" })).toBeInTheDocument();
  });

  it("re-words a failed action", async () => {
    const user = userAction();
    renderDevice(EVERYTHING, { applyPatches: vi.fn().mockRejectedValue(new ApiError(503, "")), listPatchRings: vi.fn().mockResolvedValue({ items: [], total: 0, device_total: 0 }) });
    await user.click(await screen.findByRole("button", { name: "Instalar actualizaciones" }));
    const dialog = await screen.findByRole("dialog", { name: "Instalar las actualizaciones aprobadas" });
    await user.click(within(dialog).getByRole("button", { name: "Instalar actualizaciones" }));
    const notice = await screen.findByRole("status");
    const before = notice.textContent;
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("status").textContent).not.toBe(before);
  });
});
