---
target: "subscription spend, Task 10: frontend/src/modules/subscriptions/SpendDashboard.tsx"
total_score: 28
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 1
target_identity: "file:frontend/src/modules/subscriptions/SpendDashboard.tsx"
target_fingerprint: "sha256:701a237400ce155c3631e3780ddf8d86dc5502631b34751b486973566f1182ec"
target_path: frontend/src/modules/subscriptions/SpendDashboard.tsx
timestamp: 2026-11-25T07-00-05Z
slug: modules-subscriptions-spenddashboard-tsx-707afcb5
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: subscription spend (`frontend/src/modules/subscriptions/SpendDashboard.tsx`, `/subscriptions/spend`)

Redesign plan, Task 10 (wave O5), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (1440x900 and 390x844, English and Spanish) before any detector output; Assessment B followed (the CLI detector from the worktree root, then `detect.js` injected in Chromium).

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Each roll-up loads and fails on its own |
| 2 | Match System / Real World | 3 | Amounts in their own currency, never summed |
| 3 | User Control and Freedom | 3 | The Client chip in the URL |
| 4 | Consistency and Standards | 3 | Section nav, tiles, the kit's tables |
| 5 | Error Prevention | 3 | Never adds two currencies or two clients |
| 6 | Recognition Rather Than Recall | 3 | Each total names its client and currency |
| 7 | Flexibility and Efficiency | 3 | Each total opens the subscriptions behind it |
| 8 | Aesthetic and Minimalist Design | 2 | The tiles and the 'By currency' table drew the same figures |
| 9 | Error Recovery | 3 | Empty and failed states are sentences |
| 10 | Help and Documentation | 2 | One note on never summing |
| **Total** | | **28/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** Spend per client and currency, with the rule this module exists to keep (no sum across currencies or clients) stated under the totals. Specific to an MSP's books.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/subscriptions` from the worktree root: exit 0, `[]`. Harmony (es) on `/subscriptions/spend` at four viewports: no stray token, no overflow, no fixture miss, no serious axe finding.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh context per page on this worktree's own `npx vite` on 5291 in its own process group with `?__fixtures`, title marked `[Human]`; `impeccable live-server --background` on 8400, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). /subscriptions/spend en 1440: 30 findings (`gray-on-color` x1, `overused-font` x1, `undersized-ui-text` x28); /subscriptions/spend es 390: 22 findings (`overused-font` x1, `undersized-ui-text` x21). `undersized-ui-text` is the 10.5px table caption step (DESIGN.md's Caption, the PRODUCT.md floor), `overused-font` is Inter and `gray-on-color` the rail's tenant card: false positives already answered in the shell's critique. Any `nested-cards`, `straggler` or `cramped-padding` on `ol.steps` is the shared readiness panel (`components/prerequisites`), outside this wave's files.

## Overall Impression

The brief's 'each total a StatCard' landed, but beside the 'By currency' table it was the same numbers twice.

## What's Working

- Each total is a tile in its own currency with its annual figure, opening its client's subscriptions
- The Client chip narrows both reports (the registry's run-rate tile links here with client=)

## Priority Issues

### [P1] The totals were drawn twice
- **What**: The tile row and the 'By currency' table held the same rows and figures
- **Why it matters**: Two renderings of one fact make a reader compare them for a difference that is not there
- **Fix**: Keep the tiles (they carry client, currency, monthly and annual) and drop the table, with the 'never summed' sentence under the tiles (fixed in the polish)

## Persona Red Flags

- MSP with forty clients: forty tiles; each still names its client and opens its list

## Minor Observations

- 'Not resolved' rows group vendors and owners the organisation could not name

## Questions

Questions skipped: 1 priority issues
