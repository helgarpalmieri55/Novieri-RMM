/**
 * Subscriptions in Spanish: the register with its tiles and New subscription
 * dialog, one subscription (details, renewal terms, seats, the confirmations
 * and the event history), the renewal calendar, the spend roll-ups with idle
 * seats, the readiness panel and the labels built outside React. Service,
 * vendor, plan, category and owner names, assignee names and addresses, and
 * payment references are recorded data and read as recorded; amounts are the
 * server's Decimal strings.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import { ApiError } from "../../lib/api";
import type { ApiClient, Me } from "../../lib/api";
import type { UserListOut } from "../settings/types";
import type { VendorOut } from "../licenses/types";
import {
  OWNER_PICKER_EMPTY_HINT,
  ownerPickerEmptyHint,
  ownerPickerOptions,
  vendorPickerEmptyHint,
  vendorPickerOptions,
} from "./pickerOptions";
import { RenewalCalendar } from "./RenewalCalendar";
import { SpendDashboard } from "./SpendDashboard";
import { SubscriptionDetail } from "./SubscriptionDetail";
import { SubscriptionReadiness } from "./SubscriptionReadiness";
import { billingCycleLabel, billingCyclePhrase } from "./subscriptionState";
import { SubscriptionsPage } from "./SubscriptionsPage";
import type { EventOut, IdleSeatRow, RenewalRow, SeatAssignmentOut, SpendSummary, SubscriptionOut } from "./types";

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
  permissions: ["subscriptions.read", "subscriptions.manage"],
  timezone: "UTC",
};

/** Recorded data the pages print as it is. */
const RECORDED = [
  "Acme SaaS",
  "Globex CRM",
  "Acme Software",
  "Jane Doe",
  "jane@example.com",
  "Priya Ramos",
  "owner@example.com",
  "PO-42",
  "Business",
  "productivity",
  "https://acme.example",
];

const SUB: SubscriptionOut = {
  id: "s1",
  client_id: "o1",
  vendor_id: "v1",
  service_name: "Acme SaaS",
  plan: "Business",
  category: "productivity",
  billing_cycle: "monthly",
  cost_per_cycle: "99.00",
  currency: "USD",
  seats: 10,
  owner_user_id: "u9",
  started_at: "2025-01-01",
  renews_at: "2026-08-01",
  auto_renew: true,
  notice_days: 30,
  payment_ref: "PO-42",
  license_ref: null,
  status: "active",
  created_at: "2026-01-01T00:00:00.000Z",
};
const SEATS: SeatAssignmentOut[] = [
  { id: "seat1", subscription_id: "s1", assignee_name: "Jane Doe", assignee_email: "jane@example.com", assigned_at: "2026-01-02T00:00:00.000Z", released_at: null },
];
const EVENTS: EventOut[] = [
  { id: "e1", subscription_id: "s1", type: "created", details: {}, created_at: "2026-01-01T00:00:00.000Z" },
  { id: "e2", subscription_id: "s1", type: "price_change", details: {}, created_at: "2026-02-01T00:00:00.000Z" },
  { id: "e3", subscription_id: "s1", type: "seat_change", details: {}, created_at: "2026-02-02T00:00:00.000Z" },
  { id: "e4", subscription_id: "s1", type: "renewal", details: {}, created_at: "2026-03-01T00:00:00.000Z" },
  { id: "e5", subscription_id: "s1", type: "cancel", details: {}, created_at: "2026-04-01T00:00:00.000Z" },
];
const USERS: UserListOut = {
  users: [
    { id: "u9", email: "owner@example.com", display_name: "Priya Ramos", status: "active", roles: [] },
    { id: "u8", email: "gone@example.com", display_name: "Ana Gómez", status: "disabled", roles: [] },
    { id: "u7", email: "removed@example.com", display_name: "Luis Pérez", status: "deleted", roles: [] },
  ],
};
const VENDORS: VendorOut[] = [
  { id: "v1", name: "Acme Software", website: "https://acme.example", contacts: [], created_at: "2026-01-01T00:00:00.000Z" },
];
const NEAR: RenewalRow = {
  subscription_id: "s1",
  service_name: "Acme SaaS",
  renews_at: "2026-08-01",
  notice_deadline: "2026-07-02",
  days_to_notice: 0,
  days_to_renewal: 8,
  action_required: true,
};
const PAST: RenewalRow = { ...NEAR, subscription_id: "s3", service_name: "Initech", days_to_renewal: -1, action_required: false };
const FAR: RenewalRow = {
  subscription_id: "s2",
  service_name: "Globex CRM",
  renews_at: "2026-09-20",
  notice_deadline: "2026-08-21",
  days_to_notice: 28,
  days_to_renewal: 1,
  action_required: false,
};
const SPEND: SpendSummary = {
  by_currency: [{ client_id: "o1", currency: "USD", monthly_total: "99.00", annual_total: "1188.00", group_key: null, group_type: null, group_id: null }],
  by_vendor: [
    { client_id: "o1", currency: "USD", monthly_total: "99.00", annual_total: "1188.00", group_key: "Acme Software", group_type: "vendor", group_id: "v1" },
    { client_id: "o1", currency: "USD", monthly_total: "5.00", annual_total: "60.00", group_key: null, group_type: "vendor", group_id: null },
  ],
  by_category: [{ client_id: "o1", currency: "USD", monthly_total: "99.00", annual_total: "1188.00", group_key: "productivity", group_type: "category", group_id: null }],
  by_owner: [],
};
const IDLE: IdleSeatRow[] = [
  { subscription_id: "s1", service_name: "Acme SaaS", seats: 10, assigned: 4, idle: 6, currency: "USD", idle_monthly_cost: "59.40" },
];

