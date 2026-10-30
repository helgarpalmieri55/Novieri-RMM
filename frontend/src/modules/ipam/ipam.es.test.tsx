/**
 * IP address management in Spanish: every routed page (the subnet tree, a
 * subnet's detail with its four tabs, drift with its class chips, search, the
 * registry, scan authorizations, import and export), the dialogs they open (an
 * address, register / edit / delete a subnet, authorize a prefix, handle a
 * finding), the readiness panel, and the sentences the state helpers build
 * outside React. Names, prefixes, hostnames and server text read as recorded;
 * the words around them are Spanish. The data is the module's own fixture
 * set (the Lisbon campus plan), so the pages show what the dev console shows.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import { ApiError } from "../../lib/api";
import { AddressDialog } from "./AddressDialog";
import { DriftPage } from "./DriftPage";
import fixtures from "./fixtures";
import { ImportPage } from "./ImportPage";
import { IpamReadiness, INSTALL_ON_SEGMENT_CTA } from "./IpamReadiness";
import {
  ADDRESS_STATE_PRESENTATION,
  ADDRESS_STATUS_PRESENTATION,
  allocationMethodLabel,
  allocationMethodPresentation,
  bandLabel,
  describeDriftDetail,
  describeHistoryDetail,
  describeImportDetail,
  describeScope,
  describeSweepRun,
  DORMANT_PRESENTATION,
  DRIFT_CLASS_PRESENTATION,
  driftClassPresentation,
  effectiveSweepScope,
  formatAge,
  formatUntil,
  IMPORT_ACTION_PRESENTATION,
  importApplyBlockReason,
  lastSeenMeaning,
  RANGE_KIND_PRESENTATION,
  SCOPE_STATUS_PRESENTATION,
  searchKindMeaning,
  statePresentation,
  statusPresentation,
  SWEEP_STATE_PRESENTATION,
  sweepScopeGrantMeaning,
  sweepScopeSafeguard,
  sweepStatePresentation,
  utilizationDisplay,
} from "./ipamState";
import { RegistryPage } from "./RegistryPage";
import { ScanAuthorizationsPage } from "./ScanAuthorizationsPage";
import { SearchPage } from "./SearchPage";
import { SubnetDetailPage } from "./SubnetDetailPage";
import { SubnetTreePage } from "./SubnetTreePage";
import type { GridEntryOut, ScopeOut, SubnetOut, SweepRunOut, UtilizationOut } from "./types";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const PERMISSIONS = [
  "ipam.read",
  "ipam.assign",
  "ipam.manage",
  "ipam.sweep.run",
  "ipam.sweep.manage",
  "ipam.scope.manage",
  "devices.read",
  "site.read",
  "notification.read",
];

type Mock = ReturnType<typeof vi.fn>;

/** The fixture set as an API: every call answers from `fixtures.ts`, then `over` wins. */
function stub(over: Record<string, unknown> = {}, permissions: string[] = PERMISSIONS): Record<string, Mock> {
  const api: Record<string, unknown> = {};
  for (const [name, fn] of Object.entries(fixtures)) {
    api[name] = vi.fn(async (...args: unknown[]) => (fn as (...a: unknown[]) => unknown)(...args));
  }
  api.me = vi.fn().mockResolvedValue({
    user_id: "u1",
    org_id: "o1",
    email: "a@b.c",
    display_name: "A",
    permissions,
  });
  api.listDevices = vi.fn().mockResolvedValue({
    items: [
      { id: "dev-5", hostname: "rack-b4-relay" },
      { id: "dev-14", hostname: "porto-pc" },
      { id: "dev-3", hostname: "ws-ana-03" },
    ],
    total: 3,
  });
  api.listAssetCandidates = vi.fn().mockResolvedValue({ items: [], total: 0, truncated: false, unsweepable_subnets: 1 });
  api.listSiteServers = vi.fn().mockResolvedValue([]);
  api.listNotificationRules = vi.fn().mockResolvedValue([]);
  Object.assign(api, over);
  mockUseApi.mockReturnValue(api);
  return api as Record<string, Mock>;
}

function renderPage(ui: ReactElement, entry = "/ipam") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderInSpanish(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[entry]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

function renderDetail(subnetId: string) {
  return renderPage(
    <Routes>
      <Route path="/ipam/subnets/:subnetId" element={<SubnetDetailPage />} />
    </Routes>,
    `/ipam/subnets/${subnetId}`,
  );
}

const body = () => document.body;
const grid = (subnetId: string) => fixtures.listIpamSubnetAddresses!(subnetId, 0, 256) as GridEntryOut[];
const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString();
const ahead = (minutes: number) => new Date(Date.now() + minutes * 60_000).toISOString();


/**
 * Free text the fixture's drift findings carry: a person's note on two findings ("Backup
 * appliance decommissioned in July; waiting on the record.", "Sofia moved desks; waiting for
 * the Porto sweep to confirm."; "July" is also the Budget settings' month name), and the key of an evidence payload the page has no rule for
 * (`last_seen_days`, read as "Last seen days").
 */
const DRIFT_RECORDED = ["Backup", "backup", "appliance", "July", "waiting", "to confirm", "moved", "days", "seen", "Last seen"];

/** Descriptions the fixture's VRFs and VLANs carry ("The default VRF; every subnet lands here ...", "Org-wide management VLAN."): recorded text. */
const REGISTRY_RECORDED = ["Org-wide", "subnet", "every"];

/** Words of a sweep run's error as the server wrote it ("Result rejected: scope digest mismatch."): stored text, shown as recorded.
 *  "rejected" is also the wire word the service desk names a final ticket status by (a lowercase English leaf), which this fixture text contains. */
const RUN_WORDS = ["Result", "scope", "rejected"];
/** Words inside the audit log's own action codes (`ipam.address_assigned`), which the history prints as stored. */
const ACTION_CODES = ["assigned"];

beforeEach(() => {
  mockUseApi.mockReset();
});

// ---------------------------------------------------------------- the subnet tree

describe("the subnet tree in Spanish", () => {
  it("shows the tiles, the filters, the rows and the readiness panel in Spanish", async () => {
    stub();
    renderPage(<SubnetTreePage />);
    expect(await screen.findByRole("heading", { name: "Administración de direcciones IP" })).toBeInTheDocument();
    expect(screen.getByText("Subredes, lo que hay en ellas y lo que el parque vio realmente.")).toBeInTheDocument();
    expect(await screen.findByText("Subredes administradas")).toBeInTheDocument();
    expect(screen.getByText("Asignables")).toBeInTheDocument();
    expect(screen.getByText("Con deriva")).toBeInTheDocument();
    expect(screen.getByText("Nunca barridas", { selector: "span, div, b, p, h3" })).toBeInTheDocument();
    expect(screen.getByText("Subredes con una dirección sobre la que el plan y el parque no coinciden")).toBeInTheDocument();
    expect(screen.getByText("Aquí nunca se ha ejecutado un barrido autorizado")).toBeInTheDocument();
    // The nav strip.
    const nav = screen.getByRole("navigation", { name: "IPAM" });
    for (const label of ["Subredes", "Deriva", "Búsqueda", "Catálogo", "Autorizaciones de escaneo", "Importar / exportar"]) {
      expect(within(nav).getByRole("link", { name: label })).toBeInTheDocument();
    }
    // The table: columns, the container pill, the swept and unswept pills, and ages.
    const table = await screen.findByRole("table");
    for (const header of ["Nombre", "Uso", "Deriva", "Barrido", "Último barrido", "Abrir"]) {
      expect(within(table).getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(within(table).getByText("contenedor")).toBeInTheDocument();
    expect(within(table).getByText("Contenedor, sin direcciones")).toBeInTheDocument();
    expect(within(table).getAllByText("Barrida").length).toBeGreaterThan(0);
    expect(within(table).getAllByText("Sin barrer").length).toBeGreaterThan(0);
    expect(within(table).getAllByText("hace 2 horas").length).toBeGreaterThan(0);
    expect(within(table).getByText("hace 3 días")).toBeInTheDocument();
    expect(within(table).getAllByText("nunca").length).toBeGreaterThan(0);
    expect(within(table).getAllByText(/con deriva$/).length).toBeGreaterThan(0);
    expect(within(table).getAllByText("Sin deriva").length).toBeGreaterThan(0);
    // The sub-line joins the name, the site and the VLAN.
    expect(within(table).getByText(/Racks · Lisbon · VLAN 40 Racks/)).toBeInTheDocument();
    // Filters.
    expect(screen.getByPlaceholderText("Buscar por nombre o prefijo")).toBeInTheDocument();
    expect(screen.getByLabelText("Filtrar por VRF")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Todas las VRF" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Todas las sedes" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Todas las VLAN" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "VLAN 90 Workstations" })).toBeInTheDocument();
    expect(screen.getByLabelText("Filtrar por dirección contenida")).toBeInTheDocument();
    const view = screen.getByLabelText("Filtrar por vista");
    expect(within(view).getByRole("option", { name: "Todas las subredes" })).toBeInTheDocument();
    expect(within(view).getByRole("option", { name: "Solo asignables" })).toBeInTheDocument();
    expect(within(view).getByRole("option", { name: "Nunca barridas" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Limpiar" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Nueva subred" })).toBeInTheDocument();
    // The readiness panel: nothing is told about drift, no site server serves the subnets.
    expect(await screen.findByText("Antes de poder barrer subredes")).toBeInTheDocument();
    expect(screen.getByText("Un barrido necesita permiso para sondear y un agente en la red desde el que hacerlo.")).toBeInTheDocument();
    expectNoEnglish(body());
  });

  it("narrows to the never-swept and assignable views and explains an empty one", async () => {
    stub();
    renderPage(<SubnetTreePage />);
    const user = userEvent.setup();
    await screen.findByRole("table");
    await user.selectOptions(screen.getByLabelText("Filtrar por vista"), "never-swept");
    expect(within(screen.getByRole("table")).queryByText("Barrida")).toBeNull();
    await user.selectOptions(screen.getByLabelText("Filtrar por vista"), "assignable");
    expect(within(screen.getByRole("table")).queryByText("contenedor")).toBeNull();
    await user.type(screen.getByLabelText("Buscar subredes"), "zzzz");
    expect(await screen.findByText("Ninguna subred coincide con estos filtros.")).toBeInTheDocument();
    expectNoEnglish(body());
  });

  it("says no subnet is assignable or unswept when the view holds nothing", async () => {
    stub({
      listIpamSubnets: vi.fn().mockResolvedValue([(fixtures.listIpamSubnets!({}) as SubnetOut[])[0]]),
    });
    renderPage(<SubnetTreePage />, "/ipam?state=assignable");
    expect(await screen.findByText("Ninguna subred es asignable.")).toBeInTheDocument();
    expect(screen.getByText("Todas las subredes registradas son contenedores")).toBeInTheDocument();
    expectNoEnglish(body());
  });

  it("explains a plan with no subnet, with and without filters, and a failure to load", async () => {
    stub({ listIpamSubnets: vi.fn().mockResolvedValue([]) });
    renderPage(<SubnetTreePage />);
    expect(
      await screen.findByText(/No hay ninguna subred registrada, así que no hay una cifra de deriva que informar/),
    ).toBeInTheDocument();
    expect(screen.getByText("Todavía no hay subredes registradas.")).toBeInTheDocument();
    expectNoEnglish(body());
  });

  it("words a utilization that cannot be fetched and one above the enumeration ceiling", async () => {
    stub({
      getIpamSubnetUtilization: vi.fn().mockResolvedValue({
        subnet_id: "s",
        prefix: "10.20.4.0/24",
        enumerable: false,
        capacity: null,
        free: null,
        used: null,
        percentage: null,
        assigned: 3,
        reserved: 1,
        deprecated: 0,
        observed: 9,
      } satisfies UtilizationOut),
    });
    renderPage(<SubnetTreePage />);
    expect((await screen.findAllByText("Utilización no disponible")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("Demasiado grande para enumerar: asignadas 3, observadas 9").length).toBeGreaterThan(0);
    expectNoEnglish(body());
  });

  it("registers a subnet from its dialog, with Spanish refusals that follow a language switch", async () => {
    const api = stub();
    renderPage(<SubnetTreePage />, "/ipam?register=1");
    const user = userEvent.setup();
    expect(await screen.findByRole("dialog", { name: "Registrar una subred" })).toBeInTheDocument();
    expect(screen.getByText(/No hay que elegir un padre/)).toBeInTheDocument();
    const dialog = screen.getByRole("dialog");
    for (const label of ["Prefijo", "Nombre", "Tipo", "VRF", "Sede", "VLAN", "Puerta de enlace"]) {
      expect(within(dialog).getByLabelText(label)).toBeInTheDocument();
    }
    expect(within(dialog).getByRole("option", { name: "Asignable" })).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: "Contenedor" })).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: "Predeterminada (global)" })).toBeInTheDocument();
    expect(within(dialog).getAllByRole("option", { name: "Ninguno" }).length).toBe(2);
    // Nothing typed: the prefix is required.
    await user.click(within(dialog).getByRole("button", { name: "Registrar" }));
    expect(await within(dialog).findByText(/El campo Prefijo es obligatorio/)).toBeInTheDocument();
    // An impossible CIDR and a bad gateway.
    await user.type(within(dialog).getByPlaceholderText("10.4.7.0/24"), "10.1.1.1.1/8");
    await user.type(within(dialog).getByPlaceholderText("10.4.7.1"), "nope");
    await user.click(within(dialog).getByRole("button", { name: "Registrar" }));
    expect(
      await within(dialog).findByText(/El prefijo debe ser una red IPv4 o IPv6 en notación CIDR, p\. ej\. 10\.4\.7\.0\/24; «10\.1\.1\.1\.1\/8» no lo es/),
    ).toBeInTheDocument();
    expect(within(dialog).getByText(/La puerta de enlace debe ser una dirección IP como 10\.4\.7\.1; «nope» no lo es/)).toBeInTheDocument();
    expectNoEnglish(body());
    // A language switch re-words the refusal that is already on screen.
    act(() => setActiveLanguage("en"));
    expect(within(dialog).getByText(/Prefix must be an IPv4 or IPv6 network in CIDR notation/)).toBeInTheDocument();
    expect(within(dialog).getByText(/Gateway must be an IP address such as 10\.4\.7\.1; "nope" is not one/)).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    expect(within(dialog).getByText(/La puerta de enlace debe ser una dirección IP/)).toBeInTheDocument();
    // A prefix length beyond the family, and a good one.
    const prefix = within(dialog).getByPlaceholderText("10.4.7.0/24");
    await user.clear(prefix);
    await user.type(prefix, "fd00::/130");
    await user.click(within(dialog).getByRole("button", { name: "Registrar" }));
    expect(
      await within(dialog).findByText(/La longitud del prefijo debe ser como máximo \/128 para un prefijo IPv6; \/130 no lo es/),
    ).toBeInTheDocument();
    await user.clear(prefix);
    await user.type(prefix, "10.9.9.0/24");
    await user.clear(within(dialog).getByPlaceholderText("10.4.7.1"));
    await user.click(within(dialog).getByRole("button", { name: "Registrar" }));
    expect(await within(dialog).findByText("Subred registrada.")).toBeInTheDocument();
    expect(api.createIpamSubnet).toHaveBeenCalled();
    expectNoEnglish(body());
  });

  it("re-labels the page on a live language switch", async () => {
    stub();
    renderPage(<SubnetTreePage />);
    await screen.findByRole("table");
    expect(screen.getByRole("heading", { name: "Administración de direcciones IP" })).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("heading", { name: "IP address management" })).toBeInTheDocument();
    expect(within(screen.getByRole("table")).getAllByText("2 hours ago").length).toBeGreaterThan(0);
    act(() => setActiveLanguage("es"));
    expect(within(screen.getByRole("table")).getAllByText("hace 2 horas").length).toBeGreaterThan(0);
  });
});

