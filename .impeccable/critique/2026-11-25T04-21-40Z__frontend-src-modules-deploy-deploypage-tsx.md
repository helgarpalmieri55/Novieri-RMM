---
target: frontend/src/modules/deploy/DeployPage.tsx
total_score: 31
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:/home/user/rmm-w3/frontend/src/modules/deploy/DeployPage.tsx"
target_fingerprint: "sha256:5105330aea5cf0bd3a0cab4774063d936f852a2c7456e462d7e2bca045ad156e"
target_path: /home/user/rmm-w3/frontend/src/modules/deploy/DeployPage.tsx
timestamp: 2026-11-25T04-21-40Z
slug: frontend-src-modules-deploy-deploypage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Software deployment (`frontend/src/modules/deploy/DeployPage.tsx`)

Assessment A (design review) was run first from the source and the harmony captures of /deploy (en/es, 1440x900, 390x844); Assessment B (detector + browser) after it.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Tiles say what still runs and what finished |
| 2 | Match System / Real World | 3 | Install / Update / Uninstall in words; targets by name |
| 3 | User Control and Freedom | 3 | Chips undo; ?new=deployment closes with Back |
| 4 | Consistency and Standards | 4 | SectionNav, toolbar, chips, ListEmpty from the kit |
| 5 | Error Prevention | 4 | Execute asks first and names package, target and window |
| 6 | Recognition Rather Than Recall | 3 | Names resolved for packages, devices, groups and people |
| 7 | Flexibility and Efficiency | 3 | Tiles open filtered views |
| 8 | Aesthetic and Minimalist Design | 3 | One panel of rows under four tiles |
| 9 | Error Recovery | 3 | Execute failures in a sentence |
| 10 | Help and Documentation | 2 | "Deployed in the last 7 days" is a view, not a state, among state chips |
| **Total** | | **31/40** | **Good** |

## Design Specificity Verdict

**LLM assessment**: specific: signed-job execution with a confirmation that names what runs where, and the readiness panel about installers.

**Deterministic scan**: `impeccable detect --json frontend/src/modules/deploy` (from the worktree root) → 0
findings. Browser overlay (live-server + detect.js over `npx vite`, 1440x900, `/deploy`, `/deploy/packages`, `/deploy/scripts`): only the system-level
findings every console page carries — `undersized-ui-text` on the 10.5px column headers and tile labels
(DESIGN.md's label step), `gray-on-color` on the rail's dim text (shell), `overused-font` Inter (the brand face).
No finding in this page's own markup.

## Overall Impression
A clean list page on the kit. The readiness panel is tall when a step is open and pushes the list down.

## What's Working
- The empty list offers New deployment, and filtered says so.
- Under the Client chip the deployments are that client's.
- Execute keeps its confirmation (invariant 1 untouched).

## Priority Issues
- **[P2] Two primaries on an empty list** (head + empty state). Fix: the empty state's action is secondary. *(Applied in polish.)*
- **[P2] The week view sits among the state chips**; it is a time window. Suggested command: /impeccable clarify.
- **[P3] Readiness panel** (shared component) is three numbered rows tall even when two are done.

## Persona Red Flags
**Technician**: finds a failed run in one click (tile); no flag.

## Minor Observations
- Execute buttons are identical per row; the confirmation names the row, which is the guard.

## Questions to Consider
- Should the week view be its own chip group ("Any time / This week")?
