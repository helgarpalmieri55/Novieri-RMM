---
target: /__preview, the kit test bench (frontend/src/preview/PreviewPage.tsx)
total_score: 28
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 1
target_identity: "file:frontend/src/preview/PreviewPage.tsx"
target_fingerprint: "sha256:4c07b7d6b5730d8f7062914145025eb1783daceaa0b14f52d3d3e58b58e0da96"
target_path: frontend/src/preview/PreviewPage.tsx
timestamp: 2026-11-24T20-11-53Z
slug: frontend-src-preview-previewpage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: `/__preview` (`frontend/src/preview/PreviewPage.tsx`), the kit's test bench

Redesign plan, Task 3, Step 11 ("Cierre de cada oleada", steps 1, 3 and 4). Read as what the page is: the
dev-only bench where every kit piece is drawn so the browser pass can measure it in pixels, and the reference
the eight waves copy. Assessment A (design review) was finished and recorded before the detector ran; then
Assessment B (CLI scan and the live overlay in Chromium). Captures: the full page at 1440 (en); the new
sections at 1440 (en, es) and 390 (en, es).

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | "Showing 9 of 12", "2 selected", "Unsaved changes" and "Saved." all drawn; the save bar's failing and blocked states are not |
| 2 | Match System / Real World | 3 | Fixture copy reads like real records (srv-app02, Andes Logistics · Bogotá); "Kit v4" is a bench label |
| 3 | User Control and Freedom | 3 | Clear filters, Clear selection, Discard, the back link, tabs that Back undoes (`?kit=`) |
| 4 | Consistency and Standards | 2 | Record and form drawn as cards inside the Kit card, unlike any page; the phone save bar splits its pair |
| 5 | Error Prevention | 3 | Save is disabled with nothing to save; the leave guard exists but a bench cannot show it |
| 6 | Recognition Rather Than Recall | 3 | Selected chip carries a check as well as the wash; tabs and section toggle say their state |
| 7 | Flexibility and Efficiency | 3 | Bulk bar, URL filters, arrow-key tabs, search with its glyph |
| 8 | Aesthetic and Minimalist Design | 2 | Nested cards; tall dead space between the two empty states at 390 |
| 9 | Error Recovery | 3 | "Nothing matches these filters." with the way out; the save bar's failure line is not on the bench |
| 10 | Help and Documentation | 3 | Every section carries a subtitle saying what it shows and why |
| **Total** | | **28/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** Specific in behaviour, plain in look, which is right for an Operate surface. What is
authored for this product is the behaviour the pieces carry: a chip's count drawn only when the server gave one
(never a made-up zero), "Showing 50" when no total is known, the empty state that tells "nothing yet" from
"nothing matches", one save bar instead of a button per panel, the selected chip's check as a second carrier.
The risk of sameness is in the bench's composition, not in the pieces: the record and the form are drawn inside
one card, which no page will do.

**Deterministic scan.** `impeccable detect --json frontend/src/preview` from the worktree root: exit 0, no
findings. The same scan over `shell/listKit.tsx`, `detailKit.tsx`, `formKit.tsx`, `RouteError.tsx` and
`modules/portal/PortalShell.tsx`: exit 0, no findings.

