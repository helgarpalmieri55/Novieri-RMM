# Redesign v3 Port Implementation Plan

> **For agentic workers:** executed multi-agent (Workflow). Phase A tasks run
> as sequenced agents on shared files; Phase B fans out one agent per module
> group, each confined to its own directories; Phase C is the review wave.
> Agents do NOT commit — the controller commits per task from the reported
> file list.

**Goal:** Every routed page of the console renders in the approved v3 design
from one component kit, with no function lost, and a browser-measured
harmony gate in CI that fails on any page outside the design.

**Architecture:** Re-point the existing token/base-style/radius guards at
`docs/design/itms-prototype-v3.html`; restyle the existing kit in place and
add v3's new pieces; migrate pages by module; add a DEV-only fixture mode so
every route renders headless; add `scripts/harmony.mjs` as the gate.

**Tech Stack:** React 19, TypeScript strict, Vite 8, Tailwind v4, vitest 4,
Playwright (global install, `/opt/node22/lib/node_modules/playwright`,
browsers in `/opt/pw-browsers`; never run `playwright install`), axe-core.

## Global Constraints

- Design authority: `docs/design/itms-prototype-v3.html`. Edit the design
  file FIRST when a value must change; then transcribe. Never the reverse.
- Tokens (v3 `:root`, exact): `--t-h1:22px --t-h2:15px --t-body:13.5px
  --t-sm:12px --t-cap:10.5px --r-ctl:6px --r-panel:10px --r-pill:999px
  --bg:#f5f6f9 --surface:#ffffff --surface-2:#f9fafc --ink:#141619
  --ink-2:#3b4048 --muted:#667080 --line:#e5e8ee --line-2:#cfd4dc
  --rail:#0f1a20 --rail-2:#182730 --rail-ink:#9db0b8 --rail-dim:#6d838c
  --accent:#1f5fd1 --accent-s:#e8effd --accent-ink:#ffffff
  --sev-crit:#c1182f --sev-crit-s:#fdecee --sev-warn:#9a6100
  --sev-warn-s:#fdf3e2 --sev-ok:#17703f --sev-ok-s:#e6f6ed
  --sev-run:#0f5f95 --sev-run-s:#e7f1fa --sev-mut:#667080 --sev-mut-s:#eef0f4
  --shadow:0 1px 2px rgba(15,26,32,.05),0 6px 20px rgba(15,26,32,.06)`.
  Plus the three design-file edits in the spec §2 (`--line-control:#828895`,
  `--rail-dim` lightened to ≥4.5:1 on `--rail`, `Inter` first in `--sans`).
- KPI figure size `26px` is the one size outside the five-step scale.
- Radius ladder: control 6px, panel 10px, pill 999px. Nothing else, except
  `2px`/`3px` heat cells and `50%` avatars/pips.
- Shell: rail 232px, top bar 60px, content padding `22px 24px 40px`,
  max-width 1440px, drawer below 1024px (unchanged breakpoint).
- Icons: Tabler outline, stroke 1.75, `fill:none`, from the sprite; no
  hand-drawn paths. MIT notice kept in `icons.tsx`.
- Severity kinds and icons: crit/circle-x, warn/alert-triangle,
  ok/circle-check, run/progress, mut/minus, info/info-circle. Colour is never
  the only carrier. Unmeasured renders `n/a`/absent, never `0`.
- No dead controls in the chrome (no bell, no "+ New").
- No em dashes in new UI copy; sentence case; no ALL-CAPS except `.k`/`.grp`
  captions which are CSS-uppercased.
- Frontend never gates security. Fixture mode is `import.meta.env.DEV`-only.
- Tests are adjusted for structure, never weakened or deleted.
- Agents run TARGETED tests (`npx vitest run <paths>`), never the full suite
  and never `npm run build`, unless the brief says so. They do not commit and
  do not `git add`; they report the exact file list they touched.
- Agents edit only the files in their ownership list.

---

## Phase A — foundation (shared files)

### Task A1: Tokens, guards, design contract

**Files:**
- Modify: `docs/design/itms-prototype-v3.html` (`:root` reformatted one
  declaration per line; the three edits from spec §2; any pair that fails a
  measured floor, with a comment naming the ratio)
- Modify: `frontend/src/index.css` (first `:root` = v3; second `:root` =
  legacy aliases; `@theme inline` remapped)