function api(overrides: Partial<ApiClient> = {}, me: Me = ME): Partial<ApiClient> {
  return {
    me: vi.fn().mockResolvedValue(me),
    listSubscriptions: vi.fn().mockResolvedValue([SUB]),
    renewals: vi.fn().mockResolvedValue([NEAR, FAR]),
    spend: vi.fn().mockResolvedValue(SPEND),
    idleSeats: vi.fn().mockResolvedValue(IDLE),
    listUsers: vi.fn().mockResolvedValue(USERS),
    listVendors: vi.fn().mockResolvedValue(VENDORS),
    getSubscription: vi.fn().mockResolvedValue(SUB),
    listSeats: vi.fn().mockResolvedValue(SEATS),
    listEvents: vi.fn().mockResolvedValue(EVENTS),
    listUnmatched: vi.fn().mockResolvedValue({ seats: [], devices: [], allocations: [] }),
    listNotificationRules: vi.fn().mockResolvedValue([]),
    ...overrides,
  };
}

beforeEach(() => {
  mockUseApi.mockReturnValue(api());
});

describe("the subscriptions register in Spanish", () => {
  it("words the tiles, the filter and the table", async () => {
    const { container } = mount(<SubscriptionsPage />);
    expect(await screen.findByRole("link", { name: "Acme SaaS" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Suscripciones" })).toBeInTheDocument();
    expect(screen.getByText("Gasto mensual recurrente")).toBeInTheDocument();
    expect(screen.getByText("1188.00 USD al año")).toBeInTheDocument();
    expect(screen.getByText("Suscripciones activas")).toBeInTheDocument();
    expect(screen.getByText("Renovaciones en 90 días")).toBeInTheDocument();
    expect(screen.getByText("Plazos de aviso por vencer")).toBeInTheDocument();
    expect(screen.getByText("Activa")).toBeInTheDocument();
    expect(screen.getByText("Mensual")).toBeInTheDocument();
    expect(screen.getByText("10 puestos · se renueva el 01 ago 2026")).toBeInTheDocument();
    expect(screen.getByText("1 de 1")).toBeInTheDocument();
    expect(screen.getByLabelText("Buscar suscripciones")).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });

  it("says a failed tile query as n/d, counts the one-seat case and words the empty state", async () => {
    mockUseApi.mockReturnValue(
      api({
        renewals: vi.fn().mockRejectedValue(new ApiError(503, "")),
        listSubscriptions: vi.fn().mockResolvedValue([{ ...SUB, seats: 1, renews_at: null, owner_user_id: null, status: "trial" }]),
      }),
    );
    const { container } = mount(<SubscriptionsPage />);
    expect(await screen.findByText("1 puesto")).toBeInTheDocument();
    expect((await screen.findAllByText("n/d")).length).toBeGreaterThan(0);
    expect(screen.getByText("En prueba")).toBeInTheDocument();
    expect(screen.getAllByText("Sin responsable").length).toBeGreaterThan(0);
    expect((await screen.findByRole("alert")).textContent).toMatch(/no está disponible/i);
    expectNoEnglish(container, RECORDED);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert").textContent).toMatch(/unavailable/i);
    expect(screen.getByText("1 seat")).toBeInTheDocument();
  });

  it("finds a row by the cycle as it is worded on screen", async () => {
    const user = userEvent.setup();
    mount(<SubscriptionsPage />);
    await screen.findByRole("link", { name: "Acme SaaS" });
    await user.type(screen.getByLabelText("Buscar suscripciones"), "mensual");
    expect(screen.getByRole("link", { name: "Acme SaaS" })).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Buscar suscripciones"));
    await user.type(screen.getByLabelText("Buscar suscripciones"), "trimestral");
    expect(screen.queryByRole("link", { name: "Acme SaaS" })).toBeNull();
    expect(screen.getByText("Ninguna suscripción coincide con ese filtro.")).toBeInTheDocument();
  });

  it("words the empty register", async () => {
    mockUseApi.mockReturnValue(api({ listSubscriptions: vi.fn().mockResolvedValue([]) }));
    const { container } = mount(<SubscriptionsPage />);
    expect(await screen.findByText("Aún no hay suscripciones.")).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });

  it("names the subscriptions behind a tile, with the overflow count", async () => {
    const many = ["Uno SaaS", "Dos SaaS", "Tres SaaS"].map((service_name, i) => ({ ...SUB, id: `m${i}`, service_name, owner_user_id: null }));
    mockUseApi.mockReturnValue(api({ listSubscriptions: vi.fn().mockResolvedValue(many) }));
    mount(<SubscriptionsPage />);
    expect(await screen.findByText("Uno SaaS, Dos SaaS y 1 más")).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("Uno SaaS, Dos SaaS and 1 more")).toBeInTheDocument();
  });

  it("opens New subscription, words the fields and the note, and a refused submit re-words on a switch", async () => {
    const user = userEvent.setup();
    mount(<SubscriptionsPage />);
    await screen.findByRole("link", { name: "Acme SaaS" });
    await user.click(screen.getByRole("button", { name: "Nueva suscripción" }));
    const dialog = await screen.findByRole("dialog", { name: "Nueva suscripción" });
    for (const label of ["Nombre del servicio", "Ciclo de facturación", "Costo por ciclo", "Moneda", "Puestos", "Fecha de renovación (opcional)", "Plazo de aviso (días)"]) {
      expect(within(dialog).getByText(label)).toBeInTheDocument();
    }
    expect(within(dialog).getByRole("option", { name: "Trimestral" })).toBeInTheDocument();
    expect(within(dialog).getByText(/^Con cuánta antelación a la fecha de renovación hay que cancelar este contrato\. El mosaico «Plazos de aviso por vencer»/)).toBeInTheDocument();
    expect(within(dialog).getByText("Se renueva automáticamente salvo que se cancele")).toBeInTheDocument();
    expectNoEnglish(dialog, RECORDED);

    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    expect((await within(dialog).findByRole("alert")).textContent).toMatch(/^El campo Nombre del servicio es obligatorio/);
    act(() => setActiveLanguage("en"));
    expect(within(dialog).getByRole("alert").textContent).toMatch(/^Service name is required/);
    act(() => setActiveLanguage("es"));

    await user.type(within(dialog).getByLabelText("Nombre del servicio"), "Nuevo");
    const cost = within(dialog).getByLabelText("Costo por ciclo");
    await user.clear(cost);
    await user.type(cost, "12,5");
    const seats = within(dialog).getByLabelText("Puestos");
    await user.clear(seats);
    await user.type(seats, "-4");
    const notice = within(dialog).getByLabelText("Plazo de aviso (días)");
    await user.clear(notice);
    await user.type(notice, "400");
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    const refused = (await within(dialog).findByRole("alert")).textContent ?? "";
    expect(refused).toContain("El costo por ciclo debe ser un importe como 1200.00");
    expect(refused).toContain("Los puestos deben ser un número entero, cero o más");
    expect(refused).toContain("El plazo de aviso debe ser de 365 días o menos");
    act(() => setActiveLanguage("en"));
    expect(within(dialog).getByRole("alert").textContent).toContain("Seats must be a whole number, zero or more");
  });

  it("echoes the renewal date it will record, in Spanish", async () => {
    const user = userEvent.setup();
    mount(<SubscriptionsPage />);
    await screen.findByRole("link", { name: "Acme SaaS" });
    await user.click(screen.getByRole("button", { name: "Nueva suscripción" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Fecha de renovación (opcional)"), "2026-09-22");
    expect(within(dialog).getByText("Se renueva el 22 sept 2026.")).toBeInTheDocument();
  });

  it("words a failed save from the held error", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api({ createSubscription: vi.fn().mockRejectedValue(new ApiError(503, "")) }));
    mount(<SubscriptionsPage />);
    await screen.findByRole("link", { name: "Acme SaaS" });
    await user.click(screen.getByRole("button", { name: "Nueva suscripción" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Nombre del servicio"), "Nuevo");
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    expect((await within(dialog).findByRole("alert")).textContent).toMatch(/no está disponible/i);
    act(() => setActiveLanguage("en"));
    expect(within(dialog).getByRole("alert").textContent).toMatch(/unavailable/i);
  });

  it("points an empty owner list and an empty vendor list at the screens that fill them", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api({ listUsers: vi.fn().mockResolvedValue({ users: [] }), listVendors: vi.fn().mockResolvedValue([]) }));
    mount(<SubscriptionsPage />);
    await screen.findByRole("link", { name: "Acme SaaS" });
    await user.click(screen.getByRole("button", { name: "Nueva suscripción" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByRole("link", { name: "Añade un usuario" })).toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: "Añade un proveedor" })).toHaveAttribute("href", "/licenses/vendors");
    expectNoEnglish(dialog, RECORDED);
  });

  it("re-words the page on a live switch", async () => {
    mount(<SubscriptionsPage />);
    await screen.findByRole("link", { name: "Acme SaaS" });
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("Monthly run rate")).toBeInTheDocument();
    expect(screen.getByText("1188.00 USD a year")).toBeInTheDocument();
    expect(screen.getByText("Notice deadlines due")).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(screen.getByText("Monthly")).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    expect(screen.getByText("Gasto mensual recurrente")).toBeInTheDocument();
  });
});

describe("one subscription in Spanish", () => {
  function mountDetail(route = "/subscriptions/s1") {
    return mount(
      <Routes>
        <Route path="/subscriptions/:subscriptionId" element={<SubscriptionDetail />} />
      </Routes>,
      route,
    );
  }

  it("words the details, the seats, the renewal terms and the history", async () => {
    const { container } = mountDetail();
    expect(await screen.findByRole("heading", { name: "Acme SaaS" })).toBeInTheDocument();
    for (const label of ["Plan", "Categoría", "Costo", "Puestos", "Responsable", "Inicio", "Se renueva", "Días de aviso", "Renovación automática", "Ref. de pago"]) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
    expect(screen.getByText("Sí")).toBeInTheDocument();
    expect(screen.getByText("Facturación mensual")).toBeInTheDocument();
    expect(screen.getByText("Activa")).toBeInTheDocument();
    expect(await screen.findByText("Condiciones de renovación")).toBeInTheDocument();
    expect(screen.getByText("Historial de eventos")).toBeInTheDocument();
    for (const label of ["Creada", "Cambio de precio", "Cambio de puestos", "Renovación", "Cancelación"]) {
      expect(await screen.findByText(label)).toBeInTheDocument();
    }
    expect(screen.getByRole("button", { name: "Registrar renovación" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancelar suscripción" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Asignar puesto" })).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });

  it("words the three confirmations", async () => {
    const user = userEvent.setup();
    mountDetail();
    await screen.findByRole("heading", { name: "Acme SaaS" });

    await user.click(screen.getByRole("button", { name: "Cancelar suscripción" }));
    let dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Acme SaaS, 99.00 USD por ciclo mensual")).toBeInTheDocument();
    expect(within(dialog).getByText(/^Sale de los totales de gasto/)).toBeInTheDocument();
    expectNoEnglish(dialog, RECORDED);
    await user.click(within(dialog).getByRole("button", { name: "Cancelar" }));

    await user.click(screen.getByRole("button", { name: "Registrar renovación" }));
    dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Registrar una renovación")).toBeInTheDocument();
    expectNoEnglish(dialog, RECORDED);
    await user.click(within(dialog).getByRole("button", { name: "Cancelar" }));

    await user.click(screen.getByRole("button", { name: "Liberar" }));
    dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Liberar puesto", { selector: "h2, h3, [role=heading], div, span" })).toBeInTheDocument();
    expect(within(dialog).getByText("Jane Doe")).toBeInTheDocument();
    expectNoEnglish(dialog, RECORDED);
  });

  it("words a refused cancel from the held error and re-words it on a switch", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api({ cancelSubscription: vi.fn().mockRejectedValue(new ApiError(503, "")) }));
    mountDetail();
    await screen.findByRole("heading", { name: "Acme SaaS" });
    await user.click(screen.getByRole("button", { name: "Cancelar suscripción" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Cancelar suscripción" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/no está disponible/i);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert").textContent).toMatch(/unavailable/i);
  });

  it("names the missing seat fields and a bad address, and re-words them on a switch", async () => {
    const user = userEvent.setup();
    mountDetail();
    await screen.findByRole("heading", { name: "Acme SaaS" });
    await user.click(screen.getByRole("button", { name: "Asignar puesto" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/^El campo Nombre de la persona asignada es obligatorio/);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert").textContent).toMatch(/^Assignee name is required/);
    act(() => setActiveLanguage("es"));
    await user.type(screen.getByLabelText("Nombre de la persona asignada"), "Pat");
    await user.type(screen.getByLabelText("Correo de la persona asignada"), "no-es-un-correo");
    await user.click(screen.getByRole("button", { name: "Asignar puesto" }));
    expect((await screen.findByRole("alert")).textContent).toContain("El correo de la persona asignada debe ser una dirección como nombre@ejemplo.com");
  });

  it("words a refused renewal-terms save and a bad notice period", async () => {
    const user = userEvent.setup();
    mountDetail();
    await screen.findByText("Condiciones de renovación");
    const notice = screen.getByLabelText("Plazo de aviso (días)");
    await user.clear(notice);
    await user.type(notice, "2.5");
    await user.click(screen.getByRole("button", { name: "Guardar condiciones de renovación" }));
    expect((await screen.findByRole("alert")).textContent).toContain("El plazo de aviso debe ser un número entero de días, cero o más");
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert").textContent).toContain("Notice period must be a whole number of days, zero or more");
  });

  it("words the saved confirmation of the renewal terms", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api({ updateSubscription: vi.fn().mockResolvedValue(SUB) }));
    mountDetail();
    await screen.findByText("Condiciones de renovación");
    await user.click(screen.getByRole("button", { name: "Guardar condiciones de renovación" }));
    expect(await screen.findByText("Condiciones de renovación guardadas.")).toBeInTheDocument();
  });

  it("words a cancelled subscription, an unknown event and a missing one", async () => {
    mockUseApi.mockReturnValue(
      api({
        getSubscription: vi.fn().mockResolvedValue({ ...SUB, status: "cancelled", billing_cycle: "weekly", owner_user_id: null, plan: "", category: "", payment_ref: null }),
        listEvents: vi.fn().mockResolvedValue([{ ...EVENTS[0]!, type: "contract_signed" }]),
        listSeats: vi.fn().mockResolvedValue([]),
      }),
    );
    const { container } = mountDetail();
    expect(await screen.findByText("Cancelada")).toBeInTheDocument();
    expect(screen.getByText("Facturación weekly")).toBeInTheDocument();
    expect(screen.getByText("Aún no hay puestos asignados.")).toBeInTheDocument();
    expect(screen.getByText("Sin plan registrado")).toBeInTheDocument();
    expect(screen.getByText("Sin referencia de pago")).toBeInTheDocument();
    expect(container.textContent).not.toContain("Condiciones de renovación");
  });

  it("words a subscription that is not found", async () => {
    mockUseApi.mockReturnValue(api({ getSubscription: vi.fn().mockResolvedValue(null) }));
    const { container } = mountDetail();
    expect(await screen.findByText("Suscripción no encontrada.")).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });
});

describe("the renewal calendar in Spanish", () => {
  it("words the window, the summary, the rows and the pills", async () => {
    mockUseApi.mockReturnValue(api({ renewals: vi.fn().mockResolvedValue([NEAR, PAST, FAR]) }));
    const { container } = mount(<RenewalCalendar />);
    expect(await screen.findByText("Próximas renovaciones")).toBeInTheDocument();
    expect(await screen.findByText("3 en la ventana · 1 requiere acción")).toBeInTheDocument();
    expect(screen.getByText("se renueva en 8 días")).toBeInTheDocument();
    expect(screen.getByText("se renueva en 1 día")).toBeInTheDocument();
    expect(screen.getByText("renovada hace 1 día")).toBeInTheDocument();
    expect(screen.getByText("Acción requerida")).toBeInTheDocument();
    expect(screen.getAllByText("En orden").length).toBe(2);
    expect(screen.getByRole("option", { name: "Próximos 60 días" })).toBeInTheDocument();
    expect(screen.getByText("Ventana")).toBeInTheDocument();
    expectNoEnglish(container, [...RECORDED, "Initech"]);
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("3 in window · 1 action required")).toBeInTheDocument();
    expect(screen.getByText("renewed 1 days ago")).toBeInTheDocument();
  });

  it("agrees the action count with the number", async () => {
    mockUseApi.mockReturnValue(api({ renewals: vi.fn().mockResolvedValue([NEAR, { ...NEAR, subscription_id: "s4" }]) }));
    mount(<RenewalCalendar />);
    expect(await screen.findByText("2 en la ventana · 2 requieren acción")).toBeInTheDocument();
  });

  it("words the empty calendar", async () => {
    mockUseApi.mockReturnValue(api({ renewals: vi.fn().mockResolvedValue([]) }));
    const { container } = mount(<RenewalCalendar />);
    expect(await screen.findByText("No hay renovaciones próximas.")).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });
});

