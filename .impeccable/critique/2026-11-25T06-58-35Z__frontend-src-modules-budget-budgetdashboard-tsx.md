---
target: "IT budget, wave O6: frontend/src/modules/budget/BudgetDashboard.tsx"
total_score: 29
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/budget/BudgetDashboard.tsx"
target_fingerprint: "sha256:da9ab297cab53b25a23fd9bf8fb0a64e388c92346ce3373b0ecc9536a3c0a748"
target_path: frontend/src/modules/budget/BudgetDashboard.tsx
timestamp: 2026-11-25T06-58-35Z
slug: frontend-src-modules-budget-budgetdashboard-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: IT budget (`frontend/src/modules/budget/BudgetDashboard.tsx`, `/budget`)

Redesign plan, Task 11 (wave O6), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (`/budget`, 1440x900, 390x844, 390x844-client and 641x900-client, Spanish) and this run's own captures with a client chosen (English, 1440) before any detector output; Assessment B followed.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Compact money with the exact amount under it; the unconvertible notice names what is excluded |
| 2 | Match System / Real World | 3 | "YTD actual", "% of plan", the period in words |
| 3 | User Control and Freedom | 3 | Budget and period in the URL; the module's links |
| 4 | Consistency and Standards | 3 | Rail label as the h1; the module's SectionNav like the other modules' |
| 5 | Error Prevention | 3 | Nothing is summed in the browser; one period at a time |
| 6 | Recognition Rather Than Recall | 3 | The client the figures are for is in the head's sentence |
| 7 | Flexibility and Efficiency | 3 | A budget or period view is a link |
| 8 | Aesthetic and Minimalist Design | 2 | The variance table is every category times every period (44 rows for one budget), under a chart of one period |
| 9 | Error Recovery | 3 | "Add an exchange rate" beside the excluded amounts |
| 10 | Help and Documentation | 3 | The chart note says the figures are converted to the base currency |
| **Total** | | **29/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** A budget page in finance's terms (plan, actual, committed, projected), honest about what it cannot add up. Specific.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/budget` from the worktree root: exit 0, `[]`.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh page per route on this worktree's own Vite with `?__fixtures`, a client chosen, title marked `[Human]`; `impeccable live-server --background` on 8401, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). `/budget` en 1440: 14 findings. `nested-cards` is the variance panel's notice (the unconvertible expense) inside its panel, the kit's `Notice`; the rest as below. `undersized-ui-text` on the 10.5px captions (tile labels, table headers, chip counts) is a false positive: DESIGN.md's Caption step is 10.5px and PRODUCT.md sets the floor there. `gray-on-color` (#8592ab on #1b2945) is the rail's tenant card and `overused-font` (Inter) is the brand's one face, both the shell's and both explained in its critique.

## Overall Impression

Good. The page is long because the variance table lists every period; the chart already chose one.

## What's Working

- The chart's three series differ by hatch and legend, not by colour alone (the "Actual" series in the kit's grey).
- With every client chosen the page asks for one rather than adding money across currencies.
- Each tile leads to the page that explains its number: the ledger, or the planner for "% of plan".

## Priority Issues

- **[P2] The variance table lists every period.** 44 rows for one budget at 641; the chart above shows one period and the table does not follow it. **Fix:** A later change: show the chosen period's rows by default with the whole year one click away. Not in this wave (what is shown stays as today). **Command:** /impeccable distill
- **[P3] "Choose a client at the top" is an h3 under the h1.** axe `heading-order` (moderate) on the five budget pages with every client chosen. **Fix:** `BudgetPickClient` renders its title as an h2 (EmptyState `headingLevel={2}`). **Command:** /impeccable polish
- **[P3] The budget chip is wide.** "Budget FY26 IT operating (2026)" takes a third of the head at 641. **Fix:** Leave: it names the budget shown. **Command:** /impeccable layout

## Persona Red Flags

**Alex.** Switches budget and period from the head and the chart; both are links. No red flag.

**Casey (phone).** The variance table is a long scroll (P2).

**Sam.** The heading outline skips a level with every client chosen (P3).

## Questions to Consider

1. **Should the variance table follow the chart's period?** It lists every period of the year under a chart of one. Not settled by the sources, and the wave's rule is that what a page shows stays as it is: left as it is, and asked of the coordinator in the wave's report.
2. **Is "Choose a client at the top" an h3?** Settled by the heading outline (axe `heading-order`) and DESIGN.md's type scale: it is the page's only section, so an h2. Polished.
3. **Is the budget chip too wide in the head?** Settled by the brief (6.3: the chosen budget as a `.chip-select` in the head): it names the budget shown.

Answers 2 and 3 are appended to `.impeccable/critica-respuestas.md`; 1 goes to the coordinator.

## Polish (same day, before `close`)

- P3, "Choose a client at the top": `BudgetPickClient` draws its title as an h2 (`headingLevel={2}`); the test "with every client chosen, asks for one…" asserts the h2 and was seen red with the h3.
- P2, the variance table and P3, the budget chip: left as they are (questions 1 and 3).
- Step 5: harmony on the wave's sixteen routes again, both languages, four viewports, after the polish (the wave's report lists the PNGs).
