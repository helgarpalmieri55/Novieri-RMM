---
target: "the recovery codes, Task 13 8.4: frontend/src/modules/auth/RecoveryCodesPage.tsx"
total_score: 31
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/auth/RecoveryCodesPage.tsx"
target_fingerprint: "sha256:6d62fc113ab89f01eef9d1608d388e781d86ce3372d422625db8799b4b267a60"
target_path: frontend/src/modules/auth/RecoveryCodesPage.tsx
timestamp: 2026-11-25T05-34-19Z
slug: frontend-src-modules-auth-recoverycodespage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: the recovery codes (`frontend/src/modules/auth/RecoveryCodesPage.tsx`, `/sign-in/recovery-codes`)

Redesign plan, Task 13 (wave O8), page 8.4. Assessment A from the source and the harmony captures. The walk opens the
page directly, so it photographs the "nothing to show" state (the codes live in memory only and are never in the
URL); the codes state was read from the source and the jsdom render. Then Assessment B.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | "The codes are on the clipboard." / "would not copy them" announced |
| 2 | Match System / Real World | 3 | "Save your recovery codes", "I have saved these codes somewhere safe" |
| 3 | User Control and Freedom | 3 | The console opens only after the tick; nothing irreversible before |
| 4 | Consistency and Standards | 3 | One primary ("Continue to the console"); Copy secondary |
| 5 | Error Prevention | 4 | The tick before the console is the one moment losing the codes is still cheap to avoid |
| 6 | Recognition Rather Than Recall | 3 | Ten codes in a two-column monospaced grid, selectable |
| 7 | Flexibility and Efficiency | 3 | Copy all at once; select by hand when the browser refuses |
| 8 | Aesthetic and Minimalist Design | 3 | Calm; in the "nothing to show" state the explanation is a grey note |
| 9 | Error Recovery | 3 | A refused clipboard says so and the codes stay on screen |
| 10 | Help and Documentation | 3 | The intro says what the codes do and that they are shown only now |
| **Total** | | **31/40** | **Good** |

## Design Specificity Verdict

**LLM assessment**: The step's composition is product-specific: codes once, in memory only, a tick before the console.
Monospace is used for what it is for here (codes a person transcribes), not as a costume.

**Deterministic scan**: CLI `[]`, exit 0; the overlay on `/sign-in/recovery-codes?__fixtures`: "No anti-patterns found."; harmony ok in both languages.

## Overall Impression

Correct and unchanged in behaviour; the frame adds the brand and the tab name.

## What's Working

- One primary, disabled until the tick, with the tick directly above it: the reason a control is off is on screen.
- The codes never touch the URL or the router's state, and leave memory with the page.

## Priority Issues

- **[P3] The "nothing to show" explanation is a grey note.** It is the only content of that state. Fix: leave; it is a
  sentence with its real cause and an action, which is what an empty state must be. Suggested command: /impeccable clarify.

## Persona Red Flags

**Riley (on a phone)**: the two-column grid stays two columns at 390 px; the codes fit.

## Minor Observations

- None.

## Questions to Consider

- None.

Questions skipped: 1 priority issues
