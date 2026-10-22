---
name: ITMS
description: One control plane for a managed fleet, a dark instrument rail carrying a bright, dense workspace.
colors:
  rail: "#0f1a20"
  rail-lifted: "#182730"
  rail-ink: "#9db0b8"
  rail-dim: "#7f939c"
  rail-hot: "#ff8d9a"
  page: "#f5f6f9"
  surface: "#ffffff"
  surface-tinted: "#f9fafc"
  ink: "#141619"
  ink-secondary: "#3b4048"
  muted: "#606a7a"
  hairline: "#e5e8ee"
  hairline-strong: "#cfd4dc"
  control-grey: "#828895"
  accent: "#1f5fd1"
  accent-wash: "#e8effd"
  accent-ink: "#ffffff"
  sev-crit: "#c1182f"
  sev-crit-wash: "#fdecee"
  sev-warn: "#9a6100"
  sev-warn-wash: "#fdf3e2"
  sev-ok: "#17703f"
  sev-ok-wash: "#e6f6ed"
  sev-run: "#0f5f95"
  sev-run-wash: "#e7f1fa"
  sev-mut: "#606a7a"
  sev-mut-wash: "#eef0f4"
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
    fontWeight: 400
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
  control: 6px
  panel: 10px
  pill: 999px
spacing:
  rail: 232px
  topbar: 60px
  content: 22px 24px 40px
  content-max: 1440px
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.accent-ink}"
    borderRadius: "{rounded.control}"
  button-secondary:
    backgroundColor: "{colors.surface}"
    borderColor: "{colors.control-grey}"
    textColor: "{colors.ink}"
    borderRadius: "{rounded.control}"
  input-field:
    backgroundColor: "{colors.surface}"
    borderColor: "{colors.control-grey}"
    borderRadius: "{rounded.control}"
  panel:
    backgroundColor: "{colors.surface}"
    borderColor: "{colors.hairline}"
    borderRadius: "{rounded.panel}"
  nav-item-active:
    backgroundColor: "{colors.rail-lifted}"
    textColor: "#ffffff"
    borderColor: "{colors.accent}"
---

# Design System: ITMS

> **The tokens, ratios and rules below are extracted from the approved design
> and the shipped code, and are facts.** The design authority is
> `docs/design/itms-prototype-v3.html` (operator approved 2026-09-07). The
> earlier prototype and the palette this document used to describe are retired;
> `index.css` keeps their names as aliases so nothing paints an undefined
> variable while the modules migrate, and nothing tests against them.

## Overview

**Creative North Star: "The Instrument Rail"**

A dark spine runs down the left of every screen and never changes. Everything
to its right is bright, white, and dense with fact. The rail is the instrument;
the workspace is the readout. That division is the whole idea: an operator
glances left to know *where* they are and looks right to read *what is true*,
and the two never compete for attention.

The workspace is deliberately busy. This is a console for people who manage
hundreds of endpoints, and the density is a feature: list rows, tiles, severity
pills and bars packed onto a cool white page. What keeps density from becoming
noise is that colour is rationed. Most of a screen is near-black text on white
with hairline rules. Colour appears only where it carries meaning: a severity,
or the one accent.

v3 simplified the previous language rather than replacing its idea. There is
now **one accent** where there were two, **five severities** where there were
nine hues, **five type sizes** where there were three ramps, and **three radii**
where there were six. Every page is composed from one kit, and a browser-side
harmony gate measures every routed page against the token allowlist so a page
cannot have a stray style because it cannot write one.

**Key Characteristics:**
- A permanent dark rail (232px) against a cool-white workspace
- One accent, which is the only thing on the page that is not a severity or a grey
- Five severities, each with a wash, and an icon on every pill so colour is never the only carrier
- Hairline-and-wash depth; one soft shadow
- Every ratio computed in CI, never asserted in prose; every page measured in a browser

## Colors

Cool and high-contrast: a near-black rail, a paper-white working surface, and one
saturated accent.

