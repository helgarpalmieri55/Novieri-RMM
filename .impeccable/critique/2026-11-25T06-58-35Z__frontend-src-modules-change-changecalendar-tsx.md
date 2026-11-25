---
target: "Change calendar, wave O6: frontend/src/modules/change/ChangeCalendar.tsx"
total_score: 30
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/change/ChangeCalendar.tsx"
target_fingerprint: "sha256:653f99950e9a4f5164854571a68e54e5324e273522cc8fe7a1b388aebaca51f3"
target_path: frontend/src/modules/change/ChangeCalendar.tsx
timestamp: 2026-11-25T06-58-35Z
slug: frontend-src-modules-change-changecalendar-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Change calendar (`frontend/src/modules/change/ChangeCalendar.tsx`, `/changes/calendar`)

Redesign plan, Task 11 (wave O6), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (`/changes/calendar`, 1440x900, 390x844, 390x844-client and 641x900-client, Spanish) and this run's own captures with a client chosen (English, 1440) before any detector output; Assessment B followed.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | The window spelled back in the console's format; the conflict count as a notice with its glyph |
| 2 | Match System / Real World | 3 | "From 01 Nov 2026 to 30 Nov 2026, in your timezone" |
| 3 | User Control and Freedom | 3 | The window in the URL; a cleared end is kept; the module's links |
| 4 | Consistency and Standards | 3 | PageHead, SectionNav, the Period panel as /bcp/report draws it |
| 5 | Error Prevention | 3 | Instants sent in the profile's zone, not the browser's |
| 6 | Recognition Rather Than Recall | 3 | Conflicting rows carry a badge and the critical gutter |
| 7 | Flexibility and Efficiency | 3 | A window is a link |
| 8 | Aesthetic and Minimalist Design | 3 | Compact; the date inputs keep the browser's mask (11/01/2026) beside a sentence in the console's format |
| 9 | Error Recovery | 3 | "No changes scheduled in this window." |
| 10 | Help and Documentation | 3 | The sentence under the fields says what the window means |
| **Total** | | **30/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** A maintenance calendar that is about collisions: the conflict notice and the per-row badge are the page's reason to exist.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/change` from the worktree root: exit 0, `[]`.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh page per route on this worktree's own Vite with `?__fixtures`, a client chosen, title marked `[Human]`; `impeccable live-server --background` on 8401, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). `/changes/calendar` en 1440: 6 findings, all of the kinds below. `undersized-ui-text` on the 10.5px captions (tile labels, table headers, chip counts) is a false positive: DESIGN.md's Caption step is 10.5px and PRODUCT.md sets the floor there. `gray-on-color` (#8592ab on #1b2945) is the rail's tenant card and `overused-font` (Inter) is the brand's one face, both the shell's and both explained in its critique.

## Overall Impression

Good as it is; moving the window into the URL made it shareable without changing what it shows.

## What's Working

- `?from=`/`?to=` keep a window across a reload, and a cleared end stays cleared instead of snapping back to this month.
- The conflict is carried three ways: the notice, the badge and the critical gutter.

## Priority Issues

- **[P3] The native date mask.** The inputs read `11/01/2026` to an en-US browser; the sentence under them is the console's format. **Fix:** Keep (the one widget every keyboard and reader agrees on), as /bcp/report does. **Command:** /impeccable clarify
- **[P3] "conflicto" is a legacy `Badge`.** It is the old destructive badge rather than the kit's pill. **Fix:** Leave for the shared badge retirement. **Command:** /impeccable polish

## Persona Red Flags

**Alex.** Edits a date and the URL follows; one entry, not one per keystroke. No red flag.

**Casey (phone).** The window column scrolls sideways. No other red flag.

Questions skipped: 2 priority issues

## Polish (same day, before `close`)

- Nothing to polish (two P3, left as they are).
- Step 5: harmony on the wave's sixteen routes again, both languages, four viewports, after the polish (the wave's report lists the PNGs).
