---
target: "My tickets (wave O2, 2.9): frontend/src/modules/portal/MyTicketsPage.tsx"
total_score: 29
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:/home/user/rmm-w2/frontend/src/modules/portal/MyTicketsPage.tsx"
target_fingerprint: "sha256:4f2e5342c1bc2fd2bcde63778f44608017eb065440b0028cbfb9c53025c8ebc8"
target_path: /home/user/rmm-w2/frontend/src/modules/portal/MyTicketsPage.tsx
timestamp: 2026-11-25T04-26-25Z
slug: frontend-src-modules-portal-myticketspage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: the portal's My tickets (`frontend/src/modules/portal/MyTicketsPage.tsx`, `/portal`)

Redesign plan, Task 7 (wave O2), "Cierre de cada oleada", step 3. Assessment A was written from the source and the
harmony captures (/portal: 1440x900, 390x844, 390x844-client, 641x900-client; es and en) before the detector
output; Assessment B followed. A and B in series, one context.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | «Mostrando 1 de 2», Open/All chips |
| 2 | Match System / Real World | 3 | Plain words for a requester |
| 3 | User Control and Freedom | 3 | Filters in the URL |
| 4 | Consistency and Standards | 3 | Kit list |
| 5 | Error Prevention | 3 | Nothing to edit |
| 6 | Recognition Rather Than Recall | 3 | New request in the head |
| 7 | Flexibility and Efficiency | 3 | One click to file |
| 8 | Aesthetic and Minimalist Design | 3 | One list |
| 9 | Error Recovery | 3 | Empty says whether Open hides them |
| 10 | Help and Documentation | 2 | None |
| | **Total** | **29/40** | |

## Anti-patterns verdict

Not slop.

## What works

- Open (writes nothing) and All (`state=all`), no invented counts.
- An empty list tells "none filed" from "none open", and offers New ticket only to someone who may file one.

## Priority issues

- **P3** The prerequisites panel (staff only) leads the page above the list in the console; a requester never sees it.

## Assessment B (detector)

- Static: `impeccable detect --json frontend/src/modules/portal` from the worktree root: **0 findings**.
- Browser (`npx vite` + `impeccable live-server`, Chromium on `/portal?__fixtures`, es and en, 1440 and 390): only
  system-wide findings on this route, none caused by the page: `undersized-ui-text` on 10.5px captions (DESIGN.md's
  Caption step, "The Floor Is The Ramp's Smallest Step"), `overused-font` Inter (the design's one face),
  `gray-on-color` `#8592ab` on `#1b2945` (the rail's dim ink, shell), `nested-cards` on the staff-only prerequisites steps. Proposed ignores for the coordinator are in
  the wave report.

## Questions

Settled by DESIGN.md, the brief and critica-respuestas.md; none open.

Questions skipped: 0 priority issues
