import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { ConfirmDialog } from "../../components/confirm";
import { Form, FormActions, attemptProblems, describeSubmitError } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { Button, Dialog, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { MessageKey } from "../../i18n/core";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDate } from "../../lib/datetime";
import { useScrollToHash } from "../../lib/useScrollToHash";
import {
  useBudgetCategories,
  useBudgetSettings,
  useCostCenters,
  useCreateBudgetCategoryMutation,
  useCreateCostCenterMutation,
  useCreateFxRateMutation,
  useFxRates,
  useUpdateBudgetCategoryMutation,
  useUpdateBudgetSettingsMutation,
  useUpdateCostCenterMutation,
  useUpdateFxRateMutation,
} from "./api";
import { BudgetPageNav } from "./budgetState";
import { PageHead, Panel, QueryState, StatusPill } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { KV } from "../../shell/viz";
import { useEverywherePermissions } from "../../lib/mePermissions";

/**
 * The active/inactive pill every admin table draws. The word agrees with the
 * record it sits on (a category is feminine, a cost center masculine), which
 * the shared StatusPill words cannot do, so the label is passed.
 */
function ActivePill({ active, feminine = false }: { active: boolean; feminine?: boolean }) {
  const t = useT();
  const label = feminine
    ? t(active ? "budget.admin.activeF" : "budget.admin.inactiveF")
    : t(active ? "budget.admin.activeM" : "budget.admin.inactiveM");
  return <StatusPill value={active ? "active" : "inactive"} label={label} kind={active ? "ok" : "mut"} />;
}

/** The announced failure line under a table whose row edit was refused. */
function RefusedLine({ error }: { error: unknown }) {
  useT();
  return (
    <p role="alert" className="notice red" style={{ margin: 12 }}>
      <Icon name="circle-x" size={16} />
      <span>{describeSubmitError(error)}</span>
    </p>
  );
}

/**
 * What each admin record may not be created without.
 *
 * The three create dialogs on this page all read
 * `disabled={create.isPending || !key || !name}` -- the silent no-op the audits
 * counted across six modules, on the page a new install has to visit FIRST
 * (nothing else in this module works until a category exists, and the New
 * expense dialog now links people straight here). A control that will not move
 * and says nothing is the dead end one door along.
 */
// Built when asked, never held: the labels and sentences follow the language.
const categoryRules = (): readonly FieldRule[] => [
  { name: "key", label: translate("budget.common.key") },
  { name: "name", label: translate("budget.common.name") },
];

const costCenterRules = (): readonly FieldRule[] => [
  { name: "code", label: translate("budget.common.code") },
  { name: "name", label: translate("budget.common.name") },
];

const fxRules = (): readonly FieldRule[] => [
  { name: "from_currency", label: translate("budget.common.fromCurrency"), validate: currencyCode },
  { name: "rate", label: translate("budget.common.rate"), validate: decimalRate },
  { name: "effective_date", label: translate("budget.common.effectiveDate") },
];

function currencyCode(value: string): string | null {
  if (!/^[A-Za-z]{3}$/.test(value)) return translate("budget.admin.currencyCode");
  return null;
}

function decimalRate(value: string): string | null {
  // The rate is a Decimal server-side and travels as a string -- nothing
  // between this box and pydantic parses it, and a rate of 0 would silently
  // convert every expense in that currency to zero.
  if (!/^\d+(\.\d+)?$/.test(value) || Number(value) <= 0) {
    return translate("budget.admin.rateShape");
  }
  return null;
}

