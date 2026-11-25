---
target: "the shell, Task 4: frontend/src/shell/AppShell.tsx"
total_score: 29
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 1
target_identity: "file:frontend/src/shell/AppShell.tsx"
target_fingerprint: "sha256:962a1e9cd6abab0f0b13540ff195b332128852845a108e71ff21417faa6d1db0"
target_path: frontend/src/shell/AppShell.tsx
timestamp: 2026-11-24T23-59-19Z
slug: frontend-src-shell-appshell-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: the top bar and the Assistant's round button (`frontend/src/shell/AppShell.tsx`, with `NewMenu.tsx`)

Redesign plan, Task 4, Step 12 ("Cierre de cada oleada", steps 1 to 5). The chrome on every page: the New menu,
the search, the client selector, the two live glyphs, the account, and the Assistant's round button (decision
D12). Assessment A was written down before any detector output; then Assessment B (CLI scan, the harmony
gate's axe pass, the live overlay). Captures: `/dashboard`, `/devices`, `/licenses`, `/audit`, `/assistant` at
1440 and 390, both languages.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Health and platform glyphs with their shapes; the alert count where the rail is a shut drawer |
| 2 | Match System / Real World | 3 | "New ▾" with the six things a technician creates; "Ask the Assistant" |
| 3 | User Control and Freedom | 3 | Escape closes the menu and gives focus back; the round button is a plain link |
| 4 | Consistency and Standards | 3 | New is the bar's one primary; its list reuses the account menu's items |
| 5 | Error Prevention | 3 | Only destinations the person may use; a bare `?new=` opens nothing for a reader |
| 6 | Recognition Rather Than Recall | 3 | The round button has no visible word; its name and tooltip say it |
| 7 | Flexibility and Efficiency | 3 | New from every page; `/` search; the menu opens on its first item and arrows move |
| 8 | Aesthetic and Minimalist Design | 3 | The bar is lighter without the Assistant's icon and the install button |
| 9 | Error Recovery | 3 | Nothing here fails on its own; the search's empty state says what it covers |
| 10 | Help and Documentation | 2 | No help entry in the bar (Atera has "?"); none is built |
| **Total** | | **29/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** Atera's bar order with Novieri's tokens: New on the left in cobalt, a search that grew to
640px, the two glyphs that carry their state in three ways, the account. The round button is Atera's AI button
in Novieri's cobalt, with what Atera's lacks: it never takes the save bar's corner, and the page keeps 88px at
its foot. Specific to this product.

**Deterministic scan.** `impeccable detect --json frontend/src/shell`: exit 0, no findings. The harmony gate's
axe pass, both languages: no serious violation, but a **moderate `region` finding on every console route at
every viewport (424 captures in English): `.assistant-fab` is outside every landmark.** The LLM review missed
it; the button is drawn after `</main>`, at the shell's top level.

**Visual overlays.** `detect.js` was injected through Chromium (Playwright, a fresh page per case on this worktree's own `npx vite` with `?__fixtures`, title marked `[Human]`; `impeccable live-server --background` on 8400, stopped with `--keep-inject`; no project file changed; no person's browser was attached, so no overlay is visible to anyone). Cases: `/dashboard` en 1440 (and again with the New menu open), `/licenses` en 1440, `/audit` es 1440, `/admin` es 1440 and en 390, `/assistant` en 1440, `/dashboard` es 390. Nothing on the top bar, the New button, its open menu or the round button: the same `gray-on-color` (the rail's tenant line, a graded pair) and `overused-font` as on every page, and the page bodies' own findings.

## Overall Impression

The bar now does one thing per control and New is a real menu. The round button works and keeps out of the
save bar's way; it is the one thing a screen-reader user navigating by landmark cannot find.

## What's Working

- New offers only what the person may create, each entry the page's own create dialog (`?new=`), and draws
  nothing at all when there is nothing to create: no dead control.
- The round button is a 48px link with its name, hidden on `/assistant`, and `body.assistant-fab-on` gives the
  save bar 72px (64 on a phone) on its right: Atera's own defect (walkthrough, line 639) designed out.
- On a phone the bar keeps every control: New collapses to its "+" with its name kept.

## Priority Issues

- **[P1] The round button is outside every landmark.** axe `region`, moderate, on every console page (424
  captures per language). A person moving by landmarks (main, navigation, banner) never meets it; it is also
  noise that would hide the next moderate finding a wave introduces. **Fix:** draw it as the last thing in
  `<main>`: it stays fixed in the corner, tab order puts it after the page, and no new landmark name repeats
  the button's. **Command:** /impeccable harden
- **[P2] The New menu stays open when focus leaves it by Tab.** A WAI-ARIA menu button closes its menu on Tab
  and lets focus move on; here the list stays hanging over the search field. **Fix:** close on Tab, without
  taking focus back. **Command:** /impeccable polish
- **[P2] The round button floats over content in the corner.** Mid-page, it covers whatever sits under it
  (devices' Architecture column, licenses' Renews); the 88px foot lets the last row clear it. Inherent to a
  floating button, and the operator chose Atera's (D12). **Fix:** none. **Command:** /impeccable layout
- **[P3] Adding a computer is two clicks from the bar.** Atera keeps "Install agent" as a button of its own;
  design §1.2 puts it in New and on the Computers page head instead. **Fix:** none. **Command:** /impeccable clarify

## Persona Red Flags

**Sam (keyboard, screen reader).** New: `aria-haspopup="menu"`, opens on its first item, arrows, Home/End,
Escape back to the button; Tab leaves the menu open (P2 above). The round button: named, 48px, but unreachable
by landmark (P1 above).

**Casey (phone).** At 390 the bar is the drawer toggle, "+", the search glyph, the two glyphs and the avatar;
the round button sits 16px from the corner and does not cover the save bar.

**Alex (power user).** New from anywhere in two keystrokes once focused; `/` opens search.

## Minor Observations

- On a phone the "+" alone reads as "add", not as a menu; its name ("New") is kept for assistive technology.
- The round button's tooltip repeats its name (a `title` for the pointer).

## Questions to Consider

Answered by the coordinator's sources and recorded in `.impeccable/critica-respuestas.md`, "the shell, Task 4":

1. The round button's landmark: (a) the last thing in `<main>`; (b) a labelled landmark of its own; (c) leave
   the moderate finding. **(a)**.
2. The New menu and Tab: (a) close on Tab and let focus move on; (b) keep it open. **(a)**.
3. Adding a computer from the bar: (a) New → Computer and the Computers page head; (b) a button of its own, as
   Atera's "Install agent". **(a)**: design §1.2 says so in as many words.

## Polish (same day, before `close`)

- P1 fixed: the round button is the last thing in `<main>`; the five shell routes re-measured in both
  languages carry no axe finding at all (`region` gone). Test in `AppShell.test.tsx`.
- P2 fixed: the New menu's items are not tab stops (arrows move between them) and the menu closes when focus
  leaves it; test in `NewMenu.test.tsx`.
- `/__preview` passes the six New entries, so the browser pass measures the bar's primary as it measured
  "Add a computer".
- P2 left: the round button covers what sits under the corner mid-page (D12, the operator's choice).
- P3 left: adding a computer is New → Computer (design §1.2).
