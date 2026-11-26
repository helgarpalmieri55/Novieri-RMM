---
target: "the review queue, Task 10: frontend/src/modules/licenses/ReviewQueue.tsx"
total_score: 28
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/licenses/ReviewQueue.tsx"
target_fingerprint: "sha256:0bb61d9b03f6d0f141611ae38188179ac23d3e5b6ce1898746df1d2072a3f6d9"
target_path: frontend/src/modules/licenses/ReviewQueue.tsx
timestamp: 2026-11-25T07-00-05Z
slug: frontend-src-modules-licenses-reviewqueue-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: the review queue (`frontend/src/modules/licenses/ReviewQueue.tsx`, `/licenses/review-queue`)

Redesign plan, Task 10 (wave O5), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (1440x900 and 390x844, English and Spanish) before any detector output; Assessment B followed (the CLI detector from the worktree root, then `detect.js` injected in Chromium).

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Showing x of y; an Ignore refused is announced |
| 2 | Match System / Real World | 3 | Title, publisher and device count as inventory reports them |
| 3 | User Control and Freedom | 3 | Ignore asks first and says there is no way back |
| 4 | Consistency and Standards | 3 | The list kit: toolbar count, ListEmpty |
| 5 | Error Prevention | 3 | Ignore with every client chosen names the empty Client field |
| 6 | Recognition Rather Than Recall | 3 | Publisher beside each title |
| 7 | Flexibility and Efficiency | 2 | No search over the titles |
| 8 | Aesthetic and Minimalist Design | 3 | One list and two row actions |
| 9 | Error Recovery | 3 | Map and Ignore failures are sentences |
| 10 | Help and Documentation | 2 | The page sentence only |
| **Total** | | **28/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** The queue is where installed software becomes licensable: each discovered title is mapped to a product or set aside for one client. The ignore's confirmation, naming the device count and the client, is specific to this product's MSP model.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/licenses` from the worktree root: exit 0, `[]`. Harmony (es) on `/licenses/review-queue` at four viewports: no stray token, no overflow, no fixture miss, no serious axe finding.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh context per page on this worktree's own `npx vite` on 5291 in its own process group with `?__fixtures`, title marked `[Human]`; `impeccable live-server --background` on 8400, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). /licenses/review-queue en 1440: 5 findings (`gray-on-color` x1, `overused-font` x1, `undersized-ui-text` x3). `undersized-ui-text` is the 10.5px table caption step (DESIGN.md's Caption, the PRODUCT.md floor), `overused-font` is Inter and `gray-on-color` the rail's tenant card: false positives already answered in the shell's critique. Any `nested-cards`, `straggler` or `cramped-padding` on `ol.steps` is the shared readiness panel (`components/prerequisites`), outside this wave's files.

## Overall Impression

Unchanged in what it does; it now draws the kit's count and empty state. The brief's BulkBar has nothing to hold: the queue has no bulk action today.

## What's Working

- Ignore stays one client's, with the Client field above the list when every client is chosen
- The empty queue says 'Nothing to review.'

## Priority Issues

None at P0 or P1.
## Persona Red Flags

- Technician with 200 titles: no search, the list is scrolled

## Minor Observations

- The toolbar holds only the count, a thin row under the Client field

## Questions

Questions skipped: 0 priority issues