describe("the spend roll-ups in Spanish", () => {
  it("words the four roll-ups, an unresolved group and the idle seats", async () => {
    const { container } = mount(<SpendDashboard />);
    expect(await screen.findByText("Gasto en suscripciones", { selector: "h1" })).toBeInTheDocument();
    expect(await screen.findByText("Por moneda")).toBeInTheDocument();
    expect(screen.getByText("Nunca se suma entre clientes ni entre monedas.")).toBeInTheDocument();
    expect(screen.getByText("Todas las suscripciones")).toBeInTheDocument();
    expect(screen.getByText("Sin resolver")).toBeInTheDocument();
    expect(screen.getByText("Por proveedor")).toBeInTheDocument();
    expect(screen.getByText("Por categoría")).toBeInTheDocument();
    expect(screen.getByText("Por responsable")).toBeInTheDocument();
    expect(screen.getAllByText("Total mensual", { selector: "th" }).length).toBe(3);
    expect(await screen.findByText("Puestos sin asignar", { selector: ".panel-title" })).toBeInTheDocument();
    expect(screen.getByText("Costo mensual de puestos sin asignar")).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("Idle seats")).toBeInTheDocument();
    expect(screen.getByText("Not resolved")).toBeInTheDocument();
  });

  it("words the empty states", async () => {
    mockUseApi.mockReturnValue(
      api({ spend: vi.fn().mockResolvedValue({ by_currency: [], by_vendor: [], by_category: [], by_owner: [] }), idleSeats: vi.fn().mockResolvedValue([]) }),
    );
    const { container } = mount(<SpendDashboard />);
    expect(await screen.findByText("No hay puestos sin asignar.")).toBeInTheDocument();
    expect(screen.getAllByText("Aún no hay datos de gasto.").length).toBe(4);
    expectNoEnglish(container, RECORDED);
  });
});

