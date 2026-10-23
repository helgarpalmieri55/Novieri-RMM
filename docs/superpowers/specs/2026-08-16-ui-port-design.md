# UI port — bringing the ITMS prototype into the React app

**Date:** 2026-08-16
**Status:** in progress — chrome complete, pages partly migrated (see §10)
**Input:** the operator's interactive prototype (single HTML file, 2,106 lines,
30 views, ~180 CSS classes, 880 lines of CSS).

---

## 0. What this is, and what it is not

**This is a port, not a redesign.** The visual design is settled and is the
operator's. Nothing in this document proposes changing a colour, a radius, a
spacing rule or a layout. Where this document makes a decision, it is about
*how the design reaches a running, authenticated, tenant-scoped application* —
never about what it looks like.

**It is also not a rewrite of the frontend.** The React app already carries the
part the prototype has none of: a 2,928-line typed API client covering the
backend surface, 509 tests, permission-gated navigation, routing, and TanStack
Query. The prototype carries the part the React app has none of: a complete
visual system. The work is to join them.

---

## 1. Why the prototype cannot simply be served

The prototype contains **zero `fetch()` calls**. Every screen is a function
returning a template string, and its data is either a module-level constant
(`devices`, `alerts`, `tickets`, `jobs`, `changes`, `docs`) or written directly
into the markup — `renderPatch()`'s six patch policies are a literal array
inside a template literal, not a variable a request could replace.

There is therefore **no seam to wire a backend into**. "Use it without
modifying it" and "make it live" cannot both hold: any wiring rewrites the
render functions. This spec resolves that the honest way — **the prototype file
is never modified and never shipped. It is the reference.** The design is
reproduced in React, token for token, and the file stays in the repository as
the thing the result is checked against.

---

## 2. Non-negotiables

1. **The colours are copied, not chosen.** Every value in the prototype's
   `:root` block is transcribed verbatim into the app's theme. A reviewer must
   be able to diff the two lists and find them identical.
2. **The frontend never gates security.** Invariant 6. Sidebar gating is
   cosmetic; the server decides. This is why a hidden link is not a control and
   a visible one is not a grant.
3. **No new visual language.** Screens the prototype does not cover are
   assembled from its existing components. If a screen seems to need a new
   component, that is a signal to re-read the prototype, not to invent.
4. **Every migrated page keeps its tests.** A page is not ported until its
   existing tests pass against the new markup, adjusted for structure but not
   weakened. A test deleted to make a port green is a defect.

---

## 3. The token layer

The app already runs **Tailwind v4**, whose CSS-first `@theme` takes custom
properties directly. The prototype's `:root` block is copied into
`frontend/src/index.css` unchanged — same names, same hex values, same shadows,
same `--radius: 16px`, `--sidebar-w: 258px`, `--topbar-h: 72px`.

Two decisions this forces, both of which are *findings*, not preferences:

**3.1 The design is light-only, and the app currently claims otherwise.** The
prototype sets `color-scheme: light` and has no dark theme, no
`prefers-color-scheme` block, and no `[data-theme]` hook. The app's current
`index.css` declares `color-scheme: light dark`. **The port commits to
light-only**, matching the design that was approved. Removing the dark claim is
part of the work — a declared colour scheme with no dark palette behind it
makes a browser render form controls and scrollbars dark against a light page,
which is a real visual defect, not a theoretical one.

**3.2 Inter is named but never loaded, and it cannot be loaded the usual way.**
The prototype's font stack begins with `Inter` and the file contains **no
`@font-face` and no font import** — so in the prototype it silently falls back
to the system sans-serif on any machine without Inter installed. The obvious
fix (a Google Fonts link) is **forbidden here**: this platform is self-hosted,
frequently on networks with no egress, and `docs/product.md` states as a product
property that it **does not phone home**. A stylesheet fetched from a third
party on every console load contradicts that.

**Decision: Inter is self-hosted.** The woff2 files ship with the frontend
bundle and are served from the same origin, with the system stack retained as
the fallback. If licensing or size makes that unattractive, the alternative is
to drop to the system stack everywhere — but that changes the typography of the
approved design, so it is the operator's call, not a silent substitution.

