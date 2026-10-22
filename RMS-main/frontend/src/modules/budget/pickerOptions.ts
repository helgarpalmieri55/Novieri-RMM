/**
 * The two lookups the expense forms depend on, as picker options.
 *
 * WHY THIS FILE (UX remediation cycles 6 and 7). The New expense dialog's
 * Category control was a `<select>` whose only entry on a fresh install is the
 * placeholder "Select a category", sitting above a Save disabled by
 * `!categoryId` — the same blocked flow Auditor B filed as [HIGH] against
 * /licenses ("both required lookup dropdowns are empty ... Save is a silent
 * no-op") and against /sla, and called "a product-wide first-run pattern, not
 * a one-module bug". The category list is seeded from the Admin tab, which the
 * dialog neither said nor linked to.
 *
 * Pure functions rather than JSX because the SUBLABEL carries the meaning: an
 * expense filed against a retired category disappears from the variance grid
 * (the planner only plans against `active` ones), which is a wrong number
 * rather than an error message.
 */

import type { PickerOption } from "../../components/picker";
import { translate } from "../../i18n/core";
import type { CategoryOut, CostCenterOut } from "./types";

/**
 * WHAT WOULD FILL EACH LIST — the whole of the blocked-flow fix.
 *
 * Each names the console surface that populates it AND the other reason it can
 * be empty: both reads are `budget.read`-gated and both writes need
 * `budget.manage`, which `expense.record` does not imply. An operator who may
 * file expenses but not administer the module needs to be told to ask, not
 * sent to a screen that will refuse them.
 */
// Components call the functions, which follow the language. The constants are
// the wording at load time, kept because the module's unit tests import them.
export const categoryPickerEmptyHint = (): string => translate("budget.picker.categoryEmptyHint");
export const costCenterPickerEmptyHint = (): string => translate("budget.picker.costCenterEmptyHint");
export const CATEGORY_PICKER_EMPTY_HINT = categoryPickerEmptyHint();
export const COST_CENTER_PICKER_EMPTY_HINT = costCenterPickerEmptyHint();

/**
 * A category, with the fact that decides whether it is the right one.
 *
 * ITS KEY, because the key is what the ERP and the materializer both address —
 * `system_key` rows are the ones subscription and license renewals feed
 * automatically — and two categories routinely read alike by name
 * ("Software", "Software licences") while their keys are unambiguous.
 *
 * AND WHETHER IT IS RETIRED, because an inactive category still accepts
 * expenses server-side but the planner plans no lines against it, so money
 * filed there is real spend that appears in no variance row. That is a silent
 * wrong number, which is worse than a refusal, and it is invisible from the
 * name alone. The create form feeds this the ACTIVE list, so the marker should
 * never fire there; it exists so that a caller which passes the full list — a
 * filter, an admin surface — cannot present a retired category as a live one.
 */
export function categoryPickerOptions(categories: readonly CategoryOut[]): PickerOption[] {
  return categories.map((category) => ({
    id: category.id,
    label: category.name,
    sublabel: category.active ? category.key : translate("budget.picker.categoryRetired", { key: category.key }),
  }));
}

/**
 * A cost center, with its code and whether it is retired — same reasoning: the
 * code is the identifier finance uses, and a retired one still accepts spend.
 */
export function costCenterPickerOptions(costCenters: readonly CostCenterOut[]): PickerOption[] {
  return costCenters.map((costCenter) => ({
    id: costCenter.id,
    label: costCenter.name,
    sublabel: costCenter.active ? costCenter.code : translate("budget.picker.costCenterRetired", { code: costCenter.code }),
  }));
}
