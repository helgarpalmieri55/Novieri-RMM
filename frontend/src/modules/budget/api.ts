// TanStack Query hooks for the Budget module, built on the shared ApiClient
// (useApi()). There is no live budget event stream -- every mutation below
// invalidates the relevant query keys on success so the UI refetches, rather
// than subscribing to a websocket event that doesn't exist for this module
// (mirrors modules/subscriptions/api.ts's rationale -- this module dispatches
// no agent jobs either, per api.py's module docstring: control-plane only,
// no worker).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { UseQueryResult } from "@tanstack/react-query";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { internalClientId, requireKnown } from "../../lib/internalClient";
import type {
  BudgetCreate,
  BudgetLineInput,
  BudgetUpdate,
  CategoryCreate,
  CategoryUpdate,
  CostCenterCreate,
  CostCenterUpdate,
  ExpenseConfirm,
  ExpenseCreate,
  ExpenseListFilters,
  ExpenseUpdate,
  FxRateCreate,
  FxRateUpdate,
  MaterializeRequest,
  NewRow,
  SettingsUpdate,
} from "./types";

// --------------------------------------------------------------------------
// The client these pages work on
// --------------------------------------------------------------------------

/**
 * BUDGET IS ONE CLIENT'S (MSP clients phase 2, plan 2026-10-26 Task 8): its
 * settings, categories, cost centres, budgets and expenses each belong to a
 * client, and budget is a module a client may not use. Every hook below works
 * on the client this returns: reads filter by it (and wait for it --
 * `enabled`), and creates name it.
 *
 * `null` until `/me` has answered. A read is not sent before then; a create
 * throws `InternalScopeUnknown` inside its mutation (`requireKnown`), so the
 * form says why and NOTHING is sent -- a body without its client is a 422
 * the person can do nothing about.
 */
export function useBudgetClient(): string | null {
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  // PHASE 3: the client/site selector. Until it exists that is the
  // organisation's internal client, whose id is the organisation's --
  // `/me`'s `org_id` (`lib/internalClient.ts`).
  return internalClientId(me.data);
}

/**
 * A read that cannot be sent before its client is known is LOADING until
 * then, not empty: a disabled query reports `isLoading: false` with no data,
 * which every panel here would draw as "nothing yet" (or, on the dashboard,
 * as the first-run state) while `/me` is still in flight.
 */
export function loadingUntilClient<R extends UseQueryResult<unknown, unknown>>(
  query: R,
  client: string | null,
): R {
  return client == null && query.isPending ? { ...query, isLoading: true } : query;
}

// --------------------------------------------------------------------------
// Settings -- one client's: its base currency and fiscal year
// --------------------------------------------------------------------------

export function useBudgetSettings() {
  const api = useApi();
  const client = useBudgetClient();
  return loadingUntilClient(
    useQuery({
      queryKey: ["budget-settings", client],
      queryFn: () => api.getBudgetSettings(requireKnown(client)),
      enabled: client != null,
    }),
    client,
  );
}

export function useUpdateBudgetSettingsMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  const client = useBudgetClient();
  return useMutation({
    mutationFn: (body: SettingsUpdate) => api.updateBudgetSettings(requireKnown(client), body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["budget-settings"] });
    },
  });
}

// --------------------------------------------------------------------------
// Categories
// --------------------------------------------------------------------------

export function useBudgetCategories(includeInactive = false) {
  const api = useApi();
  const client = useBudgetClient();
  return loadingUntilClient(
    useQuery({
      queryKey: ["budget-categories", client, includeInactive],
      // Naming the client also seeds its default categories on first use.
      queryFn: () => api.listBudgetCategories(includeInactive, requireKnown(client)),
      enabled: client != null,
    }),
    client,
  );
}

export function useCreateBudgetCategoryMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  const client = useBudgetClient();
  return useMutation({
    mutationFn: (body: NewRow<CategoryCreate>) =>
      // PHASE 3: the selected client; until then the internal one (`useBudgetClient`).
      api.createBudgetCategory({ ...body, client_id: requireKnown(client) }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["budget-categories"] });
    },
  });
}

export function useUpdateBudgetCategoryMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: CategoryUpdate }) =>
      api.updateBudgetCategory(id, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["budget-categories"] });
    },
  });
}

// --------------------------------------------------------------------------
// Cost centers
// --------------------------------------------------------------------------

