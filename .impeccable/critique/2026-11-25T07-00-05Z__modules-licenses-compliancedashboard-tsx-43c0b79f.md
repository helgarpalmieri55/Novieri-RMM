---
target: "license compliance, Task 10: frontend/src/modules/licenses/ComplianceDashboard.tsx"
total_score: 29
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/licenses/ComplianceDashboard.tsx"
target_fingerprint: "sha256:38f3a313f8c13bf39bda5a33a7c10d0be822d931e09b4dcba8f3deecd2e1a0a2"
target_path: frontend/src/modules/licenses/ComplianceDashboard.tsx
timestamp: 2026-11-25T07-00-05Z
slug: modules-licenses-compliancedashboard-tsx-43c0b79f
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: license compliance (`frontend/src/modules/licenses/ComplianceDashboard.tsx`, `/licenses/compliance`)

Redesign plan, Task 10 (wave O5), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (1440x900 and 390x844, English and Spanish) before any detector output; Assessment B followed (the CLI detector from the worktree root, then `detect.js` injected in Chromium).

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Bars and pills per position; Showing x of y |
| 2 | Match System / Real World | 3 | Used / owned, over and free in words |
| 3 | User Control and Freedom | 3 | The Client chip in the URL; each figure opens its licenses |
| 4 | Consistency and Standards | 3 | The list kit's toolbar and empty state |
| 5 | Error Prevention | 3 | No usage data is never drawn as a compliant position |
| 6 | Recognition Rather Than Recall | 3 | Client per row with every client |
| 7 | Flexibility and Efficiency | 3 | Every figure is a link to the licenses of that product and client |
| 8 | Aesthetic and Minimalist Design | 3 | One table, the readiness above |
| 9 | Error Recovery | 3 | An empty list says why, filtered or not |
| 10 | Help and Documentation | 2 | The readiness panel is the only help |
| **Total** | | **29/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** Compliance as a technician needs it: per client and product, owned against installed, with the figure that explains the position one click away in the register. The 'no usage data' state is this module's own honesty about what an agentless fleet or a rule-less product cannot measure.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/licenses` from the worktree root: exit 0, `[]`. Harmony (es) on `/licenses/compliance` at four viewports: no stray token, no overflow, no fixture miss, no serious axe finding.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh context per page on this worktree's own `npx vite` on 5291 in its own process group with `?__fixtures`, title marked `[Human]`; `impeccable live-server --background` on 8400, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). /licenses/compliance en 1440: 11 findings (`gray-on-color` x1, `nested-cards` x1, `overused-font` x1, `straggler` x1, `undersized-ui-text` x7); /licenses/compliance es 390: 5 findings (`nested-cards` x1, `overused-font` x1, `undersized-ui-text` x3). `undersized-ui-text` is the 10.5px table caption step (DESIGN.md's Caption, the PRODUCT.md floor), `overused-font` is Inter and `gray-on-color` the rail's tenant card: false positives already answered in the shell's critique. Any `nested-cards`, `straggler` or `cramped-padding` on `ol.steps` is the shared readiness panel (`components/prerequisites`), outside this wave's files.

## Overall Impression

The brief asked for 'each figure a StatCard'; the page keeps its table (its panel of today, the same figures) and makes each figure the link, which scales to forty products where forty cards would not. That reading is a question for the coordinator.

## What's Working

- A rule-less product (the Windows Server CAL) says 'no usage data, 300 owned' and a mut 'No usage data' pill instead of '300 free'
- The figure links carry the row's client when every client is shown
- Under the Client chip the positions are that client's (the register's tiles link here with client=)

## Priority Issues

None at P0 or P1.
## Persona Red Flags

- Auditor: the position a breach is computed from is one click from the licenses that make it up

## Minor Observations

- The Difference column keeps the server's sign convention (installed minus entitled), which the pill beside it words

## Questions

- The brief's 'cada cifra un StatCard': is the figure-as-link in today's table the intended reading, or should the page add one StatCard per (client, product) position? (Sent to the coordinator in the report.)
