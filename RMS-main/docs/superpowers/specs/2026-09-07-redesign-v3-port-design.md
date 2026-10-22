# Redesign v3 port — bringing `itms-prototype-v3.html` into the console

**Date:** 2026-09-07
**Status:** approved design, implementation in progress
**Design authority:** `docs/design/itms-prototype-v3.html` (operator approved
2026-09-07: "ok looks better"). `docs/design/itms-prototype.html` is retired
as a reference; it stays in the tree for history and nothing tests against it.

---

## 0. What this is

The 2026-08-16 UI port (`2026-08-16-ui-port-design.md`) brought the first
prototype into the React app and left three guards behind: `tokens.test.ts`
(the `:root` block is a transcription), `base-styles.test.ts` (element rules
follow the design file) and `radius.test.ts` (the ladder and the type ramp).
That machinery is kept and **re-pointed** at v3. Nothing here proposes a
colour, a radius or a layout: v3 decided those. This document decides how v3
reaches every one of the console's routed pages without losing a function the
console has today, and how that is checked after every deploy rather than
asserted once.

## 1. Non-negotiables

1. **The top bar keeps every function it has today.** Operator's words:
   "make sure that the top bar keeps the functionality that it has today."
   Inventory, from `frontend/src/shell/AppShell.tsx` at `5790884`:
   - nav toggle below 1024px (44×44, `aria-controls="sidebar"`, Escape closes,
     scrim is a button);
   - skip link to `#main`;
   - navigable breadcrumb `ITMS / Section` (`sectionTo`), `aria-current`;
   - the command palette: inline search field, `/` shortcut with the typing
     guard and the hidden-field guard, Escape returns focus to the opener,
     the "Ask the AI Analyst" row gated on `ai.use`;
   - "Install agent" → `/agent-packages`, gated `devices.read`, label visually
     hidden below 640px but present for the accessible name;
   - the health glyph (`HealthGlyph`, exported: the preview renders all four
     states) with the three-carrier state (tint, pip silhouette, label) and
     the count only where the rail is hidden;
   - the platform-status glyph, its own gate and destination;
   - AI (`ai.use`) and documentation (`docs.read`) icon links;
   - the account menu;
   - the session-expiry banner (`role="status"`, not a dialog, no redirect);
   - `document.title` = `${title} · ITMS`, restored on unmount;
   - collapsed-rail preference persisted under `itms.sidebar.collapsed`.
   v3 draws the crumb above the page's `h1` rather than in the bar; that is a
   move, not a removal, and the crumb stays a `<nav aria-label="Breadcrumb">`
   with a real link.
2. **No dead controls.** An icon in the chrome is a promise something is
   behind it. v3's mockup drew a bell; the console has no personal feed, so no
   bell ships. (`AppShell.tsx` records the same rule for "+ New".)
3. **Colour is never the only carrier.** Every severity pill carries its icon
   (`crit` circle-x, `warn` alert-triangle, `ok` circle-check, `run` progress,
   `mut` minus, `info` info-circle). Unmeasured is drawn as absent (`n/a`, no
   arc, no bar), never as a measured zero.
4. **The frontend never gates security** (invariant 6). The fixture mode that
   lets every route render without a backend is `import.meta.env.DEV`-only and
   `preview.test.ts` proves it is absent from the production bundle.
5. **Every migrated page keeps its tests**, adjusted for structure and never
   weakened. A test deleted to make a page green is a defect.
6. **Harmony is measured.** A script visits every routed page in a real
   browser at two viewports and fails on any computed font-size, radius or
   colour outside the token allowlist, on horizontal overflow, and on serious
   axe violations. It runs in CI on every push, and its screenshots are what
   the review wave reads.

## 2. The token layer

`frontend/src/index.css`'s first `:root` block becomes v3's, name for name
(`--bg`, `--surface`, `--ink`, `--muted`, `--line`, `--rail*`, `--accent*`,
`--sev-*`, `--t-*`, `--r-*`, `--shadow`, `--mono`, `--sans`). `tokens.test.ts`
compares it with v3's block; v3's block is reformatted to one declaration per
line (values unchanged) because that is what the parser reads.

**Three deliberate edits to the design file, made first, then transcribed:**
- `--line-control: #828895` is added. v3 draws input borders in `--line-2`
  (`#cfd4dc`, 1.5:1 on white); SC 1.4.11 wants 3:1 on the boundary of anything
  you can operate, and `contrast.test.ts` measures it.
- `--rail-dim` is lightened until it clears 4.5:1 on `--rail` — it paints the
  10.5px uppercase group captions, which are text.
