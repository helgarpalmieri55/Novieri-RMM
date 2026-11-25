---
target: "A person, wave O6: frontend/src/modules/people/PersonPage.tsx"
total_score: 29
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 1
target_identity: "file:frontend/src/modules/people/PersonPage.tsx"
target_fingerprint: "sha256:cd3494e857e0d1a168b22cd0cb03eb101a5df42f34e4b2a9836cfc06a95012e6"
target_path: frontend/src/modules/people/PersonPage.tsx
timestamp: 2026-11-25T06-58-35Z
slug: frontend-src-modules-people-personpage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: A person (`frontend/src/modules/people/PersonPage.tsx`, `/people/person-maria`)

Redesign plan, Task 11 (wave O6), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (`/people/person-maria`, 1440x900, 390x844, 390x844-client and 641x900-client, Spanish) and this run's own captures with a client chosen (English, 1440) before any detector output; Assessment B followed.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Status in the head; the checklist's pending count |
| 2 | Match System / Real World | 3 | Job title and department as context; how we know a link |
| 3 | User Control and Freedom | 3 | Tabs in the URL; every release asks first |
| 4 | Consistency and Standards | 3 | The kit's record: head, tiles, tabs |
| 5 | Error Prevention | 3 | Releases and turn-off confirmed |
| 6 | Recognition Rather Than Recall | 3 | Tiles lead to their tab |
| 7 | Flexibility and Efficiency | 3 | A tab is a link |
| 8 | Aesthetic and Minimalist Design | 2 | The Details beside Computers is a third of the row: the email and the sign-in name are cut off at 1440 and at 390 |
| 9 | Error Recovery | 3 | A failed release said as a sentence |
| 10 | Help and Documentation | 3 | A hidden section says it is hidden |
| **Total** | | **29/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** A person's footprint across the console (computers, licences, seats, tickets) with the starter and leaver work on top. Specific.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/people` from the worktree root: exit 0, `[]`.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh page per route on this worktree's own Vite with `?__fixtures`, a client chosen, title marked `[Human]`; `impeccable live-server --background` on 8401, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). `/people/person-maria` and `?tab=licenses` en 1440: 11 findings each. `repeated-container-text` ("Go to the page" six times) is the checklist's per-step link, each to a different page; `nested-cards` is the checklist's step list inside its panel (`ChecklistPanel`, unchanged by this wave); the rest as below. The detector does not see the clipped Details values (heuristic 8), which the capture shows. `undersized-ui-text` on the 10.5px captions (tile labels, table headers, chip counts) is a false positive: DESIGN.md's Caption step is 10.5px and PRODUCT.md sets the floor there. `gray-on-color` (#8592ab on #1b2945) is the rail's tenant card and `overused-font` (Inter) is the brand's one face, both the shell's and both explained in its critique.

## Overall Impression

Good structure; one layout defect: long values clip in the narrow Details column.

## What's Working

- The open checklist stays above the tabs, on whichever tab is open.
- Each tile is a link to the tab where its number lives.

## Priority Issues

- **[P1] Details clips its values.** At 1440 the aside is ~380px and `maria.garcia@toeshee...` and `azuread\mariagarc...` are cut, not wrapped; at 390 too. A value a person reads is not on the page. **Fix:** Computers full width, then Details and Sign-in side by side under it; long values break (`overflow-wrap: anywhere`). **Command:** /impeccable layout
- **[P3] Three tiles without a foot.** Each is a bare count. **Fix:** Leave: the tab says the rest. **Command:** /impeccable polish

## Persona Red Flags

**Casey (phone).** The email and sign-in name are clipped (P1).

**Alex.** Tabs and tiles are links. No red flag.

## Minor Observations

- The checklist's step titles are the server's words (English in the fixtures).

Questions skipped: 2 priority issues

## Polish (same day, before `close`)

- P1, Details clips its values: the email, the sign-in name and the account's address break where they must (`overflow-wrap: anywhere`, which lets the grid track of the kit's `.kv` shrink) instead of running past the aside. The layout stays the kit's `DetailGrid` (Receta F, 4: information left, the rest right) rather than the restructure the issue first proposed; the kit's `.kv dd` could carry the same rule for every page (kit request).
- P3, the tiles without a foot: left as they are.
- Step 5: harmony on the wave's sixteen routes again, both languages, four viewports, after the polish (the wave's report lists the PNGs).
