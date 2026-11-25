---
target: "wave O4, Site servers: frontend/src/modules/site-servers/SiteServersPage.tsx"
total_score: 29
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:/home/user/rmm-w4/frontend/src/modules/site-servers/SiteServersPage.tsx"
target_fingerprint: "sha256:77c2c306fa4579aa244df18d2327665abe00fd9e284a983b9c13a62e0501c67f"
target_path: /home/user/rmm-w4/frontend/src/modules/site-servers/SiteServersPage.tsx
timestamp: 2026-11-25T04-37-41Z
slug: modules-site-servers-siteserverspage-tsx-43f61ae6
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Site servers (`frontend/src/modules/site-servers/SiteServersPage.tsx`, `/site-servers`)

Redesign plan, Task 9 (wave O4), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (both languages, 1440x900 and 390x844) before any detector output; Assessment B followed. No sub-agent tool is exposed in this session, so A and B ran in series in one context.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Count, chip, agent and enable state as pills |
| 2 | Match System / Real World | 3 | Roles in words ("Wake-on-LAN") |
| 3 | User Control and Freedom | 3 | State and client in the URL; Promote from `?new=promote` |
| 4 | Consistency and Standards | 3 | Receta L |
| 5 | Error Prevention | 3 | Promote only with `site.manage`; disabled rows unmistakable |
| 6 | Recognition Rather Than Recall | 3 | Segments under the name |
| 7 | Flexibility and Efficiency | 3 | Chips, Client chip |
| 8 | Aesthetic and Minimalist Design | 2 | The head sentence is 90 characters in Spanish and wraps "segmento." alone |
| 9 | Error Recovery | 3 | "Nothing yet" wins over a filter when there is nothing |
| 10 | Help and Documentation | 3 | The roles note says what this release does not do |
| **Total** | | **29/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** The list whose reason to exist is telling a switched-off server from a live one, and it still does, in words. The nothing-yet sentence now says what to do: promote a computer on the client's network.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/site-servers` from the worktree root: exit 0,
`[]`. Harmony (`--strict-fixtures`, both languages, 1440x900, 390x844, 390x844-client, 641x900-client): 0 strays,
no overflow, the top bar in bounds, no serious axe violation, no fixture miss.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh context per route on this
worktree's own `npx vite` with `?__fixtures`, title marked `[Human]`; `impeccable live-server --background` on
8400, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is
visible to anyone). Run at en 1440x900 and es 390x844. On every route of the wave the same three come back
and are not this page's: `undersized-ui-text` on the 10.5px table headers, tile labels and fact captions
(DESIGN.md's Caption step and PRODUCT.md's floor: a false positive, as the client list's critique already
recorded), `gray-on-color` on the rail's tenant card and `overused-font` (Inter), both the shell's.
On this page: nothing beyond those three; the long Spanish head sentence was read from the capture (Assessment A).

## Overall Impression

Good. The Spanish head sentence is long.

## What's Working

- An empty estate says so even under a filter; a filter that found none of several says that instead.

## Priority Issues

- **[P2] The head sentence is too long in Spanish.** "Dispositivos que hacen barridos y encienden equipos con Wake-on-LAN en su propio segmento." (90) wraps with "segmento." alone at 1440; Receta L asks for 40 to 70. **Fix:** "Equipos que barren su segmento y encienden otros con Wake-on-LAN." (65) / "Computers that sweep their segment and wake others with Wake-on-LAN." (68). **Command:** /impeccable clarify

## Persona Red Flags

**Jordan.** "Promote device" is the action and the empty state names it. No red flag.

## Minor Observations

- None.

## Questions to Consider

Questions skipped: 1 priority issues
