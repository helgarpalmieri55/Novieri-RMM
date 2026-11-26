---
target: "wave O4, Backup points: frontend/src/modules/backup/BackupPointsPage.tsx"
total_score: 30
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/backup/BackupPointsPage.tsx"
target_fingerprint: "sha256:73efe0395e6d4e488c8bb9c3f818b4c25f79fad57eec57651a944f7ada07d947"
target_path: frontend/src/modules/backup/BackupPointsPage.tsx
timestamp: 2026-11-25T04-37-41Z
slug: frontend-src-modules-backup-backuppointspage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Backup points (`frontend/src/modules/backup/BackupPointsPage.tsx`, `/backup`)

Redesign plan, Task 9 (wave O4), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (both languages, 1440x900 and 390x844) before any detector output; Assessment B followed. No sub-agent tool is exposed in this session, so A and B ran in series in one context.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | "Showing 9 of 9", pressed chips with their counts, the kit's loading and error |
| 2 | Match System / Real World | 3 | States and checks in words ("Data checked", "Restore tested"), sizes in GiB |
| 3 | User Control and Freedom | 3 | Every filter in the URL; Clear filters; Run backup from `?new=run`, Back closes it |
| 4 | Consistency and Standards | 3 | Receta L: head with one primary, section menu, bare panel, toolbar, count, `ListEmpty` |
| 5 | Error Prevention | 3 | Restore disabled with its reason on a failed or pruned point; restore asks first |
| 6 | Recognition Rather Than Recall | 3 | Device, repository and point id named on the row, never a UUID alone |
| 7 | Flexibility and Efficiency | 3 | Device and Client chips narrow a long list; a filtered view is a link |
| 8 | Aesthetic and Minimalist Design | 3 | At 1440 the Size column breaks "96.0 GiB" over two lines |
| 9 | Error Recovery | 3 | "Nothing matches these filters." with its way out; the run refusal names the field |
| 10 | Help and Documentation | 3 | The panel sentence says what proves a point |
| **Total** | | **30/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** The list kit applied to a record of backup points, with this module's own honesty kept: the tiles that never print a rate over zero runs, the two verification columns told apart, the row's warning edge on a point nobody verified. The Device chip is the one new thing: a fleet's points by computer, the filter a technician reaching for one machine's restore actually needs.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/backup` from the worktree root: exit 0,
`[]`. Harmony (`--strict-fixtures`, both languages, 1440x900, 390x844, 390x844-client, 641x900-client): 0 strays,
no overflow, the top bar in bounds, no serious axe violation, no fixture miss.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh context per route on this
worktree's own `npx vite` with `?__fixtures`, title marked `[Human]`; `impeccable live-server --background` on
8400, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is
visible to anyone). Run at en 1440x900 and es 390x844. On every route of the wave the same three come back
and are not this page's: `undersized-ui-text` on the 10.5px table headers, tile labels and fact captions
(DESIGN.md's Caption step and PRODUCT.md's floor: a false positive, as the client list's critique already
recorded), `gray-on-color` on the rail's tenant card and `overused-font` (Inter), both the shell's.
On this page: nothing beyond those three.

## Overall Impression

Correct and dense. One visual defect: the size figure breaks across two lines in the eight-column table at 1440.

## What's Working

- The chips are the vocabulary of the tile above them: "Needs verification 3" and "Awaiting verification 3" are the same rows, and the tile's link is the chip pressed.
- The Device chip offers only the computers that have points, so choosing one never empties the table by itself.
- Run backup opens from `?new=run` with the filtered computer already picked, and only for someone holding `backup.run`.

## Priority Issues

- **[P2] The Size column breaks its value.** At 1440 "96.0 GiB" and "412 GiB" wrap onto two lines; a size read in two pieces is a number that has to be reassembled. **Fix:** Keep the size on one line (`white-space: nowrap` on the mono figure, as the plans list does for its dates). **Command:** /impeccable polish
- **[P3] The tiles repeat two of the chips' counts.** "Needs verification 3" over "Awaiting verification 3": the same number twice in one screen. **Fix:** None now: the tiles are the page's summary and link to the chip; Receta L keeps a list's tiles. **Command:** /impeccable distill

## Persona Red Flags

**Alex (power user).** Device chip, then Restore on the row: two acts, and the URL keeps the view. No red flag.

**Sam (keyboard, screen reader).** Chips are toggles in a named group; the device and client selects are labelled; the table is named "Restore points". No red flag.

**Casey (phone).** The chips stack and the table scrolls sideways with its hint; Restore is in the last column, behind the scroll (as before).

## Minor Observations

- At 390 the four tiles take a screen before the list (the kit's `StatsGrid`).

## Questions to Consider

Questions skipped: 2 priority issues
