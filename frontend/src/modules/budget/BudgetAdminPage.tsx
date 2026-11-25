import { useQuery } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { ConfirmDialog } from "../../components/confirm";
import { Form, FormActions, attemptProblems } from "../../components/form";
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
import { BudgetPageNav, BudgetPickClient, useBudgetNeedsClient } from "./budgetState";
import { useReportDirty } from "./dialogParams";
import type { CategoryOut, CostCenterOut, FxRateOut } from "./types";
import {
  Btn,
  FormSection,
  PageHead,
  QueryState,
  SaveBar,
  StatusPill,
  useDraft,
  useNewParam,
  useUnsavedGuard,
} from "../../shell/components";
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
//
// ON THE FORM KIT (redesign Task 11, 6.6; Receta FO). "General" (the base
// currency and the fiscal year's start) is a `FormSection` with the page's one
// `SaveBar`; categories, cost centres and exchange rates are each a
// `FormSection` with its table, its add dialog from `?new=` (`category`,
// `costCenter`, `rate`; decision D8) and a row's edit in a dialog where it was
// edited in the row. Every call and every body is the one it was.
//
// ONE GUARD FOR THE PAGE: General and whichever dialog is open each say
// whether they hold changes. Closing an add dialog is leaving it (`?new=`).
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
  const needsClient = useBudgetNeedsClient();

  const general = useGeneralForm();
  const [newDirty, setNewDirty] = useState(false);
  const [editDirty, setEditDirty] = useState(false);
  const { guard, release } = useUnsavedGuard(general.dirty || newDirty || editDirty, {
    search: newDirty ? ["new"] : false,
  });
  const dialogs: DialogWiring = { onNewDirty: setNewDirty, onEditDirty: setEditDirty, release };

  return (
    <div>
      <PageHead title={t("budget.admin.title")} description={t("budget.admin.description")} />

      <BudgetPageNav canRead={canRead} />

      {/* The settings, categories and cost centres are one client's; the
          exchange rates below are every client's (a global route). */}
      {needsClient ? (
        <BudgetPickClient />
      ) : (
        <>
          <GeneralSection canManage={canManage} form={general} />
          <CategoriesSection canManage={canManage} dialogs={dialogs} />
          <CostCentersSection canManage={canManage} dialogs={dialogs} />
        </>
      )}
      {/* An anchor on a wrapper: `FormSection` takes no id. */}
      <div id="fx-rates">
        <FxRatesSection canManage={canManageRates} dialogs={dialogs} />
      </div>
      {/* The page's one save bar, for General. The server's refusal of a
          base-currency change is a full sentence worth reading, passed
          through verbatim. */}
      <SaveBar
        dirty={general.dirty}
        saving={general.update.isPending}
        error={general.update.error}
        saved={general.update.isSuccess && !general.dirty}
        onDiscard={() => {
          general.update.reset();
          general.discard();
        }}
        onSave={general.submit}
      />
      {/* After the dialogs: the leave question shares their `.modal` layer, so it must come later in the DOM to be drawn on top (ruling 3a). */}
      {guard}
    </div>
  );
}

/** What a section's dialogs need from the page's one guard. */
interface DialogWiring {
  onNewDirty: (dirty: boolean) => void;
  onEditDirty: (dirty: boolean) => void;
  /** Lets the navigation a successful save makes through (`useUnsavedGuard`). */
  release: () => void;
}

