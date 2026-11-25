---
target: "Needs a person, wave O6: frontend/src/modules/people/UnmatchedPage.tsx"
total_score: 29
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/people/UnmatchedPage.tsx"
target_fingerprint: "sha256:28412cbc1815fe717be4cc031c0ab676e019f29e53fee39ab281837e9dcab246"
target_path: frontend/src/modules/people/UnmatchedPage.tsx
timestamp: 2026-11-25T06-58-35Z
slug: frontend-src-modules-people-unmatchedpage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Needs a person (`frontend/src/modules/people/UnmatchedPage.tsx`, `/people/unmatched`)

Redesign plan, Task 11 (wave O6), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (`/people/unmatched`, 1440x900, 390x844, 390x844-client and 641x900-client, Spanish) and this run's own captures with a client chosen (English, 1440) before any detector output; Assessment B followed.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Each type's count from the whole answer |
| 2 | Match System / Real World | 3 | "Every computer has a person." |
| 3 | User Control and Freedom | 3 | The type in the URL |
| 4 | Consistency and Standards | 3 | Receta L |
| 5 | Error Prevention | 3 | Linking a seat goes through the picker |
| 6 | Recognition Rather Than Recall | 3 | One table, the type chosen |
| 7 | Flexibility and Efficiency | 3 | A type is a link |
| 8 | Aesthetic and Minimalist Design | 2 | The default type can be empty while another has rows: the page opens on "Every computer has a person." with "Subscription seats 1" in a chip |
| 9 | Error Recovery | 3 | Each type says its own empty |
| 10 | Help and Documentation | 3 | The sentence over each table says what to do |
| **Total** | | **29/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** A worklist of loose ends; specific.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/people` from the worktree root: exit 0, `[]`.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh page per route on this worktree's own Vite with `?__fixtures`, a client chosen, title marked `[Human]`; `impeccable live-server --background` on 8401, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). `/people/unmatched` en 1440: 2 findings, both of the kinds below. `undersized-ui-text` on the 10.5px captions (tile labels, table headers, chip counts) is a false positive: DESIGN.md's Caption step is 10.5px and PRODUCT.md sets the floor there. `gray-on-color` (#8592ab on #1b2945) is the rail's tenant card and `overused-font` (Inter) is the brand's one face, both the shell's and both explained in its critique.

## Overall Impression

Good; the default view can open on an empty type.

## What's Working

- The three counts add up to what the three tables used to show.
- An empty type says so for that type.

## Priority Issues

- **[P2] The default type can be empty.** The page opens on computers, which may have nothing, while seats wait in a chip. **Fix:** The empty state offers the type that has rows ("See the subscription seats"); the default stays computers and writes nothing. **Command:** /impeccable onboard

## Persona Red Flags

**Jordan.** Lands on "Every computer has a person." and may not see the seat (P2).

Questions skipped: 1 priority issues

## Polish (same day, before `close`)

- P2, the empty default type: a type's empty state offers the first type that has rows ("See the subscription seats" / "Ver los puestos de suscripción", keys `people.unmatched.see.*`); the default stays computers and writes nothing. New test "offers the type that has rows when the one shown is empty", seen red then green.
- Step 5: harmony on the wave's sixteen routes again, both languages, four viewports, after the polish (the wave's report lists the PNGs).
