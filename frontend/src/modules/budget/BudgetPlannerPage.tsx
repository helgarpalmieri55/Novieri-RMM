/**
 * Budget planner: one client's fiscal-year budgets and the amounts planned
 * against each category and period.
 *
 * ON THE FORM KIT (redesign Task 11, 6.3; Receta FO). The budget open is in
 * the URL (`?budget=<id>`, the head's chip), so a reload or a link opens the
 * same one; with none chosen the page lists the budgets to choose from. The
 * budget's details (name and status) and its grid of planned amounts are ONE
 * draft (`useDraft`) with ONE `SaveBar`: Save sends the details if they
 * changed (`PATCH /budget/budgets/{id}`) and the grid if it changed
 * (`PUT .../lines`, the same lines as before), and the page asks before it is
 * left with either unsaved -- another budget in the chip is leaving too
 * (`search: ["budget"]`). The grid's old "Save grid" and the details' own
 * Save are gone into the bar. Closing a budget keeps its typed-back
 * confirmation. "New budget" opens from `?new=budget` (decision D8), starts
 * empty however it opens, and opens the budget it created.
 *
 * ONE CLIENT AT A TIME (plan Task 11 D11.1; decision D25): no Client chip;
 * with every client chosen at the top the page asks for one.
 */
import { useQuery } from "@tanstack/react-query";
import { useCallback, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ConfirmDialog } from "../../components/confirm";
import { Form, FormActions, attemptProblems, describeSubmitError } from "../../components/form";
import type { Attempt, FieldRule } from "../../components/form";
import { Button, Dialog, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import {
  useBudgetCategories,
  useBudgetLines,
  useBudgets,
  useCloseBudgetMutation,
  useCreateBudgetMutation,
  useDeleteBudgetLineMutation,
  usePutBudgetLinesMutation,
  useUpdateBudgetMutation,
} from "./api";
import { humaniseEnum } from "../../lib/labels";
import { BudgetPageNav, BudgetPickClient, budgetStatusKind, useBudgetNeedsClient } from "./budgetState";
import { useReportDirty } from "./dialogParams";
import type { BudgetLineInput, BudgetLineOut, BudgetOut, BudgetUpdateStatus, Granularity } from "./types";
import {
  Btn,
  FormSection,
  ListEmpty,
  PageHead,
  Panel,
  QueryState,
  SaveBar,
  StatusPill,
  useDraft,
  useNewParam,
  useUnsavedGuard,
  useUrlFilters,
  writeQuery,
} from "../../shell/components";
import { useRecordTitle } from "../../shell/title";
import { Icon } from "../../shell/icons";

const GRANULARITIES: Granularity[] = ["month", "quarter"];

// periods.py's period_count(granularity) mirrored client-side only for the
// grid's column count -- 12 for monthly, 4 for quarterly (spec §4). The
// server is the sole authority on validating a submitted period_index
// against the budget's actual granularity (budgets.py's put_lines).
function periodCount(granularity: string): number {
  return granularity === "quarter" ? 4 : 12;
}

function cellKey(categoryId: string, periodIndex: number): string {
  return `${categoryId}:${periodIndex}`;
}

// Auditor (HIGH), /budget → New budget line → Save: "Empty submit is a silent
// no-op." And (HIGH), /budget → planner → Fiscal year: "Fiscal year is an
// unbounded number input -- it accepted -3000." One cause,
// `disabled={create.isPending || !name}` on a form with no validation feedback,
// so the two ways of getting this wrong -- leaving a field out and filling it
// with an impossible year -- both ended in the same silence. Save stays enabled
// now and the press produces these sentences; the labels are the words ON
// SCREEN, so "Fiscal year is required" reads back to the field being looked at.
// Built when asked, never held: the labels and sentences follow the language.
const budgetRules = (): readonly FieldRule[] => [
  { name: "name", label: translate("budget.planner.ruleName") },
  { name: "fiscal_year", label: translate("budget.planner.ruleFiscalYear"), validate: invalidFiscalYear },
];

// The range is the server's own -- `budget/schema.py`'s
// `_FiscalYear = Field(ge=2000, le=2100)`, pinned by spec §8 -- rather than a
// second opinion about what a year is. Before this, -3000 travelled and came
// back as a raw Pydantic 422 body.
function invalidFiscalYear(value: string): string | null {
  if (/^-/.test(value)) {
    return translate("budget.planner.yearNegative");
  }
  if (!/^\d{4}$/.test(value)) return translate("budget.planner.yearShape");
  const year = Number(value);
  if (year < 2000 || year > 2100) return translate("budget.planner.yearRange");
  return null;
}

// Renaming a budget needs the name it is being renamed to, and nothing else --
// `status` is a select that always holds one of its two values.
const budgetDetailRules = (): readonly FieldRule[] => [{ name: "name", label: translate("budget.planner.ruleName") }];

/** What the planner edits of one budget: its details and every planned amount, as one draft. */
interface PlanDraft {
  name: string;
  status: BudgetUpdateStatus;
  /** Planned amounts by `cellKey`, as the STRINGS they travel as: money is never parsed here. */
  cells: Record<string, string>;
}

/** The amounts the server holds, keyed by cell, and the line behind each (for its delete control). */
function savedCells(lines: readonly BudgetLineOut[]): { cells: Record<string, string>; ids: Record<string, string> } {
  const cells: Record<string, string> = {};
  const ids: Record<string, string> = {};
  for (const line of lines) {
    if (line.cost_center_id == null) {
      const key = cellKey(line.category_id, line.period_index);
      cells[key] = line.planned_amount;
      ids[key] = line.id;
    }
  }
  return { cells, ids };
}

export function BudgetPlannerPage() {
  const t = useT();
  // A sub-page with no rail entry names itself in the tab, as its h1 does (general ruling 1b).
  useRecordTitle(t("budget.planner.title"));
  const api = useApi();
  // New → Budget (decision D8): `?new=budget` opens the new form.
  const created = useNewParam("budget");
  const filters = useUrlFilters(["budget"] as const);
  const [params, setParams] = useSearchParams();

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canRead = permissions.includes("budget.read");
  const canManage = permissions.includes("budget.manage");
  const needsClient = useBudgetNeedsClient();

  const budgets = useBudgets();
  const items = budgets.data ?? [];
  const selected = items.find((budget) => budget.id === filters.values.budget);

  // THE PAGE'S ONE GUARD (Receta FO, 4): the plan below and the New budget
  // dialog each say whether they hold changes, and one guard asks for both.
  // The plan is left by choosing another budget (`?budget=`); the dialog,
  // which sits over the plan, also by closing it (`?new=`).
  const [planDirty, setPlanDirty] = useState(false);
  const [newDirty, setNewDirty] = useState(false);
  // A BUDGET CREATED BESIDE AN UNSAVED PLAN (fix round 1, I3) is not opened,
  // so the page says it exists and where to find it (fix round 2, N1). The
  // line belongs to the budget open when it was said: another budget drops it.
  const [createdAside, setCreatedAside] = useState<{ name: string; beside: string } | null>(null);
  const shownAside = createdAside !== null && createdAside.beside === filters.values.budget ? createdAside : null;
  const { guard, release } = useUnsavedGuard(planDirty || newDirty, {
    search: newDirty ? ["budget", "new"] : ["budget"],
  });

  const newBudget = canManage ? (
    <Btn label={t("budget.planner.newBudget")} icon="plus" variant="primary" onClick={created.start} />
  ) : undefined;

  // ONE CLIENT AT A TIME (plan Task 11 D11.1): with every client chosen there
  // is no base currency to add these figures in, so the page asks for one.
  if (needsClient) {
    return (
      <div>
        <PageHead title={t("budget.planner.title")} description={t("budget.planner.description")} />
        <BudgetPageNav canRead={canRead} />
        <BudgetPickClient />
      </div>
    );
  }

  return (
    <div>
      <PageHead
        title={t("budget.planner.title")}
        description={t("budget.planner.description")}
        actions={
          <>
            {/* THE BUDGET OPEN, AS A CHIP (the kit's `.chip-select`): a person's
                choice is a new history entry, so Back returns to the last one. */}
            {items.length > 0 ? (
              <span className={selected ? "chip chip-select on" : "chip chip-select"}>
                <span aria-hidden="true">{t("budget.planner.budgetChip")}</span>
                <select
                  aria-label={t("budget.planner.budgetChip")}
                  value={selected?.id ?? ""}
                  onChange={(event) => filters.set("budget", event.target.value)}
                >
                  <option value="">{t("budget.planner.chooseBudget")}</option>
                  {items.map((budget) => (
                    <option key={budget.id} value={budget.id}>
                      {t("budget.planner.budgetOption", { name: budget.name, year: budget.fiscal_year })}
                    </option>
                  ))}
                </select>
              </span>
            ) : null}
            {newBudget}
          </>
        }
      />

      <BudgetPageNav canRead={canRead} />

      {shownAside ? (
        <p role="status" className="notice">
          <Icon name="circle-check" size={16} />
          <span>{t("budget.planner.createdAside", { name: shownAside.name })}</span>
        </p>
      ) : null}

      {/* WHY THERE IS NO BUTTON, said where the button would be. The
          dashboard's "No budget yet" links HERE, and this page offers "New
          budget" only with `budget.manage`: a reader without it would arrive
          at a page with no button and nothing to explain it. Shown only to
          that reader; one who has the permission can see the button. */}
      {!canManage ? <p className="note">{t("budget.planner.needsPermission")}</p> : null}

      {selected ? (
        // KEYED on the budget, so its draft always starts from the budget
        // just chosen rather than carrying edits made to another one.
        <BudgetPlan key={selected.id} budget={selected} canManage={canManage} onDirty={setPlanDirty} />
      ) : (
        <Panel bare>
          <QueryState loading={budgets.isPending} error={budgets.error} empty={false} emptyLabel="">
            {items.length === 0 ? (
              <ListEmpty filtered={false} onClear={() => undefined} nothingYet={t("budget.planner.empty")} icon="budget" action={newBudget} />
            ) : (
              <Table label={t("budget.planner.listLabel")}>
                <thead>
                  <tr>
                    <Th>{t("budget.planner.colName")}</Th>
                    <Th>{t("budget.planner.colFiscalYear")}</Th>
                    <Th>{t("budget.planner.colGranularity")}</Th>
                    <Th>{t("budget.planner.colStatus")}</Th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((budget) => (
                    <tr key={budget.id}>
                      <Td lead={<Icon name="wallet" size={18} />}>
                        {/* The row's own name opens its plan: a link to
                            `?budget=<id>`, so it can be opened in a new tab. */}
                        <div className="cell-title">
                          <Link to={`?budget=${encodeURIComponent(budget.id)}`}>{budget.name}</Link>
                        </div>
                      </Td>
                      <Td>
                        <span className="mono">{budget.fiscal_year}</span>
                      </Td>
                      <Td>{humaniseEnum(budget.granularity, "Granularity")}</Td>
                      <Td>
                        <StatusPill
                          value={budget.status}
                          label={humaniseEnum(budget.status, "BudgetStatus")}
                          kind={budgetStatusKind(budget.status)}
                        />
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            )}
          </QueryState>
        </Panel>
      )}

      {/* Open only for someone who may create one; KEYED on being open, so
          it starts empty however it opens (the button, a link, Back or Forward). */}
      <NewBudgetDialog
        key={created.open && canManage ? "open" : "closed"}
        open={created.open && canManage}
        onClose={created.close}
        onDirty={setNewDirty}
        onCreated={(budget) => {
          // Saved: the dialog's own entry goes without asking, since what it held is stored.
          release();
          setNewDirty(false);
          // In place of the dialog's entry: Back returns to where New was pressed.
          const next = new URLSearchParams(params);
          next.delete("new");
          // THE OPEN PLAN'S CHANGES STAY (fix round 1, I3). With unsaved
          // planned amounts the new budget is created and listed in the chip,
          // but not opened: switching would drop the plan, and the release
          // above is for closing the dialog, never for leaving the plan.
          if (!planDirty) next.set("budget", budget.id);
          setCreatedAside(planDirty ? { name: budget.name, beside: filters.values.budget } : null);
          writeQuery(params, next, setParams, { replace: true });
        }}
      />
      {/* After the dialogs: the leave question shares their `.modal` layer, so it must come later in the DOM to be drawn on top (ruling 3a). */}
      {guard}
    </div>
  );
}

function NewBudgetDialog({
  open,
  onClose,
  onDirty,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onDirty: (dirty: boolean) => void;
  onCreated: (budget: BudgetOut) => void;
}) {
  const t = useT();
  const create = useCreateBudgetMutation();

  const thisYear = String(new Date().getFullYear());
  const [fiscalYear, setFiscalYear] = useState(thisYear);
  const [name, setName] = useState("");
  const [granularity, setGranularity] = useState<Granularity>("month");
  // Filled ON SUBMIT, never while typing: a "Name is required" that appears
  // before the first keystroke has landed is its own kind of unhelpful. Held as
  // the rules and the values they were checked against, worded at render.
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  useReportDirty(open && (name !== "" || fiscalYear !== thisYear || granularity !== "month"), onDirty);

  function submit() {
    const next: Attempt = { rules: budgetRules, values: { name, fiscal_year: fiscalYear } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    // A new attempt starts without the last one's refusal.
    create.reset();
    create.mutate(
      // Trimmed to match what was validated -- `requiredProblems` trims before
      // testing emptiness, so an untrimmed send could carry a name the checks
      // never saw.
      { fiscal_year: Number(fiscalYear), name: name.trim(), granularity },
      { onSuccess: (budget) => onCreated(budget) },
    );
  }

  return (
    <Dialog open={open} onClose={onClose} title={t("budget.planner.newTitle")}>
      {/* A real <form>, so Enter in the Name field saves instead of doing
          nothing -- the other half of the same finding. */}
      <Form onSubmit={submit}>
        <div className="form-grid">
          <Field label={t("budget.planner.name")} full>
            {/* `required` states the same fact for assistive tech. It blocks
                nothing: <Form> is `noValidate`, because a native bubble that
                vanishes on the next click is the silent no-op wearing a hat.
                `budgetRules` is what the user reads. */}
            <input className={inputClass} required value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label={t("budget.planner.fiscalYear")}>
            {/* `min`/`max` are the server's range stated to the spinner; the
                refusal a typed or pasted -3000 gets is `invalidFiscalYear`'s. */}
            <input
              type="number"
              min={2000}
              max={2100}
              className={inputClass}
              required
              value={fiscalYear}
              onChange={(e) => setFiscalYear(e.target.value)}
            />
          </Field>
          <Field label={t("budget.planner.granularity")}>
            <select className={inputClass} value={granularity} onChange={(e) => setGranularity(e.target.value as Granularity)}>
              {GRANULARITIES.map((g) => (
                <option key={g} value={g}>
                  {humaniseEnum(g, "Granularity")}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <div className="flex justify-end gap-2">
          {/* `type="button"` is load-bearing: a button with no type inside a
              <form> IS a submit button, so Cancel would save. */}
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("budget.common.cancel")}
          </Button>
          {/* Enabled with the fields empty ON PURPOSE -- the refusal is what
              the user gets to read, and a control that will not move is not. */}
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
 * One budget's plan: its details (name and status, and the one-way Close) and
 * the grid of planned amounts, one draft and one save bar.
 */
function BudgetPlan({
  budget,
  canManage,
  onDirty,
}: {
  budget: BudgetOut;
  canManage: boolean;
  onDirty: (dirty: boolean) => void;
}) {
  const t = useT();
  const categories = useBudgetCategories();
  const lines = useBudgetLines(budget.id);
  const update = useUpdateBudgetMutation();
  const putLines = usePutBudgetLinesMutation();
  const close = useCloseBudgetMutation();
  const deleteLine = useDeleteBudgetLineMutation();

  const isClosed = budget.status === "closed";
  const canEdit = canManage && !isClosed;
  const server = useMemo(() => savedCells(lines.data ?? []), [lines.data]);
  const saved: PlanDraft | undefined = lines.data
    ? { name: budget.name, status: budget.status === "active" ? "active" : "draft", cells: server.cells }
    : undefined;
  const { draft, setDraft, dirty, discard, markSent } = useDraft(saved);
  useReportDirty(dirty, onDirty);
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  // Cycle 8. Closing was one click; it asks now, the name typed back (below).
  const [confirmingClose, setConfirmingClose] = useState(false);
  // Cycle 8. The `×` deleted a persisted planned amount on one click.
  const [pendingDelete, setPendingDelete] = useState<
    { key: string; categoryName: string; period: number; amount: string } | null
  >(null);

  const periods = Array.from({ length: periodCount(budget.granularity) }, (_, i) => i + 1);
  const activeCategories = categories.data?.filter((c) => c.active) ?? [];

  const setCell = useCallback(
    (key: string, value: string) => {
      if (draft) setDraft({ ...draft, cells: { ...draft.cells, [key]: value } });
    },
    [draft, setDraft],
  );

  function save() {
    if (!draft || !saved) return;
    const detailsChanged = draft.name !== saved.name || draft.status !== saved.status;
    const cellsChanged = JSON.stringify(draft.cells) !== JSON.stringify(saved.cells);
    if (detailsChanged) {
      // The same silent no-op as the New budget dialog, one panel down: the
      // name could be cleared and the only response was a control that
      // stopped working. The bar says which field is at fault.
      const next: Attempt = { rules: budgetDetailRules, values: { name: draft.name } };
      setAttempt(next);
      if (attemptProblems(next).length > 0) return;
    }
    setAttempt(null);
    // A new attempt starts without the last one's refusal.
    update.reset();
    putLines.reset();
    markSent(draft);
    const failed = () => markSent(null);
    if (detailsChanged) {
      update.mutate({ id: budget.id, body: { name: draft.name.trim(), status: draft.status } }, { onError: failed });
    }
    if (cellsChanged) {
      // THE SAME LINES AS BEFORE: every active category and period with an
      // amount typed in, as the string it was typed as.
      const toSave: BudgetLineInput[] = [];
      for (const category of activeCategories) {
        for (const period of periods) {
          const value = draft.cells[cellKey(category.id, period)];
          if (value != null && value !== "") {
            toSave.push({ category_id: category.id, period_index: period, planned_amount: value });
          }
        }
      }
      putLines.mutate({ budgetId: budget.id, lines: toSave }, { onError: failed });
    }
  }

  function removeLine(key: string) {
    const lineId = server.ids[key];
    if (lineId == null) return;
    deleteLine.mutate(
      { budgetId: budget.id, lineId },
      {
        onSuccess: () => {
          // The cell goes from the draft too, so the deleted amount does not
          // stay on screen reading as an unsaved one.
          if (!draft) return;
          const cells = { ...draft.cells };
          delete cells[key];
          setDraft({ ...draft, cells });
        },
      },
    );
  }

  const saving = update.isPending || putLines.isPending;
  const error = update.error ?? putLines.error;

  return (
    <>
      <FormSection title={t("budget.planner.detailsTitle")} icon="wallet">
        {isClosed ? (
          <p className="notice">
            <Icon name="lock" size={16} />
            <span>{t("budget.planner.closedNotice")}</span>
          </p>
        ) : canManage && draft ? (
          // A real <form>, so Enter in the Name field saves, as before.
          <Form onSubmit={save}>
            <div className="form-grid">
              <Field label={t("budget.planner.name")}>
                <input
                  className={inputClass}
                  required
                  value={draft.name}
                  onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                />
              </Field>
              <Field label={t("budget.planner.status")}>
                <select
                  className={inputClass}
                  value={draft.status}
                  onChange={(e) => setDraft({ ...draft, status: e.target.value as BudgetUpdateStatus })}
                >
                  <option value="draft">{humaniseEnum("draft", "BudgetStatus")}</option>
                  <option value="active">{humaniseEnum("active", "BudgetStatus")}</option>
                </select>
              </Field>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {/* `type="button"`: Close budget is one-way and irreversible, and
                  a button with no type inside a <form> IS a submit button --
                  Enter in the Name field would have closed the budget. */}
              <Button type="button" variant="outline" onClick={() => setConfirmingClose(true)} disabled={close.isPending}>
                {close.isPending ? t("budget.planner.closing") : t("budget.planner.close")}
              </Button>
              <span className="note">{t("budget.planner.closeNote")}</span>
            </div>
            {close.isError && (
              <p role="alert" className="notice red">
                <Icon name="circle-x" size={16} />
                <span>{describeSubmitError(close.error)}</span>
              </p>
            )}
          </Form>
        ) : (
          <p className="note">{t("budget.planner.detailsReadOnly", { name: budget.name, year: budget.fiscal_year })}</p>
        )}
      </FormSection>

      <Panel title={t("budget.planner.gridTitle")} bare>
        <QueryState
          loading={lines.isPending || categories.isPending}
          error={lines.error ?? categories.error}
          empty={activeCategories.length === 0}
          emptyLabel={t("budget.planner.gridEmpty")}
          emptyIcon="tag"
        >
          <Table label={t("budget.planner.gridTitle")}>
            <thead>
              <tr>
                <Th>{t("budget.planner.colCategory")}</Th>
                {periods.map((p) => (
                  <Th key={p}>{`P${p}`}</Th>
                ))}
              </tr>
            </thead>
            <tbody>
              {activeCategories.map((category) => (
                <tr key={category.id}>
                  <Td lead={<Icon name="tag" size={18} />}>
                    <div className="cell-title">{category.name}</div>
                  </Td>
                  {periods.map((period) => {
                    const key = cellKey(category.id, period);
                    return (
                      <Td key={key}>
                        <div className="flex items-center gap-1">
                          <input
                            aria-label={t("budget.planner.cellAria", { category: category.name, period })}
                            className={inputClass}
                            style={{ minWidth: 96 }}
                            value={draft?.cells[key] ?? ""}
                            onChange={(e) => setCell(key, e.target.value)}
                            disabled={!canEdit}
                          />
                          {canEdit && server.ids[key] != null && (
                            /* A real button with a boundary and a name that
                               says which cell (SC 1.4.11): a grid of these all
                               reading "Delete" would be identical buttons. */
                            <Btn
                              small
                              variant="ghost"
                              icon="close"
                              iconOnly
                              label={t("budget.planner.deleteAria", { category: category.name, period })}
                              onClick={() =>
                                setPendingDelete({
                                  key,
                                  categoryName: category.name,
                                  period,
                                  amount: server.cells[key] ?? "",
                                })
                              }
                            />
                          )}
                        </div>
                      </Td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </Table>
        </QueryState>
        {deleteLine.isError && (
          <p role="alert" className="notice red" style={{ margin: 12 }}>
            <Icon name="circle-x" size={16} />
            <span>{describeSubmitError(deleteLine.error)}</span>
          </p>
        )}
      </Panel>

      <SaveBar
        dirty={dirty}
        saving={saving}
        error={error}
        problems={attemptProblems(attempt)}
        saved={(update.isSuccess || putLines.isSuccess) && !dirty}
        onDiscard={() => {
          setAttempt(null);
          discard();
        }}
        onSave={save}
      />

      {/* The subject is the budget's name AND fiscal year: names often differ
          only by the year, and closing the wrong one freezes the year people
          are still planning. `requireTyping` because there is no reopen --
          not through the API, not through this console. */}
      <ConfirmDialog
        open={confirmingClose}
        title={t("budget.planner.close")}
        subject={t("budget.planner.closeSubject", { name: budget.name, year: budget.fiscal_year })}
        body={t("budget.planner.closeBody")}
        confirmLabel={t("budget.planner.close")}
        destructive
        requireTyping
        onCancel={() => setConfirmingClose(false)}
        onConfirm={() => {
          setConfirmingClose(false);
          close.reset();
          close.mutate(budget.id);
        }}
      />
      {/* The subject names the cell -- category and period -- and the amount
          in it, because the grid's cells are indistinguishable at a glance. */}
      <ConfirmDialog
        open={pendingDelete != null}
        title={t("budget.planner.deleteTitle")}
        subject={
          pendingDelete
            ? pendingDelete.amount
              ? t("budget.planner.deleteSubjectAmount", {
                  category: pendingDelete.categoryName,
                  period: pendingDelete.period,
                  amount: pendingDelete.amount,
                })
              : t("budget.planner.deleteSubject", { category: pendingDelete.categoryName, period: pendingDelete.period })
            : ""
        }
        body={t("budget.planner.deleteBody")}
        confirmLabel={t("budget.planner.deleteConfirm")}
        destructive
        onCancel={() => setPendingDelete(null)}
        onConfirm={() => {
          if (pendingDelete) {
            deleteLine.reset();
            removeLine(pendingDelete.key);
          }
          setPendingDelete(null);
        }}
      />
    </>
  );
}
