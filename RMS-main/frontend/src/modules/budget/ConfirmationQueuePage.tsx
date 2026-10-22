import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { ConfirmDialog } from "../../components/confirm";
import { describeSubmitError } from "../../components/form";
import { Button, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { Me } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDate } from "../../lib/datetime";
import { humaniseEnum } from "../../lib/labels";
import {
  useBudgetCategories,
  useConfirmExpenseMutation,
  useExpenses,
  useVoidExpenseMutation,
} from "./api";
import { BudgetPageNav, expenseStatusKind, sourceNote } from "./budgetState";
import type { ExpenseOut } from "./types";
import { PageHead, Panel, QueryState, StatusPill } from "../../shell/components";
import { Icon } from "../../shell/icons";

// The operator worklist that makes spec §0.3 workable: GET
// /budget/expenses?status=expected -- rows a human must still confirm or
// void, never auto-confirmed by the backend.
export function ConfirmationQueuePage() {
  const t = useT();
  const api = useApi();
  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const permissions = me.data?.permissions ?? [];
  const canRead = permissions.includes("budget.read");
  const canConfirm = permissions.includes("expense.confirm");

  const categories = useBudgetCategories();
  const categoryName = (id: string) => categories.data?.find((c) => c.id === id)?.name ?? id;

  const expenses = useExpenses({ status: "expected" });
  const confirm = useConfirmExpenseMutation();
  const voidExpense = useVoidExpenseMutation();
  const items = expenses.data ?? [];

  // Cycle 8. Both row actions were one click with no confirmation, side by
  // side, on rows that look alike -- and both are terminal server-side:
  // `confirm_expense` rejects an already-confirmed row ("a corrective rate
  // must never re-value history", spec §3) and `void_expense` says void is
  // "reversible only by creating a new entry". Confirm additionally commits
  // the AMOUNT in the box beside it, which is editable, so the figure is part
  // of what has to be read back before it lands.
  // `title` is what the ROW is called -- the expense, falling back to its
  // category -- so the dialog names the row the operator clicked rather than
  // a different half of it.
  const [confirming, setConfirming] = useState<{ expense: ExpenseOut; amount: string; title: string } | null>(null);
  const [voiding, setVoiding] = useState<{ expense: ExpenseOut; title: string } | null>(null);

  return (
    <div>
      <PageHead
        title={t("budget.confirm.title")}
        description={t("budget.confirm.description")}
      />

      <BudgetPageNav canRead={canRead} />

      <Panel bare>
        <QueryState
          loading={expenses.isLoading}
          error={expenses.error}
          empty={items.length === 0}
          emptyLabel={t("budget.confirm.empty")}
          emptyIcon="budget"
        >
          {/* THE DECISION COLUMN COMES SECOND, AND THE AMOUNT TRAVELS WITH
              IT. At 390px the table is wider than the viewport whatever it
              carries (the kit floors it at 560px), so what matters is WHICH
              columns are inside the first screenful. Confirm and Void were
              last, behind the status pill, on a queue whose entire job is
              confirm-or-void: both decisions were off screen with nothing
              saying they existed. They now sit beside the amount they commit,
              in one cell that wraps rather than one that has to be scrolled
              to, and the date and the status -- context, not decisions --
              take the columns that fall off the right. */}
          <Table>
            <thead>
              <tr>
                <Th>{t("budget.confirm.colExpense")}</Th>
                <Th>{canConfirm ? t("budget.confirm.colAmountDecision") : t("budget.confirm.colAmount")}</Th>
                <Th>{t("budget.confirm.colDate")}</Th>
                <Th>{t("budget.confirm.colStatus")}</Th>
              </tr>
            </thead>
            <tbody>
              {items.map((expense) => (
                <QueueRow
                  key={expense.id}
                  expense={expense}
                  categoryName={categoryName(expense.category_id)}
                  canConfirm={canConfirm}
                  onConfirm={(amount) =>
                    setConfirming({
                      expense,
                      amount,
                      title: expense.description || categoryName(expense.category_id),
                    })
                  }
                  onVoid={() =>
                    setVoiding({
                      expense,
                      title: expense.description || categoryName(expense.category_id),
                    })
                  }
                />
              ))}
            </tbody>
          </Table>
        </QueryState>
      </Panel>
      {/* The subject carries the AMOUNT, not just the category: the box on the
          row is editable and pre-filled with the projected figure, so "confirm"
          means "assert that this number is the real cost" and the number is the
          thing to read back. Not `destructive` -- it records a fact rather than
          destroying one -- but it is terminal, and the body says so. */}
      <ConfirmDialog
        open={confirming != null}
        title={t("budget.confirm.confirmTitle")}
        subject={
          confirming
            ? t("budget.confirm.subject", {
                title: confirming.title,
                amount: confirming.amount,
                currency: confirming.expense.currency,
                date: formatDate(confirming.expense.expense_date, "UTC"),
              })
            : ""
        }
        body={t("budget.confirm.confirmBody")}
        confirmLabel={t("budget.confirm.confirmLabel")}
        onCancel={() => setConfirming(null)}
        onConfirm={() => {
          if (confirming) {
            confirm.mutate({
              id: confirming.expense.id,
              body: { amount: confirming.amount },
            });
          }
          setConfirming(null);
        }}
      />

      <ConfirmDialog
        open={voiding != null}
        title={t("budget.confirm.voidTitle")}
        subject={
          voiding
            ? t("budget.confirm.subject", {
                title: voiding.title,
                amount: voiding.expense.amount,
                currency: voiding.expense.currency,
                date: formatDate(voiding.expense.expense_date, "UTC"),
              })
            : ""
        }
        body={t("budget.confirm.voidBody")}
        confirmLabel={t("budget.confirm.voidLabel")}
        destructive
        onCancel={() => setVoiding(null)}
        onConfirm={() => {
          if (voiding) voidExpense.mutate(voiding.expense.id);
          setVoiding(null);
        }}
      />

      {/* `String(error)` on both: a bodyless 503 rendered "ApiError: " and a
          422 rendered the raw Pydantic payload, neither announced. A failed
          Confirm is indistinguishable from a slow one on this page -- the row
          stays put either way -- so the sentence is the only signal there is. */}
      {confirm.isError && (
        <p role="alert" className="notice red">
          <Icon name="circle-x" size={16} />
          <span>{describeSubmitError(confirm.error)}</span>
        </p>
      )}
      {voidExpense.isError && (
        <p role="alert" className="notice red">
          <Icon name="circle-x" size={16} />
          <span>{describeSubmitError(voidExpense.error)}</span>
        </p>
      )}
    </div>
  );
}

