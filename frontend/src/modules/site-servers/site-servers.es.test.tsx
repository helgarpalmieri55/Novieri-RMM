/**
 * Site servers in Spanish: the list page (its tiles, filter, loading, failed,
 * empty and filtered-to-nothing states, and the promote dialog with its
 * disabled options) and a server's page (the facts, the segment check in all
 * three of its states, and the wake form through every state a wake reaches).
 * Server names, hostnames, addresses and MAC addresses read as recorded; the
 * words around them are Spanish.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import type { ApiClient, Device, Job } from "../../lib/api";
import { SiteServerDetailPage } from "./SiteServerDetailPage";
import { SiteServersPage } from "./SiteServersPage";
import type { SegmentCheckOut, SiteServerOut } from "./types";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

// ------------------------------------------------------------------ fixtures

function server(overrides: Partial<SiteServerOut> = {}): SiteServerOut {
  return {
    id: "ss-1",
    device_id: "dev-1",
    name: "rack-b4-relay",
    segments: ["10.20.4.0/24", "10.20.9.0/24"],
    roles: ["wake"],
    enabled: true,
    notes: "",
    ipam_site_id: null,
    agent_status: "online",
    agent_last_seen_at: "2026-09-30T11:55:00Z",
    ...overrides,
  };
}

const SERVERS: SiteServerOut[] = [
  server(),
  server({ id: "ss-2", device_id: "dev-2", name: "por-branch-relay", segments: ["10.30.1.0/24"], enabled: false, agent_status: "offline", roles: ["wake", "cache", "storage"] }),
  server({ id: "ss-3", device_id: "dev-3", name: "sin-agente", segments: ["10.40.0.0/16"], agent_status: null }),
];

function device(overrides: Partial<Device> = {}): Device {
  return {
    id: "dev-1",
    hostname: "lis-app-02",
    fqdn: null,
    os_family: "linux",
    os_version: "",
    architecture: "",
    device_group_id: null,
    status: "active",
    agent_status: "online",
    last_seen_at: null,
    primary_ip: "10.20.4.5",
    ...overrides,
  } as Device;
}

const CANDIDATES = [
  device(),
  device({ id: "dev-2", hostname: "por-fs-01" }),
  device({ id: "dev-9", hostname: "impresora-3", agent_status: null }),
];

const WAKE = {
  job_id: "job-1",
  device_id: "dev-1",
  broadcast: "10.20.255.255",
  target_mac: "a4:bb:6d:11:22:33",
  expires_at: "2026-09-30T12:05:00Z",
};

function job(overrides: Partial<Job> = {}): Job {
  return {
    id: "job-1",
    type: "site.wake",
    device_id: "dev-1",
    status: "queued",
    attempts: 0,
    params: {},
    result: null,
    issued_at: "2026-09-30T12:00:00Z",
    expires_at: "2026-09-30T12:05:00Z",
    started_at: null,
    finished_at: null,
    ...overrides,
  };
}

const CHECK: SegmentCheckOut = {
  site_server_id: "ss-1",
  device_id: "dev-1",
  evidence: "ipam",
  matched: ["10.20.4.0/24"],
  declared_unobserved: ["10.20.9.0/24"],
  observed_undeclared: ["10.20.10.12"],
};

const PERMISSIONS = ["site.read", "site.manage", "site.wake", "devices.read", "ipam.read", "jobs.read"];

function stubApi(overrides: Record<string, unknown> = {}, permissions: string[] = PERMISSIONS) {
  const api = {
    me: vi.fn().mockResolvedValue({ user_id: "u1", org_id: "o1", email: "a@b.c", display_name: "A", permissions, timezone: "UTC" }),
    listSiteServers: vi.fn().mockResolvedValue(SERVERS),
    getSiteServer: vi.fn().mockResolvedValue(server()),
    getSiteServerSegmentCheck: vi.fn().mockResolvedValue(CHECK),
    getDevice: vi.fn().mockResolvedValue(device()),
    listDevices: vi.fn().mockResolvedValue({ items: CANDIDATES, total: 3, state_counts: {}, fleet: { total: 3, managed: 2, online: 1, cert_attention: 0 } }),
    listIpamSubnets: vi.fn().mockResolvedValue([{ id: "s1", prefix: "10.20.4.0/24" }]),
    promoteSiteServer: vi.fn().mockResolvedValue(server()),
    wakeViaSiteServer: vi.fn().mockResolvedValue(WAKE),
    getJob: vi.fn().mockResolvedValue(job()),
    ...overrides,
  };
  mockUseApi.mockReturnValue(api as unknown as ApiClient);
  return api;
}

function renderApp(path: string, ui: ReactElement = <Routes><Route path="/site-servers" element={<SiteServersPage />} /><Route path="/site-servers/:siteServerId" element={<SiteServerDetailPage />} /></Routes>) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderInSpanish(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => mockUseApi.mockReset());

// --------------------------------------------------------------------- tests

describe("site servers in Spanish", () => {
  it("words the list: tiles, rows (live, disabled, no agent), roles in words, and the filter", async () => {
    const user = userEvent.setup();
    stubApi();
    const { container } = renderApp("/site-servers");
    expect(await screen.findByRole("heading", { name: "Servidores de sitio" })).toBeInTheDocument();
    const table = await screen.findByRole("table", { name: "Servidores de sitio" });
    expect(within(table).getByText("Deshabilitado, sin servicio")).toBeInTheDocument();
    expect(within(table).getByText("Falta el agente")).toBeInTheDocument();
    expect(within(table).getAllByText("2 segmentos").length).toBeGreaterThan(0);
    expect(within(table).getAllByText("1 segmento").length).toBe(2);
    expect(screen.getByTestId("roles-ss-2")).toHaveTextContent("Wake-on-LAN, Caché de contenido, Storage");
    expect(screen.getByText("de 3 registrados")).toBeInTheDocument();
    expectNoEnglish(container, ["Storage"]);

    await user.selectOptions(screen.getByLabelText("Filtrar los servidores de sitio por estado"), "disabled");
    expect(screen.queryByText("rack-b4-relay")).toBeNull();
    expect(screen.getByText("por-branch-relay")).toBeInTheDocument();
    expectNoEnglish(container, ["Storage"]);
  });

  it("words the loading, failed and empty states, and a filter that matches nothing", async () => {
    stubApi({ listSiteServers: vi.fn(() => new Promise<never>(() => {})) });
    const loading = renderApp("/site-servers");
    expect(await screen.findByText("Cargando los servidores de sitio…")).toBeInTheDocument();
    expectNoEnglish(loading.container);
    loading.unmount();

    stubApi({ listSiteServers: vi.fn().mockRejectedValue(new Error("boom")) });
    const failed = renderApp("/site-servers");
    expect(await screen.findByText("No se pudieron cargar los servidores de sitio.")).toBeInTheDocument();
    expectNoEnglish(failed.container);
    failed.unmount();

    stubApi({ listSiteServers: vi.fn().mockResolvedValue([]) });
    const empty = renderApp("/site-servers");
    expect(await screen.findByText("No hay servidores de sitio")).toBeInTheDocument();
    expect(screen.getByText("No hay ningún servidor de sitio que habilitar")).toBeInTheDocument();
    expectNoEnglish(empty.container);
    empty.unmount();

    stubApi({ listSiteServers: vi.fn().mockResolvedValue([server({ enabled: false })]) });
    const filtered = renderApp("/site-servers?state=enabled");
    expect(await screen.findByText("Ningún servidor de sitio coincide con ese filtro.")).toBeInTheDocument();
    expect(screen.getByText("Todos los servidores de sitio están deshabilitados, así que ninguno presta servicio")).toBeInTheDocument();
    expectNoEnglish(filtered.container);
  });

  it("words the prerequisites panel when no computer runs the agent and no subnet exists", async () => {
    stubApi({
      listDevices: vi.fn().mockResolvedValue({ items: [], total: 0, state_counts: {}, fleet: { total: 5, managed: 0, online: 0, cert_attention: 0 } }),
      listIpamSubnets: vi.fn().mockResolvedValue([]),
    });
    const { container } = renderApp("/site-servers");
    expect(await screen.findByText(/Un servidor de sitio es un equipo que ya ejecuta el agente/)).toBeInTheDocument();
    expect(await screen.findByText(/No hay ninguna subred registrada en Direcciones IP/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Registrar el segmento" })).toBeInTheDocument();
    // "Storage" is a role the server reported that this console has no word for (as above).
    expectNoEnglish(container, ["Storage"]);
  });

  it("words the prerequisites panel when the subnets are registered", async () => {
    stubApi({ listDevices: vi.fn().mockResolvedValue({ items: CANDIDATES, total: 3, state_counts: {}, fleet: { total: 5, managed: 0, online: 0, cert_attention: 0 } }), listIpamSubnets: vi.fn().mockResolvedValue([{ id: "s1" }, { id: "s2" }]) });
    const { container } = renderApp("/site-servers");
    expect(await screen.findByText("Hay 2 subredes registradas.")).toBeInTheDocument();
    expectNoEnglish(container, ["Storage"]);
  });

  it("words the promote dialog: the devices that cannot be chosen, the roles, and a refusal", async () => {
    const user = userEvent.setup();
    const api = stubApi({ promoteSiteServer: vi.fn().mockRejectedValue({ status: 400 }) });
    renderApp("/site-servers");
    await user.click(await screen.findByRole("button", { name: "Promover dispositivo" }));
    const dialog = await screen.findByRole("dialog", { name: "Promover un dispositivo a servidor de sitio" });
    await waitFor(() => expect(within(dialog).getByRole("option", { name: /impresora-3/ })).toBeInTheDocument());
    expect(within(dialog).getByRole("option", { name: "impresora-3 (sin agente inscrito, no puede recibir tareas)" })).toBeDisabled();
    expect(within(dialog).getByRole("option", { name: "lis-app-02 (ya es un servidor de sitio)" })).toBeDisabled();
    expect(within(dialog).getByLabelText("Habilitar Wake-on-LAN")).toBeInTheDocument();
    expect(within(dialog).getByText(/Solo está disponible Wake-on-LAN/)).toBeInTheDocument();
    expectNoEnglish(dialog);
    await user.selectOptions(within(dialog).getByLabelText("Dispositivo"), "dev-2");
    await user.type(within(dialog).getByLabelText("Segmentos (CIDR separados por comas)"), "10.50.0.0/24");
    await user.click(within(dialog).getByRole("button", { name: "Promover" }));
    await waitFor(() => expect(api.promoteSiteServer).toHaveBeenCalled());
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("No se pudo promover.");
    expectNoEnglish(dialog);
  });

  it("words a server's page: facts, the segment check and the wake form", async () => {
    stubApi();
    const { container } = renderApp("/site-servers/ss-1");
    expect(await screen.findByRole("heading", { name: "rack-b4-relay" })).toBeInTheDocument();
    expect(await screen.findByText("declarado para 2 segmentos", { exact: false })).toBeInTheDocument();
    expect(await screen.findByText("Coinciden")).toBeInTheDocument();
    expect(screen.getByText(/Sin preferencia de barrido en él hasta que el inventario ubique este equipo/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Encender un equipo" })).toBeInTheDocument();
    expect(screen.getByLabelText("Segmento (obligatorio, este servidor declara varios)")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("words the segment check when the evidence is unavailable, and when everything is as declared", async () => {
    stubApi({ getSiteServerSegmentCheck: vi.fn().mockResolvedValue({ ...CHECK, evidence: "unavailable", matched: [], declared_unobserved: [], observed_undeclared: [] }) });
    const unavailable = renderApp("/site-servers/ss-1");
    expect(await screen.findByText(/No se puede verificar\. IPAM no está instalado/)).toBeInTheDocument();
    expectNoEnglish(unavailable.container);
    unavailable.unmount();

    stubApi({ getSiteServer: vi.fn().mockResolvedValue(server({ segments: ["10.20.4.0/24"] })), getSiteServerSegmentCheck: vi.fn().mockResolvedValue({ ...CHECK, matched: [], declared_unobserved: [], observed_undeclared: [] }) });
    const clean = renderApp("/site-servers/ss-1");
    expect(await screen.findByText("Aún no se ha observado ningún segmento declarado.")).toBeInTheDocument();
    expect(screen.getByText("Se ha observado cada segmento declarado.")).toBeInTheDocument();
    expect(screen.getByText("No hay direcciones fuera de los segmentos declarados.")).toBeInTheDocument();
    expect(screen.getByText("Único segmento (10.20.4.0/24)")).toBeInTheDocument();
    expectNoEnglish(clean.container);
    clean.unmount();

    stubApi({ getSiteServerSegmentCheck: vi.fn(() => new Promise<never>(() => {})) });
    const checking = renderApp("/site-servers/ss-1");
    expect(await screen.findByText("Comprobando los segmentos…")).toBeInTheDocument();
    expectNoEnglish(checking.container);
  });

  it("words a one-segment server whose declared segments are all unobserved", async () => {
    stubApi({ getSiteServer: vi.fn().mockResolvedValue(server({ segments: ["10.20.9.0/24"] })), getSiteServerSegmentCheck: vi.fn().mockResolvedValue({ ...CHECK, matched: [], observed_undeclared: [], declared_unobserved: ["10.20.9.0/24", "10.20.10.0/24"] }) });
    const { container } = renderApp("/site-servers/ss-1");
    expect(await screen.findByText(/Sin preferencia de barrido en ellos hasta que el inventario ubique este equipo/)).toBeInTheDocument();
    expect(screen.getByText("declarado para 1 segmento", { exact: false })).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("words a disabled server's page and one whose agent is missing and never seen", async () => {
    stubApi({ getSiteServer: vi.fn().mockResolvedValue(server({ enabled: false, agent_status: null, agent_last_seen_at: null })) });
    const { container } = renderApp("/site-servers/ss-1");
    expect(await screen.findByText("Este servidor de sitio está deshabilitado y no enviará encendidos. Habilítalo primero.")).toBeInTheDocument();
    expect(screen.getByText("Nunca visto")).toBeInTheDocument();
    expect(screen.getByText("Falta el agente")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("words every state a wake reaches: queued, dispatched, reported, failed and expired", async () => {
    const user = userEvent.setup();
    const states: [Partial<Job>, RegExp][] = [
      [{ status: "queued" }, /Encendido en cola para rack-b4-relay\. Enviará el encendido a 10\.20\.255\.255 para a4:bb:6d:11:22:33; todavía no se ha enviado nada\./],
      [{ status: "dispatched" }, /rack-b4-relay tiene el encendido para 10\.20\.255\.255 \(a4:bb:6d:11:22:33\) y todavía no ha informado\./],
      [{ status: "succeeded", result: { sent_at: "2026-09-30T12:01:00Z" } }, /informa de un paquete enviado a 10\.20\.255\.255 para a4:bb:6d:11:22:33 el /],
      [{ status: "succeeded", result: null }, /informa de un paquete enviado a 10\.20\.255\.255 para a4:bb:6d:11:22:33\. Eso significa/],
      [{ status: "failed" }, /El encendido falló\. rack-b4-relay no informó de ningún paquete/],
      [{ status: "expired" }, /El encendido caducó el .* sin llegar a rack-b4-relay\./],
    ];
    for (const [overrides, text] of states) {
      stubApi({ getJob: vi.fn().mockResolvedValue(job(overrides)) });
      const view = renderApp("/site-servers/ss-1");
      await screen.findByText("Coinciden");
      await user.type(screen.getByLabelText("MAC de destino"), "a4:bb:6d:11:22:33");
      await user.selectOptions(screen.getByLabelText("Segmento (obligatorio, este servidor declara varios)"), "10.20.4.0/24");
      await user.click(screen.getByRole("button", { name: "Enviar encendido" }));
      expect(await screen.findByText(text)).toBeInTheDocument();
      expectNoEnglish(view.container);
      view.unmount();
    }
  });

  it("words a refused wake", async () => {
    const user = userEvent.setup();
    stubApi({ wakeViaSiteServer: vi.fn().mockRejectedValue({ status: 403 }) });
    const { container } = renderApp("/site-servers/ss-1");
    await screen.findByText("Coinciden");
    await user.click(screen.getByRole("button", { name: "Enviar encendido" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo encender.");
    expectNoEnglish(container);
  });
});

describe("a live language switch on the site server pages", () => {
  it("re-words the list and the roles already on screen", async () => {
    stubApi();
    renderApp("/site-servers");
    await screen.findByRole("table", { name: "Servidores de sitio" });
    expect(screen.getByTestId("roles-ss-2")).toHaveTextContent("Caché de contenido");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("heading", { name: "Site servers" })).toBeInTheDocument();
    expect(screen.getByTestId("roles-ss-2")).toHaveTextContent("Wake-on-LAN, Content caching, Storage");
    expect(screen.getByText("Disabled, not serving")).toBeInTheDocument();
    expect(screen.getByText("of 3 registered")).toBeInTheDocument();
  });

  it("re-words a wake result already on screen", async () => {
    const user = userEvent.setup();
    stubApi();
    renderApp("/site-servers/ss-1");
    await screen.findByText("Coinciden");
    await user.type(screen.getByLabelText("MAC de destino"), "a4:bb:6d:11:22:33");
    await user.selectOptions(screen.getByLabelText("Segmento (obligatorio, este servidor declara varios)"), "10.20.4.0/24");
    await user.click(screen.getByRole("button", { name: "Enviar encendido" }));
    expect(await screen.findByText(/Encendido en cola para rack-b4-relay/)).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByText(/Wake queued for rack-b4-relay/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send wake" })).toBeInTheDocument();
  });
});