/** A section's add button, at the head of its body: the section has no action slot. */
function SectionAction({ children }: { children: ReactNode }) {
  return <div className="flex justify-end" style={{ marginBottom: 12 }}>{children}</div>;
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

/**
 * The base currency and the fiscal year's start, as before: each field blank
 * means "unchanged", so a save sends only what was chosen. The draft is that
 * choice; the page's save bar sends it, and a change of base currency is read
 * back in its confirmation first.
 */
interface GeneralDraft {
  baseCurrency: string;
  startMonth: string;
}
const NO_CHANGE: GeneralDraft = { baseCurrency: "", startMonth: "" };

/** General's draft and its save, held by the page so its one save bar can sit at the page's foot. */
function useGeneralForm() {
  const settings = useBudgetSettings();
  const update = useUpdateBudgetSettingsMutation();
  const [confirming, setConfirming] = useState(false);
  const { draft, setDraft, dirty, discard } = useDraft<GeneralDraft>(settings.data ? NO_CHANGE : undefined);
  const baseCurrency = draft?.baseCurrency ?? "";
  const startMonth = draft?.startMonth ?? "";
  const current = settings.data;

  // CHANGING THE BASE CURRENCY IS THE CONSEQUENTIAL ONE. It never
  // retro-converts what is stored (`rates.py`: "every existing
  // `expense.fx_rate`/`amount_base` is left exactly as it was written"), so
  // afterwards the module's totals are a mix of two bases -- which is why the
  // server refuses the change outright once any non-void expense carries a
  // converted amount unless the caller passes `acknowledge_mixed_basis`. The
  // confirmation is where that acknowledgement belongs: read the consequence,
  // then send it.
  const currencyChanging =
    baseCurrency.trim() !== "" &&
    baseCurrency.trim().toUpperCase() !== (current?.base_currency ?? "").toUpperCase();

  function save(acknowledgeMixedBasis: boolean) {
    // A new attempt starts without the last one's refusal (general ruling 7).
    update.reset();
    update.mutate(
      {
        base_currency: baseCurrency || undefined,
        fiscal_year_start_month: startMonth ? Number(startMonth) : undefined,
        ...(acknowledgeMixedBasis ? { acknowledge_mixed_basis: true } : {}),
      },
      // Saved: the fields are blank again ("unchanged"), over the new values.
      { onSuccess: () => discard() },
    );
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

  return { settings, update, confirming, setConfirming, draft, setDraft, dirty, discard, save, submit };
}

function GeneralSection({ canManage, form }: { canManage: boolean; form: ReturnType<typeof useGeneralForm> }) {
  const t = useT();
  const { settings, confirming, setConfirming, draft, setDraft, save } = form;
  const baseCurrency = draft?.baseCurrency ?? "";
  const startMonth = draft?.startMonth ?? "";
  const current = settings.data;

  return (
    <FormSection title={t("budget.admin.general")} icon="settings">
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
        {canManage && draft && (
          <>
            <div className="form-grid">
              <Field label={t("budget.admin.newBaseCurrency")}>
                <input
                  className={inputClass}
                  value={baseCurrency}
                  maxLength={3}
                  placeholder={current?.base_currency}
                  onChange={(e) => setDraft({ ...draft, baseCurrency: e.target.value.toUpperCase() })}
                />
              </Field>
              {/* THE MONTH IS A MONTH, NOT ITS WIRE VALUE: a select of month
                  names (the design's `field('Fiscal year starts',
                  select('1 January'))`). BLANK STILL MEANS UNCHANGED, and the
                  first option says so and names the month it would keep. */}
              <Field label={t("budget.admin.newStartMonth")}>
                <select
                  className="select-field"
                  value={startMonth}
                  onChange={(e) => setDraft({ ...draft, startMonth: e.target.value })}
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
            </div>
            {/* The subject is the actual transition, old to new, because the
                field above holds only the new value. Confirming SENDS
                `acknowledge_mixed_basis`. */}
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
    </FormSection>
  );
}

/** The rule an edited name answers to: present. */
const nameRules = (): readonly FieldRule[] => [{ name: "name", label: translate("budget.common.name") }];

function CategoriesSection({ canManage, dialogs }: { canManage: boolean; dialogs: DialogWiring }) {
  const t = useT();
  const categories = useBudgetCategories(true);
  // New → Category (decision D8): `?new=category` opens the add dialog.
  const created = useNewParam("category");
  const [editing, setEditing] = useState<CategoryOut | null>(null);
  const items = categories.data ?? [];

  return (
    <FormSection title={t("budget.admin.categories")} icon="tag">
      {canManage ? (
        <SectionAction>
          <Btn label={t("budget.admin.newCategory")} icon="plus" onClick={created.start} />
        </SectionAction>
      ) : null}
      <QueryState
        loading={categories.isLoading}
        error={categories.error}
        empty={items.length === 0}
        emptyLabel={t("budget.admin.categoriesEmpty")}
      >
        <Table label={t("budget.admin.categories")}>
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
                  <div className="cell-title">{c.name}</div>
                </Td>
                <Td>
                  <span className="mono">{c.key}</span>
                </Td>
                <Td>
                  <ActivePill active={c.active} feminine />
                </Td>
                {canManage && (
                  <Td>
                    <Button variant="outline" onClick={() => setEditing(c)}>
                      {t("budget.common.edit")}
                    </Button>
                  </Td>
                )}
              </tr>
            ))}
          </tbody>
        </Table>
      </QueryState>

      {/* KEYED on being open, so it starts empty however it opens. */}
      <NewCategoryDialog
        key={created.open && canManage ? "open" : "closed"}
        open={created.open && canManage}
        onClose={created.close}
        onDirty={dialogs.onNewDirty}
        onSaved={() => {
          dialogs.release();
          created.close();
        }}
      />
      {editing ? (
        <EditCategoryDialog
          key={editing.id}
          category={editing}
          onDirty={dialogs.onEditDirty}
          onClose={() => setEditing(null)}
        />
      ) : null}
    </FormSection>
  );
}

function NewCategoryDialog({
  open,
  onClose,
  onDirty,
  onSaved,
}: {
  open: boolean;
  onClose: () => void;
  onDirty: (dirty: boolean) => void;
  onSaved: () => void;
}) {
  const t = useT();
  const create = useCreateBudgetCategoryMutation();
  const [key, setKey] = useState("");
  const [name, setName] = useState("");
  // Filled ON SUBMIT, never while typing; held as data and worded at render.
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  useReportDirty(open && (key !== "" || name !== ""), onDirty);

  function submit() {
    const next: Attempt = { rules: categoryRules, values: { key, name } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    create.mutate({ key: key.trim(), name: name.trim() }, { onSuccess: onSaved });
  }

  return (
    <Dialog open={open} onClose={onClose} title={t("budget.admin.newCategory")}>
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
          <Button type="button" variant="ghost" onClick={onClose}>
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
  );
}

/**
 * A category's name and whether it is active, in a dialog where the row used
 * to turn into inputs (redesign Task 11, 6.6). The same `PATCH` body. Only
 * one row is edited at a time, so the inputs keep their fixed names.
 */
function EditCategoryDialog({
  category,
  onDirty,
  onClose,
}: {
  category: CategoryOut;
  onDirty: (dirty: boolean) => void;
  onClose: () => void;
}) {
  const t = useT();
  const update = useUpdateBudgetCategoryMutation();
  const [name, setName] = useState(category.name);
  const [active, setActive] = useState(category.active);
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const dirty = name !== category.name || active !== category.active;
  useReportDirty(dirty, onDirty);

  function submit() {
    // Saved untouched, nothing is sent: an unchanged PATCH is a false audit entry (general ruling 13).
    if (!dirty) {
      onClose();
      return;
    }
    // A name cleared to nothing used to disable Save in silence; it is named now.
    const next: Attempt = { rules: nameRules, values: { name } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    update.mutate({ id: category.id, body: { name, active } }, { onSuccess: onClose });
  }

  return (
    <Dialog open onClose={onClose} title={t("budget.admin.editCategory")}>
      <Form onSubmit={submit}>
        <div className="form-grid">
          <Field label={t("budget.admin.categoryName")}>
            <input className={inputClass} required value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              aria-label={t("budget.admin.categoryActive")}
              checked={active}
              onChange={(e) => setActive(e.target.checked)}
            />
            {t("budget.admin.activeF")}
          </label>
        </div>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("budget.common.cancel")}
          </Button>
          <FormActions
            label={t("budget.common.save")}
            pending={update.isPending}
            error={update.error}
            problems={attemptProblems(attempt)}
          />
        </div>
      </Form>
    </Dialog>
  );
}

function CostCentersSection({ canManage, dialogs }: { canManage: boolean; dialogs: DialogWiring }) {
  const t = useT();
  const costCenters = useCostCenters(true);
  // New → Cost centre (decision D8): `?new=costCenter` opens the add dialog.
  const created = useNewParam("costCenter");
  const [editing, setEditing] = useState<CostCenterOut | null>(null);
  const items = costCenters.data ?? [];

  return (
    <FormSection title={t("budget.admin.costCenters")} icon="building-warehouse">
      {canManage ? (
        <SectionAction>
          <Btn label={t("budget.admin.newCostCenter")} icon="plus" onClick={created.start} />
        </SectionAction>
      ) : null}
      <QueryState
        loading={costCenters.isLoading}
        error={costCenters.error}
        empty={items.length === 0}
        emptyLabel={t("budget.admin.costCentersEmpty")}
      >
        <Table label={t("budget.admin.costCenters")}>
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
                  <div className="cell-title">{cc.name}</div>
                </Td>
                <Td>
                  <span className="mono">{cc.code}</span>
                </Td>
                <Td>
                  <ActivePill active={cc.active} />
                </Td>
                {canManage && (
                  <Td>
                    <Button variant="outline" onClick={() => setEditing(cc)}>
                      {t("budget.common.edit")}
                    </Button>
                  </Td>
                )}
              </tr>
            ))}
          </tbody>
        </Table>
      </QueryState>

      <NewCostCenterDialog
        key={created.open && canManage ? "open" : "closed"}
        open={created.open && canManage}
        onClose={created.close}
        onDirty={dialogs.onNewDirty}
        onSaved={() => {
          dialogs.release();
          created.close();
        }}
      />
      {editing ? (
        <EditCostCenterDialog
          key={editing.id}
          costCenter={editing}
          onDirty={dialogs.onEditDirty}
          onClose={() => setEditing(null)}
        />
      ) : null}
    </FormSection>
  );
}

