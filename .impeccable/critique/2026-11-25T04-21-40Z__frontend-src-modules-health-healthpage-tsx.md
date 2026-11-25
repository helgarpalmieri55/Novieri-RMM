---
target: frontend/src/modules/health/HealthPage.tsx
total_score: 30
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 1
target_identity: "file:/home/user/rmm-w3/frontend/src/modules/health/HealthPage.tsx"
target_fingerprint: "sha256:05081d00a6faee9da677b896f10de170ff6964399a81e427eecb881d898f7e5f"
target_path: /home/user/rmm-w3/frontend/src/modules/health/HealthPage.tsx
timestamp: 2026-11-25T04-21-40Z
slug: frontend-src-modules-health-healthpage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Alerts (`frontend/src/modules/health/HealthPage.tsx`, /health and its three views)

Assessment A (design review) was run first from the source and the harmony captures (en/es, 1440x900 and
390x844, plus the three `?view=` variants); Assessment B (detector + browser) after it.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 4 | Server counts on every chip and tile; the count sentence says open of shown |
| 2 | Match System / Real World | 3 | h1 "Health & alerts" while the rail and the tab say "Alerts" |
| 3 | User Control and Freedom | 3 | Every filter is a chip with "Any …" to undo it; Back undoes each |
| 4 | Consistency and Standards | 3 | Kit toolbar, chips, ListEmpty; the resolution filter is a chip-select among toggles |
| 5 | Error Prevention | 3 | The resolution filter is offered for every state although only cleared alerts have one |
| 6 | Recognition Rather Than Recall | 3 | Four views as URL tabs; old anchors land in their view |
| 7 | Flexibility and Efficiency | 3 | Tiles are links into the filtered queue; sort toggle kept |
| 8 | Aesthetic and Minimalist Design | 2 | Toolbar wraps onto three rows at 1440 (10 chips + 2 selects + sort + count) |
| 9 | Error Recovery | 3 | Load failures stay explicit (never "nothing open"); filtered empty offers Clear filters |
| 10 | Help and Documentation | 3 | Readiness panel says what has to be set up and links there |
| **Total** | | **30/40** | **Good** |

## Design Specificity Verdict

**LLM assessment**: specific to this product. The queue speaks the module's own distinctions (acknowledged and
muted still open, machine-resolved vs closed by a person) and the views split configuration from triage the way
Atera splits Alerts from its settings, without losing the anchors other modules write.

**Deterministic scan**: `impeccable detect --json frontend/src/modules/health` (from the worktree root) → 0
findings. Browser overlay (live-server + detect.js over `npx vite`, 1440x900, `/health` and `/health?view=profiles`):
the system-level findings every console page carries — `undersized-ui-text` on the 10.5px column headers and tile
labels (DESIGN.md's label step), `gray-on-color` on the rail's dim text (shell), `overused-font` Inter (the brand
face) — plus, on `/health`, `straggler` and `nested-cards` inside the shared readiness panel
(`components/prerequisites`, class 1). No finding in this page's own markup.

## Overall Impression
The views make the page readable: the queue is the default and configuration is one tab away. The weak spot is the
toolbar: two chip groups of six and four, a chip-select, the Client chip, the sort toggle and the count stack into
three rows on a desktop.

## What's Working
- Chip counts come from `/health/alerts/summary` of the list's own client; nothing is counted from the page.
- An empty queue says its cause: no computer reporting (with Add a computer), no alert yet, or the filters.
- Tiles and report figures open the rows they counted, keeping the Client chip.

## Priority Issues
- **[P1] Two chip groups read as one row.** Why: "All" twice side by side; a person cannot tell which "All" clears
  what. Fix: name each group's first chip ("Any state", "Any severity"). Suggested command: /impeccable clarify.
  *(Applied during the build, before this critique was persisted.)*
- **[P2] The toolbar is three rows at 1440.** Why: the queue starts low; ten chips compete. Fix: keep the state
  chips to the ones a person picks most, or move the resolution filter behind the Cleared chip. Suggested command:
  /impeccable distill.
- **[P2] h1 vs rail.** "Health & alerts" in the head, "Alerts" in the rail and the tab title. Fix: decide one word
  (open question for the coordinator). Suggested command: /impeccable clarify.
- **[P3] Readiness panel above the tiles** pushes the queue below the fold at 390 when a step is open.

## Persona Red Flags
**Technician on a phone (incident)**: at 390 the readiness panel and four stacked tiles come before the queue; the
first alert is two screens down.
**Auditor**: the report under the queue is reachable and its figures link to their rows; no flag.

## Minor Observations
- The count sentence ("12 open of 17 shown") sits under the floating Assistant button in full-page captures only
  (fixed position); not visible in use.

## Questions to Consider
- Should the page title become "Alerts", as the rail says?
- Does "Cleared" need to be a chip, or only a destination of the report's links?
