---
target: "IT documentation, wave O6: frontend/src/modules/docs/DocsPage.tsx"
total_score: 29
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/docs/DocsPage.tsx"
target_fingerprint: "sha256:5d8849ab0725fadfad16a1c0e4881348f45477566f6454fc7793f170b3997090"
target_path: frontend/src/modules/docs/DocsPage.tsx
timestamp: 2026-11-25T06-58-35Z
slug: frontend-src-modules-docs-docspage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: IT documentation (`frontend/src/modules/docs/DocsPage.tsx`, `/docs`)

Redesign plan, Task 11 (wave O6), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (`/docs`, 1440x900, 390x844, 390x844-client and 641x900-client, Spanish) and this run's own captures with a client chosen (English, 1440) before any detector output; Assessment B followed.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | The kit's loading, "Showing x of y", the pressed category chip; a save in flight on the save bar |
| 2 | Match System / Real World | 3 | Records and documents in the operator's words; the categories are the library's own |
| 3 | User Control and Freedom | 3 | Search, category and client in the URL; `?article=` linkable; Back undoes a chip; the editor's Discard and Cancel |
| 4 | Consistency and Standards | 3 | Receta L for the library, Receta FO for the editor, one save bar; the reader beside the list |
| 5 | Error Prevention | 3 | Leaving the editor with changes asks; saving the article selects it without asking (D16) |
| 6 | Recognition Rather Than Recall | 3 | Category chips with counts; each row says its category and how many objects link to it |
| 7 | Flexibility and Efficiency | 3 | `/` focuses the search; every view a link |
| 8 | Aesthetic and Minimalist Design | 2 | The page sentence is 94 characters and leaves "describen." alone at 1440; the search note is a permanent full-width paragraph |
| 9 | Error Recovery | 3 | "No documentation matches that search" with the search's own Clear; the kit's "Nothing matches these filters" |
| 10 | Help and Documentation | 3 | The note under the search says what it does not search |
| **Total** | | **29/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** A library that reads as this product's: the records and stored documents beside the articles, the link counts that tell whether a runbook is reachable from the machine it describes, and a reader that sits beside the list on a desk and takes the screen on a phone. The kit pieces carry it; nothing is drawn for its own sake.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/docs` from the worktree root: exit 2, one finding: `broken-image` at `DocumentViewer.tsx:255`. A false positive the brief names: the `src={url}` is on the next line (256) and the `<img>` is the document image viewer, with its security reasoning in the comment above it. Not "fixed"; the exception is requested from the coordinator (`impeccable ignores add-value broken-image "*" --file "frontend/src/modules/docs/DocumentViewer.tsx" --reason "False positive: the src is on the next line (DocumentViewer.tsx:256); the img is the document image viewer." --shared`).

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh page per route on this worktree's own Vite with `?__fixtures`, a client chosen, title marked `[Human]`; `impeccable live-server --background` on 8401, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). `/docs` en 1440: 11 findings, all of the kinds below. `/docs?article=a-1`: the same plus `first-viewport-column-overflow` on the library grid (the list column runs 151% of the viewport beside a reader that fits in 70%): a long library beside a short article, the reader being what was opened; no change. `undersized-ui-text` on the 10.5px captions (tile labels, table headers, chip counts) is a false positive: DESIGN.md's Caption step is 10.5px and PRODUCT.md sets the floor there. `gray-on-color` (#8592ab on #1b2945) is the rail's tenant card and `overused-font` (Inter) is the brand's one face, both the shell's and both explained in its critique.

## Overall Impression

Sound and quiet. The one thing that reads wrong is the page sentence, too long for its line; on a phone the four tiles push the library a screen down, which is the kit's folding.

## What's Working

- The library's search, category and Client chip are the URL, so a filtered library or an open article is a link to paste into a ticket.
- The editor is one draft on the one save bar: a new article can still be refused for an empty title, an edit shows the bar only once something changed, and saving selects the article without the "Leave without saving?" question.
- Every list on the page reads through the chip's client, so the tiles, the search and the records agree with what the chip says.

## Priority Issues

- **[P2] The page sentence is 94 characters.** Receta L asks 40 to 70; at 1440 the Spanish leaves "describen." alone on its second line, the straggler the clients page fixed. **Fix:** "Runbooks, records and files about your systems." / "Procedimientos, registros y archivos de tus sistemas." (the rail's own hint). **Command:** /impeccable clarify
- **[P3] On a phone the four tiles come before the library.** At 390 the tiles fold to one column and the list starts about 1000px down. It is the kit's `StatsGrid` folding, the same on every page. **Fix:** None in the page; a kit question if it recurs. **Command:** /impeccable adapt
- **[P3] The search note is a standing paragraph.** It is the §11.6 statement of what search does not cover; it costs one line at 1440 and four at 390. **Fix:** Keep it (the spec asks for it); it is short enough at 1440. **Command:** /impeccable distill

## Persona Red Flags

**Alex (power user).** `/` focuses the search; a category is one click and Back undoes it; an article opens in its own URL. No red flag.

**Casey (phone).** The library is a screen down under the tiles (P3); once an article is open the reader takes the screen and "All articles" leads back. No other red flag.

**Sam (keyboard, screen reader).** Chips are toggles in a named group; the editor's bar is a named region and its Save is the form's submit, so Enter saves. No red flag.

## Minor Observations

- The category chip labels are recorded data ("Runbook", "Network") and read in English in the Spanish console; data, not copy.

## Questions to Consider

1. **Should the reader beside the list be the kit's `DetailGrid`?** The brief asks for `DetailGrid` (list left, reader right); the kit's grid puts its 1fr aside on the right, so the library (the narrow column) would be on the right of the reader. Not settled by DESIGN.md: the page keeps its own two-column grid (list left, as Atera's documentation and the brief draw it) and the wave asks the coordinator for a `DetailGrid` option to put the aside first (kit request).
2. **Is the search note worth its line?** Settled by the module spec (§11.6 asks the page to say what search does not cover): it stays.
3. **Should the tiles fold after the list on a phone?** Settled by Product principle 1 (the kit folds every page the same way): no page-level change.

Answers 2 and 3 are appended to `.impeccable/critica-respuestas.md`; 1 goes to the coordinator in the wave's report.

## Polish (same day, before `close`)

- P2, the page sentence: the Spanish one was the 94-character sentence ("Manuales de procedimiento, registros y archivos, vinculados a los dispositivos que describen."); now "Procedimientos, registros y archivos, con los equipos que describen." (68, and "equipos" as the glossary says). The English one was already 66 and stays.
- P3, the tiles before the library on a phone and the search note: left as they are (the kit's folding; the module spec, §11.6).
- Step 5: harmony on the wave's sixteen routes again, both languages, four viewports, after the polish (the wave's report lists the PNGs).
