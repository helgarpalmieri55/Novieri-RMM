---
target: "the reset link, Task 13 8.7: frontend/src/modules/auth/ResetPasswordPage.tsx"
total_score: 30
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/auth/ResetPasswordPage.tsx"
target_fingerprint: "sha256:4cd60fd8afd8091b2a9f436297c5cde89dc55890bbbff05ff6011e0e375d59b5"
target_path: frontend/src/modules/auth/ResetPasswordPage.tsx
timestamp: 2026-11-25T05-34-19Z
slug: frontend-src-modules-auth-resetpasswordpage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: the reset link (`frontend/src/modules/auth/ResetPasswordPage.tsx`, `/reset-password#<token>`)

Redesign plan, Task 13 (wave O8), page 8.7. Assessment A from the source and the harmony captures (the walk has no
fragment, so it photographs the form; the done and dead-link states were read from the source and jsdom), then B.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | "Working…", then a done status with the next step |
| 2 | Match System / Real World | 3 | The rule for both kinds of account, said once |
| 3 | User Control and Freedom | 3 | A dead link offers a new one |
| 4 | Consistency and Standards | 3 | `NewPasswordFields`, as invite, recover and setup |
| 5 | Error Prevention | 3 | The two fields must match; the lower bound is checked before asking |
| 6 | Recognition Rather Than Recall | 3 | A hidden username field for the password manager |
| 7 | Flexibility and Efficiency | 3 | Enter submits; paste welcome |
| 8 | Aesthetic and Minimalist Design | 3 | Two fields and a button |
| 9 | Error Recovery | 3 | The server's `password_rejected` sentence is shown as sent |
| 10 | Help and Documentation | 3 | The rule sits under the field it governs |
| **Total** | | **30/40** | **Good** |

## Design Specificity Verdict

**LLM assessment**: Correct and quiet. The token is read from the fragment and sent in a body; the tab title is the
headline and never carries it (a new test says so).

**Deterministic scan**: CLI `[]`, exit 0; the overlay on `/reset-password?__fixtures`: "No anti-patterns found.";
harmony ok in both languages.

## Overall Impression

Unchanged in behaviour and right; the frame adds the brand and the tab name.

## What's Working

- One rule sentence that covers a technician and a contact, because the link names no account.

## Priority Issues

- **[P3] No intro under the headline**, unlike its neighbours. Fix: leave; today's text, and the rule under the field
  says what is needed. Suggested command: /impeccable clarify.

## Persona Red Flags

None.

## Minor Observations

- None.

## Questions to Consider

- None.

Questions skipped: 1 priority issues
