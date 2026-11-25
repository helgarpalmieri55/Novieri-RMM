---
target: "the ticket queue (wave O2, 2.2): frontend/src/modules/service-desk/TicketListPage.tsx"
total_score: 27
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 1
target_identity: "file:/home/user/rmm-w2/frontend/src/modules/service-desk/TicketListPage.tsx"
target_fingerprint: "sha256:d1f6bfd0b89d456a113a6d746a4c338e9b292155e542d9645250b2f5a391657d"
target_path: /home/user/rmm-w2/frontend/src/modules/service-desk/TicketListPage.tsx
timestamp: 2026-11-25T04-26-24Z
slug: c-modules-service-desk-ticketlistpage-tsx-63a6a08a
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: the ticket queue (`frontend/src/modules/service-desk/TicketListPage.tsx`, `/service-desk`)

Redesign plan, Task 7 (wave O2), "Cierre de cada oleada", step 3. Assessment A was written from the source and the
harmony captures (/service-desk: 1440x900, 390x844, 390x844-client, 641x900-client; es and en) before the detector
output; Assessment B followed. A and B in series, one context.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | «Mostrando x de y», the truncation notice, the pressed scope chip |
| 2 | Match System / Real World | 3 | Statuses, priorities and origins in words |
| 3 | User Control and Freedom | 3 | Every filter in the URL; Back undoes; Clear filters |
| 4 | Consistency and Standards | 3 | The kit's toolbar, chips and chip-selects |
| 5 | Error Prevention | 2 | Eleven status chips (fixed in polish) |
| 6 | Recognition Rather Than Recall | 3 | Each filter names itself on its chip |
| 7 | Flexibility and Efficiency | 3 | Scope first, then status, then the rest |
| 8 | Aesthetic and Minimalist Design | 2 | Toolbar took five lines at 1440 (fixed) |
| 9 | Error Recovery | 3 | The kit's error state, said |
| 10 | Help and Documentation | 2 | No help beyond the labels |
| | **Total** | **27/40** | |

## Anti-patterns verdict

Not slop. The one pattern risk was density: every status as a chip.

## What works

- Scope chips with All open as the chip that writes nothing; Mine/Unassigned still drop the assignee.
- The Client chip narrows the read only; `/service-desk?client=<id>` from the 360 lands narrowed.
- An empty queue says whether a filter is the cause.

## Priority issues

- **P1** The status as eleven chips wrapped to three lines at 1440 ("Rechazado" alone on the third), the Client chip on a fourth, and pushed the first ticket below the fold. Fixed in polish: the status is a named chip-select beside the scope chips (55378cf).
- **P2** The sort select wraps alone onto a third row of the second filter bar at 1440 in Spanish.
- **P3** "Tickets mostrados" tile and "Mostrando x de y" say related numbers twice (the tile counts rows on screen by design).

## Assessment B (detector)

- Static: `impeccable detect --json frontend/src/modules/service-desk` from the worktree root: **0 findings**.
- Browser (`npx vite` + `impeccable live-server`, Chromium on `/service-desk?__fixtures`, es and en, 1440 and 390): only
  system-wide findings on this route, none caused by the page: `undersized-ui-text` on 10.5px captions (DESIGN.md's
  Caption step, "The Floor Is The Ramp's Smallest Step"), `overused-font` Inter (the design's one face),
  `gray-on-color` `#8592ab` on `#1b2945` (the rail's dim ink, shell). Proposed ignores for the coordinator are in
  the wave report.

## Questions

Settled by DESIGN.md, the brief and critica-respuestas.md; none open.

Questions skipped: 1 priority issues
