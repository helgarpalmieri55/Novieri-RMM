/**
 * Reporting in Spanish: the catalogue of reports, one report's parameters,
 * refusals, result, totals, figure, table and paging, the readiness panel, and
 * the labels built outside React. What the SERVER sends stays as sent, and the
 * fixtures word it in Spanish to show that: each report's summary and
 * description, a parameter's title and description, a source's recency note.
 * The words the console builds itself (the built-in report names, the figure
 * captions, the column headers and totals labels, the enum cells) are
 * Spanish. CSV column names are the server's export header and are not
 * translated.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { en } from "../../i18n/en";
import { es } from "../../i18n/es";
import { setActiveLanguage, translate } from "../../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import { ApiError } from "../../lib/api";
import type { ApiClient, Me } from "../../lib/api";
import { ReportCataloguePage } from "./ReportCatalogue";
import { CHECKED_REPORTS, ReportReadiness } from "./ReportReadiness";
import { figureFor } from "./reportFigures";
import { fieldLabel, reportName, sourceLine, vocabularyCell } from "./reportText";
import { ReportViewPage } from "./ReportView";
import type { ReportCatalogue, ReportPage } from "./types";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "operator@example.com",
  display_name: "Operator",
  permissions: ["reporting.read", "devices.read", "patch.read", "backup.read", "sla.read"],
  timezone: "UTC",
};

const PAGING = {
  limit: { type: "integer", default: 25, minimum: 1, maximum: 1000, title: "Filas", description: "Máximo de filas." },
  offset: { type: "integer", default: 0, minimum: 0, title: "Desplazamiento", description: "Filas que saltar." },
};

const CATALOGUE: ReportCatalogue = {
  reports: [
    {
      key: "fleet-posture",
      summary: "Cada dispositivo con su estado de contacto y sus alertas abiertas.",
      description: "Una fila por dispositivo.",
      requires: ["devices.read", "health.read"],
      entitled: true,
      missing_permissions: [],
      params_schema: {
        type: "object",
        additionalProperties: false,
        properties: {
          os_family: {
            anyOf: [{ type: "string", enum: ["windows", "linux", "macos"] }, { type: "null" }],
            default: null,
            title: "Familia del SO",
            description: "Limita a una familia.",
          },
          severity_at_least: {
            type: "string",
            enum: ["info", "warning", "critical"],
            default: "warning",
            title: "Gravedad mínima",
          },
          stale_after_days: {
            type: "integer",
            default: 9,
            minimum: 1,
            maximum: 365,
            title: "Días hasta desactualizado",
          },
          ...PAGING,
        },
      },
    },
    {
      key: "sla-attainment",
      summary: "Cumplimiento por cola y prioridad en una ventana cerrada.",
      description: "Una fila por cola y prioridad.",
      requires: ["sla.read", "ticket.read"],
      entitled: false,
      missing_permissions: ["sla.read"],
      params_schema: {
        type: "object",
        additionalProperties: false,
        required: ["from", "to"],
        properties: {
          from: { type: "string", format: "date-time", title: "Desde" },
          to: { type: "string", format: "date-time", title: "Hasta" },
          queue_id: { anyOf: [{ type: "string", format: "uuid" }, { type: "null" }], default: null, title: "Cola" },
          priority: {
            anyOf: [{ type: "string", enum: ["low", "normal", "high", "urgent"] }, { type: "null" }],
            default: null,
            title: "Prioridad",
          },
          limit: PAGING.limit,
        },
      },
    },
    {
      key: "patch-compliance",
      summary: "Cada dispositivo con sus actualizaciones pendientes.",
      description: "Una fila por dispositivo.",
      requires: ["devices.read", "patch.read"],
      entitled: true,
      missing_permissions: [],
      params_schema: { type: "object", additionalProperties: false, properties: { ...PAGING } },
    },
    {
      key: "backup-coverage",
      summary: "Cada dispositivo con su veredicto de cobertura.",
      description: "Una fila por dispositivo.",
      requires: ["backup.read"],
      entitled: true,
      missing_permissions: [],
      params_schema: { type: "object", additionalProperties: false, properties: { ...PAGING } },
    },
  ],
};

function fleetPage(overrides: Partial<ReportPage["meta"]> = {}): ReportPage {
  return {
    meta: {
      report_key: "fleet-posture",
      generated_at: "2026-03-04T09:15:00.000Z",
      reproducible: true,
      window_from: null,
      window_to: null,
      total: 120,
      limit: 25,
      offset: 0,
      sources: [
        { module: "devices", permission: "devices.read", recency: "a fecha del último contacto del agente" },
        { module: "health", permission: "health.read", recency: "ahora" },
      ],
      ...overrides,
    },
    totals: { devices: 120, stale_devices: 7, devices_with_open_alerts: 3 },
    rows: [
      {
        device_id: "d1",
        hostname: "alfa",
        os_family: "macos",
        agent_status: "never_enrolled",
        scan_state: "fresh",
        coverage: "unprotected",
        priority: "urgent",
        stale: false,
        last_seen_at: "2026-03-01T09:00:00.000Z",
        open_alerts_critical: 0,
        newest_alert_opened_at: null,
      },
      {
        device_id: "d2",
        hostname: "beta",
        os_family: "windows",
        agent_status: "online",
        scan_state: "stale",
        coverage: "covered",
        priority: "low",
        stale: true,
        last_seen_at: "2026-02-20T09:00:00.000Z",
        open_alerts_critical: 3,
        newest_alert_opened_at: "2026-03-02T09:00:00.000Z",
      },
    ],
  };
}

beforeEach(() => {
  mockUseApi.mockReset();
});

function mountReport(api: Record<string, unknown>, key = "fleet-posture") {
  mockUseApi.mockReturnValue({
    listReports: vi.fn().mockResolvedValue(CATALOGUE),
    me: vi.fn().mockResolvedValue(ME),
    ...api,
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderInSpanish(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/reports/${key}`]}>
        <Routes>
          <Route path="/reports/:reportKey" element={<ReportViewPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function mountCatalogue(api: Record<string, unknown> = {}, at = "/reports") {
  mockUseApi.mockReturnValue({
    listReports: vi.fn().mockResolvedValue(CATALOGUE),
    me: vi.fn().mockResolvedValue(ME),
    listDevices: vi.fn().mockResolvedValue({ items: [], total: 0, fleet: { total: 0, managed: 0, online: 0 } }),
    getPatchFleetReport: vi.fn().mockResolvedValue({ devices_scanned: 0 }),
    listPatchApprovals: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    listBackupPolicies: vi.fn().mockResolvedValue([]),
    listSlaPolicies: vi.fn().mockResolvedValue([]),
    ...api,
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderInSpanish(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[at]}>
        <ReportCataloguePage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("the report catalogue in Spanish", () => {
  it("names each report, says what the caller cannot run, and what is missing", async () => {
    const { container } = mountCatalogue();
    expect(await screen.findByRole("heading", { name: "Informes y evidencias" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Postura del parque" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Cumplimiento de SLA" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Cobertura de copias de seguridad" })).toBeInTheDocument();
    const labels = [...container.querySelectorAll(".stat-card .stat-label")].map((el) => el.textContent);
    expect(labels).toEqual(["Plantillas de informe", "Ejecutables por ti", "Falta un permiso"]);
    expect(screen.getByText(/^No puedes ejecutar este informe\. Necesita /)).toBeInTheDocument();
    expect(screen.getAllByText(/^Necesita /).length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: "Abrir Postura del parque" })).toHaveAttribute("href", "/reports/fleet-posture");
    expect(screen.getByRole("columnheader", { name: "Acceso" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Exportación masiva / de conjuntos de datos para BI", level: 2 })).toBeInTheDocument();
    // The server's summaries and descriptions are shown as they were sent.
    expect(screen.getByText("Cada dispositivo con su estado de contacto y sus alertas abiertas.")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("says what the readiness panel is waiting for, with every count agreeing", async () => {
    const { container } = mountCatalogue({
      listPatchApprovals: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    });
    expect(await screen.findByText("Antes de que estos informes tengan algo que mostrar")).toBeInTheDocument();
    expect(await screen.findByText(/^Ningún equipo ejecuta todavía el agente/)).toBeInTheDocument();
    expect(await screen.findByText(/^No se ha escaneado ningún equipo en busca de actualizaciones/)).toBeInTheDocument();
    expect(await screen.findByText(/^No hay ninguna política de copias de seguridad habilitada/)).toBeInTheDocument();
    expect(await screen.findByText(/^No hay ninguna política de niveles de servicio activa/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Configurar copias de seguridad" })).toHaveAttribute("href", "/backup/policies");
    expect(screen.getByRole("link", { name: "Definir objetivos de respuesta" })).toHaveAttribute("href", "/sla");
    expectNoEnglish(container);
  });

  it("agrees the done sentences with their numbers", async () => {
    const stub = {
      listDevices: vi.fn().mockResolvedValue({ items: [], total: 1, fleet: { total: 1, managed: 1, online: 1 } }),
      getPatchFleetReport: vi.fn().mockResolvedValue({ devices_scanned: 1 }),
      listPatchApprovals: vi.fn().mockResolvedValue({ items: [], total: 2 }),
      listBackupPolicies: vi.fn().mockResolvedValue([{ id: "p1", enabled: true }, { id: "p2", enabled: true }]),
      // One thing missing, so the panel shows at all (it renders nothing when every row is done).
      listSlaPolicies: vi.fn().mockResolvedValue([]),
    };
    mockUseApi.mockReturnValue({ me: vi.fn().mockResolvedValue(ME), ...stub } as unknown as ApiClient);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { container } = renderInSpanish(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <ReportReadiness keys={CHECKED_REPORTS} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(await screen.findByText("Se ha escaneado 1 equipo y hay 2 aprobaciones en vigor.")).toBeInTheDocument();
    expect(screen.getByText("Hay 2 políticas de copias de seguridad habilitadas.")).toBeInTheDocument();
    expect(screen.getByText("Postura del parque: un equipo ejecuta el agente")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("filters to what the caller can run and says how much it hides", async () => {
    const { container } = mountCatalogue({}, "/reports?access=runnable");
    expect(await screen.findByText(/^Mostrando solo los informes que puedes ejecutar\. Este filtro oculta 1 de 4 informes\./)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Mostrar todos los informes" })).toHaveAttribute("href", "/reports");
    expectNoEnglish(container);
  });

  it("says what is blocked, and that nothing is when nothing is", async () => {
    const blocked = mountCatalogue({}, "/reports?access=blocked");
    expect(await screen.findByText(/^Mostrando solo los informes para los que te falta un permiso\. Este filtro oculta 3 de 4 informes\./)).toBeInTheDocument();
    blocked.unmount();
    const none = mountCatalogue(
      { listReports: vi.fn().mockResolvedValue({ reports: CATALOGUE.reports.filter((r) => r.entitled) }) },
      "/reports?access=blocked",
    );
    expect(await screen.findByText("No te falta ningún permiso. Puedes ejecutar todos los informes de aquí.")).toBeInTheDocument();
    expectNoEnglish(none.container);
  });

  it("says when the deployment publishes no reports, and when you can run none", async () => {
    const none = mountCatalogue({ listReports: vi.fn().mockResolvedValue({ reports: [] }) });
    expect(await screen.findByText("Esta instalación no publica ningún informe.")).toBeInTheDocument();
    expect(screen.getByText("esta instalación no publica ninguna")).toBeInTheDocument();
    none.unmount();
    const nonRunnable = mountCatalogue(
      { listReports: vi.fn().mockResolvedValue({ reports: [CATALOGUE.reports[1]] }) },
      "/reports?access=runnable",
    );
    expect(await screen.findByText("No puedes ejecutar ninguno de los informes que publica esta instalación.")).toBeInTheDocument();
    expectNoEnglish(nonRunnable.container);
  });

  it("re-words the catalogue on a live switch", async () => {
    mountCatalogue({}, "/reports?access=runnable");
    await screen.findByRole("heading", { name: "Postura del parque" });
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("heading", { name: "Fleet posture" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Backup coverage" })).toBeInTheDocument();
    expect(screen.getByText(/^Showing only the reports you can run\. 1 of 4 are hidden by this filter\./)).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    expect(screen.getByRole("heading", { name: "Cobertura de copias de seguridad" })).toBeInTheDocument();
  });
});

describe("a report page in Spanish", () => {
  it("renders the parameters, the totals, the sources, the table and the paging", async () => {
    const runReport = vi.fn().mockResolvedValue(fleetPage());
    const { container } = mountReport({ runReport, exportReport: vi.fn() });
    expect(await screen.findByRole("heading", { name: "Postura del parque" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Descargar CSV" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Todos los informes" })).toHaveAttribute("href", "/reports");
    expect(await screen.findByText("Parámetros")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ejecutar" })).toBeInTheDocument();
    // Parameter titles come from the server; the option words are the console's.
    expect(await screen.findByText("Familia del SO")).toBeInTheDocument();
    const family = screen.getByLabelText(/^Familia del SO/);
    // In Spanish the visible title names the input; the English wire name is not announced.
    expect(screen.getByRole("combobox", { name: /^Familia del SO/ })).toBe(family);
    expect(family).not.toHaveAttribute("aria-label");
    expect([...(family as HTMLSelectElement).options].map((o) => o.textContent)).toEqual(["Cualquiera", "Windows", "Linux", "macOS"]);
    const severity = screen.getByLabelText(/^Gravedad mínima/);
    expect([...(severity as HTMLSelectElement).options].map((o) => o.textContent)).toEqual(["Cualquiera", "Informativo", "Advertencia", "Crítico"]);
    // The result.
    expect(await screen.findByText(/^A fecha de .*\. 120 filas coincidentes\.$/)).toBeInTheDocument();
    for (const [label, value] of [["Dispositivos", "120"], ["Dispositivos desactualizados", "7"], ["Dispositivos con alertas abiertas", "3"]]) {
      const tile = [...container.querySelectorAll(".tile")].find((el) => el.textContent?.includes(label as string));
      expect(tile?.textContent).toContain(value);
    }
    // The result table is the last one: the figure's own data table comes before it.
    const resultTable = [...container.querySelectorAll("table")].at(-1) as HTMLElement;
    const headers = within(resultTable).getAllByRole("columnheader").map((h) => h.textContent);
    expect(headers).toEqual([
      "Dispositivo",
      "Nombre de host",
      "Familia de SO",
      "Estado del agente",
      "Estado del escaneo",
      "Cobertura",
      "Prioridad",
      "Desactualizado",
      "Visto por última vez",
      "Alertas críticas abiertas",
      "Alerta más reciente abierta",
    ]);
    const first = [...(resultTable.querySelector("tbody tr") as HTMLElement).querySelectorAll("td")].map((td) => td.textContent);
    expect(first.slice(2, 8)).toEqual(["macOS", "Nunca inscrito", "Reciente", "Sin protección", "Urgente", "No"]);
    const second = [...(resultTable.querySelectorAll("tbody tr")[1] as HTMLElement).querySelectorAll("td")].map((td) => td.textContent);
    expect(second.slice(2, 8)).toEqual(["Windows", "En línea", "Desactualizado", "Cubierto", "Baja", "Sí"]);
    expect(screen.getAllByText("Sin registrar").length).toBeGreaterThan(0);
    expect(screen.getByText("De dónde salen estas cifras")).toBeInTheDocument();
    expect(screen.getByText(/^Dispositivos, a fecha del último contacto del agente\. Necesita /)).toBeInTheDocument();
    expect(screen.getByText(/^Salud, ahora\. Necesita /)).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Filas del informe" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Filas anteriores" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Filas siguientes" })).toBeEnabled();
    expect(screen.getByText("Filas 1–25 de 120")).toBeInTheDocument();
    // Hostnames and the server's own words are recorded data.
    expectNoEnglish(container);
  });

  it("says nothing has run yet, and names the figure in Spanish with its window", async () => {
    const user = userEvent.setup();
    const page: ReportPage = {
      meta: { ...fleetPage().meta, report_key: "sla-attainment", reproducible: false, window_from: "2026-03-01T00:00:00.000Z", window_to: "2026-04-01T00:00:00.000Z", total: 4, offset: 0, limit: 5 },
      totals: { response_met: 31, response_breached: 4, resolution_met: 28, resolution_breached: 14, tickets: 45 },
      rows: [{ queue_id: "q1", queue_name: "Soporte", priority: "high", tickets: 45, response_attainment_pct: 88 }],
    };
    const { container } = mountReport(
      { runReport: vi.fn().mockResolvedValue(page), exportReport: vi.fn(), listTicketQueues: vi.fn().mockResolvedValue([{ id: "q1", key: "soporte", name: "Soporte", active: false }]) },
      "sla-attainment",
    );
    expect(await screen.findByText("Todavía no se ha ejecutado nada")).toBeInTheDocument();
    expect(screen.getByText("Define los parámetros de arriba y elige Ejecutar.")).toBeInTheDocument();
    expect(screen.getByText(/^La exportación envía los parámetros de la ejecución/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Descargar CSV" })).toBeDisabled();
    expect(screen.getByRole("option", { name: "Alta", hidden: true })).toBeInTheDocument();
    // The refusal is held as data: it names the printed titles, and re-words on a switch.
    await user.click(screen.getByRole("button", { name: "Ejecutar" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("El campo Desde es obligatorio");
    expect(alert).toHaveTextContent("El campo Hasta es obligatorio");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent("Desde is required");
    act(() => setActiveLanguage("es"));
    fireEvent.change(screen.getByLabelText(/^Desde/), { target: { value: "2026-03-01T00:00" } });
    fireEvent.change(screen.getByLabelText(/^Hasta/), { target: { value: "2026-04-01T00:00" } });
    await user.click(screen.getByRole("button", { name: "Ejecutar" }));
    const figure = await screen.findByRole("figure");
    expect(figure).toHaveTextContent(/^Cumplidos frente a incumplidos, por reloj \(/);
    expect(within(figure).getByRole("rowheader", { name: "Respuesta" })).toBeInTheDocument();
    expect(within(figure).getByRole("rowheader", { name: "Resolución" })).toBeInTheDocument();
    expect(within(figure).getByRole("columnheader", { name: "Cumplidos" })).toBeInTheDocument();
    expect(within(figure).getByRole("columnheader", { name: "Incumplidos" })).toBeInTheDocument();
    expect(figure).toHaveTextContent(/\(del .* al .*\)/);
    expect(screen.getByText(/^Estas cifras pueden cambiar/)).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("refuses a number outside the published bounds and a bad date, in Spanish", async () => {
    const user = userEvent.setup();
    mountReport({ runReport: vi.fn().mockResolvedValue(fleetPage()), exportReport: vi.fn() });
    const limit = await screen.findByLabelText(/^Filas/);
    fireEvent.change(limit, { target: { value: "0" } });
    await user.click(screen.getByRole("button", { name: "Ejecutar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Filas: debe ser al menos 1");
    fireEvent.change(limit, { target: { value: "5000" } });
    await user.click(screen.getByRole("button", { name: "Ejecutar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Filas: debe ser como máximo 1000");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent("Filas must be at most 1000");
  });

  it("says nothing matched rather than showing zeros", async () => {
    const empty: ReportPage = { ...fleetPage({ total: 0 }), totals: { devices: 0 }, rows: [] };
    const { container } = mountReport({ runReport: vi.fn().mockResolvedValue(empty), exportReport: vi.fn() });
    expect(await screen.findByText("Nada coincide con estos parámetros")).toBeInTheDocument();
    expect(screen.getByText(/^Ninguna fila coincidió, así que no se muestran totales\./)).toBeInTheDocument();
    expect(screen.getByText(/0 filas coincidentes/)).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("words a failed run, a failed export and a too-big export from the held error", async () => {
    const user = userEvent.setup();
    const exportReport = vi.fn().mockRejectedValueOnce(new ApiError(413, "")).mockRejectedValueOnce(new ApiError(503, ""));
    mountReport({ runReport: vi.fn().mockResolvedValue(fleetPage()), exportReport });
    await screen.findByText(/filas coincidentes/);
    await user.click(screen.getByRole("button", { name: "Descargar CSV" }));
    expect(await screen.findByText("Hay demasiadas filas para exportarlas en un solo archivo. Acota los parámetros e inténtalo de nuevo.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Descargar CSV" }));
    const alert = await screen.findByText(/^Ha fallado la exportación\. /);
    expect(alert.textContent).toMatch(/no está disponible/i);
    act(() => setActiveLanguage("en"));
    expect(screen.getByText(/^The export failed\. /)).toBeInTheDocument();
  });

  it("words a refused run and a refused catalogue", async () => {
    const failed = mountReport({ runReport: vi.fn().mockRejectedValue(new ApiError(403, "")) });
    expect(await screen.findByText(/^No se pudo ejecutar el informe\. /)).toBeInTheDocument();
    expectNoEnglish(failed.container);
    failed.unmount();
    const refused = mountReport({ listReports: vi.fn().mockRejectedValue(new ApiError(403, "")) });
    expect(await screen.findByText(/^No se pudo cargar el catálogo de informes/)).toBeInTheDocument();
    // The refused catalogue leaves no parameter bar, so nothing wire-named is on the page.
    expectNoEnglish(refused.container);
  });

  it("says the deployment publishes no such report, naming the key in code", async () => {
    const { container } = mountReport({}, "no-existe");
    const note = await screen.findByText(/^Esta instalación no publica ningún informe llamado /);
    expect(within(note).getByText("no-existe").tagName).toBe("CODE");
    expect(screen.getByRole("heading", { name: "No existe" })).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("explains an empty queue list in Spanish", async () => {
    const empty = mountReport({ listTicketQueues: vi.fn().mockResolvedValue([]) }, "sla-attainment");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("combobox", { name: "Cola" }));
    expect(await screen.findByText(/^Todavía no existe ninguna cola de tickets en esta organización/)).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Buscar colas…")).toBeInTheDocument();
    expectNoEnglish(empty.container);
  });

  it("explains a refused queue list from the held error", async () => {
    const user = userEvent.setup();
    mountReport({ listTicketQueues: vi.fn().mockRejectedValue(new ApiError(503, "")) }, "sla-attainment");
    await user.click(await screen.findByRole("combobox", { name: "Cola" }));
    expect(await screen.findByText(/^No se pudo leer la lista de colas/)).toBeInTheDocument();
  });

  it("re-words the page on a live switch", async () => {
    mountReport({ runReport: vi.fn().mockResolvedValue(fleetPage()), exportReport: vi.fn() });
    await screen.findByText(/filas coincidentes/);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("heading", { name: "Fleet posture" })).toBeInTheDocument();
    expect(screen.getByText("Rows 1–25 of 120")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Hostname" })).toBeInTheDocument();
    expect(screen.getByText("Never enrolled")).toBeInTheDocument();
    expect(screen.getByText(/^Devices, a fecha del último contacto del agente\. Needs /)).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    expect(screen.getByRole("columnheader", { name: "Nombre de host" })).toBeInTheDocument();
  });
});

describe("the labels built outside React", () => {
  it("names the reports, the fields and the cells in both languages, and humanises what it has not met", () => {
    setActiveLanguage("es");
    expect(["fleet-posture", "sla-attainment", "backup-coverage", "patch-compliance"].map(reportName)).toEqual([
      "Postura del parque",
      "Cumplimiento de SLA",
      "Cobertura de copias de seguridad",
      "Cumplimiento de parches",
    ]);
    // A report the console has not met reads as its key, derived.
    expect(reportName("energy-use")).toBe("Energy use");
    expect(fieldLabel("devices_never_scanned")).toBe("Dispositivos nunca escaneados");
    expect(fieldLabel("future_total")).toBe("Future total");
    expect(vocabularyCell("coverage", "stale")).toBe("Desactualizado");
    expect(vocabularyCell("offsite", "behind")).toBe("Atrasada");
    expect(vocabularyCell("consistency_level", "live")).toBe("En vivo");
    expect(vocabularyCell("last_point_state", "pruned")).toBe("Eliminado por retención");
    expect(vocabularyCell("coverage", "newvalue")).toBeNull();
    expect(vocabularyCell("hostname", "alfa")).toBeNull();
    setActiveLanguage("en");
    expect(reportName("patch-compliance")).toBe("Patch compliance");
    expect(fieldLabel("devices_never_scanned")).toBe("Devices never scanned");
    expect(vocabularyCell("coverage", "stale")).toBe("stale");
    expect(vocabularyCell("agent_status", "never_enrolled")).toBe("Never enrolled");
    expect(vocabularyCell("priority", "urgent")).toBe("Urgent");
  });

  it("words the figure captions and the provenance line from the catalogue", () => {
    setActiveLanguage("es");
    const figure = figureFor("backup-coverage", { covered: 1, stale: 2, failing: 3, unprotected: 4 });
    expect(figure?.caption).toBe("Dispositivos por veredicto de cobertura");
    expect(figure?.categories).toEqual(["Cubiertos", "Desactualizados", "Con fallos", "Sin protección"]);
    expect(figure?.unit).toBe("dispositivos");
    const patch = figureFor("patch-compliance", { devices: 1, devices_with_zero_missing: 1, devices_never_scanned: 0 });
    expect(patch?.categories).toEqual(["Todos los dispositivos", "Sin nada pendiente", "Nunca escaneados"]);
    expect(figureFor("fleet-posture", { devices: 1, stale_devices: 0, devices_with_open_alerts: 0 })?.categories).toEqual([
      "Todos los dispositivos",
      "Contacto desactualizado",
      "Con alertas abiertas",
    ]);
    expect(sourceLine({ module: "service_desk", permission: "ticket.read", recency: "actual" })).toMatch(
      /^Mesa de ayuda, actual\. Necesita /,
    );
    setActiveLanguage("en");
    expect(figureFor("backup-coverage", { covered: 1, stale: 2, failing: 3, unprotected: 4 })?.caption).toBe(
      "Devices by coverage verdict",
    );
  });

  it("agrees the counts, and keeps both catalogues the same shape", () => {
    setActiveLanguage("es");
    expect(translate("reporting.view.resultSubtitle", { date: "hoy", count: 1 })).toBe("A fecha de hoy. 1 fila coincidente.");
    expect(translate("reporting.readiness.backupDone", { count: 1 })).toBe("Hay 1 política de copias de seguridad habilitada.");
    expect(translate("reporting.readiness.scanned", { count: 3 })).toBe("Se han escaneado 3 equipos");
    expect(Object.keys(es.reporting.fields)).toEqual(Object.keys(en.reporting.fields));
    expect(Object.values(es.enums.ReportKey)).toHaveLength(4);
  });
});
