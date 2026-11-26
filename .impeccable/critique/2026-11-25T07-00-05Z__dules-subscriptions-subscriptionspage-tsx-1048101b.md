---
target: "the subscriptions registry, Task 10: frontend/src/modules/subscriptions/SubscriptionsPage.tsx"
total_score: 29
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/subscriptions/SubscriptionsPage.tsx"
target_fingerprint: "sha256:6f988e42e92b6c1dc720b28d24099f558949c67660355893666b916f91c2ee04"
target_path: frontend/src/modules/subscriptions/SubscriptionsPage.tsx
timestamp: 2026-11-25T07-00-05Z
slug: dules-subscriptions-subscriptionspage-tsx-1048101b
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: the subscriptions registry (`frontend/src/modules/subscriptions/SubscriptionsPage.tsx`, `/subscriptions`)

Redesign plan, Task 10 (wave O5), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (1440x900 and 390x844, English and Spanish) before any detector output; Assessment B followed (the CLI detector from the worktree root, then `detect.js` injected in Chromium).

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Tiles n/a until read; Showing x of y |
| 2 | Match System / Real World | 3 | Cycles, seats and renewal dates in words |
| 3 | User Control and Freedom | 3 | Search, cycle and Client chips in the URL |
| 4 | Consistency and Standards | 3 | Receta L; one section nav for the module's three pages |
| 5 | Error Prevention | 3 | New subscription names each refusal; guarded |
| 6 | Recognition Rather Than Recall | 3 | Owner as a person; seats and renewal under the service |
| 7 | Flexibility and Efficiency | 3 | Search over service, plan, category and cycle |
| 8 | Aesthetic and Minimalist Design | 3 | Readiness, tiles and list; the head keeps one action |
| 9 | Error Recovery | 3 | The kit's empty states |
| 10 | Help and Documentation | 2 | The readiness panel is the only help |
| **Total** | | **29/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** The registry of what an MSP's clients pay for, with the two dates that cost money if missed (renewal and notice) leading the tiles. The notice-deadline tile washed amber only when a deadline is closing is this product's own judgement, not a template.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/subscriptions` from the worktree root: exit 0, `[]`. Harmony (es) on `/subscriptions` at four viewports: no stray token, no overflow, no fixture miss, no serious axe finding.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh context per page on this worktree's own `npx vite` on 5291 in its own process group with `?__fixtures`, title marked `[Human]`; `impeccable live-server --background` on 8400, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). /subscriptions en 1440: 14 findings (`gray-on-color` x1, `nested-cards` x1, `overused-font` x1, `undersized-ui-text` x11); /subscriptions es 390: 11 findings (`nested-cards` x1, `overused-font` x1, `straggler` x1, `unbalanced-heading` x1, `undersized-ui-text` x7). `undersized-ui-text` is the 10.5px table caption step (DESIGN.md's Caption, the PRODUCT.md floor), `overused-font` is Inter and `gray-on-color` the rail's tenant card: false positives already answered in the shell's critique. Any `nested-cards`, `straggler` or `cramped-padding` on `ol.steps` is the shared readiness panel (`components/prerequisites`), outside this wave's files.

## Overall Impression

Consistent with the license register. Spend and Renewals moved from two head buttons into a section nav, which is where the module's pages belong.

## What's Working

- The cycle chips count the registry and appear only when there are two cycles
- Under the Client chip every tile counts and opens that client's rows
- New subscription mounted per opening and guarded with ['new']

## Priority Issues

None at P0 or P1.
## Persona Red Flags

- Technician at 390: the cycle chips wrap under the search; the table scrolls after the service

## Minor Observations

- The search placeholder clips beside the chips at 1440

## Questions

Questions skipped: 0 priority issues
