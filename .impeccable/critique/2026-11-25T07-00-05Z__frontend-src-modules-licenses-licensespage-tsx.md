---
target: "the license register, Task 10: frontend/src/modules/licenses/LicensesPage.tsx"
total_score: 29
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/licenses/LicensesPage.tsx"
target_fingerprint: "sha256:ffd84e03175519081ec6f366a20bf3649d0c34c990cb681bb1a32834ad9c3c6b"
target_path: frontend/src/modules/licenses/LicensesPage.tsx
timestamp: 2026-11-25T07-00-05Z
slug: frontend-src-modules-licenses-licensespage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: the license register (`frontend/src/modules/licenses/LicensesPage.tsx`, `/licenses`)

Redesign plan, Task 10 (wave O5), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (1440x900 and 390x844, English and Spanish) before any detector output; Assessment B followed (the CLI detector from the worktree root, then `detect.js` injected in Chromium).

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | The kit's loading, the tiles' n/a until read, Showing x of y |
| 2 | Match System / Real World | 3 | Types in words; the tile row names products, not ids |
| 3 | User Control and Freedom | 3 | Search, type and Client chips in the URL; Back undoes a chip |
| 4 | Consistency and Standards | 3 | Receta L to the letter, the same tiles as before |
| 5 | Error Prevention | 3 | New license only with licenses.manage; the dialog names each refusal |
| 6 | Recognition Rather Than Recall | 3 | Vendor and PO under each product, the client column with every client |
| 7 | Flexibility and Efficiency | 3 | Search over product, type, PO and agreement; every view a link |
| 8 | Aesthetic and Minimalist Design | 3 | Readiness, four tiles and the list compete above the fold at 1440 |
| 9 | Error Recovery | 3 | Nothing matches these filters with Clear filters; a failed tile says why once |
| 10 | Help and Documentation | 2 | No help beyond the page sentence and the readiness panel |
| **Total** | | **29/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** A register an MSP technician can read at a glance: what is over-deployed and what renews first lead the page, each tile opens the list behind it, and the list narrows to one client from the client 360 without touching the console's choice. Nothing here could belong to an unrelated product: the tiles count products and seats, not generic metrics.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/licenses` from the worktree root: exit 0, `[]`. Harmony (es) on `/licenses` at four viewports: no stray token, no overflow, no fixture miss, no serious axe finding.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh context per page on this worktree's own `npx vite` on 5291 in its own process group with `?__fixtures`, title marked `[Human]`; `impeccable live-server --background` on 8400, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). /licenses en 1440: 14 findings (`gray-on-color` x1, `nested-cards` x1, `overused-font` x1, `straggler` x1, `undersized-ui-text` x10); /licenses es 390: 10 findings (`nested-cards` x1, `overused-font` x1, `straggler` x1, `undersized-ui-text` x7). `undersized-ui-text` is the 10.5px table caption step (DESIGN.md's Caption, the PRODUCT.md floor), `overused-font` is Inter and `gray-on-color` the rail's tenant card: false positives already answered in the shell's critique. Any `nested-cards`, `straggler` or `cramped-padding` on `ol.steps` is the shared readiness panel (`components/prerequisites`), outside this wave's files.

## Overall Impression

The page does the register's job and is consistent with the other waves' lists. The weight is in the right place (the over-deployed tile is the only washed one). What remains is the type column, which still wears the muted minus glyph that means 'not applicable' on four of the five types.

## What's Working

- Every tile links to the page its number lives on, and under the Client chip to the same client's rows (ruling 2)
- The type chips count the register itself and disappear when the register holds one type: no filter that cannot narrow
- New license from ?new=license, mounted only while open, asks before a typed form is lost

## Priority Issues

### [P2] The license type reads as 'not applicable'
- **What**: Four of the five types draw the muted minus glyph in the Type column
- **Why it matters**: The minus is the console's 'no value' mark; on a type it reads as missing data, the same defect the subscriptions page fixed for the billing cycle by drawing it as text
- **Fix**: Draw the type as text in the list (as the cycle is), keeping the pill on the record head; or give every type the info kind (licenseState.ts)

## Persona Red Flags

- Technician at 390: the table scrolls sideways after Product and Client, so Entitlements and Key are one swipe away (the kit's table hint says so)
- Auditor: the register has no export here; the compliance page and the reports carry the evidence

## Minor Observations

- The Client column stays with every client chosen, as design §3.4 asks; with one chosen it goes
- The search placeholder is long and clips at 1440 beside the chips (the kit's new search ceiling)

## Questions

Questions skipped: 0 priority issues
