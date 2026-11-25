---
target: "notifications (wave O2, 2.8): frontend/src/modules/notification/NotificationAdminPage.tsx"
total_score: 29
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 1
target_identity: "file:frontend/src/modules/notification/NotificationAdminPage.tsx"
target_fingerprint: "sha256:4150a8b58e36c1afbcec3a8a50ae5efb99a32ba515cb143bf579ee8b2d681915"
target_path: frontend/src/modules/notification/NotificationAdminPage.tsx
timestamp: 2026-11-25T04-26-25Z
slug: es-notification-notificationadminpage-tsx-548264a7
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: notifications (`frontend/src/modules/notification/NotificationAdminPage.tsx`, `/notifications`)

Redesign plan, Task 7 (wave O2), "Cierre de cada oleada", step 3. Assessment A was written from the source and the
harmony captures (/notifications: 1440x900, 390x844, 390x844-client, 641x900-client; es and en) before the detector
output; Assessment B followed. A and B in series, one context.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Dead-letter tile loud, switches with their state |
| 2 | Match System / Real World | 3 | Rules as sentences |
| 3 | User Control and Freedom | 3 | Switches undoable before Save |
| 4 | Consistency and Standards | 2 | Two identical switches saved two ways (fixed) |
| 5 | Error Prevention | 3 | Allowlist warning in place |
| 6 | Recognition Rather Than Recall | 3 | Rule reads event → channel → people |
| 7 | Flexibility and Efficiency | 3 | Add rule linkable |
| 8 | Aesthetic and Minimalist Design | 3 | Four sections and the log |
| 9 | Error Recovery | 3 | Refusals in each panel's sentence |
| 10 | Help and Documentation | 3 | Section sentences |
| | **Total** | **29/40** | |

## Anti-patterns verdict

Not slop.

## What works

- Rules, channels, queue members and the allowlist as sections; Add rule from `?new=rule`.
- Every switch on the page is one draft with one save bar.

## Priority issues

- **P1** A rule's switch waited for the save bar while a channel's identical switch wrote at once. Fixed in polish: both are one draft (`switches.tsx`, b6b298d).
- **P3** Delivery rows carry 125-character reasons at 1440 (recorded data).

## Assessment B (detector)

- Static: `impeccable detect --json frontend/src/modules/notification` from the worktree root: **0 findings**.
- Browser (`npx vite` + `impeccable live-server`, Chromium on `/notifications?__fixtures`, es and en, 1440 and 390): only
  system-wide findings on this route, none caused by the page: `undersized-ui-text` on 10.5px captions (DESIGN.md's
  Caption step, "The Floor Is The Ramp's Smallest Step"), `overused-font` Inter (the design's one face),
  `gray-on-color` `#8592ab` on `#1b2945` (the rail's dim ink, shell). Proposed ignores for the coordinator are in
  the wave report.

## Questions

Settled by DESIGN.md, the brief and critica-respuestas.md; none open.

Questions skipped: 1 priority issues
