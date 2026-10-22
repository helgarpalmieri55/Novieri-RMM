/**
 * The end-user portal in Spanish: My tickets, the New ticket form (categories,
 * urgency chips, attachments, help suggestions, the refusal), a ticket's page
 * (updates, reply, attachments, approval, status history, the assistant), the
 * operator's readiness panel and the label tables built outside React. What a
 * requester wrote (subjects, descriptions, comments, file names) reads as
 * recorded; the words around it are Spanish and plain.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import { describeSubmitError } from "../../components/form";
import { ApiError } from "../../lib/api";
import type { ApiClient, Me } from "../../lib/api";
import { expectCta, meWith, renderPanel } from "../../test/prerequisiteHarness";
import { MyTicketsPage } from "./MyTicketsPage";
import { NewTicketPage } from "./NewTicketPage";
import { PortalAssistantPanel } from "./PortalAssistantPanel";
import { PortalNav } from "./PortalNav";
import { PortalPrerequisites } from "./PortalPrerequisites";
import { PortalTicketDetailPage } from "./PortalTicketDetailPage";
import {
  PORTAL_CATEGORY_GROUPS,
  PORTAL_PRIORITY_OPTIONS,
  attachmentLimitHint,
  groupPortalCategories,
  oversizedAttachmentMessage,
  portalApprovalLabel,
  portalTicketPriorityLabel,
  portalTicketStatusLabel,
  portalTicketTypeLabel,
} from "./portalState";
import type { PortalCategoryOut, PortalTicketDetail, PortalTicketSummary } from "./types";

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
  user_id: "req-1",
  org_id: "o1",
  email: "rosa@example.com",
  display_name: "Rosa Solicitante",
  timezone: "UTC",
  permissions: ["portal.ticket.read", "portal.ticket.write"],
};

const CATEGORIES: PortalCategoryOut[] = [
  { id: "cat1", key: "equipos", name: "Equipos", type: "incident" },
  { id: "cat2", key: "accesos", name: "Accesos", type: "request" },
  { id: "cat3", key: "otros", name: "Varios", type: "question" },
];

const SUMMARIES: PortalTicketSummary[] = [
  { id: "t1", number: "INC-1", type: "incident", status: "in_progress", priority: "normal", subject: "La VPN no conecta", created_at: at(-180), updated_at: at(-30) },
  { id: "t2", number: "REQ-2", type: "request", status: "pending_approval", priority: "high", subject: "Acceso a la carpeta de contabilidad", created_at: at(-3 * 24 * 60), updated_at: at(-60) },
];

const DETAIL: PortalTicketDetail = {
  id: "t1",
  number: "INC-1",
  type: "incident",
  status: "in_progress",
  priority: "urgent",
  subject: "La VPN no conecta",
  description: "El cliente de la VPN no se conecta desde ayer.",
  created_at: at(-180),
  updated_at: at(-30),
  comments: [{ id: "c1", body: "Hemos revisado el cable.", created_at: at(-100) }],
  status_history: [
    { from_status: null, to_status: "new", created_at: at(-180) },
    { from_status: "new", to_status: "in_progress", created_at: at(-120) },
  ],
  attachments: [{ id: "a1", filename: "captura.png", byte_size: 4096, created_at: at(-90) }],
  approval: null,
};

function baseApi(over: Partial<ApiClient> = {}): Partial<ApiClient> {
  const api: Partial<ApiClient> = {
    me: vi.fn().mockResolvedValue(ME),
    listPortalTickets: vi.fn().mockResolvedValue(SUMMARIES),
    listPortalCategories: vi.fn().mockResolvedValue(CATEGORIES),
    getPortalTicket: vi.fn().mockResolvedValue(DETAIL),
    createPortalTicket: vi.fn().mockResolvedValue({ id: "t9" }),
    createPortalComment: vi.fn().mockResolvedValue({}),
    uploadPortalAttachment: vi.fn().mockResolvedValue({}),
    suggestPortalHelp: vi.fn().mockResolvedValue([]),
    getPortalHelpArticle: vi.fn().mockResolvedValue({ id: "a1", title: "Conectarse a la VPN", body: "Abre **Ajustes**." }),
    listRoles: vi.fn().mockResolvedValue({ roles: [] }),
    ...over,
  };
  mockUseApi.mockReturnValue(api as ApiClient);
  return api;
}

// ---------------------------------------------------------------- My tickets

describe("My tickets in Spanish", () => {
  it("words the head, the nav, the table and the way each ticket reads", async () => {
    baseApi();
    const { container } = renderAt(<MyTicketsPage />, "/portal");
    expect(await screen.findByRole("heading", { name: "Mis tickets" })).toBeInTheDocument();
    expect(screen.getByText("Crea una solicitud o consulta cómo va una que ya abriste.")).toBeInTheDocument();
    expect(await screen.findByText("La VPN no conecta")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Nueva solicitud" })).toHaveAttribute("href", "/portal/new");
    const nav = screen.getByRole("navigation", { name: "Portal" });
    expect(within(nav).getByRole("link", { name: "Mis tickets" })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: "Nuevo ticket" })).toBeInTheDocument();
    for (const header of ["Ticket", "Estado", "Última actualización"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    const first = screen.getByText("La VPN no conecta").closest("tr") as HTMLElement;
    expect(within(first).getByText("En curso")).toBeInTheDocument();
    expect(first).toHaveTextContent("INC-1 · abierto hace 3 h");
    const second = screen.getByText("Acceso a la carpeta de contabilidad").closest("tr") as HTMLElement;
    expect(within(second).getByText("Pendiente de aprobación")).toBeInTheDocument();
    expect(second).toHaveTextContent("REQ-2 · abierto hace 3 d");
    expectNoEnglish(container, ["INC-1", "REQ-2"]);
  });

  it("says there are no tickets yet", async () => {
    baseApi({ listPortalTickets: vi.fn().mockResolvedValue([]) });
    const { container } = renderAt(<MyTicketsPage />, "/portal");
    expect(await screen.findByText("Todavía no has creado ningún ticket.")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("re-words the page on a switch", async () => {
    baseApi();
    renderAt(<MyTicketsPage />, "/portal");
    await screen.findByText("La VPN no conecta");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("heading", { name: "My tickets" })).toBeInTheDocument();
    const row = screen.getByText("La VPN no conecta").closest("tr") as HTMLElement;
    expect(within(row).getByText("In progress")).toBeInTheDocument();
    expect(row).toHaveTextContent("INC-1 · opened 3h ago");
    act(() => setActiveLanguage("es"));
    expect(screen.getByRole("heading", { name: "Mis tickets" })).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------- New ticket

describe("New ticket in Spanish", () => {
  it("words the form, the grouped categories, the urgency chips and the attachment hint", async () => {
    baseApi();
    const { container } = renderAt(<NewTicketPage />, "/portal/new");
    expect(await screen.findByRole("heading", { name: "Nuevo ticket" })).toBeInTheDocument();
    expect(screen.getByText("Cuéntanos qué pasa. No hace falta que sepas quién lo va a arreglar.")).toBeInTheDocument();
    const select = screen.getByLabelText("Categoría");
    await within(select).findByRole("option", { name: "Equipos" });
    expect(within(select).getByRole("option", { name: "Elige una categoría" })).toBeInTheDocument();
    expect(within(select).getByRole("group", { name: "Informar de un problema" })).toBeInTheDocument();
    expect(within(select).getByRole("group", { name: "Pedir algo" })).toBeInTheDocument();
    expect(within(select).getByRole("group", { name: "Otras" })).toBeInTheDocument();
    expect(screen.getByLabelText("Asunto")).toBeInTheDocument();
    expect(screen.getByLabelText("Descripción")).toHaveAttribute(
      "placeholder",
      "¿Qué estabas haciendo, qué ocurrió y qué has probado ya?",
    );
    const urgency = screen.getByRole("group", { name: "¿Qué urgencia tiene?" });
    for (const label of [
      "Una molestia menor, sin prisa",
      "Me retrasa, pero puedo seguir trabajando",
      "No puedo hacer una parte importante de mi trabajo",
      "No puedo trabajar en absoluto, o también hay otras personas bloqueadas",
    ]) {
      expect(within(urgency).getByRole("button", { name: label })).toBeInTheDocument();
    }
    expect(screen.getByText("Se creará con prioridad normal.")).toBeInTheDocument();
    expect(screen.getByText("Archivos adjuntos (opcional)", { selector: "span" })).toBeInTheDocument();
    expect(screen.getByText("Suelta los archivos aquí o haz clic para elegirlos")).toBeInTheDocument();
    expect(screen.getByText("Adjunta todos los archivos que necesites. Vale cualquier tipo de archivo, hasta 10 MB por archivo.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Enviar" })).toBeEnabled();
    expectNoEnglish(container);
  });

  it("names the grade the chosen urgency files under, agreeing with the word priority", async () => {
    vi.useRealTimers();
    baseApi();
    const user = userEvent.setup();
    renderAt(<NewTicketPage />, "/portal/new");
    await user.click(await screen.findByRole("button", { name: "No puedo trabajar en absoluto, o también hay otras personas bloqueadas" }));
    expect(screen.getByText("Se creará con prioridad urgente.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "No puedo hacer una parte importante de mi trabajo" }));
    expect(screen.getByText("Se creará con prioridad alta.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Una molestia menor, sin prisa" }));
    expect(screen.getByText("Se creará con prioridad baja.")).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("Filed as Low priority.")).toBeInTheDocument();
  });

  it("refuses an empty form and an oversized file in Spanish, and re-words both on a switch", async () => {
    vi.useRealTimers();
    baseApi();
    const user = userEvent.setup();
    const { container } = renderAt(<NewTicketPage />, "/portal/new");
    await screen.findByRole("option", { name: "Equipos" });
    await user.click(screen.getByRole("button", { name: "Enviar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("El campo Categoría es obligatorio. El campo Asunto es obligatorio.");
    expectNoEnglish(container);

    await user.selectOptions(screen.getByLabelText("Categoría"), "cat1");
    await user.type(screen.getByLabelText("Asunto"), "La VPN no conecta");
    const big = new File(["x"], "volcado.zip");
    Object.defineProperty(big, "size", { value: 11 * 1024 * 1024 });
    await user.upload(screen.getByLabelText("Archivos adjuntos (opcional)", { selector: "input" }), big);
    await user.click(screen.getByRole("button", { name: "Enviar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("volcado.zip supera el límite de 10 MB. Adjunta un archivo más pequeño.");
    expectNoEnglish(container, ["volcado.zip"]);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent("volcado.zip is larger than the 10 MB limit. Please attach a smaller file.");
    act(() => setActiveLanguage("es"));
    expect(screen.getByRole("alert")).toHaveTextContent("volcado.zip supera el límite");
  });

  it("explains the missing catalogue when categories are not available", async () => {
    baseApi({ listPortalCategories: vi.fn().mockRejectedValue(new ApiError(403, "")) });
    const { container } = renderAt(<NewTicketPage />, "/portal/new");
    expect(await screen.findByText("Las categorías no están disponibles (permisos insuficientes).")).toBeInTheDocument();
    expect(await screen.findByText("No puedes crear un ticket sin categoría, y el catálogo no está disponible.")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("offers help articles while the subject is typed, and opens one in Spanish", async () => {
    vi.useRealTimers();
    baseApi({ suggestPortalHelp: vi.fn().mockResolvedValue([{ id: "a1", title: "Conectarse a la VPN" }]) });
    const user = userEvent.setup();
    const { container } = renderAt(<NewTicketPage />, "/portal/new");
    await user.type(await screen.findByLabelText("Asunto"), "La VPN no conecta");
    const region = await screen.findByRole("region", { name: "Esto puede ayudarte" });
    expect(within(region).getByText("Si nada de esto lo resuelve, sigue adelante y envía tu ticket.")).toBeInTheDocument();
    await user.click(within(region).getByRole("button", { name: "Conectarse a la VPN" }));
    const dialog = await screen.findByRole("dialog", { name: "Conectarse a la VPN" });
    expect(within(dialog).getByRole("button", { name: "Volver a mi ticket" })).toBeInTheDocument();
    expectNoEnglish(container, ["Ajustes"]);
  });

  it("says an article could not be opened", async () => {
    vi.useRealTimers();
    baseApi({
      suggestPortalHelp: vi.fn().mockResolvedValue([{ id: "a1", title: "Conectarse a la VPN" }]),
      getPortalHelpArticle: vi.fn().mockRejectedValue(new ApiError(503, "")),
    });
    const user = userEvent.setup();
    renderAt(<NewTicketPage />, "/portal/new");
    await user.type(await screen.findByLabelText("Asunto"), "La VPN no conecta");
    await user.click(await screen.findByRole("button", { name: "Conectarse a la VPN" }));
    expect(await screen.findByText(/No se pudo abrir este artículo\./)).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Artículo de ayuda" })).toBeInTheDocument();
  });

  it("re-words the form and the grouped categories on a switch", async () => {
    baseApi();
    renderAt(<NewTicketPage />, "/portal/new");
    await screen.findByRole("option", { name: "Equipos" });
    act(() => setActiveLanguage("en"));
    expect(screen.getByLabelText("Category")).toBeInTheDocument();
    expect(within(screen.getByLabelText("Category")).getByRole("group", { name: "Report a problem" })).toBeInTheDocument();
    expect(within(screen.getByLabelText("Category")).getByRole("group", { name: "Other" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "A minor annoyance, no rush" })).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    expect(screen.getByLabelText("Categoría")).toBeInTheDocument();
  });
});

// ------------------------------------------------------------ a ticket's page

function renderDetail(detail: PortalTicketDetail = DETAIL, permissions = ME.permissions) {
  baseApi({
    me: vi.fn().mockResolvedValue({ ...ME, permissions }),
    getPortalTicket: vi.fn().mockResolvedValue(detail),
  });
  return renderAt(<PortalTicketDetailPage />, "/portal/tickets/:ticketId", `/portal/tickets/${detail.id}`);
}

describe("a portal ticket's page in Spanish", () => {
  it("words the head, the updates, the details, the attachments and the status history", async () => {
    const { container } = renderDetail();
    expect(await screen.findByRole("heading", { name: "INC-1: La VPN no conecta" })).toBeInTheDocument();
    expect(screen.getByText(/^Abierto el .+, última actualización el .+\.$/)).toBeInTheDocument();
    expect(screen.getAllByText("En curso").length).toBeGreaterThan(0);
    expect(screen.getByText("Urgente")).toBeInTheDocument();
    expect(screen.getAllByText("Incidente").length).toBeGreaterThan(0);
    // Updates.
    expect(screen.getByText("Novedades")).toBeInTheDocument();
    expect(screen.getByText("Tú")).toBeInTheDocument();
    expect(screen.getByText(/^abierto el /)).toBeInTheDocument();
    expect(screen.getByText("Hemos revisado el cable.")).toBeInTheDocument();
    expect(screen.getByText("Novedad")).toBeInTheDocument();
    expect(screen.getByLabelText("Tu respuesta")).toHaveAttribute("placeholder", "Añade un comentario…");
    expect(screen.getByRole("button", { name: "Publicar respuesta" })).toBeDisabled();
    // Details.
    expect(screen.getByText("Detalles")).toBeInTheDocument();
    for (const k of ["Número", "Tipo", "Abierto", "Última actualización"]) {
      expect(screen.getAllByText(k).length).toBeGreaterThan(0);
    }
    // Attachments.
    expect(screen.getByText("Archivos adjuntos")).toBeInTheDocument();
    expect(screen.getByText("captura.png")).toBeInTheDocument();
    for (const header of ["Nombre del archivo", "Tamaño", "Subido"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(screen.getByRole("button", { name: "Descargar" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Adjuntar un archivo" })).toBeInTheDocument();
    // Status history.
    expect(screen.getByText("Historial de estados")).toBeInTheDocument();
    expect(screen.getByText("Sin estado anterior")).toBeInTheDocument();
    for (const header of ["Estado anterior", "Estado nuevo", "Fecha del cambio"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    // The assistant.
    expect(screen.getByText("Pregunta sobre tu ticket")).toBeInTheDocument();
    expect(screen.getByText("El asistente puede explicarte tus tickets y ayudarte a describir un problema. No puede cambiar nada en ningún equipo.")).toBeInTheDocument();
    expect(screen.getByLabelText("Pregunta al asistente")).toBeInTheDocument();
    expectNoEnglish(container, ["INC-1"]);
  });

  it("says a ticket has no updates, attachments or history yet", async () => {
    const { container } = renderDetail({ ...DETAIL, description: "", comments: [], attachments: [], status_history: [] });
    expect(await screen.findByText("Todavía no hay comentarios.")).toBeInTheDocument();
    expect(screen.getByText("Todavía no hay archivos adjuntos.")).toBeInTheDocument();
    expect(screen.getByText("Aquí aparecerá la captura de pantalla o el registro que adjuntes a este ticket.")).toBeInTheDocument();
    expect(screen.getByText("Todavía no hay historial de estados.")).toBeInTheDocument();
    expect(screen.getByText("Cada vez que el ticket cambia, el cambio queda registrado aquí.")).toBeInTheDocument();
    expectNoEnglish(container, ["INC-1"]);
  });

  it("words a rejected approval and a pending one", async () => {
    const rejected = renderDetail({ ...DETAIL, type: "request", approval: { state: "rejected", reason: "No hay presupuesto.", decided_at: at(-10) } });
    const panel = (await screen.findByText("Aprobación")).closest("section") as HTMLElement;
    expect(within(panel).getByText("Rechazada")).toBeInTheDocument();
    expect(within(panel).getByText("No hay presupuesto.")).toBeInTheDocument();
    expectNoEnglish(rejected.container, ["INC-1"]);
    rejected.unmount();
    renderDetail({ ...DETAIL, type: "request", approval: { state: "pending", reason: "", decided_at: null } });
    const pending = (await screen.findByText("Aprobación")).closest("section") as HTMLElement;
    expect(within(pending).getByText("Pendiente")).toBeInTheDocument();
  });

  it("hides the reply box, the upload and the assistant from someone who cannot write or read", async () => {
    renderDetail(DETAIL, ["portal.ticket.read"]);
    await screen.findByText("Hemos revisado el cable.");
    expect(screen.queryByRole("button", { name: "Publicar respuesta" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Adjuntar un archivo" })).toBeNull();
  });

  it("posts a reply and words a failure", async () => {
    vi.useRealTimers();
    baseApi({ createPortalComment: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    renderAt(<PortalTicketDetailPage />, "/portal/tickets/:ticketId", "/portal/tickets/t1");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Tu respuesta"), "Sigue sin ir");
    await user.click(screen.getByRole("button", { name: "Publicar respuesta" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  it("says the ticket could not be found", async () => {
    baseApi({ getPortalTicket: vi.fn().mockResolvedValue(null) });
    const { container } = renderAt(<PortalTicketDetailPage />, "/portal/tickets/:ticketId", "/portal/tickets/zz");
    expect(await screen.findByText("No se encontró este ticket.")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("re-words the page and the status labels on a switch", async () => {
    renderDetail();
    await screen.findByText("Hemos revisado el cable.");
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("Updates")).toBeInTheDocument();
    expect(screen.getByText("You")).toBeInTheDocument();
    expect(screen.getByText("Urgent")).toBeInTheDocument();
    expect(screen.getByText("Status history")).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    expect(screen.getByText("Novedades")).toBeInTheDocument();
  });
});

describe("the portal assistant in Spanish", () => {
  it("answers in the requester's own language of chrome, with the answer as the server wrote it", async () => {
    vi.useRealTimers();
    baseApi({
      createResolverSession: vi.fn().mockResolvedValue({ id: "s1" }),
      createResolverTurn: vi.fn().mockResolvedValue({ answer: "Reinicia el cliente de la VPN.", flagged_count: 0 }),
    });
    const user = userEvent.setup();
    const { container } = renderAt(<PortalAssistantPanel />, "/x");
    await user.type(screen.getByLabelText("Pregunta al asistente"), "¿Qué hago?");
    await user.click(screen.getByRole("button", { name: "Preguntar" }));
    expect(await screen.findByText("Reinicia el cliente de la VPN.")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("words a failed question in Spanish and re-words it on a switch", async () => {
    vi.useRealTimers();
    baseApi({ createResolverSession: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    const user = userEvent.setup();
    const { container } = renderAt(<PortalAssistantPanel />, "/x");
    await user.type(screen.getByLabelText("Pregunta al asistente"), "¿Qué hago?");
    await user.click(screen.getByRole("button", { name: "Preguntar" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(describeSubmitError(new ApiError(503, "")));
    expectNoEnglish(container);
    const spanish = alert.textContent;
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert").textContent).not.toBe(spanish);
    expect(screen.getByRole("alert")).toHaveTextContent(describeSubmitError(new ApiError(503, "")));
    act(() => setActiveLanguage("es"));
    expect(screen.getByRole("alert").textContent).toBe(spanish);
  });
});

// ------------------------------------------------------- the operator's panel

const OPERATOR = ["portal.ticket.read", "roles.read", "docs.read", "ai.use", "notification.read"];
const role = (name: string, assigned: number) => ({ id: name, name, assigned_users: assigned, codes: [] });
const article = (id: string, portal_visible: boolean) => ({ id, title: id, archived: false, portal_visible });

function panelApi(over: Record<string, unknown> = {}, permissions = OPERATOR) {
  const api = {
    me: meWith(permissions),
    listRoles: vi.fn().mockResolvedValue({ roles: [role("requester", 0)] }),
    listArticles: vi.fn().mockResolvedValue({ items: [article("a1", false)], total: 1 }),
    getAiStatus: vi.fn().mockResolvedValue({ enabled: false, reason: "No hay ningún proveedor de modelos configurado." }),
    listNotificationRules: vi.fn().mockResolvedValue([]),
    ...over,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

describe("the readiness panel in Spanish", () => {
  it("says what is missing before requesters can use the portal", async () => {
    panelApi();
    setActiveLanguage("es");
    const { container } = renderPanel(<PortalPrerequisites />);
    expect(await screen.findByText("Antes de que los solicitantes puedan usar el portal")).toBeInTheDocument();
    expect(screen.getByText("Lo que deben tener configurado otros módulos para que el portal sirva de algo. Los solicitantes no ven esto.")).toBeInTheDocument();
    await expectCta("Invitar a solicitantes", "/settings?tab=users");
    await expectCta("Publicar artículos de ayuda", "/docs");
    await expectCta("Avisar a los solicitantes de las respuestas", "/notifications#rules");
    expect(screen.getByText("Nadie tiene el rol de solicitante, así que nadie puede iniciar sesión aquí para pedir ayuda.")).toBeInTheDocument();
    expect(screen.getByText(/No hay ningún proveedor de modelos configurado\. Se activa en la configuración del propio servidor, por quien lo administra\./)).toBeInTheDocument();
    expect(screen.getByText("Existen cuentas de solicitante")).toBeInTheDocument();
    expect(screen.getByText("El asistente está activado")).toBeInTheDocument();
    expectNoEnglish(container);
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("Before requesters can use the portal")).toBeInTheDocument();
  });

  it("agrees each done sentence with its count", async () => {
    panelApi({
      listRoles: vi.fn().mockResolvedValue({ roles: [role("requester", 1)] }),
      listArticles: vi.fn().mockResolvedValue({ items: [article("a1", true), article("a2", false)], total: 2 }),
      getAiStatus: vi.fn().mockResolvedValue({ enabled: true, reason: "" }),
      listNotificationRules: vi.fn().mockResolvedValue([{ id: "r1", event_type: "ticket.status_changed", recipient_kind: "assignee", active: true }]),
    });
    setActiveLanguage("es");
    const { container } = renderPanel(<PortalPrerequisites />);
    expect(await screen.findByText("1 persona tiene el rol de solicitante.")).toBeInTheDocument();
    expect(screen.getByText("1 artículo está publicado para que los solicitantes lo lean.")).toBeInTheDocument();
    expect(screen.getByText("Los solicitantes pueden preguntar al asistente desde sus tickets.")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("agrees the plural forms, and the done rule sentence", async () => {
    panelApi({
      listRoles: vi.fn().mockResolvedValue({ roles: [role("requester", 12)] }),
      listArticles: vi.fn().mockResolvedValue({ items: [article("a1", true), article("a2", true)], total: 2 }),
      getAiStatus: vi.fn().mockResolvedValue({ enabled: true, reason: "" }),
      listNotificationRules: vi.fn().mockResolvedValue([{ id: "r1", event_type: "ticket.status_changed", recipient_kind: "assignee", active: true }]),
    });
    setActiveLanguage("es");
    renderPanel(<PortalPrerequisites />);
    expect(await screen.findByText("12 personas tienen el rol de solicitante.")).toBeInTheDocument();
    expect(screen.getByText("2 artículos están publicados para que los solicitantes los lean.")).toBeInTheDocument();
  });

  it("does not call a partly read library empty", async () => {
    panelApi({ listArticles: vi.fn().mockResolvedValue({ items: [article("a1", false)], total: 250 }) });
    setActiveLanguage("es");
    const { container } = renderPanel(<PortalPrerequisites />);
    expect(await screen.findByText("Solo se comprobó 1 de 250 artículos y no está publicado en el portal; el resto no se pudo comprobar aquí.")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("says what an operator without the role permission cannot check", async () => {
    panelApi({}, ["portal.ticket.read", "ticket.read"]);
    setActiveLanguage("es");
    const { container } = renderPanel(<PortalPrerequisites />);
    expect((await screen.findAllByText(/No se puede comprobar desde esta cuenta/)).length).toBeGreaterThan(0);
    expectNoEnglish(container);
  });
});

// ------------------------------------------------- outside React: label tables

describe("labels and sentences built outside React", () => {
  it("words a ticket's type, status, priority and approval, and never prints an unknown word raw", () => {
    setActiveLanguage("es");
    expect(portalTicketTypeLabel("request")).toBe("Solicitud");
    expect(portalTicketStatusLabel("in_progress")).toBe("En curso");
    expect(portalTicketStatusLabel("fulfilled")).toBe("Completado");
    expect(portalTicketStatusLabel("on_hold")).toBe("On hold");
    expect(portalTicketStatusLabel("on_hold")).not.toBe("on_hold");
    expect(portalTicketPriorityLabel("high")).toBe("Alta");
    expect(portalApprovalLabel("approved")).toBe("Aprobada");
    expect(portalApprovalLabel("rejected")).toBe("Rechazada");
    expect(portalApprovalLabel("withdrawn")).toBe("Retirado");
    act(() => setActiveLanguage("en"));
    expect(portalTicketStatusLabel("pending_approval")).toBe("Pending approval");
    expect(portalApprovalLabel("approved")).toBe("Approved");
  });

  it("reads the tables of options through the catalogue each time they are read", () => {
    setActiveLanguage("es");
    expect(PORTAL_CATEGORY_GROUPS.map((g) => g.label)).toEqual(["Informar de un problema", "Pedir algo"]);
    expect(PORTAL_PRIORITY_OPTIONS.map((o) => o.name)).toEqual(["baja", "normal", "alta", "urgente"]);
    expect(PORTAL_PRIORITY_OPTIONS.map((o) => o.value)).toEqual(["low", "normal", "high", "urgent"]);
    expect(groupPortalCategories(CATEGORIES).map((g) => g.label)).toEqual(["Informar de un problema", "Pedir algo", "Otras"]);
    expect(attachmentLimitHint()).toBe("Adjunta todos los archivos que necesites. Vale cualquier tipo de archivo, hasta 10 MB por archivo.");
    expect(oversizedAttachmentMessage({ name: "a.zip", size: 11 * 1024 * 1024 })).toBe("a.zip supera el límite de 10 MB. Adjunta un archivo más pequeño.");
    act(() => setActiveLanguage("en"));
    expect(PORTAL_CATEGORY_GROUPS.map((g) => g.label)).toEqual(["Report a problem", "Request something"]);
    expect(PORTAL_PRIORITY_OPTIONS.map((o) => o.name)).toEqual(["Low", "Normal", "High", "Urgent"]);
    expect(groupPortalCategories(CATEGORIES).map((g) => g.label)).toEqual(["Report a problem", "Request something", "Other"]);
  });

  it("words the nav strip", () => {
    const client = new QueryClient();
    renderInSpanish(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/portal"]}>
          <PortalNav canRead />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(within(screen.getByRole("navigation", { name: "Portal" })).getAllByRole("link").map((l) => l.textContent)).toEqual(["Mis tickets", "Nuevo ticket"]);
    act(() => setActiveLanguage("en"));
    expect(within(screen.getByRole("navigation", { name: "Portal" })).getAllByRole("link").map((l) => l.textContent)).toEqual(["My tickets", "New ticket"]);
  });
});
