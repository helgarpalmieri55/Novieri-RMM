---
target: "one license, Task 10: frontend/src/modules/licenses/LicenseDetail.tsx"
total_score: 28
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 1
target_identity: "file:frontend/src/modules/licenses/LicenseDetail.tsx"
target_fingerprint: "sha256:71cd997eb6a55c162997e54d504ffc9e0e1fefa81debcd4b343bc87540f77975"
target_path: frontend/src/modules/licenses/LicenseDetail.tsx
timestamp: 2026-11-25T07-00-05Z
slug: frontend-src-modules-licenses-licensedetail-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: one license (`frontend/src/modules/licenses/LicenseDetail.tsx`, `/licenses/lic-1`)

Redesign plan, Task 10 (wave O5), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (1440x900 and 390x844, English and Spanish) before any detector output; Assessment B followed (the CLI detector from the worktree root, then `detect.js` injected in Chromium).

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Seats counted from the allocations; n/a until they are read |
| 2 | Match System / Real World | 3 | Entitled, in use, free; dates as recorded |
| 3 | User Control and Freedom | 3 | Back to the register; delete asks first; the key is shown once |
| 4 | Consistency and Standards | 3 | Receta F head, tiles and panels like the other records |
| 5 | Error Prevention | 3 | Delete confirms naming the product and its seats |
| 6 | Recognition Rather Than Recall | 3 | Client linked to its 360, type in the head |
| 7 | Flexibility and Efficiency | 2 | No edit of the record on this page (none today) |
| 8 | Aesthetic and Minimalist Design | 3 | Details no longer repeats the entitlement the first tile says |
| 9 | Error Recovery | 3 | A failed reveal or delete is said in a sentence |
| 10 | Help and Documentation | 2 | No help beyond the labels |
| **Total** | | **28/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** The license as a record: what it entitles, how many seats are held and free, and who holds them. The reveal flow keeps the key out of the page until asked and shows it once, which is this product's security model on screen rather than a generic detail page.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/licenses` from the worktree root: exit 0, `[]`. Harmony (es) on `/licenses/lic-1` at four viewports: no stray token, no overflow, no fixture miss, no serious axe finding.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh context per page on this worktree's own `npx vite` on 5291 in its own process group with `?__fixtures`, title marked `[Human]`; `impeccable live-server --background` on 8400, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). /licenses/lic-1 en 1440: 9 findings (`gray-on-color` x1, `overused-font` x1, `undersized-ui-text` x7). `undersized-ui-text` is the 10.5px table caption step (DESIGN.md's Caption, the PRODUCT.md floor), `overused-font` is Inter and `gray-on-color` the rail's tenant card: false positives already answered in the shell's critique. Any `nested-cards`, `straggler` or `cramped-padding` on `ol.steps` is the shared readiness panel (`components/prerequisites`), outside this wave's files.

## Overall Impression

A clean record. The polish removed the one repeat (the entitlement in Details beside its tile).

## What's Working

- DetailHead with the client linked to its 360 and the type as the head's state, out of the action row (M14)
- Three seat tiles, the free one in crit with how many are over when allocations exceed the entitlement
- useRecordTitle: the tab reads 'Adobe Acrobat Pro · Licenses · Novieri RMM'

## Priority Issues

### [P1] Details repeated the entitlement
- **What**: 'Entitlements 50' sat in Details under the 'Entitled 50' tile
- **Why it matters**: The coordinator's answer on the client 360 (point 5): Details keeps what is nowhere else on the page
- **Fix**: Removed the row (fixed in the polish)

### [P2] The type wears the minus glyph in the head
- **What**: 'Per user' is drawn with the muted 'not applicable' mark
- **Why it matters**: Same as the register's type column
- **Fix**: Give license types a neutral kind in licenseState.ts

## Persona Red Flags

- Technician: a license with no allocations shows 0 in use and all seats free, which is a measurement (the allocations answered)

## Minor Observations

- The allocation table's 'Released' column is mostly empty dashes for held seats; correct but quiet

## Questions

Questions skipped: 1 priority issues