- Modify: `frontend/src/test/tokens.test.ts` (PROTOTYPE → v3 file)
- Modify: `frontend/src/test/radius.test.ts` (LADDER `--r-ctl:6px
  --r-panel:10px --r-pill:999px`; SCALE `--radius-sm/md → --r-ctl`,
  `--radius-lg/xl/2xl → --r-panel`, `--radius-full → --r-pill`; RAMP
  `--t-h1..--t-cap` + `--t-kpi:26px`; TYPE_SCALE `--text-xs → --t-sm`,
  `--text-sm → --t-body`, `--text-base → --t-body`, `--text-lg → --t-h2`,
  `--text-xl → --t-h1`, `--text-2xl → --t-kpi`; the "no 18/20px" check kept)
- Modify: `frontend/src/test/contrast.test.ts` (tables rewritten against v3
  names: text pairs `--ink`/`--ink-2`/`--muted` on `--bg`/`--surface`/
  `--surface-2`; each `--sev-*` on its `--sev-*-s` and on `--surface`;
  `--accent-ink` on `--accent`; `--rail-ink` and `--rail-dim` on `--rail`;
  `--accent` on `--surface` and `--bg` (links); NON_TEXT: `--line-control`
  on the three surfaces, `--accent` focus ring, pip states, checkbox checked
  state; keep every describe's intent, including "type scale floor" at the
  new ramp)
- Modify: `frontend/src/test/jsx-tokens.test.ts` (NOT_FOR_TEXT: `--line`,
  `--line-2`, `--rail-dim` only where measured below 4.5:1 on the surfaces it
  can appear on; the violet check stays)
- Modify: `frontend/src/test/no-dark-variants.test.ts`,
  `frontend/src/test/responsive.test.ts` only if they reference the old
  prototype path or the old `--sidebar-w`/`--topbar-h` values
- Modify: `DESIGN.md` and `.impeccable/design.json` (palette, ramp, ladder,
  the retired Two Voices rule; "The Instrument Rail" north star stands)
- Test: the files above; run `npx vitest run src/test`

**Interfaces:**
- Produces: token names every later task uses verbatim; the alias block
  (`--canvas --surface-2 --sidebar --sidebar-2 --rail-label --rail-eyebrow
  --rail-glow --line-strong --muted-2 --ink-2 --primary --primary-2
  --primary-soft --primary-ink --action --action-2 --action-soft --action-ink
  --violet --violet-soft --violet-ink --gold --gold-2 --gold-soft --gold-ink
  --orange --orange-soft --orange-ink --green --green-soft --green-ink --red
  --red-soft --red-ink --blue --blue-soft --blue-ink --shadow-soft
  --radius-code --radius-field --radius-control --radius-card-inner
  --radius-tile --radius --sidebar-w:232px --topbar-h:60px`) each pointing at
  a v3 token so no existing reference breaks.

- [ ] Step 1: reformat and edit the design file's `:root`; add a CSS comment
      there recording the three edits and each measured ratio.
- [ ] Step 2: rewrite `index.css` `:root`, alias block, `@theme inline`.
- [ ] Step 3: re-point and rewrite the four guard tests; run them; every
      ratio they compute must pass without loosening a floor.
- [ ] Step 4: `DESIGN.md` + `.impeccable/design.json`.
- [ ] Step 5: `npx vitest run src/test src/shell` — report counts.

### Task A2: Tabler icons

**Files:**
- Create: `frontend/src/shell/tabler.ts` (generated from `/tmp/sprite.svg`
  at build time of this task: `export const TABLER: Record<string, string>`
  mapping name → inner SVG markup of the symbol; MIT notice at the top with
  the Tabler copyright line)
- Modify: `frontend/src/shell/icons.tsx` (`Icon` renders `<svg class="ic"
  viewBox="0 0 24 24" width height fill="none" stroke="currentColor"
  stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"
  aria-hidden dangerouslySetInnerHTML>`; `ICONS` becomes aliases: every
  existing name (dashboard, sparkles, devices, health, logs, patch, package,
  remote, backup, image, server, ticket, portal, bell, mail, clock, book,
  changes, bcp, chart, audit, license, subscription, budget, network, jobs,
  certificate, automation, status, settings, search, help, menu, close, plus,
  filter, download, upload, play, pause, check, warning, info, user, users,
  link, shield, lock, unlock, eye, terminal, calendar, globe, building,
  database, activity, more, chevronRight, chevronLeft, chevronDown, and any
  other name `grep -rho 'name="[a-zA-Z]*"' src | sort -u` finds) → a Tabler
  id; `ICON_NAMES` = aliases ∪ Tabler ids)
- Test: `frontend/src/shell/icons.test.tsx` (new: every alias resolves; every
  nav icon resolves; rendered svg has `fill="none"` and stroke attrs; unknown
  name renders nothing and warns once); `nav.test.ts` still passes.