function NewCostCenterDialog({
  open,
  onClose,
  onDirty,
  onSaved,
}: {
  open: boolean;
  onClose: () => void;
  onDirty: (dirty: boolean) => void;
  onSaved: () => void;
}) {
  const t = useT();
  const create = useCreateCostCenterMutation();
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  useReportDirty(open && (code !== "" || name !== ""), onDirty);

  function submit() {
    const next: Attempt = { rules: costCenterRules, values: { code, name } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    create.mutate({ code: code.trim(), name: name.trim() }, { onSuccess: onSaved });
  }

  return (
    <Dialog open={open} onClose={onClose} title={t("budget.admin.newCostCenter")}>
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
          <Button type="button" variant="ghost" onClick={onClose}>
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
  );
}

function EditCostCenterDialog({
  costCenter,
  onDirty,
  onClose,
}: {
  costCenter: CostCenterOut;
  onDirty: (dirty: boolean) => void;
  onClose: () => void;
}) {
  const t = useT();
  const update = useUpdateCostCenterMutation();
  const [name, setName] = useState(costCenter.name);
  const [active, setActive] = useState(costCenter.active);
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const dirty = name !== costCenter.name || active !== costCenter.active;
  useReportDirty(dirty, onDirty);

  function submit() {
    // Saved untouched, nothing is sent: an unchanged PATCH is a false audit entry (general ruling 13).
    if (!dirty) {
      onClose();
      return;
    }
    const next: Attempt = { rules: nameRules, values: { name } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    update.mutate({ id: costCenter.id, body: { name, active } }, { onSuccess: onClose });
  }

  return (
    <Dialog open onClose={onClose} title={t("budget.admin.editCostCenter")}>
      <Form onSubmit={submit}>
        <div className="form-grid">
          <Field label={t("budget.admin.costCenterName")}>
            <input className={inputClass} required value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              aria-label={t("budget.admin.costCenterActive")}
              checked={active}
              onChange={(e) => setActive(e.target.checked)}
            />
            {t("budget.admin.activeM")}
          </label>
        </div>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("budget.common.cancel")}
          </Button>
          <FormActions
            label={t("budget.common.save")}
            pending={update.isPending}
            error={update.error}
            problems={attemptProblems(attempt)}
          />
        </div>
      </Form>
    </Dialog>
  );
}

function FxRatesSection({ canManage, dialogs }: { canManage: boolean; dialogs: DialogWiring }) {
  const t = useT();
  const fxRates = useFxRates();
  // New → Rate (decision D8): `?new=rate` opens the add dialog.
  const created = useNewParam("rate");
  const [editing, setEditing] = useState<FxRateOut | null>(null);
  const items = fxRates.data ?? [];

  return (
    <FormSection title={t("budget.admin.fxRates")} icon="arrows-exchange">
      {canManage ? (
        <SectionAction>
          <Btn label={t("budget.admin.newRate")} icon="plus" onClick={created.start} />
        </SectionAction>
      ) : null}
      <QueryState
        loading={fxRates.isLoading}
        error={fxRates.error}
        empty={items.length === 0}
        emptyLabel={t("budget.admin.fxEmpty")}
      >
        <Table label={t("budget.admin.fxRates")}>
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
                  <span className="mono">{fx.rate}</span>
                </Td>
                {/* THE MODULE HAS ONE DATE FORMAT. `effective_date` is a
                    `date` on the wire -- a day with no time and no zone -- so
                    it is pinned to UTC, as the expense ledger pins its own. */}
                <Td>{formatDate(fx.effective_date, "UTC")}</Td>
                {canManage && (
                  <Td>
                    <Button variant="outline" onClick={() => setEditing(fx)}>
                      {t("budget.common.edit")}
                    </Button>
                  </Td>
                )}
              </tr>
            ))}
          </tbody>
        </Table>
      </QueryState>

      <NewFxRateDialog
        key={created.open && canManage ? "open" : "closed"}
        open={created.open && canManage}
        onClose={created.close}
        onDirty={dialogs.onNewDirty}
        onSaved={() => {
          dialogs.release();
          created.close();
        }}
      />
      {editing ? (
        <EditFxRateDialog key={editing.id} rate={editing} onDirty={dialogs.onEditDirty} onClose={() => setEditing(null)} />
      ) : null}
    </FormSection>
  );
}

