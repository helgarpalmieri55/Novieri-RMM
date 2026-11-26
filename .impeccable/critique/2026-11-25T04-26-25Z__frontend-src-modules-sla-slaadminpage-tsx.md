---
target: "service levels (wave O2, 2.6): frontend/src/modules/sla/SlaAdminPage.tsx"
total_score: 29
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 1
target_identity: "file:frontend/src/modules/sla/SlaAdminPage.tsx"
target_fingerprint: "sha256:fb2b7c4f860d551f788245338e12304c1043a34991456003a764fef2aadc6e38"
target_path: frontend/src/modules/sla/SlaAdminPage.tsx
timestamp: 2026-11-25T04-26-25Z
slug: frontend-src-modules-sla-slaadminpage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: service levels (`frontend/src/modules/sla/SlaAdminPage.tsx`, `/sla`)

Redesign plan, Task 7 (wave O2), "Cierre de cada oleada", step 3. Assessment A was written from the source and the
harmony captures (/sla: 1440x900, 390x844, 390x844-client, 641x900-client; es and en) before the detector
output; Assessment B followed. A and B in series, one context.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Tiles measured or n/a, calendar health |
| 2 | Match System / Real World | 3 | Policies read as priority + queue |
| 3 | User Control and Freedom | 3 | Create dialogs in the URL |
| 4 | Consistency and Standards | 2 | Two saves remain beside the sections (settings, week editor) |
| 5 | Error Prevention | 3 | Targets validated in words |
| 6 | Recognition Rather Than Recall | 3 | Resolver preview |
| 7 | Flexibility and Efficiency | 3 | Client chip on the policies |
| 8 | Aesthetic and Minimalist Design | 3 | Two sections |
| 9 | Error Recovery | 3 | Refusals named |
| 10 | Help and Documentation | 3 | The rule is explained on screen (Atera's +) |
| | **Total** | **29/40** | |

## Anti-patterns verdict

Not slop. The calendar warning `.notice` inside the section reads as a card in a card to the detector (it is the kit's notice).

## What works

- Policies and calendars as two sections; Add policy and Add calendar open from the URL.
- The Client chip narrows the policies and the preview; a policy for every client still shows.

## Priority issues

- **P1** Two saves of their own remain on the page: "Guardar configuración" (auto-close days) and the week editor's "Guardar semana". The brief did not ask for them; folding them into one save bar is an open question for the coordinator (wave report, Q1).
- **P3** Stragglers at 390 in the page sentence and the calendars sentence.

## Assessment B (detector)

- Static: `impeccable detect --json frontend/src/modules/sla` from the worktree root: **0 findings**.
- Browser (`npx vite` + `impeccable live-server`, Chromium on `/sla?__fixtures`, es and en, 1440 and 390): only
  system-wide findings on this route, none caused by the page: `undersized-ui-text` on 10.5px captions (DESIGN.md's
  Caption step, "The Floor Is The Ramp's Smallest Step"), `overused-font` Inter (the design's one face),
  `gray-on-color` `#8592ab` on `#1b2945` (the rail's dim ink, shell), `nested-cards` on the calendar warning notice. Proposed ignores for the coordinator are in
  the wave report.

## Questions

Q1 (to the coordinator): fold the SLA settings' and the week editor's own saves into the page's one save bar, or leave them as the brief left them? Default taken: left as they are.

Questions skipped: 1 priority issues
