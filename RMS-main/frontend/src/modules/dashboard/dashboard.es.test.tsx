/**
 * The dashboard in Spanish: the "needs you" list, the six tiles, every
 * panel, the customise mode with its toolbar and widget frames, the readiness
 * box and the states with nothing to show. Other modules' labels that the
 * dashboard borrows (a health check's name, a job type, an alert's reading)
 * belong to those modules and are translated with them.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import type { ApiClient, Device } from "../../lib/api";
import { DashboardPage } from "./DashboardPage";
import { needsYou } from "./needsYou";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ALL = [
  "devices.read",
  "jobs.read",
  "health.read",
  "ticket.read",
  "patch.read",
  "backup.read",
  "remote.control",
  "change.read",
];

function device(overrides: Partial<Device> = {}): Device {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    hostname: "ws-alpha",
    fqdn: "ws-alpha.corp.local",
    os_family: "windows",
    os_version: "11 23H2",
    architecture: "amd64",
    device_group_id: null,
    status: "active",
    agent_status: "online",
    last_seen_at: "2026-08-16T09:00:00Z",
    cert_not_after: null,
    cert_state: null,
    cert_revoked: false,
    lifecycle: "in_service",
    asset_type: "computer",
    source: "agent",
    provenance: "agent",
    description: null,
    primary_ip: null,
    owner: null,
    location: null,
    ...overrides,
  };
}

const saveDashboardLayout = vi.fn();

function renderPage(permissions: string[] = ALL, overrides: Record<string, () => Promise<unknown>> = {}) {
  saveDashboardLayout.mockImplementation((widgets: unknown) => Promise.resolve({ widgets }));
  const stubs: Record<string, () => Promise<unknown>> = {
    listDevices: () =>
      Promise.resolve({
        items: [
          device(),
          device({ id: "2", hostname: "srv-beta", fqdn: null, agent_status: "offline" }),
          device({ id: "3", hostname: "srv-gamma", agent_status: null }),
        ],
        total: 9,
        fleet: { total: 9, managed: 5, online: 3, cert_attention: 2 },
      }),
    listJobs: () =>
      Promise.resolve({
        items: [
          { id: "j1", type: "patch.install", status: "failed", issued_at: "2026-08-16T10:00:00Z" },
          { id: "j2", type: "inventory.collect", status: "running", issued_at: new Date().toISOString() },
          { id: "j3", type: "inventory.collect", status: "queued", issued_at: "2020-01-01T00:00:00Z" },
        ],
        total: 3,
      }),
    listHealthAlerts: () =>
      Promise.resolve([
        {
          id: "a1",
          severity: "critical",
          check_type: "disk.free_pct",
          selector: "C:",
          device_hostname: "ws-alpha",
          observed_value: "3%",
          threshold_display: "< 10%",
          opened_at: "2026-08-16T09:00:00Z",
        },
      ]),
    getHealthAlertSummary: () =>
      Promise.resolve({
        open: 3,
        by_state: { active: 3, acknowledged: 0, muted: 0 },
        by_severity: { info: 0, warning: 2, critical: 1 },
        worst_severity: "critical",
      }),
    listTickets: () =>
      Promise.resolve({
        items: [
          { id: "t1", status: "open", assigned_to: null },
          { id: "t2", status: "new", assigned_to: null },
        ],
        total: 7,
      }),
    getPatchFleetReport: () =>
      Promise.resolve({
        compliant_pct_by_os_family: { windows: 61 },
        compliant_pct_by_severity: {},
        exposed_devices: [],
        last_scan_at: "2026-08-16T09:00:00Z",
        devices_scanned: 9,
        devices_total: 14,
      }),
    getBackupReport: () =>
      Promise.resolve({
        devices_total: 14,
        devices_covered: 10,
        coverage_rate: 0.71,
        run_counts_by_state: {},
        success_rate: 0.94,
        points_needing_verification: 4,
        dedup: {},
      }),
    listRemoteSessions: () =>
      Promise.resolve({
        items: [
          { id: "r1", consent_mode: "attended" },
          { id: "r2", consent_mode: "unattended" },
        ],
        total: 2,
      }),
    getChangeCalendar: () =>
      Promise.resolve({
        entries: [
          {
            change_id: "c1",
            title: "Despliegue de firmware",
            type: "normal",
            risk: "medium",
            state: "approved",
            window_start: "2026-08-20T09:00:00Z",
            window_end: "2026-08-20T11:00:00Z",
          },
          { change_id: "c2", title: "Cambio sin ventana", type: "normal", risk: "low", state: "approved", window_start: null, window_end: null },
        ],
        conflicts: [],
      }),
    getBackupPolicies: () => Promise.resolve([]),
    listBackupPolicies: () => Promise.resolve([]),
    getDashboardLayout: () => Promise.resolve({ widgets: null }),
    resetDashboardLayout: () => Promise.resolve(undefined),
    ...overrides,
  };
  mockUseApi.mockReturnValue({
    me: () =>
      Promise.resolve({
        user_id: "u",
        org_id: "o",
        email: "op@example.com",
        display_name: "Operador",
        permissions,
        timezone: "Europe/Madrid",
      }),
    saveDashboardLayout,
    ...stubs,
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderInSpanish(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// Words borrowed from other modules: the OS names, written the same in
// Spanish. Job types come from `enums.JobType` and a health check's name from
// `enums.HealthCheckType`, so both are covered.
const BORROWED = ["Windows"];

beforeEach(() => {
  mockUseApi.mockReset();
  saveDashboardLayout.mockReset();
});

describe("the dashboard in Spanish", () => {
  it("shows the needs-you list, the tiles and every panel in Spanish", async () => {
    const { container } = renderPage();
    expect(screen.getByRole("heading", { name: "Panel" })).toBeInTheDocument();
    await screen.findByText("ws-alpha");

    // Needs you.
    const needs = screen.getByText("Requiere tu atención").closest(".panel") as HTMLElement;
    expect(within(needs).getByText("Lo más urgente primero")).toBeInTheDocument();
    expect(await within(needs).findByText("1 tarea fallida")).toBeInTheDocument();
    expect(within(needs).getByText("Averigua por qué antes de enviarla de nuevo")).toBeInTheDocument();
    expect(within(needs).getByText("2 dispositivos están sin conexión")).toBeInTheDocument();
    expect(within(needs).getByText("2 certificados de dispositivo requieren atención")).toBeInTheDocument();
    expect(within(needs).getByText("1 tarea lleva demasiado tiempo esperando")).toBeInTheDocument();
    expect(within(needs).getByText("1 ticket nuevo por clasificar")).toBeInTheDocument();
    expect(within(needs).getByText("4 copias de seguridad sin verificar")).toBeInTheDocument();
    expect(within(needs).getByText(/en ws-alpha/)).toBeInTheDocument();

    // Tiles.
    const tiles = [...document.querySelectorAll(".stat-card")].map((card) => card.textContent ?? "");
    expect(tiles.some((t) => t.includes("Tareas que requieren atención") && t.includes("1 fallida · 1 detenida"))).toBe(true);
    expect(tiles.some((t) => t.includes("Alertas abiertas") && t.includes("1 crítica · 2 de advertencia"))).toBe(true);
    expect(tiles.some((t) => t.includes("Equipos administrados") && t.includes("de 9 activos"))).toBe(true);
    expect(tiles.some((t) => t.includes("En línea ahora") && t.includes("de 5 con un agente"))).toBe(true);
    expect(tiles.some((t) => t.includes("Tickets abiertos") && t.includes("1 sin clasificar"))).toBe(true);
    expect(tiles.some((t) => t.includes("Sesiones remotas en curso") && t.includes("1 con asistencia · 1 sin asistencia"))).toBe(true);

    // Panels.
    const fleet = screen.getByText("La flota de un vistazo").closest(".panel") as HTMLElement;
    expect(within(fleet).getByText("Sobre los 3 más recientes de 9 dispositivos")).toBeInTheDocument();
    expect(within(fleet).getByText("Requieren a una persona")).toBeInTheDocument();
    expect(within(fleet).getByText("Sin agente")).toBeInTheDocument();
    expect(fleet.querySelector(".heat")?.getAttribute("aria-label")).toMatch(/^3 dispositivos:/);

    const desk = screen.getByText("Mesa de ayuda").closest(".panel") as HTMLElement;
    expect(within(desk).getByText("Trabajo abierto, contado sobre los 2 tickets más recientes de 7")).toBeInTheDocument();
    expect(within(desk).getByText("Sin asignar")).toBeInTheDocument();

    const recent = screen.getByText("Dispositivos recientes").closest(".panel") as HTMLElement;
    expect(within(recent).getByRole("columnheader", { name: "Sistema operativo" })).toBeInTheDocument();
    expect(within(recent).getByText("Sin agente", { selector: ".pill, .pill *" })).toBeInTheDocument();
    expect(within(recent).getAllByText("Sin conexión").length).toBeGreaterThan(0);
    expect(within(recent).getByText("No se registró ningún FQDN")).toBeInTheDocument();

    expect(screen.getByText("Disparadas ahora y aún sin reconocer")).toBeInTheDocument();
    expect(screen.getByText(/2 en curso · 2 que requieren atención/)).toBeInTheDocument();
    expect(screen.getByText(/\(1 esperando demasiado tiempo\)/)).toBeInTheDocument();
    expect(screen.getByText("Analizado el", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("Dispositivos analizados")).toBeInTheDocument();
    expect(screen.getByText("9 de 14")).toBeInTheDocument();
    expect(screen.getByText("Cubiertos por una política")).toBeInTheDocument();
    expect(screen.getByText("10 de 14")).toBeInTheDocument();
    expect(screen.getByText("Puntos pendientes de verificación")).toBeInTheDocument();
    expect(screen.getByText("Sin ventana definida")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Abrir el calendario" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Abrir parches" })).toBeInTheDocument();
    expect(screen.getByText("Antes de que el panel se llene")).toBeInTheDocument();
    // A job type reads as the jobs module names it (`enums.JobType`).
    expect(screen.getAllByText("Recopilar inventario").length).toBeGreaterThan(0);
    // A health check reads as the health module names it (`enums.HealthCheckType`).
    expect(screen.getAllByText(/Disco libre/).length).toBeGreaterThan(0);
    expectNoEnglish(container, BORROWED);
  });

  it("customises in Spanish: the toolbar, the frames and their resize and hide controls", async () => {
    const user = userEvent.setup();
    const { container } = renderPage();
    await screen.findByText("ws-alpha");
    await user.click(screen.getByRole("button", { name: "Personalizar" }));
    expect(screen.getByText(/Mueve, redimensiona y oculta los mosaicos\./)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restablecer los valores predeterminados" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Listo" })).toBeInTheDocument();
    expect(
      screen.getByRole("button", {
        name: "Tareas que requieren atención es pequeño; cámbialo a mediano",
      }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Mover Alertas abiertas antes" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Mover Alertas abiertas después" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Ocultar Alertas abiertas" }));
    expect(await screen.findByText("Oculto en tu panel.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Mostrar Alertas abiertas" })).toBeInTheDocument();
    expect(screen.getByText("1 mosaico está oculto.", { exact: false })).toBeInTheDocument();
    expectNoEnglish(container, BORROWED);
  });

  it("says in Spanish when the arrangement could not be saved", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("ws-alpha");
    saveDashboardLayout.mockRejectedValue(new Error("sin red"));
    await user.click(screen.getByRole("button", { name: "Personalizar" }));
    await user.click(screen.getByRole("button", { name: "Ocultar Alertas abiertas" }));
    const alert = await screen.findByText(/Tu disposición está en pantalla pero no se guardó/);
    expect(alert).toBeInTheDocument();
  });

  it("words the empty states, the zero tiles and the missing permissions in Spanish", async () => {
    const empty = renderPage(["devices.read", "ticket.read"], {
      listDevices: () => Promise.resolve({ items: [], total: 0, fleet: { total: 0, managed: 0, online: 0, cert_attention: 0 } }),
      listTickets: () => Promise.resolve({ items: [], total: 0 }),
    });
    expect(await screen.findByText("Nada requiere tu atención ahora mismo")).toBeInTheDocument();
    expect(screen.queryByText("No hay nada que mostrar aquí")).not.toBeInTheDocument();
    expect(screen.getByText("Ningún activo tiene un agente")).toBeInTheDocument();
    expect(screen.getByText("La cola está vacía")).toBeInTheDocument();
    expect(screen.getByText("Aún no hay dispositivos inscritos.")).toBeInTheDocument();
    expect(screen.getByText("No se ha creado ningún ticket.")).toBeInTheDocument();
    expectNoEnglish(empty.container, BORROWED);
    empty.unmount();

    const none = renderPage([]);
    expect(await screen.findByText("No hay nada que mostrar aquí")).toBeInTheDocument();
    expect(screen.getByText(/Esta cuenta no tiene permisos de lectura de ningún módulo/)).toBeInTheDocument();
    expectNoEnglish(none.container, BORROWED);
  });

  it("re-labels a mounted dashboard when the language is switched", async () => {
    const view = renderPage(["devices.read"]);
    await screen.findByText("ws-alpha");
    expect(screen.getByText("Equipos administrados")).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("Managed endpoints")).toBeInTheDocument();
    expect(screen.queryByText("Equipos administrados")).not.toBeInTheDocument();
    view.unmount();
  });
});

describe("the needs-you items in Spanish", () => {
  it("name a critical alert, the surplus of critical alerts and each plural", () => {
    setActiveLanguage("es");
    try {
      const alert = (id: string) => ({
        id,
        severity: "critical",
        check_type: "disk.free_pct",
        device_hostname: "ws-alpha",
        selector: "",
        observed_value: "3",
        threshold_display: "",
      });
      const items = needsYou({
        alerts: ["a", "b", "c", "d", "e"].map(alert) as never,
        failedJobs: 2,
        fleet: { managed: 5, online: 4, cert_attention: 1 },
      });
      const titles = items.map((item) => item.title);
      expect(titles).toContain("2 alertas críticas más");
      expect(titles).toContain("2 tareas fallidas");
      expect(titles).toContain("1 dispositivo está sin conexión");
      expect(titles).toContain("1 certificado de dispositivo requiere atención");
      expect(titles.filter((t) => t.endsWith("en ws-alpha"))).toHaveLength(3);
    } finally {
      setActiveLanguage("en");
    }
  });
});
