---
target: "Credential scopes, wave O1: frontend/src/modules/agent-push/PushCredentialScopesPage.tsx"
total_score: 29
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/agent-push/PushCredentialScopesPage.tsx"
target_fingerprint: "sha256:55eee8023d869ea94f46b81e04c732745550e138c0ad990a0725a3958ec30673"
target_path: frontend/src/modules/agent-push/PushCredentialScopesPage.tsx
timestamp: 2026-11-25T04-19-08Z
slug: s-agent-push-pushcredentialscopespage-tsx-08e405e5
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Credential scopes (`frontend/src/modules/agent-push/PushCredentialScopesPage.tsx`, `/agent-push/credential-scopes`)

Redesign plan, Task 6 (wave O1), «Cierre de cada oleada» step 3. Assessment A was written from the source and the harmony captures (1440x900 and 390x844, English and Spanish) before any detector output; Assessment B followed.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Readiness steps; ShowingCount |
| 2 | Match System / Real World | 3 | Credential named, never its secret |
| 3 | User Control and Freedom | 3 | Chip in the URL, survives a reload; ?new=scope |
| 4 | Consistency and Standards | 3 | SectionNav; list on the kit; form in a dialog |
| 5 | Error Prevention | 3 | Bind disabled until credential and prefix; not Domain Admin |
| 6 | Recognition Rather Than Recall | 3 | Prefix reach per row |
| 7 | Flexibility and Efficiency | 3 | Readiness and list on one page |
| 8 | Aesthetic and Minimalist Design | 2 | Same weight above the list as authorizations |
| 9 | Error Recovery | 3 | Empty picker explained |
| 10 | Help and Documentation | 3 | Separation of duties stated |
| **Total** | | **29/40** | **Good** |

## Design Specificity Verdict

**LLM assessment.** Specific: the dedicated-account warning sits where the choice is made.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/agent-push` from the worktree root: exit 0, `[]` (the five wave modules all scan clean). Harmony (`--strict-fixtures`, en and es, 1440x900, 390x844, 390x844-client, 641x900-client) on the wave's 13 routes: `ok`, 0 strays, no overflow, no serious axe violation, no fixture miss.

**Visual overlays.** `detect.js` injected through Chromium (Playwright, a fresh context per case on this worktree's own `npx vite` in its own process group, `?__fixtures`; `impeccable live-server --background` on 8400, stopped with `--keep-inject`; no project file changed; no person's browser attached, so no overlay is visible to anyone). `/agent-push/credential-scopes` en 1440: 13 findings (same as authorizations: shared hazard/readiness text, `nested-cards`); es 390: 9 (`straggler` x4, `unbalanced-heading`, `cramped-padding` on `Steps`, `nested-cards`, `undersized-ui-text` x2, `overused-font`). `undersized-ui-text` on table headers and tile captions at 10.5px is a false positive (DESIGN.md's Caption step is 10.5px and PRODUCT.md sets the floor there, as the Task 5 critique recorded); `overused-font` (Inter) and `gray-on-color` on the rail's tenant card are the shell's, already answered in its critique.

## Overall Impression

As authorizations: correct, heavy above the list.

## What's Working

- The bind body is unchanged (2a(d)) and the warning is in the dialog.
- `?unbound=1` survives a reload.

## Priority Issues

- **[P2] Hazard notice and readiness panel above the list.**
  - **Why it matters**: List below the fold.
  - **Fix**: Left (today's order, invariant copy).
  - **Suggested command**: /impeccable layout
- **[P3] `unbalanced-heading` on a Spanish readiness step title at 390.**
  - **Why it matters**: Shared readiness copy.
  - **Fix**: Left.
  - **Suggested command**: /impeccable typeset

## Persona Red Flags

**Scope manager**: binds a dedicated credential, sees what binding does not authorize.

## Minor Observations

- Stragglers in the shared readiness text at 390 (es).

## Questions to Consider

Questions skipped: 2 priority issues
