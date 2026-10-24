/**
 * Settings in Spanish, tab by tab: the section list, Organization, Email,
 * Roles, Users, Assistant, AI connections, Notifications, Service levels,
 * Health and Budget,
 * with their dialogs and refusals, plus the label tables and sentences built
 * outside React. Organization and role names, addresses, hostnames, queue and
 * channel names, the server's own permission descriptions and every recorded
 * setting read as recorded; the words around them are Spanish, including the
 * assistant's egress disclosure and provider sentences (the ai-admin module's).
 *
 * The harness fixtures (`__tests__/harness.tsx`) are the same ones the English
 * tests use. Each test sets Spanish first, so the render is the Spanish one.
 */

import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage, translate } from "../../i18n/core";
import { expectNoEnglish } from "../../i18n/test-utils";
import { ApiError } from "../../lib/api";
import {
  AUDITOR_ROLE,
  apiClient,
  BUDGET_SETTINGS_FIXTURE,
  CASEY_USER,
  CATEGORY_FIXTURE,
  CHANNEL_FIXTURE,
  EMAIL_SETTINGS_FIXTURE,
  EMAIL_UNCONFIGURED_FIXTURE,
  HEALTH_SETTINGS_FIXTURE,
  MCP_CONNECTIONS_FIXTURE,
  OPERATOR_USER,
  ORG_FIXTURE,
  PLANTED_SECRET,
  QUEUE_FIXTURE,
  RULE_FIXTURE,
  SLA_SETTINGS_FIXTURE,
  TECHNICIAN_ROLE,
  WEBHOOK_CHANNEL_FIXTURE,
  calls,
  installDefaults,
  renderSettings,
} from "./__tests__/harness";
import { testSentMessage } from "./EmailTab";
import { clientPickerOptions } from "./pickerOptions";
import { permissionReason } from "./settingsState";
import { settingsHref } from "./tabs";
import { describeGrantScope, orgSettingChoices, orgSettingLabel, userStatusLabel } from "./types";

vi.mock("../../lib/auth", () => ({
  accessToken: () => "test-token",
  oidcAuthority: "https://id.example.test/realms/it-suite",
}));
vi.mock("../../lib/ApiContext", () => ({ useApi: () => apiClient() }));

/**
 * The org the Spanish renders use unless a test says otherwise: only keys the
 * server's allowlist carries, so every caption is the console's own. The
 * harness org (`default_timezone`, `evidence_retention_days`, ...) names keys
 * the console has not met, which read as their own English names; the tests
 * that are about that set it themselves.
 */
const KNOWN_ORG = {
  id: "org-1",
  name: "Toeshee Group",
  settings: { timezone: "America/Bogota", default_page_size: 50, show_device_serials: true, date_format: "iso" },
};

beforeEach(() => {
  installDefaults();
  calls.getCurrentOrg.mockResolvedValue(KNOWN_ORG);
  calls.updateCurrentOrg.mockResolvedValue(KNOWN_ORG);
});

/** Opens Settings in Spanish at a tab (`?tab=`), and waits for the page to answer `/me`. */
async function openIn(tab: string | null, expectTab = true) {
  setActiveLanguage("es");
  const view = renderSettings(tab ? `/settings?tab=${tab}` : "/settings");
  if (expectTab) await screen.findByRole("tab", { selected: true });
  return view;
}

/** Recorded words the fixtures put on screen: names, addresses, queue and channel names. */
const RECORDED = [
  "Toeshee Group",
  "Operator",
  "Casey Doe",
  "operator@example.com",
  "casey@example.com",
  "Operations mailbox",
  "Ops chat",
  "Escalations",
  "Incident",
  "Warehouse",
  "technician",
  "auditor",
];

describe("the settings page in Spanish", () => {
  it("words the section list, the heading and the states of the sections that can say", async () => {
    const { container } = await openIn(null);
    expect(await screen.findByRole("heading", { name: "Configuración" })).toBeInTheDocument();
    expect(screen.getByText("Configuración que se aplica en toda esta organización.")).toBeInTheDocument();
    expect(screen.getByText("Los demás módulos se configuran desde su propia página.")).toBeInTheDocument();
    const tablist = screen.getByRole("tablist", { name: "Secciones de la configuración" });
    expect(within(tablist).getAllByRole("tab").map((tab) => tab.firstChild?.textContent)).toEqual([
      "Organización",
      "Correo electrónico",
      "Roles",
      "Usuarios",
      "Asistente",
      "Conexiones de IA",
      "Notificaciones",
      "Niveles de servicio",
      "Salud",
      "Presupuesto",
    ]);
    // Email, Service levels and Health each say whether they are set up.
    await waitFor(() => expect(within(tablist).getAllByText("Configurado")).toHaveLength(3));
    expectNoEnglish(container, RECORDED);
  });

  it("says a section is not set up", async () => {
    calls.getEmailSettings.mockResolvedValue(EMAIL_UNCONFIGURED_FIXTURE);
    await openIn(null);
    expect(await screen.findByText("Sin configurar")).toBeInTheDocument();
  });

  it("re-words the section list and the heading on a live switch", async () => {
    await openIn(null);
    await screen.findAllByText("Configurado");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("heading", { name: "Settings" })).toBeInTheDocument();
    expect(screen.getByRole("tablist", { name: "Settings sections" })).toBeInTheDocument();
    expect(screen.getAllByText("Set up")).toHaveLength(3);
    expect(screen.getByRole("tab", { name: /^Service levels/ })).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    expect(screen.getByRole("tab", { name: /^Niveles de servicio/ })).toBeInTheDocument();
  });
});

// ------------------------------------------------------------- Organization

