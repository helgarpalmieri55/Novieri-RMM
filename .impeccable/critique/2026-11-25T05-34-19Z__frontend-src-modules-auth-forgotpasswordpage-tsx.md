---
target: "forgotten password, Task 13 8.6: frontend/src/modules/auth/ForgotPasswordPage.tsx"
total_score: 30
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/auth/ForgotPasswordPage.tsx"
target_fingerprint: "sha256:02c71e593bdb2751768cbc2c2a7041099ba740dc6f93846d06445d3d85da0c71"
target_path: frontend/src/modules/auth/ForgotPasswordPage.tsx
timestamp: 2026-11-25T05-34-19Z
slug: frontend-src-modules-auth-forgotpasswordpage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: forgotten password (`frontend/src/modules/auth/ForgotPasswordPage.tsx`, `/forgot-password`)

Redesign plan, Task 13 (wave O8), page 8.6. Assessment A from the source and the harmony captures (1440x900, 390x844,
English and Spanish), then Assessment B.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | "Working…", then a status that says what happens next and for how long |
| 2 | Match System / Real World | 3 | "Send the link"; the answer is the same whether or not the address has an account |
| 3 | User Control and Freedom | 3 | "Back to sign-in" goes to the sign-in page the person came from |
| 4 | Consistency and Standards | 3 | The frame's field, notice and link; the primary is inline where sign-in's is across the card |
| 5 | Error Prevention | 3 | Disabled until there is an address; one request per press |
| 6 | Recognition Rather Than Recall | 3 | `autocomplete="username"` |
| 7 | Flexibility and Efficiency | 3 | Enter submits |
| 8 | Aesthetic and Minimalist Design | 3 | One field, one button, one link |
| 9 | Error Recovery | 3 | A failure is one sentence |
| 10 | Help and Documentation | 3 | The intro and the sent status say the link works once, for 30 minutes, and the factor still applies |
| **Total** | | **30/40** | **Good** |

## Design Specificity Verdict

**LLM assessment**: Plain and correct; the copy carries the product's security stance (it cannot be used to learn who
has an account) without saying it out loud.

**Deterministic scan**: CLI `[]`, exit 0; the overlay on `/forgot-password?__fixtures`: "No anti-patterns found.";
harmony ok in both languages.

## Overall Impression

Nothing to fix in the composition; the frame adds the brand and the tab name.

## What's Working

- The sent status is honest about what is and is not known.
- The way back keeps the portal's sign-in for a contact.

## Priority Issues

- **[P3] The primary is inline here and across the card on sign-in.** Fix: leave in this wave (the brief stretches
  only the sign-in pages); question for the coordinator. Suggested command: /impeccable layout.

## Persona Red Flags

None.

## Minor Observations

- None.

## Questions to Consider

- Should every one-form step of the frame stretch its primary as sign-in does?

Questions skipped: 1 priority issues
