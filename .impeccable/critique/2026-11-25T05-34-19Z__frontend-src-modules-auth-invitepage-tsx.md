---
target: "the invitation, Task 13 8.8: frontend/src/modules/auth/InvitePage.tsx"
total_score: 29
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 0
target_identity: "file:frontend/src/modules/auth/InvitePage.tsx"
target_fingerprint: "sha256:0f4d00a635bfd868386fab51b03781fbc440edbcf249357a3b4168aadc84b171"
target_path: frontend/src/modules/auth/InvitePage.tsx
timestamp: 2026-11-25T05-34-19Z
slug: frontend-src-modules-auth-invitepage-tsx
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: the invitation (`frontend/src/modules/auth/InvitePage.tsx`, `/invite#<token>`)

Redesign plan, Task 13 (wave O8), page 8.8. Assessment A from the source and the harmony captures (a technician's
invitation from the fixture, 1440x900 and 390x844, English and Spanish; the contact's passkey and Microsoft variants
read from the source and jsdom), then Assessment B.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | "Opening your invitation…", then the form; "Working…" on the button |
| 2 | Match System / Real World | 3 | "Welcome, Dana Okafor", the address in bold, what comes next |
| 3 | User Control and Freedom | 3 | A dead invitation says who can make another |
| 4 | Consistency and Standards | 3 | `NewPasswordFields`; «o» separators now the frame's own |
| 5 | Error Prevention | 3 | One action in flight across password, passkey and Microsoft |
| 6 | Recognition Rather Than Recall | 2 | The address is shown in a field that looks editable and is read-only |
| 7 | Flexibility and Efficiency | 3 | A contact may use a passkey or Microsoft instead of a password |
| 8 | Aesthetic and Minimalist Design | 3 | One primary per variant |
| 9 | Error Recovery | 3 | Each refusal one sentence; a dead link is its own state |
| 10 | Help and Documentation | 3 | The rule under the password; a technician's Microsoft note says why the password stays |
| **Total** | | **29/40** | **Good** |

## Design Specificity Verdict

**LLM assessment**: The headline greets the person by name and the brand sits above: the first screen a new
technician sees reads as this product's. The «o» separators now share the sign-in page's hairline treatment.

**Deterministic scan**: CLI `[]`, exit 0; the overlay on `/invite?__fixtures`: "No anti-patterns found."; harmony ok
in both languages.

## Overall Impression

Right; the one confusing element is the read-only address drawn exactly like the password fields under it.

## What's Working

- The tab says "Welcome, Dana Okafor · Novieri RMM"; the token never reaches the tab, the URL or the query key.
- One guard for every way in, so a technician's password cannot be sent twice through two buttons.

## Priority Issues

- **[P2] The read-only address looks like an editable field.** Why: a person tries to correct it and nothing happens.
  It is there for the password manager (`autocomplete="username"`), so it must stay a field. Fix: draw it on the
  sunken surface (`--surface-2`) like every other read-only value; the general rule belongs to the kit
  (`.input-field[readonly]`, kit request). Suggested command: /impeccable polish.

## Persona Red Flags

**Jordan (First-Timer)**: clicks into the address to fix a typo; nothing changes, no explanation.

## Minor Observations

- None.

## Questions to Consider

- None.

Questions skipped: 1 priority issues