### The accent
- **Accent** (`#1f5fd1`): The only non-severity colour on the page. Links, the
  primary button, focus rings, the active tab's underline, the active rail
  item's edge, the checked state of a checkbox, the info pill. Its wash
  (`#e8effd`) carries the same meaning at lower volume; its ink on a fill is
  white (`#ffffff`, 5.81:1). On white it measures 5.81:1 as text.

### The rail
- **Rail** (`#0f1a20`) / **Rail lifted** (`#182730`): The rail's ground, and the
  ground a rail item lifts to on hover and when active. Both are grounds; text
  on the rail owes its ratio to both.
- **Rail ink** (`#9db0b8`): Rail items. 7.85:1 on the rail, 6.81:1 lifted.
- **Rail dim** (`#7f939c`): The 10.5px uppercase group captions and the mono
  counts. 5.51:1 on the rail, 4.78:1 lifted. It arrived in v3 as `#6d838c`
  (4.44:1) and was lightened in the design file first.
- **Rail hot** (`#ff8d9a`): The alert count in the rail. 8.01:1.

### Neutrals
- **Ink** (`#141619`): Primary text. 18.13:1 on white.
- **Ink secondary** (`#3b4048`): Cell values, chips, icon buttons. 10.43:1.
- **Muted** (`#606a7a`): Captions, sub-lines, placeholders, breadcrumbs. It is
  graded on every wash it can land on: 5.47:1 on white, 5.06:1 on the page,
  4.79:1 on the crit wash (a tile's sub-line), 4.74:1 on the accent wash. v3
  shipped it at `#667080`, which measured 4.39:1 on the crit and mut washes,
  and it was darkened in the design file first.
- **Page** (`#f5f6f9`): The ground behind the panels.
- **Surface** (`#ffffff`): Every panel, tile, row and field.
- **Surface tinted** (`#f9fafc`): A hovered row.
- **Hairline** (`#e5e8ee`) / **Hairline strong** (`#cfd4dc`): Decorative
  separation only: row rules, panel edges, the tab strip's underline, the list
  header's rule. 1.23:1 and 1.49:1 on white, which is fine, because they
  identify nothing.
- **Control grey** (`#828895`): The boundary of anything you can operate: every
  button, input, select, chip, and the toggle's track. 3.56:1 on white, 3.41:1
  tinted, 3.29:1 on the page. v3 drew those in hairline strong (1.49:1) and
  gained this token in the design file first.

### Severities
Five, each an ink with a wash, and each with an icon. A hue is applied only
when it *means* something.

| Kind | Icon | Ink | Wash | Ink on wash | Ink on white |
| --- | --- | --- | --- | --- | --- |
| crit | circle-x | `#c1182f` | `#fdecee` | 5.35:1 | 6.11:1 |
| warn | alert-triangle | `#9a6100` | `#fdf3e2` | 4.68:1 | 5.14:1 |
| ok | circle-check | `#17703f` | `#e6f6ed` | 5.48:1 | 6.13:1 |
| run | progress | `#0f5f95` | `#e7f1fa` | 5.94:1 | 6.79:1 |
| mut | minus | `#606a7a` | `#eef0f4` | 4.79:1 | 5.47:1 |
| info | info-circle | the accent | the accent wash | 5.03:1 | 5.81:1 |

The avatar palette (`--av-1..6`) is six saturated fills carrying white initials
at 5.10:1 or better; it is a palette for telling people apart, not a status.

### Named Rules

**The One Accent Rule.** There is one accent and it is the only colour on the
page that does not mean a severity. A control that navigates, reveals or
commits is the accent; nothing else is. The previous "azure informs, magenta
commits" rule is retired with the magenta: v3 has no second accent, and the
`--action*` aliases resolve to the one accent.

**The Rationed Hue Rule.** A colour on this page must be carrying information.
If a hue could be swapped for grey without losing meaning, it should be grey.

**The Second Carrier Rule.** Colour is never the only carrier. Every severity
pill carries its icon; a tile's kind is carried by its wash *and* its label; the
active rail item is carried by white text and the lifted ground, not only by its
accent edge (which is 2.64:1 on the lifted ground and would fail alone).

**The Two Grounds Rule.** No accent is safe on both the rail and the page. The
accent is 5.81:1 on white and 3.04:1 on the rail; the rail tones are the mirror
image. On the rail the accent is an edge, never text.

**The Absent Zero Rule.** Unmeasured is drawn as absent: `n/a`, no arc, no bar,
a hollow pip. Never as a measured `0`, and never in the ok colour.

## Typography

**One family, Inter, self-hosted, at two weights.** v3 draws everything at 400,
500 or 600; there is no 700 or heavier anywhere in the design, and the Tailwind
weight utilities above `medium` resolve to 600. Inter is self-hosted in the
bundle (`frontend/src/fonts.css`), never fetched from a CDN: this platform runs
on networks with no egress and `docs/product.md` §5 commits that it does not
phone home.

### The ramp

Five sizes, and one named exception.

- **H1** (`--t-h1`, 22px, 600, -0.015em): The page title. One per page.
- **H2** (`--t-h2`, 15px, 600): A panel's heading, the brand, a donut's figure.
- **Body** (`--t-body`, 13.5px): Everything read: rows, cells, buttons, fields,
  the page subtitle, the rail's items.
- **Small** (`--t-sm`, 12px): Sub-lines, pills, breadcrumbs, mono values in a
  cell, the rail's counts, code.
- **Caption** (`--t-cap`, 10.5px, uppercase, 0.1em): Tile labels (`.k`), list
  headers (`.lhead`), the rail's group captions (`.grp`), fact labels. The only
  uppercase text in the product, and it is uppercased by CSS, never typed.
- **KPI** (`--t-kpi`, 26px, 600, -0.03em): The figure on a tile. The one size
  outside the five-step scale, named so the harmony gate can allow it and the
  type utilities can reach it.

Tailwind's `text-xs..2xl` resolve to Small / Body / Body / H2 / H1 / KPI, so a
module written with utilities lands on the ramp by construction. The earlier
sign-in and article ramps are not part of v3: the sign-in page lives in the
Keycloak theme, outside this bundle, and rendered documentation must be
expressed with these rungs (an H1 at 22, an H2 at 15, prose at Body, code at
Small) or the harmony gate reports the stray.

### Named Rules

**The Floor Is The Ramp's Smallest Step.** Nothing renders below `--t-cap`,
10.5px, not in a stylesheet and not inline in JSX; CI enforces both halves. The
caption is legible at that size only because every caption pair is graded at
4.5:1, which is why the contrast work and the size floor are one finding. v3
itself shipped a 9px label (`.wk-h`) and it was corrected in the design file.

**The Cascade Is The Component.** A class name in JSX is not a component; the
rule it selects is. `confirm.tsx` rendered `className="modal open"` for an
entire programme while **no `.modal` rule existed**; twelve jsdom tests passed on
it because jsdom has no cascade. That is what the browser passes exist for.

**The Label Leads Rule.** On a tile the caption sits *above* the figure. The
number is bigger, but the word is what makes it mean anything.

## Layout

A fixed 232px rail, a 60px top bar, and a fluid workspace padded `22px 24px 40px`
and capped at 1440px. Tiles sit on a 12-column grid (`span 3` by default, `span 6`
below 1100px, full width below 640px); panels stack or pair (`.g2`).

Breakpoints, in the order they bite: **1300px** (tile grids relax), **1024px**
(the rail becomes a drawer, removed from the tab order when closed; the alert
count leaves the top bar because the rail badge already says it), **640px**
(tiles go full width, content padding tightens). The breadcrumb renders above
the page's `h1`, as the first line of the content, and stays a
`<nav aria-label="Breadcrumb">` with a real link.

Density is high and deliberate. Panels sit 16px apart; tiles are 92px tall
minimum; list rows are compact with a severity gutter down the left when a row
has one. Touch targets reach 44px wherever the pointer is coarse.

## Elevation & Depth

**Almost flat, and layered by hairline rather than by shadow.** Depth comes from
three things in order: a white surface against the page, a 1px hairline around
it, and one soft shadow. There is no elevation scale; a panel and a tile sit at
the same optical height.

### Shadow Vocabulary
- **The shadow** (`0 1px 2px rgba(15,26,32,.05), 0 6px 20px rgba(15,26,32,.06)`):
  The resting state of every panel and tile, and the only shadow. It separates
  white from white.

### Named Rules

**The Flat-Until-Touched Rule.** Surfaces rest almost flat. A hover changes the
ground (a row lifts to the tinted surface) or the border, and a hover that
changes only a border must step it at least 2:1 so the eye registers it.

## Shapes

Three radii, and the ladder shrinks toward the thing you touch: **panels 10px**
(`--r-panel`: panels, tiles, facts, steps), **controls 6px** (`--r-ctl`:
buttons, fields, icon buttons, tabs' content), **pills 999px** (`--r-pill`:
pills, chips, bars, the toggle). The only other radii in the product are the
2px and 3px of the heat and week cells and the 50% of avatars and pips.

Borders are always exactly 1px. The system distinguishes them by *colour*, not
weight: a hairline closes a box, control grey says you can operate it.

## Components

### Buttons
- **Shape:** 6px radius, 13.5px/500, `9px 13px` padding.
- **Primary (`.pri`):** accent fill, white text (5.81:1), accent border.
- **Secondary (default):** white, 1px control grey, ink text.
- **Ghost:** transparent, no border; the accent as text.
- **Danger:** the crit ink as its edge and text.
- **Focus:** 2px accent outline at a positive offset. The offset is
  load-bearing: with one accent, a ring drawn against the primary button's own
  fill is accent on accent, 1:1. The offset puts it on the page.
- **Disabled:** one inert surface, `cursor: not-allowed`, and exempt from the
  boundary floor by 1.4.11's own carve-out.
- **One primary per page BODY**, and the chrome's "Install agent" is the
  standing exception. v3 draws that control `.btn.pri` in the top bar, and it
  is the one action the whole product is a promise about: the operator's own
  report was that they could not find how to install an agent. Because it
  lives in the chrome and not in the page, it does not compete with a page's
  own primary for the reader's first glance -- the two are never on the same
  rung. Nothing else in the chrome is primary. (Ruled 2026-09-08, closing the
  v3 port; before it the console drew the control secondary and
  `AppShell.test.tsx` held it there, which was a deviation from the design
  recorded for this wave to settle.)

### Tiles
- 10px radius, 92px minimum, `.k` caption over a 26px `.v` figure with an
  optional unit, a `.z` sub-line, an optional sparkline bottom-right. `kind`
  colours the whole tile and only `crit` and `warn` ever do; `ok` colours the
  figure only. A tile with a destination is a link.

### Panels
- White, 1px hairline, 10px radius, the shadow, `overflow: hidden`. A head
  with an H2 and a muted sub-line, actions right; a body at `16px 18px` or
  bare for a list.

### List rows
- `<table>` semantics, v3's look: caption headers on a strong hairline, rows
  separated by hairlines, the tinted surface on hover, an optional severity
  gutter and lead icon, a `.ttl` cell with a bold title over a muted sub-line.

### Pills
- 12px/500 on a 999px wash, `3px 9px 3px 7px`, with the kind's icon first.
  Six kinds: crit, warn, ok, run, mut, info.

### Inputs / Fields
- 36px minimum, 6px radius, white, 1px control grey. Placeholder in muted, typed
  text in ink. Focus shifts the border to the accent.
- **Checkbox / radio:** native, tinted by `accent-color: var(--accent)`.
  Deliberately not reimplemented: authoring a border switches Blink and WebKit
  out of native appearance and takes the checkmark with it, and SC 1.4.11
  exempts UA-drawn appearance the author has not modified.
- **Toggle:** a 999px track (control grey off, accent on) with a white knob.
- **Week painter:** a 7×24 grid of cells for a maintenance window, emitting the
  cron expression the server parses, with the raw expression in an "Advanced"
  disclosure so nothing expressible today stops being expressible.

### Callouts
One component, `.notice`, in the severity tones: a bordered box on the kind's
wash carrying the kind's ink, every pair graded at 4.5:1. It takes Body and
reads as a sentence, one level below the panel heading above it.

### Dialogs
- Centred, 90vw capped, 10px radius, the shadow; `.modal` alone is hidden and
  `.modal.open` is what shows it. Focus enters on open, is trapped while open,
  returns to the trigger on close; Escape closes; the page behind is
  scroll-locked.

### Navigation
- **Rail:** 232px on the rail ground; 10.5px uppercase group captions in rail
  dim; items at Body in rail ink with a Tabler icon at 70% and a right-aligned
  mono count (`.ct`, `.hot` for the alert count).
- **Active:** white text on the lifted ground with a 2px accent left edge.
- **Mobile:** below 1024px the rail becomes a drawer, removed from the tab order
  when closed.

### Icons
Tabler, outline, stroke 1.75, `fill: none`, from the sprite; never a hand-drawn
path. Every icon name in use before the port stays valid as an alias.

### The top bar
60px, white, a hairline below. A search-first command palette (flex 1, max
520px) that is an ARIA combobox rather than a dialog; "Install agent"; the
health and platform glyphs as 36px icon buttons with a 7px pip, each state a
different tint *and* a different silhouette (the unknown state's pip is
hollow); AI and documentation links; the account menu. No bell: the console
has no personal feed, and an icon in the chrome is a promise something is
behind it.

## Do's and Don'ts

### Do:
- **Do** edit `docs/design/itms-prototype-v3.html` **first**. `tokens.test.ts` requires its `:root` to match the first `:root` of `frontend/src/index.css` exactly; the design file is the authority and the console is the transcription. When a value must change, change it there, leave a comment beside it naming the old value, the new value and the measured ratio, then transcribe.
- **Do** compute every contrast ratio with the helper in `frontend/src/test/contrast.test.ts` and add the pair to the table that matches its floor: 4.5:1 for text, 3:1 for a control boundary or state.
- **Do** name the v3 token (`--sev-crit`, `--muted`, `--r-ctl`) in new code. The legacy aliases (`--red`, `--muted-2`, `--radius`) exist so old code keeps resolving; a module is finished migrating when it reads none of them.
- **Do** give every pill its icon and every unmeasured value its `n/a`.
- **Do** give every tile, KPI and figure a destination. A number an operator cannot click is a number they cannot act on.
- **Do** run `node scripts/harmony.mjs` on the routes you touched. It is the browser's opinion of whether the page is on the allowlist, and it is the one the review wave reads.

### Don't:
- **Don't** paint `--line` or `--line-2` as a control boundary. They measure 1.23:1 and 1.49:1; the criterion asks 3:1 and the boundary scan in `contrast.test.ts` grades every rule that names a control.
- **Don't** introduce a second accent, or reach for the retired magenta, violet, gold or teal by name. They alias to the one accent or to a severity, and a hue that is not a severity means nothing here.
- **Don't** write a font size or a radius as a literal. 14, 16, 18 and 20px are not on the ramp; 8, 12, 14 and 16px are not on the ladder; the harmony gate reports each as a stray.
- **Don't** type uppercase copy or an em dash into UI text. Captions are uppercased by CSS; sentence case everywhere else.
- **Don't** ship a dead control in the chrome. No bell, no "+ New".
- **Don't** reference an undefined token with a fallback (`var(--brand, #6d4aff)`). Two of those shipped, both silently painting a retired colour for months.
- **Don't** widen a guard to make a colour pass. Change the colour, in the design file, first.
