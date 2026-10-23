import { useQuery } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { attemptProblems, describeSubmitError, Form, FormActions } from "../../components/form";
import { NoValue } from "../../components/emptystate";
import type { Attempt, FieldRule } from "../../components/form";
import { EntityPicker } from "../../components/picker";
import { Button, Dialog, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDate } from "../../lib/datetime";
import {
  useBudgetCategories,
  useCostCenters,
  useCreateExpenseMutation,
  useExpenses,
  useMaterializeExpensesMutation,
  useUpdateExpenseMutation,
  useUploadExpenseAttachmentMutation,
} from "./api";
import { BudgetPageNav, expenseSourceKind, expenseStatusKind } from "./budgetState";
import {
  categoryPickerEmptyHint,
  categoryPickerOptions,
  costCenterPickerEmptyHint,
  costCenterPickerOptions,
} from "./pickerOptions";
import type { ExpenseListFilters, ExpenseOut } from "./types";
import { FilterBar, PageHead, Panel, QueryState, StatusPill } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { humaniseEnum } from "../../lib/labels";

const STATUS_FILTERS = ["", "expected", "confirmed", "void"] as const;

/**
 * What an expense may not be recorded without, and what each field may not
 * contain.
 *
 * The Save control read `disabled={create.isPending || !categoryId}` — the
 * silent no-op the audits counted across six modules, and here it was reached
 * on every fresh install because the Category list it demanded was empty. Save
 * stays enabled now and the press says which field is at fault.
 *
 * `amount` and `currency` are checked for the same reason as the money fields
 * in /licenses and /subscriptions: money is a Decimal server-side and travels
 * as a STRING (types.ts / invariant #6), so nothing between the keyboard and
 * pydantic parses "1,200" or "£1200" — they were accepted by the input,
 * rejected by the server, and the 422 was rendered as `String(create.error)`,
 * the raw-Pydantic-JSON BLOCKER.
 */
// Built when asked, never held: the labels and sentences follow the language.
const expenseRules = (): readonly FieldRule[] => [
  { name: "expense_date", label: translate("budget.expenses.ruleDate"), validate: isoDate },
  { name: "amount", label: translate("budget.expenses.ruleAmount"), validate: decimalAmount },
  { name: "currency", label: translate("budget.expenses.ruleCurrency"), validate: currencyCode },
  { name: "category_id", label: translate("budget.expenses.ruleCategory") },
];

function decimalAmount(value: string): string | null {
  // No symbol, no thousands separator: the string is forwarded verbatim as the
  // Decimal, so whatever is typed here is what the backend parses.
  if (!/^\d+(\.\d{1,2})?$/.test(value)) {
    return translate("budget.expenses.amountFigure");
  }
  return null;
}

function currencyCode(value: string): string | null {
  // ISO 4217 is three letters; `maxLength={3}` accepts "US" and "$" happily,
  // and either would be stored as a currency this module then cannot convert.
  if (!/^[A-Za-z]{3}$/.test(value)) return translate("budget.expenses.currencyCode");
  return null;
}

function isoDate(value: string): string | null {
  // A `type="date"` input yields "" or an ISO date, so this only fires when
  // the field has been cleared — but the FX conversion is looked up BY this
  // date (spec §3), so an absent one is not a cosmetic omission.
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return translate("budget.expenses.dateShape");
  return null;
}

