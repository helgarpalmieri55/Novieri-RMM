---
target: "email intake (wave O2, 2.7): frontend/src/modules/email-intake/EmailIntakeAdminPage.tsx"
total_score: 30
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 2
target_identity: "file:frontend/src/modules/email-intake/EmailIntakeAdminPage.tsx"
target_fingerprint: "sha256:8b19c9c821dc36554ebb6e83571e49f70aae8673bd73d7b2918ba3f06d5d626a"
target_path: frontend/src/modules/email-intake/EmailIntakeAdminPage.tsx
timestamp: 2026-11-25T04-26-25Z
slug: les-email-intake-emailintakeadminpage-tsx-59975b67
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: email intake (`frontend/src/modules/email-intake/EmailIntakeAdminPage.tsx`, `/email-intake`)

Redesign plan, Task 7 (wave O2), "Cierre de cada oleada", step 3. Assessment A was written from the source and the
harmony captures (/email-intake: 1440x900, 390x844, 390x844-client, 641x900-client; es and en) before the detector
output; Assessment B followed. A and B in series, one context.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Mailbox state, last poll, quarantine counts |
| 2 | Match System / Real World | 3 | "Cómo crea tickets <buzón>" |
| 3 | User Control and Freedom | 3 | One save bar, Discard, guard |
| 4 | Consistency and Standards | 3 | Settings as form sections |
| 5 | Error Prevention | 3 | Reopen window refused before the server |
| 6 | Recognition Rather Than Recall | 3 | Category by name |
| 7 | Flexibility and Efficiency | 3 | Two mailboxes saved in one act |
| 8 | Aesthetic and Minimalist Design | 3 | Settings next to their mailboxes |
| 9 | Error Recovery | 3 | Refusal names the mailbox |
| 10 | Help and Documentation | 3 | Field help on both settings |
| | **Total** | **30/40** | |

## Anti-patterns verdict

Not slop. The prerequisites' numbered steps (`ol.steps`) are the shared prerequisites pattern.

## What works

- One draft, one save bar, one guard for every mailbox's settings; the connection and its secret stay in the dialog.
- The Microsoft return page now sits in the frame with its way back.

## Priority issues

- **P1** "Añadir buzón" was a second primary in the body beside the Microsoft panel's save. Fixed in polish: secondary (55378cf).
- **P1** A mailbox whose category the list does not carry showed the bare id ("cat-other"). Fixed: named "Una categoría que no está en la lista (cat-other)" (55378cf).
- **P2** "Guardar conexión con Microsoft 365" keeps its own save (the brief: the OAuth connection unchanged; it carries a write-only secret).

## Assessment B (detector)

- Static: `impeccable detect --json frontend/src/modules/email-intake` from the worktree root: **0 findings**.
- Browser (`npx vite` + `impeccable live-server`, Chromium on `/email-intake?__fixtures`, es and en, 1440 and 390): only
  system-wide findings on this route, none caused by the page: `undersized-ui-text` on 10.5px captions (DESIGN.md's
  Caption step, "The Floor Is The Ramp's Smallest Step"), `overused-font` Inter (the design's one face),
  `gray-on-color` `#8592ab` on `#1b2945` (the rail's dim ink, shell), `nested-cards` on the prerequisites steps. Proposed ignores for the coordinator are in
  the wave report.

## Questions

Settled by DESIGN.md, the brief and critica-respuestas.md; none open.

Questions skipped: 2 priority issues
