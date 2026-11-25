---
target: "wave O4, A site server: frontend/src/modules/site-servers/SiteServerDetailPage.tsx"
total_score: 30
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:/home/user/rmm-w4/frontend/src/modules/site-servers/SiteServerDetailPage.tsx"
target_fingerprint: "sha256:53c0554f3e6a8a72852c7272d29cb9c7065683aa60abc27ca83bc67cb371776e"
target_path: /home/user/rmm-w4/frontend/src/modules/site-servers/SiteServerDetailPage.tsx
timestamp: 2026-11-25T04-37-41Z
slug: les-site-servers-siteserverdetailpage-tsx-38573b5c
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: A site server (`frontend/src/modules/site-servers/SiteServerDetailPage.tsx`, `/site-servers/ss-1`)

Redesign plan, Task 9 (wave O4), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (both languages, 1440x900 and 390x844) before any detector output; Assessment B followed. No sub-agent tool is exposed in this session, so A and B ran in series in one context.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Head badge, facts, segment check |
| 2 | Match System / Real World | 3 | "Wake a machine" in words |
| 3 | User Control and Freedom | 3 | Back to all site servers; Wake leads to its form |
| 4 | Consistency and Standards | 3 | Receta F head, one primary |
| 5 | Error Prevention | 3 | No wake offered on a switched-off server |
| 6 | Recognition Rather Than Recall | 3 | Device and address on one fact |
| 7 | Flexibility and Efficiency | 3 | Tab title names the server |
| 8 | Aesthetic and Minimalist Design | 3 | The enable state appears twice: head badge and first fact |
| 9 | Error Recovery | 3 | The wake reports what the job did, not what was asked |
| 10 | Help and Documentation | 3 | The check says what it cannot verify |
| **Total** | | **30/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** The record head adds what the page lacked, the way back and the client; the declaration-beside-evidence layout below it is the module's own and unchanged.

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
On this page: `straggler`, "inventario." alone on the segment check's note at es 390. Harmony itself failed this route before the critique (axe `link-in-text-block` on the client link inside the head's sentence, all four viewports, es); fixed in b35a55f and green since in both languages.

## Overall Impression

Harmony caught a real defect before this critique (axe `link-in-text-block`: the client link sat inside a run of plain text); fixed in b35a55f by making the context the link alone and moving the address into the facts. What remains is cosmetic.

## What's Working

- Wake a machine brings the form into view with its first field focused instead of pretending to be a one-click act.

## Priority Issues

- **[P3] The enable state twice.** Head badge and the first fact. **Fix:** None now: `siteServers.test.tsx` pins `detail-state` in the facts strip. **Command:** /impeccable distill
- **[P3] The segment check's note leaves a word alone at 390.** "inventario." (`straggler`). **Fix:** None now. **Command:** /impeccable clarify

## Persona Red Flags

**Sam.** The head action moves focus into the form. No red flag.

## Minor Observations

- None.

## Questions to Consider

Questions skipped: 2 priority issues
