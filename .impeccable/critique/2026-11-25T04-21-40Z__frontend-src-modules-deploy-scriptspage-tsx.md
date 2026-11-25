---
target: frontend/src/modules/deploy/ScriptsPage.tsx
total_score: 30
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:/home/user/rmm-w3/frontend/src/modules/deploy/ScriptsPage.tsx"
target_fingerprint: "sha256:2697bb22f087cebb64434f0b7352ce3ded4b6598d9b77941019248968e4f3477"
target_path: /home/user/rmm-w3/frontend/src/modules/deploy/ScriptsPage.tsx
timestamp: 2026-11-25T04-21-40Z
slug: frontend-src-modules-deploy-scriptspage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Scripts (`frontend/src/modules/deploy/ScriptsPage.tsx`)

Assessment A (design review) was run first from the source and the harmony captures of /deploy/scripts (en/es, 1440x900, 390x844); Assessment B (detector + browser) after it.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Schedules panel states the last runs |
| 2 | Match System / Real World | 3 | Interpreter names spelt as products |
| 3 | User Control and Freedom | 3 | Chips in the URL |
| 4 | Consistency and Standards | 4 | Kit toolbar, chips, ShowingCount, ListEmpty |
| 5 | Error Prevention | 3 | Run keeps its signed-job confirmation |
| 6 | Recognition Rather Than Recall | 3 | Platform glyph + name |
| 7 | Flexibility and Efficiency | 3 | New script from ?new=script |
| 8 | Aesthetic and Minimalist Design | 2 | Seven interpreter chips for a catalogue that uses two |
| 9 | Error Recovery | 3 | Empty states say why |
| 10 | Help and Documentation | 3 | The page says scripts run with full rights |
| **Total** | | **30/40** | **Good** |

## Design Specificity Verdict

**LLM assessment**: specific: SYSTEM/root execution is stated in the head and the run dialog.

**Deterministic scan**: `impeccable detect --json frontend/src/modules/deploy` (from the worktree root) → 0
findings. Browser overlay (live-server + detect.js over `npx vite`, 1440x900, `/deploy/scripts`): only the system-level
findings every console page carries — `undersized-ui-text` on the 10.5px column headers and tile labels
(DESIGN.md's label step), `gray-on-color` on the rail's dim text (shell), `overused-font` Inter (the brand face).
No finding in this page's own markup.

## Overall Impression
The list is right; the interpreter chip row is long for the catalogue it filters.

## What's Working
- Running is unchanged (invariant 1).
- Search, System and Interpreter all link.

## Priority Issues
- **[P2] Interpreter chips for interpreters no script uses** lead to an empty list. Fix: show only interpreters present (and the one in the URL). Brief asks for one chip per value of `INTERPRETERS`; left as specified. Suggested command: /impeccable distill.
- **[P2] Two primaries on an empty list.** *(Applied in polish.)*
- **[P3] Schedules panel** sits under the list with its own table; fine.

## Persona Red Flags
**Technician**: finds a script by name; no flag.

## Minor Observations
- None.

## Questions to Consider
- Should the Interpreter chips list only the interpreters in use?