- `--sans` starts with `Inter`, which the bundle self-hosts (`fonts.css`); the
  system stack stays as the fallback.
Any further pair that fails a measured floor is fixed the same way: design
first, note in the file, then the app.

A **second** `:root` block carries legacy aliases (`--canvas: var(--bg)`,
`--red: var(--sev-crit)`, `--gold-ink: var(--sev-warn)`, `--primary: var(--accent)`,
`--action: var(--accent)`, …) so the ~1,900 utility references across the
modules resolve to v3 colours on day one. Magenta is retired: v3 has one
accent, and "azure informs, magenta commits" is struck from `DESIGN.md`.

The type ramp is `22 / 15 / 13.5 / 12 / 10.5` with `26` reserved for the KPI
figure. Tailwind's `text-xs..2xl` are remapped onto it so the 933 `text-sm` /
`text-xs` utilities in the modules land on the scale. The radius ladder is
`6 / 10 / 999`; `rounded*` utilities map onto it.

## 3. The shell

Rail 232px on `--rail`, group captions in `--rail-dim`, items with Tabler
icons, right-aligned mono counts (`.ct`, `.hot` for the alert count), collapse
button, tenant card. Top bar 60px: nav toggle, search-first palette (flex 1,
max 520px), Install agent, health and platform glyphs as 36px icon buttons
with the pip, AI and docs links, account menu. Session banner below it.
`.content` is `22px 24px 40px`, max-width 1440. The breadcrumb renders as the
first line of `.content`, above the page's own `PageHead`.

## 4. The component layer

Restyle in place, then extend. Every existing export keeps its name and props
(`PageHead`, `Btn`, `StatsGrid`/`StatCard`, `Tabs`, `Panel`, `StatusPill`,
`Progress`, `TableWrap`, `QueryState`, `CellMain`, `FilterBar`, `Table`/`Th`/
`Td`, `Dialog`, `Field`, `EmptyState`, `EntityPicker`, charts) so 165 files
change look without changing code, then pages adopt the new pieces where v3
shows them: `Tile` (with `Sparkline`), `Donut`, `Heat`, `Timeline`,
`WeekPainter`, `Steps`, `Facts`, `Avatar`/`Person`, `OsIcon`, `Chips`,
`Toolbar`, `KV`. `StatusPill` gains the severity kind and its icon. Tables
keep `<table>` semantics and take v3's list-row look (caption headers,
hairlines, hover, optional severity gutter and lead icon).

Icons are Tabler (MIT), outline, stroke 1.75, embedded as path data in
`icons.tsx`; every icon name in use today stays valid as an alias.

Cron fields (`patch` maintenance window, `deploy` window, `backup` window)
get the `WeekPainter`: a 7×24 grid that emits the 5-field expression the
server's `window_open` parses, with the raw expression kept in an "Advanced"
disclosure so nothing expressible today stops being expressible.

## 5. Fixture mode and the harmony gate

`?__fixtures` (DEV only, remembered in `sessionStorage`) swaps the `ApiClient`
for a typed proxy answering from `src/fixtures/core.ts` plus one
`src/modules/<m>/fixtures.ts` per module, and treats the session as signed in
with every permission. A missing fixture rejects with `FixtureMissing` and is
recorded on `window.__fixtureMisses`, so a page renders its error state
rather than a blank and the gate can name what is missing.

`scripts/harmony.mjs` starts Vite, walks `scripts/routes.mjs` (every
`<Route path>` in `App.tsx`, with sample ids; `routes-covered.test.ts` fails
when a route is added without being listed), and at 1440×900 and 390×844:
collects computed `font-size`, `border-radius`, `color`, `background-color`,
`border-color` on every element and diffs against the allowlist; checks
`scrollWidth`; runs axe; reads fixture misses; screenshots to
`frontend/.harmony/`. Exit 1 on any stray, overflow, serious/critical axe
violation, or (with `--strict-fixtures`) any miss. `scripts/harmony.mjs
--proto` screenshots the same routes from the v3 prototype for side-by-side
review. CI runs it as the `harmony` job and uploads the screenshots.

## 6. The review wave

After every implementation wave, and after every deploy of this branch, a
panel of reviewer agents reads the harmony screenshots against the prototype
screenshots for every route and files findings (route, severity, what
deviates, which function is missing). Fixers act on the findings by module;
routes with findings are re-reviewed. The wave ends only when every route is
clean, so the operator never receives a half-done console.

## 7. Out of scope

Dark mode (v3 is light-only, like its predecessor). Backend changes of any
kind. A notification feed (the bell). Copy rewrites of existing pages beyond
what a migrated component requires.
