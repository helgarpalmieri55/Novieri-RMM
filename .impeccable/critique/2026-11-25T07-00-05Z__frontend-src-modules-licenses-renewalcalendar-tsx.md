---
target: "license renewals, Task 10: frontend/src/modules/licenses/RenewalCalendar.tsx"
total_score: 28
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/licenses/RenewalCalendar.tsx"
target_fingerprint: "sha256:a9cd619dad28ffa9bc850aa0c4a536c6fa10c254942ff9d9efc9354317d490e9"
target_path: frontend/src/modules/licenses/RenewalCalendar.tsx
timestamp: 2026-11-25T07-00-05Z
slug: frontend-src-modules-licenses-renewalcalendar-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: license renewals (`frontend/src/modules/licenses/RenewalCalendar.tsx`, `/licenses/renewals`)

Redesign plan, Task 10 (wave O5), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (1440x900 and 390x844, English and Spanish) before any detector output; Assessment B followed (the CLI detector from the worktree root, then `detect.js` injected in Chromium).

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Due-in pills, overdue in crit |
| 2 | Match System / Real World | 3 | Renewal or maintenance in words; months as headings |
| 3 | User Control and Freedom | 3 | Client chip in the URL |
| 4 | Consistency and Standards | 3 | The list kit's toolbar and empty state |
| 5 | Error Prevention | 3 | Dates pinned to UTC as recorded: no day moves |
| 6 | Recognition Rather Than Recall | 3 | Product and client per row |
| 7 | Flexibility and Efficiency | 2 | No window control (none today) |
| 8 | Aesthetic and Minimalist Design | 3 | Months group a quarter into blocks |
| 9 | Error Recovery | 3 | 'No license renews in this period.' |
| 10 | Help and Documentation | 2 | The page sentence only |
| **Total** | | **28/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** A renewal calendar grouped by month, which is how a technician plans a quarter of renewals with a client. Pinned dates keep the day the paperwork says.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/licenses` from the worktree root: exit 0, `[]`. Harmony (es) on `/licenses/renewals` at four viewports: no stray token, no overflow, no fixture miss, no serious axe finding.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh context per page on this worktree's own `npx vite` on 5291 in its own process group with `?__fixtures`, title marked `[Human]`; `impeccable live-server --background` on 8400, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). /licenses/renewals en 1440: 9 findings (`gray-on-color` x1, `overused-font` x1, `undersized-ui-text` x7). `undersized-ui-text` is the 10.5px table caption step (DESIGN.md's Caption, the PRODUCT.md floor), `overused-font` is Inter and `gray-on-color` the rail's tenant card: false positives already answered in the shell's critique. Any `nested-cards`, `straggler` or `cramped-padding` on `ol.steps` is the shared readiness panel (`components/prerequisites`), outside this wave's files.

## Overall Impression

A small, correct page. Grouping by month is the brief's change and reads well at 1440 and 390.

## What's Working

- One table with a row-group heading per month (scope=rowgroup)
- The empty sentence names its cause

## Priority Issues

None at P0 or P1.
## Persona Red Flags

- Technician: maintenance expiry and renewal on separate rows of the same month

## Minor Observations

- The due-in pill for anything beyond the warn window wears the muted minus glyph (dueKind)

## Questions

Questions skipped: 0 priority issues
