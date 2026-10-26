/**
 * Service levels in Spanish: the admin page (tiles, settings, the resolver,
 * policies, calendars with their weekly windows and holidays), the dialogs and
 * their refusals, the SLA panel on a ticket's page, the prerequisites strip and
 * the label tables and sentences built outside React. Calendar, policy, queue
 * and holiday names read as recorded; the words around them are Spanish.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { describeSubmitError } from "../../components/form";
import { setActiveLanguage } from "../../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import { ApiError } from "../../lib/api";
import type { ApiClient, Me } from "../../lib/api";
import { expectCta, meWith, renderPanel } from "../../test/prerequisiteHarness";
import type { QueueOut } from "../service-desk/types";
import { describePolicySubject } from "./PoliciesPanel";
import { SlaAdminPage } from "./SlaAdminPage";
import { SlaPrerequisites } from "./SlaPrerequisites";
import { TicketSlaPanel } from "./TicketSlaPanel";
import {
  calendarHealthExplanation,
  calendarHealthLabel,
  describeTargetTiming,
  formatDurationMs,
  formatMinutes,
  resolutionPreview,
  targetStateLabel,
  validateWindows,
  weekdayLabel,
} from "./slaState";
import type { CalendarDetail, CalendarOut, HolidayOut, PolicyOut, SettingsOut, TicketSlaOut } from "./types";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function renderPage(node: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderInSpanish(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/sla"]}>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const ME_READ_ONLY: Me = {
  user_id: "reader-1",
  org_id: "o1",
  email: "lector@example.com",
  display_name: "Lector",
  timezone: "UTC",
  permissions: ["sla.read", "ticket.read"],
};

const ME_ADMIN: Me = {
  ...ME_READ_ONLY,
  user_id: "admin-1",
  display_name: "Admin",
  permissions: ["sla.read", "sla.admin", "ticket.read"],
};

const CALENDARS: CalendarOut[] = [
  { id: "cal1", name: "Horario de oficina", timezone: "Europe/Madrid", active: true, health: "ok", created_at: "2026-01-01T00:00:00.000Z" },
  { id: "cal2", name: "Calendario roto", timezone: "Europe/Madrid", active: false, health: "no_working_windows", created_at: "2026-01-01T00:00:00.000Z" },
];

// A Friday evening shift ending at midnight: the window that must read 22:00–24:00.
const CALENDAR_DETAIL: CalendarDetail = {
  ...(CALENDARS[0] as CalendarOut),
  windows: [
    { id: "w1", weekday: 0, start_time: "09:00:00", end_time: "17:00:00" },
    { id: "w2", weekday: 4, start_time: "22:00:00", end_time: "00:00:00" },
  ],
};

const HOLIDAYS: HolidayOut[] = [{ id: "h1", calendar_id: "cal1", holiday_date: "2026-12-25", name: "Navidad" }];

const QUEUES: QueueOut[] = [
  { id: "q1", key: "soporte", name: "Soporte general", description: "", active: true, sort_order: 0, created_by: "a", created_at: "2026-01-01T00:00:00.000Z" },
  { id: "q2", key: "redes", name: "Redes", description: "", active: false, sort_order: 1, created_by: "a", created_at: "2026-01-01T00:00:00.000Z" },
];

function policy(overrides: Partial<PolicyOut>): PolicyOut {
  return {
    id: "p",
    client_id: null,
    queue_id: null,
    priority: null,
    calendar_id: "cal1",
    response_target_minutes: 60,
    resolution_target_minutes: 480,
    escalate_priority: false,
    escalate_to_queue_id: null,
    active: true,
    created_at: "2026-01-01T00:00:00.000Z",
    ...overrides,
  };
}

const POLICIES: PolicyOut[] = [
  policy({ id: "catch-all" }),
  policy({ id: "soporte-any", queue_id: "q1", response_target_minutes: 30, escalate_priority: true, escalate_to_queue_id: "q2" }),
  policy({ id: "any-urgent", priority: "urgent", response_target_minutes: 15, active: false }),
];

const SETTINGS: SettingsOut = { auto_close_after_days: 7, configured: false, tickets_without_sla: 4, stale_ticket_sla: 2 };

function baseApi(me: Me, settings: SettingsOut = SETTINGS): Partial<ApiClient> {
  return {
    me: vi.fn().mockResolvedValue(me),
    listSlaCalendars: vi.fn().mockResolvedValue(CALENDARS),
    getSlaCalendar: vi.fn().mockResolvedValue(CALENDAR_DETAIL),
    listSlaCalendarHolidays: vi.fn().mockResolvedValue(HOLIDAYS),
    listSlaPolicies: vi.fn().mockResolvedValue(POLICIES),
    getSlaSettings: vi.fn().mockResolvedValue(settings),
    listTicketQueues: vi.fn().mockResolvedValue(QUEUES),
  };
}

function adminApi(): Partial<ApiClient> {
  const api = baseApi(ME_ADMIN);
  api.createSlaCalendar = vi.fn().mockResolvedValue(CALENDARS[0]);
  api.updateSlaCalendar = vi.fn().mockResolvedValue(CALENDAR_DETAIL);
  api.createSlaPolicy = vi.fn().mockResolvedValue(policy({ id: "new" }));
  api.deleteSlaCalendar = vi.fn().mockRejectedValue(new ApiError(503, ""));
  api.updateSlaSettings = vi.fn().mockResolvedValue({ ...SETTINGS, auto_close_after_days: 9, configured: true });
  return api;
}

/** Fixture names the page shows as recorded (calendars, queues, a holiday). */
const RECORDED = ["Europe/Madrid", "Soporte general", "Redes", "Horario de oficina", "Calendario roto", "Navidad"];

// ------------------------------------------------------------ the admin page