// Categories, cost centers, FX rates, and the base-currency/fiscal-year
// settings -- the four admin surfaces spec §11 groups under one page.
// Reads are `budget.read`-gated (App.tsx's nav entry already requires it to
// reach any budget page); every write below is `budget.manage`-gated,
// presentationally only -- api.py's require_permission is the actual
// authority on every one of these routes.
export function BudgetAdminPage() {
  const t = useT();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canRead = permissions.includes("budget.read");
  const canManage = permissions.includes("budget.manage");
  // Exchange rates are configuration every client shares (GLOBAL_WRITES): the
  // code on EVERY client (phase 3 design §2). Categories and cost centres are not.
  const canManageRates = useEverywherePermissions().has("budget.manage");
  // `/budget/admin#fx-rates` is where the dashboard's "could not be converted"
  // notice sends an operator; without this the fragment moves nothing.
  useScrollToHash(me.isFetched);

  return (
    <div>
      <PageHead
        title={t("budget.admin.title")}
        description={t("budget.admin.description")}
      />

      <BudgetPageNav canRead={canRead} />

      <SettingsPanel canManage={canManage} />
      <CategoriesPanel canManage={canManage} />
      <CostCentersPanel canManage={canManage} />
      {/* An anchor on a wrapper: `Panel` takes no id. */}
      <div id="fx-rates">
        <FxRatesPanel canManage={canManageRates} />
      </div>
    </div>
  );
}

/**
 * The stored setting as a date rather than as its wire value. `1` was printed
 * "Month 1", which is the number in the column and not the fact it records;
 * the design file writes the same setting as "1 January". Out-of-range months
 * cannot come from this API (`fiscal_year_start_month` is 1-12 server-side)
 * but are shown verbatim rather than silently mapped to a wrong month.
 */
const MONTH_KEYS: readonly MessageKey[] = [
  "budget.admin.month.m1",
  "budget.admin.month.m2",
  "budget.admin.month.m3",
  "budget.admin.month.m4",
  "budget.admin.month.m5",
  "budget.admin.month.m6",
  "budget.admin.month.m7",
  "budget.admin.month.m8",
  "budget.admin.month.m9",
  "budget.admin.month.m10",
  "budget.admin.month.m11",
  "budget.admin.month.m12",
];

/** "1 January", read from the catalogue each time it is asked for. */
function fiscalYearStart(month: number): string {
  const key = MONTH_KEYS[month - 1];
  return key
    ? translate("budget.admin.startOf", { month: translate(key) })
    : translate("budget.admin.monthFallback", { number: month });
}

