---
target: "one subscription, Task 10: frontend/src/modules/subscriptions/SubscriptionDetail.tsx"
total_score: 29
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/subscriptions/SubscriptionDetail.tsx"
target_fingerprint: "sha256:e16dee3218aec41db5b3c66a273442f4aff44d255504d59ed89c49cfb97144f3"
target_path: frontend/src/modules/subscriptions/SubscriptionDetail.tsx
timestamp: 2026-11-25T07-00-05Z
slug: ules-subscriptions-subscriptiondetail-tsx-a08bf254
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: one subscription (`frontend/src/modules/subscriptions/SubscriptionDetail.tsx`, `/subscriptions/sub-1`)

Redesign plan, Task 10 (wave O5), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (1440x900 and 390x844, English and Spanish) before any detector output; Assessment B followed (the CLI detector from the worktree root, then `detect.js` injected in Chromium).

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | The save bar appears with a change; 'Renewal terms saved.' after the 2xx |
| 2 | Match System / Real World | 3 | Billed monthly, renews automatically, held seats |
| 3 | User Control and Freedom | 3 | Discard; leaving with a change asks |
| 4 | Consistency and Standards | 3 | Receta F head and tiles, Receta FO for the terms |
| 5 | Error Prevention | 3 | Cancel and Record renewal confirm; the notice rule is named on save |
| 6 | Recognition Rather Than Recall | 3 | Client linked to its 360 in the head |
| 7 | Flexibility and Efficiency | 3 | Enter in a field saves |
| 8 | Aesthetic and Minimalist Design | 2 | Details still lists the notice days and auto-renew the form below edits |
| 9 | Error Recovery | 3 | A refused save is a sentence in the bar |
| 10 | Help and Documentation | 3 | The terms note explains the notice deadline |
| **Total** | | **29/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** A subscription as a contract: what it costs per cycle, its seats against the held ones, and the renewal terms that decide whether a notice deadline exists. The terms note explaining when a deadline is 'action required' is this product's own.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/subscriptions` from the worktree root: exit 0, `[]`. Harmony (es) on `/subscriptions/sub-1` at four viewports: no stray token, no overflow, no fixture miss, no serious axe finding.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh context per page on this worktree's own `npx vite` on 5291 in its own process group with `?__fixtures`, title marked `[Human]`; `impeccable live-server --background` on 8400, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). /subscriptions/sub-1 en 1440: 10 findings (`gray-on-color` x1, `line-length` x1, `overused-font` x1, `undersized-ui-text` x7). `undersized-ui-text` is the 10.5px table caption step (DESIGN.md's Caption, the PRODUCT.md floor), `overused-font` is Inter and `gray-on-color` the rail's tenant card: false positives already answered in the shell's critique. Any `nested-cards`, `straggler` or `cramped-padding` on `ol.steps` is the shared readiness panel (`components/prerequisites`), outside this wave's files.

## Overall Impression

The renewal terms moved onto the console's one save bar without losing their rules. One overlap remains: Details repeats two of the four terms the form edits right below it.

## What's Working

- The cadence is a chip in the head's context, never a second status pill (M14)
- Tiles: cost per cycle, seats with how many are held, the renewal date with whether it renews automatically
- useRecordTitle: 'Figma Organization · Subscriptions · Novieri RMM'

## Priority Issues

### [P2] Details repeats two of the terms
- **What**: 'Notice days' and 'Auto-renew' are in Details and in the Renewal terms form below
- **Why it matters**: Two places for one fact; the form is the one that changes it
- **Fix**: Drop the two rows from Details when the form is shown (a manager); keep them for a reader

## Persona Red Flags

- Reader without subscriptions.manage: Details is the only place the terms appear, so the rows must stay for them

## Minor Observations

- The cadence chip has a control's border and reads a little like a button

## Questions

Questions skipped: 0 priority issues
