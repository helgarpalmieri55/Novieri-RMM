---
target: "Settings, wave 7 (7.1): frontend/src/modules/settings/SettingsPage.tsx and its twelve tabs"
total_score: 29
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 2
target_identity: "file:frontend/src/modules/settings/SettingsPage.tsx"
target_fingerprint: "sha256:25dbb89de967ba3c75ec2fb3a4ade82533bb7838915a7fc1351730260f33d89d"
target_path: frontend/src/modules/settings/SettingsPage.tsx
timestamp: 2026-11-25T06-40-33Z
slug: frontend-src-modules-settings-settingspage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Settings (`frontend/src/modules/settings/SettingsPage.tsx`, `/settings` and its twelve `?tab=` panels)

Redesign plan, Task 12 (wave O7), «Cierre de cada oleada», step 3. Assessment A was written from the page source and the harmony captures (es and en, 1440x900 and 390x844) before any detector output; Assessment B followed.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | The bar says Unsaved / Saving / Saved; a tab's set-up state is in its label |
| 2 | Match System / Real World | 3 | Settings words, not wire keys; «Set up» in the tab is plain |
| 3 | User Control and Freedom | 3 | Discard, and switching tab with a change asks first (D16) |
| 4 | Consistency and Standards | 3 | Nine tabs, one bar; Notifications and Users save in their own dialogs by design |
| 5 | Error Prevention | 3 | Input to correct is said on submit in the bar, never a dead Save |
| 6 | Recognition Rather Than Recall | 3 | Twelve tabs, each named; the strip scrolls |
| 7 | Flexibility and Efficiency | 2 | No keyboard path between tabs beyond the strip; long forms have no index |
| 8 | Aesthetic and Minimalist Design | 3 | Sections fold; intro sentences are short |
| 9 | Error Recovery | 3 | Server errors in the bar, worded by the kernel |
| 10 | Help and Documentation | 3 | Field help under each control |
| **Total** | | **29/40** | **Good** |

## Design Specificity Verdict

**LLM assessment**: Authored for this product: the tabs are the operator's own configuration surfaces (email relay, sign-in lifetimes, Microsoft sign-in, the roles matrix) and every one of them saves the same way. Category-interchangeable risk: a long horizontal strip of twelve tabs is the generic admin pattern; it is redeemed by the URL (a tab is a link) and by the set-up state in the label.

**Deterministic scan** (Assessment B, run from the worktree root so `.impeccable/config.json` applies):
`impeccable detect --json frontend/src/modules/{settings,profile,ai-admin,ai-automation}` → 0 findings in all four
modules. Browser layer: `npx vite` (own port) + `impeccable live-server --background`, Chromium through Playwright,
a fresh context per page on `<route>?__fixtures`, `detect.js` injected, console read; es and en × 1440x900 and
390x844 × the wave's 15 routes; stopped with `live-server stop --keep-inject` and the dev server killed (`pgrep`
empty). System-wide findings only, not the page's own: `gray-on-color #8592ab on #1b2945` (the rail's muted ink,
brand tokens, DESIGN.md), `overused-font inter` (the brand's one face), `undersized-ui-text 10.5px` on table
headers (the kit's `Th` caption rung). No user-visible overlay is claimed: the overlay ran in a headless tab.


## What's Working

- One way to save across nine tabs (the kit's bar), with switching tab guarded because the tab is the form (D16).
- A person who cannot edit reads why before touching a field (the notice), and never meets a disabled Save.
- Users is now a real list: search and status in the URL, an invitation with its own address (`?new=user`).

## Priority Issues

- **[P1] On a phone the selected tab scrolled out of view when a label grew**
  - Why it matters: `/settings?tab=sessions` at 390 opened with Sessions in view, then the Email label gained « · Set up» from its own query and pushed Sessions off the strip (harmony capture). The kit's `Tabs` re-centres only when the selection changes.
  - Fix: Repeat the kit's sideways scroll when the labels change (page-level layout effect), and ask the kit to do it (kit request K3).
  - Suggested command: /impeccable polish

- **[P1] The Users status chips and the rows' pills used different words**
  - Why it matters: «Desactivadas» above rows whose pill says «deshabilitado»; «Invited» above «Invitation pending». Two words for one state on one screen.
  - Fix: Name each chip with the row pill's word: Active, Invitation pending, Disabled, Deleted / Activo, Invitación pendiente, Deshabilitado, Eliminado.
  - Suggested command: /impeccable polish

- **[P2] Long hints run the full width of a section**
  - Why it matters: The Email tab's test hint is one ~1,100px line at 1440; notes elsewhere are capped near 70ch.
  - Fix: Cap long notes and hints at the measure the kit's notices use (76ch).
  - Suggested command: /impeccable polish

- **[P2] Twelve tabs overflow the strip at 1440 with no edge cue**
  - Why it matters: «Niveles de s…» is cut at the right edge; nothing says there is more.
  - Fix: Kit: a fade or a chevron at a scrolling strip's overflowing edge (not this page's to draw).
  - Suggested command: /impeccable polish

## Persona Red Flags

**Alex (power user)**: switches between Email and Users often; the URL keeps the tab, and Back now undoes a tab change (it used to leave Settings). **Jordan (first-timer)**: the «· Not set up» state on Email tells them where to start; the read-only notice says which permission they lack.

## Minor Observations

Stragglers at 390 in the page description («página.») and the Microsoft notes; the Users table at 390 keeps very tall rows because the actions column (hidden off the right edge) holds the opened disclosure.

## Questions to Consider

- Should a configured section say anything at all in its tab («Email · Set up»), or only the unconfigured one? The tests pin both today.
