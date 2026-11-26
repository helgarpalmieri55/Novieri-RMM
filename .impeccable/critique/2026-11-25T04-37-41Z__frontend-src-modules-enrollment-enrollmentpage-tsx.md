---
target: "wave O4, Enrollment: frontend/src/modules/enrollment/EnrollmentPage.tsx"
total_score: 30
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/enrollment/EnrollmentPage.tsx"
target_fingerprint: "sha256:6a0550079d1d6144d37db2fdda85cfd43fad5ecf983754cce03138faa4a764a8"
target_path: frontend/src/modules/enrollment/EnrollmentPage.tsx
timestamp: 2026-11-25T04-37-41Z
slug: frontend-src-modules-enrollment-enrollmentpage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Enrollment (`frontend/src/modules/enrollment/EnrollmentPage.tsx`, `/enrollment`)

Redesign plan, Task 9 (wave O4), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (both languages, 1440x900 and 390x844) before any detector output; Assessment B followed. No sub-agent tool is exposed in this session, so A and B ran in series in one context.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Counted toggles, the issue flow's steps |
| 2 | Match System / Real World | 3 | "Fully used", "Expired" in words |
| 3 | User Control and Freedom | 3 | Tabs, chips, search and the form in the URL |
| 4 | Consistency and Standards | 3 | UrlTabs and chips from the kit |
| 5 | Error Prevention | 4 | Three-step issue; no unlimited token can be minted |
| 6 | Recognition Rather Than Recall | 3 | Each chip names the count it hides |
| 7 | Flexibility and Efficiency | 3 | Every view a link |
| 8 | Aesthetic and Minimalist Design | 2 | The head sentence (93 in Spanish) wraps "equipos." alone |
| 9 | Error Recovery | 3 | Revocation failures say it was NOT revoked |
| 10 | Help and Documentation | 3 | The flow tab explains the enrollment invariants |
| **Total** | | **30/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** The most security-loaded page of the wave, and it keeps every one of its guards: the three-step issue, the plaintext shown once from component state, revocation that hides and never deletes. The kit made its toggles links; the chips keep the sentences that state what they hide.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/enrollment` from the worktree root: exit 0,
`[]`. Harmony (`--strict-fixtures`, both languages, 1440x900, 390x844, 390x844-client, 641x900-client): 0 strays,
no overflow, the top bar in bounds, no serious axe violation, no fixture miss.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh context per route on this
worktree's own `npx vite` with `?__fixtures`, title marked `[Human]`; `impeccable live-server --background` on
8400, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is
visible to anyone). Run at en 1440x900 and es 390x844. On every route of the wave the same three come back
and are not this page's: `undersized-ui-text` on the 10.5px table headers, tile labels and fact captions
(DESIGN.md's Caption step and PRODUCT.md's floor: a false positive, as the client list's critique already
recorded), `gray-on-color` on the rail's tenant card and `overused-font` (Inter), both the shell's.
On this page: nothing beyond those three; the long head sentence and the missing count were read from the captures and the recipe (Assessment A).

## Overall Impression

Strong. Two kit gaps: no "Showing x of y" and a head sentence too long for its line.

## What's Working

- `/enrollment?tab=certificates&revoked=1` reloads in that view.
- The issue form opens from a link onto the Tokens tab whichever tab the link names.

## Priority Issues

- **[P2] No "Showing x of y".** Receta L puts the count at the end of the toolbar; the tokens list has the org's total (`total`) and the certificates list the devices read. **Fix:** `ShowingCount` at the end of each filter bar: tokens over `total`, certificates over the devices read. **Command:** /impeccable polish
- **[P2] The head sentence is too long.** 93 characters in Spanish, "equipos." alone at 1440. **Fix:** "The tokens that enroll computers, and the certificates they get." / "Los tokens que inscriben equipos y los certificados que reciben." **Command:** /impeccable clarify
- **[P3] Four tiles over three tabs.** The tiles are counts of what the tabs list. **Fix:** None now. **Command:** /impeccable distill

## Persona Red Flags

**Riley.** Opens `?new=token` from another tab: the form appears on Tokens, and leaving the tab drops it. No red flag.

**Sam.** Chips are toggles in named groups; the tabs are a tablist with a named panel. No red flag.

## Minor Observations

- None.

## Questions to Consider

1. **The count:** (a) "Showing x of y" at the end of each tab's filter bar; (b) none, the pager says the total.
2. **The head sentence:** (a) "The tokens that enroll computers, and the certificates they get." / "Los tokens que inscriben equipos y los certificados que reciben."; (b) keep it.
3. **The four tiles:** (a) keep them; (b) drop the two the tabs already count.

All three are settled by the sources and answered in `.impeccable/critica-respuestas.md` (wave O4).
