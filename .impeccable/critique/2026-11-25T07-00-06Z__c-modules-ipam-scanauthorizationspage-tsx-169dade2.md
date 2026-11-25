---
target: "scan authorizations, Task 10: frontend/src/modules/ipam/ScanAuthorizationsPage.tsx"
total_score: 30
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/ipam/ScanAuthorizationsPage.tsx"
target_fingerprint: "sha256:d801dfb96970feea4ec36ebcfc87676a3b877d38e59ce436dc95fa4b69df0af7"
target_path: frontend/src/modules/ipam/ScanAuthorizationsPage.tsx
timestamp: 2026-11-25T07-00-06Z
slug: c-modules-ipam-scanauthorizationspage-tsx-169dade2
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: scan authorizations (`frontend/src/modules/ipam/ScanAuthorizationsPage.tsx`, `/ipam/scan-authorizations`)

Redesign plan, Task 10 (wave O5), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (1440x900 and 390x844, English and Spanish) before any detector output; Assessment B followed (the CLI detector from the worktree root, then `detect.js` injected in Chromium).

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | State, reach and expiry per grant; Showing x of y |
| 2 | Match System / Real World | 3 | Reach in addresses, not masks |
| 3 | User Control and Freedom | 3 | Revoked in the URL; Grant is a link; revoke asks |
| 4 | Consistency and Standards | 3 | The list kit |
| 5 | Error Prevention | 3 | The preview sizes a grant before it is made |
| 6 | Recognition Rather Than Recall | 3 | Covered subnets per active grant |
| 7 | Flexibility and Efficiency | 3 | Fill the prefix from a registered subnet |
| 8 | Aesthetic and Minimalist Design | 3 | The shared meaning once above the rows |
| 9 | Error Recovery | 3 | Empty says nothing will be swept |
| 10 | Help and Documentation | 3 | Deny by default stated where it acts |
| **Total** | | **30/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** The control surface for where the platform may put packets on a client's wire, built around 'what does this permit?'. Invariant 2a's caution is on the screen.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/ipam` from the worktree root: exit 0, `[]`. Harmony (es) on `/ipam/scan-authorizations` at four viewports: no stray token, no overflow, no fixture miss, no serious axe finding.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh context per page on this worktree's own `npx vite` on 5291 in its own process group with `?__fixtures`, title marked `[Human]`; `impeccable live-server --background` on 8400, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). /ipam/scan-authorizations en 1440: 9 findings (`gray-on-color` x1, `line-length` x1, `overused-font` x1, `undersized-ui-text` x6). `undersized-ui-text` is the 10.5px table caption step (DESIGN.md's Caption, the PRODUCT.md floor), `overused-font` is Inter and `gray-on-color` the rail's tenant card: false positives already answered in the shell's critique. Any `nested-cards`, `straggler` or `cramped-padding` on `ol.steps` is the shared readiness panel (`components/prerequisites`), outside this wave's files.

## Overall Impression

On the kit without losing any of its safety text. A disabled Authorize now says why.

## What's Working

- Grant from ?new=grant, guarded while a prefix, note or expiry is typed
- Revoked and expired grants are a chip and a link

## Priority Issues

None at P0 or P1.
## Persona Red Flags

- Auditor: revoked grants stay listable, so 'who authorised that segment in March' is answerable

## Minor Observations

- The two state chips read 'In force' and 'Revoked and expired too'

## Questions

- The coordinator's message names /ipam/scan-authorizations among the pages whose ?client= is the Client chip's; the brief says no chip there (invariant 2a(d)). The page keeps no chip. Confirm? (Sent to the coordinator in the report.)
