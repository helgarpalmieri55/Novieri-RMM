---
target: "account recovery, Task 13 8.9: frontend/src/modules/auth/RecoverPage.tsx"
total_score: 28
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 1
target_identity: "file:frontend/src/modules/auth/RecoverPage.tsx"
target_fingerprint: "sha256:05365937bab9288cf0f669348dd79f3a481fab1c1e736df7ac4dc493fc4ec691"
target_path: frontend/src/modules/auth/RecoverPage.tsx
timestamp: 2026-11-25T05-34-19Z
slug: frontend-src-modules-auth-recoverpage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: account recovery (`frontend/src/modules/auth/RecoverPage.tsx`, `/recover#<token>`)

Redesign plan, Task 13 (wave O8), page 8.9. Assessment A from the source and the harmony captures (1440x900, 390x844,
English and Spanish; the optional-password variant), then Assessment B.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | "Opening your recovery link…", "Working…" |
| 2 | Match System / Real World | 3 | Says what recovering removes before it removes it |
| 3 | User Control and Freedom | 3 | Opening the link does nothing; one deliberate press |
| 4 | Consistency and Standards | 3 | `NewPasswordFields`, AuthAlert |
| 5 | Error Prevention | 3 | The password is optional and says so; required when an emailed link set it |
| 6 | Recognition Rather Than Recall | 2 | The read-only address looks editable |
| 7 | Flexibility and Efficiency | 3 | Leave both fields empty to keep the password |
| 8 | Aesthetic and Minimalist Design | 2 | The optional-password note sits flush on the address label: it reads as that field's caption |
| 9 | Error Recovery | 3 | A raced 409 makes the field required and says why; nothing is spent |
| 10 | Help and Documentation | 3 | What happens, then what is optional |
| **Total** | | **28/40** | **Good** |

## Design Specificity Verdict

**LLM assessment**: The page is specific to this product's recovery command and does its job: what will happen, for
whom, and one press. The grouping inside the form is the weak point.

**Deterministic scan**: CLI `[]`, exit 0; the overlay on `/recover?__fixtures`: "No anti-patterns found."; harmony ok
in both languages.

## Overall Impression

Correct in behaviour. Visually, the note "Optional: set a new password now…" has no space below it and touches the
"Email address" label, so it reads as the address's help and separates from the two password fields it is about.

## What's Working

- "Recovering removes this account's second factors…" is said before the button, in full.
- A recovery that raced a mail reset keeps the link unspent and asks again.

## Priority Issues

- **[P1] The optional-password note is jammed against the address label.** Why: grouping tells a person which
  sentence belongs to which control; here the note about the password reads as the address's caption, at every width
  and in both languages. Fix: give the note the space below it a field has (16px, `.field`), so it heads the fields it
  introduces. Suggested command: /impeccable layout.
- **[P2] The read-only address looks editable** (as on the invitation). Fix: the sunken surface, kit request for the
  general rule. Suggested command: /impeccable polish.

## Persona Red Flags

**Riley (stressed: locked out of their own console)**: reads the note as about the address and wonders whether to
change it.

## Minor Observations

- None.

## Questions to Consider

- None that the sources do not settle.

Questions skipped: 2 priority issues
