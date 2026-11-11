/**
 * The Clients page in Spanish: the list and every tab of a client's page.
 * Client, site and contact names read as recorded; the words around them are
 * Spanish.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import type { ApiClient, ClientOut, ClientSiteOut, ContactOut, Me, MeClient } from "../../lib/api";
import { ClientDetailPage } from "./ClientDetailPage";
import { ClientsPage } from "./ClientsPage";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

const scope = vi.hoisted(() => ({ clients: [] as MeClient[] }));
vi.mock("../../lib/clientScope", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/clientScope")>()),
  useClientScope: () => ({ selected: null, select: vi.fn(), clients: scope.clients, ready: true }),
}));

const CLIENT: ClientOut = {
  id: "c-andes",
  name: "Transportes Andes",
  kind: "customer",
  status: "active",
  email_domains: ["andes.example"],
  phone: "",
  address: "Calle 80 #10-20",
  website: "",
  notes: "",
  enabled_modules: ["budget"],
  device_count: 3,
  site_count: 2,
};
const SITES: ClientSiteOut[] = [
  { id: "s-main", client_id: "c-andes", name: "Principal", address: "", description: "", egress_networks: [], archived_at: null, device_count: 3 },
  { id: "s-old", client_id: "c-andes", name: "Bodega vieja", address: "", description: "", egress_networks: [], archived_at: "2026-09-01T00:00:00Z", device_count: 0 },
];
const CONTACTS: ContactOut[] = [
  { id: "ct-1", client_id: "c-andes", kind: "contact", email: "ana@andes.example", display_name: "Ana Rojas", phone: null, status: "active", access: "active", invite_expires_at: null },
  { id: "ct-2", client_id: "c-andes", kind: "contact", email: "compras@andes.example", display_name: "Compras", phone: null, status: "disabled", access: "none", invite_expires_at: null },
  // Portal access in every state (own-auth phase 2, Task 11): invited, and none with the act it offers.
  { id: "ct-3", client_id: "c-andes", kind: "contact", email: "luis@andes.example", display_name: "Luis Pardo", phone: null, status: "active", access: "invited", invite_expires_at: "2026-11-09T12:00:00Z" },
  { id: "ct-4", client_id: "c-andes", kind: "contact", email: "marta@andes.example", display_name: "Marta Gil", phone: null, status: "active", access: "none", invite_expires_at: null },
];
const CODES = ["clients.read", "clients.manage", "sites.manage", "health.read", "enrollment.link.manage"];

function renderAt(entry: string) {
  const me: Me = {
    user_id: "u1",
    org_id: "org-1",
    email: "op@example.com",
    display_name: "Operator",
    permissions: CODES,
    permissions_everywhere: CODES,
    clients: [],
  };
  mockUseApi.mockReturnValue({
    me: vi.fn().mockResolvedValue(me),
    listClients: vi.fn().mockResolvedValue([CLIENT, { ...CLIENT, id: "org-1", name: "Toeshee", kind: "internal" }]),
    getClientRecord: vi.fn().mockResolvedValue(CLIENT),
    listClientSites: vi.fn().mockResolvedValue(SITES),
    listContacts: vi.fn().mockResolvedValue(CONTACTS),
    listHealthAssignments: vi.fn().mockResolvedValue([]),
    // The zone an invitation's expiry is shown in (`useOrgZone`).
    getCurrentOrg: vi.fn().mockResolvedValue({ id: "org-1", name: "Toeshee", settings: {} }),
  } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderInSpanish(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/clients" element={<ClientsPage />} />
          <Route path="/clients/:clientId" element={<ClientDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockUseApi.mockReset();
  scope.clients = [
    { id: "c-andes", name: "Transportes Andes", kind: "customer", enabled_modules: ["budget"], permissions: CODES },
  ];
});

describe("Clientes, en español", () => {
  it("words the list", async () => {
    const { container } = renderAt("/clients");
    expect(await screen.findByRole("heading", { name: "Clientes", level: 1 })).toBeInTheDocument();
    expect(await screen.findByText("TI propia")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Nuevo cliente" })).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Mostrar los clientes archivados" })).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it.each([
    ["summary", "Perfil de estado por defecto"],
    ["sites", "Añadir una sede"],
    ["contacts", "Añadir un contacto"],
    ["modules", "Módulos opcionales"],
  ])("words the %s tab", async (tab, text) => {
    const { container } = renderAt(`/clients/c-andes?tab=${tab}`);
    expect(await screen.findByRole("heading", { name: "Transportes Andes", level: 1 })).toBeInTheDocument();
    expect(await screen.findByText(text)).toBeInTheDocument();
    expectNoEnglish(container);
  });
});