describe("the readiness panel in Spanish", () => {
  const ALL: Me = { ...ME, permissions: ["subscriptions.read", "people.read", "notification.read"] };
  const SEAT = { seat_id: "s1", subscription_id: "sub1", service_name: "Figma", assignee_name: "Pat", assignee_email: "pat@example.com" };

  it("words the unmatched seats in the singular and the reminder gap", async () => {
    mockUseApi.mockReturnValue(api({ listUnmatched: vi.fn().mockResolvedValue({ seats: [SEAT], devices: [], allocations: [] }) }, ALL));
    const { container } = mount(<SubscriptionReadiness />);
    expect(await screen.findByText("Antes de que las suscripciones estén bien controladas")).toBeInTheDocument();
    expect(
      await screen.findByText("1 puesto no está asociado a una persona, así que falta en lo que tiene cada persona y en su baja."),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Asociar titulares de puestos" })).toHaveAttribute("href", "/people/unmatched");
    expect(screen.getByRole("link", { name: "Recibir recordatorios de renovación" })).toBeInTheDocument();
    expectNoEnglish(container, [...RECORDED, "Figma"]);
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("1 seat is not matched to a person, so it is missing from what each person holds and from their offboarding.")).toBeInTheDocument();
  });

  it("words the plural", async () => {
    mockUseApi.mockReturnValue(api({ listUnmatched: vi.fn().mockResolvedValue({ seats: [SEAT, { ...SEAT, seat_id: "s2" }], devices: [], allocations: [] }) }, ALL));
    mount(<SubscriptionReadiness />);
    expect(
      await screen.findByText("2 puestos no están asociados a una persona, así que faltan en lo que tiene cada persona y en su baja."),
    ).toBeInTheDocument();
  });
});

