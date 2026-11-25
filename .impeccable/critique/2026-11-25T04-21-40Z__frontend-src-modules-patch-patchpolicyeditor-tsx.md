---
target: frontend/src/modules/patch/PatchPolicyEditor.tsx
total_score: 31
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 2
target_identity: "file:/home/user/rmm-w3/frontend/src/modules/patch/PatchPolicyEditor.tsx"
target_fingerprint: "sha256:2e3a31557f89ba70cdf311448ddde4e781a2a963b9cdf6def527c92d05e92b0a"
target_path: /home/user/rmm-w3/frontend/src/modules/patch/PatchPolicyEditor.tsx
timestamp: 2026-11-25T04-21-40Z
slug: frontend-src-modules-patch-patchpolicyeditor-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Patch policies (`frontend/src/modules/patch/PatchPolicyEditor.tsx`)

Assessment A (design review) was run first from the source and the harmony captures of /patch/policies and a Playwright capture of /patch/policies?policy=pol-default (1440x900); Assessment B (detector + browser) after it.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Save bar says unsaved/saving/failed/saved; the edited row is marked |
| 2 | Match System / Real World | 3 | Windows painted on a week grid and read back in words |
| 3 | User Control and Freedom | 3 | Discard, Back, and a question before leaving with changes |
| 4 | Consistency and Standards | 4 | FormPage, FormSection, SaveBar, UnsavedGuard from the kit |
| 5 | Error Prevention | 4 | Overwrite confirmation before a New replaces a group's policy |
| 6 | Recognition Rather Than Recall | 3 | The table reads each window as a sentence |
| 7 | Flexibility and Efficiency | 3 | Enter saves; the policy is a link (?policy=) |
| 8 | Aesthetic and Minimalist Design | 3 | Three sections, no nested cards |
| 9 | Error Recovery | 3 | Problems and server refusals in the bar |
| 10 | Help and Documentation | 2 | Ring order and scan interval carry no help text |
| **Total** | | **31/40** | **Good** |

## Design Specificity Verdict

**LLM assessment**: specific: the week painter, the overwrite rule (the route upserts by device group) and the UTC note are this module's, and the form says them where the decision is made.

**Deterministic scan**: `impeccable detect --json frontend/src/modules/patch` (from the worktree root) → 0
findings. Browser overlay (live-server + detect.js over `npx vite`, 1440x900, `/patch/policies?policy=pol-default`): only the system-level
findings every console page carries — `undersized-ui-text` on the 10.5px column headers and tile labels
(DESIGN.md's label step), `gray-on-color` on the rail's dim text (shell), `overused-font` Inter (the brand face).
No finding in this page's own markup.

## Overall Impression
The dialog became a form below its list, which fits the console's form pattern. The risk the dialog was introduced to fix comes back: the form opens below the fold.

## What's Working
- One save bar, inside the form, so Enter saves and the bar carries the refusal.
- The open policy is in the URL and the row is marked.
- The overwrite confirmation is unchanged.

## Priority Issues
- **[P1] Edit opened the form off screen** under a long list or at 390: a click that changes nothing visible, the audit's original complaint about this page. Fix: bring the form into view when it opens. Suggested command: /impeccable harden. *(Applied in polish: `scrollIntoView` on open.)*
- **[P1] Two primaries with the form open** (New policy and Save). Fix: New policy secondary; Save is the form page's primary (DESIGN.md, "Buttons"). *(Applied in polish.)*
- **[P2] Crumb and back link both say "Patching"** one line apart (shell crumb + FormPage back). Kit-level; note for the coordinator.
- **[P3] Ring order and scan interval** have no help text; the bounds are only learnt from a refusal.

## Persona Red Flags
**Technician**: edits a ring in place, sees Saved; no flag.
**First-timer**: "Ring order" means nothing without help text.

## Minor Observations
- "Applies to: Organisation default." sits tight above the fields.

## Questions to Consider
- Should a new policy open in place of the list (a page of its own, `/patch/policies/new`)?
