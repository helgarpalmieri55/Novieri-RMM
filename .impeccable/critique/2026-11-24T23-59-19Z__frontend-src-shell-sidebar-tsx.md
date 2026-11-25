---
target: "the shell, Task 4: frontend/src/shell/Sidebar.tsx"
total_score: 28
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 1
target_identity: "file:frontend/src/shell/Sidebar.tsx"
target_fingerprint: "sha256:633dd2db37e82ee35ff9d13077905b3d1d54adbc61c417d179264f2a117f86a1"
target_path: frontend/src/shell/Sidebar.tsx
timestamp: 2026-11-24T23-59-19Z
slug: frontend-src-shell-sidebar-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: the rail (`frontend/src/shell/Sidebar.tsx`, with `AdminPanel.tsx`)

Redesign plan, Task 4, Step 12 ("Cierre de cada oleada", steps 1 to 5). Read as what it is: the console's
navigation on every page, an Operate surface. Assessment A (design review) was written down before any
detector output (`scratchpad/assessA.md`); then Assessment B (CLI scan, the harmony gate's axe pass and the
live overlay in Chromium). Captures: `/dashboard`, `/licenses`, `/audit`, `/admin`, `/assistant` at 1440
(en, es) and 390 (en, es).

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | The current page is lifted, its icon and a 7px dot in the rail mark; the folder holding it opens; Admin is lit on its pages. The Admin panel's section can stay shut on the current page |
| 2 | Match System / Real World | 3 | Task names, Atera's order; "More" says nothing of what it holds (Atera's own word) |
| 3 | User Control and Freedom | 3 | "Back to the menu" keeps the page; folders open and close in place; the fold is remembered |
| 4 | Consistency and Standards | 2 | One item shape for a link, a folder and an accordion row; in Spanish the Admin panel's names are cut where the main rail's are whole |
| 5 | Error Prevention | 3 | Nothing destructive; every link keeps its server-side code |
| 6 | Recognition Rather Than Recall | 3 | Labels and icons; folded icons carry their names; leaving Admin needs Back first |
| 7 | Flexibility and Efficiency | 3 | The panel's search ignores case and accents; `/` search reaches every page |
| 8 | Aesthetic and Minimalist Design | 3 | Calm night blue and one mark; the current item can end in a count and a dot |
| 9 | Error Recovery | 3 | "No Admin page matches that." with the field's own clear |
| 10 | Help and Documentation | 2 | No line in the rail says what a page is for; the hints are only on `/admin` |
| **Total** | | **28/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** Atera's organisation in Novieri's world, which is what A1 asked for. The order, the two
folders, the anchored foot and the Admin swap are Atera's; the look is not: a night-blue rail with one mark
(the current icon and a 7px dot in `--rail-mark`), no cobalt edge on a ground where cobalt measures 2.32:1,
the critical badge for the alert count, the 76px fold. Nothing here would read as another product's rail.

**Deterministic scan.** `impeccable detect --json frontend/src/shell` from the worktree root: exit 0, no
findings (also each of `Sidebar.tsx`, `AdminPanel.tsx`, `AppShell.tsx`, `NewMenu.tsx`, `AreaPage.tsx` alone).
The harmony gate's axe pass (both languages, 117 routes, four viewports) found nothing serious in the rail.

**Visual overlays.** `detect.js` was injected through Chromium (Playwright, a fresh page per case on this worktree's own `npx vite` with `?__fixtures`, title marked `[Human]`; `impeccable live-server --background` on 8400, stopped with `--keep-inject`; no project file changed; no person's browser was attached, so no overlay is visible to anyone). Cases: `/dashboard` en 1440 (and again with the New menu open), `/licenses` en 1440, `/audit` es 1440, `/admin` es 1440 and en 390, `/assistant` en 1440, `/dashboard` es 390. On the rail: one `gray-on-color` on every page, the tenant card's plan line (`--rail-dim` #8592ab on `--rail-2` #1b2945), a pair DESIGN.md grades at 4.62:1 and `contrast.test.ts` holds above 4.5:1: a false positive by the rule's heuristic, and the card predates this task. `overused-font` (Inter) is already ignored in `.impeccable/config.json`; the live layer reports it anyway. Nothing on the nav items, the folders, the dot or the Admin panel. The rest of each page's findings (`undersized-ui-text` on 10.5px captions, `nested-cards` on `dl.facts`, stragglers) are in page bodies the waves own.

**Browser captures (harmony, Spanish).** Not a detector finding, and the one the English captures hide: at the rail's 232px, the Admin panel's pages are cut in Spanish ("Registro de aud…", "Registros de segur…", "Portal del usuario fi…"), the current page's name among them.

