---
target: "a portal ticket (wave O2, 2.11): frontend/src/modules/portal/PortalTicketDetailPage.tsx"
total_score: 30
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:/home/user/rmm-w2/frontend/src/modules/portal/PortalTicketDetailPage.tsx"
target_fingerprint: "sha256:910f43184e2c0ffd1fa17a04a361f2b28612349de9d39c70463e65d26855f70e"
target_path: /home/user/rmm-w2/frontend/src/modules/portal/PortalTicketDetailPage.tsx
timestamp: 2026-11-25T04-26-25Z
slug: modules-portal-portalticketdetailpage-tsx-4c1b9065
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: a portal ticket (`frontend/src/modules/portal/PortalTicketDetailPage.tsx`, `/portal/tickets/tkt-1`)

Redesign plan, Task 7 (wave O2), "Cierre de cada oleada", step 3. Assessment A was written from the source and the
harmony captures (/portal/tickets/tkt-1: 1440x900, 390x844, 390x844-client, 641x900-client; es and en) before the detector
output; Assessment B followed. A and B in series, one context.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | State pill, history, posting state |
| 2 | Match System / Real World | 3 | "Novedad" for staff replies |
| 3 | User Control and Freedom | 3 | Back to My tickets |
| 4 | Consistency and Standards | 3 | DetailHead and DetailGrid |
| 5 | Error Prevention | 3 | Reply disabled when empty |
| 6 | Recognition Rather Than Recall | 3 | Details beside the conversation |
| 7 | Flexibility and Efficiency | 3 | Attach and download in place |
| 8 | Aesthetic and Minimalist Design | 3 | No tabs |
| 9 | Error Recovery | 3 | Failures said in sentences |
| 10 | Help and Documentation | 3 | Assistant in the ticket |
| | **Total** | **30/40** | |

## Anti-patterns verdict

Not slop.

## What works

- "#INC-4127 …" with its state; the tab is "#INC-4127 · Novieri RMM" in the contact's frame.
- Conversation and attachments left, details/history/assistant right.

## Priority issues

- **P3** The assistant's intro ends with a one-word straggler at 1440.

## Assessment B (detector)

- Static: `impeccable detect --json frontend/src/modules/portal` from the worktree root: **0 findings**.
- Browser (`npx vite` + `impeccable live-server`, Chromium on `/portal/tickets/tkt-1?__fixtures`, es and en, 1440 and 390): only
  system-wide findings on this route, none caused by the page: `undersized-ui-text` on 10.5px captions (DESIGN.md's
  Caption step, "The Floor Is The Ramp's Smallest Step"), `overused-font` Inter (the design's one face),
  `gray-on-color` `#8592ab` on `#1b2945` (the rail's dim ink, shell). Proposed ignores for the coordinator are in
  the wave report.

## Questions

Settled by DESIGN.md, the brief and critica-respuestas.md; none open.

Questions skipped: 0 priority issues
