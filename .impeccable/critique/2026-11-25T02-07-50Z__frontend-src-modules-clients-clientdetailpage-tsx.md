---
target: "the client 360, Task 5: frontend/src/modules/clients/ClientDetailPage.tsx"
total_score: 27
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 1
target_identity: "file:frontend/src/modules/clients/ClientDetailPage.tsx"
target_fingerprint: "sha256:6fdf9db716cb7c37744f6dd8f10b0f9eec277ee13fab2ccec88b0a741bc3ffba"
target_path: frontend/src/modules/clients/ClientDetailPage.tsx
timestamp: 2026-11-25T02-07-50Z
slug: frontend-src-modules-clients-clientdetailpage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: the client 360 (`frontend/src/modules/clients/ClientDetailPage.tsx`, `/clients/:id`)

Redesign plan, Task 5, Step 8 ("Cierre de cada oleada", steps 1 to 6). The first record built on the kit and
the model of every wave's record (Receta F). Assessment A was written down from the source and the harmony
captures (`/clients/<Andes>` and its `?tab=devices|tickets|alerts|assets` variants, 1440x900, 390x844,
390x844-client and 641x900-client, English and Spanish) before any detector output; Assessment B followed.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Tiles say `n/a` until measured; at 390 the selected tab can sit outside the scrolled strip |
| 2 | Match System / Real World | 2 | The Alerts tab names checks by wire key (`cpu.busy_pct`), in English on the Spanish page |
| 3 | User Control and Freedom | 3 | Every tab in the URL; Back undoes a tab; the back link and the crumb |
| 4 | Consistency and Standards | 2 | One count, two names ("Equipos 12" above "Dispositivos 12"); the alert console says "CPU busy" |
| 5 | Error Prevention | 3 | Archive confirms with its consequences; actions and tabs only with their codes |
| 6 | Recognition Rather Than Recall | 3 | Each tile leads to its tab; each tab leads to its list with the Client chip set |
| 7 | Flexibility and Efficiency | 3 | A tab is a link; tiles are shortcuts |
| 8 | Aesthetic and Minimalist Design | 2 | Details repeats kind, status and both counts the head and tiles show; four full-width tiles before the tabs on a phone |
| 9 | Error Recovery | 3 | Each tab's error from `QueryState`; another client chosen above says so with the one act that fixes it |
| 10 | Help and Documentation | 3 | The Alerts subtitle says what "open" counts (D19) |
| **Total** | | **27/40** | **Acceptable** |

## Design Specificity Verdict

**LLM assessment.** Atera's customer page in Novieri's dress: status and kind under the name, the record's two
acts on the right, four tiles that each open their tab, eight tabs that fit one row at 1440 in both languages
(Atera's eleven wrap into two). What is this product's own is the scope rule made visible: every tab reads
only this client, and every "See …" link lands on the module's list with the Client chip already set. The weak
spot is vocabulary, where the page borrows other modules' rows without their words.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/clients` from the worktree root: exit 0,
`[]`. Harmony on the five 360 routes, both languages, four viewports: 0 strays, no overflow, no axe finding.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh context per case on this
worktree's own `npx vite` in its own process group with `?__fixtures`, title marked `[Human]`;
`impeccable live-server --background` on 8400, stopped with `--keep-inject`; no project file changed; no
person's browser attached). Cases: en 1440 summary, es 1440 alerts, en 390 tickets, es 1440 assets, en 1440
devices: 6 to 10 findings each, all `undersized-ui-text` on 10.5px captions (tile labels, table headers: a
false positive, DESIGN.md's Caption step), the shell's `gray-on-color` and `overused-font`. The detector does
not read words, so it did not see the wire keys.

## Overall Impression

The structure is right and holds in both languages and at every width. The biggest problem is that the
Alerts tab shows `memory.used_pct` where every other alert row in the console says "Memory used".

## What's Working

- Tabs, tiles and "See …" links form one loop: tile → tab → the module's list narrowed to this client.
- The Alerts tab is honest about what it lists: active alerts, with the server's count of every open one.
- A tab, a tile or an action appears only with its code in this client, and no tab reads before `/me` answers.

## Priority Issues

- **[P1] The Alerts tab names checks by wire key** (`service.running`, `cpu.busy_pct`, `disk.free_pct`), in
  English on the Spanish page, where the alert console, the drawer and the device page say "CPU busy", "Disk
  free" through `checkLabel`. **Fix:** `checkLabel(alert.check_type)` from `modules/health/healthState`, the
  observed value beneath; the hostname kept on one line as the console keeps it. **Command:** /impeccable clarify
- **[P2] One count, two names.** "Equipos 12" in the tile and "Dispositivos 12" in Details just below (English:
  "Computers" and "Devices"); the Sites tab's column says "Dispositivos" too. **Fix:** the glossary's word,
  "Equipos" / "Computers", for the client's machines on these pages. **Command:** /impeccable clarify
- **[P2] At 390 the selected tab can be outside the strip.** Opening `?tab=alerts` (the sixth tab) shows
  Summary to Tickets and no underline. **Fix:** the kit's `Tabs` scrolls the active tab into view
  (`shell/components.tsx`, a base file: a request to the kit in the report). **Command:** /impeccable adapt
- **[P3] Details repeats the head and the tiles** (kind, status, computers, sites). **Fix:** none now; the
  rows were the page's only counts before the tiles, and removing them is the coordinator's call (question 1).
  **Command:** /impeccable distill

## Persona Red Flags

**Jordan (first-timer).** Reads `memory.used_pct` and has to guess (P1).

**Casey (phone).** Four full-width tiles (DESIGN.md: tiles go full width at 640px) put the tabs under the fold,
and a deep link to a late tab shows no selected tab (P2).

**Sam (keyboard, screen reader).** The tablist is named "Client", one tab stop with arrow keys; the panel is
labelled by its tab; tiles are links. No red flag.

## Minor Observations

- In fixture mode the Computers tab is empty under a "Computers 12" tile: `fixtures/core.ts` gives every
  machine to the internal client. Not the page's; it also means harmony measures the tab's empty state, not its
  table (a request to the kit in the report).
- The archive refusal paragraph renders inside the head's action row; with a long 409 it will sit beside the
  buttons rather than under them.

## Questions to Consider

1. Details: (a) keep every fact, as now; (b) drop the four the head and the tiles already show. Not settled by
   DESIGN.md ("Records" lists the head, tiles and tabs, not the summary tab's content): goes to the coordinator.
2. The wire keys and the two names are settled by the glossary and `checkLabel`'s own rule; the off-screen tab
   by DESIGN.md "Records" (one row of tabs) and the kit: answers appended to `.impeccable/critica-respuestas.md`.

## Polish (same day, before `close`)

- P1, the wire keys: `AlertsTab.tsx` names each check with `checkLabel` ("CPU busy" / "CPU ocupada"), the
  observed value beneath, the hostname on one line. Test: "names each alert's check in words" (RED on the
  committed tab, GREEN after).
- P2, one count, two names: `clients.summary.devices` and `clients.sites.colDevices` are "Computers" /
  "Equipos", the tile's and the tab's word.
- P2, the off-screen selected tab at 390: not fixed here; `Tabs` is the kit's (`shell/components.tsx`, a base
  file), so the report carries it as a request to the kit with its code.
- P3, Details repeating the head and the tiles: left for the coordinator (question 1).
- Step 5: harmony on the six client routes again, both languages, four viewports: `ok`, no axe finding.
