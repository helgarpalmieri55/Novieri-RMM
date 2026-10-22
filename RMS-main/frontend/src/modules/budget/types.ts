// Mirrors backend/app/modules/budget/schema.py request/response models
// exactly (snake_case keys, matching the JSON). Per invariant #6 (money is
// Decimal server-side): every money field (`planned_amount`, `amount`,
// `amount_base`, `rate`, `planned`, `actual`, `committed`, `projected`,
// `variance`, `variance_pct`, `ytd_actual`, `pct_of_plan`,
// `avg_monthly_burn`, `projected_year_end`) is `Decimal` in schema.py and
// serializes as a JSON string over the wire -- typed `string` / `string |
// null` here, never `number`, and never used for arithmetic in the UI
// (format for display only). Counts (`unconvertible_count`) are genuine
// numbers.
//
// The status/granularity/source vocabularies are sourced from the backend,
// not invented -- see budgetState.ts for the exact citations backing each
// vocabulary.

// schema.py: Granularity = Literal["month", "quarter"].
export type Granularity = "month" | "quarter";

// schema.py: BudgetStatus = Literal["draft", "active", "closed"].
export type BudgetStatus = "draft" | "active" | "closed";

// schema.py: BudgetUpdateStatus = Literal["draft", "active"] -- narrower than
// BudgetStatus: "closed" is only reachable through POST .../close.
export type BudgetUpdateStatus = "draft" | "active";

// schema.py: ExpenseStatus = Literal["expected", "confirmed", "void"].
export type ExpenseStatus = "expected" | "confirmed" | "void";

// schema.py: ExpenseSource = Literal["manual", "subscription", "license"].
export type ExpenseSource = "manual" | "subscription" | "license";

// schema.py: LinkType = Literal["subscription", "license", "device", "change"].
export type LinkType = "subscription" | "license" | "device" | "change";

export interface SettingsUpdate {
  base_currency?: string | null;
  fiscal_year_start_month?: number | null;
  // spec §3: once any non-void expense carries an `amount_base`, changing
  // the base currency is rejected unless this is explicitly true.
  acknowledge_mixed_basis?: boolean;
}

export interface SettingsOut {
  base_currency: string;
  fiscal_year_start_month: number;
  updated_by: string | null;
  updated_at: string;
}

export interface CategoryCreate {
  key: string;
  name: string;
  sort_order?: number;
}

export interface CategoryUpdate {
  name?: string | null;
  active?: boolean | null;
  sort_order?: number | null;
}

export interface CategoryOut {
  id: string;
  key: string;
  name: string;
  system_key: string | null;
  active: boolean;
  sort_order: number;
  created_at: string;
}

export interface CostCenterCreate {
  code: string;
  name: string;
  external_ref?: string | null;
}

export interface CostCenterUpdate {
  name?: string | null;
  external_ref?: string | null;
  active?: boolean | null;
}

export interface CostCenterOut {
  id: string;
  code: string;
  name: string;
  external_ref: string | null;
  active: boolean;
  created_at: string;
}

export interface FxRateCreate {
  from_currency: string;
  // Decimal on the wire; the request body sends a numeric-looking string.
  rate: string;
  effective_date: string;
}

export interface FxRateUpdate {
  rate?: string | null;
}

export interface FxRateOut {
  id: string;
  from_currency: string;
  to_currency: string;
  rate: string;
  effective_date: string;
  created_at: string;
}

export interface BudgetCreate {
  fiscal_year: number;
  name: string;
  granularity: Granularity;
}

export interface BudgetUpdate {
  name?: string | null;
  status?: BudgetUpdateStatus | null;
}

export interface BudgetOut {
  id: string;
  fiscal_year: number;
  name: string;
  // `str`, not `Granularity`/`BudgetStatus`, in schema.py's BudgetOut -- the
  // backend does not re-narrow the type on the way out.
  granularity: string;
  status: string;
  created_at: string;
}

export interface BudgetLineInput {
  category_id: string;
  cost_center_id?: string | null;
  period_index: number;
  planned_amount: string;
}

export interface BudgetLinesPut {
  lines: BudgetLineInput[];
}

