---
name: Novieri RMM
description: One console for a managed fleet, a night-blue rail beside a cool, exact workspace.
colors:
  bg: "#f3f5f8"
  surface: "#ffffff"
  surface-2: "#f7f9fc"
  ink: "#111a2b"
  muted: "#536077"
  line: "#e1e6ee"
  line-control: "#7b8599"
  rail: "#101b30"
  rail-2: "#1b2945"
  rail-ink: "#a9b6cc"
  rail-dim: "#8592ab"
  rail-mark: "#8fa6ff"
  accent: "#2747c9"
  accent-2: "#203cac"
  accent-3: "#1a3190"
  accent-s: "#e9edfc"
  accent-ink: "#ffffff"
  sev-crit: "#b8172c"
  sev-crit-s: "#fcebed"
  sev-warn: "#8d5a00"
  sev-warn-s: "#fcf1dc"
  sev-ok: "#17703f"
  sev-ok-s: "#e5f4ea"
  sev-mut: "#586274"
  sev-mut-s: "#edf0f4"
  av-1: "#5b3fb0"
  av-2: "#0f5f95"
  av-3: "#17703f"
  av-4: "#9a6100"
  av-5: "#b03a5b"
  av-6: "#0f7a80"
  scrim: "rgba(16,27,48,.38)"
  hatch-ink: "rgba(255,255,255,.55)"
typography:
  h1:
    fontFamily: Inter
    fontSize: 22px
    fontWeight: 600
    letterSpacing: "-0.015em"
  h2:
    fontFamily: Inter
    fontSize: 15px
    fontWeight: 600
  body:
    fontFamily: Inter
    fontSize: 13.5px
    fontWeight: 400
    lineHeight: "1.5"
  small:
    fontFamily: Inter
    fontSize: 12px
    fontWeight: 400
  caption:
    fontFamily: Inter
    fontSize: 10.5px
    fontWeight: 500
    letterSpacing: "0.1em"
  kpi:
    fontFamily: Inter
    fontSize: 26px
    fontWeight: 600
    letterSpacing: "-0.03em"
  code:
    fontFamily: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace
    fontSize: 12px
    fontWeight: 400
rounded:
  r-badge: 4px
  r-ctl: 6px
  r-panel: 10px
  r-pill: 999px
spacing:
  rail: 232px
  rail-collapsed: 76px
  topbar: 60px
  content: 22px 24px 40px
  content-max: 1440px
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.accent-ink}"
    rounded: "{rounded.r-ctl}"
    height: 36px
  button-primary-hover:
    backgroundColor: "{colors.accent-2}"
  button-primary-pressed:
    backgroundColor: "{colors.accent-3}"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.r-ctl}"
    height: 36px
  input-field:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.r-ctl}"
    height: 36px
  chip:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.r-ctl}"
    height: 32px
  chip-selected:
    backgroundColor: "{colors.accent-s}"
    textColor: "{colors.accent}"
  badge:
    rounded: "{rounded.r-badge}"
    height: 24px
  panel:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.r-panel}"
  nav-item-active:
    backgroundColor: "{colors.rail-2}"
    textColor: "{colors.accent-ink}"
    rounded: "{rounded.r-ctl}"
  save-bar:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
---

# Design System: Novieri RMM

> **Version 4, "Novieri" (2026-11-23).** The tokens in the frontmatter are
> normative and named after the CSS custom properties that carry them
> (`accent` is `--accent`). `frontend/src/index.css`'s first `:root` is their
> transcription and `frontend/src/test/tokens.test.ts` fails on any
> difference, in either direction, and on any brand value from
> `docs/design/marca/novieri-tokens.json` that does not reach the console
> exactly. A rule the brand forces to change is changed here first, with its
> reason, and then in the tokens and the tests. The v3 prototype
> (`docs/design/itms-prototype-v3.html`, 2026-09-07) is history.

## Overview

**Creative North Star: "The Survey Plan"**

The brand the operator chose is "Cordillera": the sobriety of a topographic
plan. A night-blue rail runs down the left of every screen and carries the
legend, the places you can go, in the order a technician walks them. Everything
to its right is the plan itself: a cool white workspace, exact and dense, where
lines are thin and colour appears only where it means something. The symbol
is an N drawn as two peaks that also read as a monitoring pulse, and the
wordmark is built from straight strokes and arcs, flat ends, no serifs.

