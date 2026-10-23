/**
 * Email intake in Spanish: the admin page (tiles, the mailbox list with its
 * Microsoft sign-in states, the Microsoft 365 connection, the message log), the
 * mailbox form and its refusals, the page Microsoft returns the browser to, the
 * prerequisites strip and the label table built outside React. Mailbox
 * addresses, hosts, sender addresses, subjects, message text and the server's
 * own error text read as recorded; the words around them are Spanish.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import { ApiError } from "../../lib/api";
import type { ApiClient, Me } from "../../lib/api";
import { expectCta, meWith, renderPanel } from "../../test/prerequisiteHarness";
import type { CategoryOut } from "../service-desk/types";
import { browser } from "./browser";
import { EmailIntakeAdminPage } from "./EmailIntakeAdminPage";
import { EmailIntakePrerequisites } from "./EmailIntakePrerequisites";
import { dispositionLabel } from "./intakeLabels";
import { OAuthCallback } from "./OAuthCallback";
import type { InboundMessageOut, MailboxOut, MessageLogOut, OAuthAppOut } from "./types";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

afterEach(() => {
  vi.restoreAllMocks();
});

function renderAt(node: ReactElement, entry = "/email-intake", path = "/email-intake") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderInSpanish(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path={path} element={node} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const ME_READ_ONLY: Me = {
  user_id: "reader-1",
  org_id: "o1",
  email: "lector@example.com",
  display_name: "Lector",
  timezone: "UTC",
  permissions: ["email_intake.read"],
};

const ME_ADMIN: Me = { ...ME_READ_ONLY, user_id: "admin-1", display_name: "Admin", permissions: ["email_intake.read", "email_intake.admin"] };

const HEALTHY: MailboxOut = {
  id: "mb1",
  address: "soporte@acme.test",
  host: "imap.acme.test",
  port: 993,
  use_ssl: true,
  folder: "INBOX",
  username: "soporte@acme.test",
  has_credential: true,
  auth_kind: "password",
  credential_state: "ok",
  default_category_id: "cat-1",
  reopen_window_days: 14,
  active: true,
  last_polled_at: "2026-07-30T09:00:00.000Z",
  last_error: null,
  created_at: "2026-01-01T00:00:00.000Z",
};

const BROKEN: MailboxOut = {
  ...HEALTHY,
  id: "mb2",
  address: "ventas@acme.test",
  username: "ventas@acme.test",
  has_credential: false,
  use_ssl: false,
  active: false,
  last_polled_at: null,
  last_error: "login failed: rejected",
};

const MS_NEW: MailboxOut = {
  ...HEALTHY,
  id: "mb3",
  address: "m365@acme.test",
  auth_kind: "microsoft_oauth",
  has_credential: false,
  active: false,
};
const MS_EXPIRED: MailboxOut = { ...MS_NEW, id: "mb4", address: "caducado@acme.test", has_credential: true, credential_state: "needs_reconsent" };
const MS_OK: MailboxOut = { ...MS_NEW, id: "mb5", address: "conectado@acme.test", has_credential: true, active: true };

const CATEGORIES: CategoryOut[] = [
  { id: "cat-1", key: "equipos", name: "Equipos", type: "incident", requires_approval: false, approver_role_id: null, default_queue_id: "q1", active: true, sort_order: 0, created_by: "a", created_at: "2026-01-01T00:00:00.000Z" },
];

function message(overrides: Partial<InboundMessageOut> = {}): InboundMessageOut {
  return {
    id: "im1",
    mailbox_id: "mb1",
    message_id: "<uno@acme.test>",
    from_address: "alicia@acme.test",
    subject: "La impresora falla",
    received_at: "2026-07-31T08:00:00.000Z",
    created_at: "2026-07-31T08:00:01.000Z",
    disposition: "created",
    reason: "",
    ticket_id: "t1",
    body_text: "La impresora de la planta 2 está atascada.",
    body_truncated: false,
    ...overrides,
  };
}

function log(overrides: Partial<MessageLogOut> = {}): MessageLogOut {
  return {
    messages: [
      message(),
      message({ id: "im2", subject: "Aviso raro", disposition: "quarantined", reason: "", ticket_id: null, body_text: null }),
      message({ id: "im3", subject: "Informe largo", disposition: "refused_parse", reason: "tamaño excesivo", ticket_id: null, body_truncated: true }),
    ],
    total: 300,
    quarantined_count: 42,
    ...overrides,
  };
}

function baseApi(me: Me, over: Partial<ApiClient> = {}): Partial<ApiClient> {
  const api: Partial<ApiClient> = {
    me: vi.fn().mockResolvedValue(me),
    listEmailMailboxes: vi.fn().mockResolvedValue([HEALTHY, BROKEN]),
    listTicketCategories: vi.fn().mockResolvedValue(CATEGORIES),
    listEmailIntakeMessages: vi.fn().mockResolvedValue(log()),
    getEmailOAuthApp: vi.fn().mockResolvedValue({ tenant_id: "", client_id: "", has_secret: false, redirect_uri: null }),
    ...over,
  };
  mockUseApi.mockReturnValue(api as ApiClient);
  return api;
}

/** Recorded fixture words the page prints as they are. */
const RECORDED = ["soporte@acme.test", "ventas@acme.test", "archivo@acme.test", "imap.acme.test", "login failed: rejected", "INBOX"];