export function ExpensesPage() {
  const t = useT();
  const api = useApi();
  const [creating, setCreating] = useState(false);
  const [editingExpense, setEditingExpense] = useState<ExpenseOut | null>(null);
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [categoryFilter, setCategoryFilter] = useState<string>("");

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canRead = permissions.includes("budget.read");
  const canRecord = permissions.includes("expense.record");

  const categories = useBudgetCategories();
  const categoryName = (id: string) => categories.data?.find((c) => c.id === id)?.name ?? id;

  const filters: ExpenseListFilters = {
    status: statusFilter || undefined,
    category_id: categoryFilter || undefined,
  };
  const expenses = useExpenses(filters);
  const uploadAttachment = useUploadExpenseAttachmentMutation();
  const items = expenses.data ?? [];

  return (
    <div>
      <PageHead
        title={t("budget.expenses.title")}
        description={t("budget.expenses.description")}
        actions={
          <>
            {canRecord && <MaterializeControl />}
            {canRecord && (
              <Button onClick={() => setCreating(true)}>
                <Icon name="plus" size={14} />
                {t("budget.expenses.newExpense")}
              </Button>
            )}
          </>
        }
      />

      <BudgetPageNav canRead={canRead} />

      <Panel bare>
        <FilterBar>
          <select
            className="select-field"
            aria-label={t("budget.expenses.filterStatus")}
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
          >
            {STATUS_FILTERS.map((s) => (
              <option key={s} value={s}>
                {s === "" ? t("budget.expenses.allStatuses") : humaniseEnum(s, "ExpenseStatus")}
              </option>
            ))}
          </select>
          <select
            className="select-field"
            aria-label={t("budget.expenses.filterCategory")}
            value={categoryFilter}
            onChange={(e) => setCategoryFilter(e.target.value)}
          >
            <option value="">{t("budget.expenses.allCategories")}</option>
            {categories.data?.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
          <span className="filter-spacer" />
          {expenses.data ? <span className="note">{t("budget.expenses.count", { count: items.length })}</span> : null}
        </FilterBar>

        <QueryState
          loading={expenses.isLoading}
          error={expenses.error}
          empty={items.length === 0}
          emptyLabel={t("budget.expenses.empty")}
          emptyIcon="budget"
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("budget.expenses.colExpense")}</Th>
                <Th>{t("budget.expenses.colAmount")}</Th>
                <Th>{t("budget.expenses.colStatus")}</Th>
                <Th>{t("budget.expenses.colSource")}</Th>
                <Th>{t("budget.expenses.colDate")}</Th>
                <Th>{t("budget.expenses.colAttachment")}</Th>
                {canRecord && <Th />}
              </tr>
            </thead>
            <tbody>
              {items.map((expense) => (
                <ExpenseRow
                  key={expense.id}
                  expense={expense}
                  categoryName={categoryName(expense.category_id)}
                  canRecord={canRecord}
                  onUpload={(file) => uploadAttachment.mutate({ id: expense.id, file })}
                  onEdit={() => setEditingExpense(expense)}
                />
              ))}
            </tbody>
          </Table>
        </QueryState>

        {/* SWALLOWED ENTIRELY BEFORE THIS. `uploadAttachment.mutate(...)` had
            no `onError` and its state was rendered nowhere, so a refused
            upload -- 413 for an oversized invoice, 403, 503 from a proxy --
            looked exactly like a successful one: the file dialog closed, the
            row did not change, and the operator had no reason to think the
            invoice was not attached until an audit asked for it.
            Auditor A's swallowed-failure class, on this route. */}
        {uploadAttachment.isError && (
          <p role="alert" className="notice red" style={{ margin: 12 }}>
            <Icon name="circle-x" size={16} />
            <span>{describeSubmitError(uploadAttachment.error)}</span>
          </p>
        )}
        {uploadAttachment.isSuccess && (
          <p role="status" className="note" style={{ margin: 12 }}>
            {t("budget.expenses.invoiceAttached")}
          </p>
        )}
      </Panel>

      <NewExpenseDialog open={creating} onClose={() => setCreating(false)} />
      {editingExpense && (
        <EditExpenseDialog
          key={editingExpense.id}
          expense={editingExpense}
          onClose={() => setEditingExpense(null)}
        />
      )}
    </div>
  );
}

