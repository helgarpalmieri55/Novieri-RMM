/**
 * Security logs in Spanish: the event search (tiles, filters, the list, the raw
 * fields panel, the empty states), the evidence export and its refusals, the
 * prerequisites strip, and the labels built outside React. Usernames,
 * hostnames and event sources read as recorded; the words around them are
 * Spanish.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { en } from "../../i18n/en";
import { es } from "../../i18n/es";
import { setActiveLanguage, translate } from "../../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import { ApiError } from "../../lib/api";
import type { ApiClient, DeviceListParams, Me, SecurityEvent } from "../../lib/api";
import { expectCta, meWith, renderPanel } from "../../test/prerequisiteHarness";
import { securityEventLabel } from "./eventBadges";
import { SecurityPage } from "./SecurityPage";
import { SecurityPrerequisites } from "./SecurityPrerequisites";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
vi.mock("../../lib/ws", () => ({ useLiveEvents: () => undefined }));

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "a@b.c",
  display_name: "Alex Morgan",
  permissions: ["audit.read"],
  timezone: "UTC",
} as Me;

function event(overrides: Partial<SecurityEvent> = {}): SecurityEvent {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    device_id: "d1",
    hostname: "DC-01",
    ts: "2026-08-16T02:32:08Z",
    event_type: "logon_failure",
    username: "jrivera",
    source: "10.20.16.87",
    outcome: "failure",
    ...overrides,
  };
}

const EVENTS = [
  event(),
  event({ id: "2", hostname: "DC-02", event_type: "account_lockout", username: "lmendez", source: "10.20.16.88" }),
  event({ id: "3", hostname: "DC-03", event_type: "logon_success", username: "", source: "10.20.16.89" }),
  event({ id: "4", hostname: "DC-04", event_type: "admin_group_change", username: "ana", source: "10.20.16.90" }),
];

/** Recorded fixture words the page prints as they are. */
const RECORDED = ["jrivera", "lmendez", "opsadmin", "DC-01", "DC-02", "DC-03", "DC-04"];

function renderPage(overrides: Partial<ApiClient> = {}, entry = "/security") {
  const api: Partial<ApiClient> = {
    me: vi.fn().mockResolvedValue(ME),
    listSecurityEvents: vi.fn().mockResolvedValue({ items: EVENTS, total: 40 }),
    getSecurityEvent: vi.fn(),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return {
    api,
    ...renderInSpanish(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[entry]}>
          <SecurityPage />
        </MemoryRouter>
      </QueryClientProvider>,
    ),
  };
}

/** jsdom implements neither object URLs nor following an anchor. */
function stubSave(): void {
  Object.assign(URL, {
    createObjectURL: vi.fn().mockReturnValue("blob:evidence"),
    revokeObjectURL: vi.fn(),
  });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
}

