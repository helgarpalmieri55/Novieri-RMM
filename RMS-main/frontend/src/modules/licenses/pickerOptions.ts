/**
 * The two lookups this module's create forms depend on, as picker options.
 *
 * WHY THIS FILE (UX remediation cycles 6 and 7). Auditor B, [HIGH], /licenses →
 * New license: "The Product `<select>` contains exactly 1 option — the
 * placeholder 'Select a product…'. The Vendor select contains exactly 1 option
 * — 'None'. With no product selectable, Save is a silent no-op and no license
 * can ever be created. The tab bar offers a 'Products' and a 'Vendors' tab that
 * would seed these lists, but the modal neither says so nor links to them."
 *
 * So the defect here is NOT a raw-UUID field — this module already used
 * `<select>` lookups, which the audit called out as the correct pattern. The
 * defect is an EMPTY lookup that explains nothing. `EntityPicker` is adopted
 * for the hint and the search; the hints below are the actual fix, and the
 * pages pair each with a link to the surface that fills it.
 *
 * Pure functions rather than JSX, because the SUBLABEL is the part that decides
 * the choice and it deserves a test of its own: a product catalog routinely
 * carries "Office Suite" from two publishers, and a license attached to the
 * wrong one silently misreports compliance for both.
 *
 * Deliberately this module's own copy rather than a shared one — the hints name
 * *this* module's surfaces, and subscriptions' vendor hint names a different
 * route into the same list.
 */

import type { PickerOption } from "../../components/picker";
import { translate } from "../../i18n/core";
import type { ProductOut, VendorOut } from "./types";

/**
 * WHAT WOULD FILL THE LIST, which is the whole of the blocked-flow fix.
 *
 * Both hints name the console surface that populates the list AND the other
 * reason it can be empty: these reads are `licenses.read`-gated server-side, so
 * "there are none yet" and "you may not see them" are both live, and a hint
 * that asserts only the first sends a reader to a screen they cannot use.
 */
// Components call the functions, which follow the language. The constants are
// the wording at load time, kept because the module's unit tests import them.
export const productPickerEmptyHint = (): string => translate("licenses.picker.productEmptyHint");
export const vendorPickerEmptyHint = (): string => translate("licenses.picker.vendorEmptyHint");
export const PRODUCT_PICKER_EMPTY_HINT = productPickerEmptyHint();
export const VENDOR_PICKER_EMPTY_HINT = vendorPickerEmptyHint();

/**
 * A product, with the fact that tells two of them apart.
 *
 * THE PUBLISHER, because the catalog is normalized on name and a name alone is
 * ambiguous by design: "Acme Office Suite" and a reseller's rebadge of it are
 * separate rows whose entitlements must never be pooled. A product with no
 * publisher recorded says so rather than rendering a bare separator — an empty
 * sublabel would read as "same publisher as the row above".
 */
export function productPickerOptions(products: readonly ProductOut[]): PickerOption[] {
  return products.map((product) => ({
    id: product.id,
    label: product.name,
    sublabel: product.publisher ? product.publisher : translate("licenses.picker.noPublisher"),
  }));
}

/**
 * A vendor, with the fact that tells two of them apart.
 *
 * THE WEBSITE, because vendor rows are created by hand from contracts and
 * near-duplicates ("Acme", "Acme Software", "Acme Software Ltd") are the normal
 * state of that table; the domain is what identifies which one is the entity on
 * the invoice.
 */
export function vendorPickerOptions(vendors: readonly VendorOut[]): PickerOption[] {
  return vendors.map((vendor) => ({
    id: vendor.id,
    label: vendor.name,
    sublabel: vendor.website ? vendor.website : translate("licenses.picker.noWebsite"),
  }));
}
