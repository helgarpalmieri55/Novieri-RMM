---
target: "wave O4, Provisioning runs: frontend/src/modules/provisioning/ProvisioningRunsPage.tsx"
total_score: 30
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:/home/user/rmm-w4/frontend/src/modules/provisioning/ProvisioningRunsPage.tsx"
target_fingerprint: "sha256:dd09f8f28e6a6ff140b8108c4acbb8df50d5210b6ea5d1755b7ebf221871bde7"
target_path: /home/user/rmm-w4/frontend/src/modules/provisioning/ProvisioningRunsPage.tsx
timestamp: 2026-11-25T04-37-41Z
slug: les-provisioning-provisioningrunspage-tsx-907d28da
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Provisioning runs (`frontend/src/modules/provisioning/ProvisioningRunsPage.tsx`, `/provisioning`)

Redesign plan, Task 9 (wave O4), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (both languages, 1440x900 and 390x844) before any detector output; Assessment B followed. No sub-agent tool is exposed in this session, so A and B ran in series in one context.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Count, pressed chip, the kit's states |
| 2 | Match System / Real World | 3 | States in words; the error sentence in the row |
| 3 | User Control and Freedom | 3 | State in the URL; Provision from `?new=run` |
| 4 | Consistency and Standards | 3 | Receta L; no Client chip, as decided |
| 5 | Error Prevention | 3 | Provision names the missing profile |
| 6 | Recognition Rather Than Recall | 3 | Profile by name, target by name or MAC |
| 7 | Flexibility and Efficiency | 3 | Chips; every view a link |
| 8 | Aesthetic and Minimalist Design | 3 | At 390 the head's title takes two lines |
| 9 | Error Recovery | 3 | Kit empty sentence with Clear |
| 10 | Help and Documentation | 3 | Tiles say which zero |
| **Total** | | **30/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** The run history on the kit, keeping the module's honest tiles (a median measured only over the runs on the page, a rate that is "Never run" over zero). Chips carry no counts because the brief keeps them bare.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/provisioning` from the worktree root: exit 0,
`[]`. Harmony (`--strict-fixtures`, both languages, 1440x900, 390x844, 390x844-client, 641x900-client): 0 strays,
no overflow, the top bar in bounds, no serious axe violation, no fixture miss.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh context per route on this
worktree's own `npx vite` with `?__fixtures`, title marked `[Human]`; `impeccable live-server --background` on
8400, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is
visible to anyone). Run at en 1440x900 and es 390x844. On every route of the wave the same three come back
and are not this page's: `undersized-ui-text` on the 10.5px table headers, tile labels and fact captions
(DESIGN.md's Caption step and PRODUCT.md's floor: a false positive, as the client list's critique already
recorded), `gray-on-color` on the rail's tenant card and `overused-font` (Inter), both the shell's.
On this page: `unbalanced-heading` on the h1 at es 390 (two lines, the second 16% of the first).

## Overall Impression

Good. Nothing to fix beyond a long title on a phone.

## What's Working

- The failed run's sentence sits in its row with a floor that keeps it readable.
- Provision opens only for someone holding `provisioning.run`, whatever the URL says.

## Priority Issues

- **[P3] The title wraps on a phone.** "Aprovisionamiento e imágenes de disco" on two lines at 390 (`unbalanced-heading`). **Fix:** None now: the page's name. **Command:** /impeccable typeset

## Persona Red Flags

**Casey.** Chips wrap to three lines at 390.

## Minor Observations

- None.

## Questions to Consider

Questions skipped: 1 priority issues