function SettingsPanel({ canManage }: { canManage: boolean }) {
  const t = useT();
  const settings = useBudgetSettings();
  const update = useUpdateBudgetSettingsMutation();
  const [baseCurrency, setBaseCurrency] = useState("");
  const [startMonth, setStartMonth] = useState("");
  const [confirming, setConfirming] = useState(false);

  const current = settings.data;

  // CHANGING THE BASE CURRENCY IS THE CONSEQUENTIAL ONE, and until now Save
  // treated it exactly like nudging the fiscal-year start month. It never
  // retro-converts what is stored (`rates.py`: "every existing
  // `expense.fx_rate`/`amount_base` is left exactly as it was written"), so
  // afterwards the module's totals are a mix of two bases -- which is why the
  // server refuses the change outright once any non-void expense carries a
  // converted amount unless the caller passes `acknowledge_mixed_basis`.
  //
  // That flag had NO control in this UI, so the refusal was a dead end: an
  // operator reading "pass acknowledge_mixed_basis=True to proceed" from a
  // 400 body has been told to call the API by hand. The confirmation below is
  // where that acknowledgement belongs -- read the consequence, then send it.
  const currencyChanging =
    baseCurrency.trim() !== "" &&
    baseCurrency.trim().toUpperCase() !== (current?.base_currency ?? "").toUpperCase();

  function save(acknowledgeMixedBasis: boolean) {
    update.mutate({
      base_currency: baseCurrency || undefined,
      fiscal_year_start_month: startMonth ? Number(startMonth) : undefined,
      ...(acknowledgeMixedBasis ? { acknowledge_mixed_basis: true } : {}),
    });
  }

  function submit() {
    // The month alone is a plain save; the currency is what needs reading
    // back before it lands.
    if (currencyChanging) {
      setConfirming(true);
      return;
    }
    save(false);
  }

  return (
    <Panel title={t("budget.admin.settings")}>
      <QueryState
        loading={settings.isLoading}
        error={settings.error}
        empty={!settings.isLoading && !settings.error && current == null}
        emptyLabel={t("budget.admin.settingsEmpty")}
      >
        {current && (
          <div style={{ marginBottom: 16 }}>
            <KV
              items={[
                [t("budget.admin.baseCurrency"), <span className="mono">{current.base_currency}</span>],
                [t("budget.admin.fiscalStarts"), fiscalYearStart(current.fiscal_year_start_month)],
              ]}
            />
          </div>
        )}
        {canManage && (
          <>
            <Field label={t("budget.admin.newBaseCurrency")}>
              <input
                className={inputClass}
                value={baseCurrency}
                maxLength={3}
                placeholder={current?.base_currency}
                onChange={(e) => setBaseCurrency(e.target.value.toUpperCase())}
              />
            </Field>
            {/* THE MONTH IS A MONTH, NOT ITS WIRE VALUE. This was a bare
                number spinner holding `1`, two lines under a key/value row
                that already reads "Fiscal year starts: 1 January" -- the one
                control an operator actually operates was the only place the
                month stayed a number to translate, and the spinner offered
                `13` as readily as `12`. The design draws this setting as a
                select of month names (`itms-prototype-v3.html`,
                `P['budget/admin']`: `field('Fiscal year starts',
                select('1 January'))`), and `fiscalYearStart` above is the
                mapping this page already owns.

                BLANK STILL MEANS UNCHANGED, which is what the panel's two
                fields have always meant and what Save's disabled rule and the
                `undefined` in the request body are built on. The first option
                says so in words and names the month it would keep, the way the
                currency field's placeholder names the currency it would
                keep. */}
            <Field label={t("budget.admin.newStartMonth")}>
              <select
                className="select-field"
                value={startMonth}
                onChange={(e) => setStartMonth(e.target.value)}
              >
                <option value="">
                  {current
                    ? t("budget.admin.unchangedWith", { month: fiscalYearStart(current.fiscal_year_start_month) })
                    : t("budget.admin.unchanged")}
                </option>
                {MONTH_KEYS.map((key, index) => (
                  <option key={key} value={String(index + 1)}>
                    {fiscalYearStart(index + 1)}
                  </option>
                ))}
              </select>
            </Field>
            <Button onClick={submit} disabled={update.isPending || (!baseCurrency && !startMonth)}>
              {update.isPending ? t("budget.common.saving") : t("budget.admin.saveSettings")}
            </Button>
            {/* `String(update.error)` printed the raw payload -- and for this
                setting the server's refusal is a full sentence worth reading
                ("base currency cannot change: at least one non-void expense
                already carries a converted amount_base"), which
                `describeSubmitError` passes through verbatim. */}
            {update.isError && (
              <p role="alert" className="notice red" style={{ marginTop: 12 }}>
                <Icon name="circle-x" size={16} />
                <span>{describeSubmitError(update.error)}</span>
              </p>
            )}

            {/* The subject is the actual transition, old to new, because the
                field above holds only the new value and "USD" alone reads the
                same whether it is the change or the thing being changed from.
                Confirming SENDS `acknowledge_mixed_basis` -- the flag exists
                precisely to make somebody read this sentence first. */}
            <ConfirmDialog
              open={confirming}
              title={t("budget.admin.changeTitle")}
              subject={t("budget.admin.changeSubject", {
                from: current?.base_currency ?? t("budget.admin.unknownCurrency"),
                to: baseCurrency.trim().toUpperCase(),
              })}
              body={t("budget.admin.changeBody")}
              confirmLabel={t("budget.admin.changeConfirm")}
              destructive
              onCancel={() => setConfirming(false)}
              onConfirm={() => {
                setConfirming(false);
                save(true);
              }}
            />
          </>
        )}
      </QueryState>
    </Panel>
  );
}