// A form + counts panel for `POST /budget/expenses/materialize` -- the
// module's headline auto-feed (spec §6.3). Deliberately its own explicit
// operator action, never a background sweep, so the three counts it returns
// (created/skipped_existing/skipped_unconvertible) are the whole point: an
// operator who cannot see them cannot tell a no-op from a failure.
function MaterializeControl() {
  const t = useT();
  const materialize = useMaterializeExpensesMutation();
  const [open, setOpen] = useState(false);
  const today = new Date().toISOString().slice(0, 10);
  const [fromDate, setFromDate] = useState(today);
  const [toDate, setToDate] = useState(today);

  function submit() {
    materialize.mutate(
      { from_date: fromDate, to_date: toDate },
      { onSuccess: () => setOpen(false) },
    );
  }

  return (
    <>
      <Button variant="outline" onClick={() => setOpen(true)}>
        {t("budget.expenses.addExpected")}
      </Button>
      <Dialog open={open} onClose={() => setOpen(false)} title={t("budget.expenses.addExpected")}>
        <p className="note" style={{ marginBottom: 12 }}>
          {t("budget.expenses.materializeNote")}
        </p>
        {/* BOTH ENDS, READ BACK IN THE CONSOLE'S OWN FORMAT. A native date
            input draws the USER AGENT'S mask and no CSS reorders it, so these
            read `09/22/2026` to a US-locale browser on a console that renders
            every other date `22 Sept 2026` (design review M1). The window
            decides which rows get written into the confirmation queue, so a
            month misread here is rows nobody asked for. */}
        <div className="form-grid">
          <Field label={t("budget.expenses.fromDate")}>
            <input
              type="date"
              className={inputClass}
              value={fromDate}
              onChange={(e) => setFromDate(e.target.value)}
            />
          </Field>
          <Field label={t("budget.expenses.toDate")}>
            <input
              type="date"
              className={inputClass}
              value={toDate}
              onChange={(e) => setToDate(e.target.value)}
            />
          </Field>
        </div>
        {fromDate !== "" || toDate !== "" ? (
          <p className="note" style={{ marginTop: 14 }}>
            {fromDate !== "" && toDate !== ""
              ? t("budget.expenses.rangeBoth", {
                  from: formatDate(`${fromDate}T00:00:00Z`, "UTC"),
                  to: formatDate(`${toDate}T00:00:00Z`, "UTC"),
                })
              : fromDate !== ""
                ? t("budget.expenses.rangeNoEnd", { from: formatDate(`${fromDate}T00:00:00Z`, "UTC") })
                : t("budget.expenses.rangeNoStart", { to: formatDate(`${toDate}T00:00:00Z`, "UTC") })}
          </p>
        ) : null}
        {/* `String(materialize.error)` rendered "ApiError: " for a bodyless
            503 and was in no live region -- on the control that writes rows
            into the confirmation queue, where a failure looks exactly like a
            window with nothing to feed. */}
        {materialize.isError && (
          <p role="alert" className="notice red" style={{ marginBottom: 12 }}>
            <Icon name="circle-x" size={16} />
            <span>{describeSubmitError(materialize.error)}</span>
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => setOpen(false)}>
            {t("budget.common.cancel")}
          </Button>
          <Button onClick={submit} disabled={materialize.isPending}>
            {materialize.isPending ? t("budget.expenses.running") : t("budget.expenses.run")}
          </Button>
        </div>
      </Dialog>
      {/* The three counts are why this is an explicit action rather than a
          background sweep -- an operator who cannot see them cannot tell a
          no-op from a failure. */}
      {materialize.isSuccess && materialize.data && (
        <p role="status" className="note">
          {t("budget.expenses.materialized", {
            created: materialize.data.created,
            existing: materialize.data.skipped_existing,
            unconvertible: materialize.data.skipped_unconvertible,
          })}
        </p>
      )}
    </>
  );
}

function ExpenseRow({
  expense,
  categoryName,
  canRecord,
  onUpload,
  onEdit,
}: {
  expense: ExpenseOut;
  categoryName: string;
  canRecord: boolean;
  onUpload: (file: File) => void;
  onEdit: () => void;
}) {
  const t = useT();
  const fileInput = useRef<HTMLInputElement>(null);

  const kind = expenseStatusKind(expense.status);

  return (
    <tr>
      {/* v3's row: the glyph, the description as the title, and the category
          and invoice reference under it. An expense with no description is
          titled by its category so the row is never blank. */}
      <Td lead={<Icon name="receipt" size={18} />} gutter={kind}>
        <div className="cell-title">{expense.description || categoryName}</div>
        <div className="cell-sub">
          {[expense.description ? categoryName : null, expense.invoice_ref].filter(Boolean).join(" · ") ||
            t("budget.expenses.noInvoiceRef")}
        </div>
      </Td>
      {/* A single text node (template literal, not JSX-expression
          concatenation) -- string concatenation of two Decimal-as-string
          fields, never Number() arithmetic. */}
      <Td>
        <span className="mono" style={{ whiteSpace: "nowrap" }}>
          {`${expense.amount} ${expense.currency}`}
        </span>
      </Td>
      <Td>
        <StatusPill value={expense.status} label={humaniseEnum(expense.status, "ExpenseStatus")} kind={kind} />
      </Td>
      <Td>
        <StatusPill
          value={expense.source}
          label={humaniseEnum(expense.source, "ExpenseSource")}
          kind={expenseSourceKind(expense.source)}
        />
      </Td>
      {/* A CALENDAR DATE, NOT AN INSTANT (cycle 5). `expense_date` is a `date`
          in schema.py -- "2026-07-01", a day with no time and no zone -- and it
          was printed as that raw ISO string, the one date in this module a
          finance reader compares against an invoice. Rendered in the product's
          one date format and PINNED TO UTC: through a zone west of UTC the day
          would move back one, which on the boundary of a fiscal period files
          the money in the wrong month. */}
      <Td>
        <span style={{ whiteSpace: "nowrap" }}>{formatDate(expense.expense_date, "UTC")}</span>
      </Td>
      <Td>
        {expense.has_attachment ? (
          <span className="flex items-center gap-1">
            <Icon name="paperclip" size={14} />
            <span style={{ whiteSpace: "nowrap" }}>{expense.invoice_filename ?? t("budget.expenses.attached")}</span>
          </span>
        ) : (
          <NoValue label={t("budget.expenses.noAttachment")} />
        )}
      </Td>
      {canRecord && (
        <Td>
          <div className="flex gap-2">
            {/* Void is terminal (spec §5) -- editing a voided row is
                rejected server-side (expenses.reject_if_void), so the
                control is not offered at all. */}
            {expense.status !== "void" && (
              <Button variant="outline" onClick={onEdit}>
                {t("budget.common.edit")}
              </Button>
            )}
            <input
              ref={fileInput}
              type="file"
              className="hidden"
              aria-label={t("budget.expenses.attachAria", { category: categoryName, date: expense.expense_date })}
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) onUpload(file);
              }}
            />
            <Button variant="outline" onClick={() => fileInput.current?.click()}>
              {expense.has_attachment ? t("budget.expenses.replace") : t("budget.expenses.attach")}
            </Button>
          </div>
        </Td>
      )}
    </tr>
  );
}

