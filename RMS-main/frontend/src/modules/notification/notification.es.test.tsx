/**
 * Notifications in Spanish: the admin page (tiles and what each figure is made
 * of, rules, channels, the allowlist, queue members, the delivery log), the
 * dialogs and their refusals, the prerequisites strip, and the label tables and
 * sentences built outside React (event names, recipients, delivery statuses and
 * the reasons a delivery did not happen). Channel names, hosts, URLs,
 * recipients, queue names and the server's own error excerpts read as recorded;
 * the words around them are Spanish.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { en } from "../../i18n/en";
import { es } from "../../i18n/es";
import { setActiveLanguage } from "../../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import { ApiError } from "../../lib/api";
import type { ApiClient, Me } from "../../lib/api";
import { expectCta, meWith, renderPanel } from "../../test/prerequisiteHarness";
import { signInProblem, signInProblemKind } from "./EmailSignInFields";
import type { EmailSignIn } from "./EmailSignInFields";
import { EVENT_TYPE_LABELS, RECIPIENT_KIND_LABELS, eventTypeLabel, recipientKindLabel } from "./labels";
import { NotificationAdminPage } from "./NotificationAdminPage";
import { NotificationPrerequisites } from "./NotificationPrerequisites";
import { channelKindLabel, deliveryErrorMessage, deliveryLastErrorMessage, deliveryStatusLabel } from "./notificationState";
import { EVENT_TYPES } from "./types";
import type { AllowlistEntryOut, ChannelOut, DeliveryOut, QueueMemberOut, RuleOut } from "./types";
import type { QueueOut } from "../service-desk/types";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function renderAt(node: ReactElement, entry = "/notifications") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderInSpanish(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[entry]}>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const ME_READ_ONLY: Me = {
  user_id: "reader-1",
  org_id: "o1",
  email: "lector@example.com",
  display_name: "Lector",
  timezone: "UTC",
  permissions: ["notification.read"],
};
const ME_ADMIN: Me = { ...ME_READ_ONLY, user_id: "admin-1", display_name: "Admin", permissions: ["notification.read", "notification.admin"] };

const CHANNELS: ChannelOut[] = [
  { id: "ch1", kind: "webhook", name: "Mattermost de operaciones", config: { url: "https://mattermost.lan/hooks/xyz" }, active: true, has_credential: false, created_at: "2026-01-01T00:00:00.000Z" },
  { id: "ch2", kind: "email", name: "Correo de avisos", config: { host: "smtp.acme.test", port: 587, from_address: "avisos@acme.test", use_tls: true }, active: false, has_credential: true, created_at: "2026-01-01T00:00:00.000Z" },
  { id: "ch3", kind: "email", name: "Correo Microsoft", config: { auth: "microsoft", host: "smtp.office365.com", port: 587, from_address: "avisos@acme.test", use_tls: true }, active: true, has_credential: true, created_at: "2026-01-01T00:00:00.000Z" },
];

const RULES: RuleOut[] = [
  { id: "r1", event_type: "ticket.assigned", channel_id: "ch1", recipient_kind: "assignee", queue_id: "q1", active: true, created_at: "2026-01-01T00:00:00.000Z" },
  { id: "r2", event_type: "platform.probe_unhealthy", channel_id: "ch2", recipient_kind: "channel_default", queue_id: null, active: false, created_at: "2026-01-01T00:00:00.000Z" },
];

const ALLOWLIST: AllowlistEntryOut[] = [
  { id: "a1", host: "mattermost.lan", port: 8065, note: "Chat de operaciones", created_at: "2026-01-01T00:00:00.000Z" },
  { id: "a2", host: "mattermost.lan", port: 443, note: "", created_at: "2026-01-01T00:00:00.000Z" },
];

const QUEUES: QueueOut[] = [
  { id: "q1", key: "soporte", name: "Soporte general", description: "", active: true, sort_order: 0, created_by: "a", created_at: "2026-01-01T00:00:00.000Z" },
];

const MEMBERS: QueueMemberOut[] = [{ user_id: "u2", queue_id: "q1" }];

const USERS = [
  { id: "u2", email: "marta@acme.test", display_name: "Marta Gómez", status: "active", roles: [] },
  { id: "u3", email: "pedro@acme.test", display_name: "Pedro Baja", status: "disabled", roles: [] },
];

function delivery(over: Partial<DeliveryOut>): DeliveryOut {
  return {
    id: "d1",
    event_id: "e1",
    channel_id: "ch1",
    recipient: "mattermost.lan",
    status: "sent",
    attempts: 1,
    last_error: null,
    last_status_code: 200,
    next_attempt_at: null,
    sent_at: "2026-07-31T08:00:00.000Z",
    created_at: "2026-07-31T08:00:00.000Z",
    ...over,
  };
}

const DELIVERIES = {
  items: [
    delivery({}),
    delivery({ id: "d2", recipient: "marta@acme.test", status: "failed", attempts: 2, last_error: "egress blocked: scheme_not_allowed", next_attempt_at: "2026-08-01T10:00:00.000Z" }),
    delivery({ id: "d3", recipient: "pedro@acme.test", status: "dead", attempts: 5, last_error: "smtp 550 rejected" }),
    delivery({ id: "d4", recipient: "otro@acme.test", status: "dead", attempts: 5, last_error: null }),
    delivery({ id: "d5", recipient: "ana@acme.test", status: "pending", attempts: 0 }),
  ],
  total: 120,
};

function baseApi(me: Me, over: Partial<ApiClient> = {}): Partial<ApiClient> {
  const api: Partial<ApiClient> = {
    me: vi.fn().mockResolvedValue(me),
    listNotificationChannels: vi.fn().mockResolvedValue(CHANNELS),
    listNotificationRules: vi.fn().mockResolvedValue(RULES),
    listNotificationAllowlist: vi.fn().mockResolvedValue(ALLOWLIST),
    listTicketQueues: vi.fn().mockResolvedValue(QUEUES),
    listNotificationQueueMembers: vi.fn().mockResolvedValue(MEMBERS),
    listNotificationDeliveries: vi.fn().mockResolvedValue(DELIVERIES),
    listUsers: vi.fn().mockResolvedValue({ users: USERS }),
    ...over,
  };
  mockUseApi.mockReturnValue(api as ApiClient);
  return api;
}

/** Recorded fixture words the page prints as they are. */
const RECORDED = [
  "mattermost.lan",
  "smtp.acme.test",
  "smtp.office365.com",
  "avisos@acme.test",
  "marta@acme.test",
  "pedro@acme.test",
  "otro@acme.test",
  "ana@acme.test",
];

