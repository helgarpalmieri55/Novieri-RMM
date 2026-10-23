/**
 * Enrollment in Spanish: the token tab (the list, the issue form with its
 * validation, the one-time reveal, the unlimited-token callout, the pager and
 * every confirmation), the certificate tab, the flow tab and the
 * prerequisites panel. A token's name, id and plaintext and a device's
 * hostname read as recorded; the words around them are Spanish.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { en } from "../../i18n/en";
import { es } from "../../i18n/es";
import { setActiveLanguage } from "../../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import type { ApiClient, Device } from "../../lib/api";
import { expectCta, meWith } from "../../test/prerequisiteHarness";
import { EnrollmentPage } from "./EnrollmentPage";
import { EnrollmentPrerequisites } from "./EnrollmentPrerequisites";
import type { EnrollmentTokenCreated, EnrollmentTokenListItem, EnrollmentTokenPage } from "./types";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const listEnrollmentTokens = vi.fn();
const createEnrollmentToken = vi.fn();
const revokeEnrollmentToken = vi.fn();
const revokeAgent = vi.fn();
vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api")>()),
  listEnrollmentTokens: (...a: unknown[]) => listEnrollmentTokens(...a),
  createEnrollmentToken: (...a: unknown[]) => createEnrollmentToken(...a),
  revokeEnrollmentToken: (...a: unknown[]) => revokeEnrollmentToken(...a),
  revokeAgent: (...a: unknown[]) => revokeAgent(...a),
}));

const FUTURE = "2099-01-01T00:00:00Z";
const PAST = "2020-01-01T00:00:00Z";

function token(overrides: Partial<EnrollmentTokenListItem> = {}): EnrollmentTokenListItem {
  return {
    id: "tttttttt-0000-0000-0000-000000000001",
    name: "Bogota HQ workstations",
    expires_at: FUTURE,
    max_uses: 50,
    use_count: 4,
    revoked: false,
    ...overrides,
  };
}

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
    cert_not_after: "2027-03-12T00:00:00Z",
    cert_state: "ok",
    cert_revoked: false,
    lifecycle: "in_service" as const,
    asset_type: "computer" as const,
    source: "agent" as const,
    provenance: "agent" as const,
    description: null,
    primary_ip: null,
    owner: null,
    location: null,
    ...overrides,
  };
}

const STORED = ["Bogota HQ workstations", "Windows"];

function renderPage(
  options: {
    tokens?: EnrollmentTokenListItem[];
    devices?: Device[];
    permissions?: string[];
    entry?: string;
    meFails?: boolean;
    total?: number;
  } = {},
) {
  const all = options.tokens ?? [token()];
  listEnrollmentTokens.mockImplementation(async (_api: unknown, params: { include_revoked?: boolean; unlimited_only?: boolean; limit?: number; offset?: number } = {}) => {
    const unlimited = (row: EnrollmentTokenListItem) => row.max_uses == null && !row.revoked && new Date(row.expires_at).getTime() > Date.now();
    let matching = params.include_revoked ? all : all.filter((row) => !row.revoked);
    if (params.unlimited_only) matching = matching.filter(unlimited);
    const offset = params.offset ?? 0;
    const limit = params.limit ?? matching.length;
    return {
      items: matching.slice(offset, offset + limit),
      total: options.total ?? matching.length,
      revoked_count: all.filter((row) => row.revoked).length,
      unlimited_count: all.filter(unlimited).length,
    } satisfies EnrollmentTokenPage;
  });
  const devices = options.devices ?? [device()];
  mockUseApi.mockReturnValue({
    me: () =>
      options.meFails
        ? Promise.reject(new Error("down"))
        : Promise.resolve({
            user_id: "u",
            org_id: "o",
            email: "op@example.com",
            display_name: "Operador",
            permissions: options.permissions ?? ["devices.read", "enrollment.manage"],
            timezone: "Europe/Madrid",
          }),
    listDevices: () => Promise.resolve({ items: devices, total: devices.length }),
    listAgentReleases: () => Promise.resolve([]),
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderInSpanish(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[options.entry ?? "/enrollment"]}>
        <EnrollmentPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockUseApi.mockReset();
  listEnrollmentTokens.mockReset();
  createEnrollmentToken.mockReset();
  revokeEnrollmentToken.mockReset();
  revokeAgent.mockReset();
});
afterEach(() => {
  Reflect.deleteProperty(navigator, "clipboard");
});

describe("the enrollment page in Spanish", () => {
  it("words the token tab: tiles, tabs, table, pills and the empty issue panel", async () => {
    const { container } = renderPage({
      tokens: [
        token(),
        token({ id: "t2", name: "Spent one", max_uses: 2, use_count: 2 }),
        token({ id: "t3", name: "Old one", expires_at: PAST }),
        token({ id: "t4", name: "Gone", revoked: true }),
      ],
    });
    expect(await screen.findByRole("heading", { name: "Inscripción" })).toBeInTheDocument();
    expect(await screen.findByText("Spent one")).toBeInTheDocument();
    for (const label of ["Equipos inscritos", "Conectados", "Certificados que requieren atención", "Tokens de inscripción activos"]) {
      expect([...document.querySelectorAll(".stat-label")].some((el) => el.textContent === label)).toBe(true);
    }
    expect(screen.getByRole("tab", { name: "Tokens de inscripción" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Certificados" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Flujo de inscripción" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Crear token" })).toBeInTheDocument();
    expect(screen.getByText("Emitir un token de inscripción")).toBeInTheDocument();
    expect(screen.getByText("No se está emitiendo ningún token")).toBeInTheDocument();
    const table = screen.getByRole("table");
    for (const header of ["Token", "Caduca", "Usos", "Estado", "Acciones"]) {
      expect(within(table).getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(within(table).getByText("Activo")).toBeInTheDocument();
    expect(within(table).getByText("Agotado")).toBeInTheDocument();
    expect(within(table).getByText("Caducado")).toBeInTheDocument();
    expect(within(table).getAllByRole("button", { name: "Revocar" }).length).toBe(3);
    expect(screen.getByText("Mostrar los tokens revocados (1 oculto)")).toBeInTheDocument();
    expectNoEnglish(container, STORED.concat(["Spent one", "Old one", "Gone"]));
  });

  it("words the unlimited callout, the unnamed token and the filtered empty state", async () => {
    const user = userEvent.setup();
    const { container } = renderPage({ tokens: [token({ id: "u1", name: "", max_uses: null })] });
    const alert = await screen.findByText(/1 token de inscripción no tiene límite de usos\./);
    expect(alert).toHaveTextContent("Cada uno puede inscribir cualquier cantidad de equipos");
    expect(screen.getByText("(sin nombre)")).toBeInTheDocument();
    expect(screen.getByText("ilimitado")).toBeInTheDocument();
    expect(screen.getByText("Solo los tokens sin límite de usos (1)")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Mostrarlos" }));
    expectNoEnglish(container, STORED);
  });

  it("validates the issue form in Spanish and asks to confirm in Spanish", async () => {
    const user = userEvent.setup();
    const { container } = renderPage();
    await screen.findByText("Bogota HQ workstations");
    await user.click(screen.getByRole("button", { name: "Crear token" }));
    expect(screen.getByLabelText("Nombre del token")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("p. ej., equipos de la sede de Bogotá")).toBeInTheDocument();
    expect(screen.getByLabelText("Vigencia en horas")).toBeInTheDocument();
    expect(screen.getByLabelText("Usos máximos")).toBeInTheDocument();
    expect(screen.getByText(/A cuántos equipos puede inscribir este token, de 1 a 1000\./)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancelar" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Emitir token…" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("El campo Nombre del token es obligatorio");
    const hours = screen.getByLabelText("Vigencia en horas");
    await user.clear(hours);
    await user.type(hours, "0");
    await user.type(screen.getByLabelText("Nombre del token"), "Lote");
    await user.click(screen.getByRole("button", { name: "Emitir token…" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "La vigencia debe ser un número entero de horas entre 1 y 2160 (90 días)",
    );
    await user.clear(hours);
    await user.type(hours, "1");
    const uses = screen.getByLabelText("Usos máximos");
    await user.clear(uses);
    await user.type(uses, "5000");
    await user.click(screen.getByRole("button", { name: "Emitir token…" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Los usos máximos deben ser un número entero entre 1 y 1000. No existe un token de inscripción ilimitado.",
    );
    await user.clear(uses);
    await user.type(uses, "1");
    await user.click(screen.getByRole("button", { name: "Emitir token…" }));
    const dialog = await screen.findByRole("dialog", { name: "Emitir token de inscripción" });
    expect(within(dialog).getByText(/Es válido durante 1 hora y puede canjearse una vez\./)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Emitir token" })).toBeInTheDocument();
    expectNoEnglish(container, STORED.concat(["Lote"]));
    expectNoEnglish(dialog, ["Lote"]);
  });

  it("words the one-time reveal, the copy states and the dismiss confirmation", async () => {
    const user = userEvent.setup();
    const created: EnrollmentTokenCreated = {
      id: "t9",
      token: "PLAINTEXT-TOKEN-VALUE",
      name: "Lote",
      expires_at: FUTURE,
      max_uses: 3,
    };
    createEnrollmentToken.mockResolvedValue(created);
    const { container } = renderPage();
    await screen.findByText("Bogota HQ workstations");
    await user.click(screen.getByRole("button", { name: "Crear token" }));
    await user.type(screen.getByLabelText("Nombre del token"), "Lote");
    const uses = screen.getByLabelText("Usos máximos");
    await user.clear(uses);
    await user.type(uses, "3");
    await user.click(screen.getByRole("button", { name: "Emitir token…" }));
    const confirm = await screen.findByRole("dialog", { name: "Emitir token de inscripción" });
    expect(within(confirm).getByText(/puede canjearse hasta 3 veces/)).toBeInTheDocument();
    await user.click(within(confirm).getByRole("button", { name: "Emitir token" }));
    expect(await screen.findByText("Copia este token ahora. Se muestra una sola vez.")).toBeInTheDocument();
    expect(screen.getByText("Este servidor solo conserva una huella del token y no puede volver a mostrarlo.")).toBeInTheDocument();
    expect(screen.getByText("PLAINTEXT-TOKEN-VALUE")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copiar token" })).toBeInTheDocument();
    expectNoEnglish(container, STORED.concat(["Lote"]));
    // Dismissing without copying asks first, in Spanish.
    await user.click(screen.getByRole("button", { name: "Ya lo he copiado" }));
    const dismiss = await screen.findByRole("dialog", { name: "Descartar el token de inscripción" });
    expect(within(dismiss).getByText(/No has copiado este token\./)).toBeInTheDocument();
    expect(within(dismiss).getByRole("button", { name: "Descartar sin copiar" })).toBeInTheDocument();
    expectNoEnglish(dismiss, ["Lote"]);
  });

  it("words the copy outcomes", async () => {
    const user = userEvent.setup();
    createEnrollmentToken.mockResolvedValue({ id: "t9", token: "PLAIN", name: "Lote", expires_at: FUTURE, max_uses: 1 });
    Object.defineProperty(navigator, "clipboard", { value: { writeText: () => Promise.reject(new Error("no")) }, configurable: true });
    renderPage();
    await screen.findByText("Bogota HQ workstations");
    await user.click(screen.getByRole("button", { name: "Crear token" }));
    await user.type(screen.getByLabelText("Nombre del token"), "Lote");
    await user.click(screen.getByRole("button", { name: "Emitir token…" }));
    await user.click(await screen.findByRole("button", { name: "Emitir token" }));
    await user.click(await screen.findByRole("button", { name: "Copiar token" }));
    expect(await screen.findByText(/Este navegador rechazó el acceso al portapapeles/)).toBeInTheDocument();
    Object.defineProperty(navigator, "clipboard", { value: { writeText: () => Promise.resolve() }, configurable: true });
    await user.click(screen.getByRole("button", { name: "Copiar token" }));
    expect(await screen.findByText("Copiado en el portapapeles.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copiado" })).toBeInTheDocument();
  });

  it("words the revoke confirmations and a refused revoke", async () => {
    const user = userEvent.setup();
    revokeEnrollmentToken.mockRejectedValue(new Error("boom"));
    const { container } = renderPage();
    await screen.findByText("Bogota HQ workstations");
    await user.click(screen.getByRole("button", { name: "Revocar" }));
    const dialog = await screen.findByRole("dialog", { name: "Revocar token de inscripción" });
    expect(within(dialog).getByText(/El token deja de funcionar de inmediato/)).toBeInTheDocument();
    expectNoEnglish(dialog, STORED);
    await user.click(within(dialog).getByRole("button", { name: "Revocar token" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("El token NO se revocó y todavía puede inscribir un dispositivo.");
    expectNoEnglish(container, STORED);
  });

  it("words the pager with its Spanish plural", async () => {
    const rows = Array.from({ length: 30 }, (_, at) => token({ id: `p${at}`, name: `Lote ${at}` }));
    const { container } = renderPage({ tokens: rows });
    expect(await screen.findByText("Página 1 de 2, 30 tokens")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Tokens siguientes" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Tokens anteriores" })).toBeInTheDocument();
    expectNoEnglish(container, STORED);
  });

  it("says in Spanish when there are no tokens or the permissions could not be read", async () => {
    const empty = renderPage({ tokens: [] });
    expect(await screen.findByText("No se ha emitido ningún token de inscripción.")).toBeInTheDocument();
    expectNoEnglish(empty.container, STORED);
    empty.unmount();
    const failed = renderPage({ meFails: true });
    expect(await screen.findByText(/No se pudieron leer tus permisos/)).toBeInTheDocument();
    expectNoEnglish(failed.container, STORED);
  });

  it("words the certificates tab: states, revoked toggle and revoke dialog", async () => {
    const user = userEvent.setup();
    revokeAgent.mockResolvedValue({ revoked: true });
    const { container } = renderPage({
      entry: "/enrollment?tab=certificates",
      devices: [
        device(),
        device({ id: "d2", hostname: "ws-beta", cert_state: "expiring" }),
        device({ id: "d3", hostname: "ws-gamma", cert_state: "renewing" }),
        device({ id: "d4", hostname: "ws-delta", cert_state: "expired" }),
        device({ id: "d5", hostname: "ws-eps", cert_revoked: true }),
        device({ id: "d6", hostname: "ws-zeta", cert_state: null, agent_status: null }),
      ],
    });
    expect(await screen.findByText("ws-beta")).toBeInTheDocument();
    const table = screen.getByRole("table");
    for (const header of ["Dispositivo", "Certificado", "Caduca", "Visto por última vez", "Acciones"]) {
      expect(within(table).getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(within(table).getByText("Válido")).toBeInTheDocument();
    expect(within(table).getByText("A punto de caducar")).toBeInTheDocument();
    expect(within(table).getByText("Renovándose")).toBeInTheDocument();
    expect(within(table).getByText("Caducado")).toBeInTheDocument();
    expect(within(table).getByText("Sin agente, por tanto sin certificado")).toBeInTheDocument();
    expect(within(table).getByText("Nada que revocar")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Buscar dispositivo, FQDN o estado del certificado")).toBeInTheDocument();
    expect(screen.getByText("Mostrar los certificados revocados (1 oculto)")).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox"));
    expect(screen.getByText("Mostrar los certificados revocados (1 visible)")).toBeInTheDocument();
    expect(within(screen.getByRole("table")).getByText("Revocado")).toBeInTheDocument();
    await user.click(within(screen.getByRole("table")).getAllByRole("button", { name: "Revocar agente" })[0]!);
    const dialog = await screen.findByRole("dialog", { name: "Revocar certificado del agente" });
    expect(within(dialog).getByText(/Se revoca el certificado, se corta la conexión del dispositivo/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Revocar certificado" })).toBeInTheDocument();
    expectNoEnglish(container, ["ws-alpha"]);
    expectNoEnglish(dialog, ["ws-alpha"]);
  });

  it("words the empty certificate states", async () => {
    const none = renderPage({ entry: "/enrollment?tab=certificates", devices: [] });
    expect(await screen.findByText("No hay dispositivos inscritos, así que no se ha emitido ningún certificado.")).toBeInTheDocument();
    none.unmount();
    const allRevoked = renderPage({ entry: "/enrollment?tab=certificates", devices: [device({ cert_revoked: true })] });
    expect(await screen.findByText(/Todos los certificados emitidos están revocados/)).toBeInTheDocument();
    expectNoEnglish(allRevoked.container, STORED);
  });

  it("words the enrollment flow tab", async () => {
    const { container } = renderPage({ entry: "/enrollment?tab=flow" });
    expect(await screen.findByText("Cómo se une un equipo a esta organización")).toBeInTheDocument();
    expect(screen.getByText("1 · Crea un token de inscripción de un solo uso")).toBeInTheDocument();
    expect(screen.getByText("Su mitad privada nunca sale del equipo.")).toBeInTheDocument();
    expect(screen.getByText("4 · El agente se conecta hacia este servidor")).toBeInTheDocument();
    expectNoEnglish(container, STORED);
  });

  it("words the prerequisites panel for a token with no published package", async () => {
    mockUseApi.mockReturnValue({
      me: meWith(["enrollment.manage", "agent.release.read"]),
      listAgentReleases: vi.fn().mockResolvedValue([]),
    } as unknown as ApiClient);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { container } = renderInSpanish(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <EnrollmentPrerequisites />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(await screen.findByText("Antes de que un token pueda inscribir un equipo")).toBeInTheDocument();
    expect(screen.getByText("Un token no instala nada: el agente se obtiene de un paquete del agente publicado.")).toBeInTheDocument();
    expect(screen.getByText(/Un token permite que un equipo se una una vez instalado el agente en él/)).toBeInTheDocument();
    await expectCta("Publicar un paquete del agente", "/agent-packages#releases");
    expectNoEnglish(container);
  });
});

describe("a live language switch", () => {
  it("re-words a refused issue form already on screen", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Bogota HQ workstations");
    await user.click(screen.getByRole("button", { name: "Crear token" }));
    await user.click(screen.getByRole("button", { name: "Emitir token…" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("El campo Nombre del token es obligatorio");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert")).toHaveTextContent("Token name is required");
  });

  it("re-words the tabs, the pills and the toggle on a mounted page", async () => {
    renderPage({ tokens: [token(), token({ id: "t4", name: "Gone", revoked: true })] });
    expect(await screen.findByText("Mostrar los tokens revocados (1 oculto)")).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("tab", { name: "Enrollment tokens" })).toBeInTheDocument();
    expect(screen.getByText("Show revoked tokens (1 hidden)")).toBeInTheDocument();
    expect(within(screen.getByRole("table")).getByText("Active")).toBeInTheDocument();
  });
});

describe("enrollment state words", () => {
  it("cover every token and certificate state in both languages", () => {
    const english = en.enums as Record<string, Record<string, string>>;
    const spanish = es.enums as Record<string, Record<string, string>>;
    for (const type of ["EnrollmentTokenState", "CertificateState"]) {
      expect(Object.keys(spanish[type]!).sort()).toEqual(Object.keys(english[type]!).sort());
    }
  });
});
