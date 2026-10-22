/**
 * Licenses in Spanish: the register with its tiles and New license dialog, one
 * license (details, allocations, the audited key reveal and the delete), the
 * compliance and renewal tables, the review queue with its map and ignore
 * dialogs, the product catalog and editor, vendors, the readiness panel and
 * the labels built outside React. Product, vendor and publisher names, PO
 * references and the fixture key are recorded data and read as recorded.
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
import type { ApiClient, Me } from "../../lib/api";
import { ComplianceDashboard } from "./ComplianceDashboard";
import { LICENSE_NAV_ITEMS } from "./licenseNav";
import { LicenseDetail } from "./LicenseDetail";
import { LicenseReadiness } from "./LicenseReadiness";
import { LicensesPage } from "./LicensesPage";
import {
  PRODUCT_PICKER_EMPTY_HINT,
  productPickerEmptyHint,
  productPickerOptions,
  vendorPickerEmptyHint,
  vendorPickerOptions,
} from "./pickerOptions";
import { ProductsPage } from "./ProductsPage";
import { RenewalCalendar } from "./RenewalCalendar";
import { ReviewQueue } from "./ReviewQueue";
import { VendorsPage } from "./VendorsPage";
import type { AllocationOut, ComplianceRow, LicenseOut, ProductOut, RenewalRow, ReviewQueueRow, VendorOut } from "./types";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function mount(ui: ReactElement, route = "/") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderInSpanish(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const ME: Me = {
  user_id: "u1",
  org_id: "o1",
  email: "operator@example.com",
  display_name: "Operator",
  permissions: ["licenses.read", "licenses.manage", "licenses.keys.read"],
  timezone: "UTC",
};

/** Recorded data the pages print as it is. */
const RECORDED = ["Acme Software", "Acme Office Suite", "Visio", "Microsoft", "PO-1", "ABCD-1234", "https://acme.example"];

const VENDORS: VendorOut[] = [
  { id: "v1", name: "Acme Software", website: "https://acme.example", contacts: [], created_at: "2026-01-01T00:00:00.000Z" },
];
const PRODUCTS: ProductOut[] = [
  {
    id: "p1",
    name: "Acme Office Suite",
    publisher: "Acme Software",
    match_rules: [
      { type: "exact", pattern: "Acme Office Suite", publisher: "Acme Software" },
      { type: "prefix", pattern: "Acme Office", publisher: null },
    ],
    created_at: "2026-01-01T00:00:00.000Z",
  },
];
const LICENSE: LicenseOut = {
  id: "l1",
  product_id: "p1",
  vendor_id: "v1",
  type: "per_device",
  entitlements: 50,
  has_key: true,
  cost: "1200.00",
  currency: "USD",
  po_ref: "PO-1",
  purchased_at: "2025-01-01",
  maintenance_expires_at: "2026-12-31",
  renews_at: "2026-08-01",
  agreement_ref: null,
  notes: "",
  created_at: "2026-01-01T00:00:00.000Z",
};
const COMPLIANCE: ComplianceRow[] = [
  { product_id: "p1", name: "Acme Office Suite", entitled: 5, installed: 8, delta: -3, breach: true },
  { product_id: "p2", name: "Visio", entitled: 5, installed: 1, delta: 4, breach: false },
];
const RENEWALS: RenewalRow[] = [
  { license_id: "l1", product_id: "p1", trigger: "renewal", due_date: "2026-08-01", due_in_days: -3 },
  { license_id: "l2", product_id: "p1", trigger: "maintenance", due_date: "2026-12-31", due_in_days: 20 },
];
const QUEUE_ROW: ReviewQueueRow = { name: "Visio", publisher: "Microsoft", device_count: 3 };

