---
target: "Changes, wave O6: frontend/src/modules/change/ChangesPage.tsx"
total_score: 28
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 1
target_identity: "file:frontend/src/modules/change/ChangesPage.tsx"
target_fingerprint: "sha256:559ecaa84bd0d7ad642ac1a24e61e57ce299f7a34dc8f605136a86864400bf50"
target_path: frontend/src/modules/change/ChangesPage.tsx
timestamp: 2026-11-25T06-58-35Z
slug: frontend-src-modules-change-changespage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Changes (`frontend/src/modules/change/ChangesPage.tsx`, `/changes`)

Redesign plan, Task 11 (wave O6), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (`/changes`, 1440x900, 390x844, 390x844-client and 641x900-client, Spanish) and this run's own captures with a client chosen (English, 1440) before any detector output; Assessment B followed.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | "Showing 7 of 7", the pressed chips, n/a on a tile before the list answers |
| 2 | Match System / Real World | 3 | States and types in words; the tiles say which zero a zero is |
| 3 | User Control and Freedom | 3 | State, type and client in the URL; "All" as the way back; New change asks before Back drops a draft |
| 4 | Consistency and Standards | 3 | Receta L, the module's SectionNav, the kit's empty states |
| 5 | Error Prevention | 3 | New change starts empty however it opens; the Client chip only narrows the read |
| 6 | Recognition Rather Than Recall | 2 | Thirteen chips in two rows at 1440; seven rows at 390, so the list starts 1200px down on a phone |
| 7 | Flexibility and Efficiency | 3 | Every tile opens the rows it counted, the chip included |
| 8 | Aesthetic and Minimalist Design | 2 | The filter toolbar is the page's heaviest object; "Rejected 0" is a chip that opens an empty view |
| 9 | Error Recovery | 3 | "Nothing matches these filters" with Clear; "No changes yet" with New change |
| 10 | Help and Documentation | 3 | The readiness panel says what approving changes needs |
| **Total** | | **28/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** The change list a technician needs: the open work, the queue that blocks someone, what is being carried out now, and the emergencies, each a tile that opens its rows. The filter bar is where it stops being specific: every state the wire knows is a chip of equal weight.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/change` from the worktree root: exit 0, `[]`.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh page per route on this worktree's own Vite with `?__fixtures`, a client chosen, title marked `[Human]`; `impeccable live-server --background` on 8401, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). `/changes` en 1440: 11 findings. `nested-cards` is the readiness panel's step inside its panel (`ChangeReadiness`, the module's prerequisite panel, drawn as credentials' is); the rest as below. `undersized-ui-text` on the 10.5px captions (tile labels, table headers, chip counts) is a false positive: DESIGN.md's Caption step is 10.5px and PRODUCT.md sets the floor there. `gray-on-color` (#8592ab on #1b2945) is the rail's tenant card and `overused-font` (Inter) is the brand's one face, both the shell's and both explained in its critique.

## Overall Impression

Correct, but the toolbar shouts. Nine state chips and four type chips are a wall of options (cognitive load: a decision point with 13 visible choices), and on a phone they bury the list.

## What's Working

- The four tiles and the chips are the same keys (`?state=`, `?type=`), so a tile's destination is visibly the chip it presses.
- Under the Client chip every tile carries `client=`, and "Awaiting approval" then opens this list's submitted view rather than the queue of every client.
- New change is a `?new=` dialog that starts empty and asks before Back or Cancel drop what was typed.

## Priority Issues

- **[P1] The filter toolbar is thirteen chips.** Two rows at 1440 and seven at 390 before the first change; a decision point with more than four visible options, and "Rejected 0" is a chip that can only open an empty list. **Fix:** Keep "All" and "Open" as chips (the tiles' views) and move the seven states and the three types into two of the kit's chips with a list (`.chip-select`, as the Client chip), each option with its count. One row at 1440, three at 390. **Command:** /impeccable distill
- **[P3] The readiness panel fills the first screen.** It is the module's shared prerequisite panel and says what approving needs; on a phone it is 380px before the tiles. **Fix:** None here; it collapses when everything is done. **Command:** /impeccable layout
- **[P3] The change's window is clipped at 390.** The sub-line ellipsises the two ends of the window. **Fix:** None: the table scrolls and the change's page has the window whole. **Command:** /impeccable adapt

## Persona Red Flags

**Alex (power user).** A state is one click but has to be found among nine; the URL keeps it. Red flag: scanning the chip wall (P1).

**Casey (phone).** Seven rows of chips and the readiness panel before the first change (P1).

**Jordan (first-timer).** "Rejected 0", "Cancelled 1", "Completed 1" with equal weight to "Open 5": nothing says which matters. Red flag (P1).

## Minor Observations

- The window's en dash in the sub-line is a range mark, not the em dash the copy rule bans.

## Questions to Consider

1. **Does a state list break "filters as chips"?** The brief asks for the state filter as chips. The kit's chip with a list (`.chip-select`, as the Client chip) is a chip, on the same `?state=` key; the two views the tiles open (All, Open) stay plain chips. Settled by DESIGN.md (one decision point, few visible options) and the kit: taken.
2. **Should a chip that would open an empty view be hidden?** No: hiding a state makes a link to it look broken. Its count says it is empty.
3. **Should the readiness panel fold away?** It already collapses when nothing is pending; no page-level change.

The answers are appended to `.impeccable/critica-respuestas.md`; none goes to the coordinator.

## Polish (same day, before `close`)

- P1, the thirteen chips: "All" and "Open" stay chips (a group named "Show"); the seven states and the three types are the kit's chip with a list (`.chip-select`, as the Client chip) on the same `?state=` and `?type=` keys, each option with its count ("Draft (1)"). New keys `change.list.viewLabel` (Show / Mostrar) and `change.list.anyState` (Any state / Cualquier estado). Tests adapted first and seen red (3 failed), then green.
- P3, the readiness panel and the clipped window at 390: left as they are.
- Step 5: harmony on the wave's sixteen routes again, both languages, four viewports, after the polish (the wave's report lists the PNGs).