describe("the security logs page in Spanish", () => {
  it("renders the tiles, the filters and the event list", async () => {
    const { container } = renderPage();
    expect(await screen.findByRole("heading", { name: "Registros de seguridad" })).toBeInTheDocument();
    expect(await screen.findByText("DC-01")).toBeInTheDocument();
    expect(screen.getByText("Eventos en el periodo")).toBeInTheDocument();
    expect(screen.getByText("Inicios de sesión fallidos")).toBeInTheDocument();
    expect(screen.getByText("Cuentas vistas")).toBeInTheDocument();
    expect(screen.getByText("los últimos 7 días")).toBeInTheDocument();
    expect(screen.getAllByText("Sobre 4 eventos cargados")).toHaveLength(2);
    expect(screen.getByLabelText("Buscar por usuario")).toBeInTheDocument();
    expect(screen.getByLabelText("Filtrar por tipo de evento")).toBeInTheDocument();
    expect(screen.getByRole("radiogroup", { name: "Periodo" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Exportar CSV de evidencias \(7d\)/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Copiar enlace a esta vista/ })).toBeInTheDocument();
    for (const header of ["Evento", "Equipo", "Usuario", "Cuándo"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    // The pills and the select read the same Spanish event names.
    const select = screen.getByLabelText("Filtrar por tipo de evento");
    expect(within(select).getByRole("option", { name: "Todos los tipos de evento" })).toBeInTheDocument();
    expect(within(select).getByRole("option", { name: "Cambio en el grupo de administradores" })).toBeInTheDocument();
    expect(within(select).getByRole("option", { name: "Inicio de sesión correcto" })).toBeInTheDocument();
    const rows = [...container.querySelectorAll("tbody tr")];
    expect(rows.map((row) => row.querySelector(".cell-title")?.textContent)).toEqual([
      "Inicio de sesión fallido",
      "Cuenta bloqueada",
      "Inicio de sesión correcto",
      "Cambio en el grupo de administradores",
    ]);
    expect(screen.getByText("Sin cuenta registrada")).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });

  it("shows the selected event's stored fields, and the panel before one is chosen", async () => {
    const user = userEvent.setup();
    const getSecurityEvent = vi
      .fn()
      .mockResolvedValue({ ...EVENTS[0], raw: { Status: "0xC000006D", IpAddress: "185.12.4.9" } });
    const { container } = renderPage({ getSecurityEvent });
    await screen.findByText("DC-01");
    expect(screen.getByText("Evento sin procesar")).toBeInTheDocument();
    expect(screen.getByText("Campos almacenados de la fila seleccionada")).toBeInTheDocument();
    expect(screen.getByText("Ningún evento seleccionado")).toBeInTheDocument();
    expect(screen.getByText("Elige una fila de la lista de eventos para ver los campos almacenados con ella.")).toBeInTheDocument();
    await user.click((screen.getByText("DC-01").closest("tr")) as HTMLElement);
    expect(await screen.findByText("185.12.4.9")).toBeInTheDocument();
    // The stored values and their field names are recorded; the panel around them is Spanish.
    // The stored fields are the agent's own, so their names ("Status", "IP address", from the
    // recorded `Status` and `IpAddress`) read as recorded; the panel around them is Spanish.
    expectNoEnglish(container, [...RECORDED, "Status", "IP address"]);
  });

  it("says why the raw fields did not load, and that an event has none", async () => {
    const user = userEvent.setup();
    const getSecurityEvent = vi.fn().mockRejectedValueOnce(new ApiError(403, "")).mockResolvedValueOnce({ ...EVENTS[1], raw: {} });
    const { container } = renderPage({ getSecurityEvent });
    await screen.findByText("DC-01");
    await user.click((screen.getByText("DC-01").closest("tr")) as HTMLElement);
    expect(await screen.findByRole("alert")).toHaveTextContent(/permiso/i);
    await user.click((screen.getByText("DC-02").closest("tr")) as HTMLElement);
    expect(await screen.findByText("Este evento no tiene campos almacenados.")).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });

  it("says nothing was collected, and offers the wider window", async () => {
    const user = userEvent.setup();
    const { container } = renderPage({ listSecurityEvents: vi.fn().mockResolvedValue({ items: [], total: 0 }) }, "/security?hours=24");
    expect(await screen.findByText("No hay eventos en este periodo")).toBeInTheDocument();
    expect(
      screen.getByText("Ningún evento de seguridad llegó a esta organización en las últimas 24 horas. Aquí un registro vacío y un agente que no recoge nada se ven igual."),
    ).toBeInTheDocument();
    expect(screen.getByText("No se recogió nada en este periodo")).toBeInTheDocument();
    expect(screen.getByText("Ningún inicio de sesión fallido en este periodo")).toBeInTheDocument();
    expect(screen.getByText("No aparece ninguna cuenta en este periodo")).toBeInTheDocument();
    expectNoEnglish(container);
    await user.click(screen.getByRole("button", { name: "Ampliar a 30d" }));
    expect(await screen.findByText(/en los últimos 30 días\./)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Ampliar a/ })).toBeNull();
  });

  it("names the filters that emptied the list, in the user's own words", async () => {
    const { container } = renderPage(
      { listSecurityEvents: vi.fn().mockResolvedValue({ items: [], total: 0 }) },
      "/security?username=opsadmin&event_type=logon_failure&hours=24",
    );
    expect(await screen.findByText("Ningún evento coincide con estos filtros")).toBeInTheDocument();
    expect(
      screen.getByText(
        "No hay nada en las últimas 24 horas que coincida con usuario «opsadmin» y tipo de evento «Inicio de sesión fallido». Puede que el filtro sea más estrecho que la evidencia, o que esté escrito de forma distinta al nombre que notificó el equipo.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Quitar filtros" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ampliar a 30d" })).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });

  it("states what the export covers, and confirms a copied link", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const { container } = renderPage({}, "/security?username=opsadmin&hours=24");
    await screen.findByText("DC-01");
    expect(
      screen.getByText(
        "El CSV abarca las últimas 24 horas e incluye los filtros de usuario y de tipo de evento de esta pantalla, así que el archivo es lo que estás viendo.",
      ),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Copiar enlace a esta vista/ }));
    expect(await screen.findByRole("status")).toHaveTextContent("Enlace copiado. Vuelve a abrir esta búsqueda con los mismos filtros.");
    expectNoEnglish(container, RECORDED);
  });

  it("words a failed export from the held error, and re-words it on a switch", async () => {
    const user = userEvent.setup();
    stubSave();
    renderPage({ exportAccessEvidence: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    await user.click(await screen.findByRole("button", { name: /Exportar CSV de evidencias/ }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/^La exportación ha fallado\. /);
    expect(alert.textContent).toMatch(/no está disponible/i);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert").textContent).toMatch(/^The export failed\. /);
    act(() => setActiveLanguage("es"));
    expect(screen.getByRole("alert")).toHaveTextContent(/^La exportación ha fallado\. /);
  });

  it("says the list did not load", async () => {
    const { container } = renderPage({ listSecurityEvents: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    expect(await screen.findByText(/no está disponible/i)).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("re-words every tile, label and sentence on a live switch", async () => {
    const { container } = renderPage({}, "/security?username=opsadmin&event_type=logon_failure&hours=24");
    await screen.findByText("DC-01");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("heading", { name: "Security logs" })).toBeInTheDocument();
    expect([...container.querySelectorAll(".stat-foot")].map((el) => el.textContent)).toEqual([
      "the last 24 hours",
      "Across the 4 events loaded",
      "Across the 4 events loaded",
    ]);
    expect(screen.getByLabelText("Filter by event type")).toBeInTheDocument();
    expect(screen.getByText("Logon failure", { selector: ".cell-title *" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Export evidence CSV \(24h\)/ })).toBeInTheDocument();
    expect(screen.getByText(/^The CSV covers the last 24 hours and carries/)).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    expect(screen.getByRole("button", { name: /Exportar CSV de evidencias \(24h\)/ })).toBeInTheDocument();
    expect([...container.querySelectorAll(".stat-foot")].map((el) => el.textContent)[0]).toBe("las últimas 24 horas");
  });

  it("re-words the filtered empty state on a live switch", async () => {
    renderPage(
      { listSecurityEvents: vi.fn().mockResolvedValue({ items: [], total: 0 }) },
      "/security?username=opsadmin&event_type=account_lockout&hours=24",
    );
    expect(await screen.findByText(/en blanco|que coincida con usuario «opsadmin» y tipo de evento «Cuenta bloqueada»\./)).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(
      screen.getByText(/^Nothing in the last 24 hours matches username “opsadmin” and event type “Account lockout”\./),
    ).toBeInTheDocument();
  });
});

describe("the event labels built outside React", () => {
  it("names every known event type in both languages, and humanises one it has not met", () => {
    setActiveLanguage("es");
    const types = ["logon_success", "logon_failure", "logoff", "account_lockout", "privilege_elevation", "admin_group_change", "policy_change"];
    expect(types.map(securityEventLabel)).toEqual([
      "Inicio de sesión correcto",
      "Inicio de sesión fallido",
      "Cierre de sesión",
      "Cuenta bloqueada",
      "Elevación de privilegios",
      "Cambio en el grupo de administradores",
      "Cambio de política",
    ]);
    // A type the catalogue lacks is humanised, never printed as the wire word.
    expect(securityEventLabel("future_event_type")).toBe("Future event type");
    setActiveLanguage("en");
    expect(types.map(securityEventLabel)).toEqual(Object.values(en.enums.SecurityEventType));
    expect(Object.keys(es.enums.SecurityEventType)).toEqual(Object.keys(en.enums.SecurityEventType));
  });

  it("agrees the loaded-events note with its count", () => {
    setActiveLanguage("es");
    expect(translate("security.page.loadedNote", { count: 1 })).toBe("Sobre 1 evento cargado");
    expect(translate("security.page.loadedNote", { count: 0 })).toBe("Sobre 0 eventos cargados");
    expect(translate("security.prerequisites.done", { count: 1, windows: 1, mac: 0 })).toBe(
      "Equipo que ejecuta el agente: 1 Windows y 0 Mac.",
    );
    expect(translate("security.prerequisites.done", { count: 3, windows: 2, mac: 1 })).toBe(
      "Equipos que ejecutan el agente: 2 Windows y 1 Mac.",
    );
  });
});

describe("the security prerequisites in Spanish", () => {
  function familyPage(all: number, assets: number) {
    return {
      items: [],
      total: all,
      state_counts: { all, online: 0, offline: 0, assets, certs: 0 },
      fleet: { total: 99, managed: 99, online: 99, cert_attention: 0 },
    };
  }

  function stub(byFamily: Record<string, ReturnType<typeof familyPage>>, permissions = ["devices.read"]) {
    const api = {
      me: meWith(permissions),
      listDevices: vi.fn((params: DeviceListParams) => Promise.resolve(byFamily[params.osFamily ?? ""])),
    };
    mockUseApi.mockReturnValue(api as unknown as ApiClient);
    return api;
  }

  it("sends a fleet with no Windows or Mac agent to add one", async () => {
    setActiveLanguage("es");
    stub({ windows: familyPage(3, 3), macos: familyPage(1, 1) });
    const { container } = renderPanel(<SecurityPrerequisites />);
    await expectCta("Añadir un equipo Windows o Mac", "/devices/onboard");
    expect(screen.getByText("Antes de poder recoger eventos de seguridad")).toBeInTheDocument();
    expect(screen.getByText("Un equipo Windows o Mac ejecuta el agente")).toBeInTheDocument();
    expect(
      screen.getByText("El agente recoge estos eventos solo en equipos Windows y Mac, y ninguno ejecuta todavía el agente, así que aquí no llegará nada."),
    ).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("says it cannot be checked without the device permission", async () => {
    setActiveLanguage("es");
    stub({}, ["audit.read"]);
    const { container } = renderPanel(<SecurityPrerequisites />);
    expect(await screen.findByText(/no se puede comprobar/i)).toBeInTheDocument();
    expectNoEnglish(container);
  });
});