function api(overrides: Partial<ApiClient> = {}, me: Me = ME): Partial<ApiClient> {
  return {
    me: vi.fn().mockResolvedValue(me),
    listLicenses: vi.fn().mockResolvedValue([LICENSE]),
    listProducts: vi.fn().mockResolvedValue(PRODUCTS),
    listVendors: vi.fn().mockResolvedValue(VENDORS),
    licenseCompliance: vi.fn().mockResolvedValue(COMPLIANCE),
    licenseRenewals: vi.fn().mockResolvedValue(RENEWALS),
    reviewQueue: vi.fn().mockResolvedValue([QUEUE_ROW]),
    getLicense: vi.fn().mockResolvedValue(LICENSE),
    getProduct: vi.fn().mockResolvedValue(PRODUCTS[0]),
    listAllocations: vi.fn().mockResolvedValue([]),
    listUsers: vi.fn().mockResolvedValue({ users: [] }),
    revealLicenseKey: vi.fn().mockResolvedValue({ key: "ABCD-1234" }),
    ...overrides,
  };
}

beforeEach(() => {
  mockUseApi.mockReturnValue(api());
});

describe("the licenses register in Spanish", () => {
  it("words the tiles, the filter and the table", async () => {
    const { container } = mount(<LicensesPage />);
    expect(await screen.findByRole("link", { name: "Acme Office Suite" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Licencias" })).toBeInTheDocument();
    expect(screen.getByText("Renovaciones en 90 días")).toBeInTheDocument();
    expect(screen.getByText("Productos con licencia")).toBeInTheDocument();
    expect(screen.getByText("de 1 fabricante")).toBeInTheDocument();
    expect(screen.getByText("de 2 productos conciliados")).toBeInTheDocument();
    expect(screen.getByText("Productos excedidos")).toBeInTheDocument();
    expect(screen.getByText("Por dispositivo")).toBeInTheDocument();
    expect(screen.getByText("Con clave")).toBeInTheDocument();
    expect(screen.getByText("1 de 1")).toBeInTheDocument();
    expect(screen.getByLabelText("Buscar licencias")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Cola de revisión" })).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });

  it("finds a row by the type as it is worded on screen", async () => {
    const user = userEvent.setup();
    mount(<LicensesPage />);
    await screen.findByRole("link", { name: "Acme Office Suite" });
    await user.type(screen.getByLabelText("Buscar licencias"), "por dispositivo");
    expect(screen.getByRole("link", { name: "Acme Office Suite" })).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Buscar licencias"));
    await user.type(screen.getByLabelText("Buscar licencias"), "perpetua");
    expect(screen.queryByRole("link", { name: "Acme Office Suite" })).toBeNull();
    expect(screen.getByText("Ninguna licencia coincide con ese filtro.")).toBeInTheDocument();
  });

  it("says a failed tile query as n/d and the empty states in Spanish", async () => {
    mockUseApi.mockReturnValue(
      api({
        licenseCompliance: vi.fn().mockRejectedValue(new ApiError(503, "")),
        listLicenses: vi.fn().mockResolvedValue([]),
      }),
    );
    const { container } = mount(<LicensesPage />);
    expect(await screen.findByText("Aún no hay licencias.")).toBeInTheDocument();
    expect((await screen.findAllByText("n/d")).length).toBeGreaterThan(0);
    expectNoEnglish(container, RECORDED);
  });

  it("opens New license, words a refused submit and re-words it on a live switch", async () => {
    const user = userEvent.setup();
    mount(<LicensesPage />);
    await screen.findByRole("link", { name: "Acme Office Suite" });
    await user.click(screen.getByRole("button", { name: "Nueva licencia" }));
    const dialog = await screen.findByRole("dialog");
    for (const label of ["Proveedor (opcional)", "Tipo", "Derechos de uso", "Costo (opcional)", "Moneda", "Clave (opcional)"]) {
      expect(within(dialog).getByText(label)).toBeInTheDocument();
    }
    expect(within(dialog).getByRole("button", { name: "Mostrar clave" })).toBeInTheDocument();
    // The mask stays: the key field is a password input until revealed.
    expect(dialog.querySelector('input[type="password"]')).not.toBeNull();
    expect(within(dialog).getByRole("option", { name: "Por usuario" })).toBeInTheDocument();
    expectNoEnglish(dialog, RECORDED);

    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert.textContent).toMatch(/^El campo Producto es obligatorio/);
    act(() => setActiveLanguage("en"));
    expect(within(dialog).getByRole("alert").textContent).toMatch(/^Product is required/);
    act(() => setActiveLanguage("es"));

    // A bad entitlement count and a bad cost name their own rule, in Spanish.
    const entitlements = within(dialog).getByLabelText("Derechos de uso");
    await user.clear(entitlements);
    await user.type(entitlements, "0");
    await user.type(within(dialog).getByLabelText("Costo (opcional)"), "1,200");
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    const refused = (await within(dialog).findByRole("alert")).textContent ?? "";
    expect(refused).toContain("Los derechos de uso deben ser un número entero de al menos 1");
    expect(refused).toContain("El costo debe ser un importe como 1200.00");
    expect(refused).toContain("El campo Moneda es obligatorio");
    act(() => setActiveLanguage("en"));
    expect(within(dialog).getByRole("alert").textContent).toContain("Entitlements must be a whole number of at least 1");
  });

  it("words a failed save from the held error and re-words it on a switch", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api({ createLicense: vi.fn().mockRejectedValue(new ApiError(503, "")) }));
    mount(<LicensesPage />);
    await screen.findByRole("link", { name: "Acme Office Suite" });
    await user.click(screen.getByRole("button", { name: "Nueva licencia" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("combobox", { name: "Producto" }));
    const list = await screen.findByRole("listbox", { name: "Producto" });
    await user.click(await within(list).findByRole("option", { name: /Acme Office Suite/ }));
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert.textContent).toMatch(/no está disponible/i);
    act(() => setActiveLanguage("en"));
    expect(within(dialog).getByRole("alert").textContent).toMatch(/unavailable/i);
  });

  it("points an empty catalog and an empty vendor list at the screens that fill them", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api({ listProducts: vi.fn().mockResolvedValue([]), listVendors: vi.fn().mockResolvedValue([]) }));
    mount(<LicensesPage />);
    await screen.findByText("Aún no hay licencias.", {}, { timeout: 3000 }).catch(() => undefined);
    await user.click(await screen.findByRole("button", { name: "Nueva licencia" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByRole("link", { name: "Añade un producto al catálogo" })).toHaveAttribute("href", "/licenses/products");
    expect(within(dialog).getByRole("link", { name: "Añade un proveedor" })).toHaveAttribute("href", "/licenses/vendors");
    expectNoEnglish(dialog, RECORDED);
  });

  it("re-words the page on a live switch", async () => {
    mount(<LicensesPage />);
    await screen.findByRole("link", { name: "Acme Office Suite" });
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("Renewals in 90 days")).toBeInTheDocument();
    expect(screen.getByText("across 1 publisher")).toBeInTheDocument();
    expect(screen.getByText("Per device")).toBeInTheDocument();
    expect(screen.getByText("Has key")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Review queue" })).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    expect(screen.getByText("Por dispositivo")).toBeInTheDocument();
  });
});

describe("one license in Spanish", () => {
  const ALLOCATIONS: AllocationOut[] = [
    { id: "a1", license_id: "l1", target_type: "device", target_id: "d1", allocated_at: "2026-01-01T23:00:00.000Z", released_at: null },
    { id: "a2", license_id: "l1", target_type: "user", target_id: "u9", person_id: "pe1", allocated_at: "2026-01-02T09:00:00.000Z", released_at: null },
  ];

  function mountDetail() {
    return mount(
      <Routes>
        <Route path="/licenses/:licenseId" element={<LicenseDetail />} />
      </Routes>,
      "/licenses/l1",
    );
  }

  it("words the details and the allocations, and keeps the key out of the page", async () => {
    mockUseApi.mockReturnValue(api({ listAllocations: vi.fn().mockResolvedValue(ALLOCATIONS) }));
    const { container } = mountDetail();
    expect(await screen.findByRole("heading", { name: "Acme Office Suite" })).toBeInTheDocument();
    for (const label of ["Derechos de uso", "Costo", "Ref. de compra", "Fecha de compra", "Vence el mantenimiento", "Renueva", "Ref. de contrato"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    expect(screen.getByText("Sin referencia de contrato")).toBeInTheDocument();
    expect(await screen.findByText("Dispositivo")).toBeInTheDocument();
    expect(screen.getByText("Ver persona")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Mostrar clave" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Eliminar licencia" })).toBeInTheDocument();
    // The stored key is never on the page until it is revealed.
    expect(container.textContent).not.toContain("ABCD-1234");
    expectNoEnglish(container, RECORDED);
  });

  it("shows the revealed key once and words the copy outcomes from held data", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValueOnce(undefined).mockRejectedValueOnce(new ApiError(503, ""));
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    mountDetail();
    await screen.findByRole("heading", { name: "Acme Office Suite" });
    await user.click(screen.getByRole("button", { name: "Mostrar clave" }));
    const dialog = await screen.findByRole("dialog", { name: "Clave de licencia" });
    expect(within(dialog).getByText("ABCD-1234")).toBeInTheDocument();
    expect(within(dialog).getByText("Se muestra una sola vez. Cópiala ahora: no volverá a mostrarse.")).toBeInTheDocument();
    expectNoEnglish(dialog, RECORDED);

    await user.click(within(dialog).getByRole("button", { name: "Copiar" }));
    expect(await within(dialog).findByRole("status")).toHaveTextContent("Copiada.");
    act(() => setActiveLanguage("en"));
    expect(within(dialog).getByRole("status")).toHaveTextContent("Copied.");
    act(() => setActiveLanguage("es"));

    await user.click(within(dialog).getByRole("button", { name: "Copiar" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert.textContent).toMatch(/no está disponible/i);
    act(() => setActiveLanguage("en"));
    expect(within(dialog).getByRole("alert").textContent).toMatch(/unavailable/i);
  });

  it("words a clipboard the browser refused outright", async () => {
    const user = userEvent.setup();
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
    mountDetail();
    await screen.findByRole("heading", { name: "Acme Office Suite" });
    await user.click(screen.getByRole("button", { name: "Mostrar clave" }));
    const dialog = await screen.findByRole("dialog", { name: "Clave de licencia" });
    await user.click(within(dialog).getByRole("button", { name: "Copiar" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Este navegador no permitió copiar. Selecciona la clave y cópiala a mano.");
    act(() => setActiveLanguage("en"));
    expect(within(dialog).getByRole("alert")).toHaveTextContent("This browser did not allow the copy.");
  });

  it("words the delete confirmation", async () => {
    const user = userEvent.setup();
    mountDetail();
    await screen.findByRole("heading", { name: "Acme Office Suite" });
    await user.click(screen.getByRole("button", { name: "Eliminar licencia" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Acme Office Suite, 50 derechos de uso")).toBeInTheDocument();
    expect(within(dialog).getByText(/^Elimina el registro y su clave guardada/)).toBeInTheDocument();
    expectNoEnglish(dialog, RECORDED);
  });

  it("words a missing license", async () => {
    mockUseApi.mockReturnValue(api({ getLicense: vi.fn().mockResolvedValue(null) }));
    const { container } = mountDetail();
    expect(await screen.findByText("Licencia no encontrada.")).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });
});

describe("compliance and renewals in Spanish", () => {
  it("words the compliance table, the pills and the bar", async () => {
    const { container } = mount(<ComplianceDashboard />);
    expect(await screen.findByText("Usadas / adquiridas")).toBeInTheDocument();
    expect(screen.getByText("3 de más")).toBeInTheDocument();
    expect(screen.getByText("4 libres")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Acme Office Suite: instaladas 8, autorizadas 5" })).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("3 over")).toBeInTheDocument();
    expect(screen.getByText("4 free")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Acme Office Suite: 8 installed of 5 entitled" })).toBeInTheDocument();
  });

  it("words the empty compliance state and the readiness panel beside it", async () => {
    mockUseApi.mockReturnValue(
      api({
        licenseCompliance: vi.fn().mockResolvedValue([]),
        reviewQueue: vi.fn().mockResolvedValue([QUEUE_ROW, { ...QUEUE_ROW, name: "Project" }]),
        listDevices: vi.fn().mockResolvedValue({ items: [], total: 0, fleet: { total: 4, managed: 3, online: 2 } }),
      }, { ...ME, permissions: [...ME.permissions, "devices.read"] }),
    );
    const { container } = mount(<ComplianceDashboard />);
    expect(await screen.findByText("Aún no hay datos de cumplimiento.")).toBeInTheDocument();
    expect(await screen.findByText("Antes de poder contar el cumplimiento")).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });

  it("words the renewal table in the singular and the plural", async () => {
    mockUseApi.mockReturnValue(
      api({
        licenseRenewals: vi.fn().mockResolvedValue([
          ...RENEWALS,
          { license_id: "l3", product_id: "p1", trigger: "renewal", due_date: "2026-09-01", due_in_days: 1 },
          { license_id: "l4", product_id: "p1", trigger: "renewal", due_date: "2026-07-01", due_in_days: -1 },
        ]),
      }),
    );
    const { container } = mount(<RenewalCalendar />);
    expect(await screen.findByText("Próximas renovaciones")).toBeInTheDocument();
    expect(await screen.findByText("3 días de retraso")).toBeInTheDocument();
    expect(screen.getByText("1 día de retraso")).toBeInTheDocument();
    expect(screen.getByText("20 días")).toBeInTheDocument();
    expect(screen.getByText("1 día")).toBeInTheDocument();
    expect(screen.getByText("Vencimiento del mantenimiento")).toBeInTheDocument();
    expect(screen.getAllByText("Renovación").length).toBeGreaterThan(0);
    expectNoEnglish(container, RECORDED);
  });

  it("words the empty renewal state", async () => {
    mockUseApi.mockReturnValue(api({ licenseRenewals: vi.fn().mockResolvedValue([]) }));
    const { container } = mount(<RenewalCalendar />);
    expect(await screen.findByText("No hay renovaciones próximas.")).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });
});

describe("the review queue in Spanish", () => {
  it("words the table and the two dialogs", async () => {
    const user = userEvent.setup();
    const { container } = mount(<ReviewQueue />);
    expect(await screen.findByText("Visio")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Cola de revisión" })).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);

    await user.click(screen.getByRole("button", { name: "Asociar" }));
    const map = await screen.findByRole("dialog", { name: "Asociar «Visio»" });
    await user.click(within(map).getByRole("button", { name: "Asociar" }));
    expect((await within(map).findByRole("alert")).textContent).toMatch(/^El campo Producto es obligatorio/);
    act(() => setActiveLanguage("en"));
    expect(within(map).getByRole("alert").textContent).toMatch(/^Product is required/);
    act(() => setActiveLanguage("es"));
    expectNoEnglish(map, RECORDED);
    await user.click(within(map).getByRole("button", { name: "Cancelar" }));

    await user.click(screen.getByRole("button", { name: "Ignorar" }));
    const ignore = await screen.findByRole("dialog");
    expect(within(ignore).getByText("Ignorar este título de software")).toBeInTheDocument();
    expect(within(ignore).getByText("Visio (3 dispositivos)")).toBeInTheDocument();
    expectNoEnglish(ignore, RECORDED);
  });

  it("words a refused ignore from the held error", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api({ ignoreSoftware: vi.fn().mockRejectedValue(new ApiError(503, "")) }));
    mount(<ReviewQueue />);
    await user.click(await screen.findByRole("button", { name: "Ignorar" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Ignorar título" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/no está disponible/i);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert").textContent).toMatch(/unavailable/i);
  });

  it("words the one-device subtitle and the empty queue", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api({ reviewQueue: vi.fn().mockResolvedValue([{ ...QUEUE_ROW, device_count: 1 }]) }));
    mount(<ReviewQueue />);
    await user.click(await screen.findByRole("button", { name: "Ignorar" }));
    expect(await within(await screen.findByRole("dialog")).findByText("Visio (1 dispositivo)")).toBeInTheDocument();
  });

  it("words the empty queue", async () => {
    mockUseApi.mockReturnValue(api({ reviewQueue: vi.fn().mockResolvedValue([]) }));
    const { container } = mount(<ReviewQueue />);
    expect(await screen.findByText("Nada que revisar.")).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });
});

describe("the product catalog in Spanish", () => {
  it("words the table, the rule kinds and the counts", async () => {
    const { container } = mount(<ProductsPage />);
    expect(await screen.findByText("Acme Office Suite", { selector: ".cell-title" })).toBeInTheDocument();
    expect(screen.getByText("2 reglas de detección")).toBeInTheDocument();
    expect(screen.getByText(/Exactamente/)).toBeInTheDocument();
    expect(screen.getByText(/Empieza por/)).toBeInTheDocument();
    expect(await screen.findByText("8 instalaciones")).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("2 detection rules")).toBeInTheDocument();
    expect(screen.getByText(/Starts with/)).toBeInTheDocument();
  });

  it("says n/d for a product with no compliance row", async () => {
    mockUseApi.mockReturnValue(api({ licenseCompliance: vi.fn().mockResolvedValue([]) }));
    mount(<ProductsPage />);
    expect(await screen.findByText("n/d")).toBeInTheDocument();
  });

  it("words the editor, its rule rows and a refused save", async () => {
    const user = userEvent.setup();
    mount(<ProductsPage />);
    await screen.findByText("Acme Office Suite", { selector: ".cell-title" });
    await user.click(screen.getByRole("button", { name: "Nuevo producto" }));
    const dialog = await screen.findByRole("dialog", { name: "Nuevo producto" });
    await user.click(within(dialog).getByRole("button", { name: "Añadir regla de coincidencia" }));
    expect(within(dialog).getByRole("group", { name: "Reglas de coincidencia" })).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Tipo de la regla 1")).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Patrón de la regla 1")).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Fabricante de la regla 1")).toBeInTheDocument();
    expect(within(dialog).getByPlaceholderText("patrón")).toBeInTheDocument();
    expect(within(dialog).getByPlaceholderText("fabricante (opcional)")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Quitar" })).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: "Coincide con" })).toBeInTheDocument();
    expectNoEnglish(dialog, RECORDED);
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    expect((await within(dialog).findByRole("alert")).textContent).toMatch(/^El campo Nombre es obligatorio/);
    act(() => setActiveLanguage("en"));
    expect(within(dialog).getByRole("alert").textContent).toMatch(/^Name is required/);
  });

  it("opens an existing product for editing", async () => {
    const user = userEvent.setup();
    mount(<ProductsPage />);
    await user.click(await screen.findByRole("button", { name: "Editar" }));
    expect(await screen.findByRole("dialog", { name: "Editar producto" })).toBeInTheDocument();
  });

  it("words the empty catalog", async () => {
    mockUseApi.mockReturnValue(api({ listProducts: vi.fn().mockResolvedValue([{ ...PRODUCTS[0], publisher: "", match_rules: [] }]) }));
    const { container } = mount(<ProductsPage />);
    expect(await screen.findByText("Sin fabricante")).toBeInTheDocument();
    expect(screen.getByText("Sin reglas de coincidencia")).toBeInTheDocument();
    expect(screen.getByText("0 reglas de detección")).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });
});

describe("vendors in Spanish", () => {
  it("words the table and the count", async () => {
    const { container } = mount(<VendorsPage />);
    expect(await screen.findByText("Acme Software")).toBeInTheDocument();
    expect(await screen.findByText("1 licencia")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Sitio web" })).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });

  it("words the dialog and a refused submit", async () => {
    const user = userEvent.setup();
    mount(<VendorsPage />);
    await user.click(await screen.findByRole("button", { name: "Nuevo proveedor" }));
    const dialog = await screen.findByRole("dialog", { name: "Nuevo proveedor" });
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    expect((await within(dialog).findByRole("alert")).textContent).toMatch(/^El campo Nombre es obligatorio/);
    act(() => setActiveLanguage("en"));
    expect(within(dialog).getByRole("alert").textContent).toMatch(/^Name is required/);
    act(() => setActiveLanguage("es"));
    expectNoEnglish(dialog, RECORDED);
  });

  it("words the empty list and a vendor with no site", async () => {
    mockUseApi.mockReturnValue(api({ listVendors: vi.fn().mockResolvedValue([{ ...VENDORS[0], website: null }]) }));
    mount(<VendorsPage />);
    expect(await screen.findByText("Sin sitio web")).toBeInTheDocument();
  });
});

describe("the readiness panel in Spanish", () => {
  const WITH_DEVICES: Me = { ...ME, permissions: ["licenses.read", "devices.read"] };

  it("words a fleet without the agent and the wait it puts on the mapping row", async () => {
    mockUseApi.mockReturnValue(
      api({ listDevices: vi.fn().mockResolvedValue({ items: [], total: 0, fleet: { total: 0, managed: 0, online: 0 } }) }, WITH_DEVICES),
    );
    const { container } = mount(<LicenseReadiness />);
    expect(await screen.findByText("Primero inventaría el software instalado.")).toBeInTheDocument();
    expect(screen.getByText("El software instalado está inventariado")).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });

  it("words the unmapped count in the singular and the plural, and the done line", async () => {
    mockUseApi.mockReturnValue(
      api({
        listDevices: vi.fn().mockResolvedValue({ items: [], total: 0, fleet: { total: 4, managed: 3, online: 2 } }),
        reviewQueue: vi.fn().mockResolvedValue([QUEUE_ROW]),
      }, WITH_DEVICES),
    );
    const { container } = mount(<LicenseReadiness />);
    expect(await screen.findByText(/^Hay 1 título instalado sin asociar a un producto/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Asociar software instalado" })).toHaveAttribute("href", "/licenses/review-queue");
    expect(screen.getByText("3 equipos informan de su software instalado.")).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
    act(() => setActiveLanguage("en"));
    expect(screen.getByText(/^1 installed title is not mapped to a product/)).toBeInTheDocument();
    expect(screen.getByText("3 computers report its installed software.")).toBeInTheDocument();
  });

  it("says one computer in the singular", async () => {
    mockUseApi.mockReturnValue(
      api({
        listDevices: vi.fn().mockResolvedValue({ items: [], total: 0, fleet: { total: 1, managed: 1, online: 1 } }),
        reviewQueue: vi.fn().mockResolvedValue([QUEUE_ROW, { ...QUEUE_ROW, name: "Project" }]),
      }, WITH_DEVICES),
    );
    mount(<LicenseReadiness />);
    expect(await screen.findByText("1 equipo informa de su software instalado.")).toBeInTheDocument();
    expect(screen.getByText(/^Hay 2 títulos instalados sin asociar/)).toBeInTheDocument();
  });
});

describe("the labels built outside React", () => {
  it("follow the language: the nav strip, the picker options and the hints", async () => {
    setActiveLanguage("es");
    expect(LICENSE_NAV_ITEMS.map((item) => item.label)).toEqual([
      "Licencias",
      "Cumplimiento",
      "Cola de revisión",
      "Renovaciones",
      "Productos",
      "Proveedores",
    ]);
    expect(productPickerOptions([{ ...PRODUCTS[0]!, publisher: "" }])[0]?.sublabel).toBe("sin fabricante registrado");
    expect(vendorPickerOptions([{ ...VENDORS[0]!, website: null }])[0]?.sublabel).toBe("sin sitio web registrado");
    expect(productPickerEmptyHint()).toMatch(/Licencias → Productos/);
    expect(vendorPickerEmptyHint()).toMatch(/Licencias → Proveedores/);
    setActiveLanguage("en");
    expect(LICENSE_NAV_ITEMS[2]?.label).toBe("Review queue");
    expect(productPickerOptions([{ ...PRODUCTS[0]!, publisher: "" }])[0]?.sublabel).toBe("no publisher recorded");
    expect(productPickerEmptyHint()).toBe(PRODUCT_PICKER_EMPTY_HINT);
    await waitFor(() => undefined);
  });
});