function CategoriesPanel({ canManage }: { canManage: boolean }) {
  const t = useT();
  const categories = useBudgetCategories(true);
  const create = useCreateBudgetCategoryMutation();
  const update = useUpdateBudgetCategoryMutation();
  const [creating, setCreating] = useState(false);
  const [key, setKey] = useState("");
  const [name, setName] = useState("");
  // Filled ON SUBMIT, never while typing; held as data and worded at render.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  // Only one row is ever in edit mode at a time, so the edit inputs below
  // can use a fixed accessible name instead of one keyed per row.
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editName, setEditName] = useState("");
  const [editActive, setEditActive] = useState(true);

  const items = categories.data ?? [];

  function submit() {
    const next: Attempt = { rules: categoryRules, values: { key, name } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;

    create.mutate(
      { key: key.trim(), name: name.trim() },
      { onSuccess: () => { setKey(""); setName(""); setAttempt(null); setCreating(false); } },
    );
  }

  function startEdit(c: { id: string; name: string; active: boolean }) {
    setEditingId(c.id);
    setEditName(c.name);
    setEditActive(c.active);
  }

  function saveEdit(id: string) {
    update.mutate(
      { id, body: { name: editName, active: editActive } },
      { onSuccess: () => setEditingId(null) },
    );
  }

  return (
    <Panel
      title={t("budget.admin.categories")}
      bare
      actions={canManage ? <Button variant="outline" onClick={() => setCreating(true)}>{t("budget.admin.newCategory")}</Button> : undefined}
    >
      <QueryState
        loading={categories.isLoading}
        error={categories.error}
        empty={items.length === 0}
        emptyLabel={t("budget.admin.categoriesEmpty")}
      >
        <Table>
          <thead>
            <tr>
              <Th>{t("budget.admin.colName")}</Th>
              <Th>{t("budget.admin.colKey")}</Th>
              <Th>{t("budget.admin.activeF")}</Th>
              {canManage && <Th />}
            </tr>
          </thead>
          <tbody>
            {items.map((c) => (
              <tr key={c.id}>
                <Td lead={<Icon name="tag" size={18} />}>
                  {editingId === c.id ? (
                    <input
                      aria-label={t("budget.admin.categoryName")}
                      className={inputClass}
                      value={editName}
                      onChange={(e) => setEditName(e.target.value)}
                    />
                  ) : (
                    <div className="cell-title">{c.name}</div>
                  )}
                </Td>
                <Td>
                  <span className="mono">{c.key}</span>
                </Td>
                <Td>
                  {editingId === c.id ? (
                    <label className="flex items-center gap-2">
                      <input
                        type="checkbox"
                        aria-label={t("budget.admin.categoryActive")}
                        checked={editActive}
                        onChange={(e) => setEditActive(e.target.checked)}
                      />
                      {t("budget.admin.activeF")}
                    </label>
                  ) : (
                    <ActivePill active={c.active} feminine />
                  )}
                </Td>
                {canManage && (
                  <Td>
                    {editingId === c.id ? (
                      <div className="flex gap-2">
                        <Button onClick={() => saveEdit(c.id)} disabled={update.isPending || !editName}>
                          {update.isPending ? t("budget.common.saving") : t("budget.common.save")}
                        </Button>
                        <Button variant="outline" onClick={() => setEditingId(null)}>
                          {t("budget.common.cancel")}
                        </Button>
                      </div>
                    ) : (
                      <Button variant="outline" onClick={() => startEdit(c)}>
                        {t("budget.common.edit")}
                      </Button>
                    )}
                  </Td>
                )}
              </tr>
            ))}
          </tbody>
        </Table>
      </QueryState>
      {/* `String(update.error)` rendered "ApiError: " and nothing after it for
          a bodyless 503, in no live region -- so a refused rename looked like
          a successful one until the next reload. */}
      {update.isError && <RefusedLine error={update.error} />}

      <Dialog open={creating} onClose={() => setCreating(false)} title={t("budget.admin.newCategory")}>
        <Form onSubmit={submit}>
          <div className="form-grid">
            <Field label={t("budget.common.key")}>
              <input className={inputClass} required value={key} onChange={(e) => setKey(e.target.value)} />
            </Field>
            <Field label={t("budget.common.name")}>
              <input className={inputClass} required value={name} onChange={(e) => setName(e.target.value)} />
            </Field>
          </div>
          <div className="flex justify-end gap-2">
            {/* `type="button"`: ui.tsx's Button sets no type, and a typeless
                button inside a <form> IS a submit button -- Cancel would save. */}
            <Button type="button" variant="ghost" onClick={() => setCreating(false)}>
              {t("budget.common.cancel")}
            </Button>
            <FormActions
              label={t("budget.common.save")}
              pending={create.isPending}
              error={create.error}
              problems={attemptProblems(attempt)}
            />
          </div>
        </Form>
      </Dialog>
    </Panel>
  );
}