---

## 4. The shell

One layout component tree, built once, used by every page:

| Prototype element | React component | Notes |
|---|---|---|
| `.app-shell` / `.shell` / `.main` | `AppShell` | Grid; owns `--sidebar-w` / `--topbar-h` |
| `.sidebar`, `.nav-group`, `.nav-item`, `.nav-badge` | `Sidebar` | **RBAC-gated — see 4.1** |
| `.topbar`, `.topbar-search`, `.top-actions`, `.profile-btn` | `TopBar` | Tenant switcher, profile |
| `.drawer` + head/body/foot | `Drawer` | Detail panel, one per page at most |
| `.modal` + head/body/foot | `Modal` | Confirmations and forms |
| `.palette` | `CommandPalette` | Global search, `⌘K` |
| `.toast-stack`, `.toast` | `Toaster` | Mutation feedback |

### 4.1 RBAC in a grouped sidebar

The prototype renders all 30 nav items unconditionally; the live app gates ~20
links on permission codes. The port keeps the prototype's seven groups and its
exact styling, and adds gating:

- each item carries its permission code and renders only when the operator's
  permission set contains it;
- **a group whose items are all hidden does not render at all** — an empty
  group heading is worse than no heading, and the prototype has no styling for
  one because the case does not arise in a mock;
- the badge counts on `health` and `service-desk` are live counts, not the
  mock's `12` and `23`, and are absent rather than zero when the operator
  cannot read that module.

**A guard test asserts that every nav item has a permission code**, so a new
module cannot be added to the sidebar ungated — the same shape as the
`bound_auth_context` sweep on the backend.

---

## 5. The component layer

~180 CSS classes resolve to roughly 25 reusable components: `Panel`,
`PageHead`, `StatCard`, `MetricInline`, `DataTable`, `FilterBar`, `Tabs`,
`Segmented`, `StatusPill`, `Progress`, `Donut`, `LineChart`, `MiniList`,
`ModuleCard`, `AssetCard`, `Timeline`, `Kanban`, `TicketCard`, `Notice`,
`EmptyState`, `Code`, `Field`, `FormGrid`, `Avatar`, `Button`.

These are transcriptions. The rule for each: **the rendered DOM carries the
prototype's class names and structure**, so a visual diff against the reference
is meaningful and a future prototype revision can be re-diffed rather than
re-interpreted.

---

## 6. Page migration

Pages migrate one at a time onto the shell. Each is its own reviewable unit and
each carries the same contract:

1. real data through the existing typed client — no new fetch layer;
2. loading, empty and error states, because the mock has none and every one of
   these screens can be all three;
3. its existing tests passing;
4. no route or permission behaviour changed.

Order follows operator value: **Devices → Jobs → Service Desk → Patch →
Health → Backup**, then the remainder. The first is the template the rest copy.

**A partially-migrated app must look deliberate, not broken.** Pages are
migrated onto a shell that is already in place, so an unmigrated page renders
inside the new chrome with old content rather than as a different application.

---

## 7. The gaps between design and reality

| Gap | Resolution |
|---|---|
| **Error monitor** has no screen in the design | Add to the *Platform* group, built from existing components |
| **Credential vault** has no screen in the design | Add to the *Fleet* group, same |
| **Dashboard, Immutable Audit, Enrollment & Certificates** are designed but have no feature folder | All three have backing APIs (kernel routes for audit and enrollment). Build as real pages |
| **Settings** is designed and largely unbacked | Scope to what exists — org profile, AI provider config, notification channels. Do not build the screen the mock implies |
| **MDM** has no screen and no backend | Out of scope here. Designed in the prototype's language when its spec lands |
| Mock content asserting facts (agent adoption %, "Financial dataset: Ready") | Wiring reveals which panels have no backing data. Each becomes either a real query or an explicit empty state — never a hardcoded number in shipped code |