export function useCostCenters(includeInactive = false) {
  const api = useApi();
  const client = useBudgetClient();
  return loadingUntilClient(
    useQuery({
      queryKey: ["budget-cost-centers", client, includeInactive],
      queryFn: () => api.listCostCenters(includeInactive, requireKnown(client)),
      enabled: client != null,
    }),
    client,
  );
}

export function useCreateCostCenterMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  const client = useBudgetClient();
  return useMutation({
    mutationFn: (body: NewRow<CostCenterCreate>) =>
      // PHASE 3: the selected client; until then the internal one (`useBudgetClient`).
      api.createCostCenter({ ...body, client_id: requireKnown(client) }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["budget-cost-centers"] });
    },
  });
}

export function useUpdateCostCenterMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: CostCenterUpdate }) =>
      api.updateCostCenter(id, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["budget-cost-centers"] });
    },
  });
}

// --------------------------------------------------------------------------
// FX rates
// --------------------------------------------------------------------------

export function useFxRates() {
  const api = useApi();
  return useQuery({ queryKey: ["budget-fx-rates"], queryFn: () => api.listFxRates() });
}

export function useCreateFxRateMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: FxRateCreate) => api.createFxRate(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["budget-fx-rates"] });
    },
  });
}

export function useUpdateFxRateMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: FxRateUpdate }) => api.updateFxRate(id, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["budget-fx-rates"] });
    },
  });
}

// --------------------------------------------------------------------------
// Budgets + lines
// --------------------------------------------------------------------------

export function useBudgets() {
  const api = useApi();
  const client = useBudgetClient();
  return loadingUntilClient(
    useQuery({
      queryKey: ["budgets", client],
      queryFn: () => api.listBudgets(requireKnown(client)),
      enabled: client != null,
    }),
    client,
  );
}

export function useCreateBudgetMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  const client = useBudgetClient();
  return useMutation({
    mutationFn: (body: NewRow<BudgetCreate>) =>
      // PHASE 3: the selected client; until then the internal one (`useBudgetClient`).
      api.createBudget({ ...body, client_id: requireKnown(client) }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["budgets"] });
    },
  });
}

export function useUpdateBudgetMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: BudgetUpdate }) => api.updateBudget(id, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["budgets"] });
      void queryClient.invalidateQueries({ queryKey: ["budget", variables.id] });
    },
  });
}

export function useCloseBudgetMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.closeBudget(id),
    onSuccess: (_data, id) => {
      void queryClient.invalidateQueries({ queryKey: ["budgets"] });
      void queryClient.invalidateQueries({ queryKey: ["budget", id] });
      // A closed budget rejects all line mutations, and its planned amounts
      // are now final -- the lines/variance/position views are stale too.
      void queryClient.invalidateQueries({ queryKey: ["budget-lines", id] });
      void queryClient.invalidateQueries({ queryKey: ["budget-variance", id] });
      void queryClient.invalidateQueries({ queryKey: ["budget-position", id] });
    },
  });
}

export function useBudgetLines(budgetId: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["budget-lines", budgetId],
    queryFn: () => api.listBudgetLines(budgetId as string),
    enabled: budgetId != null,
  });
}

export function usePutBudgetLinesMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ budgetId, lines }: { budgetId: string; lines: BudgetLineInput[] }) =>
      api.putBudgetLines(budgetId, lines),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["budget-lines", variables.budgetId] });
      void queryClient.invalidateQueries({ queryKey: ["budget-variance", variables.budgetId] });
      void queryClient.invalidateQueries({ queryKey: ["budget-position", variables.budgetId] });
    },
  });
}

export function useDeleteBudgetLineMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ budgetId, lineId }: { budgetId: string; lineId: string }) =>
      api.deleteBudgetLine(budgetId, lineId),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["budget-lines", variables.budgetId] });
      void queryClient.invalidateQueries({ queryKey: ["budget-variance", variables.budgetId] });
      void queryClient.invalidateQueries({ queryKey: ["budget-position", variables.budgetId] });
    },
  });
}

// --------------------------------------------------------------------------
// Reporting: variance, position
//
// No `useBudgetForecast` hook here (post-review controller fix,
// .git/sdd/task-13-report.md Q1): `GET /budget/forecast` has no UI surface
// and per the fix directive an unreachable hook must either be wired to one
// or deleted -- forecast is deleted rather than given a sixth nav surface
// for a read nothing else in this module depends on. The route itself is
// untouched; only the dead frontend hook (and its now-unused
// `getBudgetForecast`/`ForecastOut`/`ForecastRow` support) is removed.
// --------------------------------------------------------------------------

