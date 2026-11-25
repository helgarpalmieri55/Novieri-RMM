---
target: frontend/src/modules/deploy/PackagesPage.tsx
total_score: 31
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/deploy/PackagesPage.tsx"
target_fingerprint: "sha256:3e62f9ae79d1a8ec07c710371f7225293a13eed39295dbba3bfae806e6102c3b"
target_path: frontend/src/modules/deploy/PackagesPage.tsx
timestamp: 2026-11-25T04-21-40Z
slug: frontend-src-modules-deploy-packagespage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Packages (`frontend/src/modules/deploy/PackagesPage.tsx`)

Assessment A (design review) was run first from the source and the harmony captures of /deploy/packages (en/es, 1440x900, 390x844); Assessment B (detector + browser) after it.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Upload and delete failures named |
| 2 | Match System / Real World | 3 | Platform by glyph and name |
| 3 | User Control and Freedom | 3 | Search and System chip in the URL; Back undoes |
| 4 | Consistency and Standards | 4 | Kit toolbar, chips, ShowingCount, ListEmpty |
| 5 | Error Prevention | 4 | Delete asks and names name + version |
| 6 | Recognition Rather Than Recall | 3 | sha256 under the name |
| 7 | Flexibility and Efficiency | 3 | New package from ?new=package |
| 8 | Aesthetic and Minimalist Design | 3 | One panel |
| 9 | Error Recovery | 3 | Empty and filtered empty distinct |
| 10 | Help and Documentation | 2 | No hint of what a package needs before it can deploy |
| **Total** | | **31/40** | **Good** |

## Design Specificity Verdict

**LLM assessment**: specific enough: checksummed installers, a missing file said in the row.

**Deterministic scan**: `impeccable detect --json frontend/src/modules/deploy` (from the worktree root) → 0
findings. Browser overlay (live-server + detect.js over `npx vite`, 1440x900, `/deploy/packages`): only the system-level
findings every console page carries — `undersized-ui-text` on the 10.5px column headers and tile labels
(DESIGN.md's label step), `gray-on-color` on the rail's dim text (shell), `overused-font` Inter (the brand face).
No finding in this page's own markup.

## Overall Impression
Small and clear. The search and the System chip are what a 4-row catalogue needs; they will matter at 40.

## What's Working
- "No file uploaded" in the row, not a blank.
- Delete names the version, the row people get wrong.

## Priority Issues
- **[P2] Two primaries on an empty catalogue.** *(Applied in polish: the empty state's New package is secondary.)*
- **[P3] System chips carry no counts** (the route sends none); fine by the Absent Zero rule.
- **[P3] The row menu holds one item (Delete).**

## Persona Red Flags
**Technician**: finds a package by name; no flag.

## Minor Observations
- None.

## Questions to Consider
- Should the catalogue say, per package, whether it can be deployed yet (a file uploaded, a detection rule), rather than only "No file uploaded"?