/**
 * The delivery log shows a failure's own excerpt as the server recorded it
 * ("smtp 550 rejected"): recorded text, not catalogue text, so the one English
 * word of it that the catalogue also uses is allowed on the renders that list
 * the deliveries, and nowhere else.
 */
const EXCERPT = ["rejected"];

// ------------------------------------------------------------ the admin page

describe("the notifications page in Spanish", () => {
  it("words the head, the four tiles and what each figure is made of", async () => {
    baseApi(ME_READ_ONLY);
    const { container } = renderAt(<NotificationAdminPage />);
    expect(await screen.findByRole("heading", { name: "Notificaciones" })).toBeInTheDocument();
    expect(screen.getByText("A quién se avisa cuando ocurre algo, y dónde.")).toBeInTheDocument();
    expect(await screen.findByText("2 correos electrónicos · 1 webhook")).toBeInTheDocument();
    expect(screen.getByText("1 enrutando · 1 en pausa")).toBeInTheDocument();
    expect(screen.getByText("en 1 host")).toBeInTheDocument();
    expect(screen.getByText("reintentos agotados, no se envía nada más")).toBeInTheDocument();
    const labels = [...container.querySelectorAll(".stat-label")].map((el) => el.textContent);
    expect(labels).toEqual(["Canales", "Reglas", "Entradas de la lista de permitidos", "Entregas abandonadas"]);
    expectNoEnglish(container, [...RECORDED, ...EXCERPT]);
  });

  it("says what a zero means on each tile", async () => {
    baseApi(ME_READ_ONLY, {
      listNotificationChannels: vi.fn().mockResolvedValue([]),
      listNotificationRules: vi.fn().mockResolvedValue([]),
      listNotificationAllowlist: vi.fn().mockResolvedValue([]),
      listNotificationDeliveries: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    });
    const { container } = renderAt(<NotificationAdminPage />);
    expect(await screen.findByText("ninguno configurado, así que no se puede entregar nada")).toBeInTheDocument();
    expect(screen.getByText("todavía no se enruta ningún evento a ningún sitio")).toBeInTheDocument();
    expect(screen.getByText("no se ha añadido nada, así que las direcciones internas siguen bloqueadas")).toBeInTheDocument();
    expect(screen.getByText("todas las entregas llegaron o se siguen reintentando")).toBeInTheDocument();
    expect(screen.getByText("Todavía no hay reglas, así que no se entrega nada.")).toBeInTheDocument();
    expect(screen.getByText("Todavía no hay canales.")).toBeInTheDocument();
    expect(screen.getByText("No hay hosts permitidos. El espacio de direcciones privado se deniega de forma predeterminada.")).toBeInTheDocument();
    expect(screen.getByText("Todavía no hay entregas.")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("re-words every breakdown and label on a switch, not from a remembered sentence", async () => {
    baseApi(ME_READ_ONLY);
    const { container } = renderAt(<NotificationAdminPage />);
    expect(await screen.findByText("2 correos electrónicos · 1 webhook")).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("2 email · 1 webhook")).toBeInTheDocument();
    expect(screen.getByText("1 routing · 1 paused")).toBeInTheDocument();
    expect(screen.getByText("across 1 host")).toBeInTheDocument();
    expect([...container.querySelectorAll(".stat-label")].map((el) => el.textContent)).toEqual([
      "Channels",
      "Rules",
      "Allowlist entries",
      "Deliveries that gave up",
    ]);
    act(() => setActiveLanguage("es"));
    expect(screen.getByText("1 enrutando · 1 en pausa")).toBeInTheDocument();
  });

  it("agrees the breakdowns with their counts", async () => {
    baseApi(ME_READ_ONLY, {
      listNotificationChannels: vi.fn().mockResolvedValue([CHANNELS[0], { ...CHANNELS[0], id: "ch9" }, CHANNELS[1]]),
      listNotificationRules: vi.fn().mockResolvedValue([RULES[0]]),
      listNotificationAllowlist: vi.fn().mockResolvedValue([...ALLOWLIST, { ...ALLOWLIST[0], id: "a3", host: "otra.lan" }]),
    });
    renderAt(<NotificationAdminPage />);
    expect(await screen.findByText("2 webhooks · 1 correo electrónico")).toBeInTheDocument();
    expect(screen.getByText("todas enrutando")).toBeInTheDocument();
    expect(screen.getByText("en 2 hosts")).toBeInTheDocument();
  });
});

// -------------------------------------------------------------------- rules

describe("the rules in Spanish", () => {
  it("words the columns, the event and recipient names, the queue scope and the switches", async () => {
    baseApi(ME_ADMIN);
    const { container } = renderAt(<NotificationAdminPage />);
    const panel = (await screen.findByRole("heading", { name: "Reglas" })).closest("section") as HTMLElement;
    expect(within(panel).getByText("Qué evento va a qué canal, y quién lo recibe")).toBeInTheDocument();
    for (const header of ["Regla", "Ámbito de cola", "Activa"]) {
      expect(await within(panel).findByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(within(panel).getByText("Ticket asignado")).toBeInTheDocument();
    expect(within(panel).getByText("La persona responsable")).toBeInTheDocument();
    expect(within(panel).getByText("Problema de la plataforma detectado")).toBeInTheDocument();
    expect(within(panel).getByText("El valor predeterminado del propio canal")).toBeInTheDocument();
    expect(within(panel).getByText("Cualquier cola")).toBeInTheDocument();
    expect(within(panel).getByRole("switch", { name: "Activada" })).toBeChecked();
    expect(within(panel).getByRole("switch", { name: "Desactivada" })).not.toBeChecked();
    expect(within(panel).getByRole("button", { name: "Añadir regla" })).toBeInTheDocument();
    expectNoEnglish(container, [...RECORDED, ...EXCERPT, "Soporte general"]);
  });

  it("offers every event in Spanish, hides the requester from internal notes, and words the dialog", async () => {
    const user = userEvent.setup();
    baseApi(ME_ADMIN);
    renderAt(<NotificationAdminPage />);
    await user.click(await screen.findByRole("button", { name: "Añadir regla" }));
    const dialog = await screen.findByRole("dialog", { name: "Añadir regla" });
    const events = within(dialog).getByLabelText("Tipo de evento");
    for (const type of EVENT_TYPES) {
      expect(within(events).getByRole("option", { name: es.enums.EventType[type.replace(/\./g, "_") as keyof typeof es.enums.EventType] })).toBeInTheDocument();
    }
    expect(within(dialog).getByLabelText("Destinatario")).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: "La persona que lo creó" })).toBeInTheDocument();
    await user.selectOptions(events, "ticket.internal_note_added");
    expect(within(dialog).queryByRole("option", { name: "La persona que lo creó" })).toBeNull();
    expect(within(dialog).getByRole("combobox", { name: "Canal" })).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Ámbito de cola (opcional: en blanco significa cualquier cola)")).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: "Cualquier cola" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Guardar" })).toBeDisabled();
    expectNoEnglish(dialog, ["Soporte general"]);
  });

  it("words the channel sublabels and the empty-channel hint", async () => {
    const user = userEvent.setup();
    baseApi(ME_ADMIN);
    renderAt(<NotificationAdminPage />);
    await user.click(await screen.findByRole("button", { name: "Añadir regla" }));
    const dialog = await screen.findByRole("dialog", { name: "Añadir regla" });
    await user.click(within(dialog).getByRole("combobox", { name: "Canal" }));
    expect(await within(dialog).findByText("Webhook · activo")).toBeInTheDocument();
    expect(within(dialog).getByText("Correo electrónico · deshabilitado, no entrega nada")).toBeInTheDocument();
  });

  it("says no channel exists to route through", async () => {
    const user = userEvent.setup();
    baseApi(ME_ADMIN, { listNotificationChannels: vi.fn().mockResolvedValue([]) });
    renderAt(<NotificationAdminPage />);
    await user.click(await screen.findByRole("button", { name: "Añadir regla" }));
    const dialog = await screen.findByRole("dialog", { name: "Añadir regla" });
    await user.click(within(dialog).getByRole("combobox", { name: "Canal" }));
    expect(await within(dialog).findByText(/^No hay canales por los que enrutar\./)).toBeInTheDocument();
    expectNoEnglish(dialog);
  });

  it("confirms a delete in Spanish and words a refusal, holding the failure", async () => {
    const user = userEvent.setup();
    baseApi(ME_ADMIN, { deleteNotificationRule: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    renderAt(<NotificationAdminPage />);
    const panel = (await screen.findByRole("heading", { name: "Reglas" })).closest("section") as HTMLElement;
    await user.click(await within(panel).findByRole("button", { name: "Más acciones para regla Ticket asignado" }));
    await user.click(await screen.findByRole("menuitem", { name: "Eliminar" }));
    const dialog = await screen.findByRole("dialog", { name: "Eliminar la regla" });
    expect(within(dialog).getByText("Ticket asignado → Mattermost de operaciones")).toBeInTheDocument();
    expect(within(dialog).getByText(/^Se elimina de forma permanente\./)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Eliminar regla" }));
    expect(await screen.findByText(/^No se cambió la regla\. Ese servicio no está disponible/)).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByText(/^The rule was not changed\. That service is unavailable/)).toBeInTheDocument();
  });

  it("says the rules are not available, or did not load", async () => {
    baseApi(ME_READ_ONLY, {
      listNotificationRules: vi.fn().mockRejectedValue(new ApiError(403, "forbidden")),
      listNotificationChannels: vi.fn().mockRejectedValue(new ApiError(503, "")),
    });
    const { container } = renderAt(<NotificationAdminPage />);
    expect(await screen.findByText("Las reglas no están disponibles (permisos insuficientes).")).toBeInTheDocument();
    expect(await screen.findByText(/^No se pudieron cargar los canales\. Ese servicio no está disponible/)).toBeInTheDocument();
    expectNoEnglish(container, [...RECORDED, ...EXCERPT]);
  });
});