function NewFxRateDialog({
  open,
  onClose,
  onDirty,
  onSaved,
}: {
  open: boolean;
  onClose: () => void;
  onDirty: (dirty: boolean) => void;
  onSaved: () => void;
}) {
  const t = useT();
  const create = useCreateFxRateMutation();
  const today = new Date().toISOString().slice(0, 10);
  const [fromCurrency, setFromCurrency] = useState("");
  const [rate, setRate] = useState("1.00000000");
  const [effectiveDate, setEffectiveDate] = useState(today);
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  useReportDirty(open && (fromCurrency !== "" || rate !== "1.00000000" || effectiveDate !== today), onDirty);

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
      { onSuccess: onSaved },
    );
  }

  return (
    <Dialog open={open} onClose={onClose} title={t("budget.admin.newFxTitle")}>
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
            {/* The date is spelled out under the grid (design review M1): a
                native date input draws the user agent's mask. Not inside the
                `<Field>`, whose label would take the sentence as its name. */}
            <input
              type="date"
              className={inputClass}
              required
              value={effectiveDate}
              onChange={(e) => setEffectiveDate(e.target.value)}
            />
          </Field>
        </div>
        {effectiveDate !== "" ? (
          <p className="note" style={{ marginTop: 14, marginBottom: 14 }}>
            {t("budget.admin.effective", { date: formatDate(`${effectiveDate}T00:00:00Z`, "UTC") })}
          </p>
        ) : null}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
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
  );
}

