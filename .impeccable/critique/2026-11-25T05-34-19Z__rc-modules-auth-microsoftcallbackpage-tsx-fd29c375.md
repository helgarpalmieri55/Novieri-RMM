---
target: "Microsoft's return, Task 13 8.5: frontend/src/modules/auth/MicrosoftCallbackPage.tsx"
total_score: 24
max_score: 32
na_heuristics: 7,10
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/auth/MicrosoftCallbackPage.tsx"
target_fingerprint: "sha256:70a13a207f0343f0546b606a9b90a37d2933ee8b17874c1dbfe59de9ee555c81"
target_path: frontend/src/modules/auth/MicrosoftCallbackPage.tsx
timestamp: 2026-11-25T05-34-19Z
slug: rc-modules-auth-microsoftcallbackpage-tsx-fd29c375
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: Microsoft's return (`frontend/src/modules/auth/MicrosoftCallbackPage.tsx`, `/sign-in/microsoft`)

Redesign plan, Task 13 (wave O8), page 8.5. Assessment A from the source and the harmony captures (the walk has no
fragment from Microsoft, so it photographs the "did not finish" state), then Assessment B.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 2 | While it works: "Working…" as a grey note, not announced; on failure the headline still says "Signing in with Microsoft" |
| 2 | Match System / Real World | 3 | Microsoft's own refusal is said in this console's words; its `error_description` is never shown |
| 3 | User Control and Freedom | 3 | One way back: the sign-in page, or Sign-in and security for a link |
| 4 | Consistency and Standards | 3 | AuthAlert and a text link, as every other step |
| 5 | Error Prevention | 4 | The code goes once, in a body; the fragment leaves the address at once |
| 6 | Recognition Rather Than Recall | 3 | Nothing to remember |
| 7 | Flexibility and Efficiency | n/a | A pass-through page with no choices |
| 8 | Aesthetic and Minimalist Design | 3 | One sentence and one link |
| 9 | Error Recovery | 3 | Each refusal one sentence, with the way back |
| 10 | Help and Documentation | n/a | Nothing to document on a return page |
| **Total** | | **24/32** | **Good** |

## Design Specificity Verdict

**LLM assessment**: A return page should be nearly invisible; this one is: the frame, one sentence, one link. The
brand above the card tells a person who was just at Microsoft that they are back on Novieri RMM.

**Deterministic scan**: CLI `[]`, exit 0; the overlay on `/sign-in/microsoft?__fixtures`: "No anti-patterns found.";
harmony ok in both languages.

## Overall Impression

Right for what it is. The one mismatch is the headline on the failure: "Signing in with Microsoft" above "The sign-in
with Microsoft did not finish".

## What's Working

- The lockup is the first thing a person sees coming back from Microsoft's page.
- The code is consumed once (a ref across React's double mount) and never shown.

## Priority Issues

- **[P2] The headline says "Signing in" over a failure.** Why: status and headline disagree for a moment of doubt.
  Fix: not in this wave: the brief keeps every page's texts, and the alert under it is the status. A headline per
  state would be a new catalogue key. Suggested command: /impeccable clarify.
- **[P3] "Working…" is a grey note, not a `role="status"`.** Fix: leave; the request is sent on arrival and the page
  changes on its own. Suggested command: /impeccable harden.

## Persona Red Flags

**Sam (Accessibility)**: arrives on a page whose only content is the alert; it is announced.

## Minor Observations

- None.

## Questions to Consider

- Should the failure state carry its own headline (a new key in both catalogues)?

Questions skipped: 2 priority issues
