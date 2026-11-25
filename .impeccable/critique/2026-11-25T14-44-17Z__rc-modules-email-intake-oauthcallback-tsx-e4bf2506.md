---
target: "email intake, the Microsoft return page (final review I1, Appendix B E1): frontend/src/modules/email-intake/OAuthCallback.tsx"
total_score: 27
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 1
target_identity: "file:frontend/src/modules/email-intake/OAuthCallback.tsx"
target_fingerprint: "sha256:75f1471af4b4c2dc65729581c98f069c7c25abbd4c1649abbbc06ae2ff7fa2d9"
target_path: frontend/src/modules/email-intake/OAuthCallback.tsx
timestamp: 2026-11-25T14-44-17Z
slug: rc-modules-email-intake-oauthcallback-tsx-e4bf2506
closed: true
---
⚠️ DEGRADED: single-context (no sub-agent tool exposed)

# Critique: the Microsoft return page of Email intake (`frontend/src/modules/email-intake/OAuthCallback.tsx`, `/settings/email-intake/oauth/callback`)

Final review of project 3, finding I1 (Appendix B, E1: the wave's twelfth screen, adapted to `FormPage` in wave O2 and
never critiqued). Harmony excludes the route (`routes.mjs` `EXCLUDED`: it consumes an authorization code the gate does
not have), so Assessment A was written from the source, a jsdom render of the four states (done, cancelled, not
started, failed; en and es; axe in jsdom, color-contrast and region off) and a Chromium capture against a vite dev
server in its own process group (`?__fixtures`, the four states, en and es, 1440x900 and 390x844), before the detector
output. Assessment B followed. A and B in series, one context.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 2 | The tab says "Settings · Novieri RMM"; the outcome is a 12px coloured line |
| 2 | Match System / Real World | 3 | "Connected as support@…", "nothing changed" |
| 3 | User Control and Freedom | 3 | Back link and a way back after every outcome |
| 4 | Consistency and Standards | 2 | Filed under Settings; form-save vocabulary on a page with no form; a button that navigates |
| 5 | Error Prevention | 3 | Single-use state fired once under StrictMode; the code leaves the address bar |
| 6 | Recognition Rather Than Recall | 3 | Names the mailbox and the next step |
| 7 | Flexibility and Efficiency | 2 | The way back cannot be opened in a new tab; a tab indistinguishable from /settings |
| 8 | Aesthetic and Minimalist Design | 3 | One head, one panel, one sentence |
| 9 | Error Recovery | 3 | The server's reason, verbatim, and the way back |
| 10 | Help and Documentation | 3 | "Switch the mailbox on in Email intake when you are ready" |
| | **Total** | **27/40** | Good, with consistency gaps |

## Design Specificity Verdict

**LLM assessment.** The page is the console's own frame doing a small job well: the h1 says what just happened
("Microsoft 365 sign-in"), the back link and the button say where it belongs (Email intake), and the copy names the
mailbox and the one thing left to do. What is not product-specific is the outcome register: the three end states are
drawn three ways (green 12px `form-saved`, an unstyled paragraph, red 12px `form-error`), none with its kind's icon,
while the rest of the console says an outcome with the notice family and an icon (the auth screens' `AuthDone` and
`notice red`, the Microsoft sign-in return page next door). The shell around it files the page under Settings, because
the URL Microsoft returns to starts with `/settings`.

**Deterministic scan.** `impeccable detect --json frontend/src/modules/email-intake/OAuthCallback.tsx` (and the whole
module) from the repository root: `[]`, exit 0. No finding the review missed, no false positive.

**Visual overlays.** `impeccable live-server` with `detect.js` injected by Playwright into the page (en, done and failed,
1440 and 390): 2 findings at 1440 and 1 at 390, all system-wide and none caused by the page: `gray-on-color` `#8592ab`
on `#1b2945` (the rail's dim ink, DESIGN.md "Rail"), `overused-font` Inter (the design's one face). The same two every
wave recorded. No overlay is left on screen for anyone: the capture ran headless and the server was stopped.

## Overall Impression

A correct, quiet page whose words are right and whose frame is wrong. The single biggest opportunity is wayfinding: a
person back from Microsoft reads "Settings" in the tab, while the page says Email intake everywhere else.

## What's Working

- The security argument is in the code and holds: the code and state leave the address bar at once, the completion is
  an ordinary authenticated call, and a double mount cannot spend the single-use state twice.
