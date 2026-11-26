---
target: "the AI app's request, Task 13 8.11: frontend/src/modules/auth/ConsentPage.tsx"
total_score: 32
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/auth/ConsentPage.tsx"
target_fingerprint: "sha256:c75c342cfaa761b642a9a7fb83596de6e99a2034708c2c5a8b2c5ddd6dfa7d00"
target_path: frontend/src/modules/auth/ConsentPage.tsx
timestamp: 2026-11-25T05-34-19Z
slug: frontend-src-modules-auth-consentpage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: the AI app's request (`frontend/src/modules/auth/ConsentPage.tsx`, `/oauth/authorize`)

Redesign plan, Task 13 (wave O8), page 8.11. The gate does not walk this route (`EXCLUDED`: it needs an AI app's
request). Assessment A from the source and hand-made captures with `npx vite` in fixture mode (a new dev fixture,
`oauthAuthorizeDetails`: an app that publishes its client metadata document) at 1440x900 and 390x844, English and
Spanish; the registered-app, loopback and refusal variants read from the source and jsdom. Then Assessment B.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | "Checking the request…", "Working…" on the button that sent |
| 2 | Match System / Real World | 3 | The host an app cannot fake heads it; the name it gave itself is its claim |
| 3 | User Control and Freedom | 4 | Decline never leaves on its own; only an approval follows a redirect |
| 4 | Consistency and Standards | 3 | The frame; the warning family for both warnings |
| 5 | Error Prevention | 4 | What approving allows is a warning directly above Approve; the second factor when the server asks |
| 6 | Recognition Rather Than Recall | 3 | The account, the return host and the app id on the card |
| 7 | Flexibility and Efficiency | 3 | Two buttons, nothing else |
| 8 | Aesthetic and Minimalist Design | 3 | Dense but ordered: who, facts, how long, the warning, the decision |
| 9 | Error Recovery | 3 | A passing failure offers "Try again"; a refusal says it is for good |
| 10 | Help and Documentation | 3 | Where to end the connection later (Settings → AI connections) |
| **Total** | | **32/40** | **Excellent** |

## Design Specificity Verdict

**LLM assessment**: This is the most product-specific screen in the frame: it carries invariant 1c on the control.
The facts come first (host, claim, where the answer goes, the app id, the account, how long), and the one sentence
that says the app will act with the person's whole authority sits in the warning colour directly above "Approve",
whole, not trimmed.

**Deterministic scan**: CLI `[]`, exit 0. The overlay (`detect.js` on `/oauth/authorize?…&__fixtures`, 1440x900)
reported one: `nested-cards`, "Card inside card", on the `.notice.gold` warning inside the card. A false positive: a
notice is the design's inline message (DESIGN.md's notice family, role note), not a container, and the brief requires
exactly this element (`grants.closest(".notice")` gold, inside `.panel`). The loopback warning has always been the same
element in the same card. Proposed for the coordinator:
`.claude/skills/impeccable/scripts/impeccable ignores add-value nested-cards "*" --file "frontend/src/modules/auth/ConsentPage.tsx" --reason "the .notice.gold is the design's inline warning (role note), placed in the card beside Approve by plan Task 13 8.11 and invariant 1c; a notice is a message, not a card" --shared`.

## Overall Impression

Every fact the page showed is still shown, approve and decline send what they sent, and the authority statement is
now impossible to miss on the way to the button.

## What's Working

- Host first, name as claim, app id in monospace that breaks anywhere at 390 px.
- The tab reads "Connect the app at claude.ai · Novieri RMM": the person can tell this tab from the AI app's.

## Priority Issues

- **[P3] "App id:" is in monospace with its value.** Fix: leave; today's markup, and the label is short.
  Suggested command: /impeccable typeset.

## Persona Red Flags

**Sam (Accessibility)**: the warning is `role="note"` with a warn icon and text; Approve and Decline are named
buttons; the step-up dialog keeps its own focus.

## Minor Observations

- None.

## Questions to Consider

- None.

Questions skipped: 1 priority issues
