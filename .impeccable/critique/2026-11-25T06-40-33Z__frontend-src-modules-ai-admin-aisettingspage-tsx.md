---
target: "Assistant settings, wave 7 (7.4): frontend/src/modules/ai-admin/AiSettingsPage.tsx"
total_score: 29
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/ai-admin/AiSettingsPage.tsx"
target_fingerprint: "sha256:b734cc3d13791bb89148b9c87e81f91c07e69d1b34219365078e83b85fb52cfd"
target_path: frontend/src/modules/ai-admin/AiSettingsPage.tsx
timestamp: 2026-11-25T06-40-33Z
slug: frontend-src-modules-ai-admin-aisettingspage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Assistant settings (`frontend/src/modules/ai-admin/AiSettingsPage.tsx`, `/assistant/settings`)

Redesign plan, Task 12 (wave O7), «Cierre de cada oleada», step 3. Assessment A was written from the page source and the harmony captures (es and en, 1440x900 and 390x844) before any detector output; Assessment B followed.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | States the provider, or that none is configured |
| 2 | Match System / Real World | 3 | What leaves / what never leaves, in words |
| 3 | User Control and Freedom | 3 | Read-only by design; points to the dials |
| 4 | Consistency and Standards | 3 | Same sections as the forms |
| 5 | Error Prevention | 3 | No control that could aim the assistant |
| 6 | Recognition Rather Than Recall | 3 | Both lists captioned |
| 7 | Flexibility and Efficiency | 3 | Sections fold |
| 8 | Aesthetic and Minimalist Design | 3 | Two sections side by side, the flagged list below |
| 9 | Error Recovery | 2 | A failed flag read says so; little else to recover |
| 10 | Help and Documentation | 3 | Server-admin disclosure behind a summary |
| **Total** | | **29/40** | **Good** |

## Design Specificity Verdict

**LLM assessment**: Authored: a disclosure page for an administrator, not a settings form; it says where the data goes and names the one place the dials live.

**Deterministic scan** (Assessment B, run from the worktree root so `.impeccable/config.json` applies):
`impeccable detect --json frontend/src/modules/{settings,profile,ai-admin,ai-automation}` → 0 findings in all four
modules. Browser layer: `npx vite` (own port) + `impeccable live-server --background`, Chromium through Playwright,
a fresh context per page on `<route>?__fixtures`, `detect.js` injected, console read; es and en × 1440x900 and
390x844 × the wave's 15 routes; stopped with `live-server stop --keep-inject` and the dev server killed (`pgrep`
empty). System-wide findings only, not the page's own: `gray-on-color #8592ab on #1b2945` (the rail's muted ink,
brand tokens, DESIGN.md), `overused-font inter` (the brand's one face), `undersized-ui-text 10.5px` on table
headers (the kit's `Th` caption rung). No user-visible overlay is claimed: the overlay ran in a headless tab.


## What's Working

- No invented controls: the dials stay on Settings → Assistant (rule 4).
- Flagged content is shown as data, with the rule that flagged it.

## Priority Issues

- **[P2] The two sections side by side end at different heights**
  - Why it matters: The provider section stops half way down the disclosure beside it.
  - Fix: Acceptable on a two-column read; left.
  - Suggested command: /impeccable polish

- **[P3] Stragglers at 390 («clientes.», «nada.»)**
  - Why it matters: Sentence ends alone on the last line.
  - Fix: Kit: `text-wrap: pretty` on notes.
  - Suggested command: /impeccable polish

## Persona Red Flags

**Auditor**: reads what leaves the network without needing permission to change anything.

## Minor Observations

None.

Questions skipped: 2 priority issues