function QueueRow({
  expense,
  categoryName,
  canConfirm,
  onConfirm,
  onVoid,
}: {
  expense: ExpenseOut;
  categoryName: string;
  canConfirm: boolean;
  onConfirm: (amount: string) => void;
  onVoid: () => void;
}) {
  const t = useT();
  // Pre-filled with the currently-recorded (projected) amount -- the
  // confirmed amount may differ (spec §0.3/§5), so this is a plain editable
  // text field, not a display of the projected figure.
  const [amount, setAmount] = useState(expense.amount);

  const kind = expenseStatusKind(expense.status);

  // The row's own name, in the same order as the ledger next door: two pages
  // of one module, reachable through the same tab strip, read their rows the
  // same way round. The expense is the title; what it is filed against and
  // where it came from are the line under it. An expense with no description
  // is titled by its category so the row is never blank.
  const title = expense.description || categoryName;

  return (
    <tr>
      {/* v3's row: the glyph, the title and the line under it. */}
      {/* `Figma Organization, monthly` came out over three lines at 390,
          because the decision column beside it holds an input and two buttons
          and took the width. The expense column states 240px and clips. */}
      <Td lead={<Icon name="receipt" size={18} />} gutter={kind} clip={240}>
        <div className="cell-title">{title}</div>
        <div className="cell-sub">
          {[
            expense.description ? categoryName : null,
            sourceNote(expense.source),
          ]
            .filter(Boolean)
            .join(" · ")}
        </div>
      </Td>
      <Td>
        {canConfirm ? (
          // `flex-wrap`, so the cell's minimum width is the field rather than
          // the field plus both buttons: on a handset the two decisions drop
          // onto a second line inside the cell instead of off the table.
          <div className="flex flex-wrap items-center gap-2">
            <input
              className={inputClass}
              style={{ width: 140 }}
              aria-label={t("budget.confirm.correctedAria", { title, date: expense.expense_date })}
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
            />
            <span className="mono">{expense.currency}</span>
            {/* Outline, not primary: a queue of twenty rows is twenty Confirm
                buttons, and the page may carry one primary. The confirmation
                dialog's own button is the commit. */}
            <Button variant="outline" onClick={() => onConfirm(amount)}>
              {t("budget.confirm.confirm")}
            </Button>
            <Button variant="outline" onClick={onVoid}>
              {t("budget.confirm.void")}
            </Button>
          </div>
        ) : (
          <span className="mono">{`${expense.amount} ${expense.currency}`}</span>
        )}
      </Td>
      {/* A calendar date rendered in the product's one format and pinned to
          UTC -- see ExpensesPage's row for why the day must not move. */}
      <Td>{formatDate(expense.expense_date, "UTC")}</Td>
      <Td>
        <StatusPill value={expense.status} label={humaniseEnum(expense.status, "ExpenseStatus")} kind={kind} />
      </Td>
    </tr>
  );
}
