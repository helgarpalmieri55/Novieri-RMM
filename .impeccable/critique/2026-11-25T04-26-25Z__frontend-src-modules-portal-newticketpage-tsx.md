---
target: "New ticket (wave O2, 2.10): frontend/src/modules/portal/NewTicketPage.tsx"
total_score: 32
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:/home/user/rmm-w2/frontend/src/modules/portal/NewTicketPage.tsx"
target_fingerprint: "sha256:00f96eeeda6782c5995caf6dec11cc9dd76cdd5d3195b0e1a783ec50b6fe4a1f"
target_path: /home/user/rmm-w2/frontend/src/modules/portal/NewTicketPage.tsx
timestamp: 2026-11-25T04-26-25Z
slug: frontend-src-modules-portal-newticketpage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: the portal's New ticket (`frontend/src/modules/portal/NewTicketPage.tsx`, `/portal/new`)

Redesign plan, Task 7 (wave O2), "Cierre de cada oleada", step 3. Assessment A was written from the source and the
harmony captures (/portal/new: 1440x900, 390x844, 390x844-client, 641x900-client; es and en) before the detector
output; Assessment B followed. A and B in series, one context.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Submit says pending and failure |
| 2 | Match System / Real World | 4 | Urgency in sentences |
| 3 | User Control and Freedom | 3 | Back to My tickets; asks before losing text |
| 4 | Consistency and Standards | 3 | FormPage and one section |
| 5 | Error Prevention | 4 | Every missing field named |
| 6 | Recognition Rather Than Recall | 3 | Help beside the form |
| 7 | Flexibility and Efficiency | 3 | Enter files it |
| 8 | Aesthetic and Minimalist Design | 3 | One section |
| 9 | Error Recovery | 3 | Upload resumes where it failed |
| 10 | Help and Documentation | 3 | Attachment limits stated |
| | **Total** | **32/40** | |

## Anti-patterns verdict

Not slop.

## What works

- Leaving with text written asks; filing goes to the new ticket without asking (release after the save).
- Help suggestions on the right on a desktop.

## Priority issues

- **P2** Below 1024px the help suggestions come after the form (DetailGrid order); they matter most while typing the subject.

## Assessment B (detector)

- Static: `impeccable detect --json frontend/src/modules/portal` from the worktree root: **0 findings**.
- Browser (`npx vite` + `impeccable live-server`, Chromium on `/portal/new?__fixtures`, es and en, 1440 and 390): only
  system-wide findings on this route, none caused by the page: `undersized-ui-text` on 10.5px captions (DESIGN.md's
  Caption step, "The Floor Is The Ramp's Smallest Step"), `overused-font` Inter (the design's one face),
  `gray-on-color` `#8592ab` on `#1b2945` (the rail's dim ink, shell). Proposed ignores for the coordinator are in
  the wave report.

## Questions

Settled by DESIGN.md, the brief and critica-respuestas.md; none open.

Questions skipped: 0 priority issues
