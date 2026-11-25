---
target: "Add a computer, wave O1: frontend/src/modules/onboarding/OnboardPage.tsx"
total_score: 28
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 1
target_identity: "file:frontend/src/modules/onboarding/OnboardPage.tsx"
target_fingerprint: "sha256:2b46704637ac16ba0917d2fffd3e7cc5da1b30527773a5dc42d105b6c72dd75e"
target_path: frontend/src/modules/onboarding/OnboardPage.tsx
timestamp: 2026-11-25T04-19-08Z
slug: frontend-src-modules-onboarding-onboardpage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Add a computer (`frontend/src/modules/onboarding/OnboardPage.tsx`, `/devices/onboard`)

Redesign plan, Task 6 (wave O1), «Cierre de cada oleada» step 3. Assessment A was written from the source and the harmony captures (1440x900 and 390x844, English and Spanish) before any detector output; Assessment B followed.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Steps with their state and «You are here» |
| 2 | Match System / Real World | 2 | Spanish says «Aparece en Dispositivos» where the list is «Equipos» |
| 3 | User Control and Freedom | 3 | Back link to the computers; sections fold |
| 4 | Consistency and Standards | 3 | FormPage + FormSection, no save bar |
| 5 | Error Prevention | 3 | A missing permission is named |
| 6 | Recognition Rather Than Recall | 3 | Client and site ride on every link |
| 7 | Flexibility and Efficiency | 3 | Every step links to its page |
| 8 | Aesthetic and Minimalist Design | 2 | «Muchos equipos en una red:» repeats the section title in its first note |
| 9 | Error Recovery | 3 | No count before an answer |
| 10 | Help and Documentation | 3 | The journey explains itself |
| **Total** | | **28/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** Its own: a journey that owns no control and says what blocks the next step.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/onboarding` from the worktree root: exit 0, `[]` (the five wave modules all scan clean). Harmony (`--strict-fixtures`, en and es, 1440x900, 390x844, 390x844-client, 641x900-client) on the wave's 13 routes: `ok`, 0 strays, no overflow, no serious axe violation, no fixture miss.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh context per case on this worktree's own `npx vite` in its own process group, `?__fixtures`; `impeccable live-server --background` on 8400, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). `/devices/onboard` es 390: `cramped-padding` on the kit's `Steps` list (the kit's; the same list on /agent-push); en 1440: 0. `undersized-ui-text` on table headers and tile captions at 10.5px is a false positive (DESIGN.md's Caption step is 10.5px and PRODUCT.md sets the floor there, as the Task 5 critique recorded); `overused-font` (Inter) and `gray-on-color` on the rail's tenant card are the shell's, already answered in its critique.

## Overall Impression

Reads as a form page with nothing to save, which is what it is. Two copy defects.

## What's Working

- One computer comes first, the network journey after.
- No save bar: nothing here is kept.

## Priority Issues

- **[P1] The one-computer note says «Aparece en Dispositivos» in Spanish; the list it names is «Equipos» (D13).**
  - **Why it matters**: Two names for one page on the page that leads to it.
  - **Fix**: «Aparece en Equipos» (es); English keeps «Devices».
  - **Suggested command**: /impeccable clarify
- **[P2] The many-computers note starts «Muchos equipos en una red:», the section's own title right above it.**
  - **Why it matters**: Redundant once the note is inside the section.
  - **Fix**: Start the note at «Esta plataforma nunca se conecta…» / «This platform never connects…».
  - **Suggested command**: /impeccable clarify

## Persona Red Flags

**Technician with one machine**: two buttons in the second section; **with a subnet**: the steps say what is done and what blocks.

## Minor Observations

- The kit's `Steps` list is flush with its border on the right (`cramped-padding`): kit, not page.

## Questions to Consider

Questions skipped: 2 priority issues