The organisation is borrowed on purpose. The rail, the per-client pages, the
filtered lists, the tabbed records and the "New" menu follow Atera, the
category's leader, so a technician who has used one console knows where things
are in this one. What is not borrowed is the look, and what this system
competes on is consistency, which is where the reference is weakest: one way to
save, every view addressable by URL, a unique tab title on every page, and an
empty state that says its real cause.

Density is a feature. This is a console for people who manage hundreds of
endpoints and keep it open all day; rows, chips and badges are packed onto the
page, and what keeps that from becoming noise is that colour is rationed.

**Key Characteristics:**
- A night-blue rail (232px, 76px folded) against a cool white workspace.
- One cobalt accent, which is the only colour that is not a severity or a grey.
- Five severities, each with a wash and an icon; information shares the accent.
- Badges at 4px, chips and controls at 6px, panels at 10px: nothing is a pill
  except the switch and the progress bar.
- Every ratio computed in CI and every page measured in a browser.

## Colors

Cool and exact: a night-blue rail, a white working surface on a pale blue-grey
ground, and one cobalt accent.

### Primary
- **Cobalt** (`#2747c9`, `--accent`): the primary button, links, the focus ring,
  the active tab's underline, the selected chip, the checked checkbox, the info
  badge, the symbol. 7.42:1 on white, 6.79:1 on the page.
- **Cobalt deep** (`#203cac`, `--accent-2`): a primary button on hover.
  **Cobalt pressed** (`#1a3190`, `--accent-3`): pressed.
- **Cobalt wash** (`#e9edfc`, `--accent-s`): the selected chip, the info badge.
  The accent on it is 6.35:1.

### Rail
- **Night blue** (`#101b30`, `--rail`) and **Night blue lifted** (`#1b2945`,
  `--rail-2`): the rail's ground, and the ground an item lifts to on hover and
  when it is the current page.
- **Rail text** (`#a9b6cc`, `--rail-ink`): items, 8.39:1 and 7.07:1 lifted.
- **Rail dim** (`#8592ab`, `--rail-dim`): counts, 5.48:1 and 4.62:1 lifted.
- **Rail mark** (`#8fa6ff`, `--rail-mark`): the current item's icon and dot,
  6.25:1 on the lifted ground.

### Neutral
- **Ink** (`#111a2b`, `--ink`): primary text, 17.41:1 on white.
- **Muted** (`#536077`, `--muted`): secondary text, captions, placeholders,
  breadcrumbs; 6.35:1 on white, 5.81:1 on the page, at least 5.44:1 on every
  wash.
- **Page** (`#f3f5f8`, `--bg`), **Surface** (`#ffffff`, `--surface`),
  **Surface hover** (`#f7f9fc`, `--surface-2`).
- **Line** (`#e1e6ee`, `--line`): decorative separation only, 1.25:1.
- **Control line** (`#7b8599`, `--line-control`): the boundary of anything you
  can operate, 3.71:1 on white and 3.40:1 on the page.

### Severities

| Kind | Icon | Ink | Wash | Ink on wash | Ink on white |
| --- | --- | --- | --- | --- | --- |
| crit | circle-x | `#b8172c` | `#fcebed` | 5.71:1 | 6.57:1 |
| warn | alert-triangle | `#8d5a00` | `#fcf1dc` | 5.22:1 | 5.84:1 |
| ok | circle-check | `#17703f` | `#e5f4ea` | 5.39:1 | 6.13:1 |
| mut (unknown) | minus | `#586274` | `#edf0f4` | 5.38:1 | 6.15:1 |
| info | info-circle | the accent | the accent wash | 6.35:1 | 7.42:1 |
| run | progress | the accent | the accent wash | 6.35:1 | 7.42:1 |

"Running" is not a sixth colour: it is information's hue with its own icon,
because the brand has one blue (the operator confirmed the merge on
2026-11-24). `--sev-run` is an alias of `--accent` for the code that still
names it. Where the two are drawn without a badge, the difference is carried
by something else, and `frontend/src/test/second-carrier.test.tsx` holds it:
a chart's second series is the unknown grey, not a second cobalt; a heat cell
for information is a ring (a cobalt core in a band of the surface), running a
solid cell; a donut's label carries its kind's icon; a timeline row carries
its kind's icon already.

The avatar palette (`--av-1..6`) tells people apart; it is not a status, and
white initials clear 5.10:1 on every fill.

### Named Rules

**The One Accent Rule.** Cobalt is the only colour on the page that does not
mean a severity. A control that navigates, reveals or commits is cobalt;
nothing else is.

**The Rationed Hue Rule.** If a hue could be swapped for grey without losing
meaning, it is grey.