/**
 * Wire words that are recorded, not catalogued, on a page that shows the
 * message log and the mailboxes: a row's disposition, kept in the badge's
 * `title` so a row can be matched against the API, and the poller's own
 * `last_error` ("login failed: rejected"), which the server wrote when it
 * recorded the fault. Passed only to the renders that show those rows.
 */
const WIRE = ["quarantined", "refused", "rejected", "failed:", "failed"];

// ------------------------------------------------------------ the admin page

describe("the email intake page in Spanish", () => {
  it("words the head, the four tiles and what each figure is made of", async () => {
    baseApi(ME_READ_ONLY);
    const { container } = renderAt(<EmailIntakeAdminPage />);
    expect(await screen.findByRole("heading", { name: "Entrada de correo" })).toBeInTheDocument();
    expect(screen.getByText("El correo enviado a la dirección de soporte se convierte en ticket; las respuestas se añaden al mismo ticket.")).toBeInTheDocument();
    expect(await screen.findByText("Buzones configurados")).toBeInTheDocument();
    expect(screen.getByText("1 activo · 1 desactivado")).toBeInTheDocument();
    expect(screen.getByText("Buzones con errores")).toBeInTheDocument();
    expect(screen.getByText("1 de 2 configurados")).toBeInTheDocument();
    expect(screen.getByText("Mensajes en el ámbito")).toBeInTheDocument();
    expect(screen.getByText("3 de ellos se muestran abajo")).toBeInTheDocument();
    expect(screen.getByText("En cuarentena", { selector: ".stat-label" })).toBeInTheDocument();
    expect(screen.getByText("42 de 300 mensajes")).toBeInTheDocument();
    expectNoEnglish(container, [...RECORDED, ...WIRE]);
  });

  it("says what a zero means on each tile, and tallies a log the page holds whole", async () => {
    baseApi(ME_READ_ONLY, {
      listEmailMailboxes: vi.fn().mockResolvedValue([]),
      listEmailIntakeMessages: vi.fn().mockResolvedValue({ messages: [], total: 0, quarantined_count: 0 }),
    });
    const empty = renderAt(<EmailIntakeAdminPage />);
    expect(await screen.findByText("ninguno configurado, así que no se comprueba ningún correo")).toBeInTheDocument();
    expect(screen.getByText("todos los buzones se comprueban sin errores")).toBeInTheDocument();
    expect(screen.getByText("todavía no se ha recibido nada")).toBeInTheDocument();
    expect(screen.getByText("nada en cuarentena")).toBeInTheDocument();
    expect(screen.getByText("No hay buzones configurados.")).toBeInTheDocument();
    expect(screen.getByText("No hay mensajes registrados.")).toBeInTheDocument();
    expectNoEnglish(empty.container);
    empty.unmount();

    baseApi(ME_READ_ONLY, {
      listEmailIntakeMessages: vi.fn().mockResolvedValue(
        log({ messages: [message(), message({ id: "im2" }), message({ id: "im3", disposition: "quarantined" })], total: 3, quarantined_count: 1 }),
      ),
    });
    renderAt(<EmailIntakeAdminPage />);
    expect(await screen.findByText("2 llegaron a un ticket · 1 retenido")).toBeInTheDocument();
    expect(screen.getByText("1 de 3 mensajes")).toBeInTheDocument();
  });

  it("re-words the tiles' breakdowns on a switch, not from a remembered sentence", async () => {
    baseApi(ME_READ_ONLY);
    renderAt(<EmailIntakeAdminPage />);
    expect(await screen.findByText("1 activo · 1 desactivado")).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("1 polling · 1 switched off")).toBeInTheDocument();
    expect(screen.getByText("1 of 2 configured")).toBeInTheDocument();
    expect(screen.getByText("3 of them listed below")).toBeInTheDocument();
    expect(screen.getByText("42 of 300 messages")).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    expect(screen.getByText("1 de 2 configurados")).toBeInTheDocument();
  });

  it("says what is not available when the reads are refused", async () => {
    baseApi(ME_READ_ONLY, {
      listEmailMailboxes: vi.fn().mockRejectedValue(new ApiError(403, "forbidden")),
      listEmailIntakeMessages: vi.fn().mockRejectedValue(new ApiError(503, "")),
    });
    const { container } = renderAt(<EmailIntakeAdminPage />);
    expect(await screen.findByText("Los buzones no están disponibles (permisos insuficientes).")).toBeInTheDocument();
    expect(await screen.findByText(/^No se pudieron cargar los mensajes\. Ese servicio no está disponible/)).toBeInTheDocument();
    expectNoEnglish(container);
    act(() => setActiveLanguage("en"));
    expect(screen.getByText(/^Messages did not load\. That service is unavailable/)).toBeInTheDocument();
  });
});

