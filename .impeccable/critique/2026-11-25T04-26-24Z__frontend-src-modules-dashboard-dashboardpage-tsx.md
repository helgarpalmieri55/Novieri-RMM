---
target: "Inicio (wave O2, 2.1): frontend/src/modules/dashboard/DashboardPage.tsx"
total_score: 30
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:/home/user/rmm-w2/frontend/src/modules/dashboard/DashboardPage.tsx"
target_fingerprint: "sha256:c327130a695509116945367f1362d2997cc0f01efee866b4c3f0aa87ce254bfb"
target_path: /home/user/rmm-w2/frontend/src/modules/dashboard/DashboardPage.tsx
timestamp: 2026-11-25T04-26-24Z
slug: frontend-src-modules-dashboard-dashboardpage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Inicio, the dashboard (`frontend/src/modules/dashboard/DashboardPage.tsx`, `/dashboard`)

Redesign plan, Task 7 (wave O2), "Cierre de cada oleada", step 3. Assessment A was written from the source and the
harmony captures (/dashboard: 1440x900, 390x844, 390x844-client, 641x900-client; es and en) before the detector
output; Assessment B followed. A and B in series, one context.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Needs-you list first, every tile a figure or a sentence saying why not |
| 2 | Match System / Real World | 3 | «Inicio», the rail's word (D13); tiles in the team's words |
| 3 | User Control and Freedom | 4 | Edit is a draft: Discard undoes a hidden tile with no call |
| 4 | Consistency and Standards | 3 | Edit is secondary, the kit's save bar; Done disabled with its reason while unsaved |
| 5 | Error Prevention | 3 | Removing a tile is undoable now |
| 6 | Recognition Rather Than Recall | 3 | Each tile names its destination |
| 7 | Flexibility and Efficiency | 3 | Arrangement per person, moves by button and by drag |
| 8 | Aesthetic and Minimalist Design | 3 | Long page, but ordered by urgency |
| 9 | Error Recovery | 3 | Save failure keeps the arrangement and says so |
| 10 | Help and Documentation | 2 | Edit mode explains itself once in its toolbar |
| | **Total** | **30/40** | |

## Anti-patterns verdict

Not slop: one grid, real data, tiles that lead to their lists. The panels' `dl.facts` inside a panel are read by the detector as a card in a card; it is the kit's facts block (viz.tsx), not a second elevation.

## What works

- "Requiere tu atención" leads, then readiness, then the arrangement.
- A KPI tile with no figure now says "Todavía sin contar." or "No se pudo leer, así que no se cuenta nada." (Atera leaves them blank).
- Edit is undoable: Discard restores the arrangement without a server call; Save writes the same whole-arrangement PUT.

## Priority issues

- **P2** "Mesa de ayuda" sits in a fixed row height beside "La flota de un vistazo" and leaves an empty band under its three facts at 1440 (layout of the grid's sizes, not of this wave's change).
- **P3** Descriptions in two panels end with a one-word straggler at 1440 ("parches", "seguridad").

## Assessment B (detector)

- Static: `impeccable detect --json frontend/src/modules/dashboard` from the worktree root: **0 findings**.
- Browser (`npx vite` + `impeccable live-server`, Chromium on `/dashboard?__fixtures`, es and en, 1440 and 390): only
  system-wide findings on this route, none caused by the page: `undersized-ui-text` on 10.5px captions (DESIGN.md's
  Caption step, "The Floor Is The Ramp's Smallest Step"), `overused-font` Inter (the design's one face),
  `gray-on-color` `#8592ab` on `#1b2945` (the rail's dim ink, shell), `nested-cards` on `dl.facts` inside the fleet and service desk panels (the kit's facts block). Proposed ignores for the coordinator are in
  the wave report.

## Questions

Settled by DESIGN.md, the brief and critica-respuestas.md; none open.

Questions skipped: 0 priority issues
