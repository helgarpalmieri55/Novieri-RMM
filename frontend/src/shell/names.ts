/**
 * THE PRODUCT'S NAMES, in a module with no component and no CSS, so `nav.ts`
 * and `title.tsx` can read them without importing `brand.tsx` (which imports
 * `brand.css`). `brand.tsx` re-exports both.
 */

/** The product's name (decision A3): in the tab title, the sign-in frame, the portal. */
export const PRODUCT_NAME = "Novieri RMM";
/** What the console's own bar says (decision A3). */
export const BRAND_NAME = "Novieri";