**Visual overlays.** `detect.js` was injected through Chromium (Playwright, a new tab on the dev server's
`/__preview`, title marked `[Human]`), read at 1440 (en) and 390 (es); the live server was stopped with
`--keep-inject` and no project file changed. No person's browser was attached, so no overlay is visible to
anyone. 90 findings at 1440, located by their enclosing panel:
- **In this task's sections:** `nested-cards` ×4, all inside "Kit v4" (the Details and Activity panels, the
  Organization section, the dirty save bar), agreeing with the LLM's second issue; at 390, one `straggler`
  ("from" alone on the Kit subtitle's last line).
- **In sections that predate this task** (not this task's to change): `undersized-ui-text` ×74 (10.5px table
  headers, tile and fact labels, painter axes, rail captions: DESIGN.md's own caption step, and PRODUCT.md's
  floor is 10.5px, so these are false positives against the product's scale); `nested-cards` ×6 (Getting
  started, Callouts, Before updates can be approved, Linux machines); `cramped-padding` ×3 (the bare fleet
  table panel, two dashboard widgets); `gray-on-color` ×1 (rail dim on the rail's active ground, the brand's
  own pair, the rail is Task 4's); `straggler`, `repeated-container-text` ("Small" ×3 on the dashboard
  arrangement), `overused-font` (Inter, already ignored in `.impeccable/config.json`).

## Overall Impression

The kit reads as one system: the same 6px controls and chips, the 4px badge, one accent, and the states a mock
never has. The single biggest opportunity is the save bar on a phone, because it is the one save bar every form
in the console will use.

## What's Working

- **The list head is honest.** The count chip is drawn only with a number the caller has, the selected chip
  carries a check besides the wash, and "Showing 9 of 12" sits at the end of the toolbar where the eye ends.
- **The two empty states say different things.** "Nothing matches these filters." with "Clear filters" next to
  "No computers yet." with the page's action, side by side: the distinction Atera fails to make is visible.
- **The record head carries status and context in one line** under the name, with the badge's icon, and the
  action on the right: the shape every record page will take.

## Priority Issues

**[P1] The save bar splits on a phone.**
- *What:* at 390 (es) "Guardar" wraps alone to the left edge of a second row under "Cambios sin guardar", away
  from "Descartar", which stays on the first.
- *Why it matters:* it is the one save bar for every form in the console; a technician on a phone during an
  incident finds the primary action where nothing else puts it, split from its pair.
- *Fix:* in `shell/kit.css`, under 640px the label takes the first row and Discard and Save sit together at
  the end of the second.
- *Suggested command:* /impeccable adapt

**[P2] Cards inside a card.**
- *What:* the record's Details and Activity panels, the form's Organization section and the dirty save bar are
  drawn inside the "Kit v4" panel, as the brief lays the section out (detector: four `nested-cards`).
- *Why it matters:* no page draws them that way, so the bench is a slightly wrong reference for the waves, and
  the record head's muted context is measured on white where pages put it on the page ground (it passes on
  both; the backplate is still not the real one).
- *Fix:* draw the record and the form on the page ground below a bare list panel (a departure from the brief's
  single panel, so a question below).
- *Suggested command:* /impeccable layout

**[P2] The save bar's failure and blocked states are never measured.**
- *What:* the bench draws the bar at rest and saved; its failure line (critical ink, `role="alert"`) and its
  blocked reason (muted) are paints no browser pass sees. A second dirty bar cannot simply be added: the region
  name is fixed ("Unsaved changes"), and two would fail `landmark-unique`.
- *Why it matters:* these are the states that matter most when a save goes wrong.
- *Fix:* either draw the failing bar in place of the resting one, or let the bar take an optional name.
- *Suggested command:* /impeccable harden

**[P2] The embedded portal frame keeps a live "Sign out".**
- *What:* embedded, the frame forgets no stored client and records no person, but its Sign out still calls
  the real sign-out.
- *Why it matters:* on a dev server with a backend, a click on the picture ends the developer's console session.
- *Fix:* embedded, draw the button and make it do nothing, or leave it as the dev-only cost it is.
- *Suggested command:* /impeccable harden

## Persona Red Flags

**The technician on a phone during an incident:** Save lands alone on the left under "Cambios sin guardar",
away from Descartar (P1). In the bulk bar, "Quitar selección" drops to the end of the row only when it fits.

**The technician at a desk all day:** nothing blocking. Tabs move with the arrow keys and Back undoes a tab
(`?kit=b` opens Hardware); chips are toggles with `aria-pressed`; the section toggle says expanded or not.

**The auditor:** not served by the bench beyond legibility; the measured ratios are what they read.

## Minor Observations

- At 390 the Kit subtitle leaves "from" alone on its last line (detector `straggler`).
- The bench now holds three `h1` (the page's, the record's, the sign-in card's); axe passes, a screen reader's
  heading list is noisier.
- The save bar's region is named by the same words it shows ("Unsaved changes"), heard twice.
- At 390 the two empty states stack with their 36px padding each: tall dead space between them.

## Questions to Consider

1. **The record and the form inside the Kit card (P2).** (a) Keep the brief's single panel and accept the four
   `nested-cards`; (b) draw the record and the form on the page ground below a bare list panel, as pages do;
   (c) three sections, list, record and form, each the way its pages draw it.
2. **The save bar's failure and blocked states (P2).** (a) Leave them to `contrast.test.ts`'s token pairs;
   (b) draw the failing bar instead of the resting one; (c) give `SaveBar` an optional region name so the bench
   can draw both.
3. **The embedded portal frame's Sign out (P2).** (a) Leave it live, a dev-only cost; (b) embedded disarms it,
   which wave 2 then keeps when it repaints the portal; (c) hide it when embedded.
