---
target: "the second factor, Task 13 8.2: frontend/src/modules/auth/FactorPage.tsx"
total_score: 30
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/auth/FactorPage.tsx"
target_fingerprint: "sha256:7784512ff340e07b91b03fa2f38c2162fa0e2358419804736f6315e95fe8f3e6"
target_path: frontend/src/modules/auth/FactorPage.tsx
timestamp: 2026-11-25T05-34-19Z
slug: frontend-src-modules-auth-factorpage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: the second factor (`frontend/src/modules/auth/FactorPage.tsx`, `/sign-in/factor`)

Redesign plan, Task 13 (wave O8), page 8.2. Assessment A from the source and the harmony captures (1440x900, 390x844,
English and Spanish; the walk opens the page directly, so all three factors are offered and no remember box is drawn),
then Assessment B.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | "Working…" on the button that sent; a refusal is one announced sentence |
| 2 | Match System / Real World | 3 | "Passkey", "Authenticator app", "recovery code": the words the enrolment used |
| 3 | User Control and Freedom | 3 | "Start again" goes back to the sign-in page the person came from; the recovery code is a link with its own address, so Back closes it |
| 4 | Consistency and Standards | 3 | Two choices as two H2 sections, the same as enrolment; secondary ways as text links |
| 5 | Error Prevention | 3 | The remember box is unticked every time and says not to tick it on a shared computer |
| 6 | Recognition Rather Than Recall | 3 | `autocomplete="one-time-code"`, numeric keypad on a phone |
| 7 | Flexibility and Efficiency | 3 | Passkey first, app second, recovery folded away; Enter submits the code |
| 8 | Aesthetic and Minimalist Design | 3 | Calm; the "Verify code" secondary is disabled and grey beside an empty field |
| 9 | Error Recovery | 3 | One sentence, the code cleared; a timed-out sign-in says to start again, with the link |
| 10 | Help and Documentation | 3 | Each section says what the factor is; the recovery section says each code works once |
| **Total** | | **30/40** | **Good** |

## Design Specificity Verdict

**LLM assessment**: The step reads as a continuation of the sign-in: the same frame, the same lockup, the same
headline rung, and the two factors drawn as the enrolment drew them. "Use a recovery code instead" and "Start again"
are now two quiet text links under the choices, so the screen has one primary (the passkey) and nothing that competes
with it.

**Deterministic scan**: CLI `[]`, exit 0; the overlay on `/sign-in/factor?__fixtures` (1440x900): "No anti-patterns found."; harmony ok in
both languages.

## Overall Impression

The right order and the right weight: the passkey first and primary, the app second, the recovery code a link. The
recovery link is now a view of the page (`?use=recovery`) that keeps `next` and the factors the sign-in step handed
over, so nothing changes under the person when they open it.

## What's Working

- One primary per state; the TOTP submit is secondary when a passkey is offered and primary when it is not.
- The recovery field takes focus when the link opens it, so a keyboard user is not dropped at the top of the page
  when the link they pressed disappears.
- "Do not ask again on this computer" stays a checkbox: it is the choice sent with the factor.

## Priority Issues

- **[P3] Two text links stacked under the sections** ("Use a recovery code instead", "Start again") read as a list of
  equal weight, though one is a way through and the other a way out. Fix: leave; both are secondary and the copy says
  which is which. Suggested command: /impeccable layout.

## Persona Red Flags

**Riley (Stressed, mid-incident on a phone)**: the numeric keypad opens for the code; the passkey button is first.
No red flag.

**Sam (Accessibility)**: two H2 regions under one H1; the remember checkbox is labelled by its sentence; the link
announces as a link. No red flag.

## Minor Observations

- At 390 px the passkey hint wraps to two lines; fine.

## Questions to Consider

- None that the sources do not settle.

Questions skipped: 1 priority issues