export function useBudgetVariance(budgetId: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["budget-variance", budgetId],
    queryFn: () => api.getBudgetVariance(budgetId as string),
    enabled: budgetId != null,
  });
}

export function useBudgetPosition(budgetId: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["budget-position", budgetId],
    queryFn: () => api.getBudgetPosition(budgetId as string),
    enabled: budgetId != null,
  });
}

// --------------------------------------------------------------------------
// Expenses
// --------------------------------------------------------------------------

export function useExpenses(filters: ExpenseListFilters = {}) {
  const api = useApi();
  const client = useBudgetClient();
  return loadingUntilClient(
    useQuery({
      queryKey: ["budget-expenses", filters, client],
      queryFn: () => api.listExpenses(filters, requireKnown(client)),
      enabled: client != null,
    }),
    client,
  );
}

export function useCreateExpenseMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  const client = useBudgetClient();
  return useMutation({
    mutationFn: (body: NewRow<ExpenseCreate>) =>
      // PHASE 3: the selected client; until then the internal one (`useBudgetClient`).
      api.createExpense({ ...body, client_id: requireKnown(client) }),
    onSuccess: () => {
      // Prefix invalidation: TanStack Query matches every query keyed
      // ["budget-expenses", ...] regardless of the filter object in the rest
      // of the key, so both the plain expense list and the confirmation
      // queue (filtered to status=expected) refetch.
      void queryClient.invalidateQueries({ queryKey: ["budget-expenses"] });
      // Minor fix: a create can arrive already `confirmed` (status is
      // caller-supplied) or `expected` -- either way it changes
      // actual/committed, which variance/position sum, exactly like
      // confirm/void below. Without this the dashboard shows stale numbers
      // after creating a confirmed expense.
      void queryClient.invalidateQueries({ queryKey: ["budget-variance"] });
      void queryClient.invalidateQueries({ queryKey: ["budget-position"] });
    },
  });
}

export function useUpdateExpenseMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: ExpenseUpdate }) => api.updateExpense(id, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["budget-expenses"] });
      // Minor fix: editing amount/currency/date/category on a confirmed (or
      // expected) row changes the actual/committed figures variance/position
      // sum -- the same broad invalidation as confirm/void below.
      void queryClient.invalidateQueries({ queryKey: ["budget-variance"] });
      void queryClient.invalidateQueries({ queryKey: ["budget-position"] });
    },
  });
}

export function useConfirmExpenseMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: ExpenseConfirm }) => api.confirmExpense(id, body),
    onSuccess: () => {
      // A confirm moves a row out of the status=expected confirmation queue
      // and into the actuals the variance/position reports sum -- the same
      // broad prefix invalidation as create, plus the reports it feeds.
      void queryClient.invalidateQueries({ queryKey: ["budget-expenses"] });
      void queryClient.invalidateQueries({ queryKey: ["budget-variance"] });
      void queryClient.invalidateQueries({ queryKey: ["budget-position"] });
    },
  });
}

export function useVoidExpenseMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.voidExpense(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["budget-expenses"] });
      void queryClient.invalidateQueries({ queryKey: ["budget-variance"] });
      void queryClient.invalidateQueries({ queryKey: ["budget-position"] });
    },
  });
}

export function useMaterializeExpensesMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  const client = useBudgetClient();
  return useMutation({
    mutationFn: (body: NewRow<MaterializeRequest>) =>
      // PHASE 3: the selected client; until then the internal one (`useBudgetClient`).
      api.materializeExpenses({ ...body, client_id: requireKnown(client) }),
    onSuccess: () => {
      // N2 fix: materialization moves money from projected to committed, the
      // same shape of change as confirm/void/update above -- it must
      // invalidate variance/position too, or the dashboard keeps showing
      // pre-materialization figures until a manual refresh.
      void queryClient.invalidateQueries({ queryKey: ["budget-expenses"] });
      void queryClient.invalidateQueries({ queryKey: ["budget-variance"] });
      void queryClient.invalidateQueries({ queryKey: ["budget-position"] });
    },
  });
}

export function useUploadExpenseAttachmentMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, file }: { id: string; file: File }) => api.uploadExpenseAttachment(id, file),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["budget-expenses"] });
    },
  });
}
