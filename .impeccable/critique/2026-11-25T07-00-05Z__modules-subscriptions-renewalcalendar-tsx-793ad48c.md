---
target: "subscription renewals, Task 10: frontend/src/modules/subscriptions/RenewalCalendar.tsx"
total_score: 29
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/subscriptions/RenewalCalendar.tsx"
target_fingerprint: "sha256:58939318ac07bc65faa8bc8b5ff4b802ce743cce65628ea660dd0c5e305ee509"
target_path: frontend/src/modules/subscriptions/RenewalCalendar.tsx
timestamp: 2026-11-25T07-00-05Z
slug: modules-subscriptions-renewalcalendar-tsx-793ad48c
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: subscription renewals (`frontend/src/modules/subscriptions/RenewalCalendar.tsx`, `/subscriptions/renewals`)

Redesign plan, Task 10 (wave O5), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (1440x900 and 390x844, English and Spanish) before any detector output; Assessment B followed (the CLI detector from the worktree root, then `detect.js` injected in Chromium).

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | In window and action required counted at the toolbar's end |
| 2 | Match System / Real World | 3 | Notice deadline and renewal as recorded days |
| 3 | User Control and Freedom | 3 | The window in ?within=; Back undoes it |
| 4 | Consistency and Standards | 3 | The list kit's chips and empty state |
| 5 | Error Prevention | 3 | Only the server's windows can be chosen |
| 6 | Recognition Rather Than Recall | 3 | Days to renewal under each service |
| 7 | Flexibility and Efficiency | 3 | Each window is a link |
| 8 | Aesthetic and Minimalist Design | 3 | One list |
| 9 | Error Recovery | 3 | 'Nothing renews in the next 30 days.' |
| 10 | Help and Documentation | 2 | The page sentence only |
| **Total** | | **29/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** The calendar that catches a closing notice period, which is the money this module saves. The window as chips makes '60 days' a link a colleague can open.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/subscriptions` from the worktree root: exit 0, `[]`. Harmony (es) on `/subscriptions/renewals` at four viewports: no stray token, no overflow, no fixture miss, no serious axe finding.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh context per page on this worktree's own `npx vite` on 5291 in its own process group with `?__fixtures`, title marked `[Human]`; `impeccable live-server --background` on 8400, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). /subscriptions/renewals en 1440: 6 findings (`gray-on-color` x1, `overused-font` x1, `undersized-ui-text` x4). `undersized-ui-text` is the 10.5px table caption step (DESIGN.md's Caption, the PRODUCT.md floor), `overused-font` is Inter and `gray-on-color` the rail's tenant card: false positives already answered in the shell's critique. Any `nested-cards`, `straggler` or `cramped-padding` on `ol.steps` is the shared readiness panel (`components/prerequisites`), outside this wave's files.

## Overall Impression

A small, correct page on the kit.

## What's Working

- ?within= falls back to 30 for anything else, so no request can leave the server's range
- The empty list says what was looked at

## Priority Issues

None at P0 or P1.
## Persona Red Flags

- Technician planning a quarter: Next 90 days is one click

## Minor Observations

- No count per window (it would be three more reads): the chips carry none, by the Absent Zero rule

## Questions

Questions skipped: 0 priority issues