// ------------------------------------------------------------- the mailboxes

describe("the mailbox list in Spanish", () => {
  it("words the columns, the transports, the credential column and a mailbox's error", async () => {
    baseApi(ME_READ_ONLY, {
      listEmailMailboxes: vi.fn().mockResolvedValue([HEALTHY, BROKEN, { ...HEALTHY, id: "mb9", address: "archivo@acme.test", active: false }]),
    });
    const { container } = renderAt(<EmailIntakeAdminPage />);
    const panel = (await screen.findByRole("heading", { name: "Buzones" })).closest("section") as HTMLElement;
    expect(within(panel).getByText("La plataforma comprueba si hay correo nuevo en cada dirección cada pocos minutos.")).toBeInTheDocument();
    const healthy = await within(panel).findByRole("row", { name: /soporte@acme\.test/ });
    for (const header of ["Dirección", "Estado", "Credenciales", "Última comprobación"]) {
      expect(within(panel).getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(within(healthy).getByText("Establecida")).toBeInTheDocument();
    expect(within(healthy).getByText(/TLS implícito/)).toBeInTheDocument();
    const broken = within(panel).getByRole("row", { name: /ventas@acme\.test/ });
    expect(within(broken).getByText("Sin establecer")).toBeInTheDocument();
    expect(within(broken).getByText(/STARTTLS/)).toBeInTheDocument();
    expect(within(broken).getByText("login failed: rejected")).toBeInTheDocument();
    expect(within(panel).getByText("Sin errores registrados")).toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: /Añadir buzón/ })).toBeNull();
    expectNoEnglish(container, [...RECORDED, ...WIRE]);
  });

  it("words the Microsoft 365 sign-in states and their actions, and re-words them on a switch", async () => {
    baseApi(ME_ADMIN, { listEmailMailboxes: vi.fn().mockResolvedValue([MS_NEW, MS_EXPIRED, MS_OK]) });
    const { container } = renderAt(<EmailIntakeAdminPage />);
    const fresh = await screen.findByRole("row", { name: /m365@acme\.test/ });
    expect(within(fresh).getByText("Sin conectar")).toBeInTheDocument();
    expect(within(fresh).getByRole("button", { name: "Conectar con Microsoft: m365@acme.test" })).toHaveTextContent("Conectar con Microsoft");
    const expired = screen.getByRole("row", { name: /caducado@acme\.test/ });
    expect(within(expired).getByText("El inicio de sesión de Microsoft caducó")).toBeInTheDocument();
    expect(within(expired).getByRole("button", { name: "Volver a conectar: caducado@acme.test" })).toBeInTheDocument();
    const connected = screen.getByRole("row", { name: /conectado@acme\.test/ });
    expect(within(connected).getByText("Conectado")).toBeInTheDocument();
    expect(within(connected).getByRole("button", { name: "Desconectar: conectado@acme.test" })).toBeInTheDocument();
    expect(within(fresh).getAllByText("Microsoft 365").length).toBeGreaterThan(0);
    expectNoEnglish(container, ["m365@acme.test", "caducado@acme.test", "conectado@acme.test", "imap.acme.test", "INBOX", ...WIRE]);
    act(() => setActiveLanguage("en"));
    expect(within(expired).getByText("Microsoft sign-in expired")).toBeInTheDocument();
    expect(within(expired).getByRole("button", { name: "Reconnect: caducado@acme.test" })).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    expect(within(expired).getByText("El inicio de sesión de Microsoft caducó")).toBeInTheDocument();
  });

  it("starts the Microsoft sign-in, and words its failure", async () => {
    const user = userEvent.setup();
    const assign = vi.spyOn(browser, "assign").mockImplementation(() => undefined);
    const api = baseApi(ME_ADMIN, {
      listEmailMailboxes: vi.fn().mockResolvedValue([MS_NEW]),
      startMailboxOAuth: vi.fn().mockRejectedValue(new ApiError(503, "")),
    });
    renderAt(<EmailIntakeAdminPage />);
    await user.click(await screen.findByRole("button", { name: "Conectar con Microsoft: m365@acme.test" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/^No se pudo comenzar el inicio de sesión con Microsoft\. Ese servicio no está disponible/);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent(/^Could not start the Microsoft sign-in\. That service is unavailable/);
    expect(api.startMailboxOAuth).toHaveBeenCalled();
    expect(assign).not.toHaveBeenCalled();
  });

  it("confirms a disconnect in Spanish", async () => {
    const user = userEvent.setup();
    baseApi(ME_ADMIN, {
      listEmailMailboxes: vi.fn().mockResolvedValue([MS_OK]),
      disconnectMailboxOAuth: vi.fn().mockRejectedValue(new ApiError(503, "")),
    });
    renderAt(<EmailIntakeAdminPage />);
    await user.click(await screen.findByRole("button", { name: "Desconectar: conectado@acme.test" }));
    const dialog = await screen.findByRole("dialog", { name: "Desconectar de Microsoft" });
    expect(within(dialog).getByText(/^El inicio de sesión de Microsoft guardado se elimina/)).toBeInTheDocument();
    expectNoEnglish(dialog, ["conectado@acme.test"]);
    await user.click(within(dialog).getByRole("button", { name: "Desconectar" }));
    expect(await screen.findByText(/^El buzón no se desconectó\. Ese servicio no está disponible/)).toBeInTheDocument();
  });

  it("tests a mailbox in Spanish, and confirms a delete", async () => {
    const user = userEvent.setup();
    baseApi(ME_ADMIN, {
      testEmailMailbox: vi.fn().mockRejectedValue(new ApiError(503, "")),
      deleteEmailMailbox: vi.fn().mockRejectedValue(new ApiError(503, "")),
    });
    renderAt(<EmailIntakeAdminPage />);
    expect(await screen.findByRole("button", { name: "Añadir buzón" })).toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: "Probar soporte@acme.test" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Ese servicio no está disponible en este momento.");
    await user.click(screen.getAllByRole("button", { name: /Más acciones para soporte@acme\.test|Acciones para soporte@acme\.test/ })[0] as HTMLElement);
    await user.click(await screen.findByRole("menuitem", { name: "Eliminar" }));
    const dialog = await screen.findByRole("dialog", { name: "Eliminar el buzón" });
    expect(within(dialog).getByText(/^El buzón, su contraseña guardada y todos los mensajes entrantes/)).toBeInTheDocument();
    expectNoEnglish(dialog, ["soporte@acme.test"]);
    await user.click(within(dialog).getByRole("button", { name: "Eliminar buzón" }));
    expect(await screen.findByText(/^El buzón no se eliminó\. Ese servicio no está disponible/)).toBeInTheDocument();
  });
});

// ------------------------------------------------------------- the mailbox form

describe("the mailbox form in Spanish", () => {
  async function openAdd() {
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN, { createEmailMailbox: vi.fn().mockResolvedValue(HEALTHY) });
    const view = renderAt(<EmailIntakeAdminPage />);
    await user.click(await screen.findByRole("button", { name: "Añadir buzón" }));
    const dialog = await screen.findByRole("dialog", { name: "Añadir buzón" });
    return { user, api, dialog, ...view };
  }

  it("words the fields, the hints and the transports", async () => {
    const { dialog } = await openAdd();
    for (const label of ["Dirección", "Servidor IMAP", "Puerto", "Transporte", "Carpeta", "Nombre de usuario", "Contraseña", "Plazo de reapertura (días)"]) {
      expect(within(dialog).getByLabelText(label)).toBeInTheDocument();
    }
    expect(within(dialog).getByLabelText("Dirección")).toHaveAttribute("placeholder", "soporte@acme.test");
    expect(within(dialog).getByRole("option", { name: "TLS implícito (993)" })).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: "STARTTLS (143)" })).toBeInTheDocument();
    expect(within(dialog).getByText("Inicio de sesión")).toBeInTheDocument();
    expect(within(dialog).getByText(/^Los dos transportes verifican el certificado del servidor/)).toBeInTheDocument();
    expect(within(dialog).getByText(/^Los tickets entrantes se crean en esta categoría/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Guardar" })).toBeEnabled();
    expectNoEnglish(dialog, ["INBOX", "Microsoft 365"]);
  });

  it("refuses an empty form without ever holding the password, and re-words on a switch", async () => {
    const { user, dialog } = await openAdd();
    await user.type(within(dialog).getByLabelText("Dirección"), "sin-arroba");
    await user.clear(within(dialog).getByLabelText("Puerto"));
    await user.type(within(dialog).getByLabelText("Puerto"), "70000");
    await user.clear(within(dialog).getByLabelText("Plazo de reapertura (días)"));
    await user.type(within(dialog).getByLabelText("Plazo de reapertura (días)"), "0");
    await user.type(within(dialog).getByLabelText("Contraseña"), "   ");
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent('La dirección debe ser una dirección de correo, como soporte@acme.test; "sin-arroba" no lo es');
    expect(alert).toHaveTextContent("El campo Servidor IMAP es obligatorio");
    expect(alert).toHaveTextContent("El puerto debe estar entre 1 y 65535; 70000 no lo está");
    expect(alert).toHaveTextContent("El campo Nombre de usuario es obligatorio");
    expect(alert).toHaveTextContent("El campo Contraseña es obligatorio");
    expect(alert).toHaveTextContent("El plazo de reapertura (días) debe estar entre 1 y 3650; 0 no lo está");
    expectNoEnglish(dialog, ["sin-arroba"]);
    act(() => setActiveLanguage("en"));
    expect(within(dialog).getByRole("alert")).toHaveTextContent('Address must be an email address such as helpdesk@acme.test; "sin-arroba" is not one');
    expect(within(dialog).getByRole("alert")).toHaveTextContent("Port must be between 1 and 65535; 70000 is not");
    act(() => setActiveLanguage("es"));
    expect(within(dialog).getByRole("alert")).toHaveTextContent("El campo Contraseña es obligatorio");
  });

  it("never prints a typed password into a refusal", async () => {
    const { user, dialog } = await openAdd();
    await user.type(within(dialog).getByLabelText("Contraseña"), "s3creto-plantado");
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).not.toHaveTextContent("s3creto-plantado");
    expect(alert).not.toHaveTextContent("El campo Contraseña");
  });

  it("words the Microsoft hint and drops the password field", async () => {
    const { user, dialog } = await openAdd();
    await user.click(within(dialog).getByRole("button", { name: "Microsoft 365" }));
    expect(within(dialog).getByText(/^Después de guardar, usa Conectar con Microsoft en la lista de buzones/)).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Servidor IMAP")).toHaveValue("outlook.office365.com");
    expect(within(dialog).queryByLabelText("Contraseña")).toBeNull();
    expectNoEnglish(dialog, ["outlook.office365.com", "Microsoft 365", "INBOX"]);
  });

  it("words the edit form: whether a password is configured, and the switch to Microsoft", async () => {
    const user = userEvent.setup();
    baseApi(ME_ADMIN);
    renderAt(<EmailIntakeAdminPage />);
    await user.click(await screen.findByRole("button", { name: "Editar soporte@acme.test" }));
    const dialog = await screen.findByRole("dialog", { name: "Editar buzón" });
    expect(within(dialog).getByText("Contraseña: configurada. Se guarda en la bóveda y la API nunca la devuelve. Déjalo en blanco para conservarla.")).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Contraseña nueva")).toHaveValue("");
    await user.click(within(dialog).getByRole("button", { name: "Microsoft 365" }));
    expect(within(dialog).getByText(/^Al guardar se elimina la contraseña guardada\./)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Cancelar" }));
    await user.click(screen.getByRole("button", { name: "Editar ventas@acme.test" }));
    const second = await screen.findByRole("dialog", { name: "Editar buzón" });
    expect(within(second).getByText(/^Contraseña: sin configurar\./)).toBeInTheDocument();
  });

  it("asks for a category ID when the category list is refused, and says none exist when empty", async () => {
    const user = userEvent.setup();
    baseApi(ME_ADMIN, { listTicketCategories: vi.fn().mockRejectedValue(new ApiError(403, "forbidden")) });
    const forbidden = renderAt(<EmailIntakeAdminPage />);
    await user.click(await screen.findByRole("button", { name: "Añadir buzón" }));
    const dialog = await screen.findByRole("dialog", { name: "Añadir buzón" });
    expect(within(dialog).getByPlaceholderText("ID de la categoría")).toBeInTheDocument();
    forbidden.unmount();

    baseApi(ME_ADMIN, { listTicketCategories: vi.fn().mockResolvedValue([]) });
    renderAt(<EmailIntakeAdminPage />);
    await user.click(await screen.findByRole("button", { name: "Añadir buzón" }));
    const empty = await screen.findByRole("dialog", { name: "Añadir buzón" });
    await user.click(within(empty).getByRole("combobox", { name: "Categoría predeterminada" }));
    expect(await within(empty).findByText(/^Todavía no existen categorías de tickets\./)).toBeInTheDocument();
  });

  it("words a failed save and holds the failure, not its sentence", async () => {
    const user = userEvent.setup();
    const api = baseApi(ME_ADMIN, { createEmailMailbox: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    renderAt(<EmailIntakeAdminPage />);
    await user.click(await screen.findByRole("button", { name: "Añadir buzón" }));
    const dialog = await screen.findByRole("dialog", { name: "Añadir buzón" });
    await user.type(within(dialog).getByLabelText("Dirección"), "nuevo@acme.test");
    await user.type(within(dialog).getByLabelText("Servidor IMAP"), "imap.acme.test");
    await user.type(within(dialog).getByLabelText("Nombre de usuario"), "nuevo@acme.test");
    await user.type(within(dialog).getByLabelText("Contraseña"), "una-clave");
    await user.click(within(dialog).getByRole("combobox", { name: "Categoría predeterminada" }));
    await user.click(await within(dialog).findByRole("option", { name: /Equipos/ }));
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Ese servicio no está disponible en este momento.");
    expect(api.createEmailMailbox).toHaveBeenCalled();
    act(() => setActiveLanguage("en"));
    expect(within(dialog).getByRole("alert")).toHaveTextContent("That service is unavailable right now.");
  });
});

// ------------------------------------------------------ the Microsoft connection

describe("the Microsoft 365 connection in Spanish", () => {
  const APP: OAuthAppOut = {
    tenant_id: "contoso.onmicrosoft.com",
    client_id: "11111111-2222-3333-4444-555555555555",
    has_secret: true,
    redirect_uri: "https://itms.example/settings/email-intake/oauth/callback",
  };

  it("words the panel, and says Connect with Microsoft is off when the server has no console address", async () => {
    baseApi(ME_ADMIN);
    const { container } = renderAt(<EmailIntakeAdminPage />);
    const panel = (await screen.findByRole("heading", { name: "Conexión con Microsoft 365" })).closest("section") as HTMLElement;
    expect(within(panel).getByText("La aplicación de Azure con la que inician sesión los buzones de Microsoft 365.")).toBeInTheDocument();
    expect(await within(panel).findByText(/^Conectar con Microsoft está desactivado en este servidor/)).toBeInTheDocument();
    expect(within(panel).getByText("Lo que debe definir quien administra el servidor")).toBeInTheDocument();
    expect(within(panel).getByText("ITS_PUBLIC_CONSOLE_URL").tagName).toBe("CODE");
    expect(within(panel).getByLabelText("ID de directorio (inquilino)")).toBeInTheDocument();
    expect(within(panel).getByText("Está en la página Información general de la aplicación de Azure.")).toBeInTheDocument();
    expect(within(panel).getByLabelText("ID de aplicación (cliente)")).toBeInTheDocument();
    expect(within(panel).getByText(/^Secreto de cliente: sin establecer\./)).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "Guardar conexión con Microsoft 365" })).toBeInTheDocument();
    expectNoEnglish(container, [...RECORDED, ...WIRE, "ITS_PUBLIC_CONSOLE_URL", "contoso.onmicrosoft.com"]);
  });

  it("words a stored secret and the return address to register", async () => {
    baseApi(ME_ADMIN, { getEmailOAuthApp: vi.fn().mockResolvedValue(APP) });
    const { container } = renderAt(<EmailIntakeAdminPage />);
    expect(await screen.findByText(/^Secreto de cliente: establecido\. Deja el campo en blanco para conservarlo\./)).toBeInTheDocument();
    expect(screen.getByText("Dirección de retorno que registrar en Azure")).toBeInTheDocument();
    expect(screen.getByText(APP.redirect_uri as string)).toBeInTheDocument();
    expect(screen.getByText("Añade esta dirección exacta en la aplicación de Azure, en Autenticación → Web → URI de redirección.")).toBeInTheDocument();
    // "email" is the path segment of the recorded return address
    // (".../settings/email-intake/oauth/callback"), a word the notification
    // catalogue also uses for a channel kind; this render only.
    expectNoEnglish(container, [...RECORDED, ...WIRE, "contoso.onmicrosoft.com", "email"]);
  });

  it("refuses a missing client secret without holding the secret, and re-words on a switch", async () => {
    const user = userEvent.setup();
    baseApi(ME_ADMIN, { getEmailOAuthApp: vi.fn().mockResolvedValue({ ...APP, has_secret: false }) });
    renderAt(<EmailIntakeAdminPage />);
    await screen.findByDisplayValue("contoso.onmicrosoft.com");
    await user.click(screen.getByRole("button", { name: "Guardar conexión con Microsoft 365" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("El campo Secreto de cliente es obligatorio.");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent("Client secret is required.");
    act(() => setActiveLanguage("es"));
    await user.type(screen.getByLabelText("Secreto de cliente"), "planted-secret");
    await user.clear(screen.getByLabelText("ID de directorio (inquilino)"));
    await user.click(screen.getByRole("button", { name: "Guardar conexión con Microsoft 365" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("El campo ID de directorio (inquilino) es obligatorio.");
    expect(alert).not.toHaveTextContent("planted-secret");
  });
});

// ----------------------------------------------------------------- the message log

describe("the message log in Spanish", () => {
  it("words the columns, the dispositions, the excerpt notes and the ticket links", async () => {
    baseApi(ME_READ_ONLY);
    const { container } = renderAt(<EmailIntakeAdminPage />);
    const panel = (await screen.findByRole("heading", { name: "Mensajes entrantes" })).closest("section") as HTMLElement;
    expect(within(panel).getByText("En qué se convirtió cada mensaje, en el orden en que lo registró esta plataforma.")).toBeInTheDocument();
    for (const header of ["Mensaje", "Resultado", "Registrado", "Ticket"]) {
      expect(await within(panel).findByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(within(panel).getByLabelText("Recuento en cuarentena")).toHaveTextContent("42 en cuarentena");
    expect(within(panel).getByText("en este ámbito")).toBeInTheDocument();
    expect(within(panel).getByText("Mostrando 3 de 300 mensajes coincidentes.")).toBeInTheDocument();
    const filter = within(panel).getByLabelText("Filtrar por resultado");
    expect(within(filter).getByRole("option", { name: "Todos los resultados" })).toBeInTheDocument();
    for (const option of [
      "Creó un ticket",
      "Añadió un comentario",
      "Reabrió un ticket",
      "En cuarentena: no es de confianza",
      "Rechazado: se detectó un bucle de correo",
      "Rechazado: se alcanzó el límite de frecuencia",
      "Rechazado: no se pudo interpretar",
    ]) {
      expect(within(filter).getByRole("option", { name: option })).toBeInTheDocument();
    }
    const first = within(panel).getByText("La impresora falla").closest("tr") as HTMLElement;
    expect(within(first).getByText("Creó un ticket")).toBeInTheDocument();
    expect(within(first).getByRole("link", { name: "Abrir el ticket de La impresora falla" })).toHaveTextContent("Abrir ticket");
    const quarantined = within(panel).getByText("Aviso raro").closest("tr") as HTMLElement;
    expect(within(quarantined).getByText("Sin texto en el mensaje")).toBeInTheDocument();
    expect(within(quarantined).getByText("Sin motivo registrado")).toBeInTheDocument();
    expect(within(quarantined).getByText("Sin ticket")).toBeInTheDocument();
    const long = within(panel).getByText("Informe largo").closest("tr") as HTMLElement;
    expect(within(long).getByText("Truncado (extracto de 4 KiB)")).toBeInTheDocument();
    expect(within(long).getByText("tamaño excesivo")).toBeInTheDocument();
    expectNoEnglish(container, [...RECORDED, ...WIRE]);
  });

  it("narrows to the quarantined mail from the address, and re-words the dispositions on a switch", async () => {
    const api = baseApi(ME_READ_ONLY);
    renderAt(<EmailIntakeAdminPage />, "/email-intake?disposition=quarantined#messages");
    const filter = await screen.findByLabelText("Filtrar por resultado");
    await waitFor(() => expect(filter).toHaveValue("quarantined"));
    expect(api.listEmailIntakeMessages).toHaveBeenCalledWith(expect.objectContaining({ disposition: "quarantined" }));
    act(() => setActiveLanguage("en"));
    expect(within(screen.getByLabelText("Filter by disposition")).getByRole("option", { name: "Refused: mail loop detected" })).toBeInTheDocument();
    expect(screen.getByText("42 quarantined")).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    expect(screen.getByText("42 en cuarentena")).toBeInTheDocument();
  });
});

// ----------------------------------------------------------- Microsoft's return page

describe("the Microsoft return page in Spanish", () => {
  const PATH = "/settings/email-intake/oauth/callback";
  function renderCallback(api: Partial<ApiClient>, query: string) {
    mockUseApi.mockReturnValue(api as ApiClient);
    return renderAt(<OAuthCallback />, `${PATH}${query}`, PATH);
  }

  it("says the sign-in finished and names the mailbox", async () => {
    const { container } = renderCallback(
      { completeMailboxOAuth: vi.fn().mockResolvedValue({ ...MS_OK, address: "conectado@acme.test" }) },
      "?state=s&code=c",
    );
    expect(await screen.findByRole("heading", { name: "Inicio de sesión de Microsoft 365" })).toBeInTheDocument();
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Conectado como conectado@acme.test. Activa el buzón en Entrada de correo cuando quieras que su correo se convierta en tickets.",
    );
    expect(screen.getByRole("button", { name: "Volver a Entrada de correo" })).toBeInTheDocument();
    expectNoEnglish(container, ["conectado@acme.test"]);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("status")).toHaveTextContent("Connected as conectado@acme.test. Switch the mailbox on in Email intake");
  });

  it("says nothing changed when the sign-in was cancelled", async () => {
    const { container } = renderCallback({ abandonMailboxOAuth: vi.fn().mockResolvedValue(undefined) }, "?state=s&error=access_denied");
    expect(await screen.findByText(/^El inicio de sesión de Microsoft no se completó, así que no cambió nada\./)).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("says the page was not reached from a sign-in the console started, and re-words it", async () => {
    const { container } = renderCallback({}, "");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Sin conectar. Esta página no se abrió desde un inicio de sesión de Microsoft iniciado por esta consola.",
    );
    expectNoEnglish(container);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent("Not connected. This page was not opened by a Microsoft sign-in this console started.");
  });

  it("holds a failed completion as the failure and re-words it on a switch", async () => {
    renderCallback({ completeMailboxOAuth: vi.fn().mockRejectedValue(new ApiError(503, "")) }, "?state=s&code=c");
    expect(await screen.findByRole("alert")).toHaveTextContent(/^Sin conectar\. Ese servicio no está disponible/);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent(/^Not connected\. That service is unavailable/);
    act(() => setActiveLanguage("es"));
    expect(screen.getByRole("alert")).toHaveTextContent(/^Sin conectar\. Ese servicio/);
  });
});

// ------------------------------------------------------------- the prerequisites

describe("the prerequisites strip in Spanish", () => {
  function panelApi(over: Record<string, unknown> = {}, permissions = ["ticket.read", "email_intake.read"]) {
    mockUseApi.mockReturnValue({
      me: meWith(permissions),
      listTicketCategories: vi.fn().mockResolvedValue([{ id: "c1" }]),
      listEmailIntakeMessages: vi.fn().mockResolvedValue({ messages: [], total: 0, quarantined_count: 0 }),
      ...over,
    } as unknown as ApiClient);
  }
  const held = (from: string) => ({ id: from, from_address: from, disposition: "quarantined", reason: `unknown sender: ${from}` });

  it("says no category exists, and counts the senders who are not users, agreeing with the count", async () => {
    panelApi({
      listTicketCategories: vi.fn().mockResolvedValue([]),
      listEmailIntakeMessages: vi.fn().mockResolvedValue({ messages: [held("a@x.example")], total: 1, quarantined_count: 1 }),
    });
    setActiveLanguage("es");
    const one = renderPanel(<EmailIntakePrerequisites />);
    expect(await screen.findByText("Antes de que el correo pueda convertirse en tickets")).toBeInTheDocument();
    expect(screen.getByText("Lo que necesitan la mesa de ayuda y la configuración para que un buzón cree tickets.")).toBeInTheDocument();
    expect(await screen.findByText(/^Cada buzón crea sus tickets en una categoría y todavía no existe ninguna/)).toBeInTheDocument();
    expect(await screen.findByText(/^El correo de 1 dirección que no pertenece a ningún usuario de esta plataforma se retuvo/)).toBeInTheDocument();
    expect(screen.getByText(/Si es alguien de tu equipo, añádelo como usuario\.$/)).toBeInTheDocument();
    await expectCta("Crear una categoría", "/service-desk/admin");
    await expectCta("Añadir a estos remitentes como usuarios", "/settings?tab=users");
    expectNoEnglish(one.container);
    one.unmount();

    panelApi({
      listEmailIntakeMessages: vi.fn().mockResolvedValue({ messages: [held("a@x.example"), held("b@x.example")], total: 2, quarantined_count: 2 }),
    });
    const two = renderPanel(<EmailIntakePrerequisites />);
    expect(await screen.findByText(/^El correo de 2 direcciones que no pertenecen a ningún usuario de esta plataforma se retuvo/)).toBeInTheDocument();
    expect(screen.getByText(/Si son personas de tu equipo, añádelas como usuarios\.$/)).toBeInTheDocument();
    expect(await screen.findByText("Existe 1 categoría de tickets donde puede entrar el correo.")).toBeInTheDocument();
    expectNoEnglish(two.container);
  });

  it("agrees the done sentence with the number of categories", async () => {
    panelApi({
      listTicketCategories: vi.fn().mockResolvedValue([{ id: "c1" }, { id: "c2" }]),
      listEmailIntakeMessages: vi.fn().mockResolvedValue({ messages: [held("a@x.example")], total: 1, quarantined_count: 1 }),
    });
    setActiveLanguage("es");
    renderPanel(<EmailIntakePrerequisites />);
    expect(await screen.findByText("Existen 2 categorías de tickets donde puede entrar el correo.")).toBeInTheDocument();
  });

  it("says it cannot be checked from an account without the permission", async () => {
    panelApi({ listTicketCategories: vi.fn().mockRejectedValue(new ApiError(403, "{}")) }, ["email_intake.read"]);
    setActiveLanguage("es");
    const { container } = renderPanel(<EmailIntakePrerequisites />);
    expect((await screen.findAllByText(/No se puede comprobar desde esta cuenta/)).length).toBeGreaterThan(0);
    expectNoEnglish(container);
  });
});

// ---------------------------------------------------- outside React: label tables

describe("the disposition label table", () => {
  it("words every disposition and never prints an unknown one raw", () => {
    setActiveLanguage("es");
    expect(dispositionLabel("created")).toBe("Creó un ticket");
    expect(dispositionLabel("commented")).toBe("Añadió un comentario");
    expect(dispositionLabel("reopened")).toBe("Reabrió un ticket");
    expect(dispositionLabel("quarantined")).toBe("En cuarentena: no es de confianza");
    expect(dispositionLabel("refused_loop")).toBe("Rechazado: se detectó un bucle de correo");
    expect(dispositionLabel("refused_rate")).toBe("Rechazado: se alcanzó el límite de frecuencia");
    expect(dispositionLabel("refused_parse")).toBe("Rechazado: no se pudo interpretar");
    expect(dispositionLabel("auto_reply")).toBe("Auto reply");
    expect(dispositionLabel("auto_reply")).not.toBe("auto_reply");
    setActiveLanguage("en");
    expect(dispositionLabel("quarantined")).toBe("Quarantined: not trusted");
  });
});
