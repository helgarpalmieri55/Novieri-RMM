---
target: "Assistant actions, wave 7 (7.6): frontend/src/modules/ai-automation/ActionsPage.tsx"
total_score: 30
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 1
target_identity: "file:frontend/src/modules/ai-automation/ActionsPage.tsx"
target_fingerprint: "sha256:adc4dabd518460ceae225a099c58162b8f578c78aeb44b28871fbfc5babb978d"
target_path: frontend/src/modules/ai-automation/ActionsPage.tsx
timestamp: 2026-11-25T06-40-33Z
slug: frontend-src-modules-ai-automation-actionspage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Assistant actions (`frontend/src/modules/ai-automation/ActionsPage.tsx`, `/assistant/actions` and `/assistant/actions/:actionId`)

Redesign plan, Task 12 (wave O7), «Cierre de cada oleada», step 3. Assessment A was written from the page source and the harmony captures (es and en, 1440x900 and 390x844) before any detector output; Assessment B followed.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Showing n of m; outcome pills |
| 2 | Match System / Real World | 3 | Outcome words, not wire states |
| 3 | User Control and Freedom | 3 | Chips in the URL; back from the record |
| 4 | Consistency and Standards | 3 | List and record on the kit |
| 5 | Error Prevention | 4 | Confirm and decline keep their confirmation; the list shows no rationale |
| 6 | Recognition Rather Than Recall | 3 | Destructive and tainted marked on the row |
| 7 | Flexibility and Efficiency | 3 | Filter by outcome |
| 8 | Aesthetic and Minimalist Design | 3 | One table; the record's two regions |
| 9 | Error Recovery | 3 | 409 offers «Show the current version» |
| 10 | Help and Documentation | 2 | No word on what «Ran» covers (confirmed or dispatched) |
| **Total** | | **30/40** | **Good** |

## Design Specificity Verdict

**LLM assessment**: Authored: the record keeps §6.3's separation (what runs, from the catalogue, beside why, from the model), and the list never shows the model's words.

**Deterministic scan** (Assessment B, run from the worktree root so `.impeccable/config.json` applies):
`impeccable detect --json frontend/src/modules/{settings,profile,ai-admin,ai-automation}` → 0 findings in all four
modules. Browser layer: `npx vite` (own port) + `impeccable live-server --background`, Chromium through Playwright,
a fresh context per page on `<route>?__fixtures`, `detect.js` injected, console read; es and en × 1440x900 and
390x844 × the wave's 15 routes; stopped with `live-server stop --keep-inject` and the dev server killed (`pgrep`
empty). System-wide findings only, not the page's own: `gray-on-color #8592ab on #1b2945` (the rail's muted ink,
brand tokens, DESIGN.md), `overused-font inter` (the brand's one face), `undersized-ui-text 10.5px` on table
headers (the kit's `Th` caption rung). No user-visible overlay is claimed: the overlay ran in a headless tab.


## What's Working

- The record head is the kit's: back to the list, the state, the device, the decision.
- The tab names the action by its catalogue name.

## Priority Issues

- **[P1] The Spanish «All» chip did not agree with the state words beside it**
  - Why it matters: «Todas» before «Ejecutado», «Rechazado», «Caducado» (masculine, from the pills).
  - Fix: «Todos» (de los estados), agreeing with «Estado».
  - Suggested command: /impeccable polish

- **[P2] Table header captions at 10.5px**
  - Why it matters: The detector's `undersized-ui-text` on Acción, Qué se ejecuta…
  - Fix: Kit (`Th`) caption rung; not this page's.
  - Suggested command: /impeccable polish

## Persona Red Flags

**Alex (power user)**: `?state=previewed` is a link to the queue that matters.

## Minor Observations

None.

Questions skipped: 2 priority issues
