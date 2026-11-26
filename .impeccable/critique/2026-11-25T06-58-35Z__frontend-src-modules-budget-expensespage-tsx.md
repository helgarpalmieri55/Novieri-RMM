---
target: "Expenses, wave O6: frontend/src/modules/budget/ExpensesPage.tsx"
total_score: 30
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/budget/ExpensesPage.tsx"
target_fingerprint: "sha256:73a8df0dd141edea06fbf1a531c10c3fb89958ba96a87dbe2d7c1d38c69104d3"
target_path: frontend/src/modules/budget/ExpensesPage.tsx
timestamp: 2026-11-25T06-58-35Z
slug: frontend-src-modules-budget-expensespage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Expenses (`frontend/src/modules/budget/ExpensesPage.tsx`, `/budget/expenses`)

Redesign plan, Task 11 (wave O6), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (`/budget/expenses`, 1440x900, 390x844, 390x844-client and 641x900-client, Spanish) and this run's own captures with a client chosen (English, 1440) before any detector output; Assessment B followed.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | "Showing 11"; the upload's outcome said |
| 2 | Match System / Real World | 3 | Status and source in words; dates pinned to the calendar day |
| 3 | User Control and Freedom | 3 | Status, category and range in the URL; New and Edit dialogs in the URL, with Back |
| 4 | Consistency and Standards | 3 | Receta L: chips, the category chip, the kit's empty states |
| 5 | Error Prevention | 3 | Money stays a string; the dialogs name what is wrong; leaving a typed dialog asks |
| 6 | Recognition Rather Than Recall | 3 | Description over category and invoice reference |
| 7 | Flexibility and Efficiency | 3 | A filtered ledger is a link |
| 8 | Aesthetic and Minimalist Design | 3 | The range fields show the browser's date mask with no echo in the console's format |
| 9 | Error Recovery | 3 | "Nothing matches these filters" with Clear; "No expenses found." with New expense |
| 10 | Help and Documentation | 3 | The materialise dialog says what it adds |
| **Total** | | **30/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** A ledger with finance's habits: amounts as written, invoices attached, expected rows fed from renewals. Specific.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/budget` from the worktree root: exit 0, `[]`.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh page per route on this worktree's own Vite with `?__fixtures`, a client chosen, title marked `[Human]`; `impeccable live-server --background` on 8401, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). `/budget/expenses` en 1440: 8 findings, all of the kinds below. `undersized-ui-text` on the 10.5px captions (tile labels, table headers, chip counts) is a false positive: DESIGN.md's Caption step is 10.5px and PRODUCT.md sets the floor there. `gray-on-color` (#8592ab on #1b2945) is the rail's tenant card and `overused-font` (Inter) is the brand's one face, both the shell's and both explained in its critique.

## Overall Impression

Good. The filters are where they should be and in the URL; the range fields read in the browser's mask.

## What's Working

- The four filters travel to the API as the same fields as before.
- Edit is `?edit=<id>`, so Back closes it; a dialog with changes asks before it is dropped.

## Priority Issues

- **[P3] The range fields have no echo.** `mm/dd/yyyy` on an en-US browser in a console that writes `22 Sept 2026`; the materialise dialog spells its range back, the bar does not. **Fix:** Leave for now; the active chip and the rows' dates say what was filtered. **Command:** /impeccable clarify
- **[P3] The expense column is narrow at 641.** Titles wrap to three lines. **Fix:** Leave: the table scrolls. **Command:** /impeccable adapt

## Persona Red Flags

**Alex.** Filters by status and range in the URL. No red flag.

**Casey.** The table scrolls sideways. No other red flag.

Questions skipped: 2 priority issues

## Polish (same day, before `close`)

- Nothing to polish (two P3 left as they are).
- Step 5: harmony on the wave's sixteen routes again, both languages, four viewports, after the polish (the wave's report lists the PNGs).
