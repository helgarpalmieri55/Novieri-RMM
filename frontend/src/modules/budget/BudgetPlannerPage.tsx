import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { ConfirmDialog } from "../../components/confirm";
import {
  Form,
  FormActions,
  attemptProblems,
  describeSubmitError,
} from "../../components/form";
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
import { BudgetPageNav, budgetStatusKind } from "./budgetState";
import type { BudgetLineInput, BudgetOut, BudgetUpdateStatus, Granularity } from "./types";
import { Btn, PageHead, Panel, QueryState, StatusPill } from "../../shell/components";
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

export function BudgetPlannerPage() {
  const t = useT();
  const api = useApi();
  const [creating, setCreating] = useState(false);
  const [selectedBudgetId, setSelectedBudgetId] = useState<string | undefined>(undefined);

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canRead = permissions.includes("budget.read");
  const canManage = permissions.includes("budget.manage");

  const budgets = useBudgets();
  const items = budgets.data ?? [];

  return (
    <div>
      <PageHead
        title={t("budget.planner.title")}
        description={t("budget.planner.description")}
        actions={
          canManage ? (
            <Button onClick={() => setCreating(true)}>
              <Icon name="plus" size={14} />
              {t("budget.planner.newBudget")}
            </Button>
          ) : undefined
        }
      />

      <BudgetPageNav canRead={canRead} />

      {/* WHY THERE IS NO BUTTON, said where the button would be.
          The copy pass cut `budget.manage` out of the dashboard's "No budget
          yet" empty state, correctly -- an empty state is one sentence and an
          action, not a permissions lesson. But the action it offers is a link
          HERE, and this page renders "New budget" only for `budget.manage`, so
          a reader without it followed the link into a page with no button and
          nothing to explain it. A dead end is a worse defect than the long
          sentence that was removed.

          Conditional, and only on the page that actually withholds the
          control: an operator who HAS the permission is told nothing, because
          they can see the button. */}
      {!canManage ? (
        <p className="note">
          {t("budget.planner.needsPermission")}
        </p>
      ) : null}

      <Panel bare>
        <QueryState
          loading={budgets.isLoading}
          error={budgets.error}
          empty={items.length === 0}
          emptyLabel={t("budget.planner.empty")}
          emptyIcon="budget"
        >
          <Table>
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
                    {/* DELIBERATELY NOT A `Btn`. This is the row's own name,
                        and selecting it is what opens the budget below;
                        thirty bordered 37px buttons down a name column would
                        turn the table into a control panel. It is the row
                        title, at the row title's size and weight -- but IN THE
                        ACCENT, like every other row title that opens something
                        (the subscription service names, the IPAM prefixes).
                        `cell-title` used to be on this control too and won the
                        colour off `panel-link` on source order, so the one
                        control that opens a budget painted as plain ink and
                        the page read as a static three-row list. The three
                        focus-visible utilities restate base.css's universal
                        `:focus-visible` ring (2px, offset 2px, accent) so the
                        jsx-tokens guard, which reads source text and cannot
                        see the cascade, finds the ring where the control is
                        written. */}
                    <button
                      type="button"
                      className="panel-link focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]"
                      style={{ fontSize: "var(--t-body)", fontWeight: 600, padding: 0 }}
                      aria-pressed={selectedBudgetId === budget.id}
                      onClick={() => setSelectedBudgetId(budget.id)}
                    >
                      {budget.name}
                    </button>
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
        </QueryState>
      </Panel>

      {selectedBudgetId && (() => {
        const selectedBudget = items.find((b) => b.id === selectedBudgetId);
        if (selectedBudget == null) return null;
        return (
          <>
            {/* `key` forces a remount on budget switch so the name/status
                draft below always starts from the freshly-selected budget
                instead of carrying over edits made to a different one. */}
            <BudgetDetailCard key={selectedBudget.id} budget={selectedBudget} canManage={canManage} />
            <LineGrid
              budgetId={selectedBudget.id}
              granularity={selectedBudget.granularity}
              canManage={canManage}
              closed={selectedBudget.status === "closed"}
            />
          </>
        );
      })()}

      <NewBudgetDialog open={creating} onClose={() => setCreating(false)} />
    </div>
  );
}

function NewBudgetDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const t = useT();
  const create = useCreateBudgetMutation();

  const [fiscalYear, setFiscalYear] = useState(String(new Date().getFullYear()));
  const [name, setName] = useState("");
  const [granularity, setGranularity] = useState<Granularity>("month");
  // Filled ON SUBMIT, never while typing: a "Name is required" that appears
  // before the first keystroke has landed is its own kind of unhelpful. Held as
  // the rules and the values they were checked against, worded at render.
  const [attempt, setAttempt] = useState<Attempt | null>(null);

  function reset() {
    setFiscalYear(String(new Date().getFullYear()));
    setName("");
    setGranularity("month");
    setAttempt(null);
  }

  // BudgetPlannerPage renders this component unconditionally, so it stays
  // mounted with the dialog shut and mutation state OUTLIVES the dialog:
  // without the resets a failed Save leaves `create.error` set and the next
  // open announces the refusal of a request nobody has made yet.
  function close() {
    setAttempt(null);
    create.reset();
    onClose();
  }

  function submit() {
    const next: Attempt = { rules: budgetRules, values: { name, fiscal_year: fiscalYear } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;

    create.mutate(
      // Trimmed to match what was validated -- `requiredProblems` trims before
      // testing emptiness, so an untrimmed send could carry a name the checks
      // never saw.
      { fiscal_year: Number(fiscalYear), name: name.trim(), granularity },
      { onSuccess: () => { reset(); onClose(); } },
    );
  }

  return (
    <Dialog open={open} onClose={close} title={t("budget.planner.newTitle")}>
      {/* A real <form>, so Enter in the Name field saves instead of doing
          nothing -- the other half of the same finding. */}
      <Form onSubmit={submit}>
        <div className="form-grid">
          <Field label={t("budget.planner.name")} full>
            {/* `required` states the same fact for assistive tech. It blocks
                nothing: <Form> is `noValidate`, because a native bubble that
                vanishes on the next click is the silent no-op wearing a hat.
                `BUDGET_RULES` is what the user reads. */}
            <input
              className={inputClass}
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
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
            <select
              className={inputClass}
              value={granularity}
              onChange={(e) => setGranularity(e.target.value as Granularity)}
            >
              {GRANULARITIES.map((g) => (
                <option key={g} value={g}>
                  {humaniseEnum(g, "Granularity")}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <div className="flex justify-end gap-2">
          {/* `type="button"` is load-bearing now that there is a form around
              it: a button with no type inside a <form> IS a submit button, so
              Cancel would save. */}
          <Button type="button" variant="ghost" onClick={close}>
            {t("budget.common.cancel")}
          </Button>
          {/* Enabled with the fields empty ON PURPOSE -- the refusal is what
              the user gets to read, and a control that will not move is not.
              The server failure comes from here too, so a bodyless 503 still
              produces a sentence and `role="alert"` announces it, replacing
              the `{String(create.error)}` line that rendered a raw object. */}
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

// Rename (name/status) via `PATCH /budget/budgets/{id}` and the dedicated
// Close control via `POST /budget/budgets/{id}/close`. Kept as one panel so
// the "closing is one-way" consequence sits right next to the button that
// causes it, per the controller fix directive.
function BudgetDetailCard({ budget, canManage }: { budget: BudgetOut; canManage: boolean }) {
  const t = useT();
  const update = useUpdateBudgetMutation();
  const close = useCloseBudgetMutation();

  const [name, setName] = useState(budget.name);
  const [status, setStatus] = useState<BudgetUpdateStatus>(
    budget.status === "active" ? "active" : "draft",
  );
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  // Cycle 8. The consequence was already written next to the button -- "cannot
  // be undone through any route, not even by reopening it" -- and the button
  // still fired on one click. A sentence beside a control is not a
  // confirmation: it is read once, on the visit where nobody presses it.
  const [confirmingClose, setConfirmingClose] = useState(false);

  const isClosed = budget.status === "closed";

  // The same silent no-op as the New budget dialog, one panel down: the name
  // could be cleared and the only response was a control that stopped working.
  function saveDetails() {
    const next: Attempt = { rules: budgetDetailRules, values: { name } };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;

    update.mutate({ id: budget.id, body: { name: name.trim(), status } });
  }

  function closeBudget() {
    close.mutate(budget.id);
  }

  return (
    <Panel title={t("budget.planner.detailsTitle")}>
      {isClosed ? (
        <p className="notice">
          <Icon name="lock" size={16} />
          <span>
            {t("budget.planner.closedNotice")}
          </span>
        </p>
      ) : (
        canManage && (
          <Form onSubmit={saveDetails}>
            <Field label={t("budget.planner.name")}>
              <input
                className={inputClass}
                required
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </Field>
            <Field label={t("budget.planner.status")}>
              <select
                className={inputClass}
                value={status}
                onChange={(e) => setStatus(e.target.value as BudgetUpdateStatus)}
              >
                <option value="draft">{humaniseEnum("draft", "BudgetStatus")}</option>
                <option value="active">{humaniseEnum("active", "BudgetStatus")}</option>
              </select>
            </Field>
            <div className="flex flex-wrap items-center gap-2">
              {/* "Save details" became "Save": `FormActions` builds its pending
                  text from the label (`Save` → `Saving…`), and the panel this
                  sits in is already titled "Budget details". */}
              <FormActions
                label={t("budget.common.save")}
                pending={update.isPending}
                error={update.error}
                problems={attemptProblems(attempt)}
              />
              {/* `type="button"`: Close budget is one-way and irreversible, and
                  a button with no type inside a <form> IS a submit button --
                  Enter in the Name field would have closed the budget. */}
              <Button
                type="button"
                variant="outline"
                onClick={() => setConfirmingClose(true)}
                disabled={close.isPending}
              >
                {close.isPending ? t("budget.planner.closing") : t("budget.planner.close")}
              </Button>
            </div>
            <p className="note" style={{ marginTop: 8 }}>
              {t("budget.planner.closeNote")}
            </p>
            {/* Close has no FormActions of its own, so it gets the same
                treatment by hand: `String(close.error)` rendered "Error: " for
                a bodyless 503 and was announced to nobody. */}
            {close.isError && (
              <p role="alert" className="notice red">
                <Icon name="circle-x" size={16} />
                <span>{describeSubmitError(close.error)}</span>
              </p>
            )}

          </Form>
        )
      )}

      {/* OUTSIDE THE `<Form>`, deliberately. `ui.tsx`'s Button sets no `type`,
          so a button rendered inside a form IS a submit button -- the same trap
          the `type="button"` notes above this file guard against. Nested in the
          form, this dialog's own Cancel would have saved the budget details.

          The subject is the budget's name AND fiscal year, because a planner
          holds several budgets whose names differ by a year that is easy to
          miss ("IT operating", FY2025 and FY2026) and closing the wrong one
          freezes the year people are still planning. `requireTyping` IS
          justified here and almost nowhere else in this module: there is no
          reopen -- not through the API, not through this console -- and the
          blast radius is every line in the year, for everyone. */}
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
          closeBudget();
        }}
      />
    </Panel>
  );
}

function LineGrid({
  budgetId,
  granularity,
  canManage,
  closed,
}: {
  budgetId: string;
  granularity: string;
  canManage: boolean;
  closed: boolean;
}) {
  const t = useT();
  const categories = useBudgetCategories();
  const lines = useBudgetLines(budgetId);
  const putLines = usePutBudgetLinesMutation();
  const deleteLine = useDeleteBudgetLineMutation();

  // `cells` holds every edited (or loaded) planned amount as a STRING --
  // money is a Decimal server-side and travels as a string; this grid never
  // parses a cell to a number. `lineIds` tracks which cell corresponds to an
  // already-persisted `BudgetLine.id`, so a cell that was never saved has no
  // delete control (there is nothing on the server to delete yet).
  const [cells, setCells] = useState<Record<string, string>>({});
  const [lineIds, setLineIds] = useState<Record<string, string>>({});
  const [loadedFor, setLoadedFor] = useState<string | undefined>(undefined);
  // Cycle 8. The `×` deleted a persisted planned amount on one click, from a
  // grid of up to twelve identical cells per row, with no confirmation and no
  // undo -- the "styled identically to the safe controls beside it" shape the
  // audits found on /sla and /health, at grid density.
  const [pendingDelete, setPendingDelete] = useState<
    { key: string; categoryName: string; period: number; amount: string } | null
  >(null);

  if (lines.data && loadedFor !== budgetId) {
    const initial: Record<string, string> = {};
    const ids: Record<string, string> = {};
    for (const line of lines.data) {
      if (line.cost_center_id == null) {
        const key = cellKey(line.category_id, line.period_index);
        initial[key] = line.planned_amount;
        ids[key] = line.id;
      }
    }
    setCells(initial);
    setLineIds(ids);
    setLoadedFor(budgetId);
  }

  const periods = Array.from({ length: periodCount(granularity) }, (_, i) => i + 1);
  const activeCategories = categories.data?.filter((c) => c.active) ?? [];
  const canEdit = canManage && !closed;

  function save() {
    const toSave: BudgetLineInput[] = [];
    for (const category of activeCategories) {
      for (const period of periods) {
        const value = cells[cellKey(category.id, period)];
        if (value != null && value !== "") {
          toSave.push({ category_id: category.id, period_index: period, planned_amount: value });
        }
      }
    }
    putLines.mutate({ budgetId, lines: toSave });
  }

  function removeLine(key: string) {
    const lineId = lineIds[key];
    if (lineId == null) return;
    deleteLine.mutate(
      { budgetId, lineId },
      {
        onSuccess: () => {
          setCells((prev) => {
            const next = { ...prev };
            delete next[key];
            return next;
          });
          setLineIds((prev) => {
            const next = { ...prev };
            delete next[key];
            return next;
          });
        },
      },
    );
  }

  return (
    <Panel
      title={t("budget.planner.gridTitle")}
      bare
      actions={
        canEdit ? (
          <Button onClick={save} disabled={putLines.isPending}>
            {putLines.isPending ? t("budget.common.saving") : t("budget.planner.saveGrid")}
          </Button>
        ) : undefined
      }
    >
      <QueryState
        loading={lines.isLoading}
        error={lines.error}
        empty={activeCategories.length === 0}
        emptyLabel={t("budget.planner.gridEmpty")}
        emptyIcon="tag"
      >
        <Table>
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
                          value={cells[key] ?? ""}
                          onChange={(e) => setCells((prev) => ({ ...prev, [key]: e.target.value }))}
                          disabled={!canEdit}
                        />
                        {canEdit && lineIds[key] != null && (
                          /* THE ONE ICON-ONLY AFFORDANCE IN THIS PASS THAT IS
                             GENUINELY A BUTTON. It was a bare `×` glyph with
                             no boundary of any kind, which is what SC 1.4.11
                             means by a component you cannot identify: the
                             only thing saying "press me" was `--muted-2`
                             text. `iconOnly` and `ariaLabel` are what it
                             needed and neither existed, so it hand-rolled a
                             ring instead. The name is unchanged, and it is
                             the name that matters -- a grid of these all
                             reading "Delete" would be a list of identical
                             buttons to a screen reader. */
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
                                amount: cells[key] ?? "",
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
      {/* The subject names the cell -- category and period -- and the amount
          in it, because the grid's cells are indistinguishable at a glance and
          the number is what makes one recognisable. */}
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
          if (pendingDelete) removeLine(pendingDelete.key);
          setPendingDelete(null);
        }}
      />

      {/* `String(error)` on both: a bodyless 503 read "ApiError: " with nothing
          after it, a 422 printed the raw Pydantic payload, and neither was
          announced -- on the grid where a rejected Save leaves the typed
          numbers on screen looking exactly like saved ones. */}
      {putLines.isError && (
        <p role="alert" className="notice red">
          <Icon name="circle-x" size={16} />
          <span>{describeSubmitError(putLines.error)}</span>
        </p>
      )}
      {deleteLine.isError && (
        <p role="alert" className="notice red">
          <Icon name="circle-x" size={16} />
          <span>{describeSubmitError(deleteLine.error)}</span>
        </p>
      )}
    </Panel>
  );
}
