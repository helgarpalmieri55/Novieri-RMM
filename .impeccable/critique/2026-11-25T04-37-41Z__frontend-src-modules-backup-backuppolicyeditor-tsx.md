---
target: "wave O4, Backup policies: frontend/src/modules/backup/BackupPolicyEditor.tsx"
total_score: 29
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/backup/BackupPolicyEditor.tsx"
target_fingerprint: "sha256:c0275afe0cc317b7ef5e5fed12f6501b126d2a395599445e7588414daf9656c9"
target_path: frontend/src/modules/backup/BackupPolicyEditor.tsx
timestamp: 2026-11-25T04-37-41Z
slug: frontend-src-modules-backup-backuppolicyeditor-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Backup policies (`frontend/src/modules/backup/BackupPolicyEditor.tsx`, `/backup/policies`)

Redesign plan, Task 9 (wave O4), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (both languages, 1440x900 and 390x844) before any detector output; Assessment B followed. No sub-agent tool is exposed in this session, so A and B ran in series in one context.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Pressed chip, count, the kit's states |
| 2 | Match System / Real World | 2 | A policy on an archived group is named by its id ("grp-pilot") |
| 3 | User Control and Freedom | 3 | Editor in `?new=policy` / `?edit=`, asks before dropping a change |
| 4 | Consistency and Standards | 3 | Receta L for the list; the editor stays a dialog (Receta FO, 7) |
| 5 | Error Prevention | 3 | Only the client's active repositories are offered |
| 6 | Recognition Rather Than Recall | 3 | The window in words, not crontab |
| 7 | Flexibility and Efficiency | 3 | Chips with counts; edit by link |
| 8 | Aesthetic and Minimalist Design | 3 | Quiet list |
| 9 | Error Recovery | 3 | Save names the missing repository |
| 10 | Help and Documentation | 3 | Readiness panel says what a policy needs |
| **Total** | | **29/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** The list is the kit's; what is this product's is the painted window printed back in words and the per-client repository rule. The dialog now behaves like a form: it is a link, and it does not throw a change away without asking.

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

One real defect: a policy whose group was archived shows the group's id.

## What's Working

- The window column prints the painter's sentence, and an expression it cannot draw stays an expression.
- Cancel with a change asks; Save that worked closes without asking.

## Priority Issues

- **[P2] An archived group is named by its id.** The table names groups from the picker's options, which leave archived groups out, so `grp-pilot` is printed where "Pilot ring (2025)" belongs. **Fix:** Name the scope from every group the read returned; the picker keeps excluding archived ones. **Command:** /impeccable harden

## Persona Red Flags

**Jordan (first-timer).** "grp-pilot" reads as a code, not a group.

**Sam.** Edit buttons are per row with their row's text. No red flag.

## Minor Observations

- None.

## Questions to Consider

Questions skipped: 1 priority issues
