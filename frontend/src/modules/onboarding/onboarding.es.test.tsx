/**
 * The add-a-computer journey in Spanish. The page is routed once (`/onboard`);
 * its five steps are built in `journey.ts`, so the renders below walk the
 * states a Spanish reader meets: an empty deployment, a partly done one, one
 * the operator may not act on, and the `no_agent` sweep.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import type { ApiClient } from "../../lib/api";
import type { ScopeOut, SubnetOut, SweepRunOut } from "../ipam/types";
import { OnboardPage } from "./OnboardPage";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const subnet = (over: Partial<SubnetOut> = {}): SubnetOut => ({
  id: "s1",
  vrf_id: "v1",
  prefix: "10.0.4.0/24",
  kind: "lan",
  name: "oficina",
  description: "",
  site_id: null,
  vlan_id: null,
  gateway: null,
  dns_servers: [],
  parent_id: null,
  sweep_enabled: true,
  sweep_device_id: null,
  sweep_interval_hours: 168,
  last_swept_at: null,
  ...over,
});

const scope = (): ScopeOut => ({
  id: "sc1",
  vrf_id: "v1",
  prefix: "10.0.0.0/16",
  note: "",
  authorized_by: "alguien",
  authorized_at: "2026-08-01T00:00:00Z",
  expires_at: null,
  revoked_at: null,
});

const noAgentRun = (): SweepRunOut => ({
  id: "r1",
  subnet_id: "s1",
  job_id: null,
  device_id: null,
  state: "no_agent",
  trigger: "manual",
  target_count: 0,
  scope_digest: "",
  hosts_found: 0,
  truncated: false,
  started_at: "2026-08-26T11:00:00Z",
  finished_at: null,
  error: "",
});

const ALL = ["devices.read", "ipam.manage", "ipam.scope.manage", "ipam.sweep.run", "agent.release.read"];

function renderPage({
  permissions = ALL,
  subnets = [] as SubnetOut[],
  scopes = [] as ScopeOut[],
  runs = [] as SweepRunOut[],
  candidates = { items: [], total: 0, truncated: false, unsweepable_subnets: 0 },
  releases = [] as unknown[],
}: Record<string, unknown> = {}) {
  mockUseApi.mockReturnValue({
    me: () =>
      Promise.resolve({
        user_id: "u",
        org_id: "o",
        email: "op@example.com",
        display_name: "Operador",
        permissions,
        timezone: null,
      }),
    listIpamSubnets: () => Promise.resolve(subnets),
    listIpamScanAuthorizations: () => Promise.resolve(scopes),
    listIpamSweepRuns: () => Promise.resolve(runs),
    listAssetCandidates: () => Promise.resolve(candidates),
    listAgentReleases: () => Promise.resolve(releases),
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderInSpanish(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <OnboardPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// "Descubiertos" is the Discovered tab's own name, written the same in both languages.
const ALLOW = ["Descubiertos"];

beforeEach(() => {
  mockUseApi.mockReset();
});

describe("the add-a-computer page in Spanish", () => {
  it("shows an empty deployment in Spanish", async () => {
    const { container } = renderPage({ permissions: ["devices.read"] });
    expect(screen.getByRole("heading", { name: "Añadir un equipo" })).toBeInTheDocument();
    expect(await screen.findByText("Registra la red")).toBeInTheDocument();
    expect(await screen.findByText(/No hay ninguna red registrada\./)).toBeInTheDocument();
    expect(screen.getByText("Aún no hay ninguna red registrada. Es el paso 1, arriba.")).toBeInTheDocument();
    expect(screen.getByText("Estás aquí")).toBeInTheDocument();
    expect(screen.getByText(/Muchos equipos en una red: esta plataforma nunca se conecta/)).toBeInTheDocument();
    expect(screen.getAllByText("Bloqueado").length).toBeGreaterThan(0);
    // A step the caller may not perform names the permission, in Spanish.
    expect(
      screen.getByText(/Esto debe hacerlo un administrador\. Requiere permiso para administrar el plan de direcciones IP/),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Abrir la administración de direcciones IP/ })).toBeInTheDocument();
    expectNoEnglish(container, ALLOW);
  });

  it("words a partly done journey with Spanish plurals and chooses a network", async () => {
    const user = userEvent.setup();
    const { container } = renderPage({
      subnets: [subnet(), subnet({ id: "s2", prefix: "10.0.5.0/24", name: "", last_swept_at: "2026-08-26T11:00:00Z" })],
      scopes: [scope()],
      candidates: { items: [], total: 1, truncated: false, unsweepable_subnets: 2 },
      releases: [{ has_artifact: true }],
    });
    expect(await screen.findByText(/2 redes registradas\./)).toBeInTheDocument();
    expect(screen.getByText(/Las 2 redes registradas están autorizadas\./)).toBeInTheDocument();
    expect(screen.getByText(/Se han barrido 1 de 2 redes autorizadas\./)).toBeInTheDocument();
    expect(screen.getByText(/1 dirección respondió y el inventario no la tiene registrada\./)).toBeInTheDocument();
    expect(screen.getByText(/No se pudieron barrer 2 redes en absoluto\./)).toBeInTheDocument();
    expect(screen.getByText(/1 versión lista para instalar\./)).toBeInTheDocument();
    expect(screen.getAllByText("Hecho en parte").length).toBe(2);
    expect(screen.getByText("sin nombre")).toBeInTheDocument();

    const table = screen.getByRole("table");
    expect(within(table).getByRole("columnheader", { name: "Último barrido" })).toBeInTheDocument();
    // "Last swept" is `formatAge` (modules/ipam): a subnet never swept, and one swept long ago.
    expect(within(table).getByText("nunca")).toBeInTheDocument();
    expect(within(table).getByText(/^hace \d+ días$/)).toBeInTheDocument();
    await user.click(within(table).getByRole("button", { name: "Revisar 10.0.4.0/24" }));
    expect(within(table).getByRole("button", { name: "10.0.4.0/24 seleccionada" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Abrir 10.0.4.0/24" })).toBeInTheDocument();
    expectNoEnglish(container, ALLOW);
  });

  it("explains in Spanish a sweep that no agent could perform", async () => {
    const user = userEvent.setup();
    const { container } = renderPage({
      subnets: [subnet()],
      scopes: [scope()],
      runs: [noAgentRun()],
    });
    await screen.findByText(/1 red registrada\./);
    await user.click(await screen.findByRole("button", { name: "Revisar 10.0.4.0/24" }));
    expect(
      await screen.findByText(/El último barrido de 10\.0\.4\.0\/24 no pudo ejecutarse: no hay ningún agente inscrito/),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Obtener un comando de instalación" })).toBeInTheDocument();
    expect(screen.getByText(/Ninguna versión publicada tiene un artefacto cargado\./)).toBeInTheDocument();
    expectNoEnglish(container, ALLOW);
  });
});
