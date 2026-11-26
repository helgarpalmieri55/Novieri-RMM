---
target: "the product catalog, Task 10: frontend/src/modules/licenses/ProductsPage.tsx"
total_score: 29
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 1
target_identity: "file:frontend/src/modules/licenses/ProductsPage.tsx"
target_fingerprint: "sha256:ecb103af2b2777c1cc8532dd3735d6e9e5cb889125d2accfb5f5ea34823938a7"
target_path: frontend/src/modules/licenses/ProductsPage.tsx
timestamp: 2026-11-25T07-00-05Z
slug: frontend-src-modules-licenses-productspage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: the product catalog (`frontend/src/modules/licenses/ProductsPage.tsx`, `/licenses/products`)

Redesign plan, Task 10 (wave O5), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (1440x900 and 390x844, English and Spanish) before any detector output; Assessment B followed (the CLI detector from the worktree root, then `detect.js` injected in Chromium).

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Showing x of y; installs per product |
| 2 | Match System / Real World | 3 | Rule kinds in words, patterns verbatim |
| 3 | User Control and Freedom | 3 | Search in the URL; New and Edit are links |
| 4 | Consistency and Standards | 3 | The list kit; the editor in its dialog |
| 5 | Error Prevention | 3 | Edit now opens on the product's own rules |
| 6 | Recognition Rather Than Recall | 3 | Publisher and rule count under each name |
| 7 | Flexibility and Efficiency | 3 | Search over name and publisher |
| 8 | Aesthetic and Minimalist Design | 3 | One list |
| 9 | Error Recovery | 3 | A refused save is a sentence |
| 10 | Help and Documentation | 2 | The page sentence only |
| **Total** | | **29/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** The catalog that turns installed names into licensable products. Its match rules, shown one per line with the pattern verbatim, are the product's own mechanism, not a generic catalog.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/licenses` from the worktree root: exit 0, `[]`. Harmony (es) on `/licenses/products` at four viewports: no stray token, no overflow, no fixture miss, no serious axe finding.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh context per page on this worktree's own `npx vite` on 5291 in its own process group with `?__fixtures`, title marked `[Human]`; `impeccable live-server --background` on 8400, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). /licenses/products en 1440: 6 findings (`gray-on-color` x1, `overused-font` x1, `undersized-ui-text` x4). `undersized-ui-text` is the 10.5px table caption step (DESIGN.md's Caption, the PRODUCT.md floor), `overused-font` is Inter and `gray-on-color` the rail's tenant card: false positives already answered in the shell's critique. Any `nested-cards`, `straggler` or `cramped-padding` on `ol.steps` is the shared readiness panel (`components/prerequisites`), outside this wave's files.

## Overall Impression

The ?edit=<id> work fixed a real defect: the editor used to open empty for an existing product (its state was initialised once, from nothing), so Save sent a blank name. It now mounts per product.

## What's Working

- New product from ?new=product and Edit from ?edit=<id>, each asking before a change is lost
- Mounted per product, so the editor starts from what it edits

## Priority Issues

### [P1] A rule-less product claimed '0 installs'
- **What**: The CAL with no match rule showed '0 installations' in Detected
- **Why it matters**: A product nothing can detect has no usage data; 0 says 'none installed' (The Absent Zero Rule)
- **Fix**: Show 'no usage data' when a product has no match rule (fixed in the polish, with a test)

## Persona Red Flags

- Technician adding a product from the review queue: the queue's Map dialog links here when the catalog is empty

## Minor Observations

- '58 installations' is set in mono including the word; only the number is data

## Questions

Questions skipped: 1 priority issues