## Overall Impression

The rail reads at a glance and the Admin swap works as Atera's does, with a way back that keeps the page. Two
things undermine it, both in the Admin panel: in Spanish it cuts the names of most of its pages, and once open
it can forget which section holds the page you are on.

## What's Working

- One mark for "you are here", the same in the main rail, a folder and the Admin panel, and never colour
  alone: lifted ground, white text, the icon and the dot.
- The folder that holds the current page opens by itself, so a page in Assets is never a click away; folded,
  its pages show as icons under the folder.
- "Back to the menu" swaps the rail back without leaving the page, and the panel opens by itself on an Admin
  page: the swap never strands anyone.

## Priority Issues

- **[P1] In Spanish the Admin panel cuts most page names, the current one included.** A folder's or Admin's page
  starts its label 77px in (the 10px inset, the 40px indent, the icon and its gap), and "Registro de auditoría",
  "Registros de seguridad" or "Automatización con IA" do not fit what is left; on `/audit` the page you are on
  reads "Registro de aud…". The operator works in Spanish. **Fix:** let a folder's or Admin's page name take a
  second line instead of an ellipsis; the 232px rail and the 40px indent are DESIGN.md's and the mock's.
  **Command:** /impeccable adapt
- **[P2] The Admin panel's open section does not follow the current page.** `AdminPanel` takes its open
  section once, when it mounts. Reaching another Admin page from outside the panel while it stays mounted (a
  search result, an `/admin` card, a link on a page) leaves that page's section shut, so the current page and
  its dot are hidden. Atera opens the active group (walkthrough, line 343). **Fix:** open the section that
  holds the current page whenever it changes. **Command:** /impeccable polish
- **[P2] The panel title "Admin" is a link that does not look like one.** White, bold, no hover; it goes to
  the `/admin` index, but nothing tells the pointer so. **Fix:** an underline on hover, as the breadcrumb's
  links have. **Command:** /impeccable polish
- **[P3] The section rows count pages with the rail's count style.** "Service desk 4" uses the same `.ct` the
  rail uses for live work (12 alerts, 2 computers to review). Atera shows the same per-group page count
  (walkthrough, line 44) and the approved design follows its panel. **Fix:** none now. **Command:** /impeccable clarify

## Persona Red Flags

**Alex (power user).** In Admin, reaching Devices takes Back and a click (Atera's pattern); `/` search is the
shortcut. With the panel open on `/audit`, a jump to `/notifications` from the search leaves Service desk shut.

**Sam (keyboard, screen reader).** Folder and section rows are buttons with `aria-expanded` and
`aria-controls`; the current page is `aria-current="page"`; the dot is `aria-hidden`; the two navigations are
named ("Main", "Assistant and Admin"). No red flag beyond the hidden current page above.

**Technician on a phone (project persona, PRODUCT.md).** The rail is a drawer below 1024px; the Admin swap
happens inside it without navigating, so the drawer stays open.

## Minor Observations

- The current item can end in a count and a dot ("Devices 2 •"); both are earned, but they crowd the end.
- The panel's search is a white field on the night-blue rail, the only light control there.
- On `/admin` the panel opens its first section (no page is current there), repeating the first card.

## Questions to Consider

Answered by the coordinator's sources (DESIGN.md, the approved design, the operator's decisions) and recorded in
`.impeccable/critica-respuestas.md`, "the shell, Task 4":

1. Spanish names cut in the Admin panel: (a) let the name take a second line; (b) narrow the 40px indent;
   (c) widen the rail. **(a)**: DESIGN.md fixes the rail at 232px and the mock the indent at 40px.
2. The panel's open section on arriving at another Admin page: (a) keep what the person opened; (b) open the
   section that holds the current page. **(b)**: D10 and Atera's own panel (walkthrough, line 343).
3. The panel title's affordance: (a) as it is; (b) underline on hover, as the breadcrumb's links do; (c) a rail
   item of its own. **(b)**.

## Polish (same day, before `close`)

- P1 fixed: a folder's or Admin's page name takes a second line instead of an ellipsis (`.nav-item.sub .lbl
  { white-space: normal; }`); in Spanish "Registro de auditoría", "Registros de seguridad" and "Portal del
  usuario final" read whole (harmony, es, `/audit` and `/admin` at 1440). Guard: `responsive.test.ts`, "the
  rail's sub-items".
- P2 fixed: the Admin panel opens the section that holds the current page whenever it changes
  (`AdminPanel.tsx`); test in `AdminPanel.test.tsx`.
- P2 fixed: the panel title underlines on hover, as the breadcrumb's links do.
- P3 left: the section rows' page counts (Atera's own, walkthrough line 44).
