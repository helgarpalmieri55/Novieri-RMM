---
target: "the shell, Task 4: frontend/src/shell/AreaPage.tsx"
total_score: 31
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/shell/AreaPage.tsx"
target_fingerprint: "sha256:bf67fd6f45fe4e9a83551bbc043478854ca5d958b1381f157c5a929a4fc0a659"
target_path: frontend/src/shell/AreaPage.tsx
timestamp: 2026-11-24T23-59-19Z
slug: frontend-src-shell-areapage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: the Admin index (`frontend/src/shell/AreaPage.tsx`, `/admin`)

Redesign plan, Task 4, Step 12. One of the 86 pages: the landing page Admin's title and the breadcrumb lead to.
Assessment A before any detector output, then Assessment B. Captures: `/admin` at 1440, 390, 390 with a client
and 641 with a client, both languages.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Loading and "nothing open to your account" come from `QueryState` |
| 2 | Match System / Real World | 3 | Each page with the line that says what it is for |
| 3 | User Control and Freedom | 3 | "Clear filters" empties the search |
| 4 | Consistency and Standards | 3 | The kit's toolbar and empty state; the same search as the Admin panel |
| 5 | Error Prevention | 3 | Only pages the person may open (the rail's own rule) |
| 6 | Recognition Rather Than Recall | 4 | Every page named, iconed and explained |
| 7 | Flexibility and Efficiency | 3 | Search by any part of a name, accents ignored |
| 8 | Aesthetic and Minimalist Design | 2 | A full-width panel around one search field; cards of very different heights leave holes |
| 9 | Error Recovery | 3 | "Nothing matches these filters." with the way out |
| 10 | Help and Documentation | 4 | The hints are the page's whole point |
| **Total** | | **31/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** An index built from the same model as the rail, so it cannot offer a page that answers
403, and it explains each page in the product's own words. The look is the kit's (cards with the one shadow,
the toolbar, the empty state). Plain, which an index should be.

**Deterministic scan.** `impeccable detect --json frontend/src/shell/AreaPage.tsx`: exit 0, no findings. The
harmony gate on `/admin`, both languages, four viewports: no serious violation; the only moderate one is the
round button's `region` (see the top bar's critique).

**Visual overlays.** `detect.js` was injected through Chromium (Playwright, a fresh page per case on this worktree's own `npx vite` with `?__fixtures`, title marked `[Human]`; `impeccable live-server --background` on 8400, stopped with `--keep-inject`; no project file changed; no person's browser was attached, so no overlay is visible to anyone). Cases: `/dashboard` en 1440 (and again with the New menu open), `/licenses` en 1440, `/audit` es 1440, `/admin` es 1440 and en 390, `/assistant` en 1440, `/dashboard` es 390. On `/admin` (es 1440, en 390): only `overused-font` and the rail's `gray-on-color`, both explained in the rail's critique; nothing on the cards, the toolbar or the links.

## Overall Impression

Useful and honest; it reads as a table of contents. The weakest part is the composition: a white strip that
holds one field, and two columns of cards whose heights differ by half.

## What's Working

- Every page carries the line that says what it is for, which the rail cannot.
- The search ignores case and accents and empties into the kit's "nothing matches" with its way out.
- Built from `visibleGroups`, so the index and the rail cannot disagree about what a person may open.

## Priority Issues

- **[P2] The search sits alone in a full-width panel.** The kit's toolbar belongs flush on a list's panel; here
  no list follows it in the same panel, so it is a white box around one control, 1160px wide with 470px of
  nothing. **Fix:** a question for the coordinator (below): the layout is the plan's. **Command:** /impeccable layout
- **[P3] Cards of very different heights in a two-column grid.** Automation (two pages) sits beside Computers
  (five) or Security (three), leaving holes; Platform is alone on its row. **Fix:** none now; a masonry or a
  single column is a redesign of the plan's grid. **Command:** /impeccable layout
- **[P3] The Admin panel repeats the first card.** On `/admin` no page is current, so the panel opens its first
  section, the same pages as the first card. **Fix:** none; the panel is the rail and the cards are the page.
  **Command:** /impeccable distill

## Persona Red Flags

**Jordan (first-timer).** The index is where Jordan learns what Admin holds; the hints answer that.

**Sam (keyboard, screen reader).** Each card is a section with an `h2`; the links are plain links named by the
page; icons are decorative. No red flag.

## Minor Observations

- The cards' `h2` and the page's `h1` give a clean outline; harmony reports no `heading-order` here.

## Questions to Consider

Not settled by DESIGN.md, the approved design or the operator's decisions (design §1.1 does not describe the
`/admin` index; DESIGN.md's "Lists" is for lists), so they go to the coordinator in the task's report:

1. The search's panel: (a) keep the kit toolbar in its bare panel, as the plan draws it; (b) the field alone
   above the cards, with no panel; (c) the field in the page head's action row.
2. The cards' grid: (a) two columns as now; (b) one column; (c) CSS columns, so cards stack without holes.
3. The Admin panel on `/admin`: (a) open its first section, as now; (b) open none (no page is current there).

## Polish (same day, before `close`)

Nothing changed in `AreaPage.tsx`: its three issues are P2 and P3 and their questions are not settled by
DESIGN.md, the approved design or the operator's decisions; they go to the coordinator in the task's report.
