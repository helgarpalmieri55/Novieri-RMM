/**
 * The two lookups this module's create form needs, as picker options.
 *
 * WHY THIS FILE (UX remediation cycles 6 and 7). Auditor B, [HIGH],
 * /subscriptions: "The page's tiles are `Active subscriptions`, `Renewals in 90
 * days` and `Notice deadlines due` ... There is no renewal date, no term end,
 * no notice period and no owner field. Two of three headline metrics — and the
 * module's stated differentiator, cancellation-notice deadlines — depend on
 * data the UI provides no way to enter."
 *
 * The renewal date landed in an earlier cycle. Owner and notice period are the
 * rest of that finding: `owner_user_id` is an identifier, so it gets a picker
 * rather than a box to paste a UUID into, and the spend report's "by owner"
 * roll-up is only ever populated by what this control writes.
 *
 * This module's own copy rather than an import from `licenses/pickerOptions.ts`
 * even for vendors: the shape is the same but the HINT is not, because the
 * route that fills the list is reached from a different place in the nav and
 * this form can be saved without a vendor.
 */

import type { PickerOption } from "../../components/picker";
import { translate } from "../../i18n/core";
import { humaniseEnum, humaniseStatus } from "../../lib/labels";
import type { VendorOut } from "../licenses/types";
import type { UserOut } from "../settings/types";

/**
 * WHAT WOULD FILL EACH LIST — and, for both, the second reason it can be empty.
 *
 * Vendor reads are `licenses.read`-gated and the user directory is
 * `users.read`-gated; neither comes with `subscriptions.manage`. A hint that
 * said only "none exist yet" would send an operator to a screen they cannot
 * open, which is the blocked flow again one door along.
 */
// Components call the functions, which follow the language. The constants are
// the wording at load time, kept because the module's unit tests import them.
export const vendorPickerEmptyHint = (): string => translate("subscriptions.picker.vendorEmptyHint");
export const ownerPickerEmptyHint = (): string => translate("subscriptions.picker.ownerEmptyHint");
export const VENDOR_PICKER_EMPTY_HINT = vendorPickerEmptyHint();
export const OWNER_PICKER_EMPTY_HINT = ownerPickerEmptyHint();

/**
 * A vendor, with the fact that tells two of them apart — see
 * `licenses/pickerOptions.ts` for why the website and not the id.
 */
export function vendorPickerOptions(vendors: readonly VendorOut[]): PickerOption[] {
  return vendors.map((vendor) => ({
    id: vendor.id,
    label: vendor.name,
    sublabel: vendor.website ? vendor.website : translate("subscriptions.picker.noWebsite"),
  }));
}

/**
 * A person, with the two facts that decide whether they are the right one.
 *
 * THE EMAIL, because display names collide in every directory of any size and
 * the address is the thing an operator can check against the contract. THE
 * STATUS, because a `deleted` row is RETAINED and still listed (settings/types
 * says so explicitly) — assigning a renewal-notice owner to a disabled account
 * means the deadline this module exists to catch is owned by nobody. An active
 * account adds no status noise; anything else says what it is.
 */
export function ownerPickerOptions(users: readonly UserOut[]): PickerOption[] {
  return users.map((user) => ({
    id: user.id,
    label: user.display_name || user.email,
    sublabel: user.status === "active" ? user.email : translate("subscriptions.picker.ownerStatus", { email: user.email, status: accountStatusWord(user.status) }),
  }));
}

const ACCOUNT_STATUSES: ReadonlySet<string> = new Set(["active", "disabled", "deleted"]);

/** An account's status in lower case, for the end of a sublabel (the shared `UserStatus` words). */
function accountStatusWord(status: string): string {
  const label = ACCOUNT_STATUSES.has(status) ? humaniseEnum(status, "UserStatus") : humaniseStatus(status);
  return label.toLocaleLowerCase();
}

/**
 * The name behind an owner id already stored on a subscription.
 *
 * Falls back to the id rather than to a blank: an id whose account the caller
 * cannot read is still more than nothing, and printing "—" for a subscription
 * that HAS an owner would be the worse lie.
 */
export function ownerName(options: readonly PickerOption[], id: string | null): string | null {
  if (!id) return null;
  return options.find((option) => option.id === id)?.label ?? id;
}