function CostCentersPanel({ canManage }: { canManage: boolean }) {
  const t = useT();
  const costCenters = useCostCenters(true);
  const create = useCreateCostCenterMutation();
  const update = useUpdateCostCenterMutation();
  const [creating, setCreating] = useState(false);
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  const [editingId, setEditingId] = useState<string | null>(null);
  const [editName, setEditName] = useState("");
  const [editActive, setEditActive] = useState(true);

  const items = costCenters.data ?? [];

  function submit() {
    const next: Attempt = { rules: costCenterRules, values: { code, name } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;

    create.mutate(
      { code: code.trim(), name: name.trim() },
      { onSuccess: () => { setCode(""); setName(""); setAttempt(null); setCreating(false); } },
    );
  }

  function startEdit(cc: { id: string; name: string; active: boolean }) {
    setEditingId(cc.id);
    setEditName(cc.name);
    setEditActive(cc.active);
  }

  function saveEdit(id: string) {
    update.mutate(
      { id, body: { name: editName, active: editActive } },
      { onSuccess: () => setEditingId(null) },
    );
  }

  return (
    <Panel
      title={t("budget.admin.costCenters")}
      bare
      actions={canManage ? <Button variant="outline" onClick={() => setCreating(true)}>{t("budget.admin.newCostCenter")}</Button> : undefined}
    >
      <QueryState
        loading={costCenters.isLoading}
        error={costCenters.error}
        empty={items.length === 0}
        emptyLabel={t("budget.admin.costCentersEmpty")}
      >
        <Table>
          <thead>
            <tr>
              <Th>{t("budget.admin.colName")}</Th>
              <Th>{t("budget.admin.colCode")}</Th>
              <Th>{t("budget.admin.activeM")}</Th>
              {canManage && <Th />}
            </tr>
          </thead>
          <tbody>
            {items.map((cc) => (
              <tr key={cc.id}>
                <Td lead={<Icon name="building-warehouse" size={18} />}>
                  {editingId === cc.id ? (
                    <input
                      aria-label={t("budget.admin.costCenterName")}
                      className={inputClass}
                      value={editName}
                      onChange={(e) => setEditName(e.target.value)}
                    />
                  ) : (
                    <div className="cell-title">{cc.name}</div>
                  )}
                </Td>
                <Td>
                  <span className="mono">{cc.code}</span>
                </Td>
                <Td>
                  {editingId === cc.id ? (
                    <label className="flex items-center gap-2">
                      <input
                        type="checkbox"
                        aria-label={t("budget.admin.costCenterActive")}
                        checked={editActive}
                        onChange={(e) => setEditActive(e.target.checked)}
                      />
                      {t("budget.admin.activeM")}
                    </label>
                  ) : (
                    <ActivePill active={cc.active} />
                  )}
                </Td>
                {canManage && (
                  <Td>
                    {editingId === cc.id ? (
                      <div className="flex gap-2">
                        <Button onClick={() => saveEdit(cc.id)} disabled={update.isPending || !editName}>
                          {update.isPending ? t("budget.common.saving") : t("budget.common.save")}
                        </Button>
                        <Button variant="outline" onClick={() => setEditingId(null)}>
                          {t("budget.common.cancel")}
                        </Button>
                      </div>
                    ) : (
                      <Button variant="outline" onClick={() => startEdit(cc)}>
                        {t("budget.common.edit")}
                      </Button>
                    )}
                  </Td>
                )}
              </tr>
            ))}
          </tbody>
        </Table>
      </QueryState>
      {/* `String(update.error)` rendered "ApiError: " and nothing after it for
          a bodyless 503, in no live region -- so a refused rename looked like
          a successful one until the next reload. */}
      {update.isError && <RefusedLine error={update.error} />}

      <Dialog open={creating} onClose={() => setCreating(false)} title={t("budget.admin.newCostCenter")}>
        <Form onSubmit={submit}>
          <div className="form-grid">
            <Field label={t("budget.common.code")}>
              <input className={inputClass} required value={code} onChange={(e) => setCode(e.target.value)} />
            </Field>
            <Field label={t("budget.common.name")}>
              <input className={inputClass} required value={name} onChange={(e) => setName(e.target.value)} />
            </Field>
          </div>
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setCreating(false)}>
              {t("budget.common.cancel")}
            </Button>
            <FormActions
              label={t("budget.common.save")}
              pending={create.isPending}
              error={create.error}
              problems={attemptProblems(attempt)}
            />
          </div>
        </Form>
      </Dialog>
    </Panel>
  );
}