Missing Tabler ids the sprite lacks (e.g. `pause`, `chevron-left`,
`chevron-up`, `alert-circle`) are added from Tabler's MIT source
(`https://unpkg.com/@tabler/icons@3/icons/outline/<name>.svg`); if the
network refuses, draw nothing and list them in the report as blocked.

### Task A3: Shell — rail and top bar, every function kept

**Files:**
- Modify: `frontend/src/shell/shell.css` (rail 232, top bar 60, `.ib` icon
  buttons 36×36 with `.dot`, search-first palette, `.content` padding, crumb
  above content; drawer/collapse/skip/session-notice rules kept and restyled)
- Modify: `frontend/src/shell/AppShell.tsx` (crumb moves to first child of
  `.content`, still `<nav aria-label="Breadcrumb">` with `Link`; everything
  in spec §1.1 kept; no bell)
- Modify: `frontend/src/shell/Sidebar.tsx` (group captions, `.ct` counts,
  `.hot` for `/health`, Tabler icons, tenant card, collapse)
- Modify: `frontend/src/shell/CommandPalette.tsx`, `AccountMenu.tsx`,
  `overlays.css` only for class/visual alignment
- Modify: `frontend/src/shell/AppShell.test.tsx`, `chrome-responsive.test.ts`,
  `overlays.test.tsx`, `authSurface.test.tsx` — structure only
- Test: `npx vitest run src/shell src/App.auth.test.tsx src/App.routing.test.tsx`

**Interfaces:** `AppShellProps` unchanged. `HealthGlyph` still exported.

### Task A4: Component kit — restyle in place, add v3 pieces

**Files:**
- Modify: `frontend/src/shell/components.css`, `components.tsx`,
  `frontend/src/components/ui.tsx`, `emptystate.tsx`, `form.tsx`,
  `confirm.tsx`, `picker.tsx`, `chart.tsx`, `attributes.tsx`,
  `frontend/src/shell/base.css`, `frontend/src/shell/Toaster.tsx`
- Create: `frontend/src/shell/viz.tsx` (`Tile`, `Sparkline`, `Donut`, `Heat`,
  `Timeline`, `Facts`, `KV`, `Steps`, `Avatar`, `Person`, `OsIcon`, `Chips`,
  `Toolbar`), `frontend/src/shell/viz.css`, `frontend/src/shell/viz.test.tsx`
- Create: `frontend/src/components/weekpainter.tsx` + `weekpainter.test.tsx`
  (7×24 grid; `value: string | null` cron; `onChange(cron)`; derives the
  grid from a cron whose minute is `0`, hours a list/range, dom `*`, month
  `*`, dow a list; anything else is shown as "custom expression" with the
  grid disabled and the Advanced field editable; emits `0 <hours> * * <dows>`
  with hours as compact ranges; keyboard: arrow keys move, Space toggles,
  Shift+arrows extend; each cell is a `<button role="gridcell"
  aria-pressed>`; `aria-label` "Tuesday 02:00")
- Modify: `docs/design/itms-prototype-v3.html` to ADD any rule the app needs
  that v3 lacks (`.btn:disabled`, `.modal`/`.modal.open`/scrim, `.notice`
  and tones, `code.code`, `.form-grid`, `.field.full`, `.status` family →
  `.pill` family, `.sr-only`), in v3's vocabulary; then transcribe
- Modify: `frontend/src/test/base-styles.test.ts` (PROTOTYPE → v3; selector
  list = the rules above; the intent of every `it` kept)
- Modify: `frontend/src/preview/PreviewPage.tsx` to render every new piece
  and every `StatusPill` kind (the a11y CI pass reads this page)
- Test: `npx vitest run src/shell src/components src/test/base-styles.test.ts
  src/test/contrast.test.ts`; then `node scripts/a11y-browser.mjs --falsify`
  and `node scripts/a11y-browser.mjs` (playwright is global here)

