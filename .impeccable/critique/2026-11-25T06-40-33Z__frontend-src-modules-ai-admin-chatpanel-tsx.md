---
target: "The Assistant, wave 7 (7.4): frontend/src/modules/ai-admin/ChatPanel.tsx"
total_score: 30
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/ai-admin/ChatPanel.tsx"
target_fingerprint: "sha256:b0dfa0018550048c0d3ef345e766fc68690dd62c801d027fbb9918ea410b2b56"
target_path: frontend/src/modules/ai-admin/ChatPanel.tsx
timestamp: 2026-11-25T06-40-33Z
slug: frontend-src-modules-ai-admin-chatpanel-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: The Assistant (`frontend/src/modules/ai-admin/ChatPanel.tsx`, `/assistant`)

Redesign plan, Task 12 (wave O7), «Cierre de cada oleada», step 3. Assessment A was written from the page source and the harmony captures (es and en, 1440x900 and 390x844) before any detector output; Assessment B followed.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Checking, off, on, asking: each says so |
| 2 | Match System / Real World | 3 | The server's sentence, capitalised and closed |
| 3 | User Control and Freedom | 3 | Settings in the head; recent sessions beside |
| 4 | Consistency and Standards | 3 | Head action like every other page |
| 5 | Error Prevention | 3 | Off: no composer to type into |
| 6 | Recognition Rather Than Recall | 3 | Examples under the invitation |
| 7 | Flexibility and Efficiency | 3 | `?q=` hands over a question from search |
| 8 | Aesthetic and Minimalist Design | 3 | One panel, one composer |
| 9 | Error Recovery | 3 | Failed turn keeps the question |
| 10 | Help and Documentation | 3 | Off: who turns it on |
| **Total** | | **30/40** | **Good** |

## Design Specificity Verdict

**LLM assessment**: Authored: the off state says whose problem it is (whoever runs the server) instead of an apology.

**Deterministic scan** (Assessment B, run from the worktree root so `.impeccable/config.json` applies):
`impeccable detect --json frontend/src/modules/{settings,profile,ai-admin,ai-automation}` → 0 findings in all four
modules. Browser layer: `npx vite` (own port) + `impeccable live-server --background`, Chromium through Playwright,
a fresh context per page on `<route>?__fixtures`, `detect.js` injected, console read; es and en × 1440x900 and
390x844 × the wave's 15 routes; stopped with `live-server stop --keep-inject` and the dev server killed (`pgrep`
empty). System-wide findings only, not the page's own: `gray-on-color #8592ab on #1b2945` (the rail's muted ink,
brand tokens, DESIGN.md), `overused-font inter` (the brand's one face), `undersized-ui-text 10.5px` on table
headers (the kit's `Th` caption rung). No user-visible overlay is claimed: the overlay ran in a headless tab.


## What's Working

- An off assistant is an empty state with its cause and whom to ask, for everyone.
- Settings are a secondary action in the head for ai.admin, in both states.

## Priority Issues

- **[P3] Stragglers at 390 in the description and the examples**
  - Why it matters: «dispositivo.», «permisos.» alone on last lines.
  - Fix: Kit: `text-wrap: pretty` on notes.
  - Suggested command: /impeccable polish

## Persona Red Flags

**Jordan (first-timer)**: the invitation carries two example questions.

## Minor Observations

None.

Questions skipped: 1 priority issues