describe("the organization tab in Spanish", () => {
  const withKeys = {
    ...ORG_FIXTURE,
    settings: {
      date_format: "dmy",
      console_banner_severity: "warning",
      default_page_size: 50,
      show_device_serials: true,
      support_email: "it@toeshee.example",
      smtp_password: PLANTED_SECRET,
      escalation_matrix: { tier1: "ops" },
    },
  };

  it("words the profile, the known keys, the choices and the note about keys it will not show", async () => {
    calls.getCurrentOrg.mockResolvedValue(withKeys);
    const { container } = await openIn(null);
    expect(await screen.findByLabelText("Nombre de la organización")).toHaveValue("Toeshee Group");
    expect(screen.getByText("Perfil de la organización")).toBeInTheDocument();
    expect(screen.getByLabelText("Tamaño de página predeterminado")).toHaveValue(50);
    expect(screen.getByLabelText("Correo de soporte")).toHaveValue("it@toeshee.example");
    expect(screen.getByRole("switch", { name: "Mostrar números de serie de los dispositivos" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    const format = screen.getByRole("group", { name: "Formato de fecha" });
    expect(within(format).getByRole("button", { name: "Día/mes/año (08/09/2026)" })).toHaveAttribute("aria-pressed", "true");
    expect(within(format).getByRole("button", { name: "Año-mes-día (2026-09-08)" })).toBeInTheDocument();
    const severity = screen.getByRole("group", { name: "Gravedad del aviso de la consola" });
    expect(within(severity).getByRole("button", { name: "Advertencia" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText(/^No se pueden editar aquí: escalation_matrix, smtp_password\./)).toBeInTheDocument();
    // The planted secret is never rendered, in either language.
    expect(container.textContent).not.toContain(PLANTED_SECRET);
    expectNoEnglish(container, [...RECORDED, "it@toeshee.example"]);
  });

  it("reads a key the console has not met as its own name in words", async () => {
    calls.getCurrentOrg.mockResolvedValue(ORG_FIXTURE);
    await openIn(null);
    // `default_timezone` is not an allowlisted key: it stays as the server named it.
    expect(await screen.findByLabelText("Default timezone")).toHaveValue("America/Bogota");
    expect(orgSettingLabel("default_timezone")).toBe("Default timezone");
  });

  it("holds a refusal as data and re-words it on a switch", async () => {
    const user = userEvent.setup();
    await openIn(null);
    const name = await screen.findByLabelText("Nombre de la organización");
    await user.clear(name);
    await user.click(screen.getByRole("button", { name: "Guardar cambios" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("El campo Nombre de la organización es obligatorio");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent("Organization name is required");
    act(() => setActiveLanguage("es"));
    expect(screen.getByRole("alert")).toHaveTextContent(/obligatorio/);
  });

  it("refuses a number that is not a number and one that is negative, in Spanish", async () => {
    const user = userEvent.setup();
    calls.getCurrentOrg.mockResolvedValue(ORG_FIXTURE);
    await openIn(null);
    const days = await screen.findByLabelText("Evidence retention days");
    await user.clear(days);
    await user.type(days, "-3");
    await user.click(screen.getByRole("button", { name: "Guardar cambios" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("evidence_retention_days no puede ser negativo, y -3 lo es");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent("evidence_retention_days cannot be negative, and -3 is");
  });

  it("refuses a save with nothing to save out loud, and re-words it", async () => {
    const user = userEvent.setup();
    await openIn(null);
    await screen.findByLabelText("Nombre de la organización");
    await user.click(screen.getByRole("button", { name: "Guardar cambios" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No hay cambios sin guardar");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent("There are no unsaved changes");
  });

  it("says there are unsaved changes, then confirms the rename in Spanish", async () => {
    const user = userEvent.setup();
    const { container } = await openIn(null);
    const name = await screen.findByLabelText("Nombre de la organización");
    await user.clear(name);
    await user.type(name, "Toeshee Holding");
    expect(screen.getByText("Cambios sin guardar en esta pestaña. No se escribe nada hasta que pulses Guardar.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Guardar cambios" }));
    const dialog = await screen.findByRole("dialog", { name: "Cambiar el nombre de esta organización" });
    expect(within(dialog).getByText("Toeshee Group → Toeshee Holding")).toBeInTheDocument();
    expect(within(dialog).getByText(/^El nuevo nombre aparece en todas partes a partir de ahora\./)).toBeInTheDocument();
    // "Group" is the recorded organization name ("Toeshee Group") in the dialog's subject.
    expectNoEnglish(container, [...RECORDED, "Toeshee Holding", "Group"]);
    const renamed = { ...KNOWN_ORG, name: "Toeshee Holding" };
    calls.updateCurrentOrg.mockResolvedValue(renamed);
    calls.getCurrentOrg.mockResolvedValue(renamed);
    await user.click(within(dialog).getByRole("button", { name: "Cambiar el nombre de la organización" }));
    await waitFor(() => expect(calls.updateCurrentOrg).toHaveBeenCalled());
    expect(await screen.findByRole("status")).toHaveTextContent("Guardado. Los valores de abajo son los que devolvió el servidor.");
  });

  it("says the profile is not served, or not allowed", async () => {
    calls.getCurrentOrg.mockRejectedValue(new ApiError(404, ""));
    const first = await openIn(null);
    expect(await screen.findByText("El perfil de la organización no está disponible en este servidor.")).toBeInTheDocument();
    expect(screen.getByText(/^GET \/api\/v1\/orgs\/current respondió 404\./)).toBeInTheDocument();
    expect(screen.getByText("Este servidor no ofrece el perfil de la organización, así que no hay dónde guardar.")).toBeInTheDocument();
    expectNoEnglish(first.container, RECORDED);
    first.unmount();
    calls.getCurrentOrg.mockRejectedValue(new ApiError(403, ""));
    const second = await openIn(null);
    expect(await screen.findByText("Perfil de la organización no disponible (permisos insuficientes: orgs.manage).")).toBeInTheDocument();
    expectNoEnglish(second.container, RECORDED);
  });

  it("says a read-only account may not save", async () => {
    installDefaults([]);
    await openIn(null);
    expect(await screen.findByText(/^Solo lectura: este cambio requiere el permiso orgs\.manage\./)).toBeInTheDocument();
  });

  it("words a failed load with the reason", async () => {
    calls.getCurrentOrg.mockRejectedValue(new ApiError(503, ""));
    const { container } = await openIn(null);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/^No se pudo cargar\. /);
    expect(alert.textContent).toMatch(/Vuelve a intentarlo o consulta la página de estado de la plataforma\.$/);
    expectNoEnglish(container, RECORDED);
  });
});

// -------------------------------------------------------------------- Email

describe("the email tab in Spanish", () => {
  it("words the configured form, the password custody and the test box", async () => {
    const { container } = await openIn("email");
    expect(await screen.findByLabelText("Servidor de correo")).toHaveValue("smtp.internal");
    expect(screen.getByText("El servidor de correo para invitaciones, restablecimientos de contraseña y notificaciones")).toBeInTheDocument();
    for (const group of ["Conexión", "Identidad del remitente", "Autenticación", "Mensaje de prueba"]) {
      expect(screen.getByText(group)).toBeInTheDocument();
    }
    const encryption = screen.getByRole("group", { name: "Cifrado" });
    expect(within(encryption).getByRole("button", { name: "StartTLS · puerto 587" })).toHaveAttribute("aria-pressed", "true");
    expect(within(encryption).getByRole("button", { name: "Ninguno · conexión sin cifrar" })).toBeInTheDocument();
    expect(screen.getByText(/^Contraseña: establecida\. Deja el campo en blanco para conservarla\./)).toBeInTheDocument();
    expect(screen.getByText(/^Prueba la configuración guardada:/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Enviar un mensaje de prueba" })).toBeInTheDocument();
    expect(container.textContent).not.toContain(PLANTED_SECRET);
    expectNoEnglish(container, [...RECORDED, "smtp.internal", "itms@toeshee.com", "support@toeshee.com", "ITMS", "itms"]);
  });

  it("says email is not set up, and disables the test", async () => {
    calls.getEmailSettings.mockResolvedValue(EMAIL_UNCONFIGURED_FIXTURE);
    const { container } = await openIn("email");
    expect(await screen.findByText("El correo no está configurado, así que no se puede invitar a nadie ni restablecer ninguna contraseña.")).toBeInTheDocument();
    expect(screen.getByText(/^Contraseña: sin establecer\./)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Enviar un mensaje de prueba" })).toBeDisabled();
    expect(screen.getByText("Antes de guardar, indica un servidor de correo, una dirección del remitente y un puerto válido.")).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });

  it("words the Microsoft 365 sign-in, its replace notice and its advanced box", async () => {
    const user = userEvent.setup();
    const { container } = await openIn("email");
    await screen.findByLabelText("Servidor de correo");
    const signIn = screen.getByRole("group", { name: "Inicio de sesión" });
    await user.click(within(signIn).getByRole("button", { name: "Microsoft 365" }));
    expect(screen.getByLabelText("ID de directorio (inquilino)")).toBeInTheDocument();
    expect(screen.getByLabelText("ID de aplicación (cliente)")).toBeInTheDocument();
    expect(screen.getByText(/^Secreto de cliente: sin establecer\./)).toBeInTheDocument();
    expect(screen.getByText(/^Al guardar, la contraseña guardada del servidor de correo se sustituirá por un inicio de sesión de Microsoft 365\./)).toBeInTheDocument();
    expect(screen.getByText("Microsoft 365 envía a través de smtp.office365.com, puerto 587, StartTLS.")).toBeInTheDocument();
    expect(screen.getByText(/^Avanzado: solo para una nube de Microsoft gubernamental o nacional/)).toBeInTheDocument();
    expect(screen.getByText(/^Antes de guardar, indica un servidor de correo, una dirección del remitente, un puerto válido, el nombre de usuario/)).toBeInTheDocument();
    expectNoEnglish(container, [...RECORDED, "smtp.internal", "itms@toeshee.com", "support@toeshee.com", "ITMS", "itms", "smtp.office365.com"]);
  });

  it("refuses a bad port and a password with no username before the round trip", async () => {
    const user = userEvent.setup();
    await openIn("email");
    const port = await screen.findByLabelText("Puerto");
    await user.clear(port);
    await user.type(port, "99999");
    expect(screen.getByText("El puerto debe ser un número entre 1 y 65535.")).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Nombre de usuario"));
    await user.type(screen.getByLabelText("Contraseña"), "x");
    expect(screen.getByText(/^La contraseña de un servidor de correo necesita un nombre de usuario\./)).toBeInTheDocument();
  });

  it("says what a test send did, and re-words it on a switch", async () => {
    const user = userEvent.setup();
    await openIn("email");
    await user.click(await screen.findByRole("button", { name: "Enviar un mensaje de prueba" }));
    expect(await screen.findByText("Enviado a support@toeshee.com: revisa ese buzón. Un servidor de correo puede aceptar un mensaje y aun así descartarlo.")).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("Sent to support@toeshee.com — check that mailbox. A mail server can accept a message and still drop it.")).toBeInTheDocument();
  });

  it("holds a failed test send and re-words it", async () => {
    const user = userEvent.setup();
    calls.sendTestEmail.mockRejectedValue(new ApiError(503, ""));
    await openIn("email");
    await user.click(await screen.findByRole("button", { name: "Enviar un mensaje de prueba" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/^No se envió\. /);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent(/^Not sent\. /);
  });

  it("is not available without the permission", async () => {
    calls.getEmailSettings.mockRejectedValue(new ApiError(403, ""));
    const { container } = await openIn("email");
    expect(await screen.findByText("La configuración del correo no está disponible (permisos insuficientes: orgs.manage).")).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });
});

// -------------------------------------------------------------------- Roles

describe("the roles tab in Spanish", () => {
  it("words the table, the scope column and the delete guard", async () => {
    const { container } = await openIn("roles");
    expect(await screen.findByText("Roles y permisos")).toBeInTheDocument();
    await screen.findByRole("button", { name: "Editar los permisos de technician" });
    expect(screen.getByText("Qué significa cada rol en esta organización y cuántas personas lo tienen")).toBeInTheDocument();
    for (const header of ["Rol", "Pertenece a", "Usuarios", "Permisos"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(screen.getByText("Toda la plataforma")).toBeInTheDocument();
    expect(screen.getByText("Esta organización")).toBeInTheDocument();
    const platformWide = screen.getByRole("button", { name: "Eliminar el rol technician" });
    expect(platformWide).toBeDisabled();
    expect(platformWide).toHaveAttribute("title", "Un rol de toda la plataforma lo comparten todas las organizaciones y no se elimina desde aquí.");
    expect(screen.getByRole("button", { name: "Editar los permisos de technician" })).toBeInTheDocument();
    // The roles' own descriptions are server text, recorded.
    expectNoEnglish(container, [...RECORDED, "Day", "day", "fleet", "operations", "Read", "only", "across", "platform"]);
  });

  it("agrees the holder count with its number, and says a platform-wide role is copied", async () => {
    const user = userEvent.setup();
    const { container } = await openIn("roles");
    await user.click(await screen.findByRole("button", { name: "Editar los permisos de technician" }));
    expect(screen.getByText("Permisos de technician")).toBeInTheDocument();
    expect(screen.getByText("7 usuarios tienen technician y se verán afectados por este cambio.")).toBeInTheDocument();
    expect(screen.getByText(/^technician es de toda la plataforma\. Al guardar se edita una copia para esta organización/)).toBeInTheDocument();
    expect(screen.getByText("Todos los códigos de permiso que define esta plataforma, agrupados por módulo")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Guardar permisos" })).toBeDisabled();
    // The role and permission descriptions are server text, recorded; so are the module names.
    expectNoEnglish(container, [...RECORDED, "Day", "day", "fleet", "operations", "Read", "only", "across", "platform", "View", "Manage", "Redefine", "devices", "roles", "users", "budgets", "budget"]);
    await user.click(screen.getByRole("button", { name: "Editar los permisos de auditor" }));
    expect(screen.getByText("0 usuarios tienen auditor y se verán afectados por este cambio.")).toBeInTheDocument();
  });

  it("tells the operator a copy was made when a save comes back under another id", async () => {
    const user = userEvent.setup();
    calls.setRolePermissions.mockResolvedValue({ role_id: "copy-1", codes: ["devices.read", "devices.manage"], affected_users: 7 });
    await openIn("roles");
    await user.click(await screen.findByRole("button", { name: "Editar los permisos de technician" }));
    await user.click(screen.getByRole("checkbox", { name: /devices\.manage/ }));
    await user.click(screen.getByRole("button", { name: "Guardar permisos" }));
    expect(await screen.findByText("technician se copió en tu organización y estás editando la copia. Ninguna otra organización se ve afectada.")).toBeInTheDocument();
  });

  it("confirms a delete with the number of holders, agreed", async () => {
    const user = userEvent.setup();
    calls.listRoles.mockResolvedValue({ roles: [{ ...AUDITOR_ROLE, assigned_users: 1 }, { ...AUDITOR_ROLE, id: "r-3", name: "viewer", assigned_users: 3 }] });
    await openIn("roles");
    await user.click(await screen.findByRole("button", { name: "Eliminar el rol auditor" }));
    const dialog = await screen.findByRole("dialog", { name: "Eliminar rol" });
    expect(within(dialog).getByText(/^¿Eliminar el rol auditor\? Ahora mismo lo tiene 1 persona en esta organización\./)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Eliminar auditor" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Cancelar" })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Cancelar" }));
    await user.click(screen.getByRole("button", { name: "Eliminar el rol viewer" }));
    expect(await screen.findByText(/Ahora mismo lo tienen 3 personas en esta organización\./)).toBeInTheDocument();
  });

  it("words the create form and the refusal of a create", async () => {
    const user = userEvent.setup();
    calls.createRole.mockRejectedValue(new ApiError(503, ""));
    const { container } = await openIn("roles");
    await user.type(await screen.findByLabelText("Nombre del rol nuevo"), "soporte");
    expect(screen.getByText("Crear un rol")).toBeInTheDocument();
    expect(screen.getByText("Se crea sin permisos. Dales acceso en la matriz de arriba.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Crear rol" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/no está disponible/);
    expectNoEnglish(container, [...RECORDED, "Day", "day", "fleet", "operations", "Read", "only", "across", "platform", "soporte"]);
  });

  it("says roles are not available without the permission", async () => {
    calls.listRoles.mockRejectedValue(new ApiError(403, ""));
    await openIn("roles");
    expect(await screen.findByText("Los roles no están disponibles (permisos insuficientes: roles.read).")).toBeInTheDocument();
  });

  it("says nothing is defined yet", async () => {
    calls.listRoles.mockResolvedValue({ roles: [] });
    await openIn("roles");
    expect(await screen.findByText("Todavía no hay roles definidos para esta organización. Crea uno abajo.")).toBeInTheDocument();
  });
});

// -------------------------------------------------------------------- Users

describe("the users tab in Spanish", () => {
  it("words the directory, the statuses, the role chips and the actions", async () => {
    const { container } = await openIn("users");
    expect(await screen.findByText("casey@example.com")).toBeInTheDocument();
    expect(screen.getByText("Todas las personas que pueden iniciar sesión en esta organización y lo que tiene cada una")).toBeInTheDocument();
    for (const header of ["Persona", "Estado", "Roles", "Acciones"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(screen.getByText("activo")).toBeInTheDocument();
    expect(screen.getByText("deshabilitado")).toBeInTheDocument();
    expect(screen.getByText("Sin roles")).toBeInTheDocument();
    expect(screen.getByText(/· en todas partes/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Administrar los roles de operator@example.com" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Permisos efectivos de operator@example.com" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Deshabilitar a operator@example.com" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Habilitar a casey@example.com" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restablecer la contraseña de casey@example.com" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reenviar la invitación a casey@example.com" })).toBeInTheDocument();
    expect(container.textContent).not.toContain(PLANTED_SECRET);
    expectNoEnglish(container, RECORDED);
  });

  it("words the roles panel for one person: the grants, the scope and the removal", async () => {
    const user = userEvent.setup();
    const { container } = await openIn("users");
    await user.click(await screen.findByRole("button", { name: "Administrar los roles de operator@example.com" }));
    const section = await screen.findByRole("region", { name: "Roles de operator@example.com" });
    // RE-POINTED (MSP clients, phase 1): a grant is limited to a CLIENT now,
    // never to a device group.
    expect(within(section).getByText("El acceso se aplica en todas partes salvo que lo limites a un cliente")).toBeInTheDocument();
    expect(within(section).getByRole("button", { name: "Quitar Técnico (en todas partes) de operator@example.com" })).toBeInTheDocument();
    expect(within(section).getByRole("option", { name: "Elige un rol…" })).toBeInTheDocument();
    expect(within(section).getByText("Limitar a un cliente (opcional)")).toBeInTheDocument();
    expect(within(section).getByPlaceholderText("Déjalo en blanco para aplicarlo en todas partes")).toBeInTheDocument();
    expect(within(section).getByRole("button", { name: "Asignar rol" })).toBeDisabled();
    expectNoEnglish(container, RECORDED);
  });

  it("says a person holds no roles, and marks a duplicate grant", async () => {
    const user = userEvent.setup();
    const twice = {
      ...OPERATOR_USER,
      roles: [
        { assignment_id: "a1", role_id: TECHNICIAN_ROLE.id, role_name: "technician", client_id: null },
        { assignment_id: "a2", role_id: TECHNICIAN_ROLE.id, role_name: "technician", client_id: null },
      ],
    };
    calls.listUsers.mockResolvedValue({ users: [twice, CASEY_USER] });
    await openIn("users");
    await user.click(await screen.findByRole("button", { name: "Administrar los roles de operator@example.com" }));
    const section = await screen.findByRole("region", { name: "Roles de operator@example.com" });
    expect(within(section).getByText("(duplicado 1 de 2)")).toBeInTheDocument();
    expect(within(section).getByRole("button", { name: "Quitar Técnico (en todas partes, duplicado 2 de 2) de operator@example.com" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Administrar los roles de casey@example.com" }));
    expect(await screen.findByText("casey@example.com no tiene ningún rol, así que no puede acceder a nada.")).toBeInTheDocument();
  });

  it("words the effective permissions, with the scope of each grant", async () => {
    const user = userEvent.setup();
    const { container } = await openIn("users");
    await user.click(await screen.findByRole("button", { name: "Permisos efectivos de operator@example.com" }));
    const section = await screen.findByRole("region", { name: "Permisos efectivos de operator@example.com" });
    expect(await within(section).findByText("devices.manage")).toBeInTheDocument();
    expect(within(section).getByText("Todos los códigos que tiene esta persona y el rol del que proceden")).toBeInTheDocument();
    for (const header of ["Permiso", "Concedido por", "Se aplica a"]) {
      expect(within(section).getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(within(section).getByText("en todas partes")).toBeInTheDocument();
    expect(within(section).getByText("limitado a Warehouse")).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });

  it("words the create form, and what happens when the invitation could not be sent", async () => {
    const user = userEvent.setup();
    calls.createUser.mockResolvedValue({ ...CASEY_USER, status: "active", enrolment_email_sent: false, enrolment_email_error: "No mail relay is configured on this server." });
    const { container } = await openIn("users");
    expect(await screen.findByText("Crear un usuario")).toBeInTheDocument();
    expect(screen.getByText("Crea la identidad de inicio de sesión y después el registro de ITMS que guarda sus roles")).toBeInTheDocument();
    expect(screen.getByText("Dar acceso a")).toBeInTheDocument();
    expect(screen.getByText(/^Aquí no se establece ninguna contraseña/)).toBeInTheDocument();
    await user.type(screen.getByLabelText("Correo electrónico"), "casey@example.com");
    await user.click(screen.getByRole("button", { name: "Crear usuario" }));
    const alert = await screen.findByText(/^Se creó casey@example\.com, pero no se envió el correo de invitación/);
    expect(alert.textContent).toBe(
      "Se creó casey@example.com, pero no se envió el correo de invitación: No mail relay is configured on this server. Hasta que llegue, esta cuenta no puede iniciar sesión. Corrige el envío del correo y usa «Reenviar invitación» en su fila.",
    );
    // The server's own reason for the failure is recorded text.
    expectNoEnglish(container, [...RECORDED, "configured", "server"]);
    act(() => setActiveLanguage("en"));
    expect(screen.getByText(/^casey@example\.com was created, but the enrolment email was not sent/)).toBeInTheDocument();
  });

  it("confirms each action in Spanish, with the question carrying the address", async () => {
    const user = userEvent.setup();
    await openIn("users");
    await user.click(await screen.findByRole("button", { name: "Deshabilitar a operator@example.com" }));
    let dialog = await screen.findByRole("dialog", { name: "Deshabilitar usuario" });
    expect(within(dialog).getByText(/^¿Deshabilitar a operator@example\.com\? Se cerrará su sesión en su siguiente solicitud/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Cancelar" }));

    await user.click(screen.getByRole("button", { name: "Restablecer la contraseña de casey@example.com" }));
    dialog = await screen.findByRole("dialog", { name: "Enviar un restablecimiento de contraseña" });
    expect(within(dialog).getByText(/^¿Enviar el correo de restablecimiento a casey@example\.com\? La persona recibe por correo un enlace/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Enviar correo de restablecimiento a casey@example.com" })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Cancelar" }));

    await user.click(screen.getByRole("button", { name: "Eliminar a casey@example.com" }));
    dialog = await screen.findByRole("dialog", { name: "Eliminar usuario" });
    expect(within(dialog).getByText(/^¿Eliminar a casey@example\.com\? Se elimina su identidad de inicio de sesión\./)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Cancelar" }));
  });

  it("confirms removing a role, and holds a refusal inside the dialog", async () => {
    const user = userEvent.setup();
    calls.removeUserRole.mockRejectedValue(new ApiError(503, ""));
    await openIn("users");
    await user.click(await screen.findByRole("button", { name: "Administrar los roles de operator@example.com" }));
    await user.click(await screen.findByRole("button", { name: "Quitar Técnico (en todas partes) de operator@example.com" }));
    const dialog = await screen.findByRole("dialog", { name: "Quitar un rol" });
    expect(within(dialog).getByText(/^¿Quitar Técnico de operator@example\.com\? Solo se quita esta asignación\./)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Quitar el rol de operator@example.com" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/no está disponible/);
    act(() => setActiveLanguage("en"));
    expect(within(dialog).getByText(/^Remove Technician from operator@example\.com\? This removes just this one\./)).toBeInTheDocument();
  });

  it("points at the email tab when email is not set up", async () => {
    calls.getEmailSettings.mockResolvedValue(EMAIL_UNCONFIGURED_FIXTURE);
    const { container } = await openIn("users");
    const banner = await screen.findByRole("alert", { name: "El correo no está configurado" });
    expect(banner).toHaveTextContent("El correo no está configurado, así que no se pueden enviar invitaciones. Configurar el correo");
    expect(within(banner).getByRole("link", { name: "Configurar el correo" })).toHaveAttribute("href", settingsHref("email"));
    expectNoEnglish(container, RECORDED);
  });

  it("says the directory is not available without the permission", async () => {
    calls.listUsers.mockRejectedValue(new ApiError(403, ""));
    await openIn("users");
    expect(await screen.findByText("El directorio de usuarios no está disponible (permisos insuficientes: users.read).")).toBeInTheDocument();
  });

  it("says a read-only account may not act, on the table and under the form", async () => {
    installDefaults(["users.read", "roles.read"]);
    await openIn("users");
    const notes = await screen.findAllByText(/^Solo lectura: este cambio requiere el permiso users\.manage\./);
    expect(notes.length).toBeGreaterThan(0);
  });

  it("re-words the table, the status and the chips on a live switch", async () => {
    await openIn("users");
    await screen.findByText("activo");
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("active")).toBeInTheDocument();
    expect(screen.getByText("disabled")).toBeInTheDocument();
    expect(screen.getByText(/· everywhere/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Disable operator@example.com" })).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------- Assistant

describe("the assistant tab in Spanish", () => {
  it("words the limits, the retention and the server note", async () => {
    const { container } = await openIn("assistant");
    expect(await screen.findByLabelText("Conservar las conversaciones durante")).toHaveValue(30);
    expect(screen.getByText("Cuánto tiempo se conservan las conversaciones, los límites de frecuencia y el límite diario de uso")).toBeInTheDocument();
    expect(screen.getByText(/^Las preguntas y los datos necesarios para responderlas van a tu proveedor de modelos\./)).toBeInTheDocument();
    expect(screen.getByText("Lista completa de lo que sale y de lo que nunca sale")).toBeInTheDocument();
    expect(screen.getByText("Lo que sale")).toBeInTheDocument();
    expect(screen.getByText("Lo que nunca sale")).toBeInTheDocument();
    expect(screen.getByText("Retención")).toBeInTheDocument();
    expect(screen.getByText("Límites de frecuencia")).toBeInTheDocument();
    expect(screen.getByLabelText("Preguntas por persona y por minuto")).toHaveValue(5);
    expect(screen.getByLabelText("Preguntas por organización y por hora")).toHaveValue(100);
    expect(screen.getByLabelText("Límite diario de uso")).toHaveValue(null);
    expect(screen.getByText("Déjalo en blanco para no poner límite")).toBeInTheDocument();
    expect(screen.getByText(/^Que el asistente funcione y qué modelo use son configuración del servidor \(ITS_AI_\*\)/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Guardar configuración del asistente" })).toBeDisabled();
    expect(container.textContent).not.toContain(PLANTED_SECRET);
    expectNoEnglish(container, RECORDED);
  });

  it("says, in Spanish, everything the egress disclosure says in English, and re-words it on a live switch", async () => {
    const { container } = await openIn("assistant");
    await screen.findByLabelText("Conservar las conversaciones durante");
    const lines = [
      "Qué sale de esta red cuando responde el asistente",
      "El prompt del sistema, que es una constante.",
      "Los esquemas de las herramientas: la forma de las API de los módulos, es decir, estructura y no datos de clientes.",
      "El texto de la pregunta del administrador.",
      "Los resultados de las herramientas: datos reales de clientes. Nombres de host, asuntos y cuerpos de tickets, inventario de software, texto de alertas, nombres de licencias y de proveedores, cifras de gasto y nombres de las acciones de auditoría.",
      "El contenido de la bóveda: credenciales almacenadas y claves de licencia.",
      "Los bytes de los archivos adjuntos, las grabaciones de sesiones y los certificados de los agentes.",
      "Todo lo que pertenezca a otra organización.",
      "Lo predeterminado es un proveedor autoalojado y compatible con OpenAI en tu propia red: con él, no sale nada.",
      "Las condiciones de no entrenamiento y de no conservación con un proveedor alojado son contractuales. Esta plataforma no puede hacerlas cumplir y no finge que puede.",
    ];
    for (const line of lines) expect(container.textContent).toContain(line);
    // The four things that leave and the three that never do are lists of exactly that many lines.
    const lists = container.querySelectorAll("details ul");
    expect(lists[0]?.querySelectorAll("li")).toHaveLength(4);
    expect(lists[1]?.querySelectorAll("li")).toHaveLength(3);
    expectNoEnglish(container, RECORDED);

    act(() => setActiveLanguage("en"));
    expect(container.textContent).toContain("What leaves this network when the assistant answers");
    expect(container.textContent).toContain("The system prompt, which is a constant.");
    expect(container.textContent).toContain("Vault material: stored credentials and license keys.");
    expect(container.textContent).toContain("A self-hosted, OpenAI-compatible provider on your own network is the default: under it, nothing leaves.");
    expect(container.textContent).toContain("This platform cannot enforce them and does not pretend to.");
    for (const line of lines.slice(1)) expect(container.textContent).not.toContain(line);
    act(() => setActiveLanguage("es"));
    expect(container.textContent).toContain(lines[1] as string);
  });

  it("words the case of no provider, with the variable names kept exactly, and re-words it on a live switch", async () => {
    calls.listAiProviders.mockResolvedValue([]);
    const { container } = await openIn("assistant");
    const off = await screen.findByRole("status");
    expect(off.textContent).toBe(
      "El asistente está desactivado para todo el mundo en este servidor porque no hay ningún proveedor de modelos configurado. Alguien con acceso al servidor tiene que configurar uno.",
    );
    expect(screen.getByText("Qué tiene que configurar el administrador del servidor")).toBeInTheDocument();
    const detail = screen.getByText(/^Define ITS_AI_PROVIDERS y las variables correspondientes ITS_AI_<KEY>_KIND, _BASE_URL y _MODEL/);
    expect(detail.textContent).toContain("en el plano de control, y luego reinícialo. Se puede nombrar exactamente un proveedor.");
    expectNoEnglish(container, RECORDED);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("status").textContent).toBe(
      "The assistant is off for everyone on this server because no model provider is set up. Someone with server access has to configure one.",
    );
    expect(screen.getByText("What the server administrator needs to set")).toBeInTheDocument();
    expect(container.textContent).toContain("Exactly one provider may be named.");
  });

  it("is not available without the permission", async () => {
    calls.getAiSettings.mockRejectedValue(new ApiError(403, ""));
    await openIn("assistant");
    expect(await screen.findByText("La configuración del asistente no está disponible (permisos insuficientes: ai.admin).")).toBeInTheDocument();
  });
});

// ----------------------------------------------------------- AI connections

describe("the AI connections tab in Spanish", () => {
  const MCP_ORG = { ...KNOWN_ORG, settings: { ...KNOWN_ORG.settings, mcp_enabled: true } };

  it("words the switch, the address, the guide, the apps and the confirmation", async () => {
    calls.getCurrentOrg.mockResolvedValue(MCP_ORG);
    const user = userEvent.setup();
    const { container } = await openIn("ai-connections");
    const toggle = await screen.findByRole("switch", { name: "Permitir que las aplicaciones de IA se conecten a través de MCP" });
    await waitFor(() => expect(toggle).toHaveAttribute("aria-checked", "true"));
    expect(await screen.findByText(MCP_CONNECTIONS_FIXTURE.address)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Conexiones de IA" })).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Conectar una aplicación de IA" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Aplicación" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Último uso" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Revocar para mí: claude" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Revocar para todos: claude" }));
    const dialog = await screen.findByRole("dialog", { name: "Revocar esta aplicación de IA para todos" });
    expect(
      within(dialog).getByText(
        "Se cierran todas las sesiones actuales de esta aplicación en esta organización; las personas tendrán que volver a aprobarla para reconectarla.",
      ),
    ).toBeInTheDocument();
    expectNoEnglish(container.ownerDocument.body, [...RECORDED, "claude"]);
  });

  it("words the read-only view, the unset address and the empty list", async () => {
    installDefaults(["orgs.manage"]);
    calls.getCurrentOrg.mockResolvedValue(MCP_ORG);
    const first = await openIn("ai-connections");
    expect(await screen.findAllByText(/requieren el permiso mcp\.use/)).not.toHaveLength(0);
    expectNoEnglish(first.container, RECORDED);
    first.unmount();

    installDefaults(["mcp.use"]);
    calls.getCurrentOrg.mockResolvedValue(MCP_ORG);
    calls.getMcpConnections.mockResolvedValue({ ...MCP_CONNECTIONS_FIXTURE, address: null, apps: [] });
    const { container } = await openIn("ai-connections");
    expect(await screen.findByText(/ITS_PUBLIC_CONSOLE_URL no está configurada/)).toBeInTheDocument();
    expect(screen.getByText("Ninguna aplicación de IA se ha usado en esta organización en los últimos 90 días.")).toBeInTheDocument();
    expect(screen.getByText(permissionReason("orgs.manage"))).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });

  it("words the warning when sign-in could not be withdrawn, for one person and for you", async () => {
    calls.revokeMcpConnection.mockResolvedValue({ revoked: true, keycloak_failures: 1 });
    const user = userEvent.setup();
    const { container } = await openIn("ai-connections");
    await user.click(await screen.findByRole("button", { name: "Revocar para todos: claude" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Revocar para todos" }));
    const warning = await screen.findByText(/es posible que vuelva a conectarse sin preguntar/);
    expect(warning.textContent).toBe(
      "Se revocó claude y se rechaza cualquier sesión que ya tenga. Sin embargo, con 1 persona todavía no se pudo retirar el permiso de la aplicación para volver a iniciar sesión, así que es posible que vuelva a conectarse sin preguntar. Vuelve a intentar la revocación.",
    );
    expectNoEnglish(container, [...RECORDED, "claude"]);

    await user.click(screen.getByRole("button", { name: "Revocar para mí: claude" }));
    expect(await screen.findByText(/^Se revocó claude para ti y/)).toBeInTheDocument();
    expectNoEnglish(container, [...RECORDED, "claude"]);
  });

  it("asks before turning the switch on, in Spanish", async () => {
    calls.getCurrentOrg.mockResolvedValue(KNOWN_ORG);
    const user = userEvent.setup();
    const { container } = await openIn("ai-connections");
    const toggle = await screen.findByRole("switch", { name: "Permitir que las aplicaciones de IA se conecten a través de MCP" });
    await waitFor(() => expect(toggle).toBeEnabled());
    await user.click(toggle);
    expect(await screen.findByRole("dialog", { name: "Permitir que se conecten aplicaciones de IA" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Permitir aplicaciones de IA" })).toBeInTheDocument();
    // The dialog's subject is the org's recorded name, "Toeshee Group", whose
    // second word is also an English catalogue word.
    expectNoEnglish(container.ownerDocument.body, [...RECORDED, "Group"]);
  });

  it("re-words a revoke's outcome on a live switch", async () => {
    const user = userEvent.setup();
    await openIn("ai-connections");
    await user.click(await screen.findByRole("button", { name: "Revocar para mí: claude" }));
    expect(await screen.findByText("Se revocó claude para ti.")).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("claude is revoked for you.")).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    expect(screen.getByText("Se revocó claude para ti.")).toBeInTheDocument();
  });

  it("re-words the tab on a live switch", async () => {
    calls.getCurrentOrg.mockResolvedValue(MCP_ORG);
    await openIn("ai-connections");
    await screen.findByText(MCP_CONNECTIONS_FIXTURE.address);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("switch", { name: "Allow AI apps to connect over MCP" })).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Connect an AI app" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Revoke for everyone: claude" })).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    expect(screen.getByRole("columnheader", { name: "Personas" })).toBeInTheDocument();
  });
});

// ------------------------------------------------------------ Notifications

describe("the notifications tab in Spanish", () => {
  beforeEach(() => {
    calls.listNotificationChannels.mockResolvedValue([CHANNEL_FIXTURE, WEBHOOK_CHANNEL_FIXTURE]);
    calls.listTicketQueues.mockResolvedValue([QUEUE_FIXTURE]);
  });

  it("words the channel table, its credential state and the row actions", async () => {
    const { container } = await openIn("notifications");
    expect(await screen.findByText("Canales de notificación")).toBeInTheDocument();
    await screen.findByLabelText("Nombre de Operations mailbox");
    expect(screen.getByText("Adónde van las notificaciones salientes: un webhook o un servidor de correo")).toBeInTheDocument();
    for (const header of ["Nombre", "Tipo", "Credencial", "Activo", "Token nuevo"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(screen.getAllByText("guardada en la bóveda")).toHaveLength(2);
    expect(screen.getAllByText("activo").length).toBeGreaterThan(0);
    expect(screen.getByLabelText("Nombre de Operations mailbox")).toHaveValue("Operations mailbox");
    expect(screen.getByLabelText("Token nuevo de Ops chat")).toHaveAttribute("placeholder", "Déjalo en blanco para conservar el actual");
    expect(screen.getByText("Cambia el inicio de sesión del correo en Notificaciones → Canales.")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Enviar una prueba" })).toHaveLength(2);
    expect(container.textContent).not.toContain(PLANTED_SECRET);
    expectNoEnglish(container, [...RECORDED, "Ops chat", "Operations mailbox"]);
  });

  it("words a test send, delivered and not delivered, from the held result", async () => {
    const user = userEvent.setup();
    await openIn("notifications");
    const [first, second] = await screen.findAllByRole("button", { name: "Enviar una prueba" });
    await user.click(first as HTMLElement);
    expect(await screen.findByText("Prueba entregada (200).")).toBeInTheDocument();
    calls.testNotificationChannel.mockResolvedValue({ delivered: false, error: "algo_nuevo" });
    await user.click(second as HTMLElement);
    expect(await screen.findByText(/^Prueba no entregada: /)).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByText(/^Test not delivered: /)).toBeInTheDocument();
  });

  it("words the add-channel form for a webhook and for a mail server", async () => {
    const user = userEvent.setup();
    const { container } = await openIn("notifications");
    expect(await screen.findByText("Añadir un canal")).toBeInTheDocument();
    expect(screen.getByLabelText("URL")).toBeInTheDocument();
    expect(screen.getByLabelText("Token de portador")).toBeInTheDocument();
    expect(screen.getByText("Opcional")).toBeInTheDocument();
    const kind = screen.getByRole("group", { name: "Tipo" });
    await user.click(within(kind).getByRole("button", { name: "Correo electrónico" }));
    expect(screen.getByLabelText("Servidor SMTP")).toBeInTheDocument();
    expect(screen.getByLabelText("Puerto SMTP")).toBeInTheDocument();
    expect(screen.getByLabelText("Dirección del remitente")).toBeInTheDocument();
    expect(screen.getByLabelText("Usar TLS")).toBeChecked();
    expect(screen.getByRole("button", { name: "Crear canal" })).toBeDisabled();
    expectNoEnglish(container, [...RECORDED, "Ops chat", "Operations mailbox"]);
  });

  it("words the rules, the routing form and the empty-queue hint", async () => {
    calls.listTicketQueues.mockResolvedValue([]);
    const { container } = await openIn("notifications");
    expect(await screen.findByText("Reglas de notificación")).toBeInTheDocument();
    await screen.findByText("Cualquier cola");
    expect(screen.getByText("Qué evento va a qué canal y quién lo recibe")).toBeInTheDocument();
    expect(screen.getAllByText("Ticket asignado").length).toBeGreaterThan(0);
    expect(screen.getByText("Cualquier cola")).toBeInTheDocument();
    expect(screen.getByText("activa")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Deshabilitar" })).toBeInTheDocument();
    expect(screen.getByText("Enrutamiento")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Elige un canal" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Destinatarios" })).toBeInTheDocument();
    const hint = screen.getByText(/^Todavía no hay colas de tickets\./);
    expect(within(hint).getByRole("link", { name: "Crea una en la configuración de la mesa de ayuda" })).toHaveAttribute("href", "/service-desk/admin");
    expect(screen.getByRole("button", { name: "Crear regla" })).toBeDisabled();
    expectNoEnglish(container, [...RECORDED, "Operations mailbox"]);
  });

  it("words an inactive rule in the feminine and an inactive channel in the masculine", async () => {
    calls.listNotificationRules.mockResolvedValue([{ ...RULE_FIXTURE, active: false }]);
    calls.listNotificationChannels.mockResolvedValue([{ ...CHANNEL_FIXTURE, active: false }]);
    await openIn("notifications");
    expect(await screen.findByText("inactiva")).toBeInTheDocument();
    expect(screen.getByText("inactivo")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Habilitar" })).toBeInTheDocument();
  });

  it("degrades to a queue id when queues cannot be listed", async () => {
    calls.listTicketQueues.mockRejectedValue(new ApiError(403, ""));
    await openIn("notifications");
    const field = await screen.findByLabelText("ID de cola");
    expect(field).toBeInTheDocument();
    expect(screen.getByText("En blanco para cualquier cola.")).toBeInTheDocument();
  });

  it("is not available without the permission", async () => {
    calls.listNotificationChannels.mockRejectedValue(new ApiError(403, ""));
    await openIn("notifications");
    expect(await screen.findByText("Canales no disponibles (permisos insuficientes: notification.read).")).toBeInTheDocument();
  });
});

// ----------------------------------------------------------- Service levels

describe("the service levels tab in Spanish", () => {
  it("words the counts, the badge and the auto-close field", async () => {
    const { container } = await openIn("sla");
    expect(await screen.findByLabelText("Cerrar automáticamente los tickets resueltos tras")).toHaveValue(7);
    expect(screen.getByText("El plazo de cierre automático que se aplica en todas partes y dónde no se cumple el compromiso")).toBeInTheDocument();
    expect(screen.getByText("Tickets sin SLA:")).toBeInTheDocument();
    expect(screen.getByText("Plazos desactualizados:")).toBeInTheDocument();
    expect(screen.getByText("configurado")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Guardar niveles de servicio" })).toBeDisabled();
    expectNoEnglish(container, RECORDED);
  });

  it("agrees the unconfigured default with its number of days, and re-words it", async () => {
    calls.getSlaSettings.mockResolvedValue({ ...SLA_SETTINGS_FIXTURE, configured: false });
    await openIn("sla");
    expect(await screen.findByText("nunca se ha configurado; funciona con el valor predeterminado de 7 días")).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("never configured, running on the default of 7 days")).toBeInTheDocument();
  });

  it("refuses a value out of range with the bound in words", async () => {
    const user = userEvent.setup();
    await openIn("sla");
    const days = await screen.findByLabelText("Cerrar automáticamente los tickets resueltos tras");
    await user.clear(days);
    await user.type(days, "0");
    expect(screen.getByText(/^Debe ser un número entero entre 1 y \d+\. El servidor lo vuelve a comprobar/)).toBeInTheDocument();
    expect(screen.getByText("Corrige el valor de arriba antes de guardar.")).toBeInTheDocument();
  });

  it("is not available without the permission", async () => {
    calls.getSlaSettings.mockRejectedValue(new ApiError(403, ""));
    await openIn("sla");
    expect(await screen.findByText("La configuración de los niveles de servicio no está disponible (permisos insuficientes: sla.read).")).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------- Health

describe("the health tab in Spanish", () => {
  it("words the badges, the groups, the pickers and the numeric fields", async () => {
    calls.listTicketQueues.mockResolvedValue([QUEUE_FIXTURE, { ...QUEUE_FIXTURE, id: "queue-8", key: "legacy", name: "Legacy", active: false }]);
    calls.listTicketCategories.mockResolvedValue([CATEGORY_FIXTURE]);
    const { container } = await openIn("health");
    expect(await screen.findByText("Salud de los equipos")).toBeInTheDocument();
    await screen.findByText("configurado");
    expect(screen.getByText("Comportamiento de alertas, escalado y tickets en toda la organización")).toBeInTheDocument();
    expect(screen.getByText("configurado")).toBeInTheDocument();
    expect(screen.getByText("Las alertas todavía no pueden crear tickets")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Elige una cola y una categoría" })).toHaveAttribute("href", "#alert-tickets");
    for (const group of ["Tiempos y umbrales", "Alertas", "Monitorización"]) {
      expect(screen.getByText(group)).toBeInTheDocument();
    }
    expect(screen.getByText("Cola de alertas predeterminada")).toBeInTheDocument();
    expect(screen.getByText("Categoría de tickets predeterminada")).toBeInTheDocument();
    expect(screen.getByLabelText("Plantilla de enlace directo a la monitorización")).toBeInTheDocument();
    expect(screen.getByText("En blanco para ninguna")).toBeInTheDocument();
    expect(screen.getByLabelText("Resolver automáticamente los tickets de alerta cuando la alerta se recupera")).toBeChecked();
    expect(screen.getByRole("button", { name: "Guardar configuración de salud" })).toBeDisabled();
    expectNoEnglish(container, [...RECORDED, "Legacy", "legacy", "escalations"]);
  });

  it("words the degraded id boxes and the orphaned ids", async () => {
    calls.listTicketQueues.mockRejectedValue(new ApiError(403, ""));
    calls.listTicketCategories.mockRejectedValue(new ApiError(503, ""));
    const { container } = await openIn("health");
    expect(await screen.findByLabelText("ID de la cola de alertas predeterminada")).toBeInTheDocument();
    expect(screen.getByText("En blanco para ninguna. No se pudieron listar las colas de tickets (permisos insuficientes), así que esto sigue siendo un ID sin procesar.")).toBeInTheDocument();
    expect(screen.getByText("En blanco para ninguna. No se pudieron listar las categorías de tickets (la solicitud falló), así que esto sigue siendo un ID sin procesar.")).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });

  it("says an id that no longer exists, and offers the empty-list hint", async () => {
    calls.getHealthSettings.mockResolvedValue({ ...HEALTH_SETTINGS_FIXTURE, default_alert_queue_id: "gone-1", default_ticket_category_id: "gone-2" });
    calls.listTicketQueues.mockResolvedValue([QUEUE_FIXTURE]);
    calls.listTicketCategories.mockResolvedValue([CATEGORY_FIXTURE]);
    await openIn("health");
    expect(await screen.findByText("ID guardado gone-1: esta cola ya no existe.")).toBeInTheDocument();
    expect(screen.getByText("ID guardado gone-2: esta categoría de tickets ya no existe.")).toBeInTheDocument();
  });

  it("points to creating a category when there are none", async () => {
    calls.listTicketQueues.mockResolvedValue([QUEUE_FIXTURE]);
    calls.listTicketCategories.mockResolvedValue([]);
    await openIn("health");
    const hint = await screen.findByText(/^Todavía no hay categorías de tickets\./);
    expect(within(hint).getByRole("link", { name: "Crea una en la configuración de la mesa de ayuda" })).toHaveAttribute("href", "/service-desk/admin");
  });

  it("refuses a blank numeric field in words", async () => {
    const user = userEvent.setup();
    await openIn("health");
    const retention = await screen.findByLabelText("Eliminar las alertas cerradas tras");
    await user.clear(retention);
    const note = await screen.findByText(/^No es un número entero: alert_retention_days\./);
    expect(note).toHaveTextContent("un campo en blanco no es cero.");
    expect(screen.getByText("Corrige los campos numéricos de arriba antes de guardar.")).toBeInTheDocument();
  });

  it("is not available without the permission", async () => {
    calls.getHealthSettings.mockRejectedValue(new ApiError(403, ""));
    await openIn("health");
    expect(await screen.findByText("La configuración de salud no está disponible (permisos insuficientes: health.read).")).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------- Budget

describe("the budget tab in Spanish", () => {
  it("words the currency, the fiscal-year chips and who changed it last", async () => {
    const { container } = await openIn("budget");
    expect(await screen.findByLabelText("Moneda base")).toHaveValue("USD");
    expect(screen.getByText("La moneda base y el inicio del año fiscal que usa cada cifra del presupuesto")).toBeInTheDocument();
    expect(screen.getByText("ISO 4217, p. ej. EUR")).toBeInTheDocument();
    const months = screen.getByRole("group", { name: "El año fiscal empieza en" });
    expect(within(months).getAllByRole("button").map((button) => button.textContent)).toEqual([
      "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
    ]);
    expect(within(months).getByRole("button", { name: "Enero" })).toHaveAttribute("aria-pressed", "true");
    const changed = screen.getByText(/^Último cambio: /);
    expect(changed.textContent).toMatch(/^Último cambio: .* por admin\.$/);
    expect(screen.getByRole("button", { name: "Guardar configuración del presupuesto" })).toBeDisabled();
    expectNoEnglish(container, [...RECORDED, "admin"]);
  });

  it("asks for the acknowledgement when the currency changes, and re-words it", async () => {
    const user = userEvent.setup();
    await openIn("budget");
    const currency = await screen.findByLabelText("Moneda base");
    await user.clear(currency);
    await user.type(currency, "EUR");
    expect(screen.getByLabelText(/^Entiendo que los gastos que ya se convirtieron con la base anterior/)).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByLabelText(/^I understand that expenses already converted/)).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
  });

  it("is not available without the permission", async () => {
    calls.getBudgetSettings.mockRejectedValue(new ApiError(403, ""));
    await openIn("budget");
    expect(await screen.findByText("La configuración del presupuesto no está disponible (permisos insuficientes: budget.read).")).toBeInTheDocument();
  });

  it("re-words the months on a live switch", async () => {
    await openIn("budget");
    await screen.findByRole("button", { name: "Enero" });
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("button", { name: "January" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Fiscal year starts in" })).toBeInTheDocument();
  });
});

// ---------------------------------------------------- labels outside React

describe("the settings labels built outside React", () => {
  it("words the known org keys and the closed choices, and reads an unknown key as its own name", () => {
    setActiveLanguage("es");
    expect(orgSettingLabel("support_url")).toBe("URL de soporte");
    expect(orgSettingLabel("console_email_domain")).toBe("Dominio de correo de la consola");
    expect(orgSettingLabel("brand_primary_color")).toBe("Color principal de la marca");
    expect(orgSettingLabel("some_new_key")).toBe("Some new key");
    expect(orgSettingChoices("date_format", "iso")?.map((choice) => choice.label)).toEqual([
      "Año-mes-día (2026-09-08)",
      "Día/mes/año (08/09/2026)",
      "Mes/día/año (09/08/2026)",
    ]);
    // A stored value the table does not name is kept, as stored.
    expect(orgSettingChoices("date_format", "ymd")?.at(-1)).toMatchObject({ value: "ymd" });
    // The choices were read in Spanish above; the same objects read English after a switch.
    const choices = orgSettingChoices("console_banner_severity", "info") ?? [];
    expect(choices.map((choice) => choice.label)).toEqual(["Información", "Advertencia", "Crítica"]);
    setActiveLanguage("en");
    expect(choices.map((choice) => choice.label)).toEqual(["Information", "Warning", "Critical"]);
  });

  it("words the grant scope, the user status and the read-only reason", () => {
    setActiveLanguage("es");
    expect(describeGrantScope(null)).toBe("en todas partes");
    expect(describeGrantScope("g1", new Map([["g1", "Almacén"]]))).toBe("limitado a Almacén");
    expect(describeGrantScope("g2")).toBe("limitado a g2");
    expect(userStatusLabel("active")).toBe("activo");
    expect(userStatusLabel("deleted")).toBe("eliminado");
    // A status the console has not met is humanised, never printed as the wire word.
    expect(userStatusLabel("on_hold")).toBe("On hold");
    expect(permissionReason("users.manage")).toBe(
      "Solo lectura: este cambio requiere el permiso users.manage. Lo aplica el servidor; esta consola solo lo refleja.",
    );
    setActiveLanguage("en");
    expect(describeGrantScope(null)).toBe("everywhere");
    expect(userStatusLabel("disabled")).toBe("disabled");
  });

  it("words a test-send report and the client sub-label with the count agreed", () => {
    // RE-POINTED (MSP clients, phase 1): the grant-scope picker lists
    // clients, not device groups; the sub-label is still the device count.
    setActiveLanguage("es");
    expect(testSentMessage("a@b.c")).toBe("Enviado a a@b.c: revisa ese buzón. Un servidor de correo puede aceptar un mensaje y aun así descartarlo.");
    expect(testSentMessage(null)).toBe("Enviado: revisa el buzón remitente. Un servidor de correo puede aceptar un mensaje y aun así descartarlo.");
    const client = (count: number) => ({ id: "c", name: "Almacén", kind: "customer", status: "active", device_count: count });
    expect(clientPickerOptions([client(1)])[0]?.sublabel).toBe("1 dispositivo");
    expect(clientPickerOptions([client(6)])[0]?.sublabel).toBe("6 dispositivos");
    expect(translate("settings.clientPicker.emptyHint")).toMatch(/^No hay clientes para elegir\./);
    setActiveLanguage("en");
    expect(clientPickerOptions([client(1)])[0]?.sublabel).toBe("1 device");
  });
});

// ----------------------------------------------------------------- secrets

describe("secrets in the Spanish render", () => {
  it("never puts a planted secret on any tab", async () => {
    for (const tab of ["organization", "email", "roles", "users", "assistant", "notifications", "sla", "health", "budget"]) {
      installDefaults();
      const view = await openIn(tab === "organization" ? null : tab);
      await new Promise((resolve) => setTimeout(resolve, 30));
      expect(view.container.textContent, tab).not.toContain(PLANTED_SECRET);
      view.unmount();
    }
    // The planted password the email fixture carries is the same string.
    expect(EMAIL_SETTINGS_FIXTURE.password).toBe(PLANTED_SECRET);
    expect(BUDGET_SETTINGS_FIXTURE.base_currency).toBe("USD");
  });
});