**Interfaces (produced, exact):**
```ts
export type Kind = "crit" | "warn" | "ok" | "run" | "mut" | "info";
export function kindOf(value: string): Kind;   // slug → kind; unknown → "mut"
StatusPill({ value, label, kind? })            // icon from kind, kind defaults kindOf(value)
Tile({ label, value, unit?, sub?, kind?, spark?: number[], to?, icon? })
Sparkline({ values: number[], width?, height?, kind? })
Donut({ pct: number | null, kind, label, sub? })   // null → "n/a", no arc
Heat({ cells: Kind[], columns? })
Timeline({ runs: {at: string, kind: Kind, label: string}[], ticks: string[] })
Facts({ items: {k: string, v: ReactNode}[] })
KV({ items: [string, ReactNode][] })
Steps({ items: {label: string, state: "done" | "now" | "todo"}[] })
Avatar({ initials, index?, size? }); Person({ initials, index?, name })
OsIcon({ os: string })                          // "windows"|"macos"|"linux"|"ubuntu"|"debian"|other
Chips({ items: string[], active: number, onSelect })
Toolbar({ left, right? })
StatCard keeps its props and renders a Tile.
Table/Th/Td keep props; Td gains `lead?: ReactNode` and `gutter?: Kind`.
Btn variants: secondary (default) | primary | danger | ghost; "gold" maps to secondary.
```

### Task A5: Fixture mode, route list, harmony gate, CI

**Files:**
- Create: `frontend/src/fixtures/mode.ts` (`FIXTURE_MODE`: DEV && (`?__fixtures`
  in `location.search` → set `sessionStorage["itms.fixtures"]="1"`) ||
  sessionStorage flag; wrapped in try/catch)
- Create: `frontend/src/fixtures/client.ts` (`FixtureMap` type derived from
  `ApiClient`; `fixtureClient(): ApiClient` Proxy; `FixtureMissing` error;
  `window.__fixtureMisses: string[]`; registry = `core.ts` merged with
  `import.meta.glob("../modules/*/fixtures.ts", { eager: true })`)
- Create: `frontend/src/fixtures/core.ts` (`me` with EVERY permission code
  the nav knows plus `ai.use`, `platform.status.read`; `getCurrentOrg`;
  health reading methods used by `useHealthReading`; dashboard layout;
  `listDevices` and `getDevice("dev-1")` as the worked example; realistic
  data, no lorem)
- Modify: `frontend/src/lib/ApiContext.tsx` (`FIXTURE_MODE ? fixtureClient()
  : new ApiClient(...)`), `frontend/src/App.tsx` (`RequireAuth`: authed ||
  FIXTURE_MODE), `frontend/src/main.tsx` (skip `initAuth` in fixture mode)
- Create: `frontend/scripts/routes.mjs` (`export const ROUTES = [...]`: every
  `<Route path>` in `App.tsx` except `/__preview`, `/auth/callback`, `*`,
  with params filled: `dev-1`, `job-1`, `tkt-1`, `chg-1`, `plan-1`,
  `test-1`, `lic-1`, `sub-1`, `rpt-1`, `ss-1`, `act-1`, `subnet-1`,
  `sess-1`)
- Create: `frontend/src/test/routes-covered.test.ts` (parses `App.tsx`;
  every path is in ROUTES; ROUTES has no path App lacks)
- Create: `frontend/scripts/harmony.mjs` (as spec §5; flags `--routes a,b`,
  `--strict-fixtures`, `--proto`, `--no-shots`; prints a per-route table:
  strays, overflow, axe, misses; exits 1 per spec; allowlist read from
  `index.css` at runtime, not hardcoded)
- Modify: `frontend/src/test/preview.test.ts` (also asserts `__fixtures`,
  `FixtureMissing`, `fixtures/core` are absent from the bundle)
- Modify: `.gitignore` (`frontend/.harmony/`), `.github/workflows/ci.yml`
  (new `harmony` job cloned from `a11y`: `node scripts/harmony.mjs
  --strict-fixtures`, `actions/upload-artifact` of `frontend/.harmony`)
- Test: the two new tests; `node scripts/harmony.mjs --routes /devices,/dashboard`
  must produce screenshots and report; `npx vitest run src/test/preview.test.ts`

**Interfaces (produced):** `FixtureMap` type; each module's `fixtures.ts`
exports `const fixtures: FixtureMap = {...}` (default export). `window.__fixtureMisses`.

Phase A order: {A1, A2, A5} in parallel → {A3, A4} in parallel → phase
review (diff reviewer per task) → fixes → full suite, tsc, a11y, harmony.

---

## Phase B — pages, one agent per module group

Each agent owns ONLY: its `frontend/src/modules/<m>/**` directories and
creates `frontend/src/modules/<m>/fixtures.ts`. It may not touch `shell/`,
`components/`, `lib/`, `App.tsx`, `index.css`, or another module. Kit gaps
are REPORTED (not patched) with the exact prop needed.