**The Second Carrier Rule.** Colour is never the only carrier: every badge
carries its icon, the current rail item its lifted ground and white text.

**The Two Grounds Rule.** Cobalt measures 2.32:1 on the rail and is never drawn
there; the rail has its own mark, `--rail-mark`.

**The Absent Zero Rule.** Unmeasured is drawn as absent: `n/a`, no bar, a
hollow pip, a chip with no count. Never as `0`.

## Typography

**Inter, self-hosted, at 400, 500 and 600.** Nothing is fetched from a CDN:
the platform does not phone home and many deployments have no egress.

### Hierarchy
- **H1** (`--t-h1`, 22px, 600, -0.015em): the page title, one per page.
- **H2** (`--t-h2`, 15px, 600): a panel or section heading.
- **Body** (`--t-body`, 13.5px, 400, 1.5): everything read, the rail's items,
  buttons, fields, cells.
- **Small** (`--t-sm`, 12px): sub-lines, badges, counts, breadcrumbs, code.
- **Caption** (`--t-cap`, 10.5px, 500, uppercase by CSS, 0.1em): table headers,
  tile labels, fact labels. The only uppercase text, never typed.
- **KPI** (`--t-kpi`, 26px, 600, -0.03em): the figure on a tile.

Tailwind's `text-xs..2xl` resolve to Small, Body, Body, H2, H1 and KPI.

### Named Rules

**The Floor Is The Ramp's Smallest Step.** Nothing renders below 10.5px.

**The Label Leads Rule.** A tile's caption sits above its figure.

## Layout

A 232px rail (76px folded, icons only), a 60px top bar and a fluid workspace
padded `22px 24px 40px`, capped at 1440px; where the Assistant's round button
is drawn the workspace keeps 88px at its foot instead of 40px, so the last
line of a page scrolls clear of it. Breakpoints: **1300px** (tile grids
relax), **1024px** (the rail becomes a drawer, out of the tab order while
shut), **640px** (the client selector moves into the drawer, tiles go full
width). The breadcrumb is the first line of the content. Touch targets reach
44px on coarse pointers.

## Elevation & Depth

Almost flat. Depth is a white surface on the page, a 1px line around it and one
soft shadow, `0 1px 2px rgba(16,27,48,.05), 0 6px 20px rgba(16,27,48,.06)`,
tinted by the rail's own blue. There is no elevation scale: a menu, a dialog
and a panel wear the same shadow, and a dialog is set apart by the scrim.

**The Flat-Until-Touched Rule.** A hover changes the ground or the border; a
border-only hover steps at least 2:1.

## Shapes

The ladder shrinks toward the thing you read: **panels 10px** (`--r-panel`),
**controls and chips 6px** (`--r-ctl`), **badges 4px** (`--r-badge`). The
switch's track and the progress bar keep `--r-pill`, because their silhouette
is how they are recognised; nothing else is round. Avatars and pips are 50%;
heat cells 2px and 3px. Borders are always 1px, told apart by colour: a line
closes a box, the control line says it can be operated.

## Components

### Buttons
- 36px high, 6px radius, 13.5px/500, `0 13px`.
- **Primary:** cobalt fill, white text; deep on hover, pressed on press.
- **Secondary:** white, control line, ink text.
- **Ghost:** no edge, cobalt text. **Danger:** critical edge and text.
- **Focus:** 2px cobalt outline at a 2px offset, which is what keeps the ring
  off the primary button's own fill.
- **One primary per page body.** In the chrome, the "New" menu is the one
  primary control (it replaced "Add a computer" on 2026-11-23).

### Chips (filters)
- 32px high, 6px radius, control line, ink text at 13.5px/500, an optional
  count at 12px in the muted ink. **Selected:** cobalt wash, cobalt edge and
  text, and the count in cobalt. A filter chip is a toggle (`aria-pressed`) and
  its state lives in the URL.

### Badges (status)
- 24px high, 4px radius, 12px/500, the kind's wash and ink, the kind's icon
  first. Six kinds: crit, warn, ok, mut, info, run.

### Inputs / Fields
- 36px, 6px radius, white, control line; focus moves the border to cobalt.
  Help text sits outside the label (`aria-describedby`). Checkboxes and radios
  are native, tinted by `accent-color`.
- **Switch:** a pill track (control line off, cobalt on) and a white knob.

### Panels
- White, 1px line, 10px radius, the shadow. A head with an H2, a muted line
  and actions; a body at `16px 18px`, or bare around a list.

