---
target: "Device groups, wave O1: frontend/src/modules/devices/GroupsPage.tsx"
total_score: 30
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/devices/GroupsPage.tsx"
target_fingerprint: "sha256:fa81c3a72aa1b974b9bfcfe90899e4ca5c851bb50b0b8e6c7b179f3aa7496f81"
target_path: frontend/src/modules/devices/GroupsPage.tsx
timestamp: 2026-11-25T04-19-07Z
slug: frontend-src-modules-devices-groupspage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Device groups (`frontend/src/modules/devices/GroupsPage.tsx`, `/device-groups`)

Redesign plan, Task 6 (wave O1), «Cierre de cada oleada» step 3. Assessment A was written from the source and the harmony captures (1440x900 and 390x844, English and Spanish) before any detector output; Assessment B followed.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Membership evaluation dated; ShowingCount |
| 2 | Match System / Real World | 3 | Precedence explained under the title |
| 3 | User Control and Freedom | 3 | Search, archived chip, Client chip, ?group=, ?edit= in the URL |
| 4 | Consistency and Standards | 3 | Receta L; DetailGrid for the open group |
| 5 | Error Prevention | 3 | Archive confirms; a 409 is a to-do list |
| 6 | Recognition Rather Than Recall | 3 | Rule or hand-picked per row |
| 7 | Flexibility and Efficiency | 3 | Open group and editor are links |
| 8 | Aesthetic and Minimalist Design | 3 | One list; members beside it |
| 9 | Error Recovery | 3 | Archive refusal names where to go |
| 10 | Help and Documentation | 3 | The evaluation sentence |
| **Total** | | **30/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** Its own: the org-default row, precedence and the dated membership sweep are this product's group model.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/devices` from the worktree root: exit 0, `[]` (the five wave modules all scan clean). Harmony (`--strict-fixtures`, en and es, 1440x900, 390x844, 390x844-client, 641x900-client) on the wave's 13 routes: `ok`, 0 strays, no overflow, no serious axe violation, no fixture miss.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh context per case on this worktree's own `npx vite` in its own process group, `?__fixtures`; `impeccable live-server --background` on 8400, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). `/device-groups` en 1440: 7 findings (`gray-on-color`, `undersized-ui-text` x5, `overused-font`); es 390: 4. `undersized-ui-text` on table headers and tile captions at 10.5px is a false positive (DESIGN.md's Caption step is 10.5px and PRODUCT.md sets the floor there, as the Task 5 critique recorded); `overused-font` (Inter) and `gray-on-color` on the rail's tenant card are the shell's, already answered in its critique.

## Overall Impression

The open group beside the list reads better than the dialog did; the editor is still the big dialog the brief keeps.

## What's Working

- Members of a group are a link (`?group=`) and stay on screen while the list is used.
- Opening and closing ?group= / ?edit= are Back-symmetric.

## Priority Issues

- **[P3] The evaluation sentence sits between the toolbar and the table.**
  - **Why it matters**: Minor: it dates the counts below it, so its place is defensible.
  - **Fix**: Left.
  - **Suggested command**: /impeccable layout

## Persona Red Flags

**Technician placing a machine**: opens the group, picks from its own client's devices, sees why one was not moved.

## Minor Observations

- The org-default row shows only without a search, so a search never lists a row that is not a group.

## Questions to Consider

Questions skipped: 1 priority issue
