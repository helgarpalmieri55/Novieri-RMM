---
target: frontend/src/modules/remote/ScreenViewer.tsx
total_score: 31
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:/home/user/rmm-w3/frontend/src/modules/remote/ScreenViewer.tsx"
target_fingerprint: "sha256:a328725eb4ee7c108fd74298abcb5ee96ca63eb6c2f7119e6b302b6789ce7068"
target_path: /home/user/rmm-w3/frontend/src/modules/remote/ScreenViewer.tsx
timestamp: 2026-11-25T04-21-41Z
slug: frontend-src-modules-remote-screenviewer-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Remote session viewer (`frontend/src/modules/remote/ScreenViewer.tsx`)

Assessment A (design review) was run first from the source and the harmony captures of /remote/sess-1 (en/es, 1440x900, 390x844); Assessment B (detector + browser) after it.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Connection phase and session state both shown |
| 2 | Match System / Real World | 3 | The machine's name is the title |
| 3 | User Control and Freedom | 3 | End session asks; back link to the sessions |
| 4 | Consistency and Standards | 4 | DetailHead from the kit |
| 5 | Error Prevention | 4 | End is confirmed |
| 6 | Recognition Rather Than Recall | 3 | Facts row |
| 7 | Flexibility and Efficiency | 3 | Quality, monitor, refresh, full screen |
| 8 | Aesthetic and Minimalist Design | 3 | Canvas unchanged |
| 9 | Error Recovery | 3 | Stop failure in a sentence |
| 10 | Help and Documentation | 2 | No help on control vs view only |
| **Total** | | **31/40** | **Good** |

## Design Specificity Verdict

**LLM assessment**: specific: consent, control taken back, view-only, all stated.

**Deterministic scan**: `impeccable detect --json frontend/src/modules/remote` (from the worktree root) → 0
findings. Browser overlay (live-server + detect.js over `npx vite`, 1440x900, `/remote/sess-1`): only the system-level
findings every console page carries — `undersized-ui-text` on the 10.5px column headers and tile labels
(DESIGN.md's label step), `gray-on-color` on the rail's dim text (shell), `overused-font` Inter (the brand face).
No finding in this page's own markup.

## Overall Impression
The record head gives the viewer a way back and names the machine in the tab.

## What's Working
- The tab says the machine.
- End session keeps its confirmation.

## Priority Issues
- **[P2] Two states one line apart**: the head's "Active" (the server's session state) and the facts' "Waiting for the device…" (the stream). Fix: label them ("Session" / "Connection") or show one. Suggested command: /impeccable clarify.
- **[P3] The Device fact repeats the title.**

## Persona Red Flags
**Technician**: knows where they are and how to leave; no flag.

## Minor Observations
- None.

## Questions to Consider
Questions skipped: 2 priority issues
