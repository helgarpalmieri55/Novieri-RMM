import { Link, useLocation } from "react-router-dom";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { humaniseStatus } from "../../lib/labels";
import type { BadgeVariant } from "../jobs/jobState";
import type { Kind } from "../../shell/viz";

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
 * The nav itself -- previously duplicated verbatim in all five page files
 * (a new surface or a markup change meant five synchronized edits). Every
 * budget page renders this one component instead.
 *
 * WHY THE MARKUP CHANGED. Auditor B, [MEDIUM]: "All five tabs are `<button
 * class="btn">`. Checking ARIA: `role` = null on every tab, `aria-selected` =
 * null on every tab, and the container's role = null (no tablist) ... A screen
 * reader user hears five unrelated buttons with no indication that they form a
 * set, which one is active, or how many there are." And [LOW], the same bar:
 * "the control is a `<button>`, so it cannot be middle-clicked, opened in a new
 * tab, or copied via right-click" -- the finding notes the state IS addressable,
 * so the routing work was already done and being thrown away.
 *
 * Both are fixed by the shape this module's sibling already ships:
 * `licenses/licenseNav.tsx` renders `NavLink`s carrying `role="tab"` and the
 * design's `.tabs`/`.tab` classes. A `<button>` nested inside an `<a>` was also
 * invalid markup -- two interactive elements, one of them unreachable as a
 * link. `aria-selected` and `aria-current="page"` are both set from one
 * computed match, so assistive tech is told which tab is active by either
 * convention.
 */
export function BudgetPageNav({ canRead }: { canRead: boolean }) {
  const t = useT();
  // `NavLink`'s own `isActive` is only available to `className`/`style`, and
  // `aria-selected` needs it too -- so the match is computed once here, from
  // the same rule `NavLink`'s `end` would apply: `/budget` is a prefix of every
  // other budget route, so the Dashboard tab matches only itself.
  const { pathname } = useLocation();
  const isActive = (to: string) => (to === "/budget" ? pathname === to : pathname.startsWith(to));

  if (!canRead) return null;
  return (
    <nav aria-label={t("budget.nav.label")} className="tabs" role="tablist">
      {BUDGET_NAV_ITEMS.map((item) => {
        const active = isActive(item.to);
        return (
          <Link
            key={item.to}
            to={item.to}
            role="tab"
            aria-selected={active}
            aria-current={active ? "page" : undefined}
            className={`tab${active ? " active" : ""}`}
          >
            {item.label}
          </Link>
        );
      })}
    </nav>
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
