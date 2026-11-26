---
target: "Budget planner, wave O6: frontend/src/modules/budget/BudgetPlannerPage.tsx"
total_score: 29
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/budget/BudgetPlannerPage.tsx"
target_fingerprint: "sha256:687a7559a640ce4707064fc4fd4cbbcd9a7caad0206e70a4c35767b5ebe1fad5"
target_path: frontend/src/modules/budget/BudgetPlannerPage.tsx
timestamp: 2026-11-25T06-58-35Z
slug: frontend-src-modules-budget-budgetplannerpage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Budget planner (`frontend/src/modules/budget/BudgetPlannerPage.tsx`, `/budget/planner`)

Redesign plan, Task 11 (wave O6), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (`/budget/planner`, 1440x900, 390x844, 390x844-client and 641x900-client, Spanish) and this run's own captures with a client chosen (English, 1440) before any detector output; Assessment B followed.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | The save bar says unsaved, saving, saved; Close asks for the name typed back |
| 2 | Match System / Real World | 3 | Periods P1..P12; status in words |
| 3 | User Control and Freedom | 3 | The budget in the URL; Discard; leaving with changes asks |
| 4 | Consistency and Standards | 3 | Receta FO: one draft, one save bar, the kit's guard |
| 5 | Error Prevention | 3 | Closing is typed back; deleting a line asks with its amount |
| 6 | Recognition Rather Than Recall | 3 | The open budget is the head's chip; its list when none is open |
| 7 | Flexibility and Efficiency | 3 | A budget is a link; New budget opens the budget it made |
| 8 | Aesthetic and Minimalist Design | 2 | A delete "×" beside every saved cell doubles the grid's width: six of twelve periods fit at 1440 |
| 9 | Error Recovery | 3 | The server's refusal on the bar; an empty name named |
| 10 | Help and Documentation | 3 | "Closing freezes all line edits and cannot be undone." beside Close |
| **Total** | | **29/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** A planning grid with finance's guard rails (no float on money, one-way close, typed confirmation). Specific.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/budget` from the worktree root: exit 0, `[]`.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh page per route on this worktree's own Vite with `?__fixtures`, a client chosen, title marked `[Human]`; `impeccable live-server --background` on 8401, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). `/budget/planner?budget=bud-1` en 1440: 10 findings, all of the kinds below. `undersized-ui-text` on the 10.5px captions (tile labels, table headers, chip counts) is a false positive: DESIGN.md's Caption step is 10.5px and PRODUCT.md sets the floor there. `gray-on-color` (#8592ab on #1b2945) is the rail's tenant card and `overused-font` (Inter) is the brand's one face, both the shell's and both explained in its critique.

## Overall Impression

Good. One bar for details and grid removed the two separate Save buttons; the grid is wide because every saved cell carries its delete.

## What's Working

- The budget open is `?budget=`, so a planner is a link; switching budgets with changes asks first.
- Save sends the details only if they changed and the grid only if it changed, the same lines as before.
- New budget opens from `?new=budget`, starts empty however it opens, and lands on the budget it made.

## Priority Issues

- **[P2] A delete beside every saved cell.** Twelve inputs and twelve icons per row: half the year scrolls out of view at 1440. **Fix:** A later change (move the delete into the cell's clear, or a row menu); the grid's actions stay as they are in this wave. **Command:** /impeccable distill
- **[P3] The chip and the list say the same thing on the list view.** "Choose a budget" in the chip above a list of the budgets. **Fix:** Leave: the chip is the switch once a budget is open. **Command:** /impeccable distill

## Persona Red Flags

**Alex.** Types across a row, saves once. No red flag.

**Casey (phone).** The grid scrolls sideways; expected for twelve periods.

Questions skipped: 2 priority issues

## Polish (same day, before `close`)

- Nothing to polish (P2 and P3 left as they are; the grid's actions are behaviour this wave keeps).
- Step 5: harmony on the wave's sixteen routes again, both languages, four viewports, after the polish (the wave's report lists the PNGs).
