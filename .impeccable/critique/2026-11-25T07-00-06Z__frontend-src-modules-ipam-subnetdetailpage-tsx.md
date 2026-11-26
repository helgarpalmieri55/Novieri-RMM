---
target: "one subnet, Task 10: frontend/src/modules/ipam/SubnetDetailPage.tsx"
total_score: 28
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 2
target_identity: "file:frontend/src/modules/ipam/SubnetDetailPage.tsx"
target_fingerprint: "sha256:21011e9c3d109ce2c8120423ab3718d738b93dc691f2165473cf700b07bbd704"
target_path: frontend/src/modules/ipam/SubnetDetailPage.tsx
timestamp: 2026-11-25T07-00-06Z
slug: frontend-src-modules-ipam-subnetdetailpage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: one subnet (`frontend/src/modules/ipam/SubnetDetailPage.tsx`, `/ipam/subnets/subnet-1`)

Redesign plan, Task 10 (wave O5), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (1440x900 and 390x844, English and Spanish) before any detector output; Assessment B followed (the CLI detector from the worktree root, then `detect.js` injected in Chromium).

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Facts, utilization bar, the sweep's last run |
| 2 | Match System / Real World | 3 | Prefix, kind, VRF, VLAN as recorded |
| 3 | User Control and Freedom | 3 | Back to the tree; the tab in the URL |
| 4 | Consistency and Standards | 2 | axe link-in-text-block on the head's client link |
| 5 | Error Prevention | 3 | Sweep is the same signed job; delete counts what it holds |
| 6 | Recognition Rather Than Recall | 3 | Client linked to its 360 in the head |
| 7 | Flexibility and Efficiency | 3 | ?tab=sweeps reloads on Sweeps |
| 8 | Aesthetic and Minimalist Design | 2 | The gateway was in the head's context and again in the facts |
| 9 | Error Recovery | 3 | Errors as sentences, a 422 on utilization explained |
| 10 | Help and Documentation | 3 | Each tab's panel says what it is |
| **Total** | | **28/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** A subnet as the network team reads it: its facts, how full it is, and the three things done to it (ranges, sweeps, drift). The sweep panel's authorization preview is this product's security model in the UI.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/ipam` from the worktree root: exit 0, `[]`. Harmony (es) on `/ipam/subnets/subnet-1` at four viewports: no stray token, no overflow, no fixture miss, and one serious axe finding, `link-in-text-block` on the head's client link (see the priority issues).

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh context per page on this worktree's own `npx vite` on 5291 in its own process group with `?__fixtures`, title marked `[Human]`; `impeccable live-server --background` on 8400, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). /ipam/subnets/subnet-1 en 1440: 20 findings (`gray-on-color` x1, `overused-font` x1, `undersized-ui-text` x18). `undersized-ui-text` is the 10.5px table caption step (DESIGN.md's Caption, the PRODUCT.md floor), `overused-font` is Inter and `gray-on-color` the rail's tenant card: false positives already answered in the shell's critique. Any `nested-cards`, `straggler` or `cramped-padding` on `ol.steps` is the shared readiness panel (`components/prerequisites`), outside this wave's files.

## Overall Impression

The one page whose tabs lived in useState now keeps them in the URL. The head says what the record is; the gateway was said twice.

## What's Working

- useUrlTab: ?tab=sweeps reloads on Sweeps, an unknown tab opens Addresses
- useRecordTitle: '10.20.9.0/24 · IP addresses · Novieri RMM'
- The head is drawn on the error and loading states too, so the way back is always there

## Priority Issues

### [P1] The client link in the head failed axe (link-in-text-block)
- **What**: The context put ' · Lisbon · Workstations · VRF global · …' as text beside the client link, so the link was told apart by colour alone (axe, serious, all four viewports)
- **Why it matters**: WCAG 1.4.1: a link inside running text needs more than colour; harmony fails the route
- **Fix**: Keep the client link in a block of its own inside the context (the record kit's other heads draw the link alone) (fixed in the polish)

### [P1] The gateway was said twice
- **What**: 'gateway 10.20.9.1' closed the head's context and 'Gateway 10.20.9.1' sat in the facts below
- **Why it matters**: The coordinator's answer on the client 360 (point 5): what the head says is not repeated
- **Fix**: Drop the gateway from the head's context; the facts keep it (fixed in the polish)

## Persona Red Flags

- Technician: the address grid is the default tab, the sweep one tab away and linkable

## Minor Observations

- DNS servers wrap inside their fact cell at 1440

## Questions

Questions skipped: 2 priority issues