**Found by migrating the first page (Devices), and representative of what the
rest will hit.** The design's Devices table has eight columns; **four have no
field behind them** — Site, Patch %, Backup, Risk. `GET /api/v1/devices`
serves hostname, fqdn, os_family, os_version, architecture, status,
agent_status, last_seen_at and cert_state, and nothing more. Two of the four
stat cards are in the same position: "At risk" has no definition anywhere in
the backend, and every card's "↑ 12 vs last 7 days" delta needs a previous
value no endpoint serves.

They are **omitted, not faked**. The open questions each one raises:

| Design element | What it needs |
|---|---|
| **Site** | A device→site association. Site servers exist; a device is not assigned to one |
| **Patch %** | Per-device patch compliance. Exists inside the patch module; joining it to a device list means either an endpoint that serves the join or one request per row |
| **Backup** | Per-device backup state. Same shape of problem as Patch |
| **Risk** | Has no definition. Someone has to decide what makes a device risky before anything can compute it |
| **Trend deltas** | A previous value. Nothing stores fleet counts over time |

Patch and Backup are the two worth serving properly, and both are backend
decisions rather than frontend ones.

---

## 8. Testing requirements

| # | Test | Falsifier |
|---|---|---|
| 1 | Theme tokens match the prototype's `:root` exactly | Change one hex; the test names it |
| 2 | Every sidebar item declares a permission code | Add an ungated item |
| 3 | A group with no visible items does not render | Revoke every permission in a group; assert no heading |
| 4 | Badge counts come from queries, not constants | Grep the sidebar for numeric literals |
| 5 | Each migrated page renders loading, empty and error | Force each state |
| 6 | The 509 existing tests still pass | — |
| 7 | No stylesheet or font is fetched cross-origin | Assert the built bundle references no external host |

Test 7 is the one that protects a product property rather than a pixel.

---

## 9. Out of scope

Dark mode. Redesigning any screen. Changing routes, permissions or API shapes.
Modifying the prototype file. MDM and Odoo-budget screens — both wait for their
own specs.

---

## 10. Build status, 2026-08-16

**Done, and verified by something that runs:**

| | |
|---|---|
| Design tokens | Transcribed verbatim; `tokens.test.ts` diffs both files and fails on any drift |
| Shell | Sidebar (7 groups, RBAC-gated, empty groups dropped), brand, topbar, breadcrumb, tenant switcher, profile, collapse |
| Component layer | `PageHead`, `Panel`, `StatCard`, `StatsGrid`, `Tabs`, `StatusPill`, `Progress`, `TableWrap`, `FilterBar`, `CellMain`, `QueryState` |
| Shared primitives | `Button`, `Badge`, `Card`, `Table`, `Th`, `Td`, `Dialog`, `Field` emit the design's classes — reaching all 109 files that import them |
| Page heads | All 47 pages |
| Palette | 726 Tailwind palette utilities remapped to the design's variables; 155 `dark:` variants removed; both guarded |
| Fully migrated pages | Devices (the template), Jobs, Health |

**Not done:**

- **`Drawer`, `CommandPalette`, `Toaster`** — designed, not built. Nothing
  depends on them yet; they are additive.
- **Per-page layout** for the remaining 44 pages: they render inside the
  design's chrome with the design's palette and primitives, but keep their own
  content layout rather than the design's stat cards, tabs and filter bars.
  Devices is the template.
- **Page descriptions.** `PageHead`'s `description` is optional and only
  Devices, Jobs and Health have one. Machine-writing sixty-six product
  sentences was the alternative, and a wrong one misdescribes the product in
  the console for ever.
- **The four orphan screens** (§7) and **the two missing ones** — Dashboard,
  Settings, Enrollment, Audit; Error Monitor and Credentials are in the nav but
  keep their existing layouts.
- **Inter is still not bundled** (§3.2). Awaiting the operator's call on
  self-hosting versus dropping to the system stack.
- **Nothing has been seen in a browser.** jsdom tests and the production build
  are the whole of the verification. The DOM structure and class names are
  right and the CSS is byte-identical to the design; "the CSS is correct" and
  "it looks right" remain different claims.
