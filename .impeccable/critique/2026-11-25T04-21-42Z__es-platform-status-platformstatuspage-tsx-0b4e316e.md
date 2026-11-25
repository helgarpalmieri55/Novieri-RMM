---
target: frontend/src/modules/platform-status/PlatformStatusPage.tsx
total_score: 33
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/platform-status/PlatformStatusPage.tsx"
target_fingerprint: "sha256:f75eb3341a4d19f0c2653eccf35aaa6f8429d12705d5ad9e50c9d06dd2445f55"
target_path: frontend/src/modules/platform-status/PlatformStatusPage.tsx
timestamp: 2026-11-25T04-21-42Z
slug: es-platform-status-platformstatuspage-tsx-0b4e316e
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Platform status (`frontend/src/modules/platform-status/PlatformStatusPage.tsx`)

Assessment A (design review) was run first from the source and the harmony captures of /platform-status (en/es, 1440x900, 390x844); Assessment B (detector + browser) after it.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 4 | Verdict in the head, read-at time, banner naming failing probes |
| 2 | Match System / Real World | 3 | Probe names in words |
| 3 | User Control and Freedom | 3 | Re-probe |
| 4 | Consistency and Standards | 4 | Pills with glyphs; tables named |
| 5 | Error Prevention | 3 | n/a |
| 6 | Recognition Rather Than Recall | 3 | Tiles open their sections |
| 7 | Flexibility and Efficiency | 3 | Re-probe |
| 8 | Aesthetic and Minimalist Design | 3 | Three tables |
| 9 | Error Recovery | 4 | Guidance per reason code |
| 10 | Help and Documentation | 3 | Registration is not health: said |
| **Total** | | **33/40** | **Good** |

## Design Specificity Verdict

**LLM assessment**: specific: partial vs full outage is the page's stated purpose and the tiles carry it.

**Deterministic scan**: `impeccable detect --json frontend/src/modules/platform-status` (from the worktree root) → 0
findings. Browser overlay (live-server + detect.js over `npx vite`, 1440x900, `/platform-status`): only the system-level
findings every console page carries — `undersized-ui-text` on the 10.5px column headers and tile labels
(DESIGN.md's label step), `gray-on-color` on the rail's dim text (shell), `overused-font` Inter (the brand face).
No finding in this page's own markup.

## Overall Impression
Already a good status page; this wave named its tables.

## What's Working
- A pending probe is never a passing one.
- Each state is a word with a glyph.

## Priority Issues
- **[P2] One tile per dependency** (brief) not done: the KPI tiles stay; open question.
- **[P3] Detail column** reads small.

## Persona Red Flags
**On-call engineer**: sees the failing probe named in the banner; no flag.

## Minor Observations
- None.

## Questions to Consider
Questions skipped: 2 priority issues
