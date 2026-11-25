---
target: "Approval queue, wave O6: frontend/src/modules/change/ApprovalQueue.tsx"
total_score: 29
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/change/ApprovalQueue.tsx"
target_fingerprint: "sha256:c1dddd1c2d636f0057182c02c422de8276ed538b0a0de2e1acf6b3d49439cd6e"
target_path: frontend/src/modules/change/ApprovalQueue.tsx
timestamp: 2026-11-25T06-58-35Z
slug: frontend-src-modules-change-approvalqueue-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Approval queue (`frontend/src/modules/change/ApprovalQueue.tsx`, `/changes/approvals`)

Redesign plan, Task 11 (wave O6), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (`/changes/approvals`, 1440x900, 390x844, 390x844-client and 641x900-client, Spanish) and this run's own captures with a client chosen (English, 1440) before any detector output; Assessment B followed.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | "Showing 2 of 2"; a refused decision is said on its row |
| 2 | Match System / Real World | 3 | Risk and state in words; "window 02 Dec, 08:46" |
| 3 | User Control and Freedom | 3 | The module's links; a decision is one row's, with its comment |
| 4 | Consistency and Standards | 2 | Every row's Approve is a primary button; the budget's queue made its row actions outline for this reason |
| 5 | Error Prevention | 3 | Your own change: Approve disabled and the reason on screen (ruling 8) |
| 6 | Recognition Rather Than Recall | 3 | Name, risk, window and the decision on one row |
| 7 | Flexibility and Efficiency | 3 | Approve or reject without opening the change |
| 8 | Aesthetic and Minimalist Design | 3 | A short list, no noise; the state column repeats "Submitted" on every row |
| 9 | Error Recovery | 3 | "Nothing is waiting for your approval." |
| 10 | Help and Documentation | 3 | The page sentence says what waits |
| **Total** | | **29/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** A queue whose job is one decision per row, with the comment beside it. Specific to change approval; nothing borrowed.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/change` from the worktree root: exit 0, `[]`.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh page per route on this worktree's own Vite with `?__fixtures`, a client chosen, title marked `[Human]`; `impeccable live-server --background` on 8401, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). `/changes/approvals` en 1440: 6 findings, all of the kinds below. `undersized-ui-text` on the 10.5px captions (tile labels, table headers, chip counts) is a false positive: DESIGN.md's Caption step is 10.5px and PRODUCT.md sets the floor there. `gray-on-color` (#8592ab on #1b2945) is the rail's tenant card and `overused-font` (Inter) is the brand's one face, both the shell's and both explained in its critique.

## Overall Impression

Good. The one inconsistency is the weight of the buttons: a queue of twenty submitted changes would be twenty primaries.

## What's Working

- The decision is on the row, with its comment, and the reason someone cannot decide their own change is now written under the buttons.
- An empty queue says why ("Nothing is waiting for your approval.").
- The module's three pages are one strip of links with the current one marked.

## Priority Issues

- **[P2] Approve is a primary on every row.** DESIGN.md: one primary per page; the budget's confirmation queue already made its row decisions outline ("a queue of twenty rows is twenty Confirm buttons"). **Fix:** Approve as the outline button beside Reject; the decision is still the row's first control. **Command:** /impeccable quieter
- **[P3] The state column repeats "Submitted".** Every row of this queue is submitted by definition. **Fix:** Leave it: the column says the state the decision changes, and a re-triaged row would show it. **Command:** /impeccable distill

## Persona Red Flags

**Alex (power user).** One click per decision, comment optional. No red flag.

**Sam (keyboard).** The comment field is labelled per change; the disabled Approve has its reason in text. No red flag.

## Minor Observations

- The comment field takes the width of the column; fine at 1440, wraps under the buttons at 390.

Questions skipped: 2 priority issues

## Polish (same day, before `close`)

- P2, Approve on every row: the outline button, as Reject beside it and as the budget's confirmation queue.
- Step 5: harmony on the wave's sixteen routes again, both languages, four viewports, after the polish (the wave's report lists the PNGs).