/** A rate's figure, in a dialog where the row used to turn into an input. The same `PATCH` body. */
function EditFxRateDialog({
  rate,
  onDirty,
  onClose,
}: {
  rate: FxRateOut;
  onDirty: (dirty: boolean) => void;
  onClose: () => void;
}) {
  const t = useT();
  const update = useUpdateFxRateMutation();
  const [value, setValue] = useState(rate.rate);
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const dirty = value !== rate.rate;
  useReportDirty(dirty, onDirty);

  function submit() {
    // Saved untouched, nothing is sent: an unchanged PATCH is a false audit entry (general ruling 13).
    if (!dirty) {
      onClose();
      return;
    }
    const next: Attempt = {
      rules: () => [{ name: "rate", label: translate("budget.common.rate"), validate: decimalRate }],
      values: { rate: value },
    };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    update.mutate({ id: rate.id, body: { rate: value } }, { onSuccess: onClose });
  }

  return (
    <Dialog open onClose={onClose} title={t("budget.admin.editRate", { from: rate.from_currency, to: rate.to_currency })}>
      <Form onSubmit={submit}>
        <Field label={t("budget.admin.fxRateAria")}>
          <input className={inputClass} inputMode="decimal" required value={value} onChange={(e) => setValue(e.target.value)} />
        </Field>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("budget.common.cancel")}
          </Button>
          <FormActions
            label={t("budget.common.save")}
            pending={update.isPending}
            error={update.error}
            problems={attemptProblems(attempt)}
          />
        </div>
      </Form>
    </Dialog>
  );
}
