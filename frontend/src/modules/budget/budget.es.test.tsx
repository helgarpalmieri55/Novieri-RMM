/**
 * Budget in Spanish: the dashboard (position tiles, the category bars, the
 * chart, the variance table and the unconvertible notices), the planner
 * (budgets, details, the planned-amount grid and its confirmations), the
 * expense ledger with its dialogs, the confirmation queue, the admin page
 * (settings, categories, cost centers, FX rates) and the readiness panel.
 * Budget, category, cost-center and expense names and references are recorded
 * data and read as recorded; money goes through money.ts, which follows the
 * language, so a million is "1,2 M" here and "1.2m" there.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../../i18n/test-utils";
import { ApiError } from "../../lib/api";
import type { ApiClient, Me } from "../../lib/api";
import { BudgetAdminPage } from "./BudgetAdminPage";
import { BudgetDashboard } from "./BudgetDashboard";
import { BudgetPlannerPage } from "./BudgetPlannerPage";
import { BudgetReadiness } from "./BudgetReadiness";
import { BUDGET_NAV_ITEMS, sourceNote } from "./budgetState";
import { ConfirmationQueuePage } from "./ConfirmationQueuePage";
import { ExpensesPage } from "./ExpensesPage";
import { chartFigure } from "./money";
import {
  CATEGORY_PICKER_EMPTY_HINT,
  categoryPickerEmptyHint,
  categoryPickerOptions,
  costCenterPickerEmptyHint,
  costCenterPickerOptions,
} from "./pickerOptions";
import type { BudgetLineOut, BudgetOut, CategoryOut, CostCenterOut, ExpenseOut, FxRateOut, PositionOut, SettingsOut, VarianceOut } from "./types";

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
  permissions: ["budget.read", "budget.manage", "expense.record", "expense.confirm"],
  timezone: "UTC",
};

/** Recorded data the pages print as it is. */
const RECORDED = ["Presupuesto TI 2026", "Herramientas de oficina", "herramientas", "Operations", "OPS", "Figma renewal", "INV-7", "invoice.pdf"];

const CATEGORY: CategoryOut = {
  id: "cat1",
  client_id: "o1",
  key: "herramientas",
  name: "Herramientas de oficina",
  system_key: "herramientas",
  active: true,
  sort_order: 0,
  created_at: "2026-01-01T00:00:00.000Z",
};
const RETIRED: CategoryOut = { ...CATEGORY, id: "cat2", key: "old_tools", name: "Old tools", system_key: null, active: false };
const COST_CENTER: CostCenterOut = { id: "cc1", client_id: "o1", code: "OPS", name: "Operations", external_ref: null, active: true, created_at: "2026-01-01T00:00:00.000Z" };
const BUDGET: BudgetOut = { id: "b1", client_id: "o1", fiscal_year: 2026, name: "Presupuesto TI 2026", granularity: "month", status: "active", created_at: "2026-01-01T00:00:00.000Z" };
const SETTINGS: SettingsOut = { client_id: "o1", base_currency: "EUR", fiscal_year_start_month: 4, updated_by: null, updated_at: "2026-01-01T00:00:00.000Z" };
const FX: FxRateOut = { id: "fx1", from_currency: "USD", to_currency: "EUR", rate: "0.92000000", effective_date: "2026-01-01", created_at: "2026-01-01T00:00:00.000Z" };
const LINE: BudgetLineOut = { id: "line1", budget_id: "b1", category_id: "cat1", cost_center_id: null, period_index: 1, planned_amount: "500.00", created_at: "2026-01-01T00:00:00.000Z" };
const EXPENSE: ExpenseOut = {
  id: "e1",
  client_id: "o1",
  expense_date: "2026-07-01",
  amount: "1234.56",
  currency: "USD",
  fx_rate: null,
  amount_base: null,
  vendor_id: null,
  category_id: "cat1",
  cost_center_id: null,
  description: "Figma renewal",
  source: "subscription",
  link_type: null,
  link_id: null,
  period_key: null,
  invoice_ref: "INV-7",
  has_attachment: true,
  invoice_filename: "invoice.pdf",
  status: "expected",
  entered_by: "u1",
  created_at: "2026-07-01T00:00:00.000Z",
  confirmed_by: null,
  confirmed_at: null,
  voided_by: null,
  voided_at: null,
};
const POSITION: PositionOut = {
  ytd_actual: "1234567.89",
  pct_of_plan: "45.00",
  avg_monthly_burn: "200.00",
  elapsed_periods: 1,
  projected_year_end: "2400.00",
  unconvertible_count: 2,
  unconvertible_by_currency: { USD: "150.00" },
};
const VARIANCE: VarianceOut = {
  rows: [
    { category_id: "cat1", cost_center_id: null, period_index: 1, planned: "500.00", actual: "650.00", committed: "0.00", projected: "650.00", variance: "150.00", variance_pct: "30.00" },
    { category_id: "cat2", cost_center_id: null, period_index: 1, planned: "100.00", actual: "40.00", committed: "0.00", projected: "40.00", variance: "-60.00", variance_pct: null },
  ],
  unconvertible_count: 2,
  unconvertible_by_currency: { USD: "150.00" },
};

