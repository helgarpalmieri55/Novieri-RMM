/**
 * THE PAGE FORMS ON THIS ROUTE FOLD TO ONE COLUMN ON A HANDSET.
 *
 * `.form-grid` WAS `repeat(2, minmax(0, 1fr))` at every width. The rule meant
 * to fold it lived in `shell.css`, tied on specificity with the unconditional
 * rule in `components.css` and lost on import order, so at 390px "Default page
 * size" and "Locale" stayed two-up at about 148px each and
 * `it@toeshee.example` filled its box to the edge, on a page whose section
 * list above it folds correctly. Reflow at that width is SC 1.4.10.
 *
 * THE KIT RULE HAS SINCE LANDED (the fold now lives in `components.css`
 * immediately after the base rule, where no import order can undo it), so
 * every `.form-grid` in the console folds at 640px whether or not this file
 * exists. This one stays, because it was never only a fold.
 *
 * WHAT THIS DOES INSTEAD, AND WHY IT IS NOT A WORKAROUND WAITING FOR ONE.
 * `auto-fit` lets the grid count its own columns, so no breakpoint has to
 * guess where the fold belongs: two columns while each can hold 260px — the
 * width `shell.css` names as where "a tracked uppercase caption stops fitting
 * above its control" — and one below that. `min(100%, 260px)` keeps a single
 * column from overflowing a container narrower than the minimum, which is the
 * failure mode a bare `minmax(260px, 1fr)` has at 240px.
 *
 * THE 560px CEILING IS THE KIT'S OWN NUMBER, not a new one: `.field` declares
 * `max-width: 560px` for a field standing alone, and v3's own settings screen
 * caps the facts block on that panel at `max-width:560px`. Without a ceiling
 * `auto-fit` would draw three and four columns of fields in a wide panel,
 * which is neither what the design draws nor what `.form-grid` promised.
 *
 * It composes: a caller with its own margin spreads this first
 * (`style={{ ...FOLDING_FORM_GRID, marginTop: 14 }}`), and the `.form-grid`
 * class stays on the element, so the gap, the spanning `.full` children and
 * every other rule the kit gives the grid are unchanged.
 */
export const FOLDING_FORM_GRID = {
  gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 260px), 1fr))",
  maxWidth: 560,
} as const;