### Lists
- Page head (title, one sentence, the primary action), then one bare panel:
  search, filter chips with counts, the client chip when every client is
  shown, "Showing x of y" at the end of the toolbar; the bulk bar only where
  bulk actions exist; the table; the pager. An empty list says whether nothing
  exists yet or nothing matches the filters.

### Records (detail)
- Back, the record's name, its status badge, its context (client and site),
  actions on the right; summary tiles; one row of tabs that change the URL;
  for a device, information on the left and metrics and activity on the right.

### Forms
- Full page, back and title; collapsible sections; one sticky bar, "Unsaved
  changes · Discard · Save", for the whole console. Leaving with unsaved
  changes asks first.

### Dialogs and drawers
- Centred, 10px radius, the shadow, the scrim; focus enters, is trapped and
  returns; Escape closes.

### Navigation
- **Rail, in Atera's order, with no group captions:** Dashboard, Tickets,
  Clients, Devices, Alerts, Patching, Software, Backup, Remote sessions, Assets
  (Licenses, Subscriptions, IP addresses), IT documentation, Reports, More
  (Changes, Continuity tests, IT budget, only when on for the chosen client).
  Anchored at the bottom: Assistant, and Admin, which swaps the rail for an
  accordion of its sections with a back button and a search.
- **The Assistant is also a round button at the bottom right of every page**
  (Atera's AI button, `docs/design/referencias-atera/atera 1.png`; the
  operator, 2026-11-24), never in the top bar. See "Assistant button".
- Items at Body in rail text with an outline icon; **current**: lifted ground,
  white text, the icon and a 7px dot in rail mark. Counts in rail dim; the
  alert count is a critical badge with white text.
- Folded: icons only. Below 1024px: a drawer.

### Top bar
- 60px, white, a line below: the "New" menu (only real destinations, each
  shown with its permission), the search (`/`, wider than before: up to
  640px), the client selector, the alert glyph, the platform glyph and the
  account. No bell: there is no inbox behind one. No Assistant icon: the
  Assistant is anchored in the rail and is the round button below.

### Assistant button
- A 48px cobalt circle with the white sparkles glyph, fixed 24px from the
  bottom right of the viewport (16px on a phone), the one shadow; a link to
  `/assistant` named "Ask the Assistant", shown only with `ai.use` and not on
  the Assistant's own page. 48px clears the 44px target on every pointer.
- **It never covers the save bar.** Atera's walkthrough files exactly that
  defect ("el botón flotante de IA tapa el botón de guardar"), so while the
  button is drawn the save bar keeps 72px clear on its right (64px on a
  phone, where the button sits 16px from the edge), wherever the bar is: the
  two cannot share the corner at any width. The content keeps 88px clear at
  its foot, so the button never covers the last row or the last field.

### Brand
- **Wordmark:** "NOVIERI", built from strokes and arcs (`docs/design/marca/
  novieri-wordmark.svg`), white on the rail, ink on light grounds.
- **Symbol:** the zigzag N; on the rail and as the favicon it sits on a 7px
  cobalt tile with a white stroke (`novieri-favicon.svg`, read at 16 and 32px).
- **Lockup:** wordmark, a hairline and "RMM" in the muted ink, for the sign-in
  screens, the portal and emails (`novieri-lockup.svg`).
- **Name:** "Novieri" in the console's bar; "Page · Novieri RMM" in the tab.

### Icons
Tabler, outline, stroke 1.75, from the sprite; never a hand-drawn path except
the brand's own symbol and wordmark.

## Do's and Don'ts

### Do:
- **Do** change a token here first, then in `index.css`, then in the tests.
- **Do** compute every new pair with `frontend/src/test/contrast.test.ts` and
  add it to the table its floor belongs to (4.5:1 text, 3:1 boundary).
- **Do** build every list, record and form from the kit
  (`frontend/src/shell/listKit.tsx`, `detailKit.tsx`, `formKit.tsx`).
- **Do** keep filters, tabs and create dialogs in the URL.
- **Do** run `node scripts/harmony.mjs` on the routes you touched, in both
  languages.

### Don't:
- **Don't** draw cobalt on the rail, or a second accent anywhere.
- **Don't** make a chip or a badge a pill.
- **Don't** write a font size, radius or colour as a literal.
- **Don't** type uppercase copy or an em dash into UI text.
- **Don't** ship a dead control: no bell, no "New" entry without a real
  destination.
- **Don't** draw a second save button where the save bar belongs.
- **Don't** let a floating control cover the save bar or a page's last line.
- **Don't** widen a guard to make a colour pass.
