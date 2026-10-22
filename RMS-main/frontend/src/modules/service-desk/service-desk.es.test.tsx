/**
 * The service desk in Spanish: the ticket queue (tiles, filters, table, the
 * New ticket dialog and its refusal, the readiness panel), My work, a ticket's
 * page (header, conversation, reply box, edit form with its confirmation,
 * approval, linked device, attachments, status history, copilot) and the admin
 * page (queues, categories, saved replies), plus the label tables and sentences
 * the module builds outside React. Ticket subjects, descriptions, comments,
 * queue and requester names read as recorded; the words around them are Spanish.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import { describeSubmitError } from "../../components/form";
import { ApiError } from "../../lib/api";
import type { ApiClient, Device, Me, Page } from "../../lib/api";
import { MyWorkPage } from "./MyWorkPage";
import { ServiceDeskAdminPage } from "./ServiceDeskAdminPage";
import { ServiceDeskNav } from "./ServiceDeskNav";
import { TicketCopilotPanel } from "./TicketCopilotPanel";
import { TicketDetailPage } from "./TicketDetailPage";
import { TicketListPage } from "./TicketListPage";
import { SET_TARGETS_CTA } from "./TicketReadiness";
import {
  describeDue,
  describeTarget,
  shortDuration,
  statusChangeWarning,
  ticketPriorityLabel,
  ticketStatusLabel,
  ticketTypeLabel,
} from "./serviceDeskState";
import { devicePickerEmptyHint, devicePickerOptions, userPickerEmptyHint, userPickerOptions } from "./pickerOptions";
import type { CannedResponseOut, CategoryOut, QueueOut, TicketOut, TicketSlaSummary } from "./types";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const NOW = Date.parse("2026-09-23T12:00:00Z");
const at = (minutes: number) => new Date(NOW + minutes * 60_000).toISOString();

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});

afterEach(() => {
  vi.useRealTimers();
});

function renderAt(node: ReactElement, route: string, entry = route) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderInSpanish(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path={route.split("?")[0]} element={node} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const ME: Me = {
  user_id: "tech-1",
  org_id: "o1",
  email: "tecnica@example.com",
  display_name: "Teresa Técnica",
  timezone: "UTC",
  permissions: [
    "ticket.read",
    "ticket.write",
    "ticket.assign",
    "ticket.admin",
    "ticket.approve",
    "docs.read",
    "sla.read",
    "email_intake.read",
    "health.read",
    "notification.read",
  ],
};

// ------------------------------------------------------------------ fixtures

const QUEUES: QueueOut[] = [
  { id: "q1", key: "soporte", name: "Soporte general", description: "", active: true, sort_order: 0, created_by: "a", created_at: at(-9999) },
  { id: "q2", key: "redes", name: "Redes", description: "", active: false, sort_order: 1, created_by: "a", created_at: at(-9999) },
];

const CATEGORIES: CategoryOut[] = [
  { id: "cat1", key: "equipos", name: "Equipos", type: "incident", requires_approval: false, approver_role_id: null, default_queue_id: "q1", active: true, sort_order: 0, created_by: "a", created_at: at(-9999) },
  { id: "cat2", key: "accesos", name: "Accesos", type: "request", requires_approval: true, approver_role_id: null, default_queue_id: null, active: false, sort_order: 1, created_by: "a", created_at: at(-9999) },
];

const USERS = [
  { id: "tech-1", email: "t@example.com", display_name: "Teresa Técnica", status: "active", roles: [] },
  { id: "req-1", email: "r@example.com", display_name: "Rosa Solicitante", status: "active", roles: [] },
  { id: "old-1", email: "o@example.com", display_name: "Olga Antigua", status: "disabled", roles: [] },
];

const DEVICES = {
  items: [
    { id: "dev-1", hostname: "PC-042", fqdn: null, os_family: "windows", agent_status: "online" },
    { id: "dev-2", hostname: "PC-077", fqdn: null, os_family: "linux", agent_status: null },
  ],
  total: 2,
};

function sla(over: Partial<TicketSlaSummary> = {}): TicketSlaSummary {
  return {
    response_due_at: at(120),
    resolution_due_at: at(600),
    response_state: "running",
    resolution_state: "running",
    next_due_at: at(120),
    next_due_target: "response",
    breached: false,
    paused: false,
    ...over,
  };
}

function ticket(id: string, over: Partial<TicketOut> = {}): TicketOut {
  return {
    id,
    number: `INC-${id}`,
    type: "incident",
    category_id: "cat1",
    queue_id: "q1",
    status: "open",
    priority: "normal",
    subject: `La impresora ${id} atasca el papel`,
    description: "Se atasca cada vez que imprimo.",
    requester_id: "req-1",
    assigned_to: null,
    device_id: null,
    origin: "technician",
    source_alert_id: null,
    resolved_at: null,
    closed_at: null,
    has_attachments: false,
    created_by: "tech-1",
    created_at: at(-120),
    updated_at: at(-60),
    sla: null,
    ...over,
  };
}

const TICKETS: TicketOut[] = [
  ticket("1", { priority: "urgent", assigned_to: "tech-1", sla: sla({ next_due_at: at(30), next_due_target: "response" }) }),
  ticket("2", { type: "request", status: "pending_approval", priority: "high", origin: "alert", sla: sla({ next_due_at: at(-90), next_due_target: "resolution", breached: true }) }),
  ticket("3", { status: "in_progress", priority: "low", assigned_to: "tech-1" }),
];

function listPage(items: TicketOut[], total = items.length): Page<TicketOut> {
  return { items, total };
}

function baseApi(over: Partial<ApiClient> = {}): Partial<ApiClient> {
  const api: Partial<ApiClient> = {
    me: vi.fn().mockResolvedValue(ME),
    listTickets: vi.fn().mockResolvedValue(listPage(TICKETS)),
    listTicketQueues: vi.fn().mockResolvedValue(QUEUES),
    listTicketCategories: vi.fn().mockResolvedValue(CATEGORIES),
    listUsers: vi.fn().mockResolvedValue({ users: USERS }),
    listDevices: vi.fn().mockResolvedValue(DEVICES),
    listSlaPolicies: vi.fn().mockResolvedValue([{ id: "p1", name: "Estándar", active: true }]),
    listEmailMailboxes: vi.fn().mockResolvedValue([{ id: "m1" }]),
    getHealthSettings: vi.fn().mockResolvedValue({ alert_tickets_enabled: true }),
    listNotificationRules: vi.fn().mockResolvedValue([{ id: "r1", event_type: "ticket.assigned", active: true }]),
    listCannedResponses: vi.fn().mockResolvedValue([]),
    suggestDocArticles: vi.fn().mockResolvedValue([]),
    ...over,
  };
  mockUseApi.mockReturnValue(api as ApiClient);
  return api;
}

// ------------------------------------------------------------ the ticket queue

describe("the ticket queue in Spanish", () => {
  it("words the head, the tiles, the filters and the table", async () => {
    baseApi();
    const { container } = renderAt(<TicketListPage />, "/service-desk");
    expect(await screen.findByRole("heading", { name: "Tickets" })).toBeInTheDocument();
    expect(screen.getByText("Empieza por lo que vence antes.")).toBeInTheDocument();
    expect(await screen.findByText("La impresora 1 atasca el papel")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Nuevo ticket" })).toBeInTheDocument();
    // The nav strip.
    const nav = screen.getByRole("navigation", { name: "Mesa de ayuda" });
    expect(within(nav).getByRole("link", { name: "Mi trabajo" })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: "Administración" })).toBeInTheDocument();
    // The tiles and the line under each.
    expect(screen.getByText("Tickets mostrados")).toBeInTheDocument();
    expect(screen.getByText("2 incidentes · 1 solicitud")).toBeInTheDocument();
    expect(screen.getByText("Prioridad alta o urgente")).toBeInTheDocument();
    expect(screen.getByText("1 alta · 1 urgente")).toBeInTheDocument();
    expect(screen.getByText("Procedentes de alertas")).toBeInTheDocument();
    expect(screen.getByText("1 todavía sin asignar")).toBeInTheDocument();
    expect(screen.getByText("1 de ellos con prioridad alta o urgente")).toBeInTheDocument();
    // The filters.
    expect(screen.getByRole("group", { name: "Mostrar" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Todos los abiertos" })).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Buscar tickets")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Ordenar" })).toHaveDisplayValue("Vencimiento más próximo");
    expect(screen.getByRole("combobox", { name: "Tipo" })).toHaveDisplayValue("Todos los tipos");
    expect(screen.getByRole("combobox", { name: "Estado" })).toHaveDisplayValue("Cualquier estado");
    expect(screen.getByRole("combobox", { name: "Cola" })).toHaveDisplayValue("Todas las colas");
    expect(screen.getByRole("combobox", { name: "Prioridad" })).toHaveDisplayValue("Todas las prioridades");
    expect(screen.getByRole("combobox", { name: "Origen" })).toHaveDisplayValue("Cualquier origen");
    expect(screen.getByRole("combobox", { name: "Responsable" })).toHaveDisplayValue("Cualquiera");
    expect(screen.getByText("3 mostrados")).toBeInTheDocument();
    // The table: the recorded words stay, the labels around them are Spanish.
    const row = screen.getByText("La impresora 2 atasca el papel").closest("tr") as HTMLElement;
    expect(within(row).getByText("Rosa Solicitante · Soporte general")).toBeInTheDocument();
    expect(within(row).getByText("Pendiente de aprobación")).toBeInTheDocument();
    expect(within(row).getByText("Solicitud")).toBeInTheDocument();
    expect(within(row).getByText("Alta")).toBeInTheDocument();
    expect(within(row).getByText("Sin asignar")).toBeInTheDocument();
    expect(within(row).getByText("Incumplido hace 1 h 30 min")).toBeInTheDocument();
    expect(within(row).getByText("Resolución")).toBeInTheDocument();
    const first = screen.getByText("La impresora 1 atasca el papel").closest("tr") as HTMLElement;
    expect(within(first).getByText("Vence en 30 min")).toBeInTheDocument();
    expect(within(first).getByText("Primera respuesta")).toBeInTheDocument();
    expect(within(first).getByText("Urgente")).toBeInTheDocument();
    expect(within(first).getByText("Abierto")).toBeInTheDocument();
    expect(within(first).getByText("Teresa Técnica")).toBeInTheDocument();
    const third = screen.getByText("La impresora 3 atasca el papel").closest("tr") as HTMLElement;
    expect(within(third).getByText("En curso")).toBeInTheDocument();
    expect(within(third).getByText("Sin SLA")).toBeInTheDocument();
    for (const header of ["Ticket", "Vencimiento", "Tipo", "Estado", "Prioridad", "Responsable", "Antigüedad"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    // The readiness panel is drawn only when a row is missing; everything is set up here.
    expectNoEnglish(container, ["INC-1", "INC-2", "INC-3"]);
  });

  it("words the filter options and every origin", async () => {
    baseApi();
    const { container } = renderAt(<TicketListPage />, "/service-desk");
    await screen.findByText("La impresora 1 atasca el papel");
    const options = (name: string) =>
      within(screen.getByRole("combobox", { name })).getAllByRole("option").map((o) => o.textContent);
    expect(options("Origen")).toEqual([
      "Cualquier origen",
      "Creados por una alerta",
      "Creados por un técnico",
      "Creados en el portal",
      "Creados por correo",
      "De una integración",
    ]);
    expect(options("Prioridad")).toEqual([
      "Todas las prioridades",
      "Alta o urgente",
      "Baja",
      "Normal",
      "Alta",
      "Urgente",
    ]);
    expect(options("Tipo")).toEqual(["Todos los tipos", "Incidente", "Solicitud"]);
    expect(options("Estado")).toEqual([
      "Cualquier estado",
      "Nuevo",
      "Abierto",
      "Pendiente",
      "Resuelto",
      "Cerrado",
      "Cancelado",
      "Pendiente de aprobación",
      "En curso",
      "Completado",
      "Rechazado",
    ]);
    expect(options("Ordenar")).toEqual(["Vencimiento más próximo", "Más recientes primero"]);
    expect(options("Responsable")).toEqual(["Cualquiera", "Teresa Técnica", "Rosa Solicitante", "Olga Antigua"]);
    expectNoEnglish(container, ["INC-1"]);
  });

  it("says the list is cut short, and says there are none", async () => {
    baseApi({ listTickets: vi.fn().mockResolvedValue(listPage([TICKETS[0]!], 250)) });
    const first = renderAt(<TicketListPage />, "/service-desk");
    expect(await screen.findByText("Se muestran 1 de 250 tickets que coinciden. Acota los filtros de arriba para ver el resto.")).toBeInTheDocument();
    expectNoEnglish(first.container, ["INC-1"]);
    first.unmount();

    baseApi({ listTickets: vi.fn().mockResolvedValue(listPage([])) });
    const { container } = renderAt(<TicketListPage />, "/service-desk");
    expect(await screen.findByText("No se encontró ningún ticket.")).toBeInTheDocument();
    expect(screen.getByText("Ningún ticket coincide con estos filtros")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("explains what is missing before tickets are answered on time", async () => {
    baseApi({
      listSlaPolicies: vi.fn().mockResolvedValue([{ id: "p1", name: "Antigua", active: false }]),
      listTicketCategories: vi.fn().mockResolvedValue([
        CATEGORIES[0]!,
        { ...CATEGORIES[1]!, default_queue_id: null },
        { ...CATEGORIES[1]!, id: "cat3", name: "Licencias" },
      ]),
      listEmailMailboxes: vi.fn().mockResolvedValue([]),
      getHealthSettings: vi.fn().mockResolvedValue({ alert_tickets_enabled: false }),
      listNotificationRules: vi.fn().mockResolvedValue([]),
    });
    const { container } = renderAt(<TicketListPage />, "/service-desk");
    const step = async (label: string) => (await screen.findByText(label)).closest("li") as HTMLElement;
    expect(await screen.findByText("Antes de responder a los tickets a tiempo")).toBeInTheDocument();
    expect(screen.getByText("Lo que otras pantallas configuran para la mesa de ayuda.")).toBeInTheDocument();
    const sla = await step("Existen plazos de respuesta y de resolución");
    expect(sla).toHaveTextContent("todos los tickets muestran Sin SLA");
    expect(within(sla).getByRole("link", { name: "Definir plazos de respuesta" })).toHaveAttribute("href", "/sla");
    const queue = await step("Cada categoría tiene una cola predeterminada");
    expect(queue).toHaveTextContent("Accesos y Licencias no tienen cola predeterminada");
    expect(within(queue).getByRole("link", { name: "Elegir una cola predeterminada" })).toBeInTheDocument();
    const mailbox = await step("Un buzón de soporte convierte los correos en tickets");
    expect(within(mailbox).getByRole("link", { name: "Conectar un buzón de soporte" })).toBeInTheDocument();
    const alerts = await step("Las alertas pueden abrir tickets");
    expect(within(alerts).getByRole("link", { name: "Permitir que las alertas abran tickets" })).toBeInTheDocument();
    const rules = await step("Se avisa a las personas de la actividad de los tickets");
    expect(within(rules).getByRole("link", { name: "Avisar a las personas sobre los tickets" })).toBeInTheDocument();
    expectNoEnglish(container, ["INC-1", "INC-2", "INC-3"]);
  });

  it("agrees the readiness sentences with their counts of one", async () => {
    baseApi({
      listTicketCategories: vi.fn().mockResolvedValue([{ ...CATEGORIES[0]!, default_queue_id: null }]),
      listSlaPolicies: vi.fn().mockResolvedValue([{ id: "p1", name: "Estándar", active: true }]),
    });
    renderAt(<TicketListPage />, "/service-desk");
    const queue = (await screen.findByText("Cada categoría tiene una cola predeterminada")).closest("li") as HTMLElement;
    expect(queue).toHaveTextContent("Equipos no tiene cola predeterminada");
  });

  it("refuses an empty New ticket in Spanish and re-words the refusal on a switch", async () => {
    vi.useRealTimers();
    baseApi();
    const user = userEvent.setup();
    renderAt(<TicketListPage />, "/service-desk");
    await user.click(await screen.findByRole("button", { name: "Nuevo ticket" }));
    const dialog = await screen.findByRole("dialog", { name: "Nuevo ticket" });
    expectNoEnglish(dialog);
    expect(within(dialog).getByText("Cola (opcional, sustituye a la de la categoría)")).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: "Elige una categoría" })).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: "Usar la predeterminada de la categoría" })).toBeInTheDocument();
    expect(within(dialog).getByText("Asignar a (opcional)")).toBeInTheDocument();
    expect(within(dialog).getByText("Dispositivo (opcional)")).toBeInTheDocument();
    expect(within(dialog).getByPlaceholderText("Dejar sin asignar")).toBeInTheDocument();
    expect(within(dialog).getByPlaceholderText("Buscar nombres de host")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "El campo Categoría es obligatorio. El campo Asunto es obligatorio.",
    );
    expectNoEnglish(dialog);
    // The refusal is held as data, so a switch re-words it.
    act(() => setActiveLanguage("en"));
    expect(within(screen.getByRole("dialog", { name: "New ticket" })).getByRole("alert")).toHaveTextContent(
      "Category is required. Subject is required.",
    );
    act(() => setActiveLanguage("es"));
    expect(within(screen.getByRole("dialog", { name: "Nuevo ticket" })).getByRole("alert")).toHaveTextContent(
      "El campo Categoría es obligatorio",
    );
  });

  it("explains an empty person or device picker in Spanish", async () => {
    vi.useRealTimers();
    baseApi({ listUsers: vi.fn().mockResolvedValue({ users: [] }), listDevices: vi.fn().mockResolvedValue({ items: [], total: 0 }) });
    const user = userEvent.setup();
    renderAt(<TicketListPage />, "/service-desk");
    await user.click(await screen.findByRole("button", { name: "Nuevo ticket" }));
    const dialog = await screen.findByRole("dialog", { name: "Nuevo ticket" });
    await user.click(within(dialog).getByRole("combobox", { name: "Asignar a (opcional)" }));
    expect(await within(dialog).findByText(/No hay personas entre las que elegir/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("combobox", { name: "Dispositivo (opcional)" }));
    expect(await within(dialog).findByText(/No hay dispositivos entre los que elegir/)).toBeInTheDocument();
    expectNoEnglish(dialog);
  });

  it("re-words the whole page, tiles and table included, on a language switch", async () => {
    baseApi();
    renderAt(<TicketListPage />, "/service-desk");
    await screen.findByText("La impresora 1 atasca el papel");
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("Pick up work, soonest deadline first.")).toBeInTheDocument();
    expect(screen.getByText("2 incidents · 1 request")).toBeInTheDocument();
    expect(screen.getByText("1 high · 1 urgent")).toBeInTheDocument();
    expect(screen.getByText("1 still unassigned")).toBeInTheDocument();
    expect(screen.getByText("3 shown")).toBeInTheDocument();
    const row = screen.getByText("La impresora 2 atasca el papel").closest("tr") as HTMLElement;
    expect(within(row).getByText("Pending approval")).toBeInTheDocument();
    expect(within(row).getByText("Breached 1 h 30 min ago")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Service desk" })).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    expect(screen.getByText("Empieza por lo que vence antes.")).toBeInTheDocument();
    expect(within(row).getByText("Pendiente de aprobación")).toBeInTheDocument();
  });
});

// ----------------------------------------------------------------- my work

describe("My work in Spanish", () => {
  it("words the head, the tiles and the table", async () => {
    baseApi({ listTickets: vi.fn().mockResolvedValue(listPage([TICKETS[0]!, TICKETS[2]!])) });
    const { container } = renderAt(<MyWorkPage />, "/service-desk/my-work");
    expect(await screen.findByRole("heading", { name: "Mi trabajo" })).toBeInTheDocument();
    expect(screen.getByText("Tus tickets abiertos en todas las colas, con el vencimiento más próximo primero.")).toBeInTheDocument();
    expect(await screen.findByText("La impresora 1 atasca el papel")).toBeInTheDocument();
    expect(screen.getByText("Asignados a ti")).toBeInTheDocument();
    expect(screen.getByText("Prioridad alta o urgente")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Tickets asignados" })).toBeInTheDocument();
    expectNoEnglish(container, ["INC-1", "INC-3"]);
  });

  it("says when nothing is assigned", async () => {
    baseApi({ listTickets: vi.fn().mockResolvedValue(listPage([])) });
    const { container } = renderAt(<MyWorkPage />, "/service-desk/my-work");
    expect(await screen.findByText("No tienes tickets abiertos asignados.")).toBeInTheDocument();
    expect(screen.getByText("No tienes ningún ticket abierto asignado")).toBeInTheDocument();
    expect(screen.getByText("Ninguno de tus tickets tiene prioridad alta o urgente")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("re-words the page on a switch", async () => {
    baseApi({ listTickets: vi.fn().mockResolvedValue(listPage([TICKETS[0]!])) });
    renderAt(<MyWorkPage />, "/service-desk/my-work");
    await screen.findByText("La impresora 1 atasca el papel");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("heading", { name: "My work" })).toBeInTheDocument();
    expect(screen.getByText("Assigned to you")).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    expect(screen.getByRole("heading", { name: "Mi trabajo" })).toBeInTheDocument();
  });
});

// ------------------------------------------------------------ a ticket's page

const CANNED: CannedResponseOut[] = [
  { id: "cr1", title: "Reiniciar primero", body: "Reinicia la impresora y avísanos si vuelve a pasar.", created_by: "a", created_at: at(-999), updated_by: null, updated_at: null },
];

function detailApi(t: TicketOut, over: Partial<ApiClient> = {}) {
  return baseApi({
    getTicket: vi.fn().mockResolvedValue(t),
    listTicketComments: vi.fn().mockResolvedValue([
      { id: "c1", ticket_id: t.id, author_id: "req-1", author_type: "user", body: "Sigue atascándose.", visibility: "public", created_at: at(-100) },
      { id: "c2", ticket_id: t.id, author_id: "tech-1", author_type: "user", body: "Probando con otra bandeja.", visibility: "internal", created_at: at(-90) },
      { id: "c3", ticket_id: t.id, author_id: null, author_type: "system", body: "Asignado automáticamente.", visibility: "public", created_at: at(-80) },
    ]),
    listTicketAttachments: vi.fn().mockResolvedValue([
      { id: "a1", ticket_id: t.id, filename: "foto.png", byte_size: 2048, uploaded_by: "req-1", created_at: at(-70) },
    ]),
    listTicketStatusHistory: vi.fn().mockResolvedValue([
      { id: "h1", ticket_id: t.id, from_status: null, to_status: "new", actor_type: "user", actor_id: "req-1", reason: "", created_at: at(-120) },
      { id: "h2", ticket_id: t.id, from_status: "new", to_status: "pending_approval", actor_type: "user", actor_id: "tech-1", reason: "Falta la aprobación del responsable.", created_at: at(-60) },
    ]),
    listCannedResponses: vi.fn().mockResolvedValue(CANNED),
    suggestDocArticles: vi.fn().mockResolvedValue([{ id: "art1", title: "Cómo desatascar el papel", category: "Impresoras" }]),
    getDeviceInventory: vi.fn().mockResolvedValue({
      summary: { hostname: "PC-042", os_family: "windows", os_version: "11" },
      hardware: [{}, {}],
      software: [{}],
      facts: {},
      snapshot_taken_at: null,
    }),
    getDevicePatchStatus: vi.fn().mockResolvedValue({
      device_id: "dev-1",
      counts_by_state: { missing: 3, not_applicable: 1 },
      counts_by_severity: {},
      pending_reboot: true,
      last_scan_at: null,
    }),
    personForUser: vi.fn().mockResolvedValue({ id: "person-9" }),
    // The SLA panel on the page is the real one (modules/sla). A ticket with no
    // SLA summary has none there either; the others run a response that was met
    // and a resolution that is running against the summary's own deadline.
    getTicketSla: vi.fn().mockResolvedValue(
      t.sla == null
        ? { ticket_id: t.id, has_sla: false, response_state: "none", resolution_state: "none" }
        : {
            ticket_id: t.id,
            has_sla: true,
            response_state: "met",
            resolution_state: "running",
            response_due_at: at(-100),
            response_met_at: at(-110),
            resolution_due_at: t.sla.resolution_due_at ?? at(60),
            policy_name: "Estándar",
            calendar_name: "Horario de oficina",
            policy_source: "initial",
          },
    ),
    ...over,
  });
}

const OPEN_TICKET = ticket("t1", {
  number: "INC-100",
  device_id: "dev-1",
  assigned_to: "tech-1",
  priority: "high",
  sla: sla({ next_due_at: at(-45), next_due_target: "resolution", resolution_due_at: at(-45) }),
});

const renderTicket = (t: TicketOut) => renderAt(<TicketDetailPage />, "/service-desk/tickets/:ticketId", `/service-desk/tickets/${t.id}`);

describe("a ticket's page in Spanish", () => {
  it("words the header, the conversation and the reply box", async () => {
    detailApi(OPEN_TICKET);
    const { container } = renderTicket(OPEN_TICKET);
    expect(await screen.findByRole("heading", { name: "INC-100: La impresora t1 atasca el papel" })).toBeInTheDocument();
    expect(screen.getByText(/Abierto el .+ por Rosa Solicitante · Soporte general/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Responder" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Añadir nota interna" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resolver" })).toBeInTheDocument();
    // The quick facts.
    expect(screen.getAllByText("Responsable").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Solicitante").length).toBeGreaterThan(0);
    expect(screen.getByText("Primera respuesta")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Conectar en remoto" })).toBeInTheDocument();
    expect(screen.getByText("Con 45 min de retraso")).toBeInTheDocument();
    expect(screen.getByText("Vence en 2 h")).toBeInTheDocument();
    // The SLA panel (modules/sla), rendered for real.
    expect(await screen.findByText("45 min de retraso")).toBeInTheDocument();
    expect(screen.getByText("Motivo de la política aplicada")).toBeInTheDocument();
    expect(screen.getByText("Coincidió cuando se creó el ticket")).toBeInTheDocument();
    expect(screen.getByText("Estándar")).toBeInTheDocument();
    // The conversation.
    expect(screen.getByText("Conversación")).toBeInTheDocument();
    expect(screen.getByLabelText("Mostrar notas internas")).toBeChecked();
    expect(await screen.findByText("Sigue atascándose.")).toBeInTheDocument();
    expect(screen.getByText("Probando con otra bandeja.")).toBeInTheDocument();
    expect(screen.getByText("Sistema")).toBeInTheDocument();
    expect(screen.getAllByText("Nota interna").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Pública").length).toBeGreaterThan(0);
    // The reply box.
    expect(screen.getByText("¿Quién lo ve?")).toBeInTheDocument();
    expect(screen.getByText("Respuesta pública")).toBeInTheDocument();
    expect(screen.getByText("El solicitante la ve.")).toBeInTheDocument();
    expect(screen.getByText("Solo la ve el personal.")).toBeInTheDocument();
    expect(await screen.findByText("Insertar una respuesta guardada")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Elige una respuesta guardada…" })).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Escribe tu respuesta…")).toBeInTheDocument();
    expect(await screen.findByText("Artículos que pueden ayudar")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Enviar respuesta" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Responder y resolver" })).toBeDisabled();
    expectNoEnglish(container, ["INC-100", "PC-042"]);
  });

  it("words the internal-note box and the no-requester note", async () => {
    const alertTicket = ticket("t2", { number: "INC-101", origin: "alert", requester_id: null });
    detailApi(alertTicket);
    const { container } = renderTicket(alertTicket);
    expect(await screen.findByText("Nadie creó este ticket, así que no hay a quién responder. Las notas son internas de forma predeterminada.")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Escribe una nota para el equipo…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Añadir nota" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Añadir nota y resolver" })).toBeInTheDocument();
    expect(screen.getByText("Alerta")).toBeInTheDocument();
    expect(screen.getByText("Ninguno: lo creó una alerta")).toBeInTheDocument();
    expect(screen.getByText("Ningún dispositivo vinculado")).toBeInTheDocument();
    // Two in the facts strip and the badge of the SLA panel, which says why.
    expect(screen.getAllByText("Sin SLA").length).toBe(3);
    expect(await screen.findByText(/^Ninguna política coincidió con este ticket, o el calendario de su política/)).toBeInTheDocument();
    expectNoEnglish(container, ["INC-101"]);
  });

  it("words the side panels: edit, linked device, attachments and status history", async () => {
    detailApi(OPEN_TICKET);
    const { container } = renderTicket(OPEN_TICKET);
    await screen.findByText("Editar ticket");
    // Edit.
    expect(screen.getByLabelText("Estado")).toBeInTheDocument();
    expect(screen.getByLabelText("Prioridad")).toHaveDisplayValue("Alta");
    expect(screen.getByLabelText("Categoría")).toHaveDisplayValue("Equipos");
    expect(screen.getByLabelText("Asunto")).toBeInTheDocument();
    expect(screen.getByLabelText("Descripción")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Guardar cambios" })).toBeDisabled();
    // Linked device.
    expect(await screen.findByText("Dispositivo vinculado")).toBeInTheDocument();
    expect(await screen.findByText("Resumen del inventario")).toBeInTheDocument();
    expect(await screen.findByText("Nombre de host")).toBeInTheDocument();
    expect(screen.getByText("Sistema operativo")).toBeInTheDocument();
    expect(screen.getByText("Windows 11")).toBeInTheDocument();
    expect(screen.getByText("Componentes de hardware")).toBeInTheDocument();
    expect(screen.getByText("Software instalado")).toBeInTheDocument();
    expect(await screen.findByText("Estado de los parches")).toBeInTheDocument();
    expect(await screen.findByText("Reinicio pendiente")).toBeInTheDocument();
    expect(screen.getByText("Sí")).toBeInTheDocument();
    expect(screen.getByText("Faltante")).toBeInTheDocument();
    expect(screen.getByText("No aplica")).toBeInTheDocument();
    // Attachments.
    expect(screen.getByText("Archivos adjuntos")).toBeInTheDocument();
    expect(await screen.findByText("foto.png")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Nombre del archivo" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Tamaño" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Subido" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Descargar" })).toBeInTheDocument();
    expect(screen.getByText("Adjuntar un archivo", { selector: "span" })).toBeInTheDocument();
    // Status history.
    expect(screen.getByText("Historial de estados")).toBeInTheDocument();
    expect(await screen.findByText("Falta la aprobación del responsable.")).toBeInTheDocument();
    expect(screen.getByText("Sin estado anterior")).toBeInTheDocument();
    expect(screen.getByText("Sin motivo indicado")).toBeInTheDocument();
    for (const header of ["Estado anterior", "Estado nuevo", "Motivo", "Fecha del cambio"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expectNoEnglish(container, ["INC-100", "PC-042"]);
  });

  it("words the empty attachments, history and copilot of a quiet ticket", async () => {
    const quiet = ticket("t3", { number: "INC-102", description: "" });
    detailApi(quiet, {
      listTicketComments: vi.fn().mockResolvedValue([]),
      listTicketAttachments: vi.fn().mockResolvedValue([]),
      listTicketStatusHistory: vi.fn().mockResolvedValue([]),
      listCannedResponses: vi.fn().mockResolvedValue([]),
      suggestDocArticles: vi.fn().mockResolvedValue([]),
    });
    const { container } = renderTicket(quiet);
    expect(await screen.findByText("Todavía no hay respuestas.")).toBeInTheDocument();
    expect(await screen.findByText("Todavía no hay archivos adjuntos.")).toBeInTheDocument();
    expect(screen.getByText("Las capturas, los registros y las exportaciones que adjuntes a este ticket aparecerán aquí.")).toBeInTheDocument();
    expect(await screen.findByText("Todavía no hay historial de estados.")).toBeInTheDocument();
    expect(screen.getByText("Cada cambio de estado se registra aquí con quién lo hizo y por qué.")).toBeInTheDocument();
    expect(screen.getByLabelText("Pregunta al copiloto")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("¿Qué debería decirles?")).toBeInTheDocument();
    expectNoEnglish(container, ["INC-102"]);
  });

  it("says a closed ticket takes no replies or attachments", async () => {
    const closed = ticket("t4", { number: "INC-103", status: "closed" });
    detailApi(closed);
    const { container } = renderTicket(closed);
    expect(await screen.findByText("Este ticket está cerrado y no admite respuestas nuevas. Reábrelo para responder.")).toBeInTheDocument();
    expect(screen.getByText("Este ticket está cerrado y no admite archivos adjuntos nuevos.")).toBeInTheDocument();
    expect(screen.getByText("Este ticket está cerrado, así que sus datos no se pueden cambiar. Elige un estado activo para reabrirlo.")).toBeInTheDocument();
    expectNoEnglish(container, ["INC-103"]);
  });

  it("agrees a final status with nothing: the sentence names the state", async () => {
    const cancelled = ticket("t5", { number: "INC-104", status: "cancelled" });
    detailApi(cancelled);
    const { container } = renderTicket(cancelled);
    expect(await screen.findByText("Nada sale del estado «Cancelado». Este ticket no se puede reabrir.")).toBeInTheDocument();
    expectNoEnglish(container, ["INC-104"]);
  });

  it("asks for confirmation before a final status, worded in Spanish", async () => {
    vi.useRealTimers();
    const t = ticket("t6", { number: "INC-105" });
    detailApi(t);
    const user = userEvent.setup();
    renderTicket(t);
    await screen.findByText("Editar ticket");
    await user.selectOptions(screen.getByLabelText("Estado"), "cancelled");
    expect(await screen.findByLabelText("¿Por qué cambia el estado? (opcional)")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Guardar cambios" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Pasar este ticket a «Cancelado»")).toBeInTheDocument();
    expect(within(dialog).getByText(/Nada sale del estado «Cancelado»\. Este ticket no se podrá reabrir, comentar ni editar nunca más\./)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Pasar a «Cancelado»" })).toBeInTheDocument();
    expectNoEnglish(dialog, ["INC-105"]);
    act(() => setActiveLanguage("en"));
    const english = screen.getByRole("dialog");
    expect(within(english).getByText("Move this ticket to cancelled")).toBeInTheDocument();
    expect(within(english).getByRole("button", { name: "Move to cancelled" })).toBeInTheDocument();
  });

  it("words the close confirmation as reversible", async () => {
    vi.useRealTimers();
    const t = ticket("t7", { number: "INC-106" });
    detailApi(t);
    const user = userEvent.setup();
    renderTicket(t);
    await screen.findByText("Editar ticket");
    await user.selectOptions(screen.getByLabelText("Estado"), "closed");
    await user.click(screen.getByRole("button", { name: "Guardar cambios" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Cerrar este ticket")).toBeInTheDocument();
    expect(within(dialog).getByText(/No es definitivo: un ticket cerrado se puede reabrir desde este mismo panel\./)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Cerrar ticket" })).toBeInTheDocument();
    expectNoEnglish(dialog, ["INC-106"]);
  });

  it("words a reply that failed to send, with the server's reason as the server wrote it", async () => {
    vi.useRealTimers();
    const t = ticket("t8", { number: "INC-107" });
    detailApi(t, { resolveTicket: vi.fn().mockRejectedValue(new ApiError(503, "El servidor no responde")) });
    const user = userEvent.setup();
    renderTicket(t);
    await user.type(await screen.findByPlaceholderText("Escribe tu respuesta…"), "Ya está arreglado");
    await user.click(screen.getByRole("button", { name: "Responder y resolver" }));
    const alert = await screen.findByText(/No se envió nada y el ticket no se resolvió\./);
    expect(alert).toBeInTheDocument();
  });

  it("words the approval panel of a request awaiting a decision", async () => {
    const request = ticket("t9", { number: "REQ-9", type: "request", status: "pending_approval" });
    detailApi(request);
    const { container } = renderTicket(request);
    const panel = (await screen.findByText("Aprobación")).closest("section, div.panel, .panel") as HTMLElement;
    expect(within(panel).getByText("Motivo (obligatorio para rechazar)")).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "Aprobar" })).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "Rechazar" })).toBeInTheDocument();
    expectNoEnglish(container, ["REQ-9"]);
  });

  it("says a ticket was not found, and that device data is not available without permission", async () => {
    const t = ticket("t10", { number: "INC-110", device_id: "dev-1" });
    detailApi(t, {
      getDeviceInventory: vi.fn().mockRejectedValue(new ApiError(403, "")),
      getDevicePatchStatus: vi.fn().mockRejectedValue(new ApiError(403, "")),
    });
    const { container } = renderTicket(t);
    expect(await screen.findByText("El resumen del inventario no está disponible (permisos insuficientes).")).toBeInTheDocument();
    expect(await screen.findByText("El estado de los parches no está disponible (permisos insuficientes).")).toBeInTheDocument();
    expectNoEnglish(container, ["INC-110"]);
  });

  it("shows not-found text for a ticket that does not exist", async () => {
    baseApi({ getTicket: vi.fn().mockResolvedValue(null) });
    const { container } = renderAt(<TicketDetailPage />, "/service-desk/tickets/:ticketId", "/service-desk/tickets/zz");
    expect(await screen.findByText("No se encontró el ticket.")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("re-words the page, the pickers and the status labels on a switch", async () => {
    detailApi(OPEN_TICKET);
    renderTicket(OPEN_TICKET);
    await screen.findByText("Editar ticket");
    await screen.findByText("Sigue atascándose.");
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("Edit ticket")).toBeInTheDocument();
    expect(screen.getByText("Conversation")).toBeInTheDocument();
    expect(screen.getByText("Who sees this?")).toBeInTheDocument();
    expect(screen.getByLabelText("Priority")).toHaveDisplayValue("High");
    expect(screen.getByText("Internal note", { selector: "b" })).toBeInTheDocument();
    expect(screen.getByText("System")).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    expect(screen.getByText("Editar ticket")).toBeInTheDocument();
    expect(screen.getByLabelText("Prioridad")).toHaveDisplayValue("Alta");
  });

  it("fills the saved-reply box from the Spanish list", async () => {
    vi.useRealTimers();
    detailApi(OPEN_TICKET);
    const user = userEvent.setup();
    renderTicket(OPEN_TICKET);
    const select = await screen.findByLabelText("Insertar una respuesta guardada");
    await user.selectOptions(select, "cr1");
    expect(screen.getByPlaceholderText("Escribe tu respuesta…")).toHaveValue("Reinicia la impresora y avísanos si vuelve a pasar.");
  });
});

// ----------------------------------------------------------------- copilot

describe("the copilot in Spanish", () => {
  const TURN = {
    answer: "Prueba a cambiar el rodillo de la bandeja.",
    stop_reason: "token_budget",
    partial: true,
    tool_calls: [],
    flagged: [{ source: "x", pattern: "y" }, { source: "z", pattern: "y" }],
    proposed: ["act-1"],
  };

  it("words the panel, a partial answer, the flagged count and a prepared action", async () => {
    vi.useRealTimers();
    baseApi({
      createCopilotSession: vi.fn().mockResolvedValue({ id: "s1" }),
      createCopilotTurn: vi.fn().mockResolvedValue(TURN),
    });
    const user = userEvent.setup();
    const { container } = renderAt(<TicketCopilotPanel ticketId="t1" />, "/x");
    expect(screen.getByText("Copiloto")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Pregunta al copiloto"), "¿Qué le digo?");
    await user.click(screen.getByRole("button", { name: "Preguntar" }));
    expect(await screen.findByText("Prueba a cambiar el rodillo de la bandeja.")).toBeInTheDocument();
    expect(screen.getByText("Esta respuesta es parcial (límite de tokens).")).toBeInTheDocument();
    expect(screen.getByText("2 registros que leyó este turno contenían texto con aspecto de instrucciones inyectadas. Trata la respuesta con cuidado.")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "revísala" });
    expect(link).toHaveAttribute("href", "/assistant/actions/act-1");
    expect(container).toHaveTextContent("Hay una acción preparada que espera confirmación: revísala. No se ha ejecutado nada.");
    expectNoEnglish(container);
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("This answer is partial (token_budget).")).toBeInTheDocument();
    expect(container).toHaveTextContent("An action is prepared and awaits confirmation: review it. Nothing has run.");
  });

  it("agrees the flagged count of one, and names a stop reason the console has not met", async () => {
    vi.useRealTimers();
    baseApi({
      createCopilotSession: vi.fn().mockResolvedValue({ id: "s1" }),
      createCopilotTurn: vi.fn().mockResolvedValue({ ...TURN, flagged: [{ source: "x", pattern: "y" }], proposed: [], stop_reason: "stop_nuevo" }),
    });
    const user = userEvent.setup();
    renderAt(<TicketCopilotPanel ticketId="t1" />, "/x");
    await user.type(screen.getByLabelText("Pregunta al copiloto"), "hola");
    await user.click(screen.getByRole("button", { name: "Preguntar" }));
    expect(await screen.findByText("1 registro que leyó este turno contenía texto con aspecto de instrucciones inyectadas. Trata la respuesta con cuidado.")).toBeInTheDocument();
    // A reason outside the table is never printed raw: it reads as a derived label.
    expect(screen.getByText("Esta respuesta es parcial (stop nuevo).")).toBeInTheDocument();
  });

  it("tells the failure in Spanish", async () => {
    vi.useRealTimers();
    baseApi({ createCopilotSession: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    const user = userEvent.setup();
    const { container } = renderAt(<TicketCopilotPanel ticketId="t1" />, "/x");
    await user.type(screen.getByLabelText("Pregunta al copiloto"), "hola");
    await user.click(screen.getByRole("button", { name: "Preguntar" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(describeSubmitError(new ApiError(503, "")));
    expectNoEnglish(container);
    // The failure is held as the caught error, so a switch re-words the sentence.
    const spanish = alert.textContent;
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert").textContent).not.toBe(spanish);
    expect(screen.getByRole("alert")).toHaveTextContent(describeSubmitError(new ApiError(503, "")));
    act(() => setActiveLanguage("es"));
    expect(screen.getByRole("alert").textContent).toBe(spanish);
  });
});

// ------------------------------------------------------------------- admin

describe("the service desk settings in Spanish", () => {
  it("words the queues, the categories and the saved replies", async () => {
    baseApi({ listCannedResponses: vi.fn().mockResolvedValue(CANNED) });
    const { container } = renderAt(<ServiceDeskAdminPage />, "/service-desk/admin");
    expect(await screen.findByRole("heading", { name: "Configuración de la mesa de ayuda" })).toBeInTheDocument();
    expect(screen.getByText("Colas, categorías y respuestas guardadas: adónde van los tickets y cómo los responde tu equipo.")).toBeInTheDocument();
    // Queues.
    expect(await screen.findByText("Dónde llega un ticket y quién se hace cargo.")).toBeInTheDocument();
    expect(await screen.findByText("Soporte general")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Nueva cola" })).toBeInTheDocument();
    // Categories.
    expect(screen.getByText("De qué trata un ticket y la cola a la que va de forma predeterminada.")).toBeInTheDocument();
    expect(await screen.findByText("Equipos")).toBeInTheDocument();
    expect(screen.getByText(/· va a Soporte general/)).toBeInTheDocument();
    expect(screen.getByText("Obligatoria")).toBeInTheDocument();
    expect(screen.getByText("No obligatoria")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Nueva categoría" })).toBeInTheDocument();
    for (const header of ["Nombre", "Estado", "Tipo", "Aprobación"]) {
      expect(screen.getAllByRole("columnheader", { name: header }).length).toBeGreaterThan(0);
    }
    expect(screen.getAllByText("Activo").length).toBe(2);
    expect(screen.getAllByText("Inactivo").length).toBe(2);
    // Saved replies.
    expect(await screen.findByText("Respuestas guardadas")).toBeInTheDocument();
    expect(screen.getByText("Respuestas que tu equipo da a menudo, listas para insertar en un mensaje.")).toBeInTheDocument();
    expect(await screen.findByText("Reiniciar primero")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Editar Reiniciar primero" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Eliminar Reiniciar primero" })).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("says each list is empty", async () => {
    baseApi({
      listTicketQueues: vi.fn().mockResolvedValue([]),
      listTicketCategories: vi.fn().mockResolvedValue([]),
      listCannedResponses: vi.fn().mockResolvedValue([]),
    });
    const { container } = renderAt(<ServiceDeskAdminPage />, "/service-desk/admin");
    expect(await screen.findByText("Todavía no hay colas.")).toBeInTheDocument();
    expect(await screen.findByText("Todavía no hay categorías.")).toBeInTheDocument();
    expect(await screen.findByText("Todavía no hay respuestas guardadas.")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("edits a queue inline with Spanish controls", async () => {
    vi.useRealTimers();
    baseApi();
    const user = userEvent.setup();
    const { container } = renderAt(<ServiceDeskAdminPage />, "/service-desk/admin");
    const row = (await screen.findByText("Soporte general")).closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "Editar" }));
    expect(within(row).getByLabelText("Nombre de la cola")).toBeInTheDocument();
    expect(within(row).getByLabelText("Cola activa")).toBeInTheDocument();
    expect(within(row).getByText("Activa")).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Guardar" })).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Cancelar" })).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("refuses an empty queue, an empty category and an empty saved reply, and re-words each on a switch", async () => {
    vi.useRealTimers();
    baseApi();
    const user = userEvent.setup();
    renderAt(<ServiceDeskAdminPage />, "/service-desk/admin");
    await screen.findByText("Soporte general");

    await user.click(screen.getByRole("button", { name: "Nueva cola" }));
    let dialog = await screen.findByRole("dialog", { name: "Nueva cola" });
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("El campo Clave es obligatorio. El campo Nombre es obligatorio.");
    expectNoEnglish(dialog);
    act(() => setActiveLanguage("en"));
    expect(within(screen.getByRole("dialog", { name: "New queue" })).getByRole("alert")).toHaveTextContent("Key is required. Name is required.");
    act(() => setActiveLanguage("es"));
    await user.click(within(screen.getByRole("dialog", { name: "Nueva cola" })).getByRole("button", { name: "Cancelar" }));

    await user.click(screen.getByRole("button", { name: "Nueva categoría" }));
    dialog = await screen.findByRole("dialog", { name: "Nueva categoría" });
    expect(within(dialog).getByText("Cola predeterminada")).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: "Ninguna" })).toBeInTheDocument();
    await user.selectOptions(within(dialog).getByLabelText("Tipo"), "request");
    expect(within(dialog).getAllByText("Requiere aprobación").length).toBe(2);
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("El campo Clave es obligatorio. El campo Nombre es obligatorio.");
    expectNoEnglish(dialog);
    await user.click(within(dialog).getByRole("button", { name: "Cancelar" }));

    await user.click(screen.getByRole("button", { name: "Nueva respuesta guardada" }));
    dialog = await screen.findByRole("dialog", { name: "Nueva respuesta guardada" });
    expect(within(dialog).getByPlaceholderText("Cómo la llama tu equipo, p. ej. Reiniciar primero")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("El campo Nombre es obligatorio. El campo Texto de la respuesta es obligatorio.");
    expectNoEnglish(dialog);
  });

  it("confirms deleting a saved reply in Spanish", async () => {
    vi.useRealTimers();
    baseApi({ listCannedResponses: vi.fn().mockResolvedValue(CANNED) });
    const user = userEvent.setup();
    renderAt(<ServiceDeskAdminPage />, "/service-desk/admin");
    await user.click(await screen.findByRole("button", { name: "Eliminar Reiniciar primero" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Eliminar esta respuesta guardada")).toBeInTheDocument();
    expect(within(dialog).getByText("Desaparece del cuadro de respuesta de todo el mundo. Las respuestas ya enviadas no cambian.")).toBeInTheDocument();
    expectNoEnglish(dialog);
  });

  it("re-words the page on a switch", async () => {
    baseApi();
    renderAt(<ServiceDeskAdminPage />, "/service-desk/admin");
    await screen.findByText("Soporte general");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("heading", { name: "Service desk settings" })).toBeInTheDocument();
    expect(screen.getByText("Where a ticket lands, and who picks it up.")).toBeInTheDocument();
    expect(screen.getByText("Required")).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    expect(screen.getByRole("heading", { name: "Configuración de la mesa de ayuda" })).toBeInTheDocument();
  });
});

// ------------------------------------------------- outside React: label tables

describe("labels and sentences built outside React", () => {
  it("words ticket types, statuses and priorities, and never prints an unknown word raw", () => {
    setActiveLanguage("es");
    expect(ticketTypeLabel("incident")).toBe("Incidente");
    expect(ticketTypeLabel("request")).toBe("Solicitud");
    expect(ticketPriorityLabel("urgent")).toBe("Urgente");
    expect(ticketPriorityLabel("low")).toBe("Baja");
    expect(ticketStatusLabel("pending_approval")).toBe("Pendiente de aprobación");
    expect(ticketStatusLabel("fulfilled")).toBe("Completado");
    expect(ticketStatusLabel("on_hold")).toBe("On hold");
    expect(ticketStatusLabel("on_hold")).not.toBe("on_hold");
    expect(ticketTypeLabel("problem")).toBe("Problem");
    expect(ticketPriorityLabel("critical")).toBe("Crítico");
    act(() => setActiveLanguage("en"));
    expect(ticketTypeLabel("incident")).toBe("Incident");
    expect(ticketStatusLabel("pending_approval")).toBe("Pending approval");
    expect(ticketPriorityLabel("normal")).toBe("Normal");
  });

  it("words the deadline of a ticket, and re-words it on a switch", () => {
    setActiveLanguage("es");
    const now = NOW;
    expect(describeDue(null, now)).toMatchObject({ label: "Sin SLA" });
    expect(describeDue(sla({ paused: true }), now)).toMatchObject({ label: "En pausa" });
    expect(describeDue(sla({ next_due_at: null, breached: true }), now)).toMatchObject({ label: "Incumplido" });
    expect(describeDue(sla({ next_due_at: null, response_state: "void" }), now)).toMatchObject({ label: "Sin plazo" });
    expect(describeDue(sla({ next_due_at: null }), now)).toMatchObject({ label: "Cumplido" });
    expect(describeDue(sla({ next_due_at: at(150), next_due_target: "resolution" }), now)).toEqual({
      label: "Vence en 2 h 30 min",
      detail: "Resolución",
      variant: "muted",
    });
    expect(describeDue(sla({ next_due_at: at(-5) }), now).label).toBe("Con 5 min de retraso");
    expect(describeDue(sla({ next_due_at: at(-5), breached: true }), now).label).toBe("Incumplido hace 5 min");
    expect(describeTarget("met", at(0), now).label).toBe("Cumplido");
    expect(describeTarget("none", at(0), now).label).toBe("Sin SLA");
    expect(describeTarget("running", at(-26 * 60), now).label).toBe("Con 1 d 2 h de retraso");
    expect(shortDuration(20 * 1000)).toBe("menos de un minuto");
    act(() => setActiveLanguage("en"));
    expect(describeDue(sla({ next_due_at: at(150), next_due_target: "resolution" }), now)).toMatchObject({
      label: "Due in 2 h 30 min",
      detail: "Resolution",
    });
    expect(shortDuration(20 * 1000)).toBe("under a minute");
  });

  it("words the warning before a status change in both languages", () => {
    setActiveLanguage("es");
    expect(statusChangeWarning("rejected")).toEqual({
      title: "Pasar este ticket a «Rechazado»",
      body: "Nada sale del estado «Rechazado». Este ticket no se podrá reabrir, comentar ni editar nunca más. La única forma de volver atrás es crear un ticket nuevo.",
      destructive: true,
    });
    expect(statusChangeWarning("closed")?.title).toBe("Cerrar este ticket");
    expect(statusChangeWarning("resolved")).toBeNull();
    act(() => setActiveLanguage("en"));
    expect(statusChangeWarning("rejected")?.title).toBe("Move this ticket to rejected");
  });

  it("words the picker hints and the sublabels of people and devices", () => {
    setActiveLanguage("es");
    expect(userPickerEmptyHint()).toMatch(/^No hay personas entre las que elegir/);
    expect(devicePickerEmptyHint()).toMatch(/Dispositivos → Añadir un equipo/);
    const users = userPickerOptions(USERS as never);
    expect(users.map((u) => u.sublabel)).toEqual(["t@example.com", "r@example.com", "o@example.com · deshabilitado"]);
    const devices = devicePickerOptions(DEVICES.items as unknown as Device[]);
    expect(devices.map((d) => d.sublabel)).toEqual(["windows · en línea", "linux · sin agente"]);
    act(() => setActiveLanguage("en"));
    expect(userPickerOptions(USERS as never)[2]?.sublabel).toBe("o@example.com · disabled");
    expect(devicePickerOptions(DEVICES.items as unknown as Device[]).map((d) => d.sublabel)).toEqual(["windows · online", "linux · no agent"]);
    expect(userPickerEmptyHint()).toMatch(/^No people to choose from/);
  });

  it("reads a call to action through the catalogue each time it is read", () => {
    setActiveLanguage("es");
    expect(SET_TARGETS_CTA.label).toBe("Definir plazos de respuesta");
    act(() => setActiveLanguage("en"));
    expect(SET_TARGETS_CTA.label).toBe("Set response targets");
  });

  it("words the nav strip, and hides Admin from a technician", () => {
    baseApi();
    const client = new QueryClient();
    renderInSpanish(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/service-desk"]}>
          <ServiceDeskNav canRead canAdmin={false} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const nav = screen.getByRole("navigation", { name: "Mesa de ayuda" });
    expect(within(nav).getAllByRole("link").map((l) => l.textContent)).toEqual(["Tickets", "Mi trabajo"]);
    act(() => setActiveLanguage("en"));
    expect(within(screen.getByRole("navigation", { name: "Service desk" })).getAllByRole("link").map((l) => l.textContent)).toEqual(["Tickets", "My work"]);
    fireEvent.click(screen.getByRole("link", { name: "My work" }));
  });
});
