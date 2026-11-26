---
target: "first boot, Task 13 8.12: frontend/src/modules/setup/SetupPage.tsx"
total_score: 30
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 1
target_identity: "file:frontend/src/modules/setup/SetupPage.tsx"
target_fingerprint: "sha256:9695d8d446f0ffc3ce4b8d5fe61954227a109a0767d8282cee14897599b5936e"
target_path: frontend/src/modules/setup/SetupPage.tsx
timestamp: 2026-11-25T05-34-19Z
slug: frontend-src-modules-setup-setuppage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: first boot (`frontend/src/modules/setup/SetupPage.tsx`, no route: `SetupGate` draws it while the installation has no owner)

Redesign plan, Task 13 (wave O8), page 8.12. The gate cannot walk it (fixture mode skips `SetupGate`). Assessment A
from the source and hand-made captures with `npx vite` (no fixtures; Playwright answered `/api/v1/setup/status` with
`{"needed": true}`) at 1440x900 and 390x844, English and Spanish. Then Assessment B (CLI only: the overlay needs
fixture mode).

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | "Setting up…" on the button; refusals announced |
| 2 | Match System / Real World | 3 | "Install code from the server log" |
| 3 | User Control and Freedom | 3 | Nothing is created until the press; the form keeps what was typed after a refusal |
| 4 | Consistency and Standards | 4 | Now the same frame as every screen before a session (it was a copy with no `<main>`) |
| 5 | Error Prevention | 3 | One claim however many presses; the password checked before asking |
| 6 | Recognition Rather Than Recall | 3 | Labels above fields |
| 7 | Flexibility and Efficiency | 3 | Enter submits |
| 8 | Aesthetic and Minimalist Design | 3 | Seven fields, one primary |
| 9 | Error Recovery | 3 | A 422 names the field; a wrong code keeps the form |
| 10 | Help and Documentation | 2 | "Finish setup" is disabled until all six required fields hold something, and nothing on screen says so |
| **Total** | | **30/40** | **Good** |

## Design Specificity Verdict

**LLM assessment**: The first screen of an installation now carries the brand and the language switch it lacked
(a buyer who reads Spanish could not switch before). Seven fields in one column, one primary.

**Deterministic scan**: `impeccable detect --json frontend/src/modules/setup`: `[]`, exit 0. No overlay (the page is
not reachable in fixture mode).

## Overall Impression

Now in the frame, with a `<main>` landmark and a tab title. The one gap: the disabled "Finish setup" does not say why
it is disabled, on a form whose required fields are not marked.

## What's Working

- The double-click guard and the one whole-document load to `/sign-in` are untouched.
- Its error is the frame's AuthAlert.

## Priority Issues

- **[P1] "Finish setup" is disabled with no reason on screen** (coordinator ruling 8). Why: seven fields, none marked
  required; a person who left "Organization name" empty sees a grey button and nothing else. Fix: while the form is
  incomplete, one line beside the button says "Fill in every field to finish." (new key, both catalogues); the
  password hint stays. Suggested command: /impeccable clarify.

## Persona Red Flags

**Jordan (the buyer, first time)**: fills five fields, misses one at the bottom, the button stays grey.

## Minor Observations

- At 390 px the hint under the button wraps to three lines; fine.

## Questions to Consider

- None.

Questions skipped: 1 priority issues
