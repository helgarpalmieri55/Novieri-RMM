---
target: "the portal's sign-in, Task 13 8.10: frontend/src/modules/auth/PortalSignInPage.tsx"
total_score: 30
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/auth/PortalSignInPage.tsx"
target_fingerprint: "sha256:74d5b643e7eab48fededa9dcc2242bea3b26d6b227da7f271d51ed4564ec7007"
target_path: frontend/src/modules/auth/PortalSignInPage.tsx
timestamp: 2026-11-25T05-34-19Z
slug: frontend-src-modules-auth-portalsigninpage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: the portal's sign-in (`frontend/src/modules/auth/PortalSignInPage.tsx`, `/portal/sign-in`)

Redesign plan, Task 13 (wave O8), page 8.10. Assessment A from the source and the harmony captures (1440x900, 390x844,
English and Spanish; the walk's Chromium can use passkeys, so the passkey is drawn), then Assessment B.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | "Working…" on the button that sent; refusals announced |
| 2 | Match System / Real World | 3 | "Sign in to the portal"; a contact's words |
| 3 | User Control and Freedom | 3 | Three ways in, each available at once |
| 4 | Consistency and Standards | 4 | The same composition as the console's sign-in: each way across the card, «o» between them |
| 5 | Error Prevention | 3 | One action in flight; disabled until both fields hold something |
| 6 | Recognition Rather Than Recall | 3 | `autocomplete="username webauthn"` offers the passkey from the address field too |
| 7 | Flexibility and Efficiency | 3 | Passkey first, password second, Microsoft third |
| 8 | Aesthetic and Minimalist Design | 3 | Two «o» rules on one card; the third way is one more stop |
| 9 | Error Recovery | 3 | Any refusal one sentence |
| 10 | Help and Documentation | 2 | Nothing beyond "Forgot your password?" |
| **Total** | | **30/40** | **Good** |

## Design Specificity Verdict

**LLM assessment**: A client's contact meets the same frame and lockup as a technician: one product, two audiences.
The passkey is the primary where the browser can use one and the password's "Continue" becomes the primary where it
cannot, so the card always has exactly one.

**Deterministic scan**: CLI `[]`, exit 0; the overlay on `/portal/sign-in?__fixtures`: "No anti-patterns found.";
harmony ok in both languages.

## Overall Impression

The strongest composition in the wave; the cost is length (three ways in, two separators).

## What's Working

- Each way in is an equal-width row, so the choice reads as a choice.
- It still enters the portal, never the console.

## Priority Issues

- **[P3] Two «o» separators.** Fix: leave; three ways in need two. Suggested command: /impeccable distill.

## Persona Red Flags

**Casey (client's contact, occasional user)**: the passkey button is first and labelled in plain words.

## Minor Observations

- None.

## Questions to Consider

- None.

Questions skipped: 1 priority issues