- Every end state says what happened and what next in plain words, in both languages, with no internal vocabulary
  ("Connected as support@toeshee.example. Switch the mailbox on in Email intake when you are ready for its mail to
  become tickets.").
- It sits in the console's frame with its own head and the way back to Email intake (wave O2), so it no longer looks
  like a dead end after an external redirect.

## Priority Issues

- **[P1] The page is filed under Settings.**
  - *Why it matters:* the tab reads "Settings · Novieri RMM", the same as `/settings` (DESIGN.md: "a unique tab title on
    every page"; PRODUCT.md principle 1), and the Admin panel lights Settings, while the h1's context, the back link and
    the button all say Email intake. A person with several consoles open cannot tell this tab from Settings, and the
    rail contradicts the page.
  - *Fix:* give the route its place the way the shell already does for routes it lists nowhere (`NAV_UNLISTED`, as
    `/agent-push` and `/provisioning`): under Admin, named Email intake. The page puts its own name in front (general
    ruling 1b): "Microsoft 365 sign-in · Email intake · Novieri RMM". The URL cannot move: it is the redirect address
    registered with Microsoft.
  - *Suggested command:* /impeccable polish
- **[P2] The outcome is said in the save bar's register, three different ways, with no icon.**
  - *Why it matters:* the moment that confirms a credential was stored, or that nothing changed, or that it failed, is
    drawn at the small step as a form's save confirmation (`form-saved`), as unstyled text, or as a form error. Colour is
    the only thing that tells success from failure at a glance (The Second Carrier Rule), and the three states do not
    look like one family.
  - *Fix:* the notice family with the kind's icon, the way the auth screens and the Microsoft sign-in return page say an
    outcome: done in `.notice` with circle-check, cancelled in `.notice` with info-circle, failed in `.notice.red` with
    circle-x. Body step, 76ch measure, the same roles (`status`, `alert`).
  - *Suggested command:* /impeccable polish
- **[P2] The way back is a button that navigates.**
  - *Why it matters:* "Back to Email intake" calls `navigate()`: it cannot be middle-clicked, copied or opened in a new
    tab, and a screen reader announces a button for what is a link. The console's own rule (`PushNav`): a route is a
    link.
  - *Fix:* a link to `/email-intake` wearing the button's skin (`Link className="btn"`, as `BcpReport` does for its way
    back), same label, same place.
  - *Suggested command:* /impeccable polish

## Persona Red Flags

**Alex (Power User):** opens the Microsoft sign-in from Email intake in a second tab, comes back to a tab titled
"Settings · Novieri RMM" beside the real Settings tab, and cannot tell them apart; middle-clicking "Back to Email
intake" does nothing.

**Sam (Accessibility):** the outcome's kind is carried by green or red ink at 12px with no icon; the way back is
announced as a button. jsdom axe: 0 violations in all eight renders, so nothing structural is broken.

**Jordan (First-Timer):** connects a mailbox for the first time and lands on a page the rail says is Settings; the
sentence tells them to go to Email intake, which the rail does not light.

## Minor Observations

- The Admin panel lights the page by its path, so it keeps lighting Settings even once the crumb and the tab say Email
  intake; `/agent-push/credential-scopes` has the same property (it lights nothing). A shell change, not this page's.
- A `role="status"` paragraph is inserted with its text already in it, the console's pattern everywhere
  (`SecurityPage`, `NotificationsTab`, `EnrollmentPage`); some screen readers announce a live region only when it
  changes. Console-wide, not this page's to change alone.
- The failure prints the server's reason verbatim ("Not connected. {reason}"), as every module does through
  `describeSubmitError`; a server reason in English inside the Spanish sentence is the server's, not the page's.
- While working, the page draws only "Finishing the Microsoft sign-in…": no spinner, which is right for a call this
  short.

## Questions to Consider

Answered from DESIGN.md, the rulings and the existing pages, and recorded in `.impeccable/critica-respuestas.md`,
"La vuelta de Microsoft a Entrada de correo (revisión final, I1)":

1. Where does this page live in the shell: (a) under Email intake, by a `NAV_UNLISTED` entry under Admin, its tab
   "Microsoft 365 sign-in · Email intake · Novieri RMM"; (b) under Settings, where its URL is, with only its name in
   front; (c) a new URL outside `/settings`? **(a)**, as `/agent-push` and `/provisioning` are filed; the Admin
   panel keeps lighting by path (Settings), as it does for `/agent-push/credential-scopes`.
2. How is the outcome said: (a) the notice family with the kind's icon, as the auth screens; (b) the save bar's
   `form-saved` / `form-error` lines, as today; (c) a full empty state with an illustration-sized icon? **(a)**: The Second Carrier Rule, and `AuthDone` /
   `notice red` on the auth screens.
3. The way back: (a) a link in the button's skin, secondary, as `BcpReport`; (b) a primary link (the one next step);
   (c) drop it, the back link above the h1 is enough? **(a)**: refinement keeps the control and its weight; only
   its element changes.

## Polish (same day, before `close`)

- P1 fixed: `NAV_UNLISTED` files `/settings/email-intake/oauth/callback` under Admin, named Email intake, and the page
  names itself in front: the tab reads "Microsoft 365 sign-in · Email intake · Novieri RMM" ("Inicio de sesión de
  Microsoft 365 · Entrada de correo · Novieri RMM"). Tests: `App.crumb.test.tsx`, `test/subPageTitles.test.tsx`.
- P2 fixed: the three outcomes are notices with their kind's icon (done `notice` + circle-check, cancelled `notice` +
  info-circle, failed `notice red` + circle-x), same roles. Test: `oauth.test.tsx`, one per outcome.
- P2 fixed: "Back to Email intake" is a link to `/email-intake` in the button's skin. Tests: `oauth.test.tsx`,
  `email-intake.es.test.tsx`.
- Verified after the polish: Chromium on the dev server, the four states, en and es, 1440 and 390: no overflow, no
  console error, the tab as above; `impeccable detect --json frontend/src/modules/email-intake`: `[]`.
- Left as recorded: the Admin panel lights Settings (shell, by path); live regions inserted with their text
  (console-wide).