describe("the service levels page in Spanish", () => {
  it("words the head, the four tiles, the settings, the resolver, the policies and the calendars", async () => {
    mockUseApi.mockReturnValue(baseApi(ME_READ_ONLY) as ApiClient);
    const { container } = renderPage(<SlaAdminPage />);
    expect(await screen.findByRole("heading", { name: "Niveles de servicio" })).toBeInTheDocument();
    expect(screen.getByText("Objetivos de respuesta y resolución, por prioridad y calendario.")).toBeInTheDocument();
    // Tiles.
    expect(await screen.findByText("1 no puede producir un plazo")).toBeInTheDocument();
    expect(screen.getAllByText("Calendarios laborales")).toHaveLength(2);
    expect(screen.getByText("Políticas activas")).toBeInTheDocument();
    expect(screen.getByText("2 de 3 configuradas")).toBeInTheDocument();
    expect(screen.getByText("Tickets sin SLA")).toBeInTheDocument();
    expect(screen.getByText("Ninguna política coincidió con ellos, o su calendario no puede producir un plazo.")).toBeInTheDocument();
    expect(screen.getByText("Plazos desactualizados")).toBeInTheDocument();
    expect(screen.getByText("No se pudo aplicar a esos tickets un cambio de política o de calendario. El cambio en sí se guardó.")).toBeInTheDocument();
    // Settings.
    expect(screen.getByText("Configuración de SLA")).toBeInTheDocument();
    expect(await screen.findByText("Nunca se ha configurado: esta organización usa el valor predeterminado de 7 días.")).toBeInTheDocument();
    expect(screen.getByText("Para cambiar la configuración de SLA hace falta el permiso de administración de SLA.")).toBeInTheDocument();
    expect(screen.getByText(/^Los tickets con el plazo desactualizado siguen contando el tiempo/)).toBeInTheDocument();
    // Resolver.
    expect(screen.getByText("¿Qué política se aplica?")).toBeInTheDocument();
    expect(screen.getByText("Elige una cola para ver a qué política llegaría un ticket de esa cola.")).toBeInTheDocument();
    // Policies.
    expect(screen.getByText("Políticas de SLA")).toBeInTheDocument();
    const policies = screen.getByText("Políticas de SLA").closest("section") as HTMLElement;
    for (const header of ["Prioridad", "Calendario", "Respuesta", "Resolución", "Al incumplirse", "Activa"]) {
      expect(within(policies).getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(within(policies).getByText("subir la prioridad, mover a Redes")).toBeInTheDocument();
    expect(within(policies).getAllByText("solo notificar").length).toBeGreaterThan(0);
    expect(within(policies).getAllByText("Cualquier cola").length).toBeGreaterThan(0);
    expect(within(policies).getByText("Urgente")).toBeInTheDocument();
    expect(within(policies).getAllByText("Cualquiera").length).toBeGreaterThan(0);
    // Calendars.
    const calendars = screen.getByRole("heading", { name: "Calendarios laborales" }).closest("section") as HTMLElement;
    for (const header of ["Nombre", "Salud", "Activo", "Creado"]) {
      expect(within(calendars).getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(within(calendars).getByText("Correcto")).toBeInTheDocument();
    expect(within(calendars).getAllByText("Sin franjas laborales").length).toBeGreaterThan(0);
    expect(within(calendars).getAllByRole("button", { name: "Horario y festivos" }).length).toBe(2);
    expect(within(calendars).getByText(/^Calendario roto: Este calendario no tiene franjas laborales/)).toBeInTheDocument();
    // A reader gets no mutation controls.
    expect(screen.queryByRole("button", { name: "Añadir calendario" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Añadir política" })).toBeNull();
    expectNoEnglish(container, RECORDED);
  });

  it("agrees the policies' foot with the number of policies, not the number active", async () => {
    mockUseApi.mockReturnValue({
      ...baseApi(ME_READ_ONLY),
      listSlaPolicies: vi.fn().mockResolvedValue([policy({ id: "a" }), policy({ id: "b", active: false }), policy({ id: "c", active: false })]),
    } as unknown as ApiClient);
    renderPage(<SlaAdminPage />);
    expect(await screen.findByText("1 de 3 configuradas")).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("1 of 3 configured")).toBeInTheDocument();
  });

  it("says what a measured zero means, and that nothing is measured yet", async () => {
    mockUseApi.mockReturnValue({
      ...baseApi(ME_READ_ONLY, { ...SETTINGS, tickets_without_sla: 0, stale_ticket_sla: 0, configured: true }),
      listSlaCalendars: vi.fn().mockResolvedValue([]),
      listSlaPolicies: vi.fn().mockResolvedValue([]),
    } as unknown as ApiClient);
    const { container } = renderPage(<SlaAdminPage />);
    expect(await screen.findByText("Aún no hay ningún calendario laboral definido")).toBeInTheDocument();
    expect(screen.getByText("Ninguna política está activa, así que no se mide nada")).toBeInTheDocument();
    expect(screen.getByText("Todos los tickets tienen SLA")).toBeInTheDocument();
    expect(screen.getByText("Todos los plazos coinciden con su política")).toBeInTheDocument();
    expect(screen.getByText("Todavía no hay calendarios: una política necesita uno para que un ticket pueda tener SLA.")).toBeInTheDocument();
    expect(screen.getByText("Todavía no hay políticas: sin ninguna configurada, ningún ticket tiene SLA.")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("offers the mutation controls to an administrator and words the settings form", async () => {
    mockUseApi.mockReturnValue(adminApi() as ApiClient);
    const { container } = renderPage(<SlaAdminPage />);
    expect(await screen.findByRole("button", { name: "Añadir calendario" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Añadir política" })).toBeInTheDocument();
    expect(await screen.findByLabelText("Cerrar automáticamente los tickets resueltos después de (días)")).toHaveValue("7");
    expect(screen.getByRole("button", { name: "Guardar configuración" })).toBeEnabled();
    expectNoEnglish(container, RECORDED);
  });

  it("words the resolver's answer, the four levels and which are shadowed", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(baseApi(ME_READ_ONLY) as ApiClient);
    const { container } = renderPage(<SlaAdminPage />);
    const panel = (await screen.findByText("¿Qué política se aplica?")).closest("section") as HTMLElement;
    await within(panel).findByRole("option", { name: "Soporte general" });
    await user.selectOptions(within(panel).getByLabelText("Cola"), "q1");
    expect(within(panel).getByRole("option", { name: "Urgente" })).toBeInTheDocument();
    expect(within(panel).getByText("Se resuelve en el nivel 2 (esta cola + cualquier prioridad): respuesta 30 min, resolución 8 h, en el calendario Horario de oficina.")).toBeInTheDocument();
    for (const header of ["Nivel", "Coincide por", "Política", "Resultado"]) {
      expect(within(panel).getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(within(panel).getByText("Se aplica")).toBeInTheDocument();
    expect(within(panel).getAllByText("Desplazada por un nivel más específico").length).toBeGreaterThan(0);
    expect(within(panel).getAllByText("No hay ninguna política activa en este nivel").length).toBeGreaterThan(0);
    expect(within(panel).getByText("esta cola + esta prioridad")).toBeInTheDocument();
    expect(within(panel).getAllByText("30 min / 8 h en Horario de oficina").length).toBe(1);
    expectNoEnglish(container, RECORDED);
  });

  it("says no policy matches a queue and priority nothing covers", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue({ ...baseApi(ME_READ_ONLY), listSlaPolicies: vi.fn().mockResolvedValue([]) } as unknown as ApiClient);
    const { container } = renderPage(<SlaAdminPage />);
    const panel = (await screen.findByText("¿Qué política se aplica?")).closest("section") as HTMLElement;
    await within(panel).findByRole("option", { name: "Soporte general" });
    await user.selectOptions(within(panel).getByLabelText("Cola"), "q1");
    expect(
      within(panel).getByText("Ninguna política coincide con Soporte general + Normal. Un ticket creado con esta combinación se queda sin SLA: ni uno predeterminado ni uno en cero."),
    ).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
    act(() => setActiveLanguage("en"));
    expect(
      within(panel).getByText("No policy matches Soporte general + Normal. A ticket filed with this combination gets no SLA at all: not a default and not a zero."),
    ).toBeInTheDocument();
  });

  it("says what is missing when the policy list cannot be read", async () => {
    mockUseApi.mockReturnValue({
      ...baseApi(ME_READ_ONLY),
      listSlaPolicies: vi.fn().mockRejectedValue(new ApiError(403, "forbidden")),
      listSlaCalendars: vi.fn().mockRejectedValue(new ApiError(503, "")),
      getSlaSettings: vi.fn().mockRejectedValue(new ApiError(403, "forbidden")),
    } as unknown as ApiClient);
    const { container } = renderPage(<SlaAdminPage />);
    expect(await screen.findAllByText("Las políticas no están disponibles (permisos insuficientes).")).toHaveLength(2);
    expect(screen.getByText("La configuración no está disponible (permisos insuficientes).")).toBeInTheDocument();
    expect(await screen.findByText("No se pudieron cargar los calendarios. Ese servicio no está disponible en este momento. No se cambió nada: inténtalo de nuevo en breve.")).toBeInTheDocument();
    expectNoEnglish(container);
  });
});

// ------------------------------------------------------- weekly windows, days

describe("a calendar's hours and holidays in Spanish", () => {
  async function openHours(api: Partial<ApiClient>) {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api as ApiClient);
    const view = renderPage(<SlaAdminPage />);
    const openers = await screen.findAllByRole("button", { name: "Horario y festivos" });
    await user.click(openers[0] as HTMLElement);
    await screen.findByText("Horario laboral");
    return { user, ...view };
  }

  it("words the week grid, the midnight window and the holidays", async () => {
    const { container } = await openHours(adminApi());
    for (const day of ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]) {
      expect(screen.getAllByText(day).length).toBeGreaterThan(0);
    }
    expect(screen.getByText("10 h de tiempo laboral por semana")).toBeInTheDocument();
    expect(screen.getAllByText("Día no laborable").length).toBe(5);
    expect(screen.getByText("termina a medianoche (24:00)")).toBeInTheDocument();
    expect(screen.getByText("22:00–24:00 · 2 h")).toBeInTheDocument();
    expect(screen.getByLabelText("Lunes, franja 1, inicio")).toHaveValue("09:00");
    expect(screen.getByLabelText("Viernes, franja 1, fin")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Añadir franja (lunes)" })).toHaveTextContent("Añadir franja");
    expect(screen.getByRole("button", { name: "Añadir día de 24 horas (martes)" })).toHaveTextContent("Día de 24 horas");
    expect(screen.getByRole("button", { name: "Guardar semana" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Revertir" })).toBeInTheDocument();
    expect(screen.getByText("Al guardar se reemplaza la semana completa y se recalculan todos los plazos en curso de este calendario.")).toBeInTheDocument();
    // Holidays.
    expect(await screen.findByText("Festivos")).toBeInTheDocument();
    expect(screen.getByText("Un festivo elimina por completo las franjas laborales de esa fecha; una media jornada se expresa no marcando el día.")).toBeInTheDocument();
    expect(await screen.findByText("Navidad")).toBeInTheDocument();
    expect(screen.getByLabelText("Fecha del festivo")).toBeInTheDocument();
    expect(screen.getByLabelText("Nombre del festivo")).toHaveAttribute("placeholder", "Navidad");
    expect(screen.getByRole("button", { name: "Añadir festivo" })).toBeEnabled();
    expectNoEnglish(container, [...RECORDED]);
  });

  it("says a calendar with no windows cannot produce a deadline, and an unsaved edit", async () => {
    const api = adminApi();
    api.getSlaCalendar = vi.fn().mockResolvedValue({ ...CALENDAR_DETAIL, windows: [] });
    const { user, container } = await openHours(api);
    expect(screen.getByText(/^No hay franjas laborales\. Un calendario sin tiempo laboral/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Añadir franja (lunes)" }));
    expect(screen.getByText("Cambios sin guardar")).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });

  it("refuses an inverted window in Spanish and re-words it on a switch", async () => {
    const { user } = await openHours(adminApi());
    const end = screen.getByLabelText("Lunes, franja 1, fin");
    await user.clear(end);
    await user.type(end, "08:00");
    expect(screen.getByText(/^Lunes: 09:00 no es anterior a 08:00 \(un fin a las 00:00 significa/)).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByText(/^Monday: 09:00 is not before 08:00 \(end 00:00 means midnight/)).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    expect(screen.getByText(/^Lunes: 09:00 no es anterior a 08:00/)).toBeInTheDocument();
  });

  it("says the week was not saved, holding the failure and not its sentence", async () => {
    const api = adminApi();
    api.replaceSlaCalendarWindows = vi.fn().mockRejectedValue(new ApiError(503, ""));
    const { user, container } = await openHours(api);
    await user.click(screen.getByRole("button", { name: "Añadir franja (martes)" }));
    await user.click(screen.getByRole("button", { name: "Guardar semana" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/^No se guardó la semana\. Ese servicio no está disponible/);
    expectNoEnglish(container, RECORDED);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent(/^The week was not saved\. That service is unavailable/);
    act(() => setActiveLanguage("es"));
    expect(screen.getByRole("alert")).toHaveTextContent(/^No se guardó la semana\./);
  });

  it("refuses an empty holiday and a badly shaped date, and re-words both on a switch", async () => {
    const { user } = await openHours(adminApi());
    await screen.findByText("Navidad");
    await user.click(screen.getByRole("button", { name: "Añadir festivo" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("El campo Fecha del festivo es obligatorio. El campo Nombre del festivo es obligatorio.");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent("Holiday date is required. Holiday name is required.");
    act(() => setActiveLanguage("es"));
    expect(screen.getByRole("alert")).toHaveTextContent("El campo Fecha del festivo es obligatorio.");
  });

  it("confirms a holiday's removal in Spanish and words a failure", async () => {
    const api = adminApi();
    api.removeSlaCalendarHoliday = vi.fn().mockRejectedValue(new ApiError(503, ""));
    const { user } = await openHours(api);
    await screen.findByText("Navidad");
    const holidays = (await screen.findByText("Festivos")).parentElement as HTMLElement;
    await user.click(within(holidays).getByRole("button", { name: "Quitar" }));
    const dialog = await screen.findByRole("dialog", { name: "Quitar este festivo" });
    expect(within(dialog).getByText(/^Esta fecha vuelve a ser un día laborable/)).toBeInTheDocument();
    expectNoEnglish(dialog, ["Navidad"]);
    await user.click(within(dialog).getByRole("button", { name: "Quitar festivo" }));
    expect(await screen.findByText(/^No se pudo quitar: Ese servicio no está disponible/)).toBeInTheDocument();
  });
});

// ----------------------------------------------------------------- the dialogs

describe("the calendar and policy dialogs in Spanish", () => {
  async function openDialog(opener: string, title: string) {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(adminApi() as ApiClient);
    const view = renderPage(<SlaAdminPage />);
    await user.click(await screen.findByRole("button", { name: opener }));
    const dialog = await screen.findByRole("dialog", { name: title });
    return { user, dialog, ...view };
  }

  it("words the Add calendar dialog and refuses an empty name", async () => {
    const { user, dialog } = await openDialog("Añadir calendario", "Añadir calendario");
    expect(within(dialog).getByLabelText("Zona horaria (nombre IANA)")).toHaveValue("Europe/London");
    expect(within(dialog).getByText("Este es un valor real y se guardará tal como lo escribas: Europe/London es solo un punto de partida. Escribe para buscar en la lista IANA.")).toBeInTheDocument();
    expect(within(dialog).getByRole("checkbox", { name: "Activo" })).toBeChecked();
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("El campo Nombre es obligatorio.");
    expectNoEnglish(dialog, ["Europe/London"]);
    // The refusal is held as data: it is re-worded on a switch.
    act(() => setActiveLanguage("en"));
    expect(within(dialog).getByRole("alert")).toHaveTextContent("Name is required.");
    act(() => setActiveLanguage("es"));
    expect(within(dialog).getByRole("alert")).toHaveTextContent("El campo Nombre es obligatorio.");
  });

  it("rejects a timezone that is not an IANA name, in Spanish and in English", async () => {
    const { user, dialog } = await openDialog("Añadir calendario", "Añadir calendario");
    await user.type(within(dialog).getByLabelText("Nombre"), "Nocturno");
    const zone = within(dialog).getByLabelText("Zona horaria (nombre IANA)");
    await user.clear(zone);
    await user.type(zone, "Londres");
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent('La zona horaria debe ser un nombre IANA, como Europe/London: "Londres" no lo es.');
    act(() => setActiveLanguage("en"));
    expect(within(dialog).getByRole("alert")).toHaveTextContent('Timezone must be an IANA name such as Europe/London — "Londres" is not one.');
    act(() => setActiveLanguage("es"));
    await user.clear(zone);
    await user.type(zone, "Europe/Nowhere");
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent('No existe ninguna zona horaria llamada "Europe/Nowhere": revisa la ortografía con la lista IANA.');
  });

  it("words the Add policy dialog, its hints, and refuses a missing calendar and a zero target", async () => {
    const { user, dialog } = await openDialog("Añadir política", "Añadir política");
    expect(within(dialog).getByRole("combobox", { name: "Cola (en blanco = cualquier cola)" })).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Prioridad (en blanco = cualquier prioridad)")).toBeInTheDocument();
    for (const option of ["Cualquier prioridad", "Baja", "Normal", "Alta", "Urgente"]) {
      expect(within(dialog).getByRole("option", { name: option })).toBeInTheDocument();
    }
    expect(within(dialog).getByLabelText("Objetivo de respuesta en minutos (1 h)")).toHaveValue(60);
    expect(within(dialog).getByLabelText("Objetivo de resolución en minutos (8 h)")).toHaveValue(480);
    expect(within(dialog).getByRole("checkbox", { name: "Subir la prioridad al incumplirse" })).not.toBeChecked();
    expect(within(dialog).getByRole("combobox", { name: "Escalar a la cola al incumplirse (en blanco = seguir en la cola)" })).toBeInTheDocument();
    expect(within(dialog).getByText(/^Una escalada por incumplimiento no recalcula el plazo a propósito/)).toBeInTheDocument();
    expect(within(dialog).getByRole("checkbox", { name: "Activa (durante la resolución solo se tienen en cuenta las políticas activas)" })).toBeChecked();
    const response = within(dialog).getByLabelText("Objetivo de respuesta en minutos (1 h)");
    await user.clear(response);
    await user.type(response, "0");
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("El campo Calendario es obligatorio.");
    expect(alert).toHaveTextContent("El campo Objetivo de respuesta en minutos debe ser de al menos 1 minuto: un objetivo de cero minutos se incumple al crearse el ticket.");
    expectNoEnglish(dialog);
    act(() => setActiveLanguage("en"));
    expect(within(dialog).getByRole("alert")).toHaveTextContent("Calendar is required. Response target in minutes must be at least 1 minute — a zero-minute target is breached on creation.");
    act(() => setActiveLanguage("es"));
    expect(within(dialog).getByRole("alert")).toHaveTextContent("El campo Calendario es obligatorio.");
  });

  it("refuses a negative and a fractional target in Spanish", async () => {
    const { user, dialog } = await openDialog("Añadir política", "Añadir política");
    const response = within(dialog).getByLabelText("Objetivo de respuesta en minutos (1 h)");
    await user.clear(response);
    await user.type(response, "-5");
    const resolution = within(dialog).getByLabelText("Objetivo de resolución en minutos (8 h)");
    await user.clear(resolution);
    await user.type(resolution, "2.5");
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("El campo Objetivo de respuesta en minutos no puede ser negativo: un plazo anterior a la creación del ticket no es un compromiso");
    expect(alert).toHaveTextContent("El campo Objetivo de resolución en minutos debe ser un número entero de minutos");
  });

  it("says why the calendar list is empty and offers the way out", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue({ ...adminApi(), listSlaCalendars: vi.fn().mockResolvedValue([]) } as unknown as ApiClient);
    renderPage(<SlaAdminPage />);
    await user.click(await screen.findByRole("button", { name: "Añadir política" }));
    const dialog = await screen.findByRole("dialog", { name: "Añadir política" });
    expect(await within(dialog).findByText(/^Esta organización no tiene calendarios laborales, así que todavía no se puede guardar ninguna política\./)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Añadir un calendario laboral" })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("combobox", { name: "Calendario" }));
    expect(await within(dialog).findByText(/^Todavía no hay calendarios laborales\. Un calendario es lo que convierte/)).toBeInTheDocument();
    expectNoEnglish(dialog);
  });

  it("names the queues' limits when the queue read is refused", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue({ ...adminApi(), listTicketQueues: vi.fn().mockRejectedValue(new ApiError(403, "forbidden")) } as unknown as ApiClient);
    renderPage(<SlaAdminPage />);
    await user.click(await screen.findByRole("button", { name: "Añadir política" }));
    const dialog = await screen.findByRole("dialog", { name: "Añadir política" });
    expect(await within(dialog).findByText(/^Para ver los nombres de las colas hace falta permiso para ver tickets/)).toBeInTheDocument();
    expectNoEnglish(dialog);
  });

  it("words the Edit calendar dialog and the Edit policy dialog", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(adminApi() as ApiClient);
    renderPage(<SlaAdminPage />);
    const calendars = (await screen.findByRole("columnheader", { name: "Salud" })).closest("table") as HTMLElement;
    await user.click(within(calendars).getAllByRole("button", { name: "Editar" })[0] as HTMLElement);
    const calendarDialog = await screen.findByRole("dialog", { name: "Editar calendario" });
    expect(within(calendarDialog).getByText("Cambiar la zona horaria recalcula todos los plazos en curso de este calendario.")).toBeInTheDocument();
    await user.click(within(calendarDialog).getByRole("button", { name: "Cancelar" }));
    const policies = (await screen.findByText("Políticas de SLA")).closest("section") as HTMLElement;
    await user.click(within(policies).getAllByRole("button", { name: "Editar" })[0] as HTMLElement);
    const policyDialog = await screen.findByRole("dialog", { name: "Editar política" });
    expect(within(policyDialog).getByText("Cambiar un objetivo o el calendario recalcula todos los plazos en curso de esta política.")).toBeInTheDocument();
    expectNoEnglish(policyDialog, RECORDED);
  });
});

// ---------------------------------------------------------- the settings form

describe("the settings form in Spanish", () => {
  it("refuses a value out of range and re-words the refusal on a switch", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(adminApi() as ApiClient);
    renderPage(<SlaAdminPage />);
    const field = await screen.findByLabelText("Cerrar automáticamente los tickets resueltos después de (días)");
    await user.clear(field);
    await user.type(field, "99999");
    await user.click(screen.getByRole("button", { name: "Guardar configuración" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("El plazo de cierre automático debe estar entre 1 y 3650 días");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent("Auto-close window must be between 1 and 3650 days");
    act(() => setActiveLanguage("es"));
    await user.clear(field);
    await user.type(field, "abc");
    await user.click(screen.getByRole("button", { name: "Guardar configuración" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("El plazo de cierre automático debe ser un número entero de días");
  });

  it("confirms a save and words a refusal of the server", async () => {
    const user = userEvent.setup();
    const api = adminApi();
    mockUseApi.mockReturnValue(api as ApiClient);
    renderPage(<SlaAdminPage />);
    const field = await screen.findByLabelText("Cerrar automáticamente los tickets resueltos después de (días)");
    await user.clear(field);
    await user.type(field, "9");
    await user.click(screen.getByRole("button", { name: "Guardar configuración" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Guardado. El valor de arriba es el que devolvió el servidor.");
  });

  it("holds a failed save as the failure and re-words it on a switch", async () => {
    const user = userEvent.setup();
    const api = adminApi();
    api.updateSlaSettings = vi.fn().mockRejectedValue(new ApiError(503, ""));
    mockUseApi.mockReturnValue(api as ApiClient);
    renderPage(<SlaAdminPage />);
    await screen.findByLabelText("Cerrar automáticamente los tickets resueltos después de (días)");
    await user.click(screen.getByRole("button", { name: "Guardar configuración" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/^No se guardó la configuración\. Ese servicio no está disponible/);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent(/^Settings were not saved\. That service is unavailable/);
  });

  it("words a failed calendar delete and re-words it on a switch", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(adminApi() as ApiClient);
    renderPage(<SlaAdminPage />);
    const table = (await screen.findByRole("columnheader", { name: "Salud" })).closest("table") as HTMLElement;
    await user.click(within(table).getAllByRole("button", { name: "Eliminar" })[0] as HTMLElement);
    const dialog = await screen.findByRole("dialog", { name: "Eliminar el calendario laboral" });
    expect(within(dialog).getByText(/^Sus franjas laborales y sus festivos se eliminan con él\./)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Eliminar calendario" }));
    expect(await screen.findByText(/^No se pudo eliminar: Ese servicio no está disponible/)).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByText(/^Delete failed: That service is unavailable/)).toBeInTheDocument();
  });

  it("words the confirmation of a policy delete, naming the policy", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(adminApi() as ApiClient);
    renderPage(<SlaAdminPage />);
    const policies = (await screen.findByText("Políticas de SLA")).closest("section") as HTMLElement;
    await within(policies).findByText("Soporte general");
    await user.click(within(policies).getAllByRole("button", { name: "Eliminar" })[1] as HTMLElement);
    const dialog = await screen.findByRole("dialog", { name: "Eliminar la política de SLA" });
    expect(within(dialog).getByText("Soporte general / Cualquier prioridad")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Eliminar política" })).toBeInTheDocument();
    expectNoEnglish(dialog, ["Soporte general"]);
  });
});

// ------------------------------------------------------ the ticket's SLA panel

const HOUR = 3_600_000;
const fromNow = (ms: number): string => new Date(Date.now() + ms).toISOString();

function ticketSla(overrides: Partial<TicketSlaOut>): TicketSlaOut {
  return {
    ticket_id: "t1",
    has_sla: true,
    response_state: "running",
    resolution_state: "running",
    policy_id: "p1",
    calendar_id: "cal1",
    response_due_at: null,
    resolution_due_at: null,
    response_met_at: null,
    resolution_met_at: null,
    response_breached_at: null,
    resolution_breached_at: null,
    paused_at: null,
    paused_minutes_total: 0,
    voided_at: null,
    escalated_at: null,
    policy_source: "initial",
    ...overrides,
  };
}

function renderTicketSla(state: TicketSlaOut, me: Me = ME_READ_ONLY) {
  mockUseApi.mockReturnValue({
    getTicketSla: vi.fn().mockResolvedValue(state),
    me: vi.fn().mockResolvedValue(me),
  } as unknown as ApiClient);
  return renderPage(<TicketSlaPanel ticketId="t1" />);
}

describe("the ticket's SLA panel in Spanish", () => {
  it("counts a running clock down and words the attribution facts", async () => {
    const { container } = renderTicketSla(
      ticketSla({
        response_due_at: fromNow(2 * HOUR + 30_000),
        resolution_due_at: fromNow(8 * HOUR + 30_000),
        policy_name: "Prioridad alta en cualquier cola",
        calendar_name: "Horario de oficina",
      }),
    );
    expect(await screen.findByText("Tiempo restante: 2 h")).toBeInTheDocument();
    expect(screen.getByText("Tiempo restante: 8 h")).toBeInTheDocument();
    expect(screen.getAllByText("En marcha")).toHaveLength(2);
    expect(screen.getAllByText(/^Vencimiento: /).length).toBe(2);
    expect(screen.getByText("SLA")).toBeInTheDocument();
    expect(screen.getByText("Respuesta")).toBeInTheDocument();
    expect(screen.getByText("Resolución")).toBeInTheDocument();
    expect(screen.getByText("Motivo de la política aplicada")).toBeInTheDocument();
    expect(screen.getByText("Coincidió cuando se creó el ticket")).toBeInTheDocument();
    expect(screen.getByText("Escalado")).toBeInTheDocument();
    expect(screen.getByText("No")).toBeInTheDocument();
    expect(screen.getByText("Horario laboral")).toBeInTheDocument();
    expectNoEnglish(container, ["Prioridad alta en cualquier cola", "Horario de oficina"]);
  });

  it("says a running clock is past due without claiming a breach", async () => {
    const { container } = renderTicketSla(ticketSla({ response_due_at: fromNow(-HOUR - 30_000) }));
    expect(await screen.findByText("1 h de retraso")).toBeInTheDocument();
    expect(screen.getByText(/el servidor aún no ha registrado un incumplimiento\.$/)).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("words a paused clock, a voided promise, a late answer and a met target", async () => {
    const paused = renderTicketSla(
      ticketSla({
        response_state: "paused",
        resolution_state: "paused",
        response_due_at: fromNow(2 * HOUR),
        resolution_due_at: fromNow(8 * HOUR),
        paused_at: fromNow(-3 * HOUR - 30_000),
        paused_minutes_total: 45,
      }),
    );
    expect(await screen.findAllByText("En pausa desde hace 3 h: el reloj está detenido")).toHaveLength(2);
    expect(screen.getAllByText("En pausa")).toHaveLength(2);
    expect(screen.getAllByText(/ya se compensaron 45 min de pausas anteriores\.$/)).toHaveLength(2);
    expect(screen.getAllByText(/^En pausa desde el .+; el plazo del .+ se amplía con el tiempo laboral en pausa cuando el ticket se reanude;/)).toHaveLength(2);
    expectNoEnglish(paused.container);
    paused.unmount();

    const voided = renderTicketSla(
      ticketSla({ response_state: "void", resolution_state: "void", response_due_at: fromNow(-5 * HOUR), voided_at: fromNow(-HOUR) }),
    );
    expect(await screen.findAllByText("Compromiso anulado: ni cumplido ni incumplido")).toHaveLength(2);
    expect(screen.getAllByText("Anulado")).toHaveLength(2);
    expectNoEnglish(voided.container);
    voided.unmount();

    const late = renderTicketSla(
      ticketSla({
        response_state: "breached",
        response_due_at: fromNow(-4 * HOUR),
        response_breached_at: fromNow(-3 * HOUR - 30_000),
        response_met_at: fromNow(-HOUR),
        resolution_state: "met",
        resolution_due_at: fromNow(HOUR),
        resolution_met_at: fromNow(-2 * HOUR - 30_000),
        policy_source: "retriage",
      }),
    );
    expect(await screen.findByText("Incumplido hace 3 h")).toBeInTheDocument();
    expect(screen.getByText(/^El plazo era el .+; se respondió el .+, después del plazo\.$/)).toBeInTheDocument();
    expect(screen.getByText("Cumplido hace 2 h")).toBeInTheDocument();
    expect(screen.getByText(/^Cumplido el .+, 3 h antes del plazo del .+\.$/)).toBeInTheDocument();
    expect(screen.getByText("Una persona cambió la prioridad o la cola")).toBeInTheDocument();
    expect(screen.getByText("El plazo se recalculó desde el inicio del ticket.")).toBeInTheDocument();
    expectNoEnglish(late.container);
  });

  it("words an escalation, an unknown policy source and the stamp", async () => {
    const { container } = renderTicketSla(
      ticketSla({ policy_source: "escalation", escalated_at: fromNow(-HOUR), response_due_at: fromNow(HOUR + 30_000), resolution_due_at: fromNow(HOUR + 30_000) }),
    );
    expect(await screen.findByText("La escalada por incumplimiento la estableció")).toBeInTheDocument();
    expect(screen.getByText("La escalada no recalcula el plazo a propósito.")).toBeInTheDocument();
    expect(screen.queryByText("No")).toBeNull();
    expectNoEnglish(container);
  });

  it("does not print a policy source the console has not met raw", async () => {
    renderTicketSla(ticketSla({ policy_source: "manual_override" }));
    expect(await screen.findByText("Manual override")).toBeInTheDocument();
  });

  it("renders 'no SLA' as a state, with the way to fix it for those who can", async () => {
    const { container } = renderTicketSla({ ticket_id: "t1", has_sla: false, response_state: "none", resolution_state: "none" });
    expect(await screen.findByText("Sin SLA")).toBeInTheDocument();
    expect(screen.getByText(/^Ninguna política coincidió con este ticket, o el calendario de su política/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Definir objetivos de respuesta" })).toHaveAttribute("href", "/sla");
    expectNoEnglish(container);
  });

  it("tells someone who cannot open Service levels who can", async () => {
    const { container } = renderTicketSla(
      { ticket_id: "t1", has_sla: false, response_state: "none", resolution_state: "none" },
      { ...ME_READ_ONLY, permissions: ["ticket.read"] },
    );
    expect(await screen.findByText("Los objetivos de respuesta y resolución los define un administrador en Niveles de servicio.")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("degrades quietly when the read is refused, and says it failed on another error", async () => {
    mockUseApi.mockReturnValue({ getTicketSla: vi.fn().mockRejectedValue(new ApiError(403, "forbidden")), me: vi.fn().mockResolvedValue(ME_READ_ONLY) } as unknown as ApiClient);
    const forbidden = renderPage(<TicketSlaPanel ticketId="t1" />);
    expect(await screen.findByText("El estado del SLA no está disponible (permisos insuficientes).")).toBeInTheDocument();
    expectNoEnglish(forbidden.container);
    forbidden.unmount();

    mockUseApi.mockReturnValue({ getTicketSla: vi.fn().mockRejectedValue(new ApiError(503, "")), me: vi.fn().mockResolvedValue(ME_READ_ONLY) } as unknown as ApiClient);
    const failed = renderPage(<TicketSlaPanel ticketId="t1" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Ese servicio no está disponible en este momento. No se cambió nada: inténtalo de nuevo en breve.");
    expectNoEnglish(failed.container);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent("That service is unavailable right now.");
  });

  it("re-words the clock, the pill and the facts on a switch", async () => {
    renderTicketSla(
      ticketSla({
        response_due_at: fromNow(2 * HOUR + 30_000),
        resolution_due_at: fromNow(8 * HOUR + 30_000),
        policy_source: "retriage",
      }),
    );
    expect(await screen.findByText("Tiempo restante: 2 h")).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("2h remaining")).toBeInTheDocument();
    expect(screen.getAllByText("Running")).toHaveLength(2);
    expect(screen.getByText("Policy applied because")).toBeInTheDocument();
    expect(screen.getByText("A person changed the priority or queue")).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    expect(screen.getByText("Tiempo restante: 2 h")).toBeInTheDocument();
    expect(screen.getByText("Una persona cambió la prioridad o la cola")).toBeInTheDocument();
  });
});

// ----------------------------------------------------------- the prerequisites

describe("the prerequisites strip in Spanish", () => {
  function panelApi(queues: unknown, permissions = ["sla.read", "ticket.read"]) {
    mockUseApi.mockReturnValue({
      me: meWith(permissions),
      listTicketQueues: typeof queues === "function" ? queues : vi.fn().mockResolvedValue(queues),
    } as unknown as ApiClient);
  }

  it("says no queue exists and links to where queues are made", async () => {
    panelApi([]);
    setActiveLanguage("es");
    const { container } = renderPanel(<SlaPrerequisites />);
    expect(await screen.findByText("Antes de que un objetivo pueda coincidir con una cola")).toBeInTheDocument();
    expect(screen.getByText("Lo que necesita la mesa de ayuda para que los objetivos se apliquen a una parte de ella.")).toBeInTheDocument();
    expect(screen.getByText("Existe una cola de tickets")).toBeInTheDocument();
    expect(screen.getByText(/^Todavía no existe ninguna cola de tickets, así que un objetivo solo puede coincidir/)).toBeInTheDocument();
    await expectCta("Configurar colas", "/service-desk/admin");
    expectNoEnglish(container);
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("Before a target can match a queue")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Set up queues" })).toBeInTheDocument();
  });

  it("says it cannot be checked from an account without the ticket permission", async () => {
    panelApi(vi.fn().mockRejectedValue(new ApiError(403, "{}")), ["sla.read"]);
    setActiveLanguage("es");
    const { container } = renderPanel(<SlaPrerequisites />);
    expect(await screen.findByText(/No se puede comprobar desde esta cuenta/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Configurar colas" })).toBeNull();
    expectNoEnglish(container);
  });
});

// ------------------------------------------------- label tables and sentences

describe("the SLA label tables and sentences", () => {
  it("words calendar health and the target states, and falls back for an unknown value", () => {
    setActiveLanguage("es");
    expect(calendarHealthLabel("ok")).toBe("Correcto");
    expect(calendarHealthLabel("no_working_windows")).toBe("Sin franjas laborales");
    expect(calendarHealthLabel("invalid_timezone")).toBe("Zona horaria no válida");
    expect(calendarHealthExplanation("invalid_timezone")).toMatch(/^La zona horaria de este calendario no se reconoce/);
    expect(calendarHealthExplanation("ok")).toBeNull();
    expect(targetStateLabel("none")).toBe("Sin SLA");
    expect(targetStateLabel("void")).toBe("Anulado");
    expect(targetStateLabel("breached")).toBe("Incumplido");
    expect(targetStateLabel("met")).toBe("Cumplido");
    expect(targetStateLabel("paused")).toBe("En pausa");
    expect(targetStateLabel("running")).toBe("En marcha");
    // A value the console has not met is humanised, never printed raw.
    expect(targetStateLabel("on_hold" as never)).toBe("On hold");
    expect(calendarHealthLabel("clock_skew" as never)).toBe("Clock skew");
    setActiveLanguage("en");
    expect(calendarHealthLabel("no_working_windows")).toBe("No working windows");
    expect(targetStateLabel("met")).toBe("Met");
  });

  it("words the days, the durations and the problems of a week", () => {
    setActiveLanguage("es");
    expect([0, 1, 2, 3, 4, 5, 6].map(weekdayLabel)).toEqual(["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]);
    expect(weekdayLabel(9)).toBe("día de la semana 9");
    expect(formatMinutes(0)).toBe("0 min");
    expect(formatMinutes(90)).toBe("1 h 30 min");
    expect(formatMinutes(2 * 1440 + 4 * 60 + 30)).toBe("2 d 4 h");
    expect(formatDurationMs(1000)).toBe("menos de un minuto");
    expect(validateWindows([{ weekday: 9, start_time: "09:00", end_time: "17:00" }])).toEqual(["el día de la semana 9 está fuera del rango 0-6 (lunes=0)"]);
    expect(validateWindows([{ weekday: 1, start_time: "9am", end_time: "17:00" }])).toEqual(["Martes: las horas deben tener el formato HH:MM"]);
    expect(
      validateWindows([
        { weekday: 2, start_time: "09:00", end_time: "13:00" },
        { weekday: 2, start_time: "12:00", end_time: "17:00" },
      ]),
    ).toEqual(["Miércoles: hay franjas que se solapan"]);
    setActiveLanguage("en");
    expect(weekdayLabel(0)).toBe("Monday");
    expect(formatMinutes(90)).toBe("1h 30m");
  });

  it("words the four resolution levels, and re-words them on a switch", () => {
    setActiveLanguage("es");
    const labels = resolutionPreview([], "q1", "urgent", null).levels.map((level) => level.label);
    expect(labels).toEqual(["esta cola + esta prioridad", "esta cola + cualquier prioridad", "cualquier cola + esta prioridad", "cualquier cola + cualquier prioridad"]);
    setActiveLanguage("en");
    expect(resolutionPreview([], "q1", "urgent", null).levels[0]?.label).toBe("this queue + this priority");
  });

  it("words a policy for its confirmation", () => {
    setActiveLanguage("es");
    const queueName = (id: string | null) => (id === null ? "Cualquier cola" : "Soporte general");
    expect(describePolicySubject({ queue_id: "q1", priority: "urgent" }, queueName)).toBe("Soporte general / Urgente");
    expect(describePolicySubject({ queue_id: null, priority: null }, queueName)).toBe("Cualquier cola / Cualquier prioridad");
    setActiveLanguage("en");
    expect(describePolicySubject({ queue_id: "q1", priority: "urgent" }, queueName)).toBe("Soporte general / urgent");
  });

  it("words a target's timing and re-words it on a switch", () => {
    const now = Date.parse("2026-08-02T13:00:00Z");
    const facts = (over: Partial<Parameters<typeof describeTargetTiming>[0]>) => ({
      state: "running" as const,
      dueAt: null,
      metAt: null,
      breachedAt: null,
      pausedAt: null,
      pausedMinutesTotal: null,
      voidedAt: null,
      ...over,
    });
    setActiveLanguage("es");
    expect(describeTargetTiming(facts({ state: "none" }), now, "UTC").headline).toBe("Sin objetivo de SLA");
    expect(describeTargetTiming(facts({ state: "running" }), now, "UTC").headline).toBe("En marcha");
    expect(describeTargetTiming(facts({ state: "paused" }), now, "UTC").headline).toBe("En pausa: el reloj está detenido");
    expect(describeTargetTiming(facts({ state: "breached" }), now, "UTC").headline).toBe("Incumplido");
    expect(describeTargetTiming(facts({ state: "met" }), now, "UTC").headline).toBe("Cumplido");
    expect(describeTargetTiming(facts({ state: "void" }), now, "UTC").detail).toBe(
      "El ticket llegó a un estado final sin resolverse, así que este plazo ya no se aplica.",
    );
    const met = describeTargetTiming(facts({ state: "met", dueAt: "2026-08-02T12:00:00Z", metAt: "2026-08-02T10:00:00Z" }), now, "UTC");
    expect(met.headline).toBe("Cumplido hace 3 h");
    expect(met.detail).toMatch(/^Cumplido el .+, 2 h antes del plazo del .+\.$/);
    const late = describeTargetTiming(facts({ state: "met", dueAt: "2026-08-02T10:00:00Z", metAt: "2026-08-02T12:00:00Z" }), now, "UTC");
    expect(late.detail).toMatch(/^Cumplido el .+, 2 h después del plazo del .+\.$/);
    expect(describeTargetTiming(facts({ state: "met", metAt: "2026-08-02T12:00:00Z" }), now, "UTC").detail).toMatch(/^Cumplido el .+\.$/);
    setActiveLanguage("en");
    expect(met.headline).toBe("Cumplido hace 3 h");
    expect(describeTargetTiming(facts({ state: "met", dueAt: "2026-08-02T12:00:00Z", metAt: "2026-08-02T10:00:00Z" }), now, "UTC").headline).toBe("Met 3h ago");
  });

  it("holds an API failure as the failure and words it at render", () => {
    const error = new ApiError(503, "");
    setActiveLanguage("es");
    expect(describeSubmitError(error)).toBe("Ese servicio no está disponible en este momento. No se cambió nada: inténtalo de nuevo en breve.");
    setActiveLanguage("en");
    expect(describeSubmitError(error)).toBe("That service is unavailable right now. Nothing was changed — try again shortly.");
  });
});