// Edits the two safe, non-monetary fields of an existing expense
// (description, invoice reference) via `PATCH /budget/expenses/{id}`. Amount/
// currency/category/cost-center edits are deliberately out of scope here --
// the New/Edit split keeps this dialog from re-deriving the FX-conversion
// side effects `update_expense` runs when amount/currency/date change.
function EditExpenseDialog({ expense, onClose }: { expense: ExpenseOut; onClose: () => void }) {
  const t = useT();
  const update = useUpdateExpenseMutation();
  const [description, setDescription] = useState(expense.description);
  const [invoiceRef, setInvoiceRef] = useState(expense.invoice_ref ?? "");

  function submit() {
    update.mutate(
      { id: expense.id, body: { description, invoice_ref: invoiceRef || null } },
      { onSuccess: () => onClose() },
    );
  }

  return (
    <Dialog open onClose={onClose} title={t("budget.expenses.editTitle")}>
      <Form onSubmit={submit}>
        <div className="form-grid">
          <Field label={t("budget.common.description")}>
            <input
              className={inputClass}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </Field>
          <Field label={t("budget.common.invoiceReference")}>
            <input
              className={inputClass}
              value={invoiceRef}
              onChange={(e) => setInvoiceRef(e.target.value)}
            />
          </Field>
        </div>
        {/* Both fields are genuinely optional here (clearing a description is
            a legitimate edit), so there are no rules -- but the SERVER failure
            still had to stop being `String(update.error)`: a bodyless 503 read
            "Error: " and nothing after it, announced to nobody. */}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("budget.common.cancel")}
          </Button>
          <FormActions label={t("budget.common.save")} pending={update.isPending} error={update.error} />
        </div>
      </Form>
    </Dialog>
  );
}

function NewExpenseDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const t = useT();
  const create = useCreateExpenseMutation();
  const categories = useBudgetCategories();
  const costCenters = useCostCenters();

  // Not memoised: the sublabels are worded, and a held list would keep the
  // language it was built in.
  const categoryOptions = categoryPickerOptions(categories.data ?? []);
  const costCenterOptions = costCenterPickerOptions(costCenters.data ?? []);
  // A list still in flight is not an empty list: offering "go and create one"
  // during the first fetch sends people to fix a table that is already full.
  const categoriesEmpty = !categories.isLoading && categoryOptions.length === 0;
  const costCentersEmpty = !costCenters.isLoading && costCenterOptions.length === 0;

  const today = new Date().toISOString().slice(0, 10);
  const [expenseDate, setExpenseDate] = useState(today);
  const [amount, setAmount] = useState("0.00");
  const [currency, setCurrency] = useState("USD");
  const [categoryId, setCategoryId] = useState("");
  const [costCenterId, setCostCenterId] = useState("");
  const [description, setDescription] = useState("");
  const [invoiceRef, setInvoiceRef] = useState("");
  // Populated ON SUBMIT, never while typing -- complaining about a field the
  // operator has not finished filling in is its own kind of unhelpful. Held as
  // the rules and the values they were checked against, worded at render.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  function reset() {
    setExpenseDate(today);
    setAmount("0.00");
    setCurrency("USD");
    setCategoryId("");
    setCostCenterId("");
    setDescription("");
    setInvoiceRef("");
    setAttempt(null);
  }

  // ExpensesPage keeps this dialog mounted and toggles `open`, so without
  // clearing the mutation a failure from one attempt sits in the form waiting
  // to be read as the next one's.
  function close() {
    create.reset();
    setAttempt(null);
    onClose();
  }

  function submit() {
    const next: Attempt = {
      rules: expenseRules,
      values: { expense_date: expenseDate, amount, currency, category_id: categoryId },
    };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;

    create.mutate(
      {
        expense_date: expenseDate,
        // Money is a Decimal server-side and travels as a string -- the
        // typed value from the input, never Number()-converted.
        amount,
        currency,
        category_id: categoryId,
        cost_center_id: costCenterId || null,
        description,
        invoice_ref: invoiceRef || null,
        status: "expected",
      },
      { onSuccess: () => { reset(); close(); } },
    );
  }

  return (
    <Dialog open={open} onClose={close} title={t("budget.expenses.newTitle")}>
      {/* A real <form>, so Enter in the Amount field saves instead of doing
          nothing -- the other half of the silent-no-op finding. */}
      <Form onSubmit={submit}>
        <div className="form-grid">
          <Field label={t("budget.expenses.expenseDate")}>
            <input
              type="date"
              className={inputClass}
              required
              value={expenseDate}
              onChange={(e) => setExpenseDate(e.target.value)}
            />
            {/* The date this holds is spelled out under the grid, below (design
                review M1). It is not inside this `<Field>` because `Field`
                wraps its control in the `<label>` and text placed here becomes
                part of the field's NAME; the accounting is at
                `agent-push/PushAuthorizationsPage`. "UTC" there and below
                because this is a calendar DATE, not an instant -- rendering it
                through a zone moves it a day west of Greenwich, which is why
                the list on this page already reads it that way. */}
          </Field>
          <Field label={t("budget.expenses.amount")}>
            {/* Text with a decimal keypad, not type="number": the value is a
                Decimal string on the wire, and a number input would hand back a
                locale-formatted or exponent-notation value to re-serialise. */}
            <input
              className={inputClass}
              type="text"
              inputMode="decimal"
              required
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
            />
          </Field>
          <Field label={t("budget.common.currency")}>
            <input
              className={inputClass}
              value={currency}
              maxLength={3}
              required
              onChange={(e) => setCurrency(e.target.value.toUpperCase())}
            />
          </Field>
          {/* THE BLOCKED FLOW. The Category `<select>` held one option -- "Select
              a category" -- until somebody had visited the Admin tab, and Save
              was disabled until one was chosen, so a fresh install met a form it
              could not complete and no sentence saying why. `emptyHint` says
              what would fill it; the link goes to the tab that does. */}
          <EntityPicker
            label={t("budget.common.category")}
            options={categoryOptions}
            loading={categories.isLoading}
            emptyHint={categoryPickerEmptyHint()}
            placeholder={t("budget.expenses.searchCategories")}
            value={categoryId}
            onChange={setCategoryId}
          />
          {categoriesEmpty ? (
            <p className="note">
              <Link className="panel-link" to="/budget/admin">
                {t("budget.expenses.createCategory")}
              </Link>
            </p>
          ) : null}
          <EntityPicker
            label={t("budget.expenses.costCenter")}
            options={costCenterOptions}
            loading={costCenters.isLoading}
            emptyHint={costCenterPickerEmptyHint()}
            placeholder={t("budget.expenses.searchCostCenters")}
            value={costCenterId}
            onChange={setCostCenterId}
          />
          {costCentersEmpty ? (
            <p className="note">
              <Link className="panel-link" to="/budget/admin">
                {t("budget.expenses.createCostCenter")}
              </Link>
            </p>
          ) : null}
          <Field label={t("budget.common.description")}>
            <input
              className={inputClass}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </Field>
          <Field label={t("budget.common.invoiceReference")}>
            <input
              className={inputClass}
              value={invoiceRef}
              onChange={(e) => setInvoiceRef(e.target.value)}
            />
          </Field>
        </div>
        {expenseDate !== "" ? (
          <p className="note" style={{ marginTop: 14, marginBottom: 14 }}>
            {t("budget.expenses.dated", { date: formatDate(`${expenseDate}T00:00:00Z`, "UTC") })}
          </p>
        ) : null}
        <div className="flex justify-end gap-2">
          {/* `type="button"` is load-bearing now that there is a form around
              it: a button with no type inside a <form> IS a submit button, so
              Cancel would record the expense. */}
          <Button type="button" variant="ghost" onClick={close}>
            {t("budget.common.cancel")}
          </Button>
          {/* Enabled with no category chosen ON PURPOSE, and the server failure
              now goes through `describeSubmitError` inside a `role="alert"` --
              `String(create.error)` printed the raw Pydantic payload, the
              BLOCKER Auditor B raised on the strength of the pattern. */}
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
