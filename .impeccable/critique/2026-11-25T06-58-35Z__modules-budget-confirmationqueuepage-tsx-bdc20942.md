---
target: "Confirmation queue, wave O6: frontend/src/modules/budget/ConfirmationQueuePage.tsx"
total_score: 30
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/budget/ConfirmationQueuePage.tsx"
target_fingerprint: "sha256:ca4a16fd11d3257de279a4960791abbf57e2411933d5f715a8841b340757144b"
target_path: frontend/src/modules/budget/ConfirmationQueuePage.tsx
timestamp: 2026-11-25T06-58-35Z
slug: modules-budget-confirmationqueuepage-tsx-bdc20942
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Confirmation queue (`frontend/src/modules/budget/ConfirmationQueuePage.tsx`, `/budget/confirm`)

Redesign plan, Task 11 (wave O6), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (`/budget/confirm`, 1440x900, 390x844, 390x844-client and 641x900-client, Spanish) and this run's own captures with a client chosen (English, 1440) before any detector output; Assessment B followed.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | "Showing 3"; a refused decision said |
| 2 | Match System / Real World | 3 | "Expected amount and decision" |
| 3 | User Control and Freedom | 3 | Each decision asks first |
| 4 | Consistency and Standards | 3 | Receta L, 8 |
| 5 | Error Prevention | 3 | The amount read back before it is confirmed |
| 6 | Recognition Rather Than Recall | 3 | Decision beside the amount |
| 7 | Flexibility and Efficiency | 3 | One row, one decision |
| 8 | Aesthetic and Minimalist Design | 3 | The date wraps to three lines at 641; the status column repeats "Expected" |
| 9 | Error Recovery | 3 | "No expenses waiting for confirmation." |
| 10 | Help and Documentation | 3 | The page sentence says what waits |
| **Total** | | **30/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** The worklist that makes expected spend real; specific.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/budget` from the worktree root: exit 0, `[]`.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh page per route on this worktree's own Vite with `?__fixtures`, a client chosen, title marked `[Human]`; `impeccable live-server --background` on 8401, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). `/budget/confirm` en 1440: 6 findings, all of the kinds below. `undersized-ui-text` on the 10.5px captions (tile labels, table headers, chip counts) is a false positive: DESIGN.md's Caption step is 10.5px and PRODUCT.md sets the floor there. `gray-on-color` (#8592ab on #1b2945) is the rail's tenant card and `overused-font` (Inter) is the brand's one face, both the shell's and both explained in its critique.

## Overall Impression

Good; only the date column wraps.

## What's Working

- Confirm reads the corrected amount back in its dialog; void says it is terminal.
- An empty queue says why.

## Priority Issues

- **[P3] The date wraps.** "24 nov 2026" on three lines at 641. **Fix:** Keep the date on one line (`nowrap`), as the ledger does. **Command:** /impeccable polish

## Persona Red Flags

**Alex.** One row, one decision. No red flag.

Questions skipped: 1 priority issues

## Polish (same day, before `close`)

- P3, the date: kept on one line (`white-space: nowrap`).
- Step 5: harmony on the wave's sixteen routes again, both languages, four viewports, after the polish (the wave's report lists the PNGs).
