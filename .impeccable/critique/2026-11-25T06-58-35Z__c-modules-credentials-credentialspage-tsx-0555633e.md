---
target: "Credentials, wave O6: frontend/src/modules/credentials/CredentialsPage.tsx"
total_score: 30
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/credentials/CredentialsPage.tsx"
target_fingerprint: "sha256:fc725c12b4158822303f7467fe9a2ffd96ca1d5ddafeb3945dee6405061e9294"
target_path: frontend/src/modules/credentials/CredentialsPage.tsx
timestamp: 2026-11-25T06-58-35Z
slug: c-modules-credentials-credentialspage-tsx-0555633e
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Credentials (`frontend/src/modules/credentials/CredentialsPage.tsx`, `/credentials`)

Redesign plan, Task 11 (wave O6), "Cierre de cada oleada", step 3. Assessment A was written from the source and the harmony captures (`/credentials`, 1440x900, 390x844, 390x844-client and 641x900-client, Spanish) and this run's own captures with a client chosen (English, 1440) before any detector output; Assessment B followed.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | "Showing 4 of 4"; n/a before the answer |
| 2 | Match System / Real World | 3 | Types and vault state in words |
| 3 | User Control and Freedom | 3 | State and client in the URL; `?new=` with Back |
| 4 | Consistency and Standards | 3 | Receta L; the panel keeps its no-secret sentence |
| 5 | Error Prevention | 3 | A checkout asks for a reason; delete asks |
| 6 | Recognition Rather Than Recall | 3 | Client column with every client |
| 7 | Flexibility and Efficiency | 3 | `?new=push_install` from the push pages |
| 8 | Aesthetic and Minimalist Design | 3 | The "Checked out" tile's foot wraps to two lines and makes the row uneven |
| 9 | Error Recovery | 3 | A refused delete said |
| 10 | Help and Documentation | 3 | "How secrets are protected" |
| **Total** | | **30/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** A vault page whose first promise is "names only"; specific.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/credentials` from the worktree root: exit 0, `[]`.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh page per route on this worktree's own Vite with `?__fixtures`, a client chosen, title marked `[Human]`; `impeccable live-server --background` on 8401, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). `/credentials` en 1440: 12 findings; `?new=push_install`: 13. `nested-cards` x2 is the readiness step inside its panel and the "How secrets are protected" notice inside its panel (the kit's `Notice`); `line-length` (~106 characters) is the New credential dialog's sentence about push install; the rest as below. `undersized-ui-text` on the 10.5px captions (tile labels, table headers, chip counts) is a false positive: DESIGN.md's Caption step is 10.5px and PRODUCT.md sets the floor there. `gray-on-color` (#8592ab on #1b2945) is the rail's tenant card and `overused-font` (Inter) is the brand's one face, both the shell's and both explained in its critique.

## Overall Impression

Good.

## What's Working

- The state chips are the old tabs' `?state=`, so every link still lands.
- A secret is shown once, in its dialog, never in a list.

## Priority Issues

- **[P3] The fourth tile's foot wraps.** "Decrypted for someone, on the audit log" is two lines where the others are one. **Fix:** A shorter foot: "Each one on the audit log" / "Cada una, en la auditoría". **Command:** /impeccable clarify

## Persona Red Flags

**Sam.** The chips are a named group; the dialogs are labelled. No red flag.

Questions skipped: 1 priority issues

## Polish (same day, before `close`)

- P3, the fourth tile's foot: "Each one on the audit log" / "Cada una, en el registro de auditoría".
- Step 5: harmony on the wave's sixteen routes again, both languages, four viewports, after the polish (the wave's report lists the PNGs).
