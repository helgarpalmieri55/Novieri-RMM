import { EmptyState } from "../../components/emptystate";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { useClientScope } from "../../lib/clientScope";
import { humaniseStatus } from "../../lib/labels";
import type { BadgeVariant } from "../jobs/jobState";
import type { Kind } from "../../shell/viz";
import { SectionNav } from "../../shell/components";

// Expense.status vocabulary exactly as written by the backend
// (backend/app/modules/budget/schema.py's `ExpenseStatus` Literal is
// `Literal["expected", "confirmed", "void"]`, the only values
// ExpenseCreate/create_expense/confirm_expense/void_expense ever write to
// Expense.status). No other status value is ever persisted. "expected" is
// the pre-confirmation worklist state (spec §0.3 -- a human must still act,
// info); "confirmed" is an asserted real cost (success); "void" is a
// reversed entry, a normal lifecycle outcome rather than a failure (muted,
// mirroring subscriptionState.ts's "cancelled" -> muted precedent).
const EXPENSE_STATUS_VARIANTS: Record<string, BadgeVariant> = {
  expected: "info",
  confirmed: "success",
  void: "muted",
};

export function expenseStatusVariant(status: string): BadgeVariant {
  return EXPENSE_STATUS_VARIANTS[status] ?? "muted";
}

// Budget.status vocabulary exactly as written by the backend
// (backend/app/modules/budget/schema.py's `BudgetStatus` Literal is
// `Literal["draft", "active", "closed"]`). "draft" is not yet live (muted);
// "active" is the current, editable budget (success); "closed" is a
// terminal, normal lifecycle end -- not a failure (muted).
const BUDGET_STATUS_VARIANTS: Record<string, BadgeVariant> = {
  draft: "muted",
  active: "success",
  closed: "muted",
};

export function budgetStatusVariant(status: string): BadgeVariant {
  return BUDGET_STATUS_VARIANTS[status] ?? "muted";
}

// Expense.source vocabulary exactly as written by the backend
// (backend/app/modules/budget/schema.py's `ExpenseSource` Literal is
// `Literal["manual", "subscription", "license"]`). "manual" is operator
// entry (muted, the baseline); "subscription"/"license" are auto-fed rows
// from materialization (spec §6) and share the distinct "info" look, the
// same one-distinct-rest-neutral pattern subscriptionState.ts's
// billingCycleVariant uses.
const EXPENSE_SOURCE_VARIANTS: Record<string, BadgeVariant> = {
  manual: "muted",
  subscription: "info",
  license: "info",
};

export function expenseSourceVariant(source: string): BadgeVariant {
  return EXPENSE_SOURCE_VARIANTS[source] ?? "muted";
}

// The five budget surfaces this module contributes, shared by every page so
// the module nav renders identically everywhere (the Phase-12 lesson: a page
// routed but unlinked is not reachable). App.tsx's top-level Shell nav links
// only to "/budget" (BudgetDashboard); these are the links that make the
// other four surfaces reachable at all.
// Labels are distinct from any in-page action button text (e.g. the
// confirmation queue's own "Confirm"/"Void" row actions) so a query by
// accessible name never collides between the nav link and a page control.
// The labels are accessors over the catalogue, read when the strip is drawn.
export const BUDGET_NAV_ITEMS: ReadonlyArray<{ to: string; label: string }> = [
  { to: "/budget", get label() { return translate("budget.nav.dashboard"); } },
  { to: "/budget/planner", get label() { return translate("budget.nav.planner"); } },
  { to: "/budget/expenses", get label() { return translate("budget.nav.expenses"); } },
  { to: "/budget/confirm", get label() { return translate("budget.nav.confirm"); } },
  { to: "/budget/admin", get label() { return translate("budget.nav.admin"); } },
];

/**
 * The nav itself, one component every budget page renders.
 *
 * THE KIT'S `SectionNav` NOW (redesign Task 11, "BudgetDashboard"; the
 * waves' rule for module menus): one row of real links with the current page
 * marked by `aria-current="page"`, the shape every module strip in the console
 * takes. The earlier `role="tablist"` of links promised arrow keys and tab
 * panels a navigation does not have. Same exported name, same labels, same
 * permission: nothing for a reader without `budget.read`. `/budget` matches
 * only itself (`end`), so the dashboard is not marked on the four pages under it.
 */
export function BudgetPageNav({ canRead }: { canRead: boolean }) {
  const t = useT();
  if (!canRead) return null;
  return (
    <SectionNav
      label={t("budget.nav.label")}
      items={BUDGET_NAV_ITEMS.map((item) => ({ to: item.to, label: item.label, end: item.to === "/budget" }))}
    />
  );
}

// THE v3 KINDS, beside the retired variants. `StatusPill` takes a `Kind`; the
// judgement is the same one written in the new vocabulary. An expected
// expense is a deadline for a person (warn: v3 draws the pending row in the
// warning wash), a confirmed one is a fact (ok), a void one is a normal
// reversal (mut). A draft budget is nothing yet (mut), an active one is the
// live one (ok), a closed one is finished (mut). Auto-fed rows are the
// distinct source (info); a manual entry is the baseline (mut). Nothing on a
// page calls the `BadgeVariant` functions above any more; only their tests
// do. The plan is that the closer removes them together with those test
// cases when `Badge` is retired from the kit, in one commit, rather than a
// page at a time here.

const EXPENSE_STATUS_KINDS: Record<string, Kind> = { expected: "warn", confirmed: "ok", void: "mut" };

export function expenseStatusKind(status: string): Kind {
  return EXPENSE_STATUS_KINDS[status] ?? "mut";
}

const BUDGET_STATUS_KINDS: Record<string, Kind> = { draft: "mut", active: "ok", closed: "mut" };

export function budgetStatusKind(status: string): Kind {
  return BUDGET_STATUS_KINDS[status] ?? "mut";
}

/**
 * Where an expense came from, as the short phrase under its title: "entered by
 * hand", "from subscription". A source the console has not met reads through
 * the shared derivation, lower-cased, never as its raw wire word.
 */
export function sourceNote(source: string): string {
  if (source === "manual") return translate("budget.common.byHand");
  if (source === "subscription") return translate("budget.common.fromSubscription");
  if (source === "license") return translate("budget.common.fromLicense");
  return translate("budget.common.fromOther", { source: humaniseStatus(source).toLowerCase() });
}

export function expenseSourceKind(source: string): Kind {
  return source === "subscription" || source === "license" ? "info" : "mut";
}

/**
 * True when a budget page has nothing to show: every client is chosen at the
 * top. Budget figures are one client's, in its base currency, and are never
 * added across clients (plan Task 11 D11.1). False until `/me` has answered,
 * so the pages keep their own loading state meanwhile.
 */
export function useBudgetNeedsClient(): boolean {
  const { selected, ready } = useClientScope();
  return ready && selected === null;
}

/** What a budget page says instead of its figures with every client chosen. */
export function BudgetPickClient() {
  const t = useT();
  // An h2: with every client chosen it is the page's only section under the h1
  // (axe `heading-order`; the wave's critique).
  return (
    <EmptyState icon="building" title={t("budget.pickClient.title")} body={t("budget.pickClient.body")} headingLevel={2} />
  );
}