// ----------------------------------------------------------------- channels

describe("the channels in Spanish", () => {
  async function channelsPanel() {
    const view = renderAt(<NotificationAdminPage />);
    const panel = (await screen.findByRole("heading", { name: "Canales" })).closest("section") as HTMLElement;
    await within(panel).findByText("Mattermost de operaciones");
    return { ...view, panel };
  }

  it("words the columns, the kinds, the credential column and the sign-in note", async () => {
    baseApi(ME_ADMIN);
    const { container, panel } = await channelsPanel();
    expect(within(panel).getByText("Adónde puede ir una notificación: un webhook o un servidor de correo.")).toBeInTheDocument();
    for (const header of ["Nombre", "Credencial", "Activo"]) {
      expect(within(panel).getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    const webhook = within(panel).getByText("Mattermost de operaciones").closest("tr") as HTMLElement;
    expect(within(webhook).getByText(/^Webhook/)).toBeInTheDocument();
    expect(within(webhook).getByText("Ninguna")).toBeInTheDocument();
    const email = within(panel).getByText("Correo de avisos").closest("tr") as HTMLElement;
    expect(within(email).getByText(/^Correo electrónico/)).toBeInTheDocument();
    expect(within(email).getByText("Establecida")).toBeInTheDocument();
    const microsoft = within(panel).getByText("Correo Microsoft").closest("tr") as HTMLElement;
    expect(within(microsoft).getByText(/ · inicio de sesión de Microsoft 365$/)).toBeInTheDocument();
    expect(within(webhook).getByRole("button", { name: "Editar Mattermost de operaciones" })).toBeInTheDocument();
    expect(within(webhook).getByRole("button", { name: "Probar" })).toBeInTheDocument();
    expectNoEnglish(container, [...RECORDED, ...EXCERPT, "Soporte general"]);
  });

  it("words a delivered test, a refused one and its known reason, and an unknown reason without printing it raw", async () => {
    const user = userEvent.setup();
    const test = vi
      .fn()
      .mockResolvedValueOnce({ delivered: true, status_code: 200 })
      .mockResolvedValueOnce({ delivered: true })
      .mockResolvedValueOnce({ delivered: false, error: "port_not_allowed" })
      .mockResolvedValueOnce({ delivered: false, error: "algo_nuevo" })
      .mockResolvedValueOnce({ delivered: false, error: null });
    baseApi(ME_ADMIN, { testNotificationChannel: test });
    const { panel } = await channelsPanel();
    const row = within(panel).getByText("Mattermost de operaciones").closest("tr") as HTMLElement;
    const press = async () => user.click(within(row).getByRole("button", { name: "Probar" }));
    await press();
    expect(await within(row).findByRole("status")).toHaveTextContent("Entregado (200)");
    await press();
    await within(row).findByText("Entregado (correcto)");
    await press();
    expect(await within(row).findByText(/^No se entregó: el host está en la lista de permitidos, pero no en ese puerto\./)).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(within(row).getByText(/^Not delivered: the host is allowlisted but not on that port\./)).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    await press();
    expect(await within(row).findByText("No se entregó: Algo nuevo")).toBeInTheDocument();
    await press();
    expect(await within(row).findByText("No se entregó: no se registró el motivo.")).toBeInTheDocument();
  });

  it("words a failed test, toggle and delete, holding the failure", async () => {
    const user = userEvent.setup();
    baseApi(ME_ADMIN, {
      testNotificationChannel: vi.fn().mockRejectedValue(new ApiError(503, "")),
      updateNotificationChannel: vi.fn().mockRejectedValue(new ApiError(503, "")),
      deleteNotificationChannel: vi.fn().mockRejectedValue(new ApiError(503, "")),
    });
    const { panel } = await channelsPanel();
    const row = within(panel).getByText("Mattermost de operaciones").closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "Probar" }));
    expect(await within(row).findByRole("alert")).toHaveTextContent("Ese servicio no está disponible en este momento.");
    await user.click(within(row).getByRole("switch", { name: "Activado" }));
    expect(await within(panel).findByText(/^No se cambió el canal\. Ese servicio no está disponible/)).toBeInTheDocument();
    await user.click(within(row).getByRole("button", { name: "Más acciones para Mattermost de operaciones" }));
    await user.click(await screen.findByRole("menuitem", { name: "Eliminar" }));
    const dialog = await screen.findByRole("dialog", { name: "Eliminar el canal" });
    expect(within(dialog).getByText("El canal y su credencial guardada se eliminan de forma permanente. No se puede deshacer. 1 regla enruta ahora por él y dejará de entregar.")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Eliminar canal" }));
    expect(await within(panel).findByText(/^No se eliminó el canal\. Ese servicio no está disponible/)).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(within(panel).getByText(/^The channel was not deleted\. That service is unavailable/)).toBeInTheDocument();
  });

  it("agrees the delete warning with the number of rules, or says none route through the channel", async () => {
    const user = userEvent.setup();
    baseApi(ME_ADMIN, {
      listNotificationRules: vi.fn().mockResolvedValue([RULES[0], { ...RULES[0], id: "r9" }]),
    });
    const { panel } = await channelsPanel();
    const open = async (name: string) => {
      const row = within(panel).getByText(name).closest("tr") as HTMLElement;
      await user.click(within(row).getByRole("button", { name: `Más acciones para ${name}` }));
      await user.click(await screen.findByRole("menuitem", { name: "Eliminar" }));
      return screen.findByRole("dialog", { name: "Eliminar el canal" });
    };
    const two = await open("Mattermost de operaciones");
    expect(within(two).getByText(/No se puede deshacer\. 2 reglas enrutan ahora por él y dejarán de entregar\.$/)).toBeInTheDocument();
    await user.click(within(two).getByRole("button", { name: "Cancelar" }));
    const none = await open("Correo Microsoft");
    expect(within(none).getByText(/No se puede deshacer\. Ninguna regla enruta por él\.$/)).toBeInTheDocument();
  });

  describe("the Add channel dialog", () => {
    async function openAdd() {
      const user = userEvent.setup();
      baseApi(ME_ADMIN, { createNotificationChannel: vi.fn().mockResolvedValue(CHANNELS[0]) });
      renderAt(<NotificationAdminPage />);
      await user.click(await screen.findByRole("button", { name: "Añadir canal" }));
      const dialog = await screen.findByRole("dialog", { name: "Añadir canal" });
      return { user, dialog };
    }

    it("words the webhook form", async () => {
      const { dialog } = await openAdd();
      expect(within(dialog).getByLabelText("Tipo")).toHaveValue("webhook");
      expect(within(dialog).getByRole("option", { name: "Correo electrónico" })).toBeInTheDocument();
      expect(within(dialog).getByLabelText("Nombre")).toBeInTheDocument();
      expect(within(dialog).getByLabelText("URL")).toHaveAttribute("placeholder", "https://chat.lan/hooks/abc");
      expect(within(dialog).getByLabelText("Token de portador (opcional)")).toHaveAttribute("type", "password");
      expect(within(dialog).getByRole("button", { name: "Guardar" })).toBeEnabled();
      expectNoEnglish(dialog, ["https://chat.lan/hooks/abc"]);
    });

    it("refuses an empty webhook and a badly shaped address, re-wording both on a switch", async () => {
      const { user, dialog } = await openAdd();
      await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
      expect(await within(dialog).findByRole("alert")).toHaveTextContent("El campo Nombre es obligatorio. El campo URL es obligatorio.");
      await user.type(within(dialog).getByLabelText("Nombre"), "Chat");
      await user.type(within(dialog).getByLabelText("URL"), "chat.lan");
      await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
      expect(await within(dialog).findByRole("alert")).toHaveTextContent('La URL debe ser una dirección completa, como https://chat.lan/hooks/abc; "chat.lan" no lo es.');
      act(() => setActiveLanguage("en"));
      expect(within(dialog).getByRole("alert")).toHaveTextContent('URL must be a full address such as https://chat.lan/hooks/abc; "chat.lan" is not one.');
      act(() => setActiveLanguage("es"));
      await user.clear(within(dialog).getByLabelText("URL"));
      await user.type(within(dialog).getByLabelText("URL"), "ftp://chat.lan/x");
      await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
      expect(await within(dialog).findByRole("alert")).toHaveTextContent('La URL debe empezar por https:// o http://; "ftp:" no es un esquema al que puedan publicar las entregas.');
    });

    it("words the mail form and refuses a bad port and a bad sender", async () => {
      const { user, dialog } = await openAdd();
      await user.selectOptions(within(dialog).getByLabelText("Tipo"), "email");
      for (const label of ["Host SMTP", "Puerto SMTP", "Dirección del remitente"]) {
        expect(within(dialog).getByLabelText(label)).toBeInTheDocument();
      }
      expect(within(dialog).getByLabelText("Dirección del remitente")).toHaveAttribute("placeholder", "avisos@acme.test");
      expect(within(dialog).getByRole("checkbox")).toBeChecked();
      expect(within(dialog).getAllByText("Usar TLS").length).toBeGreaterThan(0);
      expect(within(dialog).getByText("Inicio de sesión")).toBeInTheDocument();
      expect(within(dialog).getByLabelText("Nombre de usuario")).toBeInTheDocument();
      expect(within(dialog).getByLabelText("Contraseña")).toBeInTheDocument();
      await user.type(within(dialog).getByLabelText("Nombre"), "Correo");
      await user.type(within(dialog).getByLabelText("Host SMTP"), "smtp.acme.test");
      await user.clear(within(dialog).getByLabelText("Puerto SMTP"));
      await user.type(within(dialog).getByLabelText("Puerto SMTP"), "70000");
      await user.type(within(dialog).getByLabelText("Dirección del remitente"), "avisos");
      await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
      const alert = await within(dialog).findByRole("alert");
      expect(alert).toHaveTextContent("El puerto SMTP debe estar entre 1 y 65535; 70000 no lo está");
      expect(alert).toHaveTextContent('La dirección del remitente debe ser una dirección de correo, como avisos@acme.test; "avisos" no lo es');
      expectNoEnglish(dialog, ["smtp.acme.test"]);
    });

    it("holds the sign-in problem as a code and never prints the password typed", async () => {
      const { user, dialog } = await openAdd();
      await user.selectOptions(within(dialog).getByLabelText("Tipo"), "email");
      await user.type(within(dialog).getByLabelText("Nombre"), "Correo");
      await user.type(within(dialog).getByLabelText("Host SMTP"), "smtp.acme.test");
      await user.type(within(dialog).getByLabelText("Dirección del remitente"), "avisos@acme.test");
      await user.type(within(dialog).getByLabelText("Contraseña"), "clave-plantada");
      await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
      const alert = await within(dialog).findByRole("alert");
      expect(alert).toHaveTextContent("Introduce el nombre de usuario y la contraseña, o ninguno de los dos si el servidor de correo no necesita iniciar sesión.");
      expect(alert).not.toHaveTextContent("clave-plantada");
      act(() => setActiveLanguage("en"));
      expect(within(dialog).getByRole("alert")).toHaveTextContent("Enter both the username and the password, or neither if the mail server needs no sign-in.");
    });

    it("words the Microsoft 365 sign-in and refuses it half filled, without printing the secret", async () => {
      const { user, dialog } = await openAdd();
      await user.selectOptions(within(dialog).getByLabelText("Tipo"), "email");
      await user.click(within(dialog).getByRole("button", { name: "Microsoft 365" }));
      expect(within(dialog).getByLabelText("ID de directorio (inquilino)")).toBeInTheDocument();
      expect(within(dialog).getByText("Está en la página Información general del registro de la aplicación de Azure.")).toBeInTheDocument();
      expect(within(dialog).getByLabelText("ID de aplicación (cliente)")).toBeInTheDocument();
      expect(within(dialog).getByText(/^Envía a través del servidor de correo propio de Microsoft \(smtp\.office365\.com, puerto 587, TLS\)/)).toBeInTheDocument();
      await user.type(within(dialog).getByLabelText("Nombre"), "M365");
      await user.type(within(dialog).getByLabelText("Dirección del remitente"), "avisos@acme.test");
      await user.type(within(dialog).getByLabelText("Secreto de cliente"), "secreto-plantado");
      await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
      const alert = await within(dialog).findByRole("alert");
      expect(alert).toHaveTextContent(/^El inicio de sesión de Microsoft 365 necesita el ID de directorio \(inquilino\)/);
      expect(alert).not.toHaveTextContent("secreto-plantado");
      expectNoEnglish(dialog, ["smtp.office365.com", "Microsoft 365", "contoso.onmicrosoft.com"]);
    });

    it("words a failed save and holds the failure, not its sentence", async () => {
      const { user, dialog } = await openAdd();
      const api = mockUseApi.mock.results.at(-1)?.value as Partial<ApiClient>;
      api.createNotificationChannel = vi.fn().mockRejectedValue(new ApiError(503, ""));
      await user.type(within(dialog).getByLabelText("Nombre"), "Chat");
      await user.type(within(dialog).getByLabelText("URL"), "https://chat.lan/hooks/abc");
      await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
      expect(await within(dialog).findByRole("alert")).toHaveTextContent("Ese servicio no está disponible en este momento.");
      act(() => setActiveLanguage("en"));
      expect(within(dialog).getByRole("alert")).toHaveTextContent("That service is unavailable right now.");
    });
  });

  it("words the Edit channel dialog: the fixed kind and the stored credential", async () => {
    const user = userEvent.setup();
    baseApi(ME_ADMIN);
    const { panel } = await channelsPanel();
    const row = within(panel).getByText("Correo de avisos").closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "Editar Correo de avisos" }));
    const dialog = await screen.findByRole("dialog", { name: "Editar canal" });
    expect(within(dialog).getByText("El tipo de un canal no se puede cambiar una vez creado. Elimina este y añade un canal nuevo para pasar de webhook a correo electrónico o al revés. Hay una credencial guardada para este canal; déjala en blanco para conservarla.")).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Tipo")).toBeDisabled();
    expect(within(dialog).getByText("Opcional. Guardada; déjala en blanco para conservarla.")).toBeInTheDocument();
    expectNoEnglish(dialog, ["smtp.acme.test", "avisos@acme.test"]);
  });
});

