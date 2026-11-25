---
target: "Discovered on the network, wave O1: frontend/src/modules/devices/CandidatesPage.tsx"
total_score: 30
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/devices/CandidatesPage.tsx"
target_fingerprint: "sha256:be4dfd6adc48546adb74a014102072f571e25055f6d27a7cae9853490a63f630"
target_path: frontend/src/modules/devices/CandidatesPage.tsx
timestamp: 2026-11-25T04-19-07Z
slug: frontend-src-modules-devices-candidatespage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Discovered on the network (`frontend/src/modules/devices/CandidatesPage.tsx`, `/discovered`)

Redesign plan, Task 6 (wave O1), «Cierre de cada oleada» step 3. Assessment A was written from the source and the harmony captures (1440x900 and 390x844, English and Spanish) before any detector output; Assessment B followed.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Tiles + ShowingCount; truncated list says so |
| 2 | Match System / Real World | 3 | Evidence in words («Ping only») |
| 3 | User Control and Freedom | 3 | Search + Client chip in the URL |
| 4 | Consistency and Standards | 3 | Receta L with the bulk bar |
| 5 | Error Prevention | 3 | Promote only with a selection; one request per client |
| 6 | Recognition Rather Than Recall | 3 | Address, MAC, vendor, evidence |
| 7 | Flexibility and Efficiency | 3 | Scan a subnet from ?new=scan |
| 8 | Aesthetic and Minimalist Design | 3 | Empty is the healthy state and says so |
| 9 | Error Recovery | 3 | Per-client failures listed |
| 10 | Help and Documentation | 3 | The note leads to the sweep's own rows |
| **Total** | | **30/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** Specific: the two causes of an empty list («nothing unaccounted for on the swept subnets» vs «every address belongs to a record») and the unsweepable-subnet count are this platform's honesty rules.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/devices` from the worktree root: exit 0, `[]` (the five wave modules all scan clean). Harmony (`--strict-fixtures`, en and es, 1440x900, 390x844, 390x844-client, 641x900-client) on the wave's 13 routes: `ok`, 0 strays, no overflow, no serious axe violation, no fixture miss.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh context per case on this worktree's own `npx vite` in its own process group, `?__fixtures`; `impeccable live-server --background` on 8400, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). `/discovered` en 1440: 9 findings (`gray-on-color`, `undersized-ui-text` x7, `overused-font`); es 390: 5 (same rules). `undersized-ui-text` on table headers and tile captions at 10.5px is a false positive (DESIGN.md's Caption step is 10.5px and PRODUCT.md sets the floor there, as the Task 5 critique recorded); `overused-font` (Inter) and `gray-on-color` on the rail's tenant card are the shell's, already answered in its critique.

## Overall Impression

Good. The primary (Scan a subnet) is the page's act; Promote appears only with a tick.

## What's Working

- ?client= narrows the read and never changes the console's client (D20).
- Promote is never a dead button.

## Priority Issues

- **[P3] The unsweepable note sits close above the panel.**
  - **Why it matters**: Minor rhythm.
  - **Fix**: Left.
  - **Suggested command**: /impeccable polish

## Persona Red Flags

**Technician after a sweep**: sees the residue, ticks what they recognise, promotes; the empty list says it is healthy.

## Minor Observations

- The brief's «Ningún barrido ha encontrado máquinas todavía» was not used: it is false on a healthy estate (see the wave report).

## Questions to Consider

Questions skipped: 1 priority issue
