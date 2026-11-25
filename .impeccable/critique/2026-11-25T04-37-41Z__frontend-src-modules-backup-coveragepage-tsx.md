---
target: "wave O4, Backup coverage: frontend/src/modules/backup/CoveragePage.tsx"
total_score: 30
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:/home/user/rmm-w4/frontend/src/modules/backup/CoveragePage.tsx"
target_fingerprint: "sha256:e69a9e95726ee469729f55d7e12b60a276f5d00eb6381d98d28bf26021bb96d2"
target_path: /home/user/rmm-w4/frontend/src/modules/backup/CoveragePage.tsx
timestamp: 2026-11-25T04-37-41Z
slug: frontend-src-modules-backup-coveragepage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Backup coverage (`frontend/src/modules/backup/CoveragePage.tsx`, `/backup/coverage`)

Redesign plan, Task 9 (wave O4), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (both languages, 1440x900 and 390x844) before any detector output; Assessment B followed. No sub-agent tool is exposed in this session, so A and B ran in series in one context.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Counts from the report's totals; "Showing 11 of 11" |
| 2 | Match System / Real World | 3 | Verdicts in words, "Never" for a missing date |
| 3 | User Control and Freedom | 3 | `?coverage=` kept, Clear filters, Client chip |
| 4 | Consistency and Standards | 3 | Receta L; the chips are the tiles' own links |
| 5 | Error Prevention | 3 | "Not crash-consistent" said on the row |
| 6 | Recognition Rather Than Recall | 3 | Device and its repository on one cell |
| 7 | Flexibility and Efficiency | 3 | Five chips over a whole report |
| 8 | Aesthetic and Minimalist Design | 3 | Tiles and chips say the same four numbers; chip labels singular, tile labels plural |
| 9 | Error Recovery | 3 | Filtered empty with its way out |
| 10 | Help and Documentation | 3 | The head sentence says what a verdict means |
| **Total** | | **30/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** The report rendered as a list without losing what makes it trustworthy: the totals are the server's, over every device, and "All" is their sum, which the server guarantees is the device count.

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
On this page: nothing beyond those three. (At en 1440 the injection returned no message at all on `/backup/coverage`, `/bcp`, `/bcp/tests/test-1/report` and `/provisioning`, where the same captions exist: a timing miss of the injection, not a clean page; the es 390 run on the same routes returned the three.)

## Overall Impression

Solid; the counts are the report's, not the page's. The tiles and chips duplicate each other.

## What's Working

- Chip counts are the report's `totals`, never a count of the visible rows.
- The Client chip narrows the report itself, so tiles, chips and rows all speak for one client.

## Priority Issues

- **[P3] Chip labels in the singular beside plural tiles.** "Protected 5" as a chip, "PROTECTED 5" as a tile in Spanish plural ("Protegidos"): the same filter worded twice. **Fix:** None now: the chips reuse the verdict words of the pills on the rows, which is the more useful match. **Command:** /impeccable clarify

## Persona Red Flags

**Alex.** "Failing 2" is one click from the two rows. No red flag.

**Casey.** At 390 the five chips wrap to three lines above the table.

## Minor Observations

- None.

## Questions to Consider

Questions skipped: 1 priority issues