// ---------------------------------------------------------------- allowlist

describe("the allowlist in Spanish", () => {
  it("words the warning, the columns and the rows", async () => {
    baseApi(ME_ADMIN);
    const { container } = renderAt(<NotificationAdminPage />);
    const panel = (await screen.findByRole("heading", { name: "Lista de permitidos" })).closest("section") as HTMLElement;
    expect(within(panel).getByText("Hosts internos a los que se pueden enviar notificaciones")).toBeInTheDocument();
    expect(within(panel).getByText(/^Advertencia: las direcciones internas están bloqueadas salvo que figuren aquí\./)).toBeInTheDocument();
    for (const header of ["Host", "Nota"]) {
      expect(await within(panel).findByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(within(panel).getByText("Sin nota")).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "Añadir host" })).toBeInTheDocument();
    expectNoEnglish(container, [...RECORDED, ...EXCERPT, "Soporte general"]);
  });

  it("words the Add dialog and confirms a removal, holding a failure", async () => {
    const user = userEvent.setup();
    baseApi(ME_ADMIN, { deleteNotificationAllowlistEntry: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    renderAt(<NotificationAdminPage />);
    await user.click(await screen.findByRole("button", { name: "Añadir host" }));
    const add = await screen.findByRole("dialog", { name: "Añadir entrada a la lista de permitidos" });
    expect(within(add).getByText(/^Esto permite que las notificaciones lleguen a una dirección interna/)).toBeInTheDocument();
    expect(within(add).getByLabelText("Host")).toHaveAttribute("placeholder", "mattermost.lan");
    expect(within(add).getByLabelText("Puerto")).toHaveValue(443);
    expect(within(add).getByLabelText("Nota")).toBeInTheDocument();
    expectNoEnglish(add, ["mattermost.lan"]);
    await user.click(within(add).getByRole("button", { name: "Cancelar" }));
    const panel = (await screen.findByRole("heading", { name: "Lista de permitidos" })).closest("section") as HTMLElement;
    await user.click(await within(panel).findByRole("button", { name: "Más acciones para mattermost.lan:8065" }));
    await user.click(await screen.findByRole("menuitem", { name: "Quitar" }));
    const dialog = await screen.findByRole("dialog", { name: "Quitar la entrada de la lista de permitidos" });
    expect(within(dialog).getByText("mattermost.lan:8065")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Quitar entrada" }));
    expect(await screen.findByText(/^No se quitó la entrada\. Sigue en la lista de permitidos\. Ese servicio no está disponible/)).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByText(/^The entry was not removed\. It is still allowlisted\. That service is unavailable/)).toBeInTheDocument();
  });
});

// ------------------------------------------------------------ queue members

describe("the queue members in Spanish", () => {
  it("words the resting state, then the members of a chosen queue", async () => {
    const user = userEvent.setup();
    baseApi(ME_ADMIN);
    const { container } = renderAt(<NotificationAdminPage />);
    const panel = (await screen.findByRole("heading", { name: "Miembros de la cola" })).closest("section") as HTMLElement;
    expect(within(panel).getByText("A quién llega una regla limitada a una cola")).toBeInTheDocument();
    expect(within(panel).getByText("Ninguna cola seleccionada")).toBeInTheDocument();
    expect(within(panel).getByText("Elige una cola arriba para ver y cambiar sus miembros.")).toBeInTheDocument();
    expect(within(panel).getByRole("option", { name: "Elige una cola" })).toBeInTheDocument();
    await within(panel).findByRole("option", { name: "Soporte general" });
    await user.selectOptions(within(panel).getByLabelText("Cola"), "q1");
    expect(await within(panel).findByRole("columnheader", { name: "Miembro" })).toBeInTheDocument();
    expect(await within(panel).findByText("Marta Gómez")).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "Quitar a Marta Gómez" })).toHaveTextContent("Quitar");
    expect(within(panel).getByRole("combobox", { name: "Añadir miembro" })).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "Añadir miembro" })).toBeDisabled();
    expectNoEnglish(container, [...RECORDED, ...EXCERPT, "Soporte general", "Marta Gómez"]);
  });

  it("words the person sublabels and the empty hint, with the account status in Spanish", async () => {
    const user = userEvent.setup();
    baseApi(ME_ADMIN);
    renderAt(<NotificationAdminPage />);
    const panel = (await screen.findByRole("heading", { name: "Miembros de la cola" })).closest("section") as HTMLElement;
    await within(panel).findByRole("option", { name: "Soporte general" });
    await user.selectOptions(within(panel).getByLabelText("Cola"), "q1");
    await user.click(await within(panel).findByRole("combobox", { name: "Añadir miembro" }));
    expect(await within(panel).findByText("pedro@acme.test · deshabilitado")).toBeInTheDocument();
  });

  it("says there are no members, and that the membership was not changed", async () => {
    const user = userEvent.setup();
    baseApi(ME_ADMIN, {
      listNotificationQueueMembers: vi.fn().mockResolvedValue([]),
      removeNotificationQueueMember: vi.fn().mockRejectedValue(new ApiError(503, "")),
    });
    renderAt(<NotificationAdminPage />);
    const panel = (await screen.findByRole("heading", { name: "Miembros de la cola" })).closest("section") as HTMLElement;
    await within(panel).findByRole("option", { name: "Soporte general" });
    await user.selectOptions(within(panel).getByLabelText("Cola"), "q1");
    expect(await within(panel).findByText("No hay miembros en esta cola.")).toBeInTheDocument();
  });

  it("asks for ids when the people and the queues cannot be listed", async () => {
    const user = userEvent.setup();
    baseApi(ME_ADMIN, {
      listTicketQueues: vi.fn().mockRejectedValue(new ApiError(403, "forbidden")),
      listUsers: vi.fn().mockRejectedValue(new ApiError(403, "forbidden")),
    });
    renderAt(<NotificationAdminPage />);
    const panel = (await screen.findByRole("heading", { name: "Miembros de la cola" })).closest("section") as HTMLElement;
    const queue = await within(panel).findByPlaceholderText("ID de la cola");
    await user.type(queue, "q1");
    expect(await within(panel).findByLabelText("Añadir usuario por ID")).toBeInTheDocument();
  });
});

