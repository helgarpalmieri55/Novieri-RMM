---
target: "drift, Task 10: frontend/src/modules/ipam/DriftPage.tsx"
total_score: 29
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/ipam/DriftPage.tsx"
target_fingerprint: "sha256:9f38ed45ece6b8414e857ab06a58f468f596319fed2efa6336b9e52ff24eb787"
target_path: frontend/src/modules/ipam/DriftPage.tsx
timestamp: 2026-11-25T07-00-06Z
slug: frontend-src-modules-ipam-driftpage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: drift (`frontend/src/modules/ipam/DriftPage.tsx`, `/ipam/drift`)

Redesign plan, Task 10 (wave O5), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (1440x900 and 390x844, English and Spanish) before any detector output; Assessment B followed (the CLI detector from the worktree root, then `detect.js` injected in Chromium).

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Counts n/a until the three lists answered |
| 2 | Match System / Real World | 3 | Each class named with its remedy |
| 3 | User Control and Freedom | 3 | Class, subnet and finding in the URL |
| 4 | Consistency and Standards | 3 | The list kit's chips, ChipSelect and empty state |
| 5 | Error Prevention | 3 | Suppress says why it waits |
| 6 | Recognition Rather Than Recall | 3 | Evidence as facts, not JSON |
| 7 | Flexibility and Efficiency | 3 | ?finding= opens a finding; each view a link |
| 8 | Aesthetic and Minimalist Design | 2 | Nine class chips, a subnet and a client chip over three rows |
| 9 | Error Recovery | 3 | No drift says its cause |
| 10 | Help and Documentation | 3 | The class notice spells out meaning and remedy |
| **Total** | | **29/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** The reconciliation list: drift apart from dormant and orphans, each class with its own remedy, the evidence as facts. Built around this module's distinction between 'wrong' and 'not verifiable', which no generic list would draw.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/ipam` from the worktree root: exit 0, `[]`. Harmony (es) on `/ipam/drift` at four viewports: no stray token, no overflow, no fixture miss, no serious axe finding.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh context per page on this worktree's own `npx vite` on 5291 in its own process group with `?__fixtures`, title marked `[Human]`; `impeccable live-server --background` on 8400, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). /ipam/drift en 1440: 12 findings (`gray-on-color` x1, `overused-font` x1, `undersized-ui-text` x10); /ipam/drift es 390: 6 findings (`overused-font` x1, `undersized-ui-text` x5). `undersized-ui-text` is the 10.5px table caption step (DESIGN.md's Caption, the PRODUCT.md floor), `overused-font` is Inter and `gray-on-color` the rail's tenant card: false positives already answered in the shell's critique. Any `nested-cards`, `straggler` or `cramped-padding` on `ol.steps` is the shared readiness panel (`components/prerequisites`), outside this wave's files.

## Overall Impression

The class strip and the open finding are links now, and a disabled Suppress says why. The table is wider than the panel at 1440: the Actions column needs the scroll hint.

## What's Working

- The handle panel is per finding, names its address and asks before a typed note is lost
- Orphans live in the same list panel with their register link unchanged (/ipam?register=1)
- Under the Client chip the three counts and the lists are that client's

## Priority Issues

### [P2] The Handle button is past the panel's edge at 1440
- **What**: Seven columns with the evidence facts push Actions into the table's horizontal scroll
- **Why it matters**: The row's one action should be visible where the row is read
- **Fix**: Move the confidence line under the class pill, or let Actions stick to the right edge (a kit table option)

## Persona Red Flags

- Network engineer: the evidence column holds the answer to 'which MAC', 'which claimant'

## Minor Observations

- The chip row wraps to two lines before the subnet and client chips

## Questions

- The brief says no Client chip on drift; ruling 2 requires the tree's narrowed 'With drift' tile to open the findings it counted with client=. The page reads ?client= and draws the chip. Keep it, or count the tree's drift tile un-narrowed? (Sent to the coordinator in the report.)
