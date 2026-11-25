---
target: frontend/src/modules/jobs/JobsPage.tsx
total_score: 32
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/jobs/JobsPage.tsx"
target_fingerprint: "sha256:804a4d8b39d71b01939d70725622ea15d8dcd656c8ce9360ba7c47d8f862ce70"
target_path: frontend/src/modules/jobs/JobsPage.tsx
timestamp: 2026-11-25T04-21-41Z
slug: frontend-src-modules-jobs-jobspage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Job history (`frontend/src/modules/jobs/JobsPage.tsx`)

Assessment A (design review) was run first from the source and the harmony captures of /jobs and a Playwright capture of /jobs?job=job-1 (1440x900); Assessment B (detector + browser) after it.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 4 | Stuck banner, tiles, six-hour timeline |
| 2 | Match System / Real World | 3 | Job types in words over their wire names |
| 3 | User Control and Freedom | 3 | Drawer closes with Back/Close; filters kept |
| 4 | Consistency and Standards | 4 | Kit chips, toolbar, ShowingCount, ListEmpty, Drawer |
| 5 | Error Prevention | 4 | Stop and retry confirmed and worded honestly |
| 6 | Recognition Rather Than Recall | 3 | The job open is a link |
| 7 | Flexibility and Efficiency | 3 | Tiles open filtered views |
| 8 | Aesthetic and Minimalist Design | 2 | Nine status chips wrap; timeline + tiles + list is long |
| 9 | Error Recovery | 3 | Detail failures announced |
| 10 | Help and Documentation | 3 | Cancel and retry sentences explain |
| **Total** | | **32/40** | **Good** |

## Design Specificity Verdict

**LLM assessment**: specific: withdraw vs ask-to-stop, retry as a new signed job.

**Deterministic scan**: `impeccable detect --json frontend/src/modules/jobs` (from the worktree root) → 0
findings. Browser overlay (live-server + detect.js over `npx vite`, 1440x900, `/jobs?job=job-1`): only the system-level
findings every console page carries — `undersized-ui-text` on the 10.5px column headers and tile labels
(DESIGN.md's label step), `gray-on-color` on the rail's dim text (shell), `overused-font` Inter (the brand face).
No finding in this page's own markup.

## Overall Impression
The drawer suits a job's detail better than the half-width pane did.

## What's Working
- The queue keeps full width.
- Under the chip the queue and the hostnames are that client's.

## Priority Issues
- **[P2] The drawer's title ("Job detail") and its h3 (the job type)** both head it; fine but repetitive. Suggested command: /impeccable distill.
- **[P2] Nine status chips** wrap at 390.

## Persona Red Flags
**Technician**: opens a failed job from the tile, retries; no flag.

## Minor Observations
- None.

## Questions to Consider
Questions skipped: 2 priority issues
