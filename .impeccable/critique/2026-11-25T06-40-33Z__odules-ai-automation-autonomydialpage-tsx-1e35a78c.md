---
target: "AI automation, wave 7 (7.5): frontend/src/modules/ai-automation/AutonomyDialPage.tsx"
total_score: 31
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/ai-automation/AutonomyDialPage.tsx"
target_fingerprint: "sha256:580eceffaee13be287fcfc7962981aaa6347706cd32ce55206dc7466df6b3d65"
target_path: frontend/src/modules/ai-automation/AutonomyDialPage.tsx
timestamp: 2026-11-25T06-40-33Z
slug: odules-ai-automation-autonomydialpage-tsx-1e35a78c
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: AI automation (`frontend/src/modules/ai-automation/AutonomyDialPage.tsx`, `/assistant/autonomy`)

Redesign plan, Task 12 (wave O7), «Cierre de cada oleada», step 3. Assessment A was written from the page source and the harmony captures (es and en, 1440x900 and 390x844) before any detector output; Assessment B followed.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Unsaved cells named, «Saved.» after the server |
| 2 | Match System / Real World | 3 | Levels in words, tokens under them |
| 3 | User Control and Freedom | 3 | Discard; leaving asks; cancel keeps the draft |
| 4 | Consistency and Standards | 3 | The console's one bar |
| 5 | Error Prevention | 4 | Raising confirms, named per cell, destructive |
| 6 | Recognition Rather Than Recall | 3 | A legend on the page, not only in the dialog |
| 7 | Flexibility and Efficiency | 3 | Chips, every position visible |
| 8 | Aesthetic and Minimalist Design | 3 | Grid, legend, waiting actions |
| 9 | Error Recovery | 3 | A refused cell is named, the rest stay unsaved |
| 10 | Help and Documentation | 3 | What each level permits |
| **Total** | | **31/40** | **Good** |

## Design Specificity Verdict

**LLM assessment**: Authored: a policy grid whose every change is spelled out in words before it is confirmed.

**Deterministic scan** (Assessment B, run from the worktree root so `.impeccable/config.json` applies):
`impeccable detect --json frontend/src/modules/{settings,profile,ai-admin,ai-automation}` → 0 findings in all four
modules. Browser layer: `npx vite` (own port) + `impeccable live-server --background`, Chromium through Playwright,
a fresh context per page on `<route>?__fixtures`, `detect.js` injected, console read; es and en × 1440x900 and
390x844 × the wave's 15 routes; stopped with `live-server stop --keep-inject` and the dev server killed (`pgrep`
empty). System-wide findings only, not the page's own: `gray-on-color #8592ab on #1b2945` (the rail's muted ink,
brand tokens, DESIGN.md), `overused-font inter` (the brand's one face), `undersized-ui-text 10.5px` on table
headers (the kit's `Th` caption rung). No user-visible overlay is claimed: the overlay ran in a headless tab.


## What's Working

- Save opens today's confirmation; raising reads as raising.
- Setting a cell back to what is stored is no change at all (no bar).

## Priority Issues

- **[P2] The floating Assistant button covers «All actions» at the panel's bottom-right on a short viewport**
  - Why it matters: Shell's fixed button over page content at 1440x900.
  - Fix: Shell: foot room is kept for the save bar only; not this page's.
  - Suggested command: /impeccable polish

- **[P3] A straggler «persona» at 390 es in the subtitle**
  - Why it matters: see above.
  - Fix: Kit: `text-wrap: pretty`.
  - Suggested command: /impeccable polish

## Persona Red Flags

**Auditor**: every change is confirmed and audited server-side; the page says the risk class comes from each approved action.

## Minor Observations

None.

Questions skipped: 2 priority issues