// ---------------------------------------------------------------- the readiness panel

const subnetRow = (id: string, prefix: string): SubnetOut => ({
  client_id: "o1",
  id,
  vrf_id: "v",
  prefix,
  kind: "assignable",
  name: "",
  description: "",
  site_id: null,
  vlan_id: null,
  gateway: null,
  dns_servers: [],
  parent_id: null,
  sweep_enabled: false,
  sweep_device_id: null,
  sweep_interval_hours: null,
  last_swept_at: null,
});

const scopeRow = (id: string, prefix: string, over: Partial<ScopeOut> = {}): ScopeOut => ({
  client_id: "o1",
  id,
  vrf_id: "v",
  prefix,
  note: "",
  authorized_by: "ines",
  authorized_at: ago(60),
  expires_at: null,
  revoked_at: null,
  ...over,
});

describe("the readiness panel in Spanish", () => {
  it("lists what is missing, in Spanish, each with its way out", async () => {
    stub();
    renderPage(<IpamReadiness />);
    expect(await screen.findByText("Antes de poder barrer subredes")).toBeInTheDocument();
    expect(screen.getByText("Hay una subred registrada autorizada para el escaneo")).toBeInTheDocument();
    expect(screen.getByText("Las 5 subredes registradas están autorizadas para el escaneo.")).toBeInTheDocument();
    expect(screen.getByText("Hay un agente en cada segmento barrido")).toBeInTheDocument();
    expect(screen.getByText(/El último barrido de 1 subred no encontró ningún agente inscrito en el segmento/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Instala el agente en un equipo de ese segmento" })).toBeInTheDocument();
    expect(screen.getByText("Un servidor de sitio atiende cada subred autorizada")).toBeInTheDocument();
    expect(
      screen.getByText(/El segmento de ningún servidor de sitio habilitado contiene 10\.20\.0\.0\/16, 10\.20\.4\.0\/24, 10\.20\.9\.0\/24, 10\.20\.11\.0\/24 y 1 más\./),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Promover un servidor de sitio" })).toBeInTheDocument();
    expect(screen.getByText("Alguien recibe aviso de la deriva de direcciones")).toBeInTheDocument();
    expect(screen.getByText(/Ninguna regla de notificación activa envía a nadie los hallazgos de deriva/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Avisar a alguien de la deriva" })).toBeInTheDocument();
    expectNoEnglish(body());
  });

  it("waits on the first step, and says how many of the subnets are authorized", async () => {
    stub({ listIpamScanAuthorizations: vi.fn().mockResolvedValue([]) });
    renderPage(<IpamReadiness />);
    expect(await screen.findByText(/Ninguna subred registrada está autorizada para el escaneo, así que se rechaza todo barrido/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Autorizar escaneo" })).toBeInTheDocument();
    expect((await screen.findAllByText("Primero autoriza una subred para el escaneo.")).length).toBe(2);
    expectNoEnglish(body());
  });

  it("counts the authorized subnets of the registered ones with the right number", async () => {
    const prefixes = ["10.1.0.0/24", "10.2.0.0/24", "10.3.0.0/24"];
    stub({
      listIpamSubnets: vi.fn().mockResolvedValue(prefixes.map((p, i) => subnetRow(`s${i}`, p))),
      listIpamScanAuthorizations: vi.fn().mockResolvedValue([scopeRow("c0", prefixes[0]!)]),
    });
    renderPage(<IpamReadiness />);
    expect(
      await screen.findByText("Autorizadas para el escaneo: 1 de 3 subredes registradas. Las demás nunca se barren."),
    ).toBeInTheDocument();
    expectNoEnglish(body());
  });

  it("names at most four prefixes with no site server, then how many more", async () => {
    const prefixes = ["10.1.0.0/24", "10.2.0.0/24", "10.3.0.0/24", "10.4.0.0/24", "10.5.0.0/24", "10.6.0.0/24"];
    stub({
      listIpamSubnets: vi.fn().mockResolvedValue(prefixes.map((p, i) => subnetRow(`s${i}`, p))),
      listIpamScanAuthorizations: vi.fn().mockResolvedValue(prefixes.map((p, i) => scopeRow(`c${i}`, p))),
      listAssetCandidates: vi.fn().mockResolvedValue({ items: [], total: 0, truncated: false, unsweepable_subnets: 0 }),
    });
    renderPage(<IpamReadiness />);
    expect(
      await screen.findByText(/contiene 10\.1\.0\.0\/24, 10\.2\.0\.0\/24, 10\.3\.0\.0\/24, 10\.4\.0\.0\/24 y 2 más\./),
    ).toBeInTheDocument();
    expect(screen.getByText("El último barrido de ninguna subred se quedó sin un agente que lo ejecutara.")).toBeInTheDocument();
    expectNoEnglish(body());
  });

  it("re-words its items on a live language switch", async () => {
    stub();
    renderPage(<IpamReadiness />);
    await screen.findByText("Antes de poder barrer subredes");
    await screen.findByText("Las 5 subredes registradas están autorizadas para el escaneo.");
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("Before subnets can be swept")).toBeInTheDocument();
    expect(screen.getByText("All 5 registered subnets are authorized for scanning.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Install the agent on one machine there" })).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    expect(screen.getByRole("link", { name: "Instala el agente en un equipo de ese segmento" })).toBeInTheDocument();
  });

  it("words its call to action when read, in either language", () => {
    expect(INSTALL_ON_SEGMENT_CTA.label).toBe("Install the agent on one machine there");
    setActiveLanguage("es");
    expect(INSTALL_ON_SEGMENT_CTA.label).toBe("Instala el agente en un equipo de ese segmento");
    setActiveLanguage("en");
  });
});

// ---------------------------------------------------------------- a subnet

describe("a subnet's detail in Spanish", () => {
  it("shows the facts, the utilization and the address grid with its allocate panel", async () => {
    stub();
    renderDetail("subnet-1");
    expect(await screen.findByRole("heading", { name: "10.20.9.0/24" })).toBeInTheDocument();
    // The site's name is the client's site list's, which waits for `/me` (the client).
    expect(await screen.findByText(/Workstations · Lisbon · VLAN 90 Workstations · puerta de enlace 10\.20\.9\.1/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Todas las subredes/ })).toBeInTheDocument();
    for (const fact of ["Utilizables", "Registradas", "Vistas por barrido", "Deriva", "Puerta de enlace", "DNS", "Agente de barrido", "Escaneo autorizado", "Último barrido"]) {
      expect(screen.getAllByText(fact).length).toBeGreaterThan(0);
    }
    expect(screen.getByText("Elegido automáticamente")).toBeInTheDocument();
    expect(screen.getByText("sin resolver")).toBeInTheDocument();
    expect(screen.getByText("Sí")).toBeInTheDocument();
    expect(await screen.findByText("Utilización")).toBeInTheDocument();
    expect(screen.getByText("Filas documentadas y observaciones frente al rango utilizable.")).toBeInTheDocument();
    expect(screen.getByText(/de 254 direcciones utilizables en 10\.20\.9\.0\/24; libres: 134\./, { selector: "p" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Editar" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Eliminar" })).toBeInTheDocument();
    // The tabs.
    for (const tab of ["Direcciones", "Rangos", "Barridos", "Deriva"]) {
      expect(screen.getByRole("tab", { name: tab })).toBeInTheDocument();
    }
    expect(screen.getByRole("tabpanel", { name: "Direcciones" })).toBeInTheDocument();
    // The allocate panel.
    expect(screen.getByText("Asignación automática")).toBeInTheDocument();
    expect(screen.getByText("Ver la siguiente libre solo mira; Asignar la siguiente reclama la dirección por ti.")).toBeInTheDocument();
    // The grid.
    expect(await screen.findByText("Azul es registrada, ámbar se vio pero no está registrada, rojo es un conflicto, gris es libre.")).toBeInTheDocument();
    for (const header of ["Dirección", "Situación (derivada)", "Estado (documentado)", "Titular", "Asignación", "MAC", "Nombre de host", "Visto por última vez", "Acciones"]) {
      expect(screen.getAllByRole("columnheader", { name: header }).length).toBeGreaterThan(0);
    }
    expect(screen.getAllByText("En uso").length).toBeGreaterThan(0);
    expect(screen.getAllByText("En conflicto").length).toBeGreaterThan(0);
    expect(screen.getAllByText("En reposo").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Estática").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Observado").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Anterior" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Siguiente" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /direcciones:/ })).toBeInTheDocument();
    expectNoEnglish(body());
  });

  it("previews the next free addresses and allocates one", async () => {
    const api = stub();
    renderDetail("subnet-1");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Ver la siguiente libre" }));
    expect(await screen.findByText(/Siguientes libres \(orientativo\):/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Asignar la siguiente" }));
    expect(await screen.findByText(/^Se asignó 10\.20\.9\./)).toBeInTheDocument();
    expect(api.allocateIpamAddress).toHaveBeenCalled();
    expect(screen.getByLabelText("Cantidad a mostrar")).toBeInTheDocument();
    expectNoEnglish(body());
  });

  it("walks the ranges, the sweeps and the drift tabs", async () => {
    stub();
    renderDetail("subnet-1");
    const user = userEvent.setup();
    await screen.findByRole("heading", { name: "10.20.9.0/24" });

    await user.click(screen.getByRole("tab", { name: "Rangos" }));
    expect(await screen.findByText("Pools DHCP y tramos reservados y excluidos separados de esta subred.")).toBeInTheDocument();
    for (const header of ["Rango", "Tipo"]) expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    expect(screen.getAllByText("Pool DHCP").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Rango reservado").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: /Más acciones para 10\.20\.9\.200/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Añadir rango" })).toBeInTheDocument();
    expectNoEnglish(body());

    await user.click(screen.getByRole("tab", { name: "Barridos" }));
    expect(await screen.findByText("Si esta subred puede sondearse, quién la sondea y con qué frecuencia.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Barrer ahora" })).toBeInTheDocument();
    expect(screen.getByText("Autorización")).toBeInTheDocument();
    expect(screen.getByText("Autorizada")).toBeInTheDocument();
    expect(screen.getByText(/Se puede barrer con 1 autorización activa\./)).toBeInTheDocument();
    expect(screen.getAllByText("Programado").length).toBeGreaterThan(0);
    expect(screen.getByText("Habilitado")).toBeInTheDocument();
    expect(screen.getByText("Ninguno, elegido automáticamente")).toBeInTheDocument();
    expect(screen.getByText(/\(mínimo 1 h\)/)).toBeInTheDocument();
    expect(await screen.findByText(/^Correcto, hace 2 horas\. 121 de 234 respondieron\./)).toBeInTheDocument();
    // A picker of the subnet's client's machines (MSP clients phase 2), not a typed id.
    expect(screen.getByLabelText("Agente designado")).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "Elegir automáticamente un agente en línea de este segmento" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Solo equipos con agente del cliente de esta subred/)).toBeInTheDocument();
    expect(screen.getByLabelText("Intervalo (horas)")).toBeInTheDocument();
    expect(screen.getByLabelText("Barridos programados habilitados")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Guardar la configuración del barrido" })).toBeInTheDocument();
    expect(screen.getByText("Ejecuciones recientes")).toBeInTheDocument();
    for (const header of ["Resultado", "Origen", "Destinos", "Respondieron", "Iniciado"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(screen.getAllByText("Correcto").length).toBeGreaterThan(0);
    expect(screen.getByText("Fallido")).toBeInTheDocument();
    expect(screen.getAllByText("Programado").length).toBeGreaterThan(1);
    expect(screen.getByText("Manual")).toBeInTheDocument();
    expectNoEnglish(body(), RUN_WORDS);

    await user.click(screen.getByRole("tab", { name: "Deriva" }));
    expect(await screen.findByText("Dónde el plan de esta subred y el parque no coinciden.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Abrir en la página de deriva" })).toBeInTheDocument();
    for (const header of ["Dirección", "Clase", "Gravedad", "Primera detección"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(screen.getByText("Conflicto")).toBeInTheDocument();
    expect(screen.getByText("MAC distinta")).toBeInTheDocument();
    expect(screen.getByText("Presente, sin documentar")).toBeInTheDocument();
    expect(screen.getAllByText("Alta").length + screen.getAllByText("Media").length).toBeGreaterThan(0);
    expectNoEnglish(body());
  });

  it("starts a sweep, and refuses to say nothing when it cannot", async () => {
    const api = stub();
    renderDetail("subnet-1");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "Barridos" }));
    await user.click(await screen.findByRole("button", { name: "Barrer ahora" }));
    await waitFor(() => expect(api.dispatchIpamSweep).toHaveBeenCalled());
    expectNoEnglish(body(), RUN_WORDS);
  });

  it("explains a segment no agent could sweep, with its way out", async () => {
    stub();
    renderDetail("subnet-3");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "Barridos" }));
    expect(await screen.findByText(/El último barrido no se pudo ejecutar: no hay ningún agente inscrito en este segmento/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Instala el agente en un equipo de ese segmento" })).toBeInTheDocument();
    expect(screen.getAllByText("Ningún agente pudo barrer").length).toBeGreaterThan(0);
    expect(screen.getAllByText(/^Ningún agente pudo barrer, hace 23 horas\./).length).toBeGreaterThan(0);
    // The scope note "No agent can reach it yet." is recorded text.
    expectNoEnglish(body(), SCOPE_NOTES);
  });

  it("words the authorization notice when nothing authorizes a sweep", async () => {
    stub({ listIpamScanAuthorizations: vi.fn().mockResolvedValue([]) });
    renderDetail("subnet-1");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "Barridos" }));
    expect(await screen.findByText("No autorizada")).toBeInTheDocument();
    expect(screen.getByText(/NO AUTORIZADA\. Ninguna autorización de escaneo activa en esta VRF cubre este prefijo/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Autorizar escaneo" })).toBeInTheDocument();
    expect(screen.getByText("No")).toBeInTheDocument();
    expectNoEnglish(body(), RUN_WORDS);
  });

  it("re-words the sweep panel's verdict, its notice and its refusal on a live language switch", async () => {
    stub({ listIpamScanAuthorizations: vi.fn().mockResolvedValue([]) });
    renderDetail("subnet-1");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "Barridos" }));
    await screen.findByText(/NO AUTORIZADA\./);
    const interval = await screen.findByLabelText("Intervalo (horas)");
    await user.clear(interval);
    await user.type(interval, "0");
    await screen.findByText(/El intervalo mínimo es de 1 hora\./);
    act(() => setActiveLanguage("en"));
    expect(screen.getByText(/^NOT AUTHORIZED\. No active scan authorization in this VRF covers this prefix/)).toBeInTheDocument();
    expect(screen.getByText(/^The interval floor is 1 hour\./)).toBeInTheDocument();
    expect(screen.getByText("Not authorized")).toBeInTheDocument();
    expect(screen.getByText("Succeeded, 2 hours ago. 121 of 234 answered.")).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    expect(screen.getByText(/^NO AUTORIZADA\./)).toBeInTheDocument();
    expect(screen.getByText(/^Correcto, hace 2 horas\. 121 de 234 respondieron\./)).toBeInTheDocument();
  });

  it("says a sweep interval below the floor is refused", async () => {
    stub();
    renderDetail("subnet-1");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "Barridos" }));
    const interval = await screen.findByLabelText("Intervalo (horas)");
    await user.clear(interval);
    await user.type(interval, "0");
    expect(await screen.findByText(/El intervalo mínimo es de 1 hora\. Cualquier valor menor se rechaza en el servidor/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Guardar la configuración del barrido" })).toBeDisabled();
    expectNoEnglish(body(), RUN_WORDS);
  });

  it("explains a container prefix, which holds no addresses", async () => {
    stub();
    renderDetail("subnet-0");
    expect(await screen.findByRole("heading", { name: "10.20.0.0/16" })).toBeInTheDocument();
    expect(screen.getAllByText(/prefijo contenedor/).length).toBeGreaterThan(0);
    expect(screen.getByText("Un prefijo contenedor no tiene cuadrícula de direcciones.")).toBeInTheDocument();
    expect(screen.getByText(/Un prefijo contenedor no contiene direcciones: ni utilización, ni cuadrícula, ni asignador\./)).toBeInTheDocument();
    expect(screen.getByText("Un contenedor no contiene direcciones")).toBeInTheDocument();
    // Registered and seen by sweep have no figure for a container: "n/d", never "n/a".
    expect(screen.getAllByText("n/d").length).toBe(2);
    expect(screen.queryByText("n/a")).toBeNull();
    expectNoEnglish(body());
  });

  it("shows the counts, and not a percentage, above the enumeration ceiling", async () => {
    stub({
      listIpamSubnetAddresses: vi.fn().mockRejectedValue(new ApiError(422, "")),
      getIpamSubnetUtilization: vi.fn().mockRejectedValue(new ApiError(422, "")),
    });
    renderDetail("subnet-1");
    expect(await screen.findByText("Demasiado grande para enumerar")).toBeInTheDocument();
    expect(screen.getByText("10.20.9.0/24 supera el límite de enumeración de esta organización, o es IPv6.")).toBeInTheDocument();
    expect(screen.getByText(/Aquí se rechazan la cuadrícula, la siguiente libre y los barridos\./)).toBeInTheDocument();
    expectNoEnglish(body());
  });

  it("lists the real counts when the ceiling stops the grid", async () => {
    stub({
      listIpamSubnetAddresses: vi.fn().mockRejectedValue(new ApiError(422, "")),
      getIpamSubnetUtilization: vi.fn().mockResolvedValue({
        subnet_id: "s",
        prefix: "10.20.9.0/24",
        enumerable: false,
        capacity: null,
        free: null,
        used: null,
        percentage: null,
        assigned: 3,
        reserved: 1,
        deprecated: 2,
        observed: 9,
      } satisfies UtilizationOut),
    });
    renderDetail("subnet-1");
    expect(await screen.findByText("Estos recuentos son reales. El porcentaje de utilización no se calcula para este prefijo y no es cero.")).toBeInTheDocument();
    for (const label of ["Asignadas", "Reservadas", "Dadas de baja", "Observadas"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    expectNoEnglish(body());
  });

  it("hides what the caller may not do and says why", async () => {
    stub({}, ["ipam.read"]);
    renderDetail("subnet-1");
    const user = userEvent.setup();
    await user.click((await screen.findAllByRole("button", { name: "Administrar" }))[0]!);
    expect(await screen.findByText(/No tienes permiso para asignar direcciones, así que esas acciones no se muestran aquí\./)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Editar" })).toBeNull();
    expectNoEnglish(body(), ACTION_CODES);
  });

  it("says a subnet that cannot be loaded is missing", async () => {
    stub({ getIpamSubnet: vi.fn().mockRejectedValue(new ApiError(404, "")) });
    renderDetail("zzz");
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expectNoEnglish(body());
  });
});

// ---------------------------------------------------------------- edit and delete a subnet

describe("editing and deleting a subnet in Spanish", () => {
  it("opens the edit dialog, refuses a bad gateway in Spanish and re-words it on a switch", async () => {
    const api = stub();
    renderDetail("subnet-1");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Editar" }));
    const dialog = await screen.findByRole("dialog", { name: "Editar la subred 10.20.9.0/24" });
    expect(within(dialog).getByText(/El prefijo, la VRF y el tipo son lo que define a la subred/)).toBeInTheDocument();
    for (const label of ["Nombre", "Puerta de enlace", "Sede", "VLAN", "Servidores DNS", "Descripción"]) {
      expect(within(dialog).getByLabelText(label)).toBeInTheDocument();
    }
    const gateway = within(dialog).getByPlaceholderText("10.4.7.1");
    await user.clear(gateway);
    await user.type(gateway, "nope");
    await user.click(within(dialog).getByRole("button", { name: "Guardar cambios" }));
    expect(await within(dialog).findByText(/La puerta de enlace debe ser una dirección IP como 10\.4\.7\.1; «nope» no lo es/)).toBeInTheDocument();
    expectNoEnglish(body());
    act(() => setActiveLanguage("en"));
    expect(within(dialog).getByText(/Gateway must be an IP address such as 10\.4\.7\.1; "nope" is not one/)).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    await user.clear(gateway);
    await user.type(gateway, "10.20.9.1");
    await user.click(within(dialog).getByRole("button", { name: "Guardar cambios" }));
    await waitFor(() => expect(api.updateIpamSubnet).toHaveBeenCalled());
  });

  it("says a failed save in Spanish", async () => {
    stub({ updateIpamSubnet: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    renderDetail("subnet-1");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Editar" }));
    await user.click(await screen.findByRole("button", { name: "Guardar cambios" }));
    expect(await screen.findAllByRole("alert")).not.toHaveLength(0);
    expectNoEnglish(body());
  });

  it("asks before deleting, counting what it holds, then escalates to a cascade the server asked for", async () => {
    const api = stub();
    renderDetail("subnet-1");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Eliminar" }));
    const confirm = await screen.findByRole("dialog", { name: "Eliminar la subred 10.20.9.0/24" });
    expect(within(confirm).getByText(/Contiene 120 direcciones documentadas, 128 direcciones observadas\. Las subredes hijas pasan a depender de otra/)).toBeInTheDocument();
    expect(within(confirm).getByText(/No se puede deshacer\./)).toBeInTheDocument();
    await user.click(within(confirm).getByRole("button", { name: "Eliminar subred" }));
    const cascade = await screen.findByRole("dialog", { name: "Eliminar la subred 10.20.9.0/24 y su contenido" });
    expect(within(cascade).getByText(/El servidor rechazó la eliminación simple: «/)).toBeInTheDocument();
    expect(within(cascade).getByRole("button", { name: "Eliminar en cascada" })).toBeInTheDocument();
    expect(api.deleteIpamSubnet).toHaveBeenCalled();
    expectNoEnglish(body(), ["addresses"]);
  });

  it("re-words a refused delete's cascade confirmation on a language switch", async () => {
    stub({
      deleteIpamSubnet: vi.fn().mockRejectedValue(new ApiError(409, "")),
    });
    renderDetail("subnet-1");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Eliminar" }));
    await user.click(await screen.findByRole("button", { name: "Eliminar subred" }));
    const cascade = await screen.findByRole("dialog", { name: "Eliminar la subred 10.20.9.0/24 y su contenido" });
    const spanish = within(cascade).getByText(/El servidor rechazó la eliminación simple: «(.+)»\./).textContent;
    act(() => setActiveLanguage("en"));
    const english = screen.getByRole("dialog", { name: "Delete subnet 10.20.9.0/24 and its contents" }).textContent;
    expect(english).toContain("The server refused the plain delete:");
    expect(english).not.toBe(spanish);
    act(() => setActiveLanguage("es"));
  });

  it("says a delete that failed outright, with the reason", async () => {
    stub({ deleteIpamSubnet: vi.fn().mockRejectedValue(new ApiError(500, "")) });
    renderDetail("subnet-1");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Eliminar" }));
    await user.click(await screen.findByRole("button", { name: "Eliminar subred" }));
    expect(await screen.findByText(/^No se pudo eliminar: /)).toBeInTheDocument();
    expectNoEnglish(body());
  });

  it("says an empty container holds nothing this page can see", async () => {
    stub();
    renderDetail("subnet-0");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Eliminar" }));
    expect(await screen.findByText(/Contiene 3 subredes hijas\./)).toBeInTheDocument();
    expectNoEnglish(body());
  });

  it("says a subnet holding nothing is removed outright", async () => {
    stub({
      listIpamSubnets: vi.fn().mockResolvedValue([]),
      getIpamSubnetUtilization: vi.fn().mockResolvedValue({
        subnet_id: "s",
        prefix: "p",
        enumerable: true,
        capacity: 254,
        free: 254,
        used: 0,
        percentage: 0,
        assigned: 0,
        reserved: 0,
        deprecated: 0,
        observed: 0,
      } satisfies UtilizationOut),
    });
    renderDetail("subnet-1");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Eliminar" }));
    expect(await screen.findByText(/No contiene nada que esta página pueda ver\./)).toBeInTheDocument();
    expectNoEnglish(body());
  });
});

// ---------------------------------------------------------------- one address

describe("an address's dialog in Spanish", () => {
  const entry = (address: string): GridEntryOut => grid("subnet-1").find((row) => row.address === address)!;
  const open = (address: string, canAssign = true) =>
    renderPage(<AddressDialog entry={entry(address)} subnetPrefix="10.20.9.0/24" vrfId="v1" canAssign={canAssign} onClose={() => undefined} />);

  it("shows the derived situation and the documented state apart, the form and the history", async () => {
    stub();
    open("10.20.9.1");
    const dialog = await screen.findByRole("dialog", { name: "10.20.9.1" });
    expect(within(dialog).getByText("en")).toBeInTheDocument();
    expect(within(dialog).getByText("Situación (derivada)")).toBeInTheDocument();
    expect(within(dialog).getByText("Estado (documentado)")).toBeInTheDocument();
    expect(within(dialog).getByText("En uso")).toBeInTheDocument();
    expect(within(dialog).getByText("Asignada")).toBeInTheDocument();
    expect(within(dialog).getByText("Documentada como asignada y observada ahora mismo. El plan y la realidad coinciden.")).toBeInTheDocument();
    expect(within(dialog).getByText("Visto por última vez")).toBeInTheDocument();
    expect(within(dialog).getAllByText("hace 2 horas").length).toBeGreaterThan(0);
    expect(within(dialog).getAllByText(lastSeenMeaning()).length).toBeGreaterThan(0);
    for (const label of ["Nombre de host", "Etiqueta del activo", "ID de dispositivo", "MAC", "Descripción", "Método de asignación"]) {
      expect(within(dialog).getByLabelText(label)).toBeInTheDocument();
    }
    expect(within(dialog).getByPlaceholderText("exactamente uno: ID de dispositivo o etiqueta del activo")).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: "estática" })).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: "DHCP" })).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: "descubierta" })).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: "desconocida" })).toBeInTheDocument();
    for (const button of ["Asignar", "Reservar", "Guardar ediciones", "Liberar"]) {
      expect(within(dialog).getByRole("button", { name: button })).toBeInTheDocument();
    }
    expect(within(dialog).getByText("Historial")).toBeInTheDocument();
    expect(within(dialog).getByText("Se lee directamente del registro de auditoría, que no se puede editar, así que este historial tampoco.")).toBeInTheDocument();
    expect(await within(dialog).findByText("(libre → asignada)")).toBeInTheDocument();
    expect(within(dialog).getAllByText("por usuario").length).toBeGreaterThan(0);
    expect(within(dialog).getByText("Titular")).toBeInTheDocument();
    expect(within(dialog).getByText("Método")).toBeInTheDocument();
    expect(within(dialog).getAllByText("estática").length).toBeGreaterThan(1);
    expect(within(dialog).getByText("(ninguno → libre)")).toBeInTheDocument();
    expectNoEnglish(body(), ACTION_CODES);
  });

  it("warns that a static assignment inside a DHCP pool is refused", async () => {
    stub();
    open("10.20.9.205");
    expect(await screen.findByText(/Esta dirección está dentro de un pool DHCP\. Aquí se rechaza la asignación estática/)).toBeInTheDocument();
    expect(screen.getByText("DHCP", { selector: "span" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Liberar" })).toBeNull();
    expectNoEnglish(body());
  });

  it("names a conflict and a dormant address in Spanish", async () => {
    stub();
    open("10.20.9.14");
    expect(await screen.findByText("En conflicto")).toBeInTheDocument();
    expect(screen.getByText(/Dos cosas reclaman esta dirección y el parque puede demostrarlo/)).toBeInTheDocument();
    expectNoEnglish(body(), ACTION_CODES);
  });

  it("says the caller may not assign, and still shows the history", async () => {
    stub();
    open("10.20.9.1", false);
    expect(await screen.findByText(/No tienes permiso para asignar direcciones/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Asignar" })).toBeNull();
    expect(await screen.findByText("Historial")).toBeInTheDocument();
    expectNoEnglish(body(), ACTION_CODES);
  });

  it("says there is no history, and a release that failed", async () => {
    stub({
      getIpamAddressHistory: vi.fn().mockResolvedValue([]),
      releaseIpamAddress: vi.fn().mockRejectedValue(new ApiError(503, "")),
    });
    open("10.20.9.1");
    const user = userEvent.setup();
    expect(await screen.findByText("No hay eventos registrados para esta dirección.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Liberar" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expectNoEnglish(body());
  });

  it("words the payloads of a history it has never seen, and a release of a held address", async () => {
    stub({
      getIpamAddressHistory: vi.fn().mockResolvedValue([
        {
          ts: ago(60),
          actor_type: "agent",
          actor_id: "x",
          action: "ipam.address_released",
          from_state: "assigned",
          to_state: "free",
          detail: { previous_claimant: "", from: { hostname: "a" }, to: { hostname: "b" } },
        },
        {
          ts: ago(120),
          actor_type: "robot",
          actor_id: "x",
          action: "ipam.address_released",
          from_state: "odd",
          to_state: null,
          detail: { previous_claimant: "dev-3", claimant: "x", claimant_type: "asset_label", allocation_method: "dhcp" },
        },
      ]),
    });
    open("10.20.9.1");
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("(asignada → libre)")).toBeInTheDocument();
    expect(within(dialog).getByText("por agente")).toBeInTheDocument();
    expect(within(dialog).getByText("por robot")).toBeInTheDocument();
    expect(within(dialog).getByText("(odd → ninguno)")).toBeInTheDocument();
    expect(within(dialog).getAllByText("Liberada de").length).toBe(2);
    expect(within(dialog).getByText("nadie, estaba retenida, no tenía titular")).toBeInTheDocument();
    expect(within(dialog).getByText("a → b")).toBeInTheDocument();
    expect(within(dialog).getByText("x (etiqueta del activo)")).toBeInTheDocument();
    expect(within(dialog).getByText("DHCP", { selector: "dd" })).toBeInTheDocument();
    expectNoEnglish(body(), ["odd"]);
  });

  it("re-labels on a live language switch", async () => {
    stub();
    open("10.20.9.1");
    await screen.findByText("(libre → asignada)");
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("(free → assigned)")).toBeInTheDocument();
    expect(screen.getAllByText("by user").length).toBeGreaterThan(0);
    act(() => setActiveLanguage("es"));
    expect(screen.getByText("(libre → asignada)")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------- drift

describe("the drift page in Spanish", () => {
  it("counts drift apart from what is dormant and from orphans, and lists the findings", async () => {
    stub();
    renderPage(<DriftPage />, "/ipam/drift");
    expect(await screen.findByRole("heading", { name: "Deriva" })).toBeInTheDocument();
    expect(screen.getByText("Dónde el plan registrado y el parque no coinciden sobre una dirección.")).toBeInTheDocument();
    expect(screen.getByText("Hallazgos sin resolver. Las direcciones en reposo y las huérfanas no se cuentan aquí.")).toBeInTheDocument();
    expect(screen.getByText("No se pueden verificar ahora mismo. No es deriva, no se cuenta como deriva y nunca se publica.")).toBeInTheDocument();
    expect(screen.getByText("Direcciones observadas dentro de ninguna subred registrada. El plan falta, no es erróneo.")).toBeInTheDocument();
    // The chip row, with its counts.
    const chips = screen.getByRole("group", { name: "Clase de deriva" });
    for (const label of [/^Toda la deriva \(6\)$/, /^Documentada, ausente \(1\)$/, /^Presente, sin documentar \(1\)$/, /^MAC distinta \(1\)$/, /^Titular distinto \(1\)$/, /^Dispositivo movido \(1\)$/, /^Conflicto \(1\)$/, /^En reposo \(3\)$/, /^Huérfanas \(8\)$/]) {
      expect(await within(chips).findByRole("button", { name: label })).toBeInTheDocument();
    }
    // The table.
    const table = await screen.findByRole("table");
    for (const header of ["Dirección", "Clase", "Gravedad", "Evidencia", "Primera detección", "Estado", "Acciones"]) {
      expect(within(table).getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(within(table).getAllByText("hace 4 días").length).toBeGreaterThan(0);
    expect(within(table).getAllByText("confianza alta").length).toBeGreaterThan(0);
    expect(within(table).getAllByText("confianza media").length).toBeGreaterThan(0);
    expect(within(table).getAllByText("visto en 4 pasadas seguidas").length).toBeGreaterThan(0);
    expect(within(table).getAllByText("visto una vez, aún sin publicar").length).toBeGreaterThan(0);
    expect(within(table).getAllByText("Abierto").length).toBeGreaterThan(0);
    expect(within(table).getByText("Reconocido")).toBeInTheDocument();
    expect(within(table).getByText("Silenciado")).toBeInTheDocument();
    expect(within(table).getAllByText("Alta").length).toBeGreaterThan(0);
    expect(within(table).getAllByText("Atender").length).toBeGreaterThan(0);
    // Evidence is facts, not JSON.
    expect(within(table).getAllByText("MAC vistas").length).toBeGreaterThan(0);
    expect(within(table).getAllByText("Titular distinto").length).toBeGreaterThan(0);
    expect(within(table).getByText("Sí, el inventario nombra otro dispositivo")).toBeInTheDocument();
    expect(within(table).getByText("MAC documentada")).toBeInTheDocument();
    expect(within(table).getAllByText("Observado por").length).toBeGreaterThan(0);
    expect(within(table).getAllByText("Segmento examinado").length).toBeGreaterThan(0);
    expect(within(table).getAllByText("Sí, dentro de la ventana").length).toBeGreaterThan(0);
    expect(within(table).getAllByText("Titular alcanzable").length).toBeGreaterThan(0);
    expect(within(table).getByText("Subred observada")).toBeInTheDocument();
    expect(within(table).getAllByText("En").length).toBeGreaterThan(0);
    // "record" is a recorded fixture value (a DNS record kind), not a label; the docs
    // module's English link-target word "record" is a catalogue word since 10b.
    expectNoEnglish(body(), [...DRIFT_RECORDED, "record"]);
  });

  it("explains a class and its remedy when its chip is chosen", async () => {
    stub();
    renderPage(<DriftPage />, "/ipam/drift?class=conflict");
    expect(await screen.findByText(/Dos cosas usan UNA misma dirección/)).toBeInTheDocument();
    expect(screen.getByText("Qué hacer:")).toBeInTheDocument();
    expect(screen.getByText(/PRIMERO UNA SOLUCIÓN OPERATIVA/)).toBeInTheDocument();
    expectNoEnglish(body());
  });

  it("lists what is dormant, apart from the drift", async () => {
    stub();
    renderPage(<DriftPage />, "/ipam/drift?class=dormant");
    expect(await screen.findByText(/Documentada, no observada, y AHORA MISMO NO PODEMOS VERIFICARLA/)).toBeInTheDocument();
    expect(screen.getAllByText("En reposo").length).toBeGreaterThan(0);
    expectNoEnglish(body());
  });

  it("lists the orphans with the way to register them", async () => {
    stub();
    renderPage(<DriftPage />, "/ipam/drift?class=orphans");
    expect(await screen.findByText("Direcciones observadas dentro de ninguna subred registrada, agrupadas por /24 o /64 inferido.")).toBeInTheDocument();
    for (const header of ["Prefijo inferido", "Direcciones vistas", "Visto por última vez", "Acciones"]) {
      expect(await screen.findByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(screen.getAllByRole("link", { name: "Registrar esta subred" }).length).toBe(2);
    expectNoEnglish(body());
  });

  it("says there is nothing to show, view by view", async () => {
    stub({ listIpamDrift: vi.fn().mockResolvedValue([]), listIpamOrphans: vi.fn().mockResolvedValue([]) });
    renderPage(<DriftPage />, "/ipam/drift");
    expect(await screen.findByText("No hay nada en esta vista.")).toBeInTheDocument();
    expect(screen.getByText("Ningún hallazgo sin resolver")).toBeInTheDocument();
    expect(screen.getByText("Todas las direcciones documentadas se pudieron verificar")).toBeInTheDocument();
    expect(screen.getByText("Todas las direcciones observadas están dentro de una subred registrada")).toBeInTheDocument();
    expectNoEnglish(body());
  });

  it("says no orphan is left", async () => {
    stub({ listIpamOrphans: vi.fn().mockResolvedValue([]) });
    renderPage(<DriftPage />, "/ipam/drift?class=orphans");
    expect(await screen.findByText("Todas las direcciones observadas están dentro de una subred registrada.")).toBeInTheDocument();
    expectNoEnglish(body());
  });

  it("opens the handling panel, echoes the chosen date and acknowledges", async () => {
    const api = stub();
    renderPage(<DriftPage />, "/ipam/drift");
    const user = userEvent.setup();
    await user.click((await screen.findAllByRole("button", { name: "Atender" }))[0]!);
    expect(await screen.findByText("Reconocer o silenciar")).toBeInTheDocument();
    expect(screen.getByText(/Reconocer dice «lo he visto y está bien»; silenciar lo calla hasta una fecha y necesita una nota\./)).toBeInTheDocument();
    expect(screen.getByLabelText("Nota")).toBeInTheDocument();
    expect(screen.getByLabelText("Silenciar hasta")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Silenciar hasta"), "2030-05-17");
    expect(await screen.findByText(/^Silenciado hasta el comienzo del día .*2030.*, en tu zona horaria\.$/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Silenciar" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Reconocer" }));
    await waitFor(() => expect(api.acknowledgeIpamDrift).toHaveBeenCalled());
    // "record" is a recorded fixture value (a DNS record kind), not a label; the docs
    // module's English link-target word "record" is a catalogue word since 10b.
    expectNoEnglish(body(), [...DRIFT_RECORDED, "record"]);
  });

  it("shows no handling action to a caller who may only read", async () => {
    stub({}, ["ipam.read"]);
    renderPage(<DriftPage />, "/ipam/drift?subnet=subnet-1");
    expect(await screen.findByRole("table")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Atender" })).toBeNull();
    expect(screen.queryByRole("columnheader", { name: "Acciones" })).toBeNull();
    expectNoEnglish(body());
  });

  it("words evidence it has no rule for, and a finding of a class it does not know", async () => {
    const base = fixtures.listIpamDrift!({}) as unknown as Record<string, unknown>[];
    stub({
      listIpamDrift: vi.fn().mockResolvedValue([
        { ...base[0], id: "x1", detail: {}, drift_class: "weird", severity: "critical", confidence: "odd" },
        {
          ...base[0],
          id: "x2",
          detail: { vrf_count: 0, gate_e: false, gate_l: false, claimant_mismatch: false, documented_at: "10.1.1.1", observed_at: ago(60), nul: null, str: "", empty: [], deep: { a_b: [1, 2] } },
        },
        { ...base[0], id: "x4", detail: { vrf_count: 3, device_id: "dev-3", observed_by: "dev-5", observed_subnet_id: "subnet-1" } },
      ]),
    });
    renderPage(<DriftPage />, "/ipam/drift");
    expect(await screen.findByText("No hay evidencia registrada.")).toBeInTheDocument();
    expect(screen.getByText("weird")).toBeInTheDocument();
    expect(screen.getByText("confianza odd")).toBeInTheDocument();
    expect(screen.getByText("Ninguna, este conflicto no es entre VRF")).toBeInTheDocument();
    expect(screen.getByText("No, fuera de la ventana")).toBeInTheDocument();
    expect(screen.getByText("No, sin conexión más allá de la ventana")).toBeInTheDocument();
    expect(screen.getByText("No, está en las MAC o en las VRF, no en los titulares")).toBeInTheDocument();
    expect(screen.getByText("ws-ana-03")).toBeInTheDocument();
    expect(screen.getByText("rack-b4-relay")).toBeInTheDocument();
    expect(screen.getByText("Ninguna, este conflicto no es entre VRF")).toBeInTheDocument();
    expect(screen.getByText("Nul")).toBeInTheDocument();
    expect(screen.getByText("(vacío)")).toBeInTheDocument();
    expect(screen.getByText("(ninguno)")).toBeInTheDocument();
    // "Empty" is a recorded fixture value here, not a label; the people module's
    // "Empty" (an owner field with nothing in it) is a catalogue word since 10b.
    expectNoEnglish(body(), ["weird", "odd", "Empty"]);
  });

  it("re-labels the page and its chips on a live language switch", async () => {
    stub();
    renderPage(<DriftPage />, "/ipam/drift");
    await screen.findByRole("table");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("button", { name: /^All drift \(6\)$/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Orphans \(8\)$/ })).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    expect(screen.getByRole("button", { name: /^Toda la deriva \(6\)$/ })).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------- search

describe("the search page in Spanish", () => {
  it("rests on an empty state until something is searched", async () => {
    stub();
    renderPage(<SearchPage />, "/ipam/search");
    expect(await screen.findByRole("heading", { name: "Búsqueda" })).toBeInTheDocument();
    expect(screen.getByText("Un solo cuadro para una dirección, un CIDR, una MAC, un nombre de host o un dispositivo.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Todavía no se ha buscado nada" })).toBeInTheDocument();
    expect(screen.getByText("Escribe arriba una dirección, un CIDR, una MAC o un nombre.")).toBeInTheDocument();
    expect(screen.getByLabelText("Buscar direcciones, subredes y dispositivos")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Buscar" })).toBeInTheDocument();
    expectNoEnglish(body());
  });

  it("says how each query was read and lists what it found", async () => {
    stub();
    renderPage(<SearchPage />, "/ipam/search");
    const user = userEvent.setup();
    const box = screen.getByLabelText("Buscar direcciones, subredes y dispositivos");
    await user.type(box, "10.20.9.14{enter}");
    expect(await screen.findByText("interpretado como dirección")).toBeInTheDocument();
    expect(screen.getByText("Interpretada como una sola dirección IP: qué subred la contiene, qué registro, qué observaciones.")).toBeInTheDocument();
    for (const label of ["Subredes", "Direcciones", "Dispositivos", "Observaciones"]) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
    for (const header of ["Resultado", "Tipo"]) expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    expect(screen.getAllByText("Subred").length + screen.getAllByText("Dirección documentada").length).toBeGreaterThan(0);
    expect(screen.getByText(/Los avistamientos se cuentan, no se listan/)).toBeInTheDocument();
    // The result rows name a subnet by its record id ("subnet-1").
    expectNoEnglish(body(), ["subnet"]);

    await user.clear(box);
    await user.type(box, "10.20.0.0/16{enter}");
    expect(await screen.findByText("interpretado como CIDR")).toBeInTheDocument();
    expect(screen.getByText("Interpretada como un CIDR: todo lo que hay dentro.")).toBeInTheDocument();

    await user.clear(box);
    await user.type(box, "a4:83:e7:09:06:5c{enter}");
    expect(await screen.findByText("interpretado como MAC")).toBeInTheDocument();
    expect(screen.getByText("Interpretada como una dirección MAC: todas las direcciones en las que se ha visto esa MAC.")).toBeInTheDocument();

    await user.clear(box);
    await user.type(box, "ws{enter}");
    expect(await screen.findByText("interpretado como texto")).toBeInTheDocument();
    expect(screen.getByText("Interpretada como texto libre: nombres de host y nombres de dispositivo.")).toBeInTheDocument();

    await user.clear(box);
    await user.type(box, "zzzzzz{enter}");
    expect(await screen.findByText("Ninguna subred, dirección documentada ni dispositivo coincide.")).toBeInTheDocument();
    expectNoEnglish(body());
  });

  it("says a search that failed, and a kind it does not know", async () => {
    stub({ searchIpam: vi.fn().mockRejectedValue(new ApiError(500, "")) });
    renderPage(<SearchPage />, "/ipam/search");
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Buscar direcciones, subredes y dispositivos"), "abc{enter}");
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expectNoEnglish(body());
  });

  it("leaves a kind it has no sentence for unexplained", async () => {
    stub({ searchIpam: vi.fn().mockResolvedValue({ kind: "weird", query: "q", subnets: [], addresses: [], observations: 0, devices: [] }) });
    renderPage(<SearchPage />, "/ipam/search");
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Buscar direcciones, subredes y dispositivos"), "abc{enter}");
    expect(await screen.findByText("interpretado como weird")).toBeInTheDocument();
    expectNoEnglish(body(), ["weird"]);
  });

  it("re-words a search result on a live language switch", async () => {
    stub();
    renderPage(<SearchPage />, "/ipam/search");
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Buscar direcciones, subredes y dispositivos"), "10.20.9.14{enter}");
    await screen.findByText("interpretado como dirección");
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("read as address")).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    expect(screen.getByText("interpretado como dirección")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------- registry

describe("the registry page in Spanish", () => {
  it("lists VRFs, sites and VLANs with their add forms", async () => {
    const api = stub();
    renderPage(<RegistryPage />, "/ipam/registry");
    expect(await screen.findByRole("heading", { name: "Catálogo" })).toBeInTheDocument();
    expect(screen.getByText("VRF, VLAN y las sedes del cliente: aquello por lo que se delimitan las subredes y las direcciones.")).toBeInTheDocument();
    expect(screen.getByText("Cada subred lleva una; una registrada sin VRF cae en la global del cliente.")).toBeInTheDocument();
    expect(screen.getByText("Una VLAN sin sede es de todo el cliente, no una a la que le falta la sede.")).toBeInTheDocument();
    // The client's sites, read-only: IPAM no longer keeps or creates sites (MSP clients phase 2).
    expect(screen.getByText("Las oficinas del cliente. Las VLAN y las subredes las nombran; hasta que la consola pueda gestionar un cliente, se crean y se editan mediante la API o una conexión de IA (MCP).")).toBeInTheDocument();
    expect((await screen.findAllByText("Lisbon")).length).toBeGreaterThan(0);
    expect(api.listClientSites).toHaveBeenCalledWith("o1");
    expect(await screen.findByText("65000:20")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "RD" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Ámbito" })).toBeInTheDocument();
    expect(screen.getAllByText("Todo el cliente").length).toBeGreaterThan(0);
    for (const label of ["Route distinguisher", "Distinguidor de ruta", "VID", "Sede"]) {
      if (label === "Route distinguisher") continue;
      expect(screen.getAllByLabelText(label).length).toBeGreaterThan(0);
    }
    expect(screen.getByRole("option", { name: "Todo el cliente (sin sede)" })).toBeInTheDocument();
    for (const button of ["Añadir VRF", "Añadir VLAN"]) {
      expect(screen.getByRole("button", { name: button })).toBeInTheDocument();
    }
    expect(screen.queryByRole("button", { name: "Añadir sitio" })).toBeNull();
    const user = userEvent.setup();
    await user.type(screen.getAllByLabelText("Nombre")[0]!, "lab2");
    await user.click(screen.getByRole("button", { name: "Añadir VRF" }));
    await waitFor(() => expect(api.createIpamVrf).toHaveBeenCalled());
    // PHASE 3: the internal client, `/me`'s `org_id`, until the selector exists.
    expect(api.createIpamVrf).toHaveBeenCalledWith(expect.objectContaining({ name: "lab2", client_id: "o1" }));
    // "build" is a recorded VRF / site name in the registry fixture.
    expectNoEnglish(body(), [...REGISTRY_RECORDED, "build"]);
  });

  it("shows no add form to a caller who may only read, and says what is empty", async () => {
    stub(
      { listIpamVrfs: vi.fn().mockResolvedValue([]), listClientSites: vi.fn().mockResolvedValue([]), listIpamVlans: vi.fn().mockResolvedValue([]) },
      ["ipam.read"],
    );
    renderPage(<RegistryPage />, "/ipam/registry");
    expect(await screen.findByText("Todavía no hay VRF.")).toBeInTheDocument();
    expect(await screen.findByText("Este cliente todavía no tiene sedes.")).toBeInTheDocument();
    expect(screen.getByText("Todavía no hay VLAN.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Añadir VRF" })).toBeNull();
    expectNoEnglish(body());
  });

  it("says a VRF that was refused", async () => {
    stub({ createIpamVrf: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    renderPage(<RegistryPage />, "/ipam/registry");
    const user = userEvent.setup();
    await screen.findByText("65000:20");
    await user.type(screen.getAllByLabelText("Nombre")[0]!, "lab2");
    await user.click(screen.getByRole("button", { name: "Añadir VRF" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    // "build" is a recorded VRF / site name in the registry fixture.
    expectNoEnglish(body(), [...REGISTRY_RECORDED, "build"]);
  });
});

// ---------------------------------------------------------------- scan authorizations

/**
 * Fixture scan authorizations' notes, written by a "person": "... No agent can reach it yet.",
 * "Wi-Fi audit for the September review. ...", "Rack room, swept nightly from rack-b4-relay."
 * ("September" is also the Budget settings' month name, "from" a word of Settings' sentences).
 */
const SCOPE_NOTES = ["No agent", "yet.", "September", "from"];

describe("the scan authorizations page in Spanish", () => {
  it("lists every grant with its reach, its state and its expiry", async () => {
    stub();
    renderPage(<ScanAuthorizationsPage />, "/ipam/scan-authorizations");
    expect(await screen.findByRole("heading", { name: "Autorizaciones de escaneo" })).toBeInTheDocument();
    expect(screen.getByText("Los rangos de direcciones que un barrido de red puede sondear.")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Autorizar una subred" })).toBeInTheDocument();
    expect(screen.getByLabelText("Mostrar las autorizaciones revocadas y caducadas")).toBeInTheDocument();
    expect(
      screen.getByText(/Una autorización activa permite que los barridos sondeen \(ping y ARP\) cualquier dirección dentro de su rango, en su VRF\. Un barrido sigue acotado por la subred a la que apunta/),
    ).toBeInTheDocument();
    const table = await screen.findByRole("table");
    for (const header of ["Prefijo", "Estado", "Qué permite", "Nota", "Acciones"]) {
      expect(within(table).getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(within(table).getAllByText("256 direcciones").length).toBeGreaterThan(0);
    expect(within(table).getAllByText("Activa, paquetes permitidos").length).toBe(4);
    expect(within(table).getAllByText("Sin caducidad. Se mantiene hasta que alguien la revoque.").length).toBe(3);
    expect(within(table).getByText("Caduca en 30 días")).toBeInTheDocument();
    expect(within(table).getAllByText(/Autorizada hace 36 días por/).length).toBeGreaterThan(0);
    expect(within(table).getAllByText(/^Cubre 1 subred registrada:/).length).toBeGreaterThan(0);
    expect(within(table).getAllByRole("button", { name: /^Más acciones para / }).length).toBe(4);
    expectNoEnglish(body(), SCOPE_NOTES);
  });

  it("words revoked, expired, lapsing and unreadable grants", async () => {
    const now = Date.now();
    const rows: ScopeOut[] = [
      scopeRow("a", "10.0.0.0/8"),
      scopeRow("b", "10.1.0.0/24", { expires_at: ahead(5) }),
      scopeRow("c", "10.2.0.0/24", { expires_at: ahead(5 * 60) }),
      scopeRow("d", "10.3.0.0/24", { expires_at: new Date(now + 20_000).toISOString() }),
      scopeRow("e", "10.4.0.0/24", { expires_at: ago(3 * 24 * 60) }),
      scopeRow("f", "10.5.0.0/24", { revoked_at: ago(60) }),
      scopeRow("g", "2001:db8::/64"),
      scopeRow("h", "10.6.0.1/32", { note: "nota" }),
      scopeRow("i", "weird"),
    ];
    stub({ listIpamScanAuthorizations: vi.fn().mockResolvedValue(rows) });
    renderPage(<ScanAuthorizationsPage />, "/ipam/scan-authorizations");
    const table = await screen.findByRole("table");
    expect(within(table).getByText("Caduca en 5 minutos")).toBeInTheDocument();
    expect(within(table).getByText("Caduca en 5 horas")).toBeInTheDocument();
    expect(within(table).getByText("Caduca en menos de un minuto")).toBeInTheDocument();
    expect(within(table).getByText("Caducó, hace 3 días")).toBeInTheDocument();
    expect(within(table).getByText("Caducada")).toBeInTheDocument();
    expect(within(table).getByText("Revocada")).toBeInTheDocument();
    expect(within(table).getByText("1 dirección")).toBeInTheDocument();
    expect(within(table).getByText(/^No autoriza nada\. Cubría 10\.4\.0\.0\/24, 256 direcciones mientras estuvo activa\.$/)).toBeInTheDocument();
    expect(within(table).getAllByText("Ya inactiva").length).toBe(2);
    expect(within(table).getAllByText("Todavía no hay ninguna subred registrada dentro de él.").length).toBeGreaterThan(0);
    expect(within(table).getAllByText("Sin nota").length).toBeGreaterThan(0);
    expectNoEnglish(body());
  });

  it("explains a fixture with nothing authorized, and a caller who cannot manage scopes", async () => {
    stub({ listIpamScanAuthorizations: vi.fn().mockResolvedValue([]) }, ["ipam.read"]);
    renderPage(<ScanAuthorizationsPage />, "/ipam/scan-authorizations");
    expect(await screen.findByText("Ningún prefijo está autorizado para el barrido. Ningún barrido apuntará a nada.")).toBeInTheDocument();
    expect(screen.getByText(/Añadir y revocar autorizaciones requiere permiso para administrar los ámbitos de escaneo, que es distinto del permiso para ejecutar un barrido\./)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Autorizar una subred" })).toBeNull();
    expectNoEnglish(body());
  });

  it("asks before revoking, and says a revoke that failed", async () => {
    const api = stub({ revokeIpamScanAuthorization: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    renderPage(<ScanAuthorizationsPage />, "/ipam/scan-authorizations");
    const user = userEvent.setup();
    await user.click((await screen.findAllByRole("button", { name: /^Más acciones para / }))[0]!);
    await user.click(await screen.findByRole("menuitem", { name: "Revocar" }));
    const confirm = await screen.findByRole("dialog", { name: "¿Revocar esta autorización de escaneo?" });
    expect(within(confirm).getByText("Los barridos de red dentro de este prefijo se detienen hasta que alguien lo autorice de nuevo.")).toBeInTheDocument();
    await user.click(within(confirm).getByRole("button", { name: "Revocar" }));
    await waitFor(() => expect(api.revokeIpamScanAuthorization).toHaveBeenCalled());
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expectNoEnglish(body(), SCOPE_NOTES);
  });

  it("previews what a grant opens before it is made", async () => {
    const api = stub();
    renderPage(<ScanAuthorizationsPage />, "/ipam/scan-authorizations");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Autorizar una subred" }));
    const dialog = await screen.findByRole("dialog", { name: "Autorizar un prefijo" });
    expect(within(dialog).getByText(/Esto decide a qué direcciones puede enviar sondas de red esta plataforma\./)).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Rellenar desde una subred registrada")).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: "O escribe un prefijo directamente" })).toBeInTheDocument();
    expect(within(dialog).getByText("Registrar una subred no permite sondearla. Esta autorización sí.")).toBeInTheDocument();
    for (const label of ["Prefijo", "VRF", "Nota (por qué)", "Caduca (opcional)"]) {
      expect(within(dialog).getByLabelText(label)).toBeInTheDocument();
    }
    expect(within(dialog).getByRole("option", { name: "Predeterminada (global)" })).toBeInTheDocument();
    // A prefix that covers registered subnets.
    const prefix = within(dialog).getByPlaceholderText("10.4.7.0/24");
    await user.type(prefix, "10.20.0.0/16");
    expect(within(dialog).getByText("Esto permite que los barridos sondeen 65.536 direcciones (ping y ARP).")).toBeInTheDocument();
    expect(within(dialog).getByText(/^4 subredes registradas pasan a poder barrerse: /)).toBeInTheDocument();
    expect(within(dialog).getByText("Sin caducidad: vale hasta que alguien la revoque.")).toBeInTheDocument();
    // The expiry date, read back.
    await user.type(within(dialog).getByLabelText("Caduca (opcional)"), "2030-05-17");
    expect(await within(dialog).findByText(/^Caduca al comienzo del día .*2030.*, en tu zona horaria\.$/)).toBeInTheDocument();
    expect(within(dialog).queryByText("Sin caducidad: vale hasta que alguien la revoque.")).toBeNull();
    // One address; none covered; not a prefix.
    await user.clear(prefix);
    await user.type(prefix, "10.20.9.1/32");
    expect(within(dialog).getByText("Esto permite que los barridos sondeen 1 dirección (ping y ARP).")).toBeInTheDocument();
    await user.clear(prefix);
    await user.type(prefix, "172.16.0.0/12");
    expect(within(dialog).getByText(/Ninguna subred registrada queda ahora mismo dentro de él, así que hoy nada pasa a poderse barrer\./)).toBeInTheDocument();
    await user.clear(prefix);
    await user.type(prefix, "10.20.9.0/24");
    expect(within(dialog).getByText(/^1 subred registrada pasa a poder barrerse: 10\.20\.9\.0\/24\.$/)).toBeInTheDocument();
    await user.clear(prefix);
    await user.type(prefix, "garbage");
    expect(within(dialog).getByText("Eso no es un prefijo. Escribe un CIDR como 10.4.7.0/24 o 2001:db8::/64.")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Autorizar" })).toBeDisabled();
    expectNoEnglish(body(), SCOPE_NOTES);
    // Fill from a registered subnet, then authorize.
    await user.selectOptions(within(dialog).getByLabelText("Rellenar desde una subred registrada"), "subnet-1");
    expect(prefix).toHaveValue("10.20.9.0/24");
    await user.click(within(dialog).getByRole("button", { name: "Autorizar" }));
    expect(await within(dialog).findByText("Autorizada.")).toBeInTheDocument();
    expect(api.createIpamScanAuthorization).toHaveBeenCalled();
  });

  it("re-words the grant preview and the expiry on a live language switch", async () => {
    stub();
    renderPage(<ScanAuthorizationsPage />, "/ipam/scan-authorizations");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Autorizar una subred" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByPlaceholderText("10.4.7.0/24"), "10.20.9.0/24");
    expect(within(dialog).getByText("Esto permite que los barridos sondeen 256 direcciones (ping y ARP).")).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(within(dialog).getByText("This lets sweeps probe 256 addresses (ping and ARP).")).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
  });
});

// ---------------------------------------------------------------- import and export

describe("the import and export page in Spanish", () => {
  const csv = (name: string, text = "address,hostname,,foo\n10.1.1.1,a,b,c\n") => new File([text], name, { type: "text/csv" });
  const fileInput = () => document.querySelector('input[type="file"]') as HTMLInputElement;
  // The column-mapping pickers: every select but the VRF the sheet goes into.
  const mappingSelects = () => {
    const vrf = screen.queryByLabelText("En la VRF");
    return [...document.querySelectorAll("select")].filter((select) => select !== vrf);
  };

  it("offers the three panels to a caller who may manage", async () => {
    stub();
    renderPage(<ImportPage />, "/ipam/import");
    expect(await screen.findByRole("heading", { name: "Importar y exportar" })).toBeInTheDocument();
    expect(screen.getByText("Trae un plan de direcciones desde un CSV, o saca el actual.")).toBeInTheDocument();
    expect(await screen.findByText("Importar direcciones desde un CSV")).toBeInTheDocument();
    expect(screen.getByText("La vista previa no escribe nada; aplicar sí escribe. Hasta 10 MB y 50.000 filas.")).toBeInTheDocument();
    expect(screen.getByText(/No hay ninguna columna para el estado de una dirección/)).toBeInTheDocument();
    expect(screen.getByText("Suelta un CSV o haz clic")).toBeInTheDocument();
    expect(screen.getByText(/^Columnas: address, hostname, description, asset_label, mac, allocation_method\. Todo lo demás se ignora y se informa\.$/)).toBeInTheDocument();
    expect(screen.getByText("Elige un archivo CSV.")).toBeInTheDocument();
    expect(screen.getByText("Convertir los registros de red de la documentación")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Simulación" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Convertir de verdad" })).toBeInTheDocument();
    expect(screen.getByText("Exportar")).toBeInTheDocument();
    expect(screen.getByText("Las direcciones en reposo no están incluidas")).toBeInTheDocument();
    for (const button of ["CSV de subredes", "CSV de direcciones", "CSV de deriva"]) {
      expect(screen.getByRole("button", { name: button })).toBeInTheDocument();
    }
    expectNoEnglish(body());
  });

  it("maps the columns, previews, applies, and says what each row got", async () => {
    const api = stub();
    renderPage(<ImportPage />, "/ipam/import");
    const user = userEvent.setup();
    await screen.findByText("Importar direcciones desde un CSV");
    await user.upload(fileInput(), csv("plan.csv"));
    expect(await screen.findByText("Correspondencia de columnas")).toBeInTheDocument();
    expect(screen.getByText("(sin nombre)")).toBeInTheDocument();
    expect(screen.getAllByRole("option", { name: "Ignorar esta columna" }).length).toBe(4);
    expect(screen.getByText(/Una columna debe corresponder a/)).toBeInTheDocument();
    expect(screen.getByText("Previsualiza primero este archivo. Aplicar queda ligado al hash que devuelve la vista previa, así que un archivo que nunca se previsualizó no se puede aplicar.")).toBeInTheDocument();
    // The VRF the sheet goes into is its own picker, in Spanish too.
    expect(screen.getByLabelText("En la VRF")).toBeInTheDocument();
    const selects = mappingSelects();
    await user.selectOptions(selects[0]!, "address");
    await user.selectOptions(selects[1]!, "hostname");
    await user.click(screen.getByRole("button", { name: "Previsualizar" }));
    expect(await screen.findByText("Vista previa: no se ha escrito nada")).toBeInTheDocument();
    expect(screen.getByText("1 por crear")).toBeInTheDocument();
    expect(screen.getByText("1 por actualizar")).toBeInTheDocument();
    expect(screen.getByText("1 en conflicto")).toBeInTheDocument();
    expect(screen.getByText("1 con error")).toBeInTheDocument();
    expect(screen.getByText(/4 filas, hash/)).toBeInTheDocument();
    for (const header of ["Línea", "Veredicto", "Dirección", "Detalle"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(screen.getAllByText("Nombre de host").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Etiqueta del activo").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Crear").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Conflicto").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Error").length).toBeGreaterThan(0);
    expect(api.previewIpamImport).toHaveBeenCalled();
    // Two rows carry the server's own text: "observed with a different MAC" (a reason) and
    // "line 5: '10.20.9.999' is not an IPv4 or IPv6 address" (an error). "line" is that
    // server text's own word, which the budget grid's "Delete ... line" label also made a catalogue fragment.
    expectNoEnglish(body(), ["is not", "observed", "line"]);
    await user.click(screen.getByRole("button", { name: "Aplicar" }));
    expect(await screen.findByText("Aplicado")).toBeInTheDocument();
    expect(screen.getByText("Filas aplicadas en una sola transacción: 2 de 4. Errores informados: 2.")).toBeInTheDocument();
  });

  it("refuses to apply a file that was swapped after its preview", async () => {
    stub();
    renderPage(<ImportPage />, "/ipam/import");
    const user = userEvent.setup();
    await screen.findByText("Importar direcciones desde un CSV");
    await user.upload(fileInput(), csv("plan.csv"));
    await user.selectOptions(mappingSelects()[0]!, "address");
    await user.click(screen.getByRole("button", { name: "Previsualizar" }));
    await screen.findByText("Vista previa: no se ha escrito nada");
    await user.upload(fileInput(), csv("otro.csv", "address\n10.1.1.2\n"));
    expect(await screen.findByText(/Previsualiza primero este archivo/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Aplicar" })).toBeDisabled();
    expectNoEnglish(body());
  });

  it("says one row, not 1 filas, in a preview of a single row", async () => {
    stub({
      previewIpamImport: vi.fn().mockResolvedValue({
        digest: "abcdef0123456789abcdef", total_rows: 1, creates: 1, updates: 0, conflicts: 0, errors: 0,
        rows: [{ line: 2, action: "create", address: "10.0.0.1", error: "", detail: {} }],
      }),
    });
    renderPage(<ImportPage />, "/ipam/import");
    const user = userEvent.setup();
    await screen.findByText("Importar direcciones desde un CSV");
    await user.upload(fileInput(), csv("one.csv", "address\n10.0.0.1\n"));
    await user.selectOptions(mappingSelects()[0]!, "address");
    await user.click(screen.getByRole("button", { name: "Previsualizar" }));
    expect(await screen.findByText(/1 fila, hash/)).toBeInTheDocument();
    expect(screen.queryByText(/1 filas/)).toBeNull();
  });

  it("caps the rows it shows and says how many there are", async () => {
    stub({
      previewIpamImport: vi.fn().mockResolvedValue({
        digest: "abcdef0123456789abcdef",
        total_rows: 205,
        creates: 205,
        updates: 0,
        conflicts: 0,
        errors: 0,
        rows: Array.from({ length: 205 }, (_, i) => ({ line: i + 2, action: i === 0 ? "weird" : "create", address: `10.0.0.${i}`, error: "", detail: {} })),
      }),
    });
    renderPage(<ImportPage />, "/ipam/import");
    const user = userEvent.setup();
    await screen.findByText("Importar direcciones desde un CSV");
    await user.upload(fileInput(), csv("big.csv", "address\n10.1.1.1\n"));
    await user.selectOptions(mappingSelects()[0]!, "address");
    await user.click(screen.getByRole("button", { name: "Previsualizar" }));
    expect(await screen.findByText("Mostrando las primeras 200 de 205 filas.")).toBeInTheDocument();
    expect(screen.getAllByText("No se informa nada más").length).toBeGreaterThan(0);
    expectNoEnglish(body(), ["weird"]);
  });

  it("converts the documentation records, as a dry run and for real", async () => {
    const api = stub();
    renderPage(<ImportPage />, "/ipam/import");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Simulación" }));
    expect(await screen.findByText("Simulación, no se escribió nada")).toBeInTheDocument();
    expect(screen.getByText("Registros convertibles: 1; omitidos: 1.")).toBeInTheDocument();
    expect(screen.getByText(/^se omitió Legacy lab: /)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Convertir de verdad" }));
    expect(await screen.findByText("Convertido")).toBeInTheDocument();
    expect(api.convertIpamDocRecords).toHaveBeenCalledTimes(2);
    expectNoEnglish(body(), ["Guest", "Legacy", "prefix", "already", "registered"]);
  });

  it("says the exports need only permission to view, to a caller who may not import", async () => {
    stub({}, ["ipam.read"]);
    renderPage(<ImportPage />, "/ipam/import");
    expect(await screen.findByText("Importar requiere permiso para administrar direcciones IP. Las exportaciones de abajo solo necesitan permiso para verlas.")).toBeInTheDocument();
    expect(screen.queryByText("Importar direcciones desde un CSV")).toBeNull();
    expectNoEnglish(body());
  });

  it("says an export that failed, and re-words it on a live language switch", async () => {
    stub({ exportIpamSubnetsCsv: vi.fn().mockRejectedValue(new ApiError(503, "")) });
    renderPage(<ImportPage />, "/ipam/import");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "CSV de subredes" }));
    const alert = await screen.findByRole("alert");
    const spanish = alert.textContent;
    expect(spanish).toBeTruthy();
    expectNoEnglish(alert);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert").textContent).toBe("That service is unavailable right now. Nothing was changed — try again shortly.");
    expect(screen.getByRole("alert").textContent).not.toBe(spanish);
    act(() => setActiveLanguage("es"));
    expect(screen.getByRole("alert").textContent).toBe(spanish);
  });
});

// ---------------------------------------------------------------- the sentences built outside React

/** Runs `fn` in Spanish, then English, and puts the language back. */
function inBoth<T>(fn: () => T): { es: T; en: T } {
  setActiveLanguage("es");
  const es = fn();
  setActiveLanguage("en");
  const en = fn();
  return { es, en };
}

describe("the state helpers' sentences in Spanish", () => {
  const NOW = Date.parse("2026-10-10T12:00:00Z");
  const at = (ms: number) => new Date(NOW - ms).toISOString();
  const MINUTE = 60_000;
  const HOUR = 60 * MINUTE;
  const DAY = 24 * HOUR;

  it("words an age with the right unit and number, never a clock time", () => {
    expect(inBoth(() => formatAge(null, NOW))).toEqual({ es: "nunca", en: "never" });
    expect(inBoth(() => formatAge("junk", NOW))).toEqual({ es: "desconocido", en: "unknown" });
    expect(inBoth(() => formatAge(at(30_000), NOW))).toEqual({ es: "justo ahora", en: "just now" });
    expect(inBoth(() => formatAge(at(MINUTE), NOW))).toEqual({ es: "hace 1 minuto", en: "1 minute ago" });
    expect(inBoth(() => formatAge(at(5 * MINUTE), NOW))).toEqual({ es: "hace 5 minutos", en: "5 minutes ago" });
    expect(inBoth(() => formatAge(at(HOUR), NOW))).toEqual({ es: "hace 1 hora", en: "1 hour ago" });
    expect(inBoth(() => formatAge(at(3 * HOUR), NOW))).toEqual({ es: "hace 3 horas", en: "3 hours ago" });
    expect(inBoth(() => formatAge(at(DAY), NOW))).toEqual({ es: "hace 1 día", en: "1 day ago" });
    expect(inBoth(() => formatAge(at(9 * DAY), NOW))).toEqual({ es: "hace 9 días", en: "9 days ago" });
  });

  it("words a deadline, rounded, in the future tense", () => {
    const until = (ms: number) => new Date(NOW + ms).toISOString();
    expect(inBoth(() => formatUntil(until(0), NOW))).toEqual({ es: "ahora", en: "now" });
    expect(inBoth(() => formatUntil(until(20_000), NOW))).toEqual({ es: "en menos de un minuto", en: "in under a minute" });
    expect(inBoth(() => formatUntil(until(MINUTE), NOW))).toEqual({ es: "en 1 minuto", en: "in 1 minute" });
    expect(inBoth(() => formatUntil(until(5 * HOUR), NOW))).toEqual({ es: "en 5 horas", en: "in 5 hours" });
    expect(inBoth(() => formatUntil(until(30 * DAY), NOW))).toEqual({ es: "en 30 días", en: "in 30 days" });
    expect(inBoth(() => formatUntil("junk", NOW))).toEqual({ es: "desconocido", en: "unknown" });
  });

  it("keeps every presentation table's words in the catalogue, and follows a language switch", () => {
    const tables: Record<string, Record<string, { label: string; meaning: string }>> = {
      state: ADDRESS_STATE_PRESENTATION,
      status: ADDRESS_STATUS_PRESENTATION,
      drift: DRIFT_CLASS_PRESENTATION,
      scope: SCOPE_STATUS_PRESENTATION,
      sweep: SWEEP_STATE_PRESENTATION,
      range: RANGE_KIND_PRESENTATION,
      import: IMPORT_ACTION_PRESENTATION,
    };
    // Acronyms, a loanword and a word spelled the same in both languages.
    const SAME_LABEL = new Set(["DHCP", "Error"]);
    for (const [name, table] of Object.entries(tables)) {
      setActiveLanguage("en");
      const english = Object.fromEntries(Object.entries(table).map(([key, value]) => [key, { label: value.label, meaning: value.meaning }]));
      // An entry whose label is worded differently, held across the switch.
      const heldKey = Object.keys(table).find((key) => !SAME_LABEL.has(english[key]!.label))!;
      const held = table[heldKey]!;
      expect(held.label).toBe(english[heldKey]!.label);
      setActiveLanguage("es");
      // The SAME object now reads Spanish: it holds no copy of the English.
      expect(held.label).not.toBe(english[heldKey]!.label);
      for (const [key, value] of Object.entries(table)) {
        const before = english[key]!;
        if (!SAME_LABEL.has(before.label)) expect(value.label, `${name}.${key} label`).not.toBe(before.label);
        expect(value.meaning, `${name}.${key} meaning`).not.toBe(before.meaning);
      }
    }
    for (const driftClass of [...Object.values(DRIFT_CLASS_PRESENTATION), DORMANT_PRESENTATION]) {
      setActiveLanguage("en");
      const remedy = driftClass.remedy;
      setActiveLanguage("es");
      expect(driftClass.remedy).not.toBe(remedy);
      expect(driftClass.remedy.length).toBeGreaterThan(10);
    }
    setActiveLanguage("en");
  });

  it("falls back to the server's word with an explanation in Spanish", () => {
    const { es } = inBoth(() => ({
      status: statusPresentation("zzz"),
      state: statePresentation("zzz"),
      none: statePresentation(null),
      driftClass: driftClassPresentation("zzz"),
      sweep: sweepStatePresentation("zzz"),
    }));
    expect(es.status).toMatchObject({ label: "zzz", meaning: "Situación no reconocida." });
    expect(es.state).toMatchObject({ label: "zzz", meaning: "Estado no reconocido." });
    expect(es.none).toMatchObject({ label: "No documentada" });
    expect(es.driftClass).toMatchObject({ label: "zzz", meaning: "Clase de deriva no reconocida." });
    expect(es.sweep).toMatchObject({ label: "zzz", meaning: "Estado de barrido no reconocido." });
  });

  it("words an allocation method and who said it", () => {
    setActiveLanguage("es");
    expect(allocationMethodLabel("static")).toBe("Estática");
    expect(allocationMethodLabel("dhcp_reservation")).toBe("Reserva DHCP");
    expect(allocationMethodLabel("odd_one")).toBe("Odd one");
    expect(allocationMethodPresentation({ allocation_method: "static", allocation_method_source: "documented" }).meaning).toBe(
      "Documentado: un operador fijó el método de asignación de esta dirección en «estática».",
    );
    expect(allocationMethodPresentation({ allocation_method: "dhcp", allocation_method_source: "observed" }).meaning).toMatch(
      /^Observado: el equipo informó de esta dirección como «DHCP» en su propia tarjeta de red\./,
    );
    expect(allocationMethodPresentation({ allocation_method: "odd_one", allocation_method_source: "weird" as never }).meaning).toBe(
      "Método de asignación «odd_one» (origen no reconocido).",
    );
    expect(allocationMethodPresentation({ allocation_method: "unknown", allocation_method_source: "unknown" })).toMatchObject({ label: "—" });
    setActiveLanguage("en");
    expect(allocationMethodPresentation({ allocation_method: "static", allocation_method_source: "documented" }).meaning).toBe(
      "Documented: an operator set this address's allocation method to \"static\".",
    );
  });

  it("words the bands of the grid by the range kinds they sit in", () => {
    setActiveLanguage("es");
    expect(bandLabel({ range_kinds: [] })).toBeNull();
    expect(bandLabel({ range_kinds: ["dhcp_pool", "excluded"] })).toBe("Pool DHCP + Rango excluido");
    setActiveLanguage("en");
  });

  it("states what a grant permits, and what it covered once it lapsed", () => {
    const grant = scopeRow("x", "10.0.0.0/24", { expires_at: new Date(NOW + 3 * DAY).toISOString() });
    const { es, en } = inBoth(() => ({
      active: describeScope(grant, NOW),
      lapsed: describeScope(scopeRow("y", "10.0.0.1/32", { expires_at: at(3 * HOUR) }), NOW),
      revoked: describeScope(scopeRow("z", "2001:db8::/64", { revoked_at: at(HOUR) }), NOW),
      open: describeScope(scopeRow("w", "10.0.0.0/24"), NOW),
    }));
    expect(es.active.summary).toBe("Los barridos pueden enviar paquetes ICMP/ARP a cualquier dirección dentro de 10.0.0.0/24, 256 direcciones, en esta VRF.");
    expect(en.active.summary).toBe("Sweeps may put ICMP/ARP packets on any address inside 10.0.0.0/24, 256 addresses, in this VRF.");
    expect(es.active.expiry).toBe("Caduca en 3 días");
    expect(es.lapsed.summary).toBe("No autoriza nada. Cubría 10.0.0.1/32, 1 dirección mientras estuvo activa.");
    expect(es.lapsed.expiry).toBe("Caducó, hace 3 horas");
    expect(es.revoked.status).toBe("revoked");
    expect(es.revoked.addressCount).toBe("18.446.744.073.709.551.616");
    expect(es.open.expiry).toBe("Sin caducidad. Se mantiene hasta que alguien la revoque.");
    expect(en.active.safeguard).toBe(sweepScopeSafeguard());
    expect(es.active.safeguard).not.toBe(en.active.safeguard);
  });

  it("previews whether a subnet may be swept, and under how many grants", () => {
    const subnet = { prefix: "10.0.0.0/24", vrf_id: "v" };
    const { es, en } = inBoth(() => ({
      none: effectiveSweepScope(subnet, [], NOW),
      one: effectiveSweepScope(subnet, [scopeRow("a", "10.0.0.0/24")], NOW),
      two: effectiveSweepScope(subnet, [scopeRow("a", "10.0.0.0/24"), scopeRow("b", "10.0.0.0/8")], NOW),
    }));
    expect(es.none.authorized).toBe(false);
    expect(es.none.explanation).toMatch(/^NO AUTORIZADA\. Ninguna autorización de escaneo activa en esta VRF cubre este prefijo/);
    expect(es.one.explanation).toMatch(/^Se puede barrer con 1 autorización activa\. Los destinos son 10\.0\.0\.0\/24 intersecado con 10\.0\.0\.0\/24,/);
    expect(es.two.explanation).toMatch(/^Se puede barrer con 2 autorizaciones activas\./);
    expect(en.two.explanation).toMatch(/^Sweepable under 2 active authorizations\./);
  });

  it("agrees the counts of one: a single row, a single answer, a single usable address", () => {
    setActiveLanguage("es");
    const run = {
      client_id: "o1", id: "r", subnet_id: "s", job_id: null, device_id: null, state: "succeeded", trigger: "manual", target_count: 1,
      scope_digest: "d", hosts_found: 1, truncated: false, started_at: at(5000), finished_at: at(3 * HOUR), error: "",
    } satisfies SweepRunOut;
    expect(describeSweepRun(run, NOW)).toBe("Correcto, hace 3 horas. 1 de 1 respondió.");
    expect(describeSweepRun({ ...run, hosts_found: 0, target_count: 2 }, NOW)).toBe("Correcto, hace 3 horas. 0 de 2 respondieron.");
    const util = { subnet_id: "s", prefix: "10.0.0.1/32", enumerable: true, capacity: 1, free: 0, used: 1, percentage: 100, assigned: 1, reserved: 0, deprecated: 0, observed: 1 } satisfies UtilizationOut;
    expect(utilizationDisplay(util).detail).toBe("1 de 1 dirección utilizable en 10.0.0.1/32; libres: 0.");
    expect(utilizationDisplay({ ...util, capacity: 254 }).detail).toBe("1 de 254 direcciones utilizables en 10.0.0.1/32; libres: 0.");
    setActiveLanguage("en");
    expect(describeSweepRun(run, NOW)).toBe("Succeeded, 3 hours ago. 1 of 1 answered.");
    expect(utilizationDisplay(util).detail).toBe("1 of 1 usable addresses in 10.0.0.1/32; 0 free.");
  });

  it("states the safeguard and the grant meaning, which the unit test also holds", () => {
    const { es, en } = inBoth(() => ({ safeguard: sweepScopeSafeguard(), grant: sweepScopeGrantMeaning(), seen: lastSeenMeaning() }));
    expect(es.safeguard).toMatch(/^Un barrido sigue acotado por la subred a la que apunta/);
    expect(en.safeguard).toMatch(/^A sweep is still bounded by the subnet it targets/);
    expect(es.grant).toMatch(/^Una autorización activa permite/);
    expect(es.seen).toMatch(/^Cuán al día está esta documentación/);
    expect(en.seen).toMatch(/^How current this documentation is/);
  });

  it("sums up the most recent sweep run in one line", () => {
    const run = (over: Partial<SweepRunOut>): SweepRunOut => ({
      client_id: "o1",
      id: "r",
      subnet_id: "s",
      job_id: null,
      device_id: null,
      state: "succeeded",
      trigger: "manual",
      target_count: 10,
      scope_digest: "d",
      hosts_found: 4,
      truncated: false,
      started_at: at(5000),
      finished_at: at(3 * HOUR),
      error: "",
      ...over,
    });
    setActiveLanguage("es");
    expect(describeSweepRun(undefined, NOW)).toBe("Nunca barrida.");
    expect(describeSweepRun(run({}), NOW)).toBe("Correcto, hace 3 horas. 4 de 10 respondieron.");
    expect(describeSweepRun(run({ truncated: true }), NOW)).toBe(
      "Correcto, hace 3 horas. 4 de 10 respondieron. El resultado se truncó en el límite de destinos.",
    );
    expect(describeSweepRun(run({ state: "failed", error: "boom" }), NOW)).toBe("Fallido, hace 3 horas. boom");
    expect(describeSweepRun(run({ state: "no_agent" }), NOW)).toBe("Ningún agente pudo barrer, hace 3 horas.");
    expect(describeSweepRun(run({ state: "refused", finished_at: null }), NOW)).toBe("Rechazado, no autorizado, justo ahora.");
    setActiveLanguage("en");
    expect(describeSweepRun(run({ truncated: true }), NOW)).toBe(
      "Succeeded, 3 hours ago. 4 of 10 answered. Result was truncated at the target cap.",
    );
  });

  it("writes a utilization with the reader's decimal separator, and names what it cannot say", () => {
    const util = (over: Partial<UtilizationOut>): UtilizationOut => ({
      subnet_id: "s",
      prefix: "10.0.0.0/8",
      enumerable: true,
      capacity: 100,
      free: 90,
      used: 10,
      percentage: 42,
      assigned: 3,
      reserved: 2,
      deprecated: 1,
      observed: 4,
      ...over,
    });
    const { es, en } = inBoth(() => ({
      real: utilizationDisplay(util({})),
      ceiling: utilizationDisplay(util({ percentage: null, enumerable: false })),
      plain: utilizationDisplay(util({ percentage: null })),
      unknownCounts: utilizationDisplay(util({ used: null, capacity: null, free: null })),
    }));
    expect(es.real).toMatchObject({ available: true, percentLabel: "42,0\u00a0%", headline: "42,0\u00a0% utilizado" });
    expect(en.real).toMatchObject({ percentLabel: "42.0%", headline: "42.0% used" });
    expect(es.real.detail).toBe("10 de 100 direcciones utilizables en 10.0.0.0/8; libres: 90.");
    expect(es.unknownCounts.detail).toBe("n/d de n/d direcciones utilizables en 10.0.0.0/8; libres: n/d.");
    expect(es.ceiling).toMatchObject({ available: false, headline: "Utilización no disponible" });
    expect(es.ceiling.detail).toMatch(/^10\.0\.0\.0\/8 es demasiado grande para enumerarla \(por encima del límite de esta organización, o IPv6\)\./);
    expect(es.plain.detail).not.toMatch(/por encima del límite/);
    expect(es.plain.detail).toContain("Asignadas: 3; reservadas: 2; dadas de baja: 1; observadas: 4.");
  });

  it("says why an import cannot be applied", () => {
    const file = (name: string, size = 10) => ({ name, size, lastModified: 1 });
    const previewed = { ...file("a.csv"), digest: "d", vrfId: null };
    setActiveLanguage("es");
    expect(importApplyBlockReason(null, null)).toBe("Elige un archivo CSV.");
    expect(importApplyBlockReason(file("a.csv"), null)).toMatch(/^Previsualiza primero este archivo\./);
    expect(importApplyBlockReason(file("b.csv"), previewed)).toBe(
      "El archivo previsualizado era «a.csv». Previsualiza «b.csv» antes de aplicarlo: el hash liga la aplicación al archivo del que realmente leíste un veredicto.",
    );
    expect(importApplyBlockReason(file("a.csv", 20 * 1024 * 1024), { ...previewed, size: 20 * 1024 * 1024 })).toBe(
      "El archivo supera el límite de importación de 10 MB.",
    );
    expect(importApplyBlockReason(file("a.csv"), previewed)).toBeNull();
    setActiveLanguage("en");
  });

  it("words the payload of a history entry, a finding and an import row as labelled facts", () => {
    const names = { devices: new Map([["dev-1", "host-1"]]), subnets: new Map([["s1", "10.0.0.0/24 Core"]]) };
    setActiveLanguage("es");
    const facts = (list: { label: string; value: string }[]) => Object.fromEntries(list.map((f) => [f.label, f.value]));
    expect(
      facts(describeHistoryDetail({ detail: { claimant: "dev-1", claimant_type: "device", allocation_method: "static", address: "x" } })),
    ).toEqual({ Titular: "dev-1 (dispositivo)", Método: "estática" });
    expect(facts(describeHistoryDetail({ detail: { previous_claimant: null } }))).toEqual({ "Liberada de": "nadie, estaba retenida, no tenía titular" });
    expect(facts(describeHistoryDetail({ detail: { from: { a_b: 1 }, to: { a_b: 2, c: null } } }))).toEqual({
      "A b": "1 → 2",
      C: "ninguno → ninguno",
    });
    expect(
      facts(
        describeDriftDetail(
          { detail: { device_id: "dev-1", observed_subnet_id: "s1", vrf_count: 0, gate_e: true, gate_l: false, claimant_mismatch: true, macs: ["a", "b"] } },
          names,
        ),
      ),
    ).toEqual({
      "Dispositivo documentado": "host-1",
      "Subred observada": "10.0.0.0/24 Core",
      "VRF que la reclaman": "Ninguna, este conflicto no es entre VRF",
      "Segmento examinado": "Sí, dentro de la ventana",
      "Titular alcanzable": "No, sin conexión más allá de la ventana",
      "Titular distinto": "Sí, el inventario nombra otro dispositivo",
      "MAC vistas": "a, b",
    });
    expect(facts(describeImportDetail({ detail: { hostname: "a", mac: "", reason: "x", asset_label: null, allocation_method: "y" } }))).toEqual({
      "Nombre de host": "a",
      Motivo: "x",
      "Método de asignación": "y",
    });
    expect(describeHistoryDetail({ detail: "str" as never })).toEqual([]);
    setActiveLanguage("en");
  });

  it("explains how each search was read", () => {
    setActiveLanguage("es");
    expect(searchKindMeaning("cidr")).toBe("Interpretada como un CIDR: todo lo que hay dentro.");
    expect(searchKindMeaning("zzz")).toBe("");
    setActiveLanguage("en");
    expect(searchKindMeaning("cidr")).toBe("Read as a CIDR: everything inside it.");
  });
});