Per page the agent: (1) renders the v3 reference (`node scripts/harmony.mjs
--proto --routes <its routes>`) and reads it; (2) migrates the page to the
kit: `PageHead` (crumb comes from the shell), `Tile`s where v3 has tiles,
`Panel`, `Table` with lead icons/`OsIcon`/`Person`, `StatusPill` with kind,
`Donut`/`Heat`/`Timeline`/`Steps` where v3 draws them, `EmptyState`,
`QueryState`; replaces raw `text-[var(--…)]`, `text-xs/sm/lg`, `rounded-*`,
`border-[var(--line)]`, `.notice`-as-layout with kit pieces; (3) keeps
every control, dialog, filter, keyboard path and test; (4) writes
`fixtures.ts` so every route of the group renders with no `__fixtureMisses`;
(5) runs its tests and `node scripts/harmony.mjs --strict-fixtures --routes
<its routes>` until clean; (6) reports files touched, test counts, kit gaps.

Groups and routes:
1. **overview**: dashboard, health, platform-status, error-monitor →
   `/dashboard /health /platform-status /error-monitor`
2. **fleet**: devices, inventory, onboarding, enrollment, agent-releases,
   agent-push → `/devices /devices/onboard /devices/:deviceId /software
   /discovered /device-groups /enrollment /agent-packages /agent-push
   /agent-push/authorizations /agent-push/credential-scopes`
3. **operations**: jobs, security, credentials, remote, audit → `/jobs
   /security /credentials /remote /remote/:sessionId
   /remote/:sessionId/recording /audit`
4. **patch**: patch (WeekPainter on maintenance window) → `/patch
   /patch/policies`
5. **deploy-backup-provisioning**: deploy (WeekPainter), backup (WeekPainter),
   provisioning → `/deploy /deploy/packages /deploy/scripts /backup
   /backup/repositories /backup/policies /provisioning /provisioning/images
   /provisioning/profiles`
6. **service**: service-desk, portal, sla, email-intake, notification →
   `/service-desk /service-desk/my-work /service-desk/admin
   /service-desk/tickets/:ticketId /portal /portal/new
   /portal/tickets/:ticketId /sla /email-intake /notifications`
7. **governance**: docs, change, bcp, reporting → `/docs /changes
   /changes/approvals /changes/calendar /changes/:changeId /bcp
   /bcp/plans/:planId /bcp/report /bcp/tests/:testId
   /bcp/tests/:testId/report /reports /reports/:reportKey`
8. **finance**: licenses, subscriptions, budget → `/licenses
   /licenses/vendors /licenses/products /licenses/compliance
   /licenses/review-queue /licenses/renewals /licenses/:licenseId
   /subscriptions /subscriptions/spend /subscriptions/renewals
   /subscriptions/:subscriptionId /budget /budget/planner /budget/expenses
   /budget/confirm /budget/admin`
9. **network**: ipam, site-servers → `/ipam /ipam/drift /ipam/search
   /ipam/registry /ipam/scan-authorizations /ipam/import
   /ipam/subnets/:subnetId /site-servers /site-servers/:siteServerId`
10. **assistant-settings**: ai-admin, ai-automation, settings, profile, setup
    → `/assistant /assistant/settings /assistant/autonomy
    /assistant/actions /assistant/actions/:actionId /settings /profile`

After the fan-out: a kit-gap agent applies the reported gaps to the kit
(owning `shell/` and `components/` only), then the full suite, tsc, build,
a11y and `harmony --strict-fixtures` over all routes.

---

## Phase C — the review wave (repeat after every deploy)

1. `node scripts/harmony.mjs --strict-fixtures` and `--proto` produce
   `frontend/.harmony/<route>@1440.png`, `@390.png`, and `proto-<route>.png`.
2. Five reviewer agents, ~18 routes each, read the three images per route
   and file findings with this rubric, each finding `{route, severity:
   blocker|major|minor, kind: harmony|composition|function|a11y|copy,
   what, where}`:
   - harmony: only kit pieces; no stray sizes/radii/colours; spacing rhythm
     matches neighbours;
   - composition: the page carries what v3 draws for it (tiles, panel,
     list, pills with icons, viz) in the same order;
   - function: every control the page had at `5790884` is still there and
     reachable (compare the page's test file and the old screenshot when in
     doubt); dialogs open; filters filter;
   - a11y: nothing colour-only, headings in order, 390px has no overflow;
   - copy: sentence case, no em dashes in new copy, no raw enum strings.
3. Findings are grouped by module and handed to fixers (one per group with
   findings); routes with findings are re-reviewed; loop until dry.
4. Final: full vitest, `tsc`, `vite build`, a11y, harmony strict; a
   whole-branch code review on the most capable model for lost function and
   for invariant 6 (fixture mode absent from the bundle).