describe("the labels built outside React", () => {
  it("follow the language: the picker options, the hints and the billing words", () => {
    setActiveLanguage("es");
    expect(ownerPickerOptions(USERS.users).map((o) => o.sublabel)).toEqual([
      "owner@example.com",
      "gone@example.com · deshabilitado",
      "removed@example.com · eliminado",
    ]);
    expect(ownerPickerOptions([{ ...USERS.users[0]!, status: "pending_review" }])[0]?.sublabel).toBe("owner@example.com · pending review");
    expect(vendorPickerOptions([{ ...VENDORS[0]!, website: null }])[0]?.sublabel).toBe("sin sitio web registrado");
    expect(ownerPickerEmptyHint()).toMatch(/Configuración → Usuarios/);
    expect(vendorPickerEmptyHint()).toMatch(/Licencias → Proveedores/);
    expect(["monthly", "quarterly", "annual"].map(billingCycleLabel)).toEqual(["Mensual", "Trimestral", "Anual"]);
    expect(["monthly", "quarterly", "annual", "weekly"].map(billingCyclePhrase)).toEqual([
      "Facturación mensual",
      "Facturación trimestral",
      "Facturación anual",
      "Facturación weekly",
    ]);
    setActiveLanguage("en");
    expect(ownerPickerOptions(USERS.users)[1]?.sublabel).toBe("gone@example.com · disabled");
    expect(ownerPickerEmptyHint()).toBe(OWNER_PICKER_EMPTY_HINT);
    expect(billingCyclePhrase("annual")).toBe("Billed annually");
    expect(billingCyclePhrase("weekly")).toBe("Billed weekly");
  });
});
