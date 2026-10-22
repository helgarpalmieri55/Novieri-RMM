/**
 * Devices in Spanish: the fleet list (tiles, tabs, filters, every row state,
 * the push-install selection bar, the review flow and Approve all), the
 * discovered page with its Scan-a-subnet dialog, the suggested-match panel,
 * the add-asset and push-details dialogs, and the device groups console with
 * its rules builder, members dialog and archive refusal. Hostnames, addresses,
 * group names and vendors read as recorded; the words around them are Spanish.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage, translate } from "../../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import { ApiError } from "../../lib/api";
import type {
  ApiClient,
  AssetCandidate,
  AssetSuggestion,
  Device,
  DeviceGroup,
  DeviceGroupReferences,
  DeviceListParams,
  DevicePage,
  Me,
  MembershipEvaluation,
} from "../../lib/api";
import { AddAssetDialog } from "./AddAssetDialog";
import { approveAllMessage } from "./ApproveAllDialog";
import { CandidatesPage } from "./CandidatesPage";
import { DevicesPage } from "./DevicesPage";
import { GroupsPage, previewSentence } from "./GroupsPage";
import { PushDetailsDialog } from "./PushDetailsDialog";
import { SuggestionsPanel } from "./SuggestionsPanel";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
vi.mock("../../lib/ws", () => ({ useLiveEvents: () => undefined }));

const client = () => new QueryClient({ defaultOptions: { queries: { retry: false } } });

// ------------------------------------------------------------------ fixtures

function device(overrides: Partial<Device> = {}): Device {
  return {
    id: "d-1",
    hostname: "ws-alpha",
    fqdn: "ws-alpha.corp.local",
    os_family: "windows",
    os_version: "11 24H2",
    architecture: "amd64",
    device_group_id: null,
    status: "active",
    agent_status: "online",
    last_seen_at: "2026-09-28T09:00:00Z",
    cert_not_after: null,
    cert_state: "ok",
    cert_revoked: false,
    lifecycle: "in_service",
    asset_type: "computer",
    source: "agent",
    provenance: "agent",
    description: null,
    primary_ip: null,
    owner: null,
    location: null,
    review_state: "approved",
    link_joined_from: null,
    link_joined_at: null,
    ...overrides,
  };
}

function devicePage(estate: Device[], params: DeviceListParams = {}): DevicePage {
  const pending = estate.filter((d) => d.review_state === "pending" || d.review_state === "rejected");
  const items = params.state === "review" ? pending : estate;
  return {
    items,
    total: items.length,
    state_counts: { all: estate.length, online: 1, offline: 1, assets: 1, certs: 1, review: pending.length },
    fleet: { total: estate.length, managed: estate.length - 1, online: 1, cert_attention: 1 },
  };
}

const ESTATE: Device[] = [
  device({ assigned_to: { id: "u1", display_name: "Marta Ruiz" } as Device["assigned_to"] }),
  device({ id: "d-2", hostname: "srv-beta", fqdn: null, agent_status: "offline", cert_state: "expiring", os_family: "linux", os_version: "Ubuntu 24.04" }),
  device({
    id: "d-3",
    hostname: "",
    fqdn: null,
    description: "boxed laptop",
    agent_status: null,
    cert_state: null,
    os_family: null,
    architecture: "",
    source: "discovery",
    provenance: "sweep",
    primary_ip: "10.20.4.9",
    asset_type: "printer",
  }),
  device({ id: "d-4", hostname: "lab-pc", agent_status: null, cert_state: null, source: "manual", provenance: "operator", primary_ip: "10.20.4.7" }),
];

const PENDING = device({
  id: "p1",
  hostname: "DESKTOP-7Q2M4KD",
  review_state: "pending",
  link_joined_from: "10.50.0.9",
  link_joined_at: new Date(Date.now() - 35 * 60_000).toISOString(),
});
const REJECTED = device({
  id: "r1",
  hostname: "DESKTOP-OLD",
  review_state: "rejected",
  rejection_outcome: "agent_cannot_uninstall",
  link_joined_from: "10.50.0.9",
  link_joined_at: new Date(Date.now() - 3 * 3_600_000).toISOString(),
});

function renderDevices(estate: Device[], permissions: string[], path = "/devices", extra: Partial<ApiClient> = {}) {
  mockUseApi.mockReturnValue({
    listDevices: vi.fn(async (params: DeviceListParams = {}) => devicePage(estate, params)),
    me: vi.fn().mockResolvedValue({ permissions, timezone: "Europe/Madrid" } as Me),
    ...extra,
  } as unknown as ApiClient);
  return renderInSpanish(
    <QueryClientProvider client={client()}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/devices" element={<DevicesPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const STORED = ["Windows", "Linux", "amd64", "Ubuntu 24.04", "Marta Ruiz"];

beforeEach(() => mockUseApi.mockReset());

// ---------------------------------------------------------------- fleet list

describe("the devices page in Spanish", () => {
  it("words the page head, tiles, tabs, filters, columns and every row state", async () => {
    const { container } = renderDevices(ESTATE, ["devices.read", "people.read", "agent.push.run", "devices.review"]);
    expect(await screen.findByRole("heading", { name: "Dispositivos" })).toBeInTheDocument();
    await screen.findByText("ws-alpha");
    for (const label of ["Añadir activo", "Exportar inventario", "Instalar el agente de forma remota…", "Ejecuciones de instalación remota", "Añadir un equipo"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    const labels = [...document.querySelectorAll(".stat-label")].map((el) => el.textContent);
    expect(labels).toEqual(["Equipos administrados", "Activos", "En línea ahora", "Certificados que requieren atención"]);
    for (const tab of ["Todos los dispositivos", "En línea", "Sin conexión", "Sin agente", "Certificados que requieren atención", "Pendientes de revisión"]) {
      expect(screen.getByRole("tab", { name: new RegExp(tab) })).toBeInTheDocument();
    }
    const table = screen.getByRole("table");
    for (const header of ["Dispositivo", "Agente", "Visto por última vez", "Asignado a", "Certificado", "Sistema operativo", "Arquitectura", "Registrado por", "Acciones"]) {
      expect(within(table).getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    for (const label of ["Introducido a mano", "Encontrado por un barrido", "Inscripción del agente"]) {
      expect(within(table).getAllByText(label).length).toBeGreaterThan(0);
    }
    expect(screen.getByRole("button", { name: "Ejecutar tarea en ws-alpha" })).toBeInTheDocument();
    expect(screen.getByLabelText("Seleccionar lab-pc para la instalación remota")).toBeInTheDocument();
    expect(screen.getByLabelText("Buscar dispositivos")).toBeInTheDocument();
    expect(screen.getByLabelText("Filtrar por cómo se creó el registro")).toBeInTheDocument();
    expect(screen.getByText(/Mostrando 1–4 de 4 dispositivos\./)).toBeInTheDocument();
    expectNoEnglish(container, [...STORED, "boxed laptop", "ws-alpha", "srv-beta", "lab-pc"]);
  });

  it("words the provenance filter options and the search empty state", async () => {
    const user = userEvent.setup();
    const { container } = renderDevices(ESTATE, ["devices.read"]);
    await screen.findByText("ws-alpha");
    const filter = screen.getByLabelText("Filtrar por cómo se creó el registro");
    for (const option of ["Registrado por cualquiera", "Inscripción del agente", "Introducido a mano", "Confirmado desde el descubrimiento", "Encontrado por un barrido"]) {
      expect(within(filter).getByRole("option", { name: option })).toBeInTheDocument();
    }
    expect(screen.getByPlaceholderText("Buscar por nombre, descripción, IP, FQDN, sistema operativo o arquitectura")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Buscar dispositivos"), "zzzz");
    expect(screen.getByRole("button", { name: "Borrar la búsqueda" })).toBeInTheDocument();
    expectNoEnglish(container, STORED);
  });

  it("words the empty states", async () => {
    const { container } = renderDevices([], ["devices.read"]);
    expect(await screen.findByText("Aún no hay dispositivos inscritos.")).toBeInTheDocument();
    expectNoEnglish(container, STORED);
  });

  it("words the push-install selection bar", async () => {
    const user = userEvent.setup();
    const { container } = renderDevices(ESTATE, ["devices.read", "agent.push.run"]);
    await screen.findByText("lab-pc");
    await user.click(screen.getByLabelText("Seleccionar lab-pc para la instalación remota"));
    expect(await screen.findByText(/1 de un máximo de \d+ equipos seleccionado para la instalación remota\./)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Instalar el agente de forma remota… (1)" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Borrar la selección" })).toBeInTheDocument();
    expectNoEnglish(container, [...STORED, "boxed laptop"]);
  });

  it("words the review tab: pending and rejected rows, Approve, Reject and Approve all", async () => {
    const user = userEvent.setup();
    const { container } = renderDevices([PENDING, REJECTED, device()], ["devices.read", "devices.review"], "/devices?state=review", {
      approveDevice: vi.fn(),
      rejectDevice: vi.fn(),
    });
    const row = (await screen.findByText("DESKTOP-7Q2M4KD")).closest("tr")!;
    expect(row).toHaveTextContent("Se unió mediante el enlace de instalación desde 10.50.0.9");
    expect(within(row).getByText("Pendiente de revisión")).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Aprobar DESKTOP-7Q2M4KD" })).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Rechazar DESKTOP-7Q2M4KD" })).toBeInTheDocument();
    const old = screen.getByText("DESKTOP-OLD").closest("tr")!;
    expect(within(old).getByText("Rechazado")).toBeInTheDocument();
    expect(old).toHaveTextContent("Rechazado: este agente es demasiado antiguo para eliminarse solo.");
    expect(within(old).getByRole("button", { name: "Ya quité el agente de DESKTOP-OLD" })).toBeInTheDocument();
    expectNoEnglish(container, [...STORED, "DESKTOP-7Q2M4KD", "DESKTOP-OLD"]);

    await user.click(screen.getByRole("button", { name: "Rechazar DESKTOP-7Q2M4KD" }));
    const dialog = await screen.findByRole("dialog", { name: "Rechazar este equipo" });
    expect(within(dialog).getByRole("button", { name: "Rechazar y desinstalar" })).toBeInTheDocument();
    expectNoEnglish(dialog, ["DESKTOP-7Q2M4KD"]);
    await user.click(within(dialog).getByRole("button", { name: "Cancelar" }));
  });

  it("words the Approve all dialog and its result sentences", async () => {
    const user = userEvent.setup();
    renderDevices([PENDING, device({ id: "p2", hostname: "DESKTOP-2", review_state: "pending", link_joined_from: "10.50.0.10", link_joined_at: PENDING.link_joined_at })], ["devices.read", "devices.review"], "/devices?state=review", {
      approveAllDevices: vi.fn(),
    });
    await user.click(await screen.findByRole("button", { name: "Aprobar todos…" }));
    const dialog = await screen.findByRole("dialog", { name: "Aprobar todos los equipos pendientes" });
    expect(await within(dialog).findByText(/2 equipos están pendientes de revisión\./)).toBeInTheDocument();
    expect(within(dialog).getByRole("list", { name: "Equipos que se van a aprobar" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Aprobar 2 equipos" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Cancelar" })).toBeInTheDocument();
    expectNoEnglish(dialog, ["DESKTOP-7Q2M4KD", "DESKTOP-2", "10.50.0.9", "10.50.0.10"]);
  });

  it("agrees in number in the Approve all result", () => {
    setActiveLanguage("es");
    const text = (approved: number, not_pending: number, still_waiting: number, capped = false) =>
      approveAllMessage({ approved, not_pending, still_waiting } as never, { capped });
    expect(text(1, 0, 0)).toBe("Se aprobó 1 equipo.");
    expect(text(3, 1, 1)).toContain("Se aprobaron 3 equipos.");
    expect(text(3, 2, 0)).toContain("2 equipos de la lista ya no estaban pendientes.");
    expect(text(0, 0, 2)).toContain("siguen pendientes de revisión");
    expect(text(0, 0, 1, true)).toContain("como máximo 1000");
    // An accidental English leftover would show as the English sentence.
    expect(text(2, 0, 0)).not.toMatch(/Approved/);
  });
});

// ---------------------------------------------------------------- dialogs

describe("the asset and push dialogs in Spanish", () => {
  it("words the add-asset dialog, its selects and a failed save", async () => {
    const user = userEvent.setup();
    const createAsset = vi.fn().mockRejectedValue(new ApiError(500, ""));
    mockUseApi.mockReturnValue({ createAsset } as unknown as ApiClient);
    const { baseElement } = renderInSpanish(
      <QueryClientProvider client={client()}>
        <AddAssetDialog open onClose={() => {}} />
      </QueryClientProvider>,
    );
    const dialog = await screen.findByRole("dialog", { name: "Añadir activo" });
    expect(within(dialog).getByText("Un registro sin agente. Todo lo de aquí es opcional.")).toBeInTheDocument();
    for (const label of ["Nombre", "Descripción", "Dirección IP", "Tipo", "Ciclo de vida", "Ubicación"]) {
      expect(within(dialog).getByLabelText(label)).toBeInTheDocument();
    }
    expect(within(dialog).getByRole("option", { name: "impresora" })).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: "En servicio" })).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: "En stock (sin desplegar)" })).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Descripción"), "caja");
    await user.click(within(dialog).getByRole("button", { name: "Añadir activo" }));
    expect(await within(dialog).findByText(/No se añadió el activo\./)).toBeInTheDocument();
    expectNoEnglish(baseElement, ["core-sw-01"]);
  });

  it("words the push-install details dialog", async () => {
    const updateAsset = vi.fn();
    mockUseApi.mockReturnValue({ updateAsset } as unknown as ApiClient);
    const { baseElement } = renderInSpanish(
      <QueryClientProvider client={client()}>
        <PushDetailsDialog device={device({ agent_status: null, fqdn: null })} open onClose={() => {}} />
      </QueryClientProvider>,
    );
    const dialog = await screen.findByRole("dialog", { name: "Datos de la instalación remota" });
    for (const label of ["Nombre DNS", "Sistema operativo", "Procesador"]) {
      expect(within(dialog).getByLabelText(label)).toBeInTheDocument();
    }
    expect(within(dialog).getAllByRole("option", { name: "Sin registrar" }).length).toBe(2);
    expect(within(dialog).getByRole("option", { name: "Intel/AMD de 64 bits" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Guardar" })).toBeInTheDocument();
    expectNoEnglish(baseElement, ["pc1.corp.example.com"]);
  });
});

// ---------------------------------------------------------------- discovery

function candidate(overrides: Partial<AssetCandidate> = {}): AssetCandidate {
  return {
    address: "172.22.104.55",
    mac: "00:50:56:aa:bb:cc",
    vendor: "VMware",
    oui_prefix: "00:50:56",
    hostname: "",
    first_seen_at: "2026-08-10T09:00:00Z",
    last_seen_at: "2026-08-18T09:00:00Z",
    sightings: 12,
    confidence: "medium",
    subnet_id: null,
    ...overrides,
  };
}

function suggestion(): AssetSuggestion {
  return {
    id: "s1",
    basis: "hostname",
    created_at: "2026-08-18T09:00:00Z",
    device: { id: "d-new", hostname: "wks-001", description: null, primary_ip: null, source: "agent", lifecycle: "in_service", last_seen_at: "2026-08-18T09:00:00Z", managed: true },
    candidate: { id: "d-old", hostname: "wks-001", description: "the one somebody typed in", primary_ip: "172.22.104.55", source: "manual", lifecycle: "in_service", last_seen_at: null, managed: false },
  } as AssetSuggestion;
}

const subnet = {
  id: "sub-1",
  vrf_id: "vrf-1",
  prefix: "10.0.4.0/24",
  kind: "network",
  name: "Ops LAN",
  description: "",
  site_id: null,
  vlan_id: null,
  gateway: null,
  dns_servers: [],
  parent_id: null,
  sweep_enabled: true,
  sweep_device_id: "dev-1",
  sweep_interval_hours: 24,
  last_swept_at: null,
};

function renderCandidates(page: unknown, extra: Partial<ApiClient> = {}, permissions: string[] = ["devices.read"]) {
  mockUseApi.mockReturnValue({
    listAssetCandidates: vi.fn().mockResolvedValue(page),
    promoteAssetCandidates: vi.fn().mockResolvedValue({ results: [] }),
    listAssetSuggestions: vi.fn().mockResolvedValue({ items: [suggestion()] }),
    listIpamSubnets: vi.fn().mockResolvedValue([subnet]),
    dispatchIpamSweep: vi.fn().mockResolvedValue({ id: "run-1", state: "queued" }),
    listDevices: vi.fn().mockResolvedValue({ items: [], total: 4, state_counts: {}, fleet: { total: 4, managed: 1, online: 1, cert_attention: 0 } }),
    me: vi.fn().mockResolvedValue({ permissions } as Me),
    ...extra,
  } as unknown as ApiClient);
  return renderInSpanish(
    <QueryClientProvider client={client()}>
      <MemoryRouter>
        <CandidatesPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("the discovered page in Spanish", () => {
  it("words the tiles, the table, the confidence labels and the suggestions panel", async () => {
    const user = userEvent.setup();
    const { container } = renderCandidates(
      {
        items: [candidate(), candidate({ address: "172.22.104.56", mac: null, vendor: null, confidence: "low" }), candidate({ address: "172.22.104.57", confidence: "high" })],
        total: 3,
        truncated: true,
        unsweepable_subnets: 2,
      },
      {},
      ["devices.read", "assets.manage"],
    );
    expect(await screen.findByRole("heading", { name: "Descubiertos en la red" })).toBeInTheDocument();
    await screen.findByText("172.22.104.55");
    expect(screen.getByRole("button", { name: /Escanear una subred/ })).toBeInTheDocument();
    for (const header of ["Dirección", "MAC", "Fabricante", "Evidencia", "Visto"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(screen.getByText("ARP (dirección ↔ MAC)")).toBeInTheDocument();
    expect(screen.getByText("Notificado por un agente")).toBeInTheDocument();
    expect(screen.getByText("Solo ping")).toBeInTheDocument();
    await user.click(screen.getByLabelText("172.22.104.55"));
    expect(screen.getByRole("button", { name: "Promover 1" })).toBeInTheDocument();
    // The suggestions panel mounted under the page.
    expect(await screen.findByText(/2 registros|1 registro/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Son equipos distintos/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Es el mismo equipo/ })).toBeInTheDocument();
    expectNoEnglish(container, ["VMware", "00:50:56:aa:bb:cc", "wks-001", "the one somebody typed in", "ARP", "MAC"]);
  });

  it("words the clean and the nothing-could-look empty states", async () => {
    const { container } = renderCandidates({ items: [], total: 0, truncated: false, unsweepable_subnets: 0 }, { listAssetSuggestions: vi.fn().mockResolvedValue({ items: [] }) });
    expect(await screen.findByText(/No queda nada sin explicar/)).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("words the swept tile for a caller who cannot read the fleet", async () => {
    const { container } = renderCandidates(
      { items: [], total: 0, truncated: false, unsweepable_subnets: 0 },
      { listAssetSuggestions: vi.fn().mockResolvedValue({ items: [] }) },
      [],
    );
    expect(await screen.findByText(/Para contarlos hace falta la lectura de la flota/)).toHaveTextContent(
      "Para contarlos hace falta la lectura de la flota (devices.read), que tu cuenta no tiene",
    );
    expectNoEnglish(container, ["devices"]);
  });

  it("words the Scan-a-subnet dialog through to the queued confirmation", async () => {
    const user = userEvent.setup();
    renderCandidates({ items: [], total: 0, truncated: false, unsweepable_subnets: 0 }, { listAssetSuggestions: vi.fn().mockResolvedValue({ items: [] }) });
    await screen.findByRole("heading", { name: "Descubiertos en la red" });
    await user.click(await screen.findByRole("button", { name: /Escanear una subred/ }));
    const dialog = await screen.findByRole("dialog", { name: "Escanear una subred en busca de dispositivos" });
    await user.selectOptions(await within(dialog).findByRole("combobox"), "sub-1");
    expect(within(dialog).getByText("Esta subred tiene un agente designado. Nunca se ha barrido.")).toBeInTheDocument();
    expectNoEnglish(dialog, ["Ops LAN", "10.0.4.0/24"]);
    await user.click(within(dialog).getByRole("button", { name: "Escanear ahora" }));
    expect(await within(dialog).findByText(/Se puso en cola/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Escanear otra" })).toBeInTheDocument();
    expectNoEnglish(dialog, ["10.0.4.0/24"]);
  });

  it("words a refused sweep rather than leaving it English", async () => {
    const user = userEvent.setup();
    renderCandidates(
      { items: [], total: 0, truncated: false, unsweepable_subnets: 0 },
      { listAssetSuggestions: vi.fn().mockResolvedValue({ items: [] }), dispatchIpamSweep: vi.fn().mockRejectedValue(new ApiError(403, "not authorized")) },
    );
    await user.click(await screen.findByRole("button", { name: /Escanear una subred/ }));
    const dialog = await screen.findByRole("dialog", { name: "Escanear una subred en busca de dispositivos" });
    await user.selectOptions(await within(dialog).findByRole("combobox"), "sub-1");
    await user.click(within(dialog).getByRole("button", { name: "Escanear ahora" }));
    expect(await within(dialog).findByRole("alert")).toBeInTheDocument();
    // "authorized" is the stubbed server's own message ("not authorized"), shown as sent.
    expectNoEnglish(dialog, ["Ops LAN", "10.0.4.0/24", "authorized"]);
  });

  it("words the suggestions panel on its own", async () => {
    mockUseApi.mockReturnValue({ listAssetSuggestions: vi.fn().mockResolvedValue({ items: [suggestion()] }), resolveAssetSuggestion: vi.fn() } as unknown as ApiClient);
    const { container } = renderInSpanish(
      <QueryClientProvider client={client()}>
        <SuggestionsPanel />
      </QueryClientProvider>,
    );
    expect(await screen.findByText("1 registro podría ser el mismo equipo")).toBeInTheDocument();
    expect(screen.getByText("Recién inscrito")).toBeInTheDocument();
    expect(screen.getByText("Ya está en el inventario")).toBeInTheDocument();
    expectNoEnglish(container, ["wks-001", "the one somebody typed in", "172.22.104.55"]);
  });
});

// ------------------------------------------------------------------- groups

function group(overrides: Partial<DeviceGroup> = {}): DeviceGroup {
  return {
    id: "g-1",
    name: "servers",
    parent_group_id: null,
    description: "",
    dynamic: false,
    device_count: 2,
    precedence: 0,
    archived_at: null,
    ...overrides,
  };
}

function evaluated(overrides: Partial<MembershipEvaluation> = {}): MembershipEvaluation {
  return {
    last_evaluated_at: new Date(Date.now() - 4 * 60_000).toISOString(),
    last_full_pass_at: null,
    full_pass: false,
    groups_evaluated: 1,
    devices_evaluated: 3,
    devices_moved: 1,
    devices_without_inventory: 2,
    interval_seconds: 300,
    ...overrides,
  };
}

const references = (over: Partial<DeviceGroupReferences> = {}): DeviceGroupReferences => ({
  devices: 0,
  child_groups: 0,
  policies: 0,
  patch_policies: 0,
  backup_policies: 0,
  remote_policies: 0,
  health_assignments: 0,
  credentials: 0,
  role_grants: 0,
  ...over,
});

function renderGroups(
  groups: DeviceGroup[],
  { evaluation = evaluated(), api = {} as Partial<ApiClient>, permissions = ["devices.read", "devices.manage"] } = {},
) {
  mockUseApi.mockReturnValue({
    listDeviceGroups: vi.fn().mockResolvedValue({ items: groups, total: groups.length, ungrouped_device_count: 5, evaluation }),
    listDevices: vi.fn().mockResolvedValue({ items: [device({ id: "m1", hostname: "ws-01", device_group_id: "g-1" }), device({ id: "m2", hostname: "ws-02" })], total: 2 }),
    getDeviceGroup: vi.fn().mockResolvedValue({ group: groups[0], rules: null, references: references() }),
    previewDeviceGroupRules: vi.fn().mockResolvedValue({
      matched: 1,
      devices_without_inventory: 2,
      sample: [{ device_id: "m1", hostname: "ws-01", current_group_name: null }],
      sample_truncated: true,
    }),
    me: vi.fn().mockResolvedValue({ permissions, timezone: "Europe/Madrid" } as Me),
    ...api,
  } as unknown as ApiClient);
  return renderInSpanish(
    <QueryClientProvider client={client()}>
      <MemoryRouter>
        <GroupsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("the device groups page in Spanish", () => {
  it("words the table, the evaluation sentence and the ungrouped row", async () => {
    const { container } = renderGroups([group(), group({ id: "g-2", name: "web", parent_group_id: "g-1", dynamic: true }), group({ id: "g-3", name: "old", archived_at: "2026-01-01T00:00:00Z" })]);
    expect(await screen.findByRole("heading", { name: "Grupos de dispositivos" })).toBeInTheDocument();
    await screen.findByText("servers");
    for (const header of ["Grupo", "Precedencia", "Dispositivos", "Pertenencia"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(screen.getByText("Sin grupo")).toBeInTheDocument();
    expect(screen.getByText("Se rige por la política predeterminada de la organización")).toBeInTheDocument();
    expect(screen.getByText("Por reglas")).toBeInTheDocument();
    expect(screen.getAllByText("Elegidos a mano").length).toBeGreaterThan(0);
    expect(screen.getByText("Archivado")).toBeInTheDocument();
    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent("La pertenencia se reevaluó por última vez hace 4 minutos: se leyeron los 3 dispositivos cuyo inventario había cambiado, se movió 1. 2 dispositivos aún no tienen inventario y ninguna regla puede reclamarlos.");
    expect(screen.getByRole("button", { name: "Nuevo grupo" })).toBeInTheDocument();
    expectNoEnglish(container, ["servers", "web", "old"]);
  });

  it("words a stale and a never-run evaluation", async () => {
    const stale = renderGroups([group()], { evaluation: evaluated({ last_evaluated_at: new Date(Date.now() - 3 * 3_600_000).toISOString() }) });
    expect(await screen.findByRole("alert")).toHaveTextContent("hace 3 horas, hace más que el intervalo de 5 minutos");
    expectNoEnglish(stale.container, ["servers"]);
    stale.unmount();
    mockUseApi.mockReset();
    const never = renderGroups([group()], { evaluation: null as unknown as MembershipEvaluation });
    expect(await screen.findByRole("alert")).toHaveTextContent("nunca se ha reevaluado");
    expectNoEnglish(never.container, ["servers"]);
  });

  it("words the empty state", async () => {
    const { container } = renderGroups([]);
    expect(await screen.findByText("Aún no hay grupos")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("words the editor, its rules builder and the live preview", async () => {
    const user = userEvent.setup();
    renderGroups([group({ dynamic: false, device_count: 0 })]);
    await user.click(await screen.findByRole("button", { name: "Nuevo grupo" }));
    const dialog = await screen.findByRole("dialog", { name: "Nuevo grupo de dispositivos" });
    for (const label of ["Nombre", "Descripción", "Precedencia", "Pertenencia"]) {
      expect(within(dialog).getAllByText(label).length).toBeGreaterThan(0);
    }
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Pertenencia" }), "dynamic");
    expect(within(dialog).getByText("Dato")).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: "Familia del SO (windows / linux / macos)" })).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: "Software instalado (software:…)" })).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: "mayor que" })).toBeInTheDocument();
    expect(within(dialog).getByText(/cada/)).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Valor"), "windows");
    expect(await within(dialog).findByText("Coincidencias: 1 de 2 dispositivos. 2 dispositivos aún no tienen inventario y no se pueden comparar.")).toBeInTheDocument();
    expect(within(dialog).getByText("Los primeros 1 y dónde está cada uno ahora:")).toBeInTheDocument();
    expect(within(dialog).getByText("ws-01: predeterminado de la organización")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Añadir condición" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Crear grupo" })).toBeInTheDocument();
    expectNoEnglish(dialog, ["ws-01", "windows", "software:", "facts."]);
    // A save with the name empty speaks Spanish too.
    await user.click(within(dialog).getByRole("button", { name: "Crear grupo" }));
    expect(await within(dialog).findAllByText(/obligatorio|Nombre/)).not.toHaveLength(0);
    expectNoEnglish(dialog, ["ws-01", "windows", "software:", "facts."]);
  });

  it("words the members dialog", async () => {
    const user = userEvent.setup();
    renderGroups([group()]);
    await user.click(await screen.findByRole("button", { name: "Miembros" }));
    const dialog = await screen.findByRole("dialog", { name: "Miembros de servers" });
    expect(await within(dialog).findByText("ws-01")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Quitar" })).toBeInTheDocument();
    expect(within(dialog).getByText("Un dispositivo que sale de un grupo no pierde su política: vuelve a la predeterminada de la organización.")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Añadir al grupo" })).toBeInTheDocument();
    expectNoEnglish(dialog, ["ws-01", "ws-02", "servers"]);
  });

  it("words the archive confirmation and the refusal as a to-do list", async () => {
    const user = userEvent.setup();
    const archiveDeviceGroup = vi.fn().mockRejectedValue(
      new ApiError(409, JSON.stringify({ blocked_by: references({ devices: 1, child_groups: 2, patch_policies: 1, role_grants: 3 }) })),
    );
    renderGroups([group()], { api: { archiveDeviceGroup } as unknown as Partial<ApiClient> });
    await user.click(await screen.findByRole("button", { name: "Archivar" }));
    const confirm = await screen.findByRole("dialog", { name: "Archivar grupo de dispositivos" });
    expectNoEnglish(confirm, ["servers"]);
    await user.click(within(confirm).getByRole("button", { name: "Archivar grupo" }));
    const blocked = await screen.findByRole("dialog", { name: "servers sigue en uso" });
    expect(within(blocked).getByText("Este grupo no se puede archivar mientras todo esto siga apuntando a él:")).toBeInTheDocument();
    expect(within(blocked).getByText("1 dispositivo todavía en este grupo")).toBeInTheDocument();
    expect(within(blocked).getByText("2 grupos hijos por debajo")).toBeInTheDocument();
    expect(within(blocked).getByText("1 política de parches")).toBeInTheDocument();
    expect(within(blocked).getByText("3 concesiones de rol limitadas a él")).toBeInTheDocument();
    expectNoEnglish(blocked, ["servers"]);
  });

  it("words the preview sentence the page builds outside React", () => {
    setActiveLanguage("es");
    expect(previewSentence({ matched: 37, devices_without_inventory: 18, sample: [], sample_truncated: true }, 412)).toBe(
      "Coincidencias: 37 de 412 dispositivos. 18 dispositivos aún no tienen inventario y no se pueden comparar.",
    );
    expect(translate("devices.groups.preview.matches", { matched: 1, count: 1 })).toBe("Coincidencias: 1 de 1 dispositivo.");
  });
});

// ------------------------------------------------------------ live language switch

describe("a live language switch on the devices pages", () => {
  it("re-words a mounted fleet list", async () => {
    renderDevices(ESTATE, ["devices.read", "agent.push.run"]);
    expect(await screen.findByRole("heading", { name: "Dispositivos" })).toBeInTheDocument();
    await screen.findByText("ws-alpha");
    expect(screen.getByRole("tab", { name: /Sin conexión/ })).toBeInTheDocument();
    expect(screen.getByText(/Mostrando 1–4 de 4 dispositivos\./)).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("heading", { name: "Devices" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Offline/ })).toBeInTheDocument();
    expect(screen.getByText("Showing 1–4 of 4 devices.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run job on ws-alpha" })).toBeInTheDocument();
    // The provenance words are catalogue lookups, not stored text.
    expect(within(screen.getByRole("table")).getAllByText("Entered by hand").length).toBeGreaterThan(0);
  });

  it("re-words the Approve all result already on screen", async () => {
    const user = userEvent.setup();
    const approveAllDevices = vi.fn().mockResolvedValue({ approved: 2, not_pending: 0, still_waiting: 1 });
    const second = device({ id: "p2", hostname: "DESKTOP-2", review_state: "pending", link_joined_from: "10.50.0.10", link_joined_at: PENDING.link_joined_at });
    renderDevices([PENDING, second], ["devices.read", "devices.review"], "/devices?state=review", { approveAllDevices });
    await user.click(await screen.findByRole("button", { name: "Aprobar todos…" }));
    const dialog = await screen.findByRole("dialog", { name: "Aprobar todos los equipos pendientes" });
    await user.click(await within(dialog).findByRole("button", { name: "Aprobar 2 equipos" }));
    expect(await screen.findByText(/Se aprobaron 2 equipos\./)).toHaveTextContent("1 equipo sigue pendiente de revisión");
    act(() => setActiveLanguage("en"));
    expect(screen.getByText(/Approved 2 machines\./)).toHaveTextContent("1 machine still waits for review");
  });

  it("re-words a refused group save already on screen", async () => {
    const user = userEvent.setup();
    renderGroups([group()]);
    await user.click(await screen.findByRole("button", { name: "Nuevo grupo" }));
    const dialog = await screen.findByRole("dialog", { name: "Nuevo grupo de dispositivos" });
    await user.click(within(dialog).getByRole("button", { name: "Crear grupo" }));
    expect(await within(dialog).findByText(/Nombre es obligatorio|obligatorio/)).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(within(screen.getByRole("dialog", { name: "New device group" })).getByText(/required/i)).toBeInTheDocument();
  });

  it("agrees the partial-promotion sentence with the count that was promoted", () => {
    setActiveLanguage("es");
    const say = (created: number) => translate("devices.candidates.partial", { count: created, created, total: 3, list: "10.0.0.1 (x)" });
    expect(say(1)).toBe("Se promovió 1 de 3. No se promovieron: 10.0.0.1 (x).");
    expect(say(2)).toBe("Se promovieron 2 de 3. No se promovieron: 10.0.0.1 (x).");
    expect(say(0)).toBe("Se promovieron 0 de 3. No se promovieron: 10.0.0.1 (x).");
    setActiveLanguage("en");
    expect(say(1)).toBe("1 of 3 promoted. Not promoted: 10.0.0.1 (x).");
  });
});
