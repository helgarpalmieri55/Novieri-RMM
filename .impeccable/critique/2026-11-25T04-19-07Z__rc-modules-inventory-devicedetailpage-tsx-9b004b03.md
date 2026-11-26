---
target: "the device record, wave O1: frontend/src/modules/inventory/DeviceDetailPage.tsx"
total_score: 29
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/inventory/DeviceDetailPage.tsx"
target_fingerprint: "sha256:6e81b2e0b547a70cdec44f2bea55f453c7f1abf760e25408faa484e69c60d842"
target_path: frontend/src/modules/inventory/DeviceDetailPage.tsx
timestamp: 2026-11-25T04-19-07Z
slug: rc-modules-inventory-devicedetailpage-tsx-9b004b03
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: the device record (`frontend/src/modules/inventory/DeviceDetailPage.tsx`, `/devices/dev-1`)

Redesign plan, Task 6 (wave O1), «Cierre de cada oleada» step 3. Assessment A was written from the source and the harmony captures (1440x900 and 390x844, English and Spanish) before any detector output; Assessment B followed.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Status pill in the head; alert and patch counts each lead to their tab |
| 2 | Match System / Real World | 3 | Checks named in words, values as reported |
| 3 | User Control and Freedom | 3 | Back link, tabs in the URL, Back undoes a tab |
| 4 | Consistency and Standards | 3 | Receta F: DetailHead, two summary cards, UrlTabs, DetailGrid |
| 5 | Error Prevention | 3 | Install updates and Restart confirm; Remote in first |
| 6 | Recognition Rather Than Recall | 3 | Information, relations and activity on one screen |
| 7 | Flexibility and Efficiency | 3 | Every number links to its tab; a tab is a link |
| 8 | Aesthetic and Minimalist Design | 2 | Seven actions wrap under the title; the aside runs ~700 px past the main column |
| 9 | Error Recovery | 3 | Kit loading/error per panel |
| 10 | Help and Documentation | 3 | Each panel's link says where the rest is |
| **Total** | | **29/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** Close to the «atera 8» capture in organisation (alert and patching status on top, info left, metrics and activity right) and specific to this product: the certificate and the last sweep sit in the information, and «Checks» shows each check's latest reading rather than a 24-hour chart nobody serves (decision 2026-11-24).

**Deterministic scan.** `impeccable detect --json frontend/src/modules/inventory` from the worktree root: exit 0, `[]` (the five wave modules all scan clean). Harmony (`--strict-fixtures`, en and es, 1440x900, 390x844, 390x844-client, 641x900-client) on the wave's 13 routes: `ok`, 0 strays, no overflow, no serious axe violation, no fixture miss.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh context per case on this worktree's own `npx vite` in its own process group, `?__fixtures`; `impeccable live-server --background` on 8400, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). `/devices/dev-1` en 1440: 0 findings on the detailed pass; es 390: 1 (`overused-font`, the shell's). `?tab=hardware` and `?tab=health` (first pass): 1 summary finding each. `undersized-ui-text` on table headers and tile captions at 10.5px is a false positive (DESIGN.md's Caption step is 10.5px and PRODUCT.md sets the floor there, as the Task 5 critique recorded); `overused-font` (Inter) and `gray-on-color` on the rail's tenant card are the shell's, already answered in its critique.

## Overall Impression

Reads as Atera's device page in Novieri's voice. The cost is height on the right: checks, activity, backup and tickets stack to twice the left column.

## What's Working

- Every count on the two cards is a link to the tab holding the rest, and «N open alerts» keeps its link to the alert list.
- No chart is drawn: latest readings only, as the operator decided.
- The tab title names the machine: «lis-ws-041 · Devices · Novieri RMM».

## Priority Issues

- **[P2] Seven actions wrap into a row of their own under the title at 1440 and three rows at 390.**
  - **Why it matters**: Atera groups them as Connect + Manage; here each is a button.
  - **Fix**: A menu is a kit piece that does not exist; left as is (Remote in stays first and primary).
  - **Suggested command**: /impeccable distill
- **[P2] The aside (checks, activity, backup, tickets) runs ~700 px below the main column, which ends after Relations.**
  - **Why it matters**: A blank band on the left at 1440.
  - **Fix**: The brief places those four panels right; left as is.
  - **Suggested command**: /impeccable layout

## Persona Red Flags

**Technician during an incident**: alerts and missing updates are the first thing under the name, each one click from its tab. **Auditor**: the certificate, origin and last sweep are in one list with their instants.

## Minor Observations

- «Agent · Agent last seen 2 minutes ago» repeats the term; the tests pin the sentence.
- «Backup details» wraps beside its title in the narrow aside.

## Questions to Consider

Questions skipped: 2 priority issues
