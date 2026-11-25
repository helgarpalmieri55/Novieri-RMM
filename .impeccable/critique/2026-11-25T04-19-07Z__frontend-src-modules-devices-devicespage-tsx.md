---
target: "Devices (Equipos), wave O1: frontend/src/modules/devices/DevicesPage.tsx"
total_score: 29
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 1
target_identity: "file:frontend/src/modules/devices/DevicesPage.tsx"
target_fingerprint: "sha256:24e26473b6d4e4400476f4cd786ff89d72bd70a04c879eb8c6d98ed135367a9f"
target_path: frontend/src/modules/devices/DevicesPage.tsx
timestamp: 2026-11-25T04-19-07Z
slug: frontend-src-modules-devices-devicespage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Devices (Equipos) (`frontend/src/modules/devices/DevicesPage.tsx`, `/devices`)

Redesign plan, Task 6 (wave O1), «Cierre de cada oleada» step 3. Assessment A was written from the source and the harmony captures (1440x900 and 390x844, English and Spanish) before any detector output; Assessment B followed.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Chips with server counts, ShowingCount, bulk bar only with a tick, kit loading/error |
| 2 | Match System / Real World | 3 | «Equipos» in Spanish (D13); provenance in words |
| 3 | User Control and Freedom | 3 | Every filter in the URL; Clear filters; Back undoes a chip |
| 4 | Consistency and Standards | 3 | Receta L: head, tiles, bare panel, toolbar, bulk bar, table |
| 5 | Error Prevention | 3 | Push ticks capped at 25, no select-all (2a(f)) |
| 6 | Recognition Rather Than Recall | 3 | Name, agent, last seen, certificate and origin on one row |
| 7 | Flexibility and Efficiency | 3 | Search, two chip rows, Client chip, site select; every view a link |
| 8 | Aesthetic and Minimalist Design | 2 | Five head actions wrap to two rows (four at 390); the toolbar is four rows before the first row |
| 9 | Error Recovery | 3 | Empty list says nothing-yet with Add a computer, or nothing-matches with Clear filters |
| 10 | Help and Documentation | 3 | Sentence under the title; tile feet say what each count is |
| **Total** | | **29/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** Authored for a fleet: the server-counted chips, the provenance chips ("who vouched for this row") and the push-install ticks capped at 25 are this product's, not a generic table. The kit makes it look like every other list, which is the point (PRODUCT.md principle 1).

**Deterministic scan.** `impeccable detect --json frontend/src/modules/devices` from the worktree root: exit 0, `[]` (the five wave modules all scan clean). Harmony (`--strict-fixtures`, en and es, 1440x900, 390x844, 390x844-client, 641x900-client) on the wave's 13 routes: `ok`, 0 strays, no overflow, no serious axe violation, no fixture miss.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh context per case on this worktree's own `npx vite` in its own process group, `?__fixtures`; `impeccable live-server --background` on 8400, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). `/devices` es 1440: 15 findings on the first pass (summary only), 0 on the detailed pass (the script reads the console 3 s after injection); the rules seen on the other wave routes are the shell's and the kit's. `undersized-ui-text` on table headers and tile captions at 10.5px is a false positive (DESIGN.md's Caption step is 10.5px and PRODUCT.md sets the floor there, as the Task 5 critique recorded); `overused-font` (Inter) and `gray-on-color` on the rail's tenant card are the shell's, already answered in its critique.

## Overall Impression

The page is now one list on the kit with the Client chip narrowing the read. What hurts is weight: five head actions and a four-row toolbar push the first machine below 600 px, and a single-page foot draws two disabled buttons.

## What's Working

- The Client chip narrows the read only (D6), and a change of chip never keeps another client's rows on screen.
- Absent Zero holds: «Waiting for review» is drawn only when the server counts it; origin chips carry no invented count.
- The bulk bar appears only with a ticked machine, so Push install (N) is never a dead button.

## Priority Issues

- **[P1] The paging foot draws «Previous devices» and «Next devices» disabled on a single page, beside a range that repeats «Showing 16 of 16»; at 390 the range is clipped at the left edge.**
  - **Why it matters**: Two dead controls (DESIGN.md: don't ship a dead control) and a second count on every short list.
  - **Fix**: Use the kit's `Pager` (it hides itself on one page) with the range as its children: on a single page the toolbar's ShowingCount is the count; on more pages the foot appears with the range and the two buttons.
  - **Suggested command**: /impeccable polish
- **[P2] Five head actions (Add asset, Export inventory, Push install…, Push runs, Add a computer) wrap under the sentence at 1440 and into four rows at 390.**
  - **Why it matters**: The primary is the last of five; on a phone the actions take a screen before the tiles.
  - **Fix**: Not settled by DESIGN.md (it fixes one primary, not the secondaries' number). Options: (a) keep; (b) Push runs as a ghost link; (c) a «More» menu, a kit piece that does not exist. Left as is; raised to the coordinator.
  - **Suggested command**: /impeccable distill
- **[P2] The search takes the toolbar's whole first row; state chips, origin chips and the Client chip wrap onto three more rows.**
  - **Why it matters**: ≈190 px of controls before the table at 1440.
  - **Fix**: Kit request (ListToolbar's search flex basis), not a page change: every list would follow.
  - **Suggested command**: /impeccable layout

## Persona Red Flags

**Technician on a desk** (PRODUCT.md): finds a machine fast with search + chips, but scrolls past tiles and four toolbar rows first. **Technician on a phone during an incident**: four rows of head buttons and nine rows of chips before the first machine.

## Minor Observations

- Client and Site read «—» on every row in fixture mode: the fixture rows carry no `client_id` (fixtures are the base's).
- The table scrolls sideways at 1440 (eleven columns); existing behaviour.

## Questions to Consider

1. Head actions: (a) keep five, (b) Push runs as a ghost link, (c) a «More» menu (needs a kit piece). Not settled by the sources: raised to the coordinator; kept (a), the option most consistent with today's page.
2. Single-page foot: (a) the kit's `Pager`, hidden on one page. Settled by DESIGN.md «Don't ship a dead control» and Receta L («el pie (Pager)»): applied.
3. Toolbar height: (a) a kit change to the search's flex basis. A kit request, raised to the coordinator.
