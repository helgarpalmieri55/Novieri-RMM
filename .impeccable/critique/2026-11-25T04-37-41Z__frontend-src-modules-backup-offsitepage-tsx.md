---
target: "wave O4, Off-site copy: frontend/src/modules/backup/OffsitePage.tsx"
total_score: 28
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:/home/user/rmm-w4/frontend/src/modules/backup/OffsitePage.tsx"
target_fingerprint: "sha256:fabd27b85577976a164fc0c30b9ecbd3df74f3f6b3cbb40c914fc6fd3617b1f0"
target_path: /home/user/rmm-w4/frontend/src/modules/backup/OffsitePage.tsx
timestamp: 2026-11-25T04-37-41Z
slug: frontend-src-modules-backup-offsitepage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Off-site copy (`frontend/src/modules/backup/OffsitePage.tsx`, `/backup/offsite`)

Redesign plan, Task 9 (wave O4), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (both languages, 1440x900 and 390x844) before any detector output; Assessment B followed. No sub-agent tool is exposed in this session, so A and B ran in series in one context.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Each storage's state, last test and its steps; the save bar's states |
| 2 | Match System / Real World | 2 | A test step printed by its key ("reach") in the fixture data |
| 3 | User Control and Freedom | 3 | Edit in `?edit=`, Close and leaving ask with a change; Discard |
| 4 | Consistency and Standards | 3 | Receta FO for the edit, one primary in the head |
| 5 | Error Prevention | 3 | Rules refuse before sending; the secret never enters a message |
| 6 | Recognition Rather Than Recall | 3 | Keys are help-texted "leave blank to keep the current one" |
| 7 | Flexibility and Efficiency | 3 | One storage edited on the page instead of a 13-field dialog |
| 8 | Aesthetic and Minimalist Design | 2 | At 390 the head sentence runs to three lines, "borrarla." alone on the last |
| 9 | Error Recovery | 3 | The bar names the problem; a failed save keeps the change |
| 10 | Help and Documentation | 3 | The intro says how to make the bucket before adding it |
| **Total** | | **28/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** The page that decides whether a lost server can be recovered, and it reads like it: the key first, then the storage, then which repositories go there. Moving the edit out of a dialog into a section with the console's one save bar is the change that matters: thirteen fields in a modal was the wrong container.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/backup` from the worktree root: exit 0,
`[]`. Harmony (`--strict-fixtures`, both languages, 1440x900, 390x844, 390x844-client, 641x900-client): 0 strays,
no overflow, the top bar in bounds, no serious axe violation, no fixture miss.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh context per route on this
worktree's own `npx vite` with `?__fixtures`, title marked `[Human]`; `impeccable live-server --background` on
8400, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is
visible to anyone). Run at en 1440x900 and es 390x844. On every route of the wave the same three come back
and are not this page's: `undersized-ui-text` on the 10.5px table headers, tile labels and fact captions
(DESIGN.md's Caption step and PRODUCT.md's floor: a false positive, as the client list's critique already
recorded), `gray-on-color` on the rail's tenant card and `overused-font` (Inter), both the shell's.
On this page: `straggler`, "borrarla." alone on the last of three lines of the head sentence (es 390), which agrees with heuristic 8.

## Overall Impression

Good structure; the head sentence is too long for a phone, and the fixture shows a raw step key. The editor cannot be seen in the harmony captures because the fixture session does not hold `backup.offsite.manage`.

## What's Working

- One storage at a time on the page, with one bar and one guard: no second save button anywhere.
- The keys stay out of the draft that is compared with the server, so the secret is never serialised for a diff.

## Priority Issues

- **[P2] The head sentence is 79 characters (es 104).** At 390 it wraps to three lines with "borrarla." alone (the detector's `straggler`). **Fix:** "A second, locked copy that this server cannot delete." / "Una segunda copia bloqueada, que este servidor no puede borrar." **Command:** /impeccable clarify
- **[P2] A test step printed by its key.** The fixture's first step is `reach`, a key the server never writes (`offsite.py` records `connect`), so the row reads "✓ reach". **Fix:** Fixture fix: `connect` (`modules/backup/fixtures.ts`). **Command:** /impeccable harden
- **[P3] The editor is not in any capture.** Harmony's session lacks `backup.offsite.manage`, so the section and its bar are only measured by the tests. **Fix:** A kit request for a harmony variant with the code; recorded in the wave report. **Command:** /impeccable audit

## Persona Red Flags

**Riley (stress tester).** Types one key and saves: the bar says both keys or neither, and sends nothing. No red flag.

**Sam.** The section's title is a button with `aria-expanded`; the bar is a named region. No red flag.

**Casey.** The head sentence takes three lines.

## Minor Observations

- "Set up the recovery key" is a plain button now: the head's Add off-site storage is the page's one primary.

## Questions to Consider

1. **The head sentence:** (a) "A second, locked copy that this server cannot delete." / "Una segunda copia bloqueada, que este servidor no puede borrar."; (b) keep the long one; (c) move the long one under the storage panel's title.
2. **The editor no capture shows:** (a) ask the coordinator for a harmony variant whose session holds `backup.offsite.manage`; (b) rely on the tests; (c) give the fixture session the code.
3. **The raw step key:** (a) fix the fixture to the server's `connect`; (b) also word unknown keys generically; (c) leave it.

All three are settled by the sources and answered in `.impeccable/critica-respuestas.md` (wave O4).
