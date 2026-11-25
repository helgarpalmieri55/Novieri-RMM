---
target: "the client list, Task 5: frontend/src/modules/clients/ClientsPage.tsx"
total_score: 30
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/clients/ClientsPage.tsx"
target_fingerprint: "sha256:5047ac3e30495c875fd2e9900b97ddbe2444fdeea6d59e72b999d72f0ca961b7"
target_path: frontend/src/modules/clients/ClientsPage.tsx
timestamp: 2026-11-25T02-07-50Z
slug: frontend-src-modules-clients-clientspage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: the client list (`frontend/src/modules/clients/ClientsPage.tsx`, `/clients`)

Redesign plan, Task 5, Step 8 ("Cierre de cada oleada", steps 1 to 6). The first list built on the kit and the
model the waves copy (Receta L). Assessment A was written down from the source and the harmony captures
(`/clients`, 1440x900, 390x844, 390x844-client and 641x900-client, English and Spanish) before any detector
output; Assessment B followed.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | "Showing 3 of 3", the pressed chip with its check, the kit's loading and error |
| 2 | Match System / Real World | 3 | Kinds in words ("Your own IT"); the Spanish column says "Dispositivos" where the rail says "Equipos" |
| 3 | User Control and Freedom | 3 | Chip and search in the URL; Back undoes a chip; "Clear filters" |
| 4 | Consistency and Standards | 3 | Receta L to the letter: head, bare panel, toolbar, count, `ListEmpty` |
| 5 | Error Prevention | 3 | New client offered only with `clients.manage` everywhere; the dialog names a missing field |
| 6 | Recognition Rather Than Recall | 3 | Name, domains, kind, counts and status on one row |
| 7 | Flexibility and Efficiency | 3 | Search by name or e-mail domain, case and accents ignored; every view a link |
| 8 | Aesthetic and Minimalist Design | 3 | The page sentence is 98 characters and leaves "uses." alone on its second line |
| 9 | Error Recovery | 3 | "Nothing matches these filters." with its way out; "No clients yet" with New client |
| 10 | Help and Documentation | 3 | The page sentence says what a client holds |
| **Total** | | **30/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** A plain, dense list in the product's own words, built from the kit with nothing of its own:
the same head, toolbar, chips and empty state every wave will draw. That sameness is the point (Product
principle 1). What makes it this product's list is the system clients (its own IT, Unassigned) named for what
they are, and the search that reads e-mail domains, which is how mail is filed to a client.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/clients` from the worktree root: exit 0,
`[]`. Harmony on `/clients`, both languages, four viewports: 0 strays, no overflow, no axe finding at all.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh context per case on this
worktree's own `npx vite` in its own process group with `?__fixtures`, title marked `[Human]`;
`impeccable live-server --background` on 8400, stopped with `--keep-inject`; no project file changed; no
person's browser attached, so no overlay is visible to anyone). `/clients` en 1440: 8 findings; es 390: 4.
`straggler` ("uses." alone on the last of 2 lines, 6% of the measure: agrees with heuristic 8);
`undersized-ui-text` x5 on the table headers at 10.5px (a false positive: DESIGN.md's Caption step is 10.5px
and PRODUCT.md sets the floor there); `gray-on-color` on the rail's tenant card and `overused-font` (Inter),
both the shell's and both already explained in its critique.

## Overall Impression

Correct and quiet. The two things a reader notices are words: a page sentence too long for its line, and, in
Spanish, a column that calls the machines by another name than the rail does.

## What's Working

- The archived filter is a chip whose state is the address it always had (`?archived=1`), so old links work.
- The empty list says which empty it is, and offers New client only when nothing exists yet.
- The search filters the server's whole answer and the count says so ("Showing 1 of 3").

## Priority Issues

- **[P2] The page sentence is 98 characters** against Receta L's 40 to 70, and wraps at 1440 with "uses."
  alone on its second line (the detector's `straggler`). **Fix:** "The companies you serve, with their sites,
  contacts and modules." / "Las empresas a las que das servicio, con sus sedes, contactos y módulos."
  **Command:** /impeccable clarify
- **[P2] "Dispositivos" in the Spanish column** while the rail, the 360's tile and its tab say "Equipos" (the
  glossary: the machine list is "Equipos"). **Fix:** `clients.list.colDevices` → "Equipos" (and the same word
  wherever the clients pages count machines). **Command:** /impeccable clarify
- **[P3] At 390 the status column is behind the table's sideways scroll.** It is the row's one coloured carrier.
  **Fix:** none now: the kit's table scrolls, and the column order is the list's; the health console's rule
  ("name and status first") would move it second. **Command:** /impeccable adapt

## Persona Red Flags

**Alex (power user).** Types a domain into the search and gets the client; the URL keeps it. No red flag.

**Sam (keyboard, screen reader).** The chips are toggles (`aria-pressed`) in a named group; the search is
labelled; the count is text. No red flag.

**Casey (phone).** The status column needs a sideways scroll (P3).

## Minor Observations

- "Unassigned" is the system client's stored name, so it reads in English in the Spanish list beside "Sin
  asignar"; data, not copy.

## Questions to Consider

All three are settled by the sources (Receta L, the Spanish glossary, the kit's table), so none goes to the
coordinator: the answers are appended to `.impeccable/critica-respuestas.md`.

## Polish (same day, before `close`)

- P2, the page sentence: "The companies you serve, with their sites, contacts and modules." (64 characters) /
  "Las empresas que atiendes, con sus sedes, contactos y módulos." (62); one line at 1440 in both languages.
- P2, the Spanish column: `clients.list.colDevices` is "Equipos" (the glossary's word for the machine list).
- P3, the status column at 390: left as it is (the kit's table and the list's column order).
- Step 5: harmony on the six client routes again, both languages, four viewports: `ok`, no axe finding.