export interface BudgetLineOut {
  id: string;
  budget_id: string;
  category_id: string;
  cost_center_id: string | null;
  period_index: number;
  planned_amount: string;
  created_at: string;
}

export interface ExpenseCreate {
  expense_date: string;
  amount: string;
  currency: string;
  category_id: string;
  cost_center_id?: string | null;
  vendor_id?: string | null;
  description?: string;
  invoice_ref?: string | null;
  link_type?: LinkType | null;
  link_id?: string | null;
  // Defaults to "expected" server-side (schema.py's ExpenseCreate) -- a row
  // is never `confirmed` by omission. Callers that want to omit it entirely
  // simply do not set this field.
  status?: "expected" | "confirmed";
}

export interface ExpenseUpdate {
  expense_date?: string | null;
  amount?: string | null;
  currency?: string | null;
  category_id?: string | null;
  cost_center_id?: string | null;
  vendor_id?: string | null;
  description?: string | null;
  invoice_ref?: string | null;
  link_type?: LinkType | null;
  link_id?: string | null;
}

export interface ExpenseConfirm {
  // spec §0.3/§5: the confirmed amount may differ from the projected one;
  // omitting it confirms at the currently-recorded amount.
  amount?: string | null;
}

export interface MaterializeRequest {
  from_date: string;
  to_date: string;
}

export interface ExpenseOut {
  // No `org_id` field here -- security invariant #5.
  id: string;
  expense_date: string;
  amount: string;
  currency: string;
  fx_rate: string | null;
  amount_base: string | null;
  vendor_id: string | null;
  category_id: string;
  cost_center_id: string | null;
  description: string;
  // `str`, not `ExpenseSource`, in schema.py's ExpenseOut.
  source: string;
  link_type: string | null;
  link_id: string | null;
  period_key: string | null;
  invoice_ref: string | null;
  // The raw object-storage key (`invoice_object_key`) is never exposed here
  // -- it leaks the tenant id and bucket name. Only whether an attachment
  // exists and its sanitized display name are ever returned (api.py's
  // `_expense_out`, schema.py's `ExpenseOut`).
  has_attachment: boolean;
  invoice_filename: string | null;
  status: string;
  entered_by: string;
  created_at: string;
  confirmed_by: string | null;
  confirmed_at: string | null;
  voided_by: string | null;
  voided_at: string | null;
}

// GET /budget/budgets/{id}/variance's response shape (api.py's local
// VarianceOut wrapper): rows plus the spec §3 unconvertible disclosure that
// must be rendered, never hidden.
export interface VarianceRow {
  category_id: string;
  cost_center_id: string | null;
  period_index: number;
  // Four distinct columns -- planned, actual, committed, projected -- never
  // collapsed into one "forecast" figure (the Critical bug the backend
  // caught during Task 11).
  planned: string;
  actual: string;
  committed: string;
  projected: string;
  variance: string;
  variance_pct: string | null;
}

export interface VarianceOut {
  rows: VarianceRow[];
  unconvertible_count: number;
  unconvertible_by_currency: Record<string, string>;
}

// GET /budget/budgets/{id}/position's response shape (schema.py's
// PositionOut): year-to-date burn, straight-line projection, and the same
// unconvertible disclosure.
export interface PositionOut {
  ytd_actual: string;
  pct_of_plan: string | null;
  avg_monthly_burn: string;
  elapsed_periods: number;
  projected_year_end: string;
  unconvertible_count: number;
  unconvertible_by_currency: Record<string, string>;
}

// No `ForecastRow`/`ForecastOut` here -- `GET /budget/forecast` has no
// frontend caller (see modules/budget/api.ts's note next to where
// `useBudgetForecast` used to live); the backend route itself is untouched.

// POST /budget/expenses/materialize's response shape (api.py's local
// MaterializeOut wrapper).
export interface MaterializeOut {
  created: number;
  skipped_existing: number;
  skipped_unconvertible: number;
}

// GET /budget/expenses' query filters (api.py's list_expenses_route).
export interface ExpenseListFilters {
  status?: string;
  category_id?: string;
  cost_center_id?: string;
  vendor_id?: string;
  source?: string;
  date_from?: string;
  date_to?: string;
}