function FxRatesPanel({ canManage }: { canManage: boolean }) {
  const t = useT();
  const fxRates = useFxRates();
  const create = useCreateFxRateMutation();
  const update = useUpdateFxRateMutation();
  const [creating, setCreating] = useState(false);
  const [fromCurrency, setFromCurrency] = useState("");
  const [rate, setRate] = useState("1.00000000");
  const [effectiveDate, setEffectiveDate] = useState(new Date().toISOString().slice(0, 10));
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  const [editingId, setEditingId] = useState<string | null>(null);
  const [editRate, setEditRate] = useState("");

  const items = fxRates.data ?? [];

  function submit() {
    const next: Attempt = {
      rules: fxRules,
      values: { from_currency: fromCurrency, rate, effective_date: effectiveDate },
    };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;

    create.mutate(
      // The rate stays a STRING all the way to the wire -- it is a Decimal
      // server-side, and Number()-ing it here is how 0.92500000 becomes 0.925.
      { from_currency: fromCurrency.trim().toUpperCase(), rate: rate.trim(), effective_date: effectiveDate },
      { onSuccess: () => { setFromCurrency(""); setRate("1.00000000"); setAttempt(null); setCreating(false); } },
    );
  }

  function startEdit(fx: { id: string; rate: string }) {
    setEditingId(fx.id);
    setEditRate(fx.rate);
  }

  function saveEdit(id: string) {
    update.mutate(
      { id, body: { rate: editRate } },
      { onSuccess: () => setEditingId(null) },
    );
  }

  return (
    <Panel
      title={t("budget.admin.fxRates")}
      bare
      actions={canManage ? <Button variant="outline" onClick={() => setCreating(true)}>{t("budget.admin.newRate")}</Button> : undefined}
    >
      <QueryState
        loading={fxRates.isLoading}
        error={fxRates.error}
        empty={items.length === 0}
        emptyLabel={t("budget.admin.fxEmpty")}
      >
        <Table>
          <thead>
            <tr>
              <Th>{t("budget.admin.colFrom")}</Th>
              <Th>{t("budget.admin.colTo")}</Th>
              <Th>{t("budget.admin.colRate")}</Th>
              <Th>{t("budget.admin.colEffective")}</Th>
              {canManage && <Th />}
            </tr>
          </thead>
          <tbody>
            {items.map((fx) => (
              <tr key={fx.id}>
                <Td lead={<Icon name="arrows-exchange" size={18} />}>
                  <div className="cell-title mono">{fx.from_currency}</div>
                </Td>
                <Td>
                  <span className="mono">{fx.to_currency}</span>
                </Td>
                {/* Rate is a Decimal on the wire -- rendered verbatim as
                    the string the API returns, never coerced to a number. */}
                <Td>
                  {editingId === fx.id ? (
                    <input
                      aria-label={t("budget.admin.fxRateAria")}
                      className={inputClass}
                      value={editRate}
                      onChange={(e) => setEditRate(e.target.value)}
                    />
                  ) : (
                    <span className="mono">{fx.rate}</span>
                  )}
                </Td>
                {/* THE MODULE HAS ONE DATE FORMAT. `effective_date` is a
                    `date` on the wire -- a day with no time and no zone -- so
                    it is pinned to UTC for the same reason the expense ledger
                    pins its own: rendered in a westward zone the raw string
                    would land a day early. It was the one date in this module
                    still printed in the wire format. */}
                <Td>{formatDate(fx.effective_date, "UTC")}</Td>
                {canManage && (
                  <Td>
                    {editingId === fx.id ? (
                      <div className="flex gap-2">
                        <Button onClick={() => saveEdit(fx.id)} disabled={update.isPending || !editRate}>
                          {update.isPending ? t("budget.common.saving") : t("budget.common.save")}
                        </Button>
                        <Button variant="outline" onClick={() => setEditingId(null)}>
                          {t("budget.common.cancel")}
                        </Button>
                      </div>
                    ) : (
                      <Button variant="outline" onClick={() => startEdit(fx)}>
                        {t("budget.common.edit")}
                      </Button>
                    )}
                  </Td>
                )}
              </tr>
            ))}
          </tbody>
        </Table>
      </QueryState>
      {/* `String(update.error)` rendered "ApiError: " and nothing after it for
          a bodyless 503, in no live region -- so a refused rename looked like
          a successful one until the next reload. */}
      {update.isError && <RefusedLine error={update.error} />}

      <Dialog open={creating} onClose={() => setCreating(false)} title={t("budget.admin.newFxTitle")}>
        <Form onSubmit={submit}>
          <div className="form-grid">
            <Field label={t("budget.common.fromCurrency")}>
              <input
                className={inputClass}
                value={fromCurrency}
                maxLength={3}
                required
                onChange={(e) => setFromCurrency(e.target.value.toUpperCase())}
              />
            </Field>
            <Field label={t("budget.common.rate")}>
              {/* Text with a decimal keypad, not type="number": the value is a
                  Decimal string on the wire, and a number input would hand back
                  a locale-formatted or exponent-notation value to re-serialise. */}
              <input
                className={inputClass}
                type="text"
                inputMode="decimal"
                required
                value={rate}
                onChange={(e) => setRate(e.target.value)}
              />
            </Field>
            <Field label={t("budget.common.effectiveDate")}>
              <input
                type="date"
                className={inputClass}
                required
                value={effectiveDate}
                onChange={(e) => setEffectiveDate(e.target.value)}
              />
              {/* The date this holds is spelled out under the grid, below
                  (design review M1): a native date input draws the USER
                  AGENT'S mask and no CSS reorders it, so this reads
                  `09/22/2026` to a US-locale browser on a console that renders
                  every other date `22 Sept 2026`. It is not inside this
                  `<Field>` because `Field` wraps its control in the `<label>`
                  and text placed here becomes part of the field's NAME; the
                  accounting is at `agent-push/PushAuthorizationsPage`. "UTC"
                  because this is a calendar DATE, not an instant. */}
            </Field>
          </div>
          {effectiveDate !== "" ? (
            <p className="note" style={{ marginTop: 14, marginBottom: 14 }}>
              {t("budget.admin.effective", { date: formatDate(`${effectiveDate}T00:00:00Z`, "UTC") })}
            </p>
          ) : null}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setCreating(false)}>
              {t("budget.common.cancel")}
            </Button>
            <FormActions
              label={t("budget.common.save")}
              pending={create.isPending}
              error={create.error}
              problems={attemptProblems(attempt)}
            />
          </div>
        </Form>
      </Dialog>
    </Panel>
  );
}