function api(overrides: Partial<ApiClient> = {}, me: Me = ME): Partial<ApiClient> {
  return {
    me: vi.fn().mockResolvedValue(me),
    listBudgets: vi.fn().mockResolvedValue([BUDGET]),
    listBudgetCategories: vi.fn().mockResolvedValue([CATEGORY, RETIRED]),
    listCostCenters: vi.fn().mockResolvedValue([COST_CENTER]),
    listFxRates: vi.fn().mockResolvedValue([FX]),
    listBudgetLines: vi.fn().mockResolvedValue([LINE]),
    listExpenses: vi.fn().mockResolvedValue([EXPENSE]),
    getBudgetSettings: vi.fn().mockResolvedValue(SETTINGS),
    getBudgetPosition: vi.fn().mockResolvedValue(POSITION),
    getBudgetVariance: vi.fn().mockResolvedValue(VARIANCE),
    listSubscriptions: vi.fn().mockResolvedValue([]),
    listLicenses: vi.fn().mockResolvedValue([]),
    ...overrides,
  };
}

beforeEach(() => {
  mockUseApi.mockReturnValue(api());
});

describe("the budget dashboard in Spanish", () => {
  it("words the tiles, the money, the bars, the chart, the notices and the variance table", async () => {
    const { container } = mount(<BudgetDashboard />);
    expect(await screen.findByText("Real en lo que va del año")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Panel del presupuesto" })).toBeInTheDocument();
    // Money follows the language: Spanish compact and grouped figures.
    expect(screen.getByText(/^1,2\s?M\s?€$/)).toBeInTheDocument();
    expect(screen.getByText("1.234.567,89 EUR")).toBeInTheDocument();
    expect(screen.getByText("% del plan")).toBeInTheDocument();
    expect(screen.getByText("1 periodo transcurrido")).toBeInTheDocument();
    expect(screen.getByText("Gasto mensual medio")).toBeInTheDocument();
    expect(screen.getByText("Proyección a fin de año")).toBeInTheDocument();
    expect(await screen.findByText("Por categoría")).toBeInTheDocument();
    expect(screen.getByText("Proyectado frente a lo planificado en el periodo 1, en EUR.")).toBeInTheDocument();
    expect(screen.getByText("+30% sobre el plan")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Herramientas de oficina: 130% del plan, 30% por encima del plan" })).toBeInTheDocument();
    expect(screen.getByText("Plan frente a real")).toBeInTheDocument();
    expect(screen.getByText("Periodo 1")).toBeInTheDocument();
    expect(screen.getByText("Sin porcentaje de variación")).toBeInTheDocument();
    expect(screen.getByText("Variación %")).toBeInTheDocument();
    expect(
      screen.getByText("2 gastos no se pudieron convertir a la moneda base y quedan excluidos de las cifras de posición de arriba."),
    ).toBeInTheDocument();
    expect(screen.getByText("Se excluyen de estas filas de variación los mismos 2 gastos no convertibles. Los importes figuran arriba.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Añadir un tipo de cambio" })).toHaveAttribute("href", "/budget/admin#fx-rates");
    expectNoEnglish(container, RECORDED);
  });

  it("re-words the page on a live switch", async () => {
    mount(<BudgetDashboard />);
    await screen.findByText("Real en lo que va del año");
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("YTD actual")).toBeInTheDocument();
    expect(screen.getByText("€1.2m")).toBeInTheDocument();
    expect(screen.getByText("1,234,567.89 EUR")).toBeInTheDocument();
    expect(screen.getByText("1 period elapsed")).toBeInTheDocument();
    expect(screen.getByText("+30% over plan")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Confirm queue" })).toBeInTheDocument();
    act(() => setActiveLanguage("es"));
    expect(screen.getByRole("tab", { name: "Cola de confirmación" })).toBeInTheDocument();
  });

  it("words the one-expense notice and the first-run state", async () => {
    mockUseApi.mockReturnValue(api({ getBudgetPosition: vi.fn().mockResolvedValue({ ...POSITION, unconvertible_count: 1 }) }));
    mount(<BudgetDashboard />);
    expect(
      await screen.findByText("1 gasto no se pudo convertir a la moneda base y queda excluido de las cifras de posición de arriba."),
    ).toBeInTheDocument();
  });

  it("words the empty state when there is no budget", async () => {
    mockUseApi.mockReturnValue(api({ listBudgets: vi.fn().mockResolvedValue([]) }));
    const { container } = mount(<BudgetDashboard />);
    expect(await screen.findByText("Aún no hay presupuesto")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Crear un presupuesto en el Planificador" })).toHaveAttribute("href", "/budget/planner");
    expectNoEnglish(container, RECORDED);
  });

  it("says n/d when nothing is planned, and words an empty period", async () => {
    mockUseApi.mockReturnValue(
      api({
        getBudgetPosition: vi.fn().mockResolvedValue({ ...POSITION, pct_of_plan: null, unconvertible_count: 0, unconvertible_by_currency: {} }),
        getBudgetVariance: vi.fn().mockResolvedValue({ rows: [], unconvertible_count: 0, unconvertible_by_currency: {} }),
      }),
    );
    const { container } = mount(<BudgetDashboard />);
    expect(await screen.findByText("n/d")).toBeInTheDocument();
    expect(await screen.findByText("Aún no hay importes planificados ni registrados para este periodo.")).toBeInTheDocument();
    expect(screen.getByText("Aún no hay datos de variación.")).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });

  it("words a refused position as a sentence", async () => {
    mockUseApi.mockReturnValue(api({ getBudgetPosition: vi.fn().mockRejectedValue(new ApiError(503, "")) }));
    mount(<BudgetDashboard />);
    expect((await screen.findAllByRole("alert"))[0]!.textContent).toMatch(/no está disponible/i);
  });
});

describe("the budget planner in Spanish", () => {
  it("words the table, the details and the planned-amount grid", async () => {
    const user = userEvent.setup();
    const { container } = mount(<BudgetPlannerPage />);
    expect(await screen.findByRole("heading", { name: "Planificador del presupuesto" })).toBeInTheDocument();
    expect(await screen.findByText("Mensual", { selector: "td" }).catch(() => screen.findByText("Mes", { selector: "td" }))).toBeInTheDocument();
    expect(screen.getByText("Activo", { selector: ".pill, span, td, div" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Presupuesto TI 2026" }));
    expect(await screen.findByText("Detalles del presupuesto")).toBeInTheDocument();
    expect(await screen.findByText("Importes planificados")).toBeInTheDocument();
    expect(screen.getByLabelText("Herramientas de oficina, periodo 1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Eliminar la línea de Herramientas de oficina del periodo 1" })).toBeInTheDocument();
    expect(screen.getByText("Cerrar congela todas las ediciones de líneas y no se puede deshacer.")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Borrador" })).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });

  it("words the grid delete confirmation, with and without an amount", async () => {
    const user = userEvent.setup();
    mount(<BudgetPlannerPage />);
    await user.click(await screen.findByRole("button", { name: "Presupuesto TI 2026" }));
    await user.click(await screen.findByRole("button", { name: "Eliminar la línea de Herramientas de oficina del periodo 1" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Herramientas de oficina, periodo 1, 500.00")).toBeInTheDocument();
    expect(within(dialog).getByText("Eliminar importe planificado")).toBeInTheDocument();
    expectNoEnglish(dialog, RECORDED);
  });

  it("words the close confirmation", async () => {
    const user = userEvent.setup();
    mount(<BudgetPlannerPage />);
    await user.click(await screen.findByRole("button", { name: "Presupuesto TI 2026" }));
    await user.click(await screen.findByRole("button", { name: "Cerrar presupuesto" }));
    const dialog = await screen.findByRole("dialog");
    // Typing to confirm repeats the subject beside the field, so it is on the page twice.
    expect(within(dialog).getAllByText("Presupuesto TI 2026 (año fiscal 2026)").length).toBeGreaterThan(0);
    expect(within(dialog).getByText(/^Congela todos los importes planificados/)).toBeInTheDocument();
    expectNoEnglish(dialog, [...RECORDED, "FY26"]);
  });

  it("words a closed budget", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api({ listBudgets: vi.fn().mockResolvedValue([{ ...BUDGET, status: "closed", granularity: "quarter" }]) }));
    mount(<BudgetPlannerPage />);
    expect(await screen.findByText("Cerrado")).toBeInTheDocument();
    expect(screen.getByText("Trimestre")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Presupuesto TI 2026" }));
    expect(await screen.findByText("Este presupuesto está cerrado. Sus líneas están congeladas y no se puede reabrir.")).toBeInTheDocument();
  });

  it("explains why there is no New budget button without budget.manage", async () => {
    mockUseApi.mockReturnValue(api({}, { ...ME, permissions: ["budget.read"] }));
    const { container } = mount(<BudgetPlannerPage />);
    expect(
      await screen.findByText("Crear o editar un presupuesto requiere permiso para administrar presupuestos. Pídeselo a un administrador si lo necesitas."),
    ).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });

  it("words the New budget dialog, and its refusals re-word on a live switch", async () => {
    const user = userEvent.setup();
    mount(<BudgetPlannerPage />);
    await user.click(await screen.findByRole("button", { name: "Nuevo presupuesto" }));
    const dialog = await screen.findByRole("dialog", { name: "Nuevo presupuesto" });
    expect(within(dialog).getByRole("option", { name: "Trimestre" })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    expect((await within(dialog).findByRole("alert")).textContent).toMatch(/^El campo Nombre es obligatorio/);
    act(() => setActiveLanguage("en"));
    expect(within(dialog).getByRole("alert").textContent).toMatch(/^Name is required/);
    act(() => setActiveLanguage("es"));

    await user.type(within(dialog).getByLabelText("Nombre"), "Plan");
    const year = within(dialog).getByLabelText("Año fiscal");
    await user.clear(year);
    await user.type(year, "-3000");
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    expect((await within(dialog).findByRole("alert")).textContent).toContain("El año fiscal no puede ser negativo; los presupuestos van de 2000 a 2100");
    await user.clear(year);
    await user.type(year, "1999");
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    expect((await within(dialog).findByRole("alert")).textContent).toContain("El año fiscal debe estar entre 2000 y 2100");
  });

  it("words a failed save from the held error", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api({ createBudget: vi.fn().mockRejectedValue(new ApiError(503, "")) }));
    mount(<BudgetPlannerPage />);
    await user.click(await screen.findByRole("button", { name: "Nuevo presupuesto" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Nombre"), "Plan");
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    expect((await within(dialog).findByRole("alert")).textContent).toMatch(/no está disponible/i);
    act(() => setActiveLanguage("en"));
    expect(within(dialog).getByRole("alert").textContent).toMatch(/unavailable/i);
  });

  it("words the empty list", async () => {
    mockUseApi.mockReturnValue(api({ listBudgets: vi.fn().mockResolvedValue([]) }));
    const { container } = mount(<BudgetPlannerPage />);
    expect(await screen.findByText("Aún no hay presupuestos.")).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });
});

describe("the expense ledger in Spanish", () => {
  it("words the filters, the table, the status and source pills and the row controls", async () => {
    const { container } = mount(<ExpensesPage />);
    expect(await screen.findByText("Figma renewal")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Gastos" })).toBeInTheDocument();
    expect(screen.getByLabelText("Filtrar por estado")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Todos los estados" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Anulado" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Todas las categorías" })).toBeInTheDocument();
    expect(screen.getByText("1 gasto")).toBeInTheDocument();
    expect(screen.getByText("Previsto", { selector: ".pill, span" })).toBeInTheDocument();
    expect(screen.getByText("Suscripción", { selector: ".pill, span" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Añadir gastos previstos" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reemplazar adjunto" })).toBeInTheDocument();
    expect(screen.getByLabelText("Adjuntar factura del gasto de Herramientas de oficina del 2026-07-01")).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });

  it("words an expense with no description, no invoice and no attachment", async () => {
    mockUseApi.mockReturnValue(
      api({ listExpenses: vi.fn().mockResolvedValue([{ ...EXPENSE, description: "", invoice_ref: null, has_attachment: false, source: "manual", status: "void" }]) }),
    );
    const { container } = mount(<ExpensesPage />);
    expect(await screen.findByText("sin referencia de factura")).toBeInTheDocument();
    expect(screen.getByText("Sin adjunto")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Adjuntar factura" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Editar" })).toBeNull();
    expectNoEnglish(container, RECORDED);
  });

  it("words the New expense dialog, its refusals, the dated note and a failed save", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api({ createExpense: vi.fn().mockRejectedValue(new ApiError(503, "")) }));
    mount(<ExpensesPage />);
    await screen.findByText("Figma renewal");
    await user.click(screen.getByRole("button", { name: "Nuevo gasto" }));
    const dialog = await screen.findByRole("dialog", { name: "Nuevo gasto" });
    for (const label of ["Fecha del gasto", "Importe", "Moneda", "Descripción", "Referencia de la factura"]) {
      expect(within(dialog).getByText(label)).toBeInTheDocument();
    }
    expect(within(dialog).getByText(/^Con fecha /)).toBeInTheDocument();
    expectNoEnglish(dialog, RECORDED);

    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    const refused = (await within(dialog).findByRole("alert")).textContent ?? "";
    expect(refused).toContain("El campo Categoría es obligatorio");
    act(() => setActiveLanguage("en"));
    expect(within(dialog).getByRole("alert").textContent).toContain("Category is required");
    act(() => setActiveLanguage("es"));

    const amount = within(dialog).getByLabelText("Importe");
    await user.clear(amount);
    await user.type(amount, "1,200");
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    expect((await within(dialog).findByRole("alert")).textContent).toContain("El importe debe ser una cifra como 1200.00");

    await user.clear(amount);
    await user.type(amount, "12.50");
    await user.click(within(dialog).getByRole("combobox", { name: "Categoría" }));
    const list = await screen.findByRole("listbox", { name: "Categoría" });
    await user.click(await within(list).findByRole("option", { name: /Herramientas de oficina/ }));
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    expect((await within(dialog).findByRole("alert")).textContent).toMatch(/no está disponible/i);
    act(() => setActiveLanguage("en"));
    expect(within(dialog).getByRole("alert").textContent).toMatch(/unavailable/i);
  });

  it("points an empty category list and an empty cost-center list at the admin page", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api({ listBudgetCategories: vi.fn().mockResolvedValue([]), listCostCenters: vi.fn().mockResolvedValue([]) }));
    mount(<ExpensesPage />);
    await user.click(await screen.findByRole("button", { name: "Nuevo gasto" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByRole("link", { name: "Crea una categoría de presupuesto" })).toHaveAttribute("href", "/budget/admin");
    expect(within(dialog).getByRole("link", { name: "Crea un centro de costo" })).toBeInTheDocument();
    expectNoEnglish(dialog, RECORDED);
  });

  it("words the edit dialog", async () => {
    const user = userEvent.setup();
    mount(<ExpensesPage />);
    await user.click(await screen.findByRole("button", { name: "Editar" }));
    const dialog = await screen.findByRole("dialog", { name: "Editar gasto" });
    expect(within(dialog).getByLabelText("Referencia de la factura")).toBeInTheDocument();
    expectNoEnglish(dialog, RECORDED);
  });

  it("words the Add expected expenses dialog, its date range and its counts", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api({ materializeExpenses: vi.fn().mockResolvedValue({ created: 3, skipped_existing: 2, skipped_unconvertible: 1 }) }));
    mount(<ExpensesPage />);
    await user.click(await screen.findByRole("button", { name: "Añadir gastos previstos" }));
    const dialog = await screen.findByRole("dialog", { name: "Añadir gastos previstos" });
    expect(within(dialog).getByText(/^Del \d{2} \w+\.? 20\d\d al \d{2} \w+\.? 20\d\d\.$/)).toBeInTheDocument();
    await user.clear(within(dialog).getByLabelText("Fecha de fin"));
    expect(within(dialog).getByText(/^Desde el .*, sin fecha de fin\.$/)).toBeInTheDocument();
    expectNoEnglish(dialog, RECORDED);
    await user.type(within(dialog).getByLabelText("Fecha de fin"), "2026-09-30");
    await user.clear(within(dialog).getByLabelText("Fecha de inicio"));
    expect(within(dialog).getByText(/^Hasta el .*, sin fecha de inicio\.$/)).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Fecha de inicio"), "2026-09-01");
    await user.click(within(dialog).getByRole("button", { name: "Ejecutar" }));
    expect(await screen.findByText("Añadidos: 3. Omitidos por estar ya registrados: 2. Omitidos por no tener tipo de cambio: 1.")).toBeInTheDocument();
    act(() => setActiveLanguage("en"));
    expect(screen.getByText("Added 3. Skipped 2 already recorded and 1 with no exchange rate.")).toBeInTheDocument();
  });

  it("words a refused materialize from the held error", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api({ materializeExpenses: vi.fn().mockRejectedValue(new ApiError(503, "")) }));
    mount(<ExpensesPage />);
    await user.click(await screen.findByRole("button", { name: "Añadir gastos previstos" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Ejecutar" }));
    expect((await within(dialog).findByRole("alert")).textContent).toMatch(/no está disponible/i);
  });

  it("words the empty ledger", async () => {
    mockUseApi.mockReturnValue(api({ listExpenses: vi.fn().mockResolvedValue([]) }));
    const { container } = mount(<ExpensesPage />);
    expect(await screen.findByText("No se encontraron gastos.")).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });
});

describe("the confirmation queue in Spanish", () => {
  it("words the table, the row and the two confirmations", async () => {
    const user = userEvent.setup();
    const { container } = mount(<ConfirmationQueuePage />);
    expect(await screen.findByText("Figma renewal")).toBeInTheDocument();
    expect(screen.getByText("Importe previsto y decisión")).toBeInTheDocument();
    expect(screen.getByText("Herramientas de oficina · de suscripción")).toBeInTheDocument();
    expect(screen.getByLabelText("Importe corregido del gasto Figma renewal del 2026-07-01")).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);

    await user.click(screen.getByRole("button", { name: "Confirmar" }));
    let dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Figma renewal, 1234.56 USD el 01 jul 2026")).toBeInTheDocument();
    expect(within(dialog).getByText("Confirmar este gasto")).toBeInTheDocument();
    expectNoEnglish(dialog, RECORDED);
    await user.click(within(dialog).getByRole("button", { name: "Cancelar" }));

    await user.click(screen.getByRole("button", { name: "Anular" }));
    dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Anular este gasto")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Anular gasto" })).toBeInTheDocument();
    expectNoEnglish(dialog, RECORDED);
  });

  it("words a refused confirm and re-words it on a switch", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api({ confirmExpense: vi.fn().mockRejectedValue(new ApiError(503, "")) }));
    mount(<ConfirmationQueuePage />);
    await user.click(await screen.findByRole("button", { name: "Confirmar" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Confirmar gasto" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/no está disponible/i);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert").textContent).toMatch(/unavailable/i);
  });

  it("words the read-only column header and the empty queue", async () => {
    mockUseApi.mockReturnValue(api({}, { ...ME, permissions: ["budget.read"] }));
    mount(<ConfirmationQueuePage />);
    expect(await screen.findByText("Importe previsto")).toBeInTheDocument();
    mockUseApi.mockReturnValue(api({ listExpenses: vi.fn().mockResolvedValue([]) }));
  });

  it("words the empty queue", async () => {
    mockUseApi.mockReturnValue(api({ listExpenses: vi.fn().mockResolvedValue([]) }));
    const { container } = mount(<ConfirmationQueuePage />);
    expect(await screen.findByText("Nada que confirmar.")).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });
});

describe("the budget admin page in Spanish", () => {
  it("words the settings, the three tables and the month the fiscal year starts", async () => {
    const { container } = mount(<BudgetAdminPage />);
    expect(await screen.findByText("Configuración del presupuesto")).toBeInTheDocument();
    expect(await screen.findByText("1 de abril", { selector: "dd" })).toBeInTheDocument();
    expect(screen.getByText("Sin cambios (1 de abril)")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "1 de diciembre" })).toBeInTheDocument();
    expect(screen.getByText("El año fiscal empieza")).toBeInTheDocument();
    expect(screen.getByText("Nuevo mes de inicio del año fiscal")).toBeInTheDocument();
    expect(await screen.findByText("Categorías")).toBeInTheDocument();
    expect(screen.getByText("Centros de costo")).toBeInTheDocument();
    expect(screen.getByText("Tipos de cambio")).toBeInTheDocument();
    expectNoEnglish(container, RECORDED);
  });

  it("agrees the active pill with the record: a category is feminine, a cost center masculine", async () => {
    mount(<BudgetAdminPage />);
    expect(await screen.findByText("Activa", { selector: ".pill, span" })).toBeInTheDocument();
    expect(await screen.findByText("Inactiva", { selector: ".pill, span" })).toBeInTheDocument();
    expect(screen.getByText("Activo", { selector: ".pill, span" })).toBeInTheDocument();
  });

  it("words the base-currency change confirmation", async () => {
    const user = userEvent.setup();
    mount(<BudgetAdminPage />);
    await user.type(await screen.findByLabelText("Nueva moneda base"), "usd");
    await user.click(screen.getByRole("button", { name: "Guardar configuración" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("EUR a USD")).toBeInTheDocument();
    expect(within(dialog).getByText("Cambiar la moneda base del presupuesto")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Cambiar moneda base" })).toBeInTheDocument();
    expectNoEnglish(dialog, RECORDED);
  });

  it("words the edit row of a category and of a cost center", async () => {
    const user = userEvent.setup();
    mount(<BudgetAdminPage />);
    await screen.findByText("Categorías");
    // The categories are the internal client's, read once `/me` has named it:
    // the first "Editar" is a category's once its row is there.
    await screen.findByText("Herramientas de oficina");
    await user.click((await screen.findAllByRole("button", { name: "Editar" }))[0]!);
    expect(await screen.findByLabelText("Nombre de la categoría")).toBeInTheDocument();
    expect(screen.getByLabelText("Categoría activa")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Guardar" })).toBeInTheDocument();
  });

  it("words the three create dialogs and their refusals, which re-word on a switch", async () => {
    const user = userEvent.setup();
    mount(<BudgetAdminPage />);
    await screen.findByText("Categorías");

    await user.click(await screen.findByRole("button", { name: "Nueva categoría" }));
    let dialog = await screen.findByRole("dialog", { name: "Nueva categoría" });
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    expect((await within(dialog).findByRole("alert")).textContent).toMatch(/^El campo Clave es obligatorio/);
    act(() => setActiveLanguage("en"));
    expect(within(dialog).getByRole("alert").textContent).toMatch(/^Key is required/);
    act(() => setActiveLanguage("es"));
    await user.click(within(dialog).getByRole("button", { name: "Cancelar" }));

    await user.click(await screen.findByRole("button", { name: "Nuevo centro de costo" }));
    dialog = await screen.findByRole("dialog", { name: "Nuevo centro de costo" });
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    expect((await within(dialog).findByRole("alert")).textContent).toMatch(/^El campo Código es obligatorio/);
    await user.click(within(dialog).getByRole("button", { name: "Cancelar" }));

    await user.click(await screen.findByRole("button", { name: "Nuevo tipo de cambio" }));
    dialog = await screen.findByRole("dialog", { name: "Nuevo tipo de cambio" });
    expect(within(dialog).getByText(/^En vigor desde el /)).toBeInTheDocument();
    const rate = within(dialog).getByLabelText("Tipo de cambio");
    await user.clear(rate);
    await user.type(rate, "0");
    await user.type(within(dialog).getByLabelText("Moneda de origen"), "ab");
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));
    const refused = (await within(dialog).findByRole("alert")).textContent ?? "";
    expect(refused).toContain("La moneda de origen debe ser un código de 3 letras, como EUR");
    expect(refused).toContain("El tipo de cambio debe ser un número positivo, como 0.92");
    expectNoEnglish(dialog, RECORDED);
  });

  it("words a refused rename from the held error", async () => {
    const user = userEvent.setup();
    mockUseApi.mockReturnValue(api({ updateBudgetCategory: vi.fn().mockRejectedValue(new ApiError(503, "")) }));
    mount(<BudgetAdminPage />);
    await screen.findByText("Herramientas de oficina");
    await user.click((await screen.findAllByRole("button", { name: "Editar" }))[0]!);
    await user.click(await screen.findByRole("button", { name: "Guardar" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/no está disponible/i);
    act(() => setActiveLanguage("en"));
    expect(screen.getByRole("alert").textContent).toMatch(/unavailable/i);
  });

  it("words the empty tables and a fiscal month the console has no name for", async () => {
    mockUseApi.mockReturnValue(
      api({
        listBudgetCategories: vi.fn().mockResolvedValue([]),
        listCostCenters: vi.fn().mockResolvedValue([]),
        listFxRates: vi.fn().mockResolvedValue([]),
        getBudgetSettings: vi.fn().mockResolvedValue({ ...SETTINGS, fiscal_year_start_month: 13 }),
      }),
    );
    const { container } = mount(<BudgetAdminPage />);
    expect(await screen.findByText("Aún no hay categorías.")).toBeInTheDocument();
    expect(screen.getByText("Aún no hay centros de costo.")).toBeInTheDocument();
    expect(screen.getByText("Aún no hay tipos de cambio.")).toBeInTheDocument();
    expect((await screen.findAllByText("Mes 13", { exact: false })).length).toBeGreaterThan(0);
    expectNoEnglish(container, RECORDED);
  });
});

describe("the readiness panel in Spanish", () => {
  const READ: Me = { ...ME, permissions: ["budget.read", "subscriptions.read", "licenses.read"] };

  it("words the gap and the link that fills it", async () => {
    mockUseApi.mockReturnValue(api({}, READ));
    const { container } = mount(<BudgetReadiness />);
    expect(await screen.findByText("Antes de poder proyectar el gasto previsto")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Registrar suscripciones" })).toHaveAttribute("href", "/subscriptions");
    expectNoEnglish(container, RECORDED);
  });

  it("says the account cannot check it without the read permissions, and shows nothing once it is done", async () => {
    mount(<BudgetReadiness />);
    expect(await screen.findByText(/No se puede comprobar desde esta cuenta/)).toBeInTheDocument();
  });

  it("shows nothing once subscriptions and licenses are recorded", async () => {
    const listSubscriptions = vi.fn().mockResolvedValue([{ id: "s1" }, { id: "s2" }]);
    mockUseApi.mockReturnValue(api({ listSubscriptions, listLicenses: vi.fn().mockResolvedValue([{ id: "l1" }]) }, READ));
    const { container } = mount(<BudgetReadiness />);
    await vi.waitFor(() => expect(listSubscriptions).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(container.textContent).toBe("");
  });
});

describe("the labels built outside React", () => {
  it("follow the language: the nav strip, the picker options, the hints, the source note and the chart figure", () => {
    setActiveLanguage("es");
    expect(BUDGET_NAV_ITEMS.map((item) => item.label)).toEqual(["Panel", "Planificador", "Gastos", "Cola de confirmación", "Administración"]);
    expect(categoryPickerOptions([RETIRED])[0]?.sublabel).toBe("old_tools · retirada");
    expect(costCenterPickerOptions([{ ...COST_CENTER, active: false }])[0]?.sublabel).toBe("OPS · retirado");
    expect(categoryPickerEmptyHint()).toMatch(/Presupuesto → Administración/);
    expect(costCenterPickerEmptyHint()).toMatch(/centros de costo/);
    expect(["manual", "subscription", "license", "import_file"].map(sourceNote)).toEqual([
      "introducido a mano",
      "de suscripción",
      "de licencia",
      "de import file",
    ]);
    expect(chartFigure(1234567.5)).toBe("1.234.567,50");
    setActiveLanguage("en");
    expect(BUDGET_NAV_ITEMS[3]?.label).toBe("Confirm queue");
    expect(categoryPickerOptions([RETIRED])[0]?.sublabel).toBe("old_tools · retired");
    expect(categoryPickerEmptyHint()).toBe(CATEGORY_PICKER_EMPTY_HINT);
    expect(sourceNote("manual")).toBe("entered by hand");
    expect(sourceNote("import_file")).toBe("from import file");
    expect(chartFigure(1234567.5)).toBe("1234567.50");
  });
});
