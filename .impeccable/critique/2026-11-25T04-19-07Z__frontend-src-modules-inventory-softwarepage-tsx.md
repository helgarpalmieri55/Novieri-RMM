---
target: "Software, wave O1: frontend/src/modules/inventory/SoftwarePage.tsx"
total_score: 30
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/inventory/SoftwarePage.tsx"
target_fingerprint: "sha256:ac2208c4661ee605a90c0a72661fefacf869b5e30c7f84fe1a1d81b73dfe8c13"
target_path: frontend/src/modules/inventory/SoftwarePage.tsx
timestamp: 2026-11-25T04-19-07Z
slug: frontend-src-modules-inventory-softwarepage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Software (`frontend/src/modules/inventory/SoftwarePage.tsx`, `/software`)

Redesign plan, Task 6 (wave O1), «Cierre de cada oleada» step 3. Assessment A was written from the source and the harmony captures (1440x900 and 390x844, English and Spanish) before any detector output; Assessment B followed.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | ShowingCount; kit loading/error |
| 2 | Match System / Real World | 3 | Publisher under each title |
| 3 | User Control and Freedom | 3 | Search in the URL; Close takes ?title= away |
| 4 | Consistency and Standards | 3 | Receta L (search only) |
| 5 | Error Prevention | 3 | Export says when it failed |
| 6 | Recognition Rather Than Recall | 3 | Title, version, installs on a row |
| 7 | Flexibility and Efficiency | 3 | A chosen title is a link |
| 8 | Aesthetic and Minimalist Design | 3 | One list, nothing else |
| 9 | Error Recovery | 3 | Nothing-yet vs nothing-matches |
| 10 | Help and Documentation | 3 | Sentence under the title |
| **Total** | | **30/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** A plain catalogue, which is what it is; the chosen title's computers under the list are the one interaction of its own.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/inventory` from the worktree root: exit 0, `[]` (the five wave modules all scan clean). Harmony (`--strict-fixtures`, en and es, 1440x900, 390x844, 390x844-client, 641x900-client) on the wave's 13 routes: `ok`, 0 strays, no overflow, no serious axe violation, no fixture miss.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh context per case on this worktree's own `npx vite` in its own process group, `?__fixtures`; `impeccable live-server --background` on 8400, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). `/software` en 1440: 5 findings (`gray-on-color`, `undersized-ui-text` x3, `overused-font`); es 390: 4 (`straggler` «inventario.» on the page sentence, 23% of the measure; `undersized-ui-text` x2; `overused-font`). `undersized-ui-text` on table headers and tile captions at 10.5px is a false positive (DESIGN.md's Caption step is 10.5px and PRODUCT.md sets the floor there, as the Task 5 critique recorded); `overused-font` (Inter) and `gray-on-color` on the rail's tenant card are the shell's, already answered in its critique.

## Overall Impression

Clean. The panel for a chosen title only exists once a title is chosen, so the page is one list.

## What's Working

- The search and the chosen title are both links.
- Close names which list it closes (a bare «Remove» could read as uninstalling).

## Priority Issues

- **[P3] At 390 the Spanish sentence leaves «inventario.» alone on its second line.**
  - **Why it matters**: A straggler on the page's first line of copy.
  - **Fix**: Left: the sentence is the module's, unchanged by this wave.
  - **Suggested command**: /impeccable clarify

## Persona Red Flags

**Technician hunting an old Chrome**: types, picks the version, gets the machines, sends the link.

## Minor Observations

- Two «Google Chrome» rows (two versions) open the same title's list, as before.

## Questions to Consider

Questions skipped: 1 priority issue
