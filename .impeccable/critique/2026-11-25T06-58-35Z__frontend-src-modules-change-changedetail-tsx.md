---
target: "A change, wave O6: frontend/src/modules/change/ChangeDetail.tsx"
total_score: 30
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/change/ChangeDetail.tsx"
target_fingerprint: "sha256:6436a2985c60982848c4e648313e92e094349b4d3fd5c1555c186eb53e93a5f0"
target_path: frontend/src/modules/change/ChangeDetail.tsx
timestamp: 2026-11-25T06-58-35Z
slug: frontend-src-modules-change-changedetail-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: A change (`frontend/src/modules/change/ChangeDetail.tsx`, `/changes/chg-1`)

Redesign plan, Task 11 (wave O6), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (`/changes/chg-1`, 1440x900, 390x844, 390x844-client and 641x900-client, Spanish) and this run's own captures with a client chosen (English, 1440) before any detector output; Assessment B followed.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | State in the head; the lifecycle says why a closed change offers nothing |
| 2 | Match System / Real World | 3 | Risk and type in words; the window in the profile's zone |
| 3 | User Control and Freedom | 3 | Tabs in the URL; "All changes" back; every ending asks first |
| 4 | Consistency and Standards | 3 | The kit's record: head, tabs; the facts strip below |
| 5 | Error Prevention | 3 | The requester cannot decide their own change, and it says so |
| 6 | Recognition Rather Than Recall | 3 | State, risk and client in the head; the facts strip keeps what the head does not say |
| 7 | Flexibility and Efficiency | 3 | Approve with a comment in two clicks |
| 8 | Aesthetic and Minimalist Design | 3 | The head's context reads "Risk [High] · Toeshee" with the separator floating after the pill |
| 9 | Error Recovery | 3 | The server's own refusal, as a sentence |
| 10 | Help and Documentation | 3 | Hidden actions named with the permission that hides them |
| **Total** | | **30/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** A change record: the decision is the page's centre and the facts and history are a tab away.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/change` from the worktree root: exit 0, `[]`.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh page per route on this worktree's own Vite with `?__fixtures`, a client chosen, title marked `[Human]`; `impeccable live-server --background` on 8401, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). `/changes/chg-1` en 1440: 5 findings, all of the kinds below. `undersized-ui-text` on the 10.5px captions (tile labels, table headers, chip counts) is a false positive: DESIGN.md's Caption step is 10.5px and PRODUCT.md sets the floor there. `gray-on-color` (#8592ab on #1b2945) is the rail's tenant card and `overused-font` (Inter) is the brand's one face, both the shell's and both explained in its critique.

## Overall Impression

Good. The tab title now comes from the shell, and the head says the three facts a reader looks for first.

## What's Working

- The lifecycle offers exactly the transitions the server accepts from this state, with the reason when none.
- Approvals and linked actions are their own tabs in the URL, so a link can open the history.

## Priority Issues

- **[P3] The head's context separator.** "· Toeshee" sits after the pill's gap rather than between two words. **Fix:** Leave: the kit's context slot spaces its children; a separate span per fact is the kit's call. **Command:** /impeccable polish
- **[P3] Three facts in a strip.** Type, window and raised; the strip is narrow on a desk. **Fix:** Leave; it no longer repeats the head. **Command:** /impeccable layout

## Persona Red Flags

**Alex.** Approve, comment, confirm. No red flag.

**Sam.** Tabs are a tablist with the selected one marked. No red flag.

Questions skipped: 2 priority issues

## Polish (same day, before `close`)

- Nothing to polish (two P3, left as they are).
- Step 5: harmony on the wave's sixteen routes again, both languages, four viewports, after the polish (the wave's report lists the PNGs).