// -------------------------------------------------------------- deliveries

describe("the delivery log in Spanish", () => {
  it("words the columns, the statuses, the reasons and the count that gave up", async () => {
    baseApi(ME_READ_ONLY);
    const { container } = renderAt(<NotificationAdminPage />);
    const panel = (await screen.findByRole("heading", { name: "Entregas" })).closest("section") as HTMLElement;
    expect(within(panel).getByText("Cada intento de llegar a un destinatario, con sus reintentos y lo que lo detuvo.")).toBeInTheDocument();
    expect(await within(panel).findByLabelText("Entregas abandonadas")).toHaveTextContent("0 abandonadas");
    expect(within(panel).getByText(/^Las pruebas de envío no aparecen aquí\./)).toBeInTheDocument();
    for (const header of ["Destinatario", "Estado", "Intentos", "Próximo intento"]) {
      expect(await within(panel).findByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(within(panel).getByText("5 de 120 mostradas")).toBeInTheDocument();
    const filter = within(panel).getByLabelText("Filtrar por estado");
    expect(within(filter).getByRole("option", { name: "Todos los estados" })).toBeInTheDocument();
    for (const option of ["Pendiente", "Enviada", "Fallida, reintentando", "Abandonada"]) {
      expect(within(filter).getByRole("option", { name: option })).toBeInTheDocument();
    }
    const failed = within(panel).getByText("marta@acme.test").closest("tr") as HTMLElement;
    expect(within(failed).getByText("Fallida, reintentando")).toBeInTheDocument();
    expect(within(failed).getByText(/^Bloqueado antes de enviar: el esquema de la URL no es uno al que puedan publicar las entregas\./)).toBeInTheDocument();
    const dead = within(panel).getByText("pedro@acme.test").closest("tr") as HTMLElement;
    expect(within(dead).getByText("Abandonada")).toBeInTheDocument();
    expect(within(dead).getByText("smtp 550 rejected")).toBeInTheDocument();
    const bare = within(panel).getByText("otro@acme.test").closest("tr") as HTMLElement;
    expect(within(bare).getByText("Sin errores registrados")).toBeInTheDocument();
    expectNoEnglish(container, [...RECORDED, ...EXCERPT]);
  });

  it("counts the deliveries that gave up, agreeing with the number", async () => {
    baseApi(ME_READ_ONLY, { listNotificationDeliveries: vi.fn().mockResolvedValue({ items: [], total: 1 }) });
    renderAt(<NotificationAdminPage />);
    expect(await screen.findByLabelText("Entregas abandonadas", { selector: "span" })).toHaveTextContent("1 abandonada");
  });

  it("narrows to the deliveries that gave up from the address, and re-words the statuses on a switch", async () => {
    baseApi(ME_READ_ONLY);
    renderAt(<NotificationAdminPage />, "/notifications?status=dead#deliveries");
    const filter = await screen.findByLabelText("Filtrar por estado");
    expect(filter).toHaveValue("dead");
    act(() => setActiveLanguage("en"));
    const english = screen.getByLabelText("Filter by status");
    expect(within(english).getByRole("option", { name: "Failed, retrying" })).toBeInTheDocument();
    expect(within(english).getByRole("option", { name: "Gave up" })).toBeInTheDocument();
  });

  it("says the deliveries are not available, or did not load", async () => {
    baseApi(ME_READ_ONLY, { listNotificationDeliveries: vi.fn().mockRejectedValue(new ApiError(403, "forbidden")) });
    const forbidden = renderAt(<NotificationAdminPage />);
    expect(await screen.findByText("Las entregas no están disponibles (permisos insuficientes).")).toBeInTheDocument();
    forbidden.unmount();
    baseApi(ME_READ_ONLY, { listNotificationDeliveries: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    renderAt(<NotificationAdminPage />);
    expect(await screen.findByText(/^No se pudieron cargar las entregas\. Ese servicio no está disponible/)).toBeInTheDocument();
  });
});

// ------------------------------------------------------------ prerequisites

describe("the prerequisites strip in Spanish", () => {
  const user = (id: string, status = "active") => ({ id, email: `${id}@x`, display_name: id, status, roles: [] });
  function panelApi(over: Record<string, unknown> = {}, permissions = ["users.read", "ticket.read"]) {
    mockUseApi.mockReturnValue({
      me: meWith(permissions),
      listUsers: vi.fn().mockResolvedValue({ users: [user("a"), user("b")] }),
      listTicketQueues: vi.fn().mockResolvedValue([{ id: "q1" }]),
      ...over,
    } as unknown as ApiClient);
  }

  it("says nobody else can sign in and counts the queues", async () => {
    panelApi({ listUsers: vi.fn().mockResolvedValue({ users: [user("a"), user("b", "disabled")] }) });
    setActiveLanguage("es");
    const { container } = renderPanel(<NotificationPrerequisites />);
    expect(await screen.findByText("Antes de que las notificaciones puedan llegar a las personas")).toBeInTheDocument();
    expect(screen.getByText("Lo que necesitan la mesa de ayuda y la configuración para que una regla tenga a quién avisar.")).toBeInTheDocument();
    expect(await screen.findByText(/^Como mucho una persona puede iniciar sesión aquí/)).toBeInTheDocument();
    expect(screen.getByText("Existe 1 cola de tickets.")).toBeInTheDocument();
    await expectCta("Añadir usuarios", "/settings?tab=users");
    expectNoEnglish(container);
  });

  it("agrees the done sentences with their counts, and links to the queues", async () => {
    panelApi({ listTicketQueues: vi.fn().mockResolvedValue([]) });
    setActiveLanguage("es");
    renderPanel(<NotificationPrerequisites />);
    expect(await screen.findByText("2 personas pueden iniciar sesión aquí.")).toBeInTheDocument();
    await expectCta("Configurar colas", "/service-desk/admin");
    expect(screen.getByText("No existe ninguna cola de tickets, así que no hay ninguna cola cuyos miembros pueda avisar una regla.")).toBeInTheDocument();
  });

  it("says it cannot be checked from an account without the permission", async () => {
    panelApi({}, ["ticket.read"]);
    setActiveLanguage("es");
    const { container } = renderPanel(<NotificationPrerequisites />);
    expect((await screen.findAllByText(/No se puede comprobar desde esta cuenta/)).length).toBeGreaterThan(0);
    expectNoEnglish(container);
  });
});

// ---------------------------------------------------- outside React: the labels

describe("the notification label tables", () => {
  it("has a Spanish name for every event type, different from the English one", () => {
    const english = en.enums.EventType as Record<string, string>;
    const spanish = es.enums.EventType as Record<string, string>;
    for (const type of EVENT_TYPES) {
      const key = type.replace(/\./g, "_");
      expect(english[key], `${type} (en)`).toBeTruthy();
      expect(spanish[key], `${type} (es)`).toBeTruthy();
      expect(spanish[key]).not.toBe(english[key]);
    }
    expect(Object.keys(english).length).toBe(EVENT_TYPES.length);
  });

  it("words every event and recipient kind, through the records the rest of the console indexes", () => {
    setActiveLanguage("es");
    expect(EVENT_TYPE_LABELS["ticket.comment_added"]).toBe("Respuesta pública añadida");
    expect(EVENT_TYPE_LABELS["push.run.stopped"]).toBe("Ejecución de instalación remota detenida tras fallos de inicio de sesión");
    expect(eventTypeLabel("device.joined_via_link")).toBe("Equipo en espera de revisión");
    expect(RECIPIENT_KIND_LABELS.queue_members).toBe("Todas las personas de la cola");
    expect(recipientKindLabel("requester")).toBe("La persona que lo creó");
    // A value this client has not met is humanised, never printed raw.
    expect(eventTypeLabel("ticket.escalated_twice")).toBe("Ticket escalated twice");
    setActiveLanguage("en");
    expect(EVENT_TYPE_LABELS["ticket.comment_added"]).toBe("Public reply added");
    expect(recipientKindLabel("assignee")).toBe("Whoever it's assigned to");
  });

  it("words the delivery statuses and the channel kinds", () => {
    setActiveLanguage("es");
    expect(deliveryStatusLabel("pending")).toBe("Pendiente");
    expect(deliveryStatusLabel("sent")).toBe("Enviada");
    expect(deliveryStatusLabel("failed")).toBe("Fallida, reintentando");
    expect(deliveryStatusLabel("dead")).toBe("Abandonada");
    expect(deliveryStatusLabel("on_hold")).toBe("On hold");
    expect(channelKindLabel("webhook")).toBe("Webhook");
    expect(channelKindLabel("email")).toBe("Correo electrónico");
    setActiveLanguage("en");
    expect(deliveryStatusLabel("dead")).toBe("Gave up");
    expect(channelKindLabel("email")).toBe("Email");
  });

  it("has a Spanish sentence for every reason a delivery can fail", () => {
    const codes = Object.keys(en.notification.deliveryError).filter((key) => key !== "notRecorded" && key !== "blockedBeforeSending");
    expect(codes.length).toBe(16);
    for (const code of codes) {
      setActiveLanguage("es");
      const spanish = deliveryErrorMessage(code);
      setActiveLanguage("en");
      const english = deliveryErrorMessage(code);
      expect(spanish, code).not.toBe(english);
      expect(spanish, code).not.toBe(code);
    }
    setActiveLanguage("es");
    expect(deliveryErrorMessage(undefined)).toBe("no se registró el motivo.");
    expect(deliveryLastErrorMessage("egress blocked: timeout")).toMatch(/^Bloqueado antes de enviar: el destino no respondió a tiempo\./);
    // What is not a known reason passes through untouched.
    expect(deliveryLastErrorMessage("egress blocked: algo_nuevo")).toBe("egress blocked: algo_nuevo");
    expect(deliveryLastErrorMessage("smtp 550 rejected")).toBe("smtp 550 rejected");
    setActiveLanguage("en");
    expect(deliveryLastErrorMessage("egress blocked: timeout")).toMatch(/^Blocked before sending: the destination did not answer in time\./);
  });

  it("words what a sign-in is missing, holding the code and not the secrets", () => {
    const base: EmailSignIn = { auth: "password", username: "", password: "", tenantId: "", clientId: "", clientSecret: "" };
    expect(signInProblemKind({ ...base, username: "ana" }, false)).toBe("passwordIncomplete");
    expect(signInProblemKind({ ...base, auth: "microsoft", tenantId: "t" }, false)).toBe("microsoftIncomplete");
    expect(signInProblemKind({ ...base, auth: "microsoft" }, true)).toBeNull();
    expect(signInProblemKind(base, false)).toBeNull();
    setActiveLanguage("es");
    expect(signInProblem({ ...base, username: "ana" }, false)).toBe(
      "Introduce el nombre de usuario y la contraseña, o ninguno de los dos si el servidor de correo no necesita iniciar sesión.",
    );
    setActiveLanguage("en");
    expect(signInProblem({ ...base, username: "ana" }, false)).toBe(
      "Enter both the username and the password, or neither if the mail server needs no sign-in.",
    );
  });
});
