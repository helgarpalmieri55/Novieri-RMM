---
target: frontend/src/modules/patch/FleetPatchPage.tsx
total_score: 30
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 1
target_identity: "file:/home/user/rmm-w3/frontend/src/modules/patch/FleetPatchPage.tsx"
target_fingerprint: "sha256:69ff533323183de8408b1e9400de12de23581fce9c2b45134036855ee9a5ad9b"
target_path: /home/user/rmm-w3/frontend/src/modules/patch/FleetPatchPage.tsx
timestamp: 2026-11-25T04-21-40Z
slug: frontend-src-modules-patch-fleetpatchpage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Patching (`frontend/src/modules/patch/FleetPatchPage.tsx`)

Assessment A (design review) was run first from the source and the harmony captures of /patch (en/es, 1440x900, 390x844); Assessment B (detector + browser) after it.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 4 | Every tile, tab count and chip count is the server's |
| 2 | Match System / Real World | 3 | Severity words are the vendors'; the provenance line says so |
| 3 | User Control and Freedom | 3 | Chips undo; changing platform drops the old tab's filter and selection |
| 4 | Consistency and Standards | 3 | Toolbar, chips and UrlTabs from the kit; the decision bar is the module's own |
| 5 | Error Prevention | 3 | A severity chip on the app list could only empty it |
| 6 | Recognition Rather Than Recall | 3 | Platform tabs carry counts; the selection is stated in the bar |
| 7 | Flexibility and Efficiency | 3 | Select all N matching; tiles open filtered views |
| 8 | Aesthetic and Minimalist Design | 2 | Four stacked panels below the tiles; the range sentence wraps under the toolbar |
| 9 | Error Recovery | 3 | Empty tab names which of three causes |
| 10 | Help and Documentation | 3 | Severity provenance beside the filter |
| **Total** | | **30/40** | **Good** |

## Design Specificity Verdict

**LLM assessment**: specific: three platforms, three severity vocabularies, one decision model, and the page says which applies where. Nothing here is a generic patch dashboard.

**Deterministic scan**: `impeccable detect --json frontend/src/modules/patch` (from the worktree root) → 0
findings. Browser overlay (live-server + detect.js over `npx vite`, 1440x900, `/patch`): only the system-level
findings every console page carries — `undersized-ui-text` on the 10.5px column headers and tile labels
(DESIGN.md's label step), `gray-on-color` on the rail's dim text (shell), `overused-font` Inter (the brand face).
No finding in this page's own markup.

## Overall Impression
The list kit fits the update list well: search, one chip per severity with the server's count, the Client chip, the range at the end. The page is long, and two lists on a tab plus apps, approvals, restarts and compliance below make it a scroll.

## What's Working
- Severity chips carry `severity_counts`, and a tile's link lands on a pressed chip.
- Under the Client chip every list and count reads that client's, including the select-all resolvers.
- The decision bar states its blast radius before the first tick.

## Priority Issues
- **[P1] The app updates list offered severity chips** although its package managers publish no severity: every chip but "All" could only empty it. Fix: no severity filter on that list (`severityFilter={false}`). Suggested command: /impeccable harden. *(Applied in polish.)*
- **[P2] The selection is not the kit's BulkBar.** The module's DecisionBar must stay visible with nothing ticked (ring, blast radius, Select all N matching); BulkBar renders only with a selection. Open question for the coordinator.
- **[P2] The range sentence wraps** to its own line under the toolbar when the Client chip is drawn (1440). Suggested command: /impeccable layout.
- **[P3] Long page**: approvals, restarts and compliance under the tabs; consider anchors from the head.

## Persona Red Flags
**Technician (desk)**: approving a set is fast (select all N, ring, approve); no flag.
**Technician (phone)**: at 390 the four tiles stack before the list; the platform tabs scroll sideways but stay one row.

## Minor Observations
- The `Policies` head action and the page's other two secondary buttons share one weight; fine (no primary in the head).

## Questions to Consider
- Should approvals, restarts and compliance move to views like Alerts' (D21), or stay as the record below?
