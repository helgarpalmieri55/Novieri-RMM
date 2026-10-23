# ITMS UI/UX Audit — Auditor A
Target: https://itms.toeshee.com
Date: 2026-08-19
Routes assigned (16): /dashboard, /assistant, /devices, /health, /security, /patch, /deploy, /remote, /credentials, /backup, /provisioning, /site-servers, /service-desk, /portal, /notifications, /email-intake

---

## Findings

### [MEDIUM] Post-login redirect discards the originally requested deep link
- **Page:** /login (Sign in with SSO button) → lands on /devices
- **What happens:** Navigating directly to `https://itms.toeshee.com/dashboard` while unauthenticated redirects to `/login`. After clicking "Sign in with SSO", the app lands on `/devices` — not the `/dashboard` route that was originally requested. The requested path is not preserved as a `?next=` / `returnTo` parameter (URL at login is a bare `/login`).
- **Why it's a problem:** Any bookmarked or shared deep link (a specific device, a ticket link from an email notification) silently dumps the user on an unrelated page after login, forcing them to re-navigate and re-find context. Violates Nielsen #1 (visibility of system status) and #7 (flexibility/efficiency). This is especially costly for an ITSM tool whose notification emails will contain deep links to tickets.
- **Fix:** Capture the attempted path on the auth redirect (`/login?next=%2Fdashboard`) and restore it after a successful SSO callback. Fall back to `/dashboard` only when `next` is absent or non-local.
- **Evidence:** Navigated tab to /dashboard → URL became `https://itms.toeshee.com/login` with no query string (verified via `location.href` in the page context: `"url":"https://itms.toeshee.com/login"`). Clicking Sign in with SSO rendered "Devices & Inventory" at /devices.

### [MEDIUM] Login screen is a single unlabeled button with no context, no error surface, and no product/tenant identification
- **Page:** /login
- **What happens:** The entire login page is: heading "ITMS", subtext "IT management console", and one button "Sign in with SSO". The accessibility tree for the whole page is only 3 nodes (`heading "ITMS"`, `generic "IT management console"`, `button "Sign in with SSO"`). There is no indication of which identity provider will be used, no tenant/organisation name, no support/help link, no "trouble signing in?" affordance, and no region for auth error messages.
- **Why it's a problem:** If SSO fails (IdP down, user not provisioned, expired session), there is nowhere for an error to be displayed and no self-service recovery path — the user is stranded with a button that does nothing visible. Violates Nielsen #9 (help users recognise, diagnose and recover from errors) and #1. Naming the IdP is also a phishing-resistance signal users are trained to look for.
- **Fix:** Name the provider on the button ("Sign in with Okta" / "Continue with Microsoft Entra ID"), add a persistent `role="alert"` container for auth failures, and add a support contact / status-page link in the card footer.
- **Evidence:** `read_page` (filter: all) on /login returned exactly three refs and no form, no alert region, no links.

### [BLOCKER] Sign-out redirect is misconfigured — user is stranded on a raw Keycloak "Invalid redirect uri" error page with no way back
- **Page:** Global app header (`button.profile-btn`, "OA / Ops Admin / Operator", rect x:722 y:16 w:113 h:39) — reproduced from /dashboard
- **What happens:** A single left-click on the profile chip in the top-right immediately terminates the session and navigates to the Keycloak identity provider, which renders a full-screen dark error page: heading "IT-SUITE", panel "We are sorry..." with body text "Invalid redirect uri". The page contains **no link, no button and no navigation of any kind** — `document.querySelectorAll('button,a')` returned an empty array. The user cannot get back to ITMS except by manually retyping the URL. `document.body.innerText.includes('Sign out')` → false; `[role=menu],[role=dialog],.menu,.dropdown,.popover` count → 0, confirming no menu was ever shown.
- **Why it's a problem:** This is the terminal failure mode for an operator mid-incident. The OIDC RP-initiated logout is calling Keycloak's `end_session_endpoint` with a `post_logout_redirect_uri` that is not registered on the `it-suite-web` client, so Keycloak refuses the redirect. Every sign-out in this deployment dead-ends. It also leaks the internal realm branding ("IT-SUITE") and an implementation-level error string to end users. Violates Nielsen #9 (recover from errors), #3 (user control and freedom) and WCAG 2.4.5 (multiple ways) — there is literally no way out of the page.
- **Fix:** (1) Register `https://itms.toeshee.com/` (and `/login`) in the Keycloak client's **Valid post logout redirect URIs**, and pass a matching `post_logout_redirect_uri` + `id_token_hint` on the logout call. (2) Style the Keycloak error theme to at least carry a "Return to ITMS" link. (3) Verify the round trip in CI — a smoke test that signs in and signs out.
- **Evidence:** Screenshot ss_6957lc7bo — full-viewport grey polygon background, centred "IT-SUITE" wordmark, dark card reading "We are sorry..." / "Invalid redirect uri". Preceding network log confirms the Keycloak realm is `it-suite` with client_id `it-suite-web` and login redirect_uri `https://itms.toeshee.com/auth/callback`.

### [BLOCKER] Clicking the profile chip signs the user out instantly — no menu, no confirmation, no undo
- **Page:** Global app header (`button.profile-btn`) — present on every route
- **What happens:** The profile chip renders as an avatar + name + role ("OA / Ops Admin / Operator") with a card/button affordance. It carries no `aria-haspopup` and no `aria-expanded` attribute (verified: both `null`). A single click does **not** open an account menu — it performs the sign-out immediately and navigates away to the IdP. No confirmation dialog is shown and there is no undo.
- **Why it's a problem:** A profile chip in the top-right corner is a universally understood *menu* affordance (Gmail, GitHub, M365, Jira all open a menu). Users click it to check which account they are in, switch tenants, or open settings — and instead lose their session and any unsaved work on the page. Combined with the broken logout redirect above, one stray click ends the operator's session and strands them. Violates Nielsen #3 (user control and freedom), #4 (consistency and standards) and #5 (error prevention).
- **Fix:** Make the chip open a proper menu (`aria-haspopup="menu"`, `aria-expanded`, roving focus, Escape to close) containing Profile / Tenant / Settings / **Sign out** as a distinct destructive item. If a direct sign-out control is genuinely wanted, label it "Sign out" with a visible icon and confirm before destroying the session.
- **Evidence:** DOM probe before the click: `{"txt":"OA|Ops Admin|Operator","cls":"profile-btn","rect":{"x":722,"y":16,"w":113,"h":39},"ae":null,"haspopup":null}`. One click at (785,35) → immediate navigation to the Keycloak logout error page; post-click DOM had zero buttons/links and zero menu roles.

### [HIGH] Tenant switcher is a dead control — looks like a dropdown, does nothing
- **Page:** /dashboard (and every route) — global header, `button.tenant-switcher` labelled "Operations", rect x:524 y:16 w:190 h:39
- **What happens:** The header contains a bordered, icon-prefixed button reading "Operations" that is styled exactly like a workspace/tenant picker. Clicking it (coordinate 624,35) produced **no** visual change whatsoever — before/after screenshots are pixel-identical, no dropdown, no panel, no route change, no toast. It exposes no `aria-haspopup` and no `aria-expanded` (both `null`), and the DOM contains zero elements with `role="menu"`, `role="dialog"`, `.menu`, `.dropdown` or `.popover`.
- **Why it's a problem:** A prominent, chrome-styled control in the primary header that silently does nothing is the clearest possible violation of Nielsen #1 (visibility of system status) and #6 (recognition rather than recall) — the user cannot tell whether the click registered, whether the app hung, or whether they lack permission. In a multi-tenant IT platform the tenant switcher is a high-traffic control; a dead one erodes trust in the whole chrome. The duplicate `button.tenant-card` at the sidebar foot (x:12 y:821, "OP / Operations / Self-hosted enterprise") has the same problem.
- **Fix:** Either wire it to a real tenant menu (with `aria-haspopup="listbox"`, keyboard support, current-tenant checkmark), or — if this deployment is single-tenant — render it as static non-interactive text/badge with no button semantics, no hover state and no pointer cursor. Never ship a `<button>` with no handler.
- **Evidence:** Screenshots ss_0354defrw (after click) vs the pre-click capture are identical; DOM probe returned `{"txt":"Operations","cls":"tenant-switcher","ae":null,"haspopup":null}` and `menus: 0`.

### [MEDIUM] Two controls compete to represent the same tenant, in two different visual languages
- **Page:** Global chrome — sidebar footer `button.tenant-card` ("OP / Operations / Self-hosted enterprise") and header `button.tenant-switcher` ("Operations")
- **What happens:** The same tenant, "Operations", is rendered twice simultaneously: once as a dark pill card pinned to the bottom of the sidebar with a gold "OP" monogram and the subtitle "Self-hosted enterprise", and once as a light bordered button in the top bar with a document icon. Both are `<button>` elements; neither responds to clicks.
- **Why it's a problem:** Duplicated, differently-styled representations of one object force the user to work out that they are the same thing, and waste the two most valuable slots in the chrome. Violates Nielsen #8 (aesthetic and minimalist design) and #4 (consistency).
- **Fix:** Keep one tenant affordance. Convention is the sidebar footer for workspace identity and the header right for account; move the tenant switcher into the sidebar card and free the header slot for global search or the notification bell.
- **Evidence:** DOM probe listed both `tenant-card` (233x55 at sidebar foot) and `tenant-switcher` (190x39 in header) with identical primary label "Operations".

### [MEDIUM] Dashboard two-column grid leaves an ~800px empty void in the left column
- **Page:** /dashboard — "Recent signed jobs" card in the left column of the `div.grid.grid-2` (measured height 1391px)
- **What happens:** The right column stacks four cards (Patch compliance, Backup coverage, Service desk, Upcoming changes) while the left column holds only "Recent signed jobs" with 3 rows of content. The left card is stretched to match the full 1391px column height, so roughly 800px of pure white space sits below the last job row ("2 in flight · 0 needing attention"). At the bottom of the page the left column is entirely blank while the right column still has content.
- **Why it's a problem:** The user scrolls through a screen and a half of nothing to reach the last cards, and the emptiness reads as "content failed to load" rather than "this card is short". Violates Nielsen #8 and basic proximity/Gestalt grouping.
- **Fix:** Use a masonry/auto-flow-dense grid, or `align-items: start` so cards size to content, or rebalance so the tall left card (Recent signed jobs) shows more rows (it has room for ~25) rather than 3.
- **Evidence:** `getBoundingClientRect().height` of the `.grid.grid-2` container = 1391px containing 3 job rows; screenshots ss_7774aqjek and ss_8222usykn show the left column blank from y≈300 to the page bottom.

### [HIGH] Jobs queued for over 24 hours are reported as healthy — "Jobs needing attention: 0"
- **Page:** /dashboard — "Jobs needing attention" stat card and "Recent signed jobs" card
- **What happens:** Two `patch.scan` jobs are listed with status **queued**, timestamped 8/18/2026 11:04:18 AM and 8/18/2026 11:04:14 AM. The current date is 8/19/2026, so both have been queued for over 24 hours. The card footer reads "2 in flight · 0 needing attention" and the headline stat card reads "Jobs needing attention 0". The only managed endpoint (Toeshee-sentinel) is simultaneously shown as **offline**, last seen 8/18/2026 6:25:52 AM — i.e. the jobs were dispatched to a device that has been offline the whole time and can never run them.
- **Why it's a problem:** The dashboard's headline health metric actively asserts that nothing needs attention while two jobs are permanently stuck against a dead agent. That is worse than no metric: it suppresses the signal an operator opened the dashboard to find. A job in `queued` past its dispatch SLA, or targeted at an offline agent, is the canonical "needs attention" case. Violates Nielsen #1 (visibility of true system status).
- **Fix:** Define "needing attention" to include `queued` beyond a threshold (e.g. > 15 min, or > 2x the agent check-in interval) and any job whose target agent is offline. Surface age as a relative label ("queued 25h ago") and badge such rows amber. Add a "stuck jobs" filter on /jobs.
- **Evidence:** Dashboard text: "patch.scan / 8/18/2026, 11:04:18 AM / queued", "patch.scan / 8/18/2026, 11:04:14 AM / queued", "2 in flight · 0 needing attention"; stat card "Jobs needing attention 0"; device row "Toeshee-sentinel ... offline ... 8/18/2026, 6:25:52 AM".

### [MEDIUM] All timestamps are raw locale strings with no relative time and no timezone
- **Page:** /dashboard (Recent devices "LAST SEEN", Recent signed jobs) — pattern repeats across modules
- **What happens:** Every timestamp renders as `8/18/2026, 6:25:52 AM` — US `M/D/YYYY` with seconds precision and **no timezone indicator**. There is no relative form ("yesterday, 25h ago") anywhere on the dashboard, and no `title`/tooltip carrying the absolute UTC value.
- **Why it's a problem:** (a) An operator scanning "last seen" needs *staleness*, not a wall-clock reading — they have to do date arithmetic in their head to notice the agent has been dark for a day. (b) Seconds precision is noise at this density. (c) Absent a timezone, a distributed IT team cannot tell whether 6:25 AM is their morning or the server's. (d) `M/D/YYYY` is ambiguous for the majority of the world (8/18 is unambiguous only by luck; 8/9 is not). Violates Nielsen #2 (match between system and real world).
- **Fix:** Show relative time as the primary label ("25h ago") with the absolute ISO-8601 value with timezone in a `title` and in `<time datetime>`. Drop seconds. Let the user pick local vs UTC in settings and state which is in use.
- **Evidence:** Literal strings from the DOM: `8/18/2026, 6:25:52 AM`, `8/18/2026, 11:04:18 AM`, `8/18/2026, 11:04:14 AM`, `8/18/2026, 6:25:51 AM`.

### [MEDIUM] Body copy and table headers rendered at 9-11px
- **Page:** /dashboard (pattern is global — same tokens on every route)
- **What happens:** Computed font sizes sampled from the live DOM include **9px** (section eyebrows "OVERVIEW"/"FLEET", the "—" placeholder under the device name), **10px** ("Control plane", "Self-hosted enterprise", the `patch.scan` job name at 10px/800), and **11px** (stat-card labels "Managed endpoints", card descriptions "Live inventory from the outbound-only agent", "View all" links, table column headers).
- **Why it's a problem:** 9-11px is well below the ~16px baseline for sustained reading and below the 12px floor most design systems set for any text. Operators use this tool all day, often on high-DPI laptop screens where these render physically tiny. Small text also compounds the contrast failures below (WCAG allows a lower ratio only for *large* text — everything here is small text, so the strict 4.5:1 bar applies). Relates to WCAG 1.4.4 (resize text) and 1.4.12 (text spacing).
- **Fix:** Raise the type scale floor to 12px for metadata/eyebrows and 14px for any content the user reads (job names, card descriptions, table headers). The stat-card *label* is more important than its 25px number and should not be the smallest text in the card.
- **Evidence:** `getComputedStyle` sweep returned distinct sizes 9px, 10px, 11px in use for text content; e.g. `{"t":"patch.scan","fs":"10px","fw":"800"}`, `{"t":"Managed endpoints","fs":"11px","fw":"700"}`, `{"t":"—","fs":"9px"}`.

### [HIGH] Table column headers fail WCAG 1.4.3 contrast
- **Page:** /dashboard "Recent devices" table (`DEVICE / OPERATING SYSTEM / AGENT / LAST SEEN`); same header token used on the tables in every module
- **What happens:** Column header text computes to `rgb(135, 133, 144)` on a white/near-white card background at 11px — a contrast ratio of approximately **3.2:1**, against the 4.5:1 required for text below 18.66px. Secondary card text at `rgb(114, 113, 125)` on white computes to ~4.3:1, also below 4.5:1. The 9px eyebrow `rgb(119, 115, 128)` measured **4.24:1** (script-computed), likewise failing.
- **Why it's a problem:** Column headers are the key that makes a table readable; low-vision users and anyone on a glare-affected screen lose the mapping between value and meaning. Fails WCAG 2.2 SC 1.4.3 Contrast (Minimum), Level AA.
- **Fix:** Darken the muted-foreground token to at least `#5A5866` (≈6:1 on white) and the table-header token to the same or darker; re-run a token-level contrast check in CI (e.g. axe or a Style Dictionary lint) rather than eyeballing.
- **Evidence:** Computed-style sweep: table header `rgb(135,133,144)` 11px; muted text `rgb(114,113,125)` 11px used for "Managed endpoints", "Live inventory from the outbound-only agent", "—"; script-computed ratio 4.24 for `rgb(119,115,128)` at 9px/800 against `rgb(245,245,247)`.

### [LOW] Missing-value placeholder is a bare em-dash at 9px with no accessible meaning
- **Page:** /dashboard "Recent devices" — the line under "Toeshee-sentinel"
- **What happens:** The device row shows the hostname with a lone "—" beneath it (computed 9px, `rgb(114,113,125)`), occupying the slot where a secondary identifier (FQDN, owner, site) would go. Screen readers announce this as "em dash" or skip it; sighted users cannot tell which field is empty.
- **Why it's a problem:** An unlabeled placeholder communicates nothing about *what* is missing, so the user cannot act on it (e.g. "this device has no assigned owner"). Violates Nielsen #2 and #6.
- **Fix:** Either omit the line entirely when empty, or render the field name with an explicit empty state ("No FQDN recorded") and hide decorative dashes from assistive tech with `aria-hidden="true"`.
- **Evidence:** `{"t":"—","c":"rgb(114, 113, 125)","fs":"9px"}` appearing 8 times in the computed-style sweep of the page.

---
## /devices — Devices & Inventory

### [HIGH] Dispatching a signed job gives no confirmation of any kind — the form just disappears
- **Page:** /devices → row action "Run job" → "Dispatch" button (`button.btn.primary`, type=submit)
- **What happens:** With "Job type = hello" selected, clicking **Dispatch** causes the inline job panel to vanish. There is **no toast, no success message, no inline confirmation, no job ID, no row added to any list, and no navigation to the new job**. The device table is not refetched (no follow-up `GET /api/v1/jobs` or `/api/v1/devices` after the POST). From the UI alone it is indistinguishable from "the panel was cancelled". Network log proves the job *was* created: `POST https://itms.toeshee.com/api/v1/jobs → 201`.
- **Why it's a problem:** Dispatching a signed job executes code on a managed endpoint — one of the highest-consequence actions in the product. The operator has no way to know it worked, so the natural response is to click Dispatch again, creating duplicate jobs on the endpoint. The dashboard already shows two identical `patch.scan` jobs queued 4 seconds apart (11:04:14 and 11:04:18), which is exactly the duplicate-submit signature this missing feedback produces. Violates Nielsen #1 (visibility of system status) — the single most important heuristic for an action with side effects.
- **Fix:** On 201, show a success toast naming the job and job type with a "View job" link to the job detail, optimistically insert the job into a "Recent jobs" list on the page, and refetch. Disable the Dispatch button and show a spinner while the request is in flight. Keep the panel open on error with an inline message.
- **Evidence:** `read_network_requests` after clicking Dispatch: `url: https://itms.toeshee.com/api/v1/jobs, method: POST, statusCode: 201`. Screenshot ss_15207sar8 taken 2s after the click shows the page identical to its pre-click state with the panel gone and no notification anywhere. No console errors.

### [HIGH] Job dispatch accepts an empty Message with no validation — server returns 201
- **Page:** /devices → Run job panel, "Message" text input
- **What happens:** The Message field is pre-populated with the value `ping`. I cleared it to an empty string and clicked Dispatch. There was **no inline error, no toast, no `required` attribute** (`req: false`), no `aria-invalid`, and no browser-native constraint message. The request was sent and the server accepted it: `POST /api/v1/jobs → 201`.
- **Why it's a problem:** Either the Message is required — in which case an empty one should be blocked client-side with an inline error — or it is optional, in which case the field should say so and not be silently pre-filled with a value the user may not intend to send. As shipped, a mistyped/cleared field produces a real signed job on a real endpoint with empty parameters. Violates Nielsen #5 (error prevention) and #9, and WCAG 3.3.1 (Error Identification) / 3.3.3 (Error Suggestion) — no error is identified at all.
- **Fix:** Mark the field `required` with a visible "(required)" affix, block submit client-side, render the error inline beneath the field with `aria-describedby` + `aria-invalid="true"`, and add server-side validation returning 422 with a field-level message. If optional, label it "Message (optional)" and leave it blank by default.
- **Evidence:** DOM probe `{"ph":"","val":"ping","req":false,"al":null,"max":null,"type":"text"}`; after programmatically clearing to `""` (verified `len0`) and clicking Dispatch, network log shows `POST /api/v1/jobs → 201`.

### [HIGH] Jobs can be dispatched to an offline agent with no warning
- **Page:** /devices → "Run job" on Toeshee-sentinel (AGENT column shows a red dot and the label **offline**)
- **What happens:** The device row plainly marks the agent as `offline` (last seen 8/18/2026, 6:25:52 AM — over 24h before the current date of 8/19/2026), yet the "Run job" action is fully enabled, the panel opens normally, and Dispatch succeeds (201). No warning, no "this agent is offline, the job will queue until it checks in" note, and no indication of when the agent is expected back.
- **Why it's a problem:** The operator believes they have taken an action; in reality the job sits in `queued` indefinitely — which is precisely the state the two stale `patch.scan` jobs are in. Because the dashboard also reports "Jobs needing attention: 0", nothing ever tells the operator the work did not happen. Violates Nielsen #1 and #5.
- **Fix:** When the target agent is offline, either disable "Run job" with a tooltip explaining why, or keep it enabled but show a prominent inline warning in the panel ("Toeshee-sentinel has been offline for 25h. This job will queue and run at next check-in.") and set an explicit expiry/TTL on the queued job.
- **Evidence:** Row reads `Toeshee-sentinel | linux Ubuntu 26.04 LTS | amd64 | ● offline | ● ok | 8/18/2026, 6:25:52 AM | ▶ Run job`; the "Online now" stat card reads 0 and the "Offline" tab count reads 1. Dispatch still returned 201.

### [HIGH] Production job-type dropdown exposes a developer test job called "hello", pre-filled with "ping"
- **Page:** /devices → Run job panel → "Job type" `<select>`
- **What happens:** The dropdown contains exactly two options: **`hello`** and `inventory.collect`. `hello` is selected by default, and the Message field is pre-filled with the literal string `ping`.
- **Why it's a problem:** (a) `hello` is unmistakably a smoke-test/scaffolding job type left in the shipped UI — it is placeholder content in a production control plane, and it is the *default*, so the path of least resistance dispatches a meaningless job. (b) The two options are raw internal identifiers, not human language — an operator cannot tell what `inventory.collect` does, how long it takes, or whether it is disruptive. (c) The dropdown does not include the job types the rest of the product clearly supports (`patch.scan` appears in the dashboard job feed but cannot be launched here). Violates Nielsen #2 (match between system and the real world) and the general rule against shipping debug fixtures.
- **Fix:** Remove `hello` from production builds. Present job types as human-readable names with a one-line description and an icon ("Collect inventory — re-reads hardware, software and network interfaces. ~30s, non-disruptive"), grouped by category, and include the full supported set. Default to no selection so the user must make a deliberate choice.
- **Evidence:** `select.options` = `["hello|hello", "inventory.collect|inventory.collect"]`; Message input `value: "ping"`.

### [MEDIUM] The Run job panel is not a `<form>`, yet its button is `type="submit"` — Enter does not submit
- **Page:** /devices → Run job panel
- **What happens:** `document.querySelector('form')` returns **null** on the page while the panel is open, but the Dispatch button carries `type="submit"`. A submit button with no owning form is inert as a submit control, so pressing Enter while focus is in the Message field does nothing. The controls also have no `id`/`name` attributes (`selId: ""`, `selName: ""`, message input `id: ""`, `name: ""`).
- **Why it's a problem:** Keyboard users expect Enter to submit a two-field dialog; here they must Tab past Cancel to reach Dispatch. Non-form markup also forfeits native validation, autofill and browser-level submit semantics. Violates WCAG 2.1.1 (Keyboard) in spirit and Nielsen #4 (consistency with platform conventions).
- **Fix:** Wrap the panel in a real `<form>` with an `onSubmit` handler, give each control an `id`/`name`, and keep `type="submit"` on Dispatch and `type="button"` on Cancel.
- **Evidence:** `{"isForm": false, ..., "dispatchBtn":{"dis":false,"cls":"btn primary","type":"submit"}}`.

### [MEDIUM] The Run job panel is visually unstyled and escapes the card it belongs to
- **Page:** /devices → Run job panel (renders directly below the device table)
- **What happens:** Every other block on the page sits inside a white rounded card with padding and a border. The Run job panel renders as bare text and controls on the page background: the heading "Run job on Toeshee-sentinel" is unstyled body text (not a heading element), and "Job type [select] Message [input]" sit on a single line with no container, no padding, no border and no visual separation from the table above. The Cancel/Dispatch buttons float at the right edge, unaligned with the form controls on the left.
- **Why it's a problem:** The panel does not read as a distinct, modal-like task; it reads as page debris. It also breaks the card grammar the rest of the product establishes, so users do not perceive it as a bounded task with a beginning and an end. Violates Nielsen #4 (consistency and standards) and #8 (aesthetic and minimalist design).
- **Fix:** Render it in the same card component as the rest of the page (or as a proper modal/side-sheet), give the title a real `<h2>`, stack label-above-control, and left-align the button row with the form. Confirm the design-system form layout is applied.
- **Evidence:** Screenshot ss_8291q4l55 — table card ends at y≈513; "Run job on Toeshee-sentinel" and the field row render on the grey page background below it with buttons right-aligned at x≈1258.

### [MEDIUM] Filter tabs do not change the URL — filtered views cannot be linked, bookmarked or restored
- **Page:** /devices → tabs "All devices 1 / Online 0 / Offline 1 / Certificate attention 0"
- **What happens:** Clicking the "Offline" tab correctly filters the table, but `location.href` remains exactly `https://itms.toeshee.com/devices` — no query string, no hash. Reloading returns to "All devices"; the browser Back button does not undo the tab change. The same applies to the search box, whose text is also not reflected in the URL.
- **Why it's a problem:** In IT operations, "show me every offline endpoint" is a link you paste into a ticket or a chat channel. Losing filter state on reload and breaking Back are classic SPA regressions. Violates Nielsen #3 (user control and freedom) and #7 (flexibility and efficiency of use).
- **Fix:** Sync tab and search state to the query string (`/devices?state=offline&q=sentinel`) via `history.pushState`, hydrate from it on mount, and make Back/Forward traverse filter states.
- **Evidence:** After clicking the "Offline" tab and confirming the table filtered, `location.href` evaluated to `"https://itms.toeshee.com/devices"`.

### [MEDIUM] Tabs are missing `aria-controls`/`tabpanel` wiring and roving tabindex
- **Page:** /devices → `div[role=tablist]` with four `button[role=tab]`
- **What happens:** The tabs correctly expose `role="tab"` and `aria-selected` (`true` on the active tab, `false` on the rest) inside a `role="tablist"`. However **no element on the page has `role="tabpanel"`** (count = 0), every tab has `aria-controls: null`, and all four tabs have `tabIndex: 0` rather than the roving `0/-1` pattern.
- **Why it's a problem:** A screen-reader user is told "tab, selected" but there is no associated panel to navigate to, so the relationship between the tab and the filtered table is not conveyed. With all four tabs in the tab order, keyboard users must press Tab four times to get past the tab strip instead of using arrow keys, contrary to the WAI-ARIA Authoring Practices tabs pattern. Fails WCAG 1.3.1 (Info and Relationships) and 4.1.2 (Name, Role, Value).
- **Fix:** Give the table container `role="tabpanel"` with an `id`, point each tab's `aria-controls` at it, apply roving tabindex (active tab `0`, others `-1`), and handle Left/Right/Home/End arrow keys.
- **Evidence:** `{"tabs":[{"t":"All devices 1","sel":"true","ctrl":null,"ti":0},{"t":"Online 0","sel":"false","ctrl":null,"ti":0},{"t":"Offline 1","sel":"false","ctrl":null,"ti":0},{"t":"Certificate attention 0","sel":"false","ctrl":null,"ti":0}],"tablist":true,"panels":0}`.

### [MEDIUM] Tab counts are absolute totals and contradict the visible result set when a search is active
- **Page:** /devices → tab strip + search box
- **What happens:** With the search box containing `<b>x</b>` (or a 500-character string), the table shows the empty state "No devices match that filter." while the tab strip continues to read "**All devices 1** / Online 0 / Offline 1 / Certificate attention 0".
- **Why it's a problem:** The badge asserts there is 1 device in the currently selected tab while the body of that tab shows none. The user cannot tell whether the search failed, the data failed to load, or the filter is genuinely empty. Violates Nielsen #1 and the principle that a count and its list must agree.
- **Fix:** Recompute tab counts against the active search term, or move counts out of the tabs when a search is active and show a single result summary ("0 of 1 devices match 'x'").
- **Evidence:** Screenshot ss_42769qh9z — tab strip "All devices 1" above the empty state "No devices match that filter."

### [MEDIUM] No-results empty state offers no way to clear the search
- **Page:** /devices → search box + table empty state
- **What happens:** When a search returns nothing, the table (including its column headers) is replaced by an icon and the sentence "No devices match that filter." There is no "Clear search" button in the empty state and **no clear (×) affordance inside the search input** — the user must manually select the text and delete it. With a 500-character value the input scrolls horizontally (`scrollWidth > clientWidth`) and shows only the trailing characters, making manual clearing harder still.
- **Why it's a problem:** The dead-end state gives the user nothing to act on. Recovery from an over-narrow filter is one of the most frequent interactions in a list view. Violates Nielsen #3 and #9.
- **Fix:** Add a clear (×) button inside the search field whenever it has a value, and put a "Clear search" primary action plus the searched term inside the empty state ("No devices match 'x'. Clear search").
- **Evidence:** Screenshots ss_42769qh9z and ss_2188ewng0; `read_page` of the empty state contains no button other than the tabs and Export inventory; probe returned `{"len":500,"maxlength":null,"ovf":true}`.

### [LOW] Search input has no maxlength and accepts 500+ characters
- **Page:** /devices → search input (`aria-label="Search devices"`)
- **What happens:** The input has `maxlength: null` and accepted a 500-character string, which overflows the visible field.
- **Why it's a problem:** Minor, but unbounded input is passed to the filter (and, in list views that search server-side, to the API). Capping it prevents accidental paste of an entire document and keeps the control legible.
- **Fix:** Set `maxlength` to a sane bound (e.g. 128) for search fields.
- **Evidence:** `{"len":500,"maxlength":null}`.

### [LOW] Table header cells omit `scope="col"`
- **Page:** /devices table (and the same table component elsewhere)
- **What happens:** All seven `<th>` elements (`DEVICE, OPERATING SYSTEM, ARCHITECTURE, AGENT, CERTIFICATE, LAST SEEN, ACTIONS`) have `scope: null`.
- **Why it's a problem:** Without `scope`, assistive technology has to guess the header-to-cell association. For a simple single-header-row table most screen readers infer it correctly, so impact is low, but it is a one-attribute fix that makes the association explicit. Relates to WCAG 1.3.1.
- **Fix:** Add `scope="col"` to every column header in the shared table component.
- **Evidence:** `th` probe returned `scope: null` for all seven headers.

### [LOW] "ACTIONS" column header is announced but the actions have no per-row context
- **Page:** /devices table, ACTIONS column
- **What happens:** Each row's only action is a button labelled "Run job". With one row this is unambiguous; with a full fleet, a screen-reader user tabbing the column hears "Run job, Run job, Run job…" with no indication of which device each belongs to.
- **Why it's a problem:** Ambiguous link/button purpose in context. Relates to WCAG 2.4.4 (Link Purpose in Context) / 2.4.6 (Headings and Labels).
- **Fix:** Add `aria-label="Run job on Toeshee-sentinel"` (visible text stays "Run job") so each row's action is self-describing.
- **Evidence:** Button probe returned `{"t":"Run job","cls":"btn small","role":null}` with no `aria-label`.

### [LOW] Page has a single `<h1>` and no section headings
- **Page:** /devices (pattern repeats on /dashboard)
- **What happens:** The heading probe returns exactly one heading for the whole page: `H1: Devices`. The stat cards ("Managed endpoints", "Online now", "Certificate attention"), the tab strip, the table and the Run job panel title are all non-heading elements.
- **Why it's a problem:** Screen-reader users navigate by heading. With one heading per page, the heading shortcut is useless and the page's structure is invisible to assistive tech. Fails WCAG 1.3.1 and 2.4.10 (Section Headings, AAA) and hurts scannability for sighted users too.
- **Fix:** Promote card and section titles to `<h2>`/`<h3>` in document order, styled to match the current visual weight.
- **Evidence:** `[...document.querySelectorAll('h1,h2,h3,h4')]` → `["H1:Devices"]` on /devices and `["H1:Dashboard"]` on /dashboard.

---
## /devices/:id — Device detail (Toeshee-sentinel)

### [HIGH] Raw JSON blobs are rendered directly into the UI as table content
- **Page:** /devices/06a843da-c3e1-7908-8000-e503456f8459 → Hardware tab → "ATTRIBUTES" column
- **What happens:** **35 of the 105 table cells** on the page contain unparsed JSON printed verbatim, e.g. `{"cores":12}`, `{"model":"LOGICAL VOLUME","size_bytes":256022937600}`, `{"size_bytes":16093573120}`, and NIC rows up to 225 characters long such as `{"mac":"3c:a8:2a:15:6b:11","name":"eno2","addresses":[{"family":"ipv4","origin":"static","address":"172.22.104.28/29"},{"family":"ipv6","origin":"link_local","address":"fe80::3ea8:2aff:fe15:6b11%eno2/64"}]}`.
- **Why it's a problem:** This is the API response leaking into the presentation layer. The user must mentally parse JSON to learn a disk is 256 GB or a NIC has IP 172.22.104.28/29. Byte counts are unformatted (`256022937600` instead of "238 GiB" — note the Memory stat card *does* format correctly to "15.0 GiB", proving the formatter exists and simply is not applied here). Braces, quotes and escaped `%` interface suffixes are pure noise. Violates Nielsen #2 (match between system and the real world) and #8 (aesthetic and minimalist design).
- **Fix:** Render attributes as a definition list or a set of labelled chips per row type: for `disk` → "Model: LOGICAL VOLUME · Size: 238 GiB"; for `nic` → "eno2 · 172.22.104.28/29 · fe80::3ea8:2aff:fe15:6b11". Reuse the existing byte formatter. Keep a "View raw" disclosure for the JSON if engineers need it.
- **Evidence:** DOM probe: `{"jsonCells":35,"totalCells":105,"longestJson":225,"sampleJson":"{\"cores\":12}"}`. Screenshot ss_100582nox shows the ATTRIBUTES column filled with brace-delimited JSON across every visible row.

### [HIGH] Device detail page never shows whether the agent is online or offline
- **Page:** /devices/:id — header area (title, subtitle, OS line, four stat cards)
- **What happens:** A regex sweep of the entire `<main>` text for "online"/"offline" returns **false for both** — the words do not appear anywhere on the device detail page. The header shows only the hostname, a generic description, the OS string `linux Ubuntu 26.04 LTS`, and four stat cards (CPU, Memory, Serial, Last sweep). Meanwhile the device *list* prominently badges this same device `● offline`, and the page offers an enabled primary "Run job" button.
- **Why it's a problem:** Agent connectivity is the single most decision-relevant fact about an endpoint, and it is present on the list but lost on the detail page — the opposite of the expected progressive-disclosure direction. An operator who deep-links to this page (from a ticket, an alert, or the dashboard's "Recent devices") sees a healthy-looking page with a prominent purple "Run job" CTA and no hint that the endpoint has been unreachable for over 24 hours. Violates Nielsen #1 (visibility of system status).
- **Fix:** Put an agent-state badge next to the H1 (`● Offline · last seen 25h ago`), colour-coded and with a text label (not colour alone, per WCAG 1.4.1), and reflect it in the "Run job" button state per the offline-dispatch finding above.
- **Evidence:** `{"statusWords":{"offline":false,"online":false,"agent":true}}` over `document.querySelector('main').innerText`; `hasRunJobBtn: true`.

### [MEDIUM] Hardware inventory is 91% network interfaces, 20 of which are Docker veth pairs
- **Page:** /devices/:id → Hardware tab
- **What happens:** The Hardware table has 35 rows: `cpu` 1, `disk` 1, `memory` 1, **`nic` 32**. Twenty of those NIC rows are ephemeral Docker virtual interfaces (`veth7670993`, `veth30ffb86`, `vethf9b899d`, `veth6c0c03d`, `vethdb286cc`, `veth9252ec3`, …), each carrying only a link-local IPv6 address. The four real physical interfaces (`eno1`–`eno4`, the ones with actual IPv4 addresses like `172.22.104.2/29`) are buried among them in MAC-address sort order.
- **Why it's a problem:** The signal an operator needs — "what are this machine's addresses?" — is diluted 1:8 by container plumbing that changes on every container restart, making the inventory both unreadable and unstable (a diff between two sweeps would show dozens of spurious changes). The single flat "TYPE / IDENTIFIER / ATTRIBUTES" table also forces four completely different entity shapes into one schema. Violates Nielsen #8 and #6.
- **Fix:** Group the tab into sections (Processor, Memory, Storage, Network) with type-appropriate columns. Filter out `veth*`/`docker*`/`br-*` interfaces by default behind a "Show virtual interfaces (20)" toggle, and sort real NICs first by whether they hold a routable address.
- **Evidence:** Row-type tally from the DOM: `{"rowCount":35,"types":{"cpu":1,"disk":1,"memory":1,"nic":32},"vethRows":20}`.

### [MEDIUM] Breadcrumb "ITMS / Fleet" is not navigable — there is no link back to the device list
- **Page:** /devices/:id (and every detail page) — the header breadcrumb
- **What happens:** The top bar renders `ITMS / Fleet` above the page title, styled as a breadcrumb trail with the second segment in accent purple. A DOM query for anchors within the header returned an **empty array** — neither segment is a link. There is also no "Back to devices" control anywhere on the page. The only route back to the list is the sidebar's "Devices & Inventory" item, which loses any filter/search the user came from.
- **Why it's a problem:** A coloured breadcrumb that is not clickable is a false affordance, and detail pages without a back path are a classic navigation dead end. Violates Nielsen #3 (user control and freedom) and #4, plus WCAG 2.4.8 (Location).
- **Fix:** Make the breadcrumb a real `<nav aria-label="Breadcrumb">` with an ordered list of links: `ITMS / Fleet / Devices / Toeshee-sentinel`, where "Devices" returns to the list preserving the previous filter state.
- **Evidence:** `[...document.querySelectorAll('header a, [class*=crumb] a')]` → `[]`; `crumbText: []`.

### [MEDIUM] "Serial" stat card shows a bare em-dash given equal visual weight to real data
- **Page:** /devices/:id → stat card row (CPU, Memory, Serial, Last sweep)
- **What happens:** The Serial card renders a large "—" in the same 25px+ heavy type used for the real values ("15.0 GiB", the CPU model). Nothing explains why the serial is missing or how to populate it.
- **Why it's a problem:** A quarter of the most prominent real estate on the page is spent announcing an absence, in a style that reads as data. Serial number matters for warranty and asset management, so "we don't have it" is actionable information that is being presented as a shrug. Violates Nielsen #1 and #8.
- **Fix:** Render missing values in muted, smaller type with an explanation and an action — "Not reported by the agent · Learn why" or "Add manually" — or drop the card and surface serial only when present.
- **Evidence:** Screenshot ss_100582nox, third stat card: label "Serial", value "—". Page text: `Serial\n—`.

### [LOW] "Last sweep" puts a full timestamp into a numeric stat-card slot, where it wraps to two lines
- **Page:** /devices/:id → "Last sweep" stat card
- **What happens:** The fourth card displays `8/18/2026, 6:25:52 AM` in the large stat-value type, which overflows the card width and wraps onto two lines ("8/18/2026," / "6:25:52 AM"), breaking alignment with the three cards beside it.
- **Why it's a problem:** Stat cards are designed for short scannable values; a wrapped timestamp is neither scannable nor aligned, and — as with every other timestamp in the product — it fails to convey the staleness that actually matters (this sweep is over a day old).
- **Fix:** Show "25h ago" as the stat value with the absolute timestamp as the card's secondary line or `title`.
- **Evidence:** Screenshot ss_100582nox — the "Last sweep" value renders across two lines while CPU/Memory/Serial values sit on one.

### [HIGH] Queued jobs cannot be cancelled, inspected, or retried — the Jobs tab is read-only text
- **Page:** /devices/:id → Jobs tab
- **What happens:** The tab lists four jobs (`hello` queued 8/19/2026 6:55:39 AM — the one I dispatched; `patch.scan` queued 8/18 11:04:18; `patch.scan` queued 8/18 11:04:14; `inventory.collect` succeeded 8/18 6:25:51) in a three-column table `TYPE / STATUS / ISSUED`. Every row contains **zero buttons and zero links** (`rowBtns: [0,0,0,0]`), and row `cursor` computes to `auto` on all four — the rows are not clickable. There is no Cancel, no Retry, no "view output", no job ID, no target, no duration, and no error column.
- **Why it's a problem:** Three jobs are stuck queued against an agent that has been offline for over a day and there is no way to clear them. An operator who dispatched a job by mistake — very easy to do given the silent-success and no-validation findings above — has no undo. When a job eventually fails there is nowhere to see why. Violates Nielsen #3 (user control and freedom — "emergency exit"), #1, and #9.
- **Fix:** Add a per-row action menu with Cancel (for `queued`/`running`), Retry (for `failed`), and Copy job ID; make the row link to a job detail page showing parameters, signature, target, stdout/stderr and timing. Add a `RESULT` column.
- **Evidence:** `{"rowBtns":[0,0,0,0],"clickable":["auto","auto","auto","auto"],"cols":["TYPE","STATUS","ISSUED"]}`. I could not complete the assigned "cancel a job" test because **no cancel affordance exists anywhere in the product for a queued job.**

### [HIGH] The same device reports two different "last contact" times in two different date formats
- **Page:** /devices/:id → "Last sweep" stat card vs Health tab
- **What happens:** The stat card reads **`Last sweep 8/18/2026, 6:25:52 AM`** and the device list's LAST SEEN column agrees (`8/18/2026, 6:25:52 AM`). The Health tab on the same page states **`Last contact 2026-08-18 13:24`** — a seven-hour difference — and renders it in a completely different format (ISO-style `YYYY-MM-DD HH:mm`, 24-hour, no seconds) from every other timestamp in the product (US `M/D/YYYY, h:mm:ss AM/PM`).
- **Why it's a problem:** Two authoritative-looking "when did we last hear from this machine" values that disagree by seven hours destroy trust in the inventory — the operator cannot tell which subsystem is right or whether one is stale. The format divergence compounds it by making the two values hard to compare at a glance. If "last sweep" (inventory collection) and "last contact" (agent heartbeat) are genuinely different concepts, nothing in the UI says so. Violates Nielsen #4 (consistency and standards) and #2.
- **Fix:** Either reconcile to a single source of truth, or label the two concepts explicitly and adjacently ("Last inventory sweep" vs "Last agent heartbeat") with a tooltip explaining the difference. Standardise on one timestamp format and one timezone across every module.
- **Evidence:** Stat card text `Last sweep\n8/18/2026, 6:25:52 AM`; Health tab text `Last contact 2026-08-18 13:24, outside the 15 min offline window.`

### [MEDIUM] Patch tab exposes the raw enum value `not_applicable` as a user-facing badge label
- **Page:** /devices/:id → Patch tab → "By state" card
- **What happens:** The state breakdown lists badges `missing`, `approved`, `installed`, `failed`, **`not_applicable`** — the last one printed with its underscore, unlike its four neighbours which happen to be single words. All are lower-case machine identifiers rather than sentence-case labels.
- **Why it's a problem:** `not_applicable` is visibly a database enum, not English. It sits in the same badge row as human-readable words, so the inconsistency is unmissable. Violates Nielsen #2 (speak the users' language).
- **Fix:** Map every enum to a display string at the presentation layer: "Not applicable", "Missing", "Approved", "Installed", "Failed". Apply the same mapping to job types (`inventory.collect` → "Collect inventory") and severity levels.
- **Evidence:** Screenshot ss_3041wpbas — "By state" card badges read `missing / approved / installed / failed / not_applicable`.

### [MEDIUM] "Reboot" card occupies a full third of the Patch tab to display one badge
- **Page:** /devices/:id → Patch tab → left card
- **What happens:** A card titled "Reboot" contains a single small badge reading "Up to date" at the top, then roughly 130px of empty white space. It is sized as an equal third of a three-column grid alongside the "By state" and "By severity" cards, which each hold five rows of data.
- **Why it's a problem:** Grid-enforced equal columns waste a third of the tab and make the sparse card look like it failed to load. Also, "Reboot: Up to date" is semantically odd — the reboot *state* is what is being reported (i.e. "No reboot required"), not its up-to-dateness.
- **Fix:** Let cards size to content or collapse the Reboot state into the header as a single inline chip ("No reboot pending"). Reword to state the condition directly.
- **Evidence:** Screenshot ss_3041wpbas — the "Reboot" card spans x≈303-620 with content ending at y≈525 and the card bottom at y≈655.

### [MEDIUM] Empty states are implemented three different ways across the tabs of a single page
- **Page:** /devices/:id → Software / Changes / Docs / Deploy / Provisioning / Backup tabs
- **What happens:** Six tabs on the *same page* render "no data" six different ways: **Software** → plain sentence, table removed ("No software inventory collected yet."); **Changes** → plain sentence, table removed ("No inventory changes recorded for this device."); **Docs** → plain sentence, table removed ("No documentation linked to this device."); **Deploy** → the table and its six column headers are kept and the message is stuffed into a row, leaving five trailing empty cells (page text: `DEPLOYMENT\tPACKAGE\tACTION\tSTATE\tEXIT CODE\tFINISHED\nNo deployments recorded for this device.\t\t\t\t\t`); **Provisioning** → a card of field labels each showing "—" plus a badge "not enrolled"; **Backup** → a card of field labels showing "—", the literal word "none" for Last good point, plus badges "not verified"/"not tested". The device list's empty state uses yet another treatment (centred icon + sentence).
- **Why it's a problem:** The user must re-learn "what does nothing look like?" on every tab, and the em-dash/`none`/`not enrolled` mixture makes it impossible to tell "not collected yet" from "collected, and the answer is nothing" from "feature not configured". Violates Nielsen #4 (consistency and standards) — and empty states are exactly where a new user forms their model of the product.
- **Fix:** Build one `<EmptyState>` component (icon, one-line explanation of *why* it is empty, and a primary action such as "Run inventory sweep" / "Assign a backup policy"). Never leave a header row with a stub row beneath it. Standardise the missing-value token to a single treatment.
- **Evidence:** Verbatim tab bodies captured above, per tab.

### [LOW] "Last good point: none" mixes a lowercase word into a column of em-dashes
- **Page:** /devices/:id → Backup tab
- **What happens:** The Backup status card shows `Last run state —`, `Last run finished —`, `Repository —`, but `Last good point` renders the lowercase literal **`none`** instead of the em-dash used by its three neighbours in the same row.
- **Why it's a problem:** Two different renderings of "no value" side by side in one card implies a semantic difference that does not exist, and `none` reads like an unmapped API value. Violates Nielsen #4.
- **Fix:** Use one empty-value token throughout, and for backup specifically prefer an explicit, alarming phrasing — "No recovery point" — since this field is the whole point of the module.
- **Evidence:** Screenshot ss_6167qog16 — the Backup status card row reads `Last run state / — · Last run finished / — · Repository / — · Last good point / none`.

---
## /assistant + /assistant/settings — AI Admin Copilot

### [HIGH] "Save" on Assistant settings does nothing and says nothing — no request, no validation message, no feedback
- **Page:** /assistant/settings → "Save" button
- **What happens:** With Model provider = "None selected" and both checkboxes unchecked, clicking **Save** results in: no toast, no inline error, no `role="alert"` or `role="status"` element anywhere (`alerts: []`), the button stays enabled (`disabled: false`), the page text is byte-identical before and after, and — critically — **no write request is issued**. The network log for `/api/v1/ai/*` after the click contains only `GET /ai/status`, `GET /ai/injection-flags`, `GET /ai/settings`, `GET /ai/providers`. There is no `PUT`/`POST`/`PATCH` at all.
- **Why it's a problem:** The user cannot distinguish "saved successfully", "blocked because you must pick a provider and tick the acknowledgement", and "the button is broken". If validation is silently rejecting the submit, that is a textbook WCAG 3.3.1 (Error Identification) failure — an error was detected and nothing was communicated. Violates Nielsen #1 and #9.
- **Fix:** Validate on submit and render inline errors next to each offending control with `aria-invalid` and `aria-describedby`, move focus to the first error, and surface a live-region summary. On success, show a confirmation and update the "the AI assistant is turned off" banner. Disable Save until the form is valid *and* explain why it is disabled.
- **Evidence:** `read_network_requests` filtered to `/api/v1/ai` after the click returns 10 entries, all `method: GET`. Post-click DOM probe: `{"alerts":[],"saveDis":false}` and unchanged `main` text.

### [HIGH] Model provider dropdown is empty — the assistant cannot be enabled and the UI never says why
- **Page:** /assistant/settings → "Model provider" `<select>`
- **What happens:** The select contains exactly one option: `value=""`, text **"None selected"**. There is no configured provider to choose. `GET /api/v1/ai/providers` returns 200 (so the call succeeds — the list is genuinely empty). The page offers no link to add a provider, no instructions, and no explanation; the surrounding copy simply says "a self-hosted, OpenAI-compatible provider on your own network is the default".
- **Why it's a problem:** The feature is unreachable from its own settings page and the dead end is unexplained. The user ticks the acknowledgement, ticks "Enable the assistant", presses Save, and nothing happens (per the finding above) with no hint that the missing provider is the blocker. Violates Nielsen #1, #9 and #10 (help and documentation).
- **Fix:** When the provider list is empty, replace the select with an explanatory empty state and a primary action — "No model provider is configured. Add a provider →" linking to the server/provider configuration — and describe the self-hosted OpenAI-compatible setup inline or via a docs link.
- **Evidence:** `select.options` = `["|None selected"]` (single entry, empty value); `GET /api/v1/ai/providers → 200`.

### [HIGH] All three form controls on Assistant settings are programmatically unlabeled
- **Page:** /assistant/settings — Model provider `<select>`, "I understand what leaves this network" checkbox, "Enable the assistant for this organization" checkbox
- **What happens:** The page contains three `<label>` elements with the correct visible text ("Model provider", "I understand what leaves this network, as listed above.", "Enable the assistant for this organization"), but **none of them is associated with its control**: the inputs have no `id`, the labels have no `for`, and no input is nested inside a label (`input.closest('label')` → false for all three). Result: 3 of 3 controls have no accessible name (`unlabeled: 3`).
- **Why it's a problem:** A screen-reader user hears "combo box" and "checkbox, not checked" twice, with no indication of what they control — and one of those checkboxes governs whether real customer data (hostnames, ticket bodies, spend figures — enumerated on this very page) is transmitted to an external model provider. Consenting to a data-egress term you cannot hear is a serious accessibility and compliance failure. Clicking the visible label text also does not toggle the checkbox, which enlarges the effective hit target problem. Fails WCAG 1.3.1 (Info and Relationships), 3.3.2 (Labels or Instructions) and 4.1.2 (Name, Role, Value).
- **Fix:** Give each control an `id` and each label a matching `for` (or wrap the control in its label). Add `aria-describedby` from the acknowledgement checkbox to the "What leaves this network" list so the disclosure is announced with the control.
- **Evidence:** `labelCount: 3`, labels `["Model provider","I understand what leaves this network, as listed a","Enable the assistant for this organization"]`; inputs probe `unlabeled: 3`, every entry `{"al":null,"lab":false}`.

### [MEDIUM] The feature is called three different things in three adjacent places
- **Page:** /assistant — sidebar item, page header, eyebrow, H1 and H2
- **What happens:** The sidebar nav item reads **"AI Admin Copilot"**. The top-bar page title also reads **"AI Admin Copilot"**. The eyebrow above the title reads **"AI"**. The `<h1>` reads **"Assistant"**. The card `<h2>` immediately below it also reads **"Assistant"**. The body copy calls it "the assistant"; the settings route is `/assistant/settings` and its heading is "Assistant settings".
- **Why it's a problem:** A user who wants "the Copilot" cannot find "Assistant" and vice versa; documentation, support tickets and search all fracture across two names. It also produces a visibly duplicated heading — `H1: Assistant` directly above `H2: Assistant` — which screen-reader users hear twice in a row. Violates Nielsen #4 (consistency and standards).
- **Fix:** Pick one name and use it in the nav, header, headings, body copy, URL and docs. Remove the redundant `H2` (the card is the only content on the page, so the `H1` already names it).
- **Evidence:** Heading probe on /assistant: `["H1:Assistant","H2:Assistant"]`; sidebar link text "AI Admin Copilot" → `/assistant`; top bar "AI Admin Copilot"; eyebrow "AI".

### [MEDIUM] Disabled-feature message is lowercase, unpunctuated, and offers no path forward for the operator
- **Page:** /assistant — the only card on the page
- **What happens:** The card body reads verbatim: **"the AI assistant is turned off for this organization"** — no leading capital, no full stop, no icon, no explanation of who can turn it on. Below it sits a bare text link "Assistant settings" with no button styling.
- **Why it's a problem:** The sentence casing marks it as an unpolished developer string rather than product copy, and it appears alongside a properly capitalised, punctuated page description directly above it — so the inconsistency is visible in a single glance. The plain link is a weak affordance for what is the page's only action. Violates Nielsen #4 and #9.
- **Fix:** "The AI assistant is turned off for this organization." Add an icon, state the required role ("An organization administrator can enable it"), and promote the action to a real button: "Open assistant settings".
- **Evidence:** Page text: `Assistant\n\nthe AI assistant is turned off for this organization\n\nAssistant settings`; the link is `a[href="/assistant/settings"]` with no button class.

### [LOW] Untested: enabling the assistant
- **Page:** /assistant/settings
- **What happens:** I deliberately did **not** attempt to enable the assistant or tick the data-egress acknowledgement, because doing so is an organisation-wide configuration change that would authorise transmission of customer data (hostnames, ticket bodies, spend figures) to an external model provider.
- **Why it's a problem:** N/A — recorded for coverage completeness.
- **Fix:** N/A. The success path of this form should be verified by the owner.
- **Evidence:** Only the no-op Save (provider unselected, both boxes unchecked, assistant remaining off) was exercised.

---
## Authentication / session (root cause confirmed)

### [BLOCKER] Confirmed root cause of the broken sign-out: Keycloak returns HTTP 400 to the logout call because `post_logout_redirect_uri` is not registered
- **Page:** Global — RP-initiated logout triggered from the header profile chip
- **What happens:** The application issues, correctly, a fully-formed OIDC RP-initiated logout with a valid `id_token_hint`:
  `GET https://itms.toeshee.com/realms/it-suite/protocol/openid-connect/logout?id_token_hint=<JWT>&post_logout_redirect_uri=https%3A%2F%2Fitms.toeshee.com`
  Keycloak responds **HTTP 400** and renders the "We are sorry... Invalid redirect uri" page. So the client code is right and the **Keycloak client configuration is wrong**: `https://itms.toeshee.com` is absent from the `it-suite-web` client's *Valid post logout redirect URIs*.
- **Why it's a problem:** Sign-out is 100% broken for every user of this deployment, and the failure destroys the local session first and *then* fails the redirect — so the user is both signed out and stranded. Recovery requires manually retyping the app URL, and because the IdP SSO session is also gone, the next visit demands a full username/password re-authentication.
- **Fix:** In the Keycloak admin console, on realm `it-suite`, client `it-suite-web`, add `https://itms.toeshee.com` and `https://itms.toeshee.com/*` to **Valid post logout redirect URIs** (the login-side *Valid redirect URIs* is already correct — `https://itms.toeshee.com/auth/callback` works). Then add an end-to-end sign-in → sign-out smoke test.
- **Evidence:** Network log entry: `url: .../openid-connect/logout?id_token_hint=eyJhbGciOiJSUzI1NiIs...&post_logout_redirect_uri=https%3A%2F%2Fitms.toeshee.com | method: GET | statusCode: 400`. Login-side auth call `.../openid-connect/auth?client_id=it-suite-web&redirect_uri=https%3A%2F%2Fitms.toeshee.com%2Fauth%2Fcallback&...&code_challenge_method=S256` returns 200, and `POST .../openid-connect/token` returns 200 — proving the login half of the config is fine while the logout half is not.

### [HIGH] A failed SSO round trip returns the user to /login with no error message — an invisible auth failure loop
- **Page:** /login → "Sign in with SSO"
- **What happens:** After the IdP session was destroyed, clicking "Sign in with SSO" navigates to Keycloak (confirmed: `GET /resources/.../login/keycloak.v2/js/authChecker.js` → 200, i.e. the Keycloak login page was served) and the browser ends up back at `https://itms.toeshee.com/login` showing the identical card — heading "ITMS", subtext "IT management console", one button. **No error text, no banner, no toast, no reason.** Repeating the click reproduces the loop. `document.querySelectorAll('input')` on the returned page is empty, so there is no form and nothing to act on.
- **Why it's a problem:** The user clicks Sign in, is bounced around, and lands exactly where they started with zero explanation. They cannot tell whether they are unauthorised, whether the IdP is down, whether their session expired, or whether the button is broken — the three states are visually identical. This is the concrete manifestation of the "no error surface on the login page" defect noted earlier. Violates Nielsen #1 and #9, and WCAG 3.3.1.
- **Fix:** Handle the `/auth/callback` error branch: read the OIDC `error`/`error_description` parameters and render them in a `role="alert"` on the login card ("Sign-in failed: your session has expired. Try again."). Distinguish "cancelled", "access_denied", "IdP unreachable" and "session expired", and add a retry plus a support link.
- **Evidence:** Three consecutive click-and-wait cycles (5s and 9s waits) each ended at `{"href":"https://itms.toeshee.com/login","title":"ITMS","inputs":[],"txt":"ITMS\n\nIT management console\n\nSign in with SSO"}`. Navigating directly to /health during this state also lands on /login.

### [MEDIUM] Access/ID token lifetime is 5 minutes, and the app drops to /login rather than recovering
- **Page:** Global session handling
- **What happens:** The ID token issued to this client decodes to `iat: 1787140133`, `exp: 1787140433` — a **300-second (5 minute)** lifetime. Repeated `POST /realms/it-suite/protocol/openid-connect/token` calls in the network log show the app does perform token exchanges. However, when the session could not be renewed, the app responded by hard-redirecting to `/login` with no warning, no "your session expired" message, and no preservation of the route the user was on.
- **Why it's a problem:** With a 5-minute token, any interruption in silent renewal ejects the operator mid-task. Combined with the deep-link loss (post-login always lands on /devices) and the silent failure above, an expiry costs the user their place and their context with no explanation. Violates Nielsen #1 and #9.
- **Fix:** Refresh proactively before expiry, and on unrecoverable failure show a non-destructive "Your session expired — sign in to continue" modal that returns the user to the exact route (and ideally the exact form state) afterwards. Consider raising the token lifetime or lengthening the refresh window for an internal ops console.
- **Evidence:** ID token payload from the logout URL: `{"exp":1787140433,"iat":1787140133,"auth_time":1787136344,...,"preferred_username":"opsadmin","org":"toeshee"}`; multiple `POST .../openid-connect/token → 200` entries; subsequent hard redirects to `/login`.
- **Caveat:** Two other browser tabs on this host were on the same origin during the audit and share the Keycloak SSO session, so I cannot fully isolate whether this particular drop was caused by idle expiry or by the shared-session logout. The 5-minute token lifetime and the absence of any expiry messaging are directly observed and are not affected by that caveat.

---
## Strengths

1. **Genuinely outstanding UX writing on the device Health tab.** Instead of a green tick, it says: "No health profile — agent offline. No profile resolves to this device, so nothing is being checked on it. **This is not the same as healthy.**" and "No open alerts — but this device is not reporting, so that is not evidence of health." This is honest, anti-false-confidence design of a quality most commercial RMM tools never reach. It should be the model for the rest of the product (and directly contradicts the dashboard's "Jobs needing attention: 0", which commits exactly the sin this tab warns against).
2. **Exemplary, specific privacy disclosure on /assistant/settings.** The page enumerates precisely what leaves the network ("The administrator's question text", "Tool results: real customer data. Hostnames, ticket subjects and bodies, ... spend figures") and what never does ("Vault material — stored credentials and license keys", "Attachment bytes, session recordings and agent certificates"), then adds the rare and admirable line: "No-training and no-retention terms with a hosted provider are contractual. This platform cannot enforce them and does not pretend to." Refusing to overstate your own guarantees is a real trust asset.
3. **Correct output escaping — no XSS in the search path.** Typing `<b>x</b>` into the /devices search rendered the literal string in the input and filtered on it; nothing was interpreted as markup and no element was injected into the DOM.
4. **Modern, correct OIDC login implementation.** Authorization Code flow **with PKCE** (`code_challenge_method=S256`), proper `state`, and a server-side token exchange — the right choice for a browser SPA, and notably better than the implicit-flow patterns still common in self-hosted tools. The login half of the config works flawlessly.
5. **Clean, consistent visual system with a coherent card grammar.** Rounded cards, a restrained purple accent, subtle corner gradients on stat tiles, and a legible dark sidebar produce a product that looks considerably more polished than typical self-hosted IT tooling. The design language is applied consistently enough that the exceptions (the unstyled Run job panel) stand out immediately.
6. **Proper document landmarks.** Every page exposes `ASIDE`, `NAV`, `HEADER` and `MAIN` landmarks, so screen-reader users can jump to the main content region — a basic that a great many dashboards still miss.
7. **Zero JavaScript console errors across every page visited.** `read_console_messages` with `onlyErrors: true` returned no errors on /dashboard, /devices, /devices/:id, /assistant and /assistant/settings.
8. **Fast, all-200 API layer with sensible resource-oriented routes.** Every one of the ~48 XHRs observed returned 200/201, with a clean scheme (`/api/v1/devices/:id/inventory`, `/api/v1/patch/devices/:id/status`, `/api/v1/deploy/devices/:id/deployments`). No 4xx/5xx and no visibly slow calls.
9. **Byte formatting exists and is correct where used.** The Memory stat card renders `15.0 GiB` from a raw `16093573120` — the formatter is right, it simply is not applied to the inventory table.
10. **Server-side filtering scopes are surfaced honestly in the copy.** The dashboard describes itself as "composed from each module's own query, and showing only what this account may read", and /assistant explains it is "re-entered with your own bearer token and has no permission you don't have" — accurate, security-literate explanations of RBAC behaviour.

## Cross-cutting patterns

1. **Silent mutations.** Every state-changing action tested produced no feedback: dispatching a job (`POST /api/v1/jobs → 201`) showed no toast and refetched nothing; Save on /assistant/settings issued no request and showed no message. There is no toast/snackbar system anywhere in the product — `[role=alert]`, `[role=status]`, `.toast` and `.alert` returned zero elements on every page checked. This is the single highest-leverage fix: one global notification component would close a large fraction of the findings above.
2. **Two date formats, no relative time, no timezone.** `M/D/YYYY, h:mm:ss AM/PM` in every table and stat card, but `YYYY-MM-DD HH:mm` in the device Health tab — and the two disagree by seven hours for the same device. Nothing anywhere shows staleness ("25h ago"), which is the form an operator actually needs.
3. **Empty states implemented ad hoc, at least six ways.** Centred icon + sentence (device list, dashboard cards); bare sentence with the table removed (Software, Changes, Docs); message stuffed into a table row leaving trailing empty cells (Deploy); em-dash fields (Provisioning, Backup); the literal lowercase word `none` (Backup "Last good point"); badge-style ("not enrolled", "not verified", "not tested"). No shared component and no consistent distinction between "not collected", "none exist" and "not configured".
4. **Raw machine values leak into the UI.** Unparsed JSON in 35 of 105 device-inventory cells; enum identifiers as user-facing labels (`not_applicable`, `inventory.collect`, `hello`); unformatted byte counts (`256022937600`) beside a correctly formatted `15.0 GiB`.
5. **False affordances — `<button>` elements with no behaviour.** `tenant-switcher` and `tenant-card` look interactive and do nothing; `profile-btn` looks like a menu and instead destroys the session. None of the three carries `aria-haspopup` or `aria-expanded`.
6. **Form semantics are consistently incomplete.** The Run job panel is not a `<form>` yet its button is `type="submit"`; controls lack `id`/`name` throughout; three of three controls on /assistant/settings are programmatically unlabeled despite having visible `<label>` elements; no `required`, no `aria-invalid`, no inline error slots, and no client-side validation anywhere tested.
7. **Client state never reaches the URL.** Tab selection and search text on /devices are invisible to `location.href`, so filtered views cannot be bookmarked, shared into a ticket, or restored by Back/Reload. Detail pages compound this with non-navigable breadcrumbs.
8. **Thin heading structure.** Every page tested exposes exactly one heading (`H1`) with all card and section titles as non-heading elements — except /assistant, which has a redundant `H2` duplicating its `H1`.
9. **Type scale bottoms out too low, and muted tokens fail contrast.** Text at 9px, 10px and 11px is used for eyebrows, metadata, job names, card descriptions and table headers; the muted (`rgb(114,113,125)`) and table-header (`rgb(135,133,144)`) tokens fall below the 4.5:1 required for small text.
10. **Terminology drifts between navigation and content.** "AI Admin Copilot" (nav/header) vs "Assistant" (H1/body/URL); "Last seen" (device list) vs "Last sweep" (detail card) vs "Last contact" (Health tab) for closely related concepts.

## Coverage log

**Fully audited (6 routes/pages):**
- **/login** — inspected full a11y tree (3 nodes); clicked "Sign in with SSO" (4 times across the session); observed successful auth, and later a silent failure loop; checked for error regions and forms (none).
- **/dashboard** — screenshotted at 1440x900, scrolled full page (3 viewports); read interactive a11y tree (26 refs); ran computed-style contrast and font-size sweeps; enumerated all 6 stat cards and 6 panels; clicked `tenant-switcher` (no effect) and `profile-btn` (signed me out); measured the empty-column grid at 1391px; console errors (none) and network (43 requests) captured.
- **/devices** — screenshot; interactive a11y tree; all 4 filter tabs clicked and verified; search tested with `<b>x</b>`, a 500-character string, and empty; verified `location.href` does not change; inspected `Export inventory` (an `<a href="/api/v1/export/devices.csv">`); opened the "Run job" panel; enumerated the job-type `<select>`; **submitted the job form with an empty Message → `POST /api/v1/jobs` 201**; checked `th` scope, tab ARIA wiring, heading count; console (none) and network captured.
- **/devices/:id** (Toeshee-sentinel, `06a843da-…`) — screenshot; **all 10 tabs opened and read** (Hardware, Software, Changes, Jobs, Docs, Patch, Deploy, Backup, Provisioning, Health); counted 35 JSON-bearing cells of 105; tallied row types (32 NICs, 20 of them Docker `veth`); verified rows carry no actions (`rowBtns: [0,0,0,0]`); confirmed my dispatched `hello` job appears as `queued`; verified breadcrumb anchors (none); confirmed agent online/offline state is absent from the page.
- **/assistant** — screenshot; probed headings, buttons, links, bad-token regex; recorded the disabled-feature message verbatim.
- **/assistant/settings** — full text read; enumerated the `Model provider` select (1 empty option); found 3 `<label>` elements with 0 associated controls; **clicked Save and proved no write request was issued** (all `/api/v1/ai/*` traffic was `GET`).

**[SUPERSEDED — see "Coverage log (final)" at the end of this file]**

**Reason:** Clicking the header profile chip on /dashboard — a normal audit action on an unlabeled header control — executed an immediate sign-out (see BLOCKER above). That destroyed the Keycloak SSO session. The app now redirects every route to `/login`, and "Sign in with SSO" returns to `/login` without an error. Restoring access requires typing the `opsadmin` username and password into the Keycloak form, which I am not permitted to do. Awaiting the owner to re-authenticate the browser session.

**Also not completed:**
- **Responsive testing (1280x800 / 768x900 / 390x844 on 4 pages).** Only 1440x900 was exercised before the lockout. Not attempted on the login page alone, as a single-button card would not be representative.
- **Keyboard / focus-visibility Tab sweeps and modal focus-trap + Escape tests.** No true modal was encountered before the lockout — the Run job panel renders inline, not as a dialog — so focus trapping could not be assessed.
- **Destructive-action testing (delete a record I created; cancel a job).** No delete affordance was reachable in the audited routes, and **the product exposes no cancel control for a queued job at all** (itself reported as a HIGH finding).
- **Loading-state observation.** All API responses returned fast enough that no skeleton or spinner was observed; whether loading states exist is untested.
---
## /health — Health & Alerts

### [HIGH] Server validation errors are dumped into the UI as raw FastAPI/Pydantic JSON
- **Page:** /health → Health settings → "Save settings"
- **What happens:** Submitting `Offline after = -5` produces `PUT /api/v1/health/settings → 422`, and the response body is printed verbatim next to the Save button in red 14px text:
  `{"detail":[{"type":"greater_than_equal","loc":["body","offline_after_s"],"msg":"Input should be greater than or equal to 300","input":-5,"ctx":{"ge":300}}]}`
  The element is `<... class="text-sm text-[var(--red)]">` with `role: null`. The page contains **zero** live regions (`[role=alert],[role=status],[aria-live]` → 0).
- **Why it's a problem:** The user is shown the API's internal schema — field name `offline_after_s`, validator type `greater_than_equal`, constraint object `{"ge":300}` — instead of "Offline after must be at least 300 seconds (5 minutes)". Non-technical operators cannot parse it; the useful sentence is buried mid-string. The error is also not attached to the offending field, so on a form with eight numeric inputs the user must decode `loc: ["body","offline_after_s"]` to work out which box is wrong. And with no live region, screen-reader users are never told the save failed at all. Violates Nielsen #9 (error messages in plain language, precisely indicating the problem) and fails WCAG 3.3.1 (Error Identification) and 4.1.3 (Status Messages).
- **Fix:** Map `detail[].loc` to the corresponding control, render the message inline beneath that field with `aria-invalid="true"` and `aria-describedby`, translate the constraint into the field's own units ("Must be between 5 minutes and 24 hours"), and put a summary in a `role="alert"` region. Never render an API response body directly.
- **Evidence:** Screenshot ss_5981jmilo shows the JSON string spanning two lines to the right of the Save button; DOM probe returned the exact `errText` above with `errRole: null`, `errCls: "text-sm text-[var(--red)]"`, `liveRegions: 0`.

### [HIGH] `min`/`max` constraints on all eight settings fields are decorative — the panel is not a `<form>`, so nothing is validated client-side
- **Page:** /health → Health settings (8 `input[type=number]` fields) → "Save settings" (`type="submit"`)
- **What happens:** Every numeric field carries proper HTML constraints (`Offline after` min=300 max=86400; `Escalate after` min=300 max=604800; `Unavailable clears after` min=3600 max=2592000; `Flap window` min=300 max=86400; `Flap threshold` min=2 max=50; `Flap cooldown` min=300 max=86400; `Approval window` min=300 max=604800; `Alert retention` min=90 max=3650). With `Offline after = -5`, `input.checkValidity()` returns **false** and the browser's own message is `"Value must be greater than or equal to 300."` — yet **`document.querySelector('form')` returns null**, the Save button is a `type="submit"` with no owning form, and the app posted the value to the server anyway.
- **Why it's a problem:** All the constraint metadata needed to stop the mistake at the point of entry already exists and is thrown away, so every invalid edit costs a network round trip and comes back as unreadable JSON (previous finding). This is the same non-form defect found in the /devices "Run job" panel, so it is a systemic pattern rather than a one-off. Violates Nielsen #5 (error prevention — better than error messages) and #4.
- **Fix:** Wrap each settings panel in a real `<form>` and call `reportValidity()` (or equivalent) before submitting; block the request when the form is invalid and focus the first invalid control. Adopt this as a lint rule — a `type="submit"` button with no ancestor `<form>` should fail CI.
- **Evidence:** `{"form":false,"saveType":"submit"}`; field probe `{"lab":"Offline after","val":"-5","valid":false,"msg":"Value must be greater than or equal to 300.","min":"300","max":"86400"}`; network `PUT /api/v1/health/settings → 422`.

### [HIGH] Typing letters into a numeric field silently erases it, and the empty result passes validation
- **Page:** /health → Health settings → "Alert retention" (and all eight `input[type=number]` fields)
- **What happens:** Typing `abc` into the "Alert retention" field (which held `400`) leaves the field **visually blank** and `input.value` equal to `""` — the browser discards non-numeric keystrokes in a number input without any feedback. Crucially, `checkValidity()` on the now-empty field returns **true**, because the field is not `required` (`req: false` on all eight). So an accidentally-cleared retention setting is considered a perfectly valid submission.
- **Why it's a problem:** The user sees their value vanish with no explanation and no error, and the form reports itself as valid. "Alert retention" governs how long cleared alerts are kept before deletion — submitting it blank risks writing a null/default over a deliberate 400-day retention setting, which is a silent data-governance change. Violates Nielsen #5 and #1, and WCAG 3.3.1.
- **Fix:** Mark all settings fields `required`, validate that each holds a number within range before enabling Save, and show an inline "Enter a number between 90 and 3650 days" when the field is emptied. Consider a text input with explicit parsing and a unit suffix so keystrokes are never silently swallowed.
- **Evidence:** After typing `abc`: `{"lab":"Alert retention","val":"","valid":true,"msg":"","min":"90","max":"3650"}`.

### [MEDIUM] Seven settings are in seconds and one is in days, with no unit shown in or beside any input
- **Page:** /health → Health settings
- **What happens:** The eight inputs contain bare numbers — `900`, `3600`, `86400`, `3600`, `4`, `3600`, `86400`, `400` — with no unit suffix, no adornment inside the field and no unit in the label. Seven are **seconds** (Offline after, Escalate after, Unavailable clears after, Flap window, Flap cooldown, Approval window, and the count-based Flap threshold), while **"Alert retention" is in days** — discoverable only from its helper sentence, "Cleared alerts are deleted after this many days." The seconds fields do carry a helpful derived hint below ("15 min —…", "1 h —…", "1 d —…"), but "Alert retention" has no equivalent, so nothing converts its `400`.
- **Why it's a problem:** A field reading `400` sitting in a grid of fields reading `3600` and `86400` invites the reading "400 seconds". An admin intending 400 days who types `86400` (copying the pattern of its neighbours) would set retention to 236 years; one intending 90 days who types `7776000` gets rejected as out of range with the unreadable JSON error. Mixed units in one visual group is a classic Nielsen #2 and #5 failure — and the min/max of 90–3650 confirms days, but only to someone who checks the DOM.
- **Fix:** Put the unit in the label or as a suffix adornment inside every field ("Offline after **900** seconds", "Alert retention **400** days"), or better, use a value+unit control (number + a seconds/minutes/hours/days select) so the stored unit is explicit. Add the derived hint to Alert retention like its siblings.
- **Evidence:** Input probe listing all eight with `min`/`max`: Alert retention `min:"90", max:"3650"` versus Offline after `min:"300", max:"86400"`; visible values `900 / 3600 / 86400 / 3600 / 4 / 3600 / 86400 / 400`.

### [MEDIUM] A failed save silently reverts every field, so the user cannot correct the value the error complains about
- **Page:** /health → Health settings, after the 422
- **What happens:** Following the failed `PUT`, all eight inputs re-render with the **server's stored values** — `Offline after` is back to `900` and `Alert retention` back to `400` — while the error message beside the Save button still reads `..."input":-5...`. The user's edits are gone.
- **Why it's a problem:** The error refers to a value that is no longer anywhere on screen, so the message is unintelligible in context, and any other legitimate edits made in the same pass are silently discarded — the user must redo all of them. Violates Nielsen #3 (user control and freedom) and #9; loss of user input on a recoverable error is one of the most frustrating form behaviours there is.
- **Fix:** Preserve the user's entered values on a validation failure, highlight only the offending field, and let the user fix that one field and resubmit. Only reset from the server on an explicit Cancel/Discard.
- **Evidence:** Post-422 value probe: `[{"lab":"Offline after","val":"900"},…,{"lab":"Alert retention","val":"400"}]` while the on-screen error still contains `"input":-5`.

### [MEDIUM] "Create profile" with an empty name is a silent no-op
- **Page:** /health → Health profiles → "New profile name" input + "Create profile" button
- **What happens:** Clicking **Create profile** with the name field empty does nothing at all: no inline error, no toast, no `[role=alert]` (`alerts: []`), no network request, and the page text is unchanged. The field carries no `required` attribute.
- **Why it's a problem:** Identical to the /assistant/settings Save defect — the user cannot distinguish "you must enter a name" from "the button is broken". Violates Nielsen #9 and WCAG 3.3.1/3.3.2.
- **Fix:** Mark the input `required`, show "Enter a profile name" inline on submit, and move focus to the field.
- **Evidence:** Post-click probe: unchanged Health profiles section text ("No health profiles. Nothing in this organisation is being checked."), `alerts: []`, and no matching network request.

### [MEDIUM] Profile names accept arbitrary markup-like strings with no format validation
- **Page:** /health → Health profiles → "New profile name"
- **What happens:** Submitting the name `<b>audit-a</b>` succeeded (`POST /api/v1/health/profiles → 201`) and the profile now appears in the table with the literal name `<b>audit-a</b>` (correctly escaped — it renders as text, not bold). There is no length limit, no character-set restriction and no duplicate-name check surfaced.
- **Why it's a problem:** The escaping is correct and safe (recorded as a strength), but an object that other modules reference by name should not accept angle brackets and unbounded length — such names leak into CSV exports, ticket subjects, alert text and email notifications where the escaping guarantees may not hold. Violates Nielsen #5.
- **Fix:** Constrain profile names to a sensible pattern and length (e.g. 1–64 chars, letters/digits/space/`-_.`), validate inline on blur, and enforce uniqueness with a clear "A profile with that name already exists" message.
- **Evidence:** `POST /api/v1/health/profiles → 201` followed by `GET /api/v1/health/profiles → 200`; the resulting row's NAME cell reads `<b>audit-a</b>` as visible text.

### [LOW] The error message pushes the Save button into a two-line wrap
- **Page:** /health → Health settings, error state
- **What happens:** The validation error renders as a sibling inline element to the right of the Save button. Because the JSON string is long, it consumes the row and squeezes the button, whose label "Save settings" wraps onto two lines ("Save" / "settings"), changing the button's height and shifting the layout.
- **Why it's a problem:** A primary action that visually deforms whenever an error appears looks broken and undermines confidence at exactly the moment the user is already confused. Violates Nielsen #8.
- **Fix:** Place error text on its own row below the button (or above the field group) with a fixed minimum height reserved, so appearance of an error does not reflow the action row.
- **Evidence:** Screenshot ss_5981jmilo — the Save button renders as a two-line "Save settings" with the JSON error occupying the space to its right.

### [BLOCKER] A failed destructive API call (HTTP 503) is silently swallowed — the UI shows success and the user is never told
- **Page:** /health → Health profiles → row action "Delete"
- **What happens:** Deleting the profile I had created issued `DELETE https://itms.toeshee.com/api/v1/health/profiles/06a85b23-59e3-70c6-8000-88e6ff828ef6` which returned **HTTP 503 (Service Unavailable)**. The UI displayed **nothing** — no error text, no toast, no red state, no retry affordance (`[role=dialog],[role=alertdialog],dialog,.modal` → 0; no error element rendered anywhere in `<main>`). The row simply vanished and the section reverted to its empty state, "No health profiles. Nothing in this organisation is being checked." The follow-up `GET /api/v1/health/profiles → 200` rendered that same empty list.
- **Why it's a problem:** The user is shown an unambiguous success signal — the record is gone from the list — while the server actually reported a failure. This is the most dangerous class of UI bug: the interface asserts a state the backend did not confirm. If the 503 meant the delete did not commit (a transient dependency failure, a queue outage), the profile still exists server-side and will reappear on the next hard refresh, and the operator will have moved on believing it is gone. In a health-monitoring module, believing you deleted a profile that is still live means alerts you think are switched off are still firing — or vice versa. Because the response is discarded entirely, **the UI cannot distinguish 200 from 503**, so every write in this module is unverified. Violates Nielsen #1 (visibility of system status) and #9, and WCAG 3.3.1/4.1.3.
- **Fix:** Check the response status on every mutation. On non-2xx, keep the row in place, surface a specific error ("Couldn't delete this profile — the server is temporarily unavailable (503). Try again."), offer Retry, and never optimistically remove a row until the server confirms. Add a global error interceptor so no mutation can fail silently, and alert on 5xx rates.
- **Evidence:** Network log: `url: .../api/v1/health/profiles/06a85b23-59e3-70c6-8000-88e6ff828ef6 | method: DELETE | statusCode: 503`, immediately followed by `GET .../health/profiles → 200`. Post-click DOM: `{"dialogs":0,"hp":"Health profiles\n\nNo health profiles. Nothing in this organisation is being checked.\n\nNew profile name\nCreate profile..."}` — no error string present anywhere.

### [HIGH] Delete destroys a record instantly with no confirmation, and is styled identically to the non-destructive actions beside it
- **Page:** /health → Health profiles → row actions "Checks / Retire / Clone / Delete"
- **What happens:** A single click on **Delete** removes the profile immediately. There is no confirmation dialog, no "type the name to confirm", no undo and no toast with an Undo affordance (`dialogs: 0` after the click). The button's class is **`btn ghost`** — byte-identical to its neighbours **Clone** (`btn ghost`) and **Retire** (`btn ghost`) — so the irreversible action carries no visual distinction from the action that duplicates a record. The four buttons are packed into a cramped 2x2 block at the row's right edge (`Checks` 72x37 and `Retire` 62x37 on the top line, `Clone` 62x37 and `Delete` 65x37 directly beneath), so Clone at (1081, 809) sits **68px** from Delete at (1149, 809) on the same line.
- **Why it's a problem:** Deleting a health profile removes the definition of what is being monitored across the fleet — a high-consequence, unrecoverable action. Placing it adjacent to Clone in matching styling, with no confirmation step, makes a misclick both easy and permanent. Violates Nielsen #5 (error prevention), #3 (user control and freedom — undo), and the near-universal convention that destructive actions are visually differentiated and confirmed.
- **Fix:** Style Delete as destructive (red text/border, distinct from ghost), separate it from the constructive actions (a divider, or move it into an overflow "…" menu), and require confirmation naming the object ("Delete profile `<b>audit-a</b>`? Devices resolved by it will stop being checked."). Better still, offer a soft delete with an Undo toast for ~10 seconds — note the product already has a "Retire" concept that may be the correct default action.
- **Evidence:** Button probe: `[{"t":"Checks","x":1086,"y":768,"cls":"btn"},{"t":"Retire","x":1157,"y":768,"cls":"btn ghost"},{"t":"Clone","x":1081,"y":809,"cls":"btn ghost"},{"t":"Delete","x":1149,"y":809,"cls":"btn ghost"}]`. Screenshot ss_8828kvt5q shows the 2x2 button cluster; one click at (1149,809) emptied the list with no intermediate state.

---
## /security — Security Logs

### [HIGH] "Export evidence CSV" ignores the active username and event-type filters — the export does not match what is on screen
- **Page:** /security → "Export evidence CSV" button (an `<a download href="/api/v1/reports/access.csv?…">`)
- **What happens:** With the username filter set to `opsadmin` and the event-type filter set to `logon_failure` (both verified applied: `inputVal: "opsadmin"`, `selVal: "logon_failure"`), the export link's href is `"/api/v1/reports/access.csv?from_ts=2026-08-18T13%3A51%3A03.613Z"` — **`from_ts` is the only parameter**. No `username`, no `event_type`. The time-range selector *is* honoured (switching 7d → 24h moved `from_ts` from `2026-08-12T13:48:40.229Z` to `2026-08-18T13:51:03.613Z`), so two of the three filters are silently dropped and one is respected.
- **Why it's a problem:** This page is explicitly framed as **"GOVERNANCE EVIDENCE"** — "Centralized login, logout, and privilege evidence… scoped by tenant". An auditor filters to "failed logins for opsadmin", clicks Export, and receives a CSV containing *every* event type for *every* principal in the window. Because the button sits directly beside the filters, the user has every reason to believe the export is the filtered set. Handing an auditor or regulator a file that silently over-discloses (and does not answer the question asked) is a compliance problem, not just a usability one. The partial correctness — time range respected, other filters not — makes it far more deceptive than if no filter were applied. Violates Nielsen #1 (visibility of system status) and #2, and WCAG-adjacent principle of predictable behaviour (3.2.x).
- **Fix:** Build the export URL from the same filter state object that drives the table (`?from_ts=…&username=…&event_type=…`), and label the button with what it will produce ("Export 0 filtered events (24h)"). If a full export is also wanted, offer it as an explicitly separate action ("Export all events in window").
- **Evidence:** Probe with filters applied: `{"inputVal":"opsadmin","selVal":"logon_failure","href":"/api/v1/reports/access.csv?from_ts=2026-08-18T13%3A51%3A03.613Z"}`. Time-range probe before/after clicking 24h: `from_ts=2026-08-12T13:48:40.229Z` → `from_ts=2026-08-18T13:51:03.613Z`.

### [HIGH] The 24h/7d/30d time-range control conveys its selection by colour alone and exposes no state to assistive technology
- **Page:** /security → segmented control "24h | 7d | 30d" (top-right of the filter bar)
- **What happens:** The three options are plain `<button>` elements. The selected one is distinguished **only** by the class `btn small primary` (a solid purple fill); the others are `btn small `. There is **no `aria-pressed`, no `role`, no `aria-current`** on any of them (all three attributes returned `null` for all three buttons), and they are not wrapped in a `role="group"` or `role="radiogroup"` (`hasGroup: false`).
- **Why it's a problem:** A screen-reader user hears "24h, button. 7d, button. 30d, button." with nothing indicating which range the data on screen represents — so the numbers in the stat cards ("Events in window: 0") are meaningless to them. For sighted users with a colour-vision deficiency, a purple-vs-white fill with no icon, underline, border or text cue is the sole signal. Fails WCAG 1.4.1 (Use of Color) and 4.1.2 (Name, Role, Value); relates to 1.3.1.
- **Fix:** Use the WAI-ARIA radio-group pattern — `role="radiogroup"` with `aria-label="Time range"` on the wrapper and `role="radio" aria-checked` on each option (or `aria-pressed` toggle buttons) — plus a non-colour visual cue such as a bottom border or a check glyph on the selected option, and arrow-key navigation.
- **Evidence:** `[{"t":"24h","cls":"btn small primary","pressed":null,"role":null,"cur":null},{"t":"7d","cls":"btn small ","pressed":null,...},{"t":"30d","cls":"btn small ","pressed":null,...}]`, `hasGroup: false`.

### [MEDIUM] The module's own labelling contradicts itself: eyebrow says "GOVERNANCE EVIDENCE", breadcrumb and sidebar say "Fleet"
- **Page:** /security — page eyebrow, header breadcrumb, sidebar grouping, page title
- **What happens:** The eyebrow above the H1 reads **"GOVERNANCE EVIDENCE"**, the header breadcrumb reads **"ITMS / Fleet"**, and the sidebar places "Security Logs" inside the **FLEET** group (alongside Devices, Health, Patch, Deploy, Remote Control, Credentials) — while the sidebar has a separate **GOVERNANCE** group further down containing IT Documentation, Change Management, BCP/DR Tests, Reporting & Evidence and Immutable Audit. The top-bar page title says "Security Logs" while the H1 says "Security logs" (different capitalisation of the same name).
- **Why it's a problem:** Three simultaneous, conflicting answers to "where am I?" defeat the purpose of both the breadcrumb and the section eyebrow, and a user hunting for security evidence will look under GOVERNANCE — where the product's own eyebrow tells them it belongs — and not find it. Violates Nielsen #4 (consistency and standards), #6 (recognition rather than recall) and WCAG 2.4.8 (Location).
- **Fix:** Decide whether security logs are a Fleet or a Governance concern and make the sidebar group, breadcrumb and eyebrow agree. Normalise title casing so the nav label, page title and H1 are the same string.
- **Evidence:** `{"eyebrow":"GOVERNANCE EVIDENCE","crumb":["ITMS","/"]}` with the header rendering "ITMS / Fleet" and the top bar "Security Logs"; H1 probe `["1:Security logs"]`; sidebar text lists "Security Logs" under FLEET and a separate GOVERNANCE group.

### [MEDIUM] Stat cards are qualified with the implementation term "(loaded)"
- **Page:** /security → stat cards
- **What happens:** The three cards read "Events in window", "**Failed logins (loaded)**" and "**Distinct principals (loaded)**". Two use the parenthetical "(loaded)" and the first uses a different qualifier ("in window"); nothing explains what "loaded" means.
- **Why it's a problem:** "(loaded)" is developer shorthand for "computed over the page of rows currently fetched into the client, not over the whole window" — an important caveat, because it means these two numbers are **not** totals and will change as more rows load. Expressed as "(loaded)" it communicates none of that; a compliance user could easily report "3 failed logins" when the true figure for the window is far higher. Violates Nielsen #2 (speak the users' language) and #1.
- **Fix:** Either compute the figures server-side over the full window and drop the qualifier, or state the caveat in plain words beneath the number ("across the 250 events loaded so far — scroll to load more"). Use one consistent qualifier style across all three cards.
- **Evidence:** Card labels verbatim: `Events in window / 0`, `Failed logins (loaded) / 0`, `Distinct principals (loaded) / 0`.

### [MEDIUM] Filter state is invisible to the URL, so an evidence view cannot be shared or reproduced
- **Page:** /security → username search, event-type select, time-range buttons
- **What happens:** After setting username `opsadmin`, event type `logon_failure` and range `24h`, `location.href` remains exactly `https://itms.toeshee.com/security`. Reloading discards all three.
- **Why it's a problem:** Worse here than on /devices: this is an evidence page. "The failed logins for this account over the last 24 hours" is precisely the artefact one pastes into an incident ticket or a regulator's request, and it cannot be linked to or reproduced by a colleague. It also means the view cannot be cited in the immutable-audit trail the product otherwise emphasises. Violates Nielsen #3 and #7.
- **Fix:** Reflect all filter state in the query string and hydrate from it on load; add a "Copy link to this view" action next to Export.
- **Evidence:** `{"inputVal":"opsadmin","selVal":"logon_failure","url":"https://itms.toeshee.com/security"}` and, after clicking 24h, `url: "https://itms.toeshee.com/security"`.

### [MEDIUM] Empty state gives no way to widen the search that produced it
- **Page:** /security → results area, and the "Raw event" panel
- **What happens:** With no matching events the table area shows an icon and "No events in this window." The three stat cards all read 0. There is no suggestion to widen the range, no "clear filters" action, and no indication of which of the three active filters caused the emptiness. Below it, the "Raw event" panel reads "Select an event." permanently.
- **Why it's a problem:** The user is left to guess whether there genuinely are no security events, whether their username filter is misspelled, or whether the agent is not shipping logs at all — a materially different conclusion in a security context. Violates Nielsen #9 and #1.
- **Fix:** Make the empty state name the active filters and offer one-click widening ("No events for `opsadmin` (logon failure) in the last 24h — try 30d, or clear filters"). Distinguish "no data has ever been collected" from "no data matches" — the former should point at agent/collection setup.
- **Evidence:** Screenshot ss_14852irzc; page text `No events in this window.` and `Raw event / Selected row's stored fields / Select an event.`

### [LOW] Untested: the "Raw event" detail panel
- **Page:** /security → "Raw event" panel
- **What happens:** The panel is titled **"Raw event"** with the subtitle "Selected row's stored fields", and permanently reads "Select an event." because no security events exist in this environment to select.
- **Why it's a problem:** Not verified. Flagged because the naming ("Raw event", "stored fields") strongly suggests the same unformatted-JSON presentation confirmed on /devices/:id, where 35 of 105 cells rendered raw JSON. If so it would be the same HIGH finding; if it renders a formatted field list it may be fine.
- **Fix:** N/A pending verification — but ensure it renders labelled fields with a "View raw JSON" disclosure rather than a JSON dump by default.
- **Evidence:** Panel text `Raw event / Selected row's stored fields / Select an event.`; the events table is empty (`Events in window: 0`) so no row could be selected.

---
## /patch — Patch Management

### [HIGH] "Scan now" queues a fleet scan with zero feedback — and this is demonstrably what produced the duplicate jobs on the dashboard
- **Page:** /patch → "Scan now" (`button.btn.primary`, top right)
- **What happens:** Clicking **Scan now** issues `POST /api/v1/patch/scan → 202 Accepted` and refetches (`GET /api/v1/patch/report`, `GET /api/v1/patch/patches`). The screen after the click is **visually identical** to before: no toast, no "Scan queued" message, no spinner, no disabled state on the button, and every stat card still reads 0 ("Patches in catalog 0", "Critical / high severity 0", "Exposed devices 0", "Critical exposure 0"). A 202 explicitly means "accepted, not yet done" — precisely the case that most needs an explanatory UI state, and it gets none.
- **Why it's a problem:** This is the strongest evidence in the audit that missing feedback causes real duplicate work: the dashboard's "Recent signed jobs" panel shows **two identical `patch.scan` jobs queued four seconds apart** (8/18/2026 11:04:14 AM and 11:04:18 AM). Four seconds is the interval of a user clicking, seeing nothing happen, and clicking again. Both have now been stuck in `queued` for over 24 hours against the fleet's only agent, which is offline. Violates Nielsen #1 (visibility of system status) and #5.
- **Fix:** On 202, disable the button and switch it to a progress state ("Scanning… started 12:03"), show a toast linking to the queued job, and surface scan status on the page (last scan time, current scan progress, targets pending). Guard against a second submission while one is in flight, and warn when no target agent is online.
- **Evidence:** Network: `POST /api/v1/patch/scan | 202`. Screenshot ss_0943vd7du taken 3s after the click is indistinguishable from the pre-click state. Dashboard job feed: `patch.scan / 8/18/2026, 11:04:18 AM / queued` and `patch.scan / 8/18/2026, 11:04:14 AM / queued`.

### [MEDIUM] A `<button>` is nested inside an `<a>` — invalid HTML and an ambiguous control
- **Page:** /patch → "Policies" action
- **What happens:** The markup is literally `<a href="/patch/policies" data-discover="true"><button class="btn">Policies</button></a>` — an interactive `<button>` nested inside an interactive `<a>`. Both elements report the same bounding box (x 1007, y 172, 74x37), so the accessibility tree exposes two overlapping controls for one visual target.
- **Why it's a problem:** Nesting interactive elements is invalid HTML and explicitly disallowed by the ARIA authoring practices. Behaviour is browser-dependent: the button swallows the click in some engines so the link never navigates, and screen readers may announce "link, Policies, button, Policies" or expose an empty link. It also breaks the browser affordances users expect from a link — middle-click to open in a new tab, right-click "Copy link address", and Ctrl/Cmd-click — none of which work reliably through a nested button. Fails WCAG 4.1.2 (Name, Role, Value) and 4.1.1-style parsing expectations.
- **Fix:** Pick one element. For navigation, use a single `<a href="/patch/policies" class="btn">Policies</a>` styled as a button. Add a lint rule (`jsx-a11y/no-nested-interactive` or equivalent) to catch this pattern.
- **Evidence:** `a.outerHTML` = `<a href="/patch/policies" data-discover="true"><button class="btn">Policies</button></a>`; `nestedBtns: 1`, `tabbables: 1`.

### [MEDIUM] Two chart panels use a device/monitor icon and identical copy for their empty state
- **Page:** /patch → "Compliant % by OS family" and "Compliant % by severity" cards
- **What happens:** Both chart cards render a centred **monitor/display icon** — the same glyph used for the Devices module — above the identical sentence "No compliance data yet." The "Exposed devices" panel below uses the same monitor icon again.
- **Why it's a problem:** A monitor icon in a percentage-chart card mis-signals what is missing (it suggests a device problem rather than an absent chart), and repeating one icon and one sentence across three semantically different panels means the empty states carry no information at all. Neither card explains *why* there is no data — here, because no patch scan has ever completed — nor offers the obvious action (Scan now sits at the top of the same page). Violates Nielsen #1 and #8.
- **Fix:** Use a chart-shaped empty glyph, write panel-specific copy that names the cause and the fix ("No patch scan has completed yet — run a scan to populate compliance"), and include an inline action button. Reuse the shared `<EmptyState>` component recommended earlier.
- **Evidence:** Screenshot ss_0943vd7du — identical icon and the string "No compliance data yet." in both chart cards; page text also shows "No exposed devices." and "No patches in the catalog." for the panels below.

### [LOW] Four stat cards all read 0 with no indication that the module has never run
- **Page:** /patch → stat card row
- **What happens:** "Patches in catalog 0", "Critical / high severity 0", "Exposed devices 0", "Critical exposure 0" — four zeros presented in the same confident style used for real values elsewhere in the product.
- **Why it's a problem:** "0 critical exposure" reads as an excellent security posture when the truth is "we have never scanned anything, so we do not know" — the same false-confidence trap the device Health tab explicitly warns against ("This is not the same as healthy."). Violates Nielsen #1.
- **Fix:** When no scan has ever completed, render the cards in an explicit unknown state ("—", "Never scanned") rather than 0, and carry the Health module's honest phrasing across to this page.
- **Evidence:** Stat cards read `0 / 0 / 0 / 0` while the panels below state "No patches in the catalog." and "No compliance data yet."

---
## /patch/policies — Patch policies

### [BLOCKER] "New policy" is completely non-functional — patch policies cannot be created at all
- **Page:** /patch/policies → "New policy" (`button.btn.primary`, type=submit, at x 1240 y 172) — the page's only action
- **What happens:** The page states "No patch policies configured." and offers exactly one control. Clicking **New policy** does nothing whatsoever. Verified with both a synthetic `.click()` and a real mouse click at (1240, 172), each followed by a 2-second wait:
  - `document.querySelectorAll('input,select,textarea').length` → **0** (no form appeared)
  - `[role=dialog], dialog` → **0** (no modal appeared)
  - `location.pathname` → still `/patch/policies` (no navigation)
  - Network for `polic`: only `GET /patch/policies → 200` and `GET /api/v1/patch/policies → 200`. **No POST, no navigation, nothing.**
  - `<main>` text is byte-identical before and after.
- **Why it's a problem:** Patch policies are described on this page as "Deployment rings, maintenance windows and reboot behavior applied per device group" — they are the mechanism by which patching is actually controlled and staged from test ring to broad rollout. With this button dead there is **no path in the UI to create one**, so the entire patch-policy capability is unreachable and the module is stuck permanently in its empty state. This is a feature that does not work, not a usability nit. It also repeats the `type="submit"` outside any `<form>` pattern (`form: false`), which may well be the cause.
- **Fix:** Wire the button to the policy-creation form/modal (or route to a `/patch/policies/new` page) and wrap it in a real `<form>`. Add an end-to-end test covering "create a patch policy" — the absence of one is why a fully dead primary action shipped.
- **Evidence:** Two independent click methods, post-click probes `{"inputs":0,"dialogs":0,"txt":"FLEET\nPatch policies\n\n…\n\nNew policy\nNo patch policies configured.","p":"/patch/policies"}`, and a network log containing only GETs.

---
## Session handling (second, cleaner observation)

### [HIGH] The session drops mid-task during continuous active use, with no warning, no expiry notice, and total loss of context
- **Page:** Global — occurred while navigating from /patch/policies to /deploy
- **What happens:** Roughly 20 minutes into an uninterrupted working session — during which the app was making API calls continuously (health profile create/delete, health settings PUT, patch scan POST, several page loads) — the application silently dropped to `https://itms.toeshee.com/login`. There was **no "your session is about to expire" warning, no countdown, no re-authentication prompt in place, and no preservation of the route or any in-progress work**. The user simply finds themselves at a login card.
- **Why it's a problem:** This observation is materially stronger than the earlier one because the confounds are gone: this run used a **single foreground tab** (confirmed by `tabs_context_mcp`: one tab only), and I did **not** touch the profile chip or any sign-out control at any point. So the session ended on its own, despite constant activity — this is not an idle timeout being correctly enforced. Combined with the confirmed 5-minute (300s) ID-token lifetime, it points at silent token renewal that fails intermittently and then hard-redirects instead of recovering. For an operator mid-incident — halfway through editing health thresholds, or having just dispatched a job whose outcome they were waiting on — losing the session with no warning and landing on a context-free login card is severe. Because the app also discards the attempted route (finding: post-login redirect always lands on /devices), the user loses their place entirely. Violates Nielsen #1 (visibility of system status), #3 (user control and freedom) and #9.
- **Fix:** (1) Renew the access token proactively well before the 300s expiry and retry once on failure before giving up. (2) Never hard-redirect on an auth failure while the user has unsaved state — show a non-destructive "Session expired, sign in to continue" modal over the current page and restore in place. (3) Warn before expiry with a countdown and a "Stay signed in" action. (4) Preserve and restore the attempted route. (5) Instrument and alert on unexpected session terminations — a self-terminating session during active use is a bug, not a policy.
- **Evidence:** Run 2 timeline — session confirmed live on /health at the start (`{"p":"/health","vis":"visible","chip":true}`), then continuous successful work across /health, /security, /patch and /patch/policies with successful API writes throughout (`POST /health/profiles 201`, `PUT /health/settings 422`, `DELETE /health/profiles/… 503`, `POST /patch/scan 202`), then on navigating to /deploy: `tabs_context_mcp` → `{"tabId":1442057179,"title":"ITMS","url":"https://itms.toeshee.com/login"}`. Single tab, foreground, no logout control ever clicked in this run.

---
## Coverage log (final)

### Run 1 (tab 1442057190) — 4 routes + 2 sub-pages
- **/login** — full a11y tree (3 nodes); SSO clicked; observed both successful auth and a silent failure loop; no error region or form present.
- **/dashboard** — screenshot at 1440x900; full-page scroll (3 viewports); interactive a11y tree (26 refs); computed-style contrast + font-size sweeps; all 6 stat cards and 6 panels enumerated; clicked `tenant-switcher` (dead) and `profile-btn` (**signed me out — BLOCKER**); measured the 1391px empty-column grid; console (clean) + network (43 requests).
- **/devices** — all 4 filter tabs clicked; search tested with `<b>x</b>`, a 500-char string, and empty; URL-sync checked; `Export inventory` inspected; "Run job" panel opened; job-type `<select>` enumerated; **empty-Message dispatch submitted → `POST /api/v1/jobs` 201**; `th` scope, tab ARIA wiring and heading count checked.
- **/devices/:id** — **all 10 tabs opened and read**; 35/105 JSON cells counted; row types tallied (32 NICs, 20 Docker `veth`); row actions probed (none); dispatched job confirmed `queued`; breadcrumb anchors (none); agent state confirmed absent.
- **/assistant** and **/assistant/settings** — provider select enumerated (1 empty option); 3 labels with 0 associated controls; **Save clicked → proved no write request issued**.

### Run 2 (tab 1442057179) — 3 routes + 1 sub-page
- **/health** — screenshot; full text; all inputs enumerated with min/max; **created a health profile** (`POST 201` → refetch → row rendered); tested **empty-name submit** (silent no-op); tested **out-of-range numeric** (`-5` → `PUT 422` → **raw Pydantic JSON shown in UI**); tested **letters in a number field** (silently emptied, still "valid"); confirmed no `<form>` wrapper so `min`/`max` never enforced; confirmed post-error field reset; **deleted the profile I created** → `DELETE 503` **silently swallowed**, no confirmation dialog.
- **/security** — screenshot; filters enumerated; **time-range segmented control probed** (no `aria-pressed`/role/group); **Export href tested against each filter** — respects time range, ignores username and event type; URL-sync checked; empty states recorded.
- **/patch** — screenshot; **"Scan now" clicked → `POST /api/v1/patch/scan` 202 with zero UI change**; nested `<button>` inside `<a>` confirmed via `outerHTML`; empty-state icons and copy recorded.
- **/patch/policies** — **"New policy" clicked twice (synthetic + real mouse) → completely dead**: no form, no dialog, no navigation, no POST. Recorded as a BLOCKER.

### Never audited (9 of 16 assigned routes)
/deploy, /remote, /credentials, /backup, /provisioning, /site-servers, /service-desk, /portal, /notifications, /email-intake — plus the /email-intake and /service-desk detail pages.

**Reason:** the session terminated on its own while navigating to /deploy (see the HIGH session finding directly above). Per the coordinator's standing instruction, landing on /login is a stop-and-report condition, so no recovery was attempted. Run 1 was ended by a different cause — the sign-out BLOCKER.

### Passes not completed
- **Responsive sweep (1280x800 / 768x900 / 390x844 on 4 pages).** Not run. Only 1440x900 was exercised across both runs. **Untested — no responsive findings are claimed anywhere in this report.**
- **Keyboard / focus-visibility Tab sweep and modal focus-trap + Escape tests.** Not run as a systematic pass. Note that **no true modal was encountered on any of the 7 pages audited** — the Run job panel renders inline and the only "dialog-like" interactions (Delete, New policy) opened nothing — so focus trapping and Escape-to-close could not be assessed at all. Keyboard-relevant defects that *were* found structurally (roving tabindex missing on tabs, `type="submit"` outside any `<form>` so Enter cannot submit, nested interactive elements, segmented control with no ARIA state) are reported individually above.
- **Loading/skeleton states.** All API responses returned fast enough that no loading state was observed; whether any exist is untested.
---
# RUN 3

## /deploy — Software Deployment

### [BLOCKER] Deployments cannot be created — the Package dropdown is empty and Create silently does nothing
- **Page:** /deploy → "New deployment" modal → "Package" `<select>` and "Create" button
- **What happens:** The "New deployment" modal opens correctly, but the **Package** select contains exactly one option: `"Select a package…"` — there are no packages to choose. Clicking **Create** with the dialog in its default state leaves the dialog open with **no error message anywhere** (`errTxt: "NO ERROR TEXT"`), no live region (`[role=alert],[role=status],[aria-live]` → 0), and no network request. The page behind still reads "No deployments yet." and "Deployments 0".
- **Why it's a problem:** This is the same dead-end shape as the AI provider dropdown on /assistant/settings and the dead "New policy" button on /patch/policies: the module's single creation path cannot complete, and the UI never explains why. The user has no way to learn that the blocker is an empty package catalog, and there is no link to a catalog/import screen to populate it. Software deployment — the module's entire purpose — is unreachable. Violates Nielsen #1, #9 and #5.
- **Fix:** When the package catalog is empty, replace the select with an explanatory empty state and a primary action ("No packages in the catalog. Add a package →"), and disable Create with a stated reason. On submit, validate and show inline field errors rather than doing nothing.
- **Evidence:** `{"lab":"Package","val":"","opts":["Select a package…"]}`; post-Create probe `{"dialogStillOpen":true,"live":0,"errTxt":"NO ERROR TEXT"}`; page text "Deployments 0 … No deployments yet."

### [HIGH] The deployment modal has no Escape handler — Escape does not close it
- **Page:** /deploy → "New deployment" modal (`[role=dialog]`, `aria-modal="true"`, `aria-label="New deployment"`)
- **What happens:** Pressing Escape leaves the dialog open (`dialogsAfterEsc: 1`). Dispatching synthetic `keydown`+`keyup` Escape events at **both** `document` and the dialog element also leaves it open (`{"before":1,"after":1}`), confirming the application registers **no Escape-to-close handler** rather than the keypress simply not being delivered.
- **Why it's a problem:** Escape-to-dismiss is the single most ingrained dialog convention on every platform, and it is the primary keyboard escape route from a modal. Without it, a keyboard-only user must locate and activate Cancel or the Close button to leave. Fails the WAI-ARIA Authoring Practices dialog pattern and WCAG 2.1.2 (No Keyboard Trap, in spirit); violates Nielsen #3 (user control and freedom — clearly marked emergency exit) and #4.
- **Fix:** Bind Escape to the same handler as Cancel on every dialog. If the form is dirty, confirm before discarding rather than suppressing Escape.
- **Evidence:** Real keypress → `{"dialogsAfterEsc":1}`; synthetic dispatch at document and dialog → `{"before":1,"after":1}`.

### [HIGH] "Target ID" requires the operator to type a raw device UUID by hand — no picker, no search, no validation
- **Page:** /deploy → "New deployment" modal → "Target type" (`device`/`group`) + "Target ID" free-text input
- **What happens:** After choosing a target *type*, the target itself is specified through a plain `<input type="text">` labelled "Target ID" with **no placeholder, no autocomplete, no device picker, no search, no `pattern` and no `required`**. Device IDs in this product are UUIDs — the one device in this fleet is `06a843da-c3e1-7908-8000-e503456f8459`, which is only discoverable by navigating to /devices, opening the device and copying it out of the browser address bar.
- **Why it's a problem:** Asking a human to hand-transcribe a 36-character UUID into an unvalidated free-text box, in a dialog that installs or **uninstalls software on production endpoints**, is a serious error-prevention failure — a single mistyped character either fails opaquely or, worse, targets the wrong machine. It also makes the feature effectively unusable at fleet scale. This corroborates Auditor B's finding of bare UUIDs on /audit, and is worse here because the user must *produce* the UUID rather than merely read it. Violates Nielsen #6 (recognition rather than recall — the canonical case) and #5.
- **Fix:** Replace with a searchable device/group picker showing hostname, OS and agent state, resolving to the ID internally. Never surface UUIDs as user input. If a raw-ID escape hatch is needed, validate the format and resolve-and-display the matched device name before allowing Create.
- **Evidence:** `{"lab":"Target ID","tag":"INPUT","type":"text","ph":"","req":false,"val":""}` with sibling `{"lab":"Target type","opts":["device","group"]}`.

### [MEDIUM] Maintenance window is a raw cron expression with no timezone stated
- **Page:** /deploy → "New deployment" modal → "Window (cron, optional)" input, placeholder `e.g. 0 2 * * SAT`
- **What happens:** Scheduling is expressed as a bare 5-field cron string typed into a text input. There is **no timezone indicator anywhere** in the dialog or on the page, no preview of when the window will next open, and no validation of the expression.
- **Why it's a problem:** The page description promises jobs are "dispatched only inside an open maintenance window", so this field decides when software installs hit production machines — and the user cannot tell whether `0 2 * * SAT` means 2 AM UTC, 2 AM server time, or 2 AM in their own timezone. Auditor B found a confirmed −5h timezone shift on /changes (typed 09:00 rendered as 4:00:00 AM) with no timezone stated, and this product-wide absence of timezone labelling means the same class of error is available here. Cron is also a poor primary interface for a maintenance window — it excludes non-engineer operators and cannot express a duration, only a start. Violates Nielsen #2, #6 and #9.
- **Fix:** State the timezone explicitly next to the field and store windows with an IANA zone. Offer a plain-language builder (day(s) + start time + duration) with cron as an advanced toggle, validate the expression on blur, and show a live preview ("Next window: Sat 22 Aug, 02:00–04:00 Europe/London").
- **Evidence:** `{"lab":"Window (cron, optional)","ph":"e.g. 0 2 * * SAT","req":false}`; no timezone string present anywhere in the dialog text: `"New deployment | Package | Select a package… | Action | install | update | uninstall | Target type | device | group | Target IDWindow (cron, optional) | Cancel | Create"`.

### [MEDIUM] Modal does not lock background scrolling
- **Page:** /deploy → "New deployment" modal
- **What happens:** With the modal open and `aria-modal="true"` set, `getComputedStyle(document.body).overflow` is **`auto`** — the underlying page remains scrollable behind the dialog.
- **Why it's a problem:** Scrolling the background while a modal claims to be modal breaks the illusion of a focused task, can scroll the dialog's anchor out of view, and on touch devices frequently results in the user scrolling the page when they meant to scroll the dialog. It also contradicts the `aria-modal="true"` promise made to assistive technology. Violates Nielsen #4.
- **Fix:** Apply `overflow: hidden` (with scrollbar-gutter compensation to avoid layout shift) to `body` while any dialog is open, and restore on close.
- **Evidence:** `{"bodyScrollLocked":"auto"}` measured with the dialog present.

### [MEDIUM] The modal is not a `<form>`, continuing the product-wide pattern
- **Page:** /deploy → "New deployment" modal
- **What happens:** `document.querySelector('form')` returns **null** while the dialog is open, despite the dialog containing five controls and a Create button. None of the controls carry `required` (all `req: false`).
- **Why it's a problem:** Enter does not submit, native validation is unavailable, and nothing is required — the same defect already recorded on the /devices Run job panel and the /health settings panel. Three independent occurrences make this a systemic architectural choice rather than an oversight. Violates Nielsen #4 and WCAG 3.3.2.
- **Fix:** Wrap dialog contents in a `<form>` with `onSubmit`, mark required fields, and let Enter submit. Add a CI lint for `type="submit"` outside a form.
- **Evidence:** `{"dialogs":1,"form":false}`; all five controls `req: false`.

### [LOW] Positives: the dialog is correctly identified to assistive technology
- **Page:** /deploy → "New deployment" modal
- **What happens:** The dialog exposes `role="dialog"`, `aria-modal="true"` and `aria-label="New deployment"`, and its icon-only close button carries `aria-label="Close"` (`<button class="close-btn" aria-label="Close"></button>`). All five form controls are correctly associated with wrapping `<label>` elements (`unlabeled: 0`), unlike /assistant/settings.
- **Why it's a problem:** N/A — recorded as a positive and as the baseline the other dialogs and forms in the product should match.
- **Fix:** N/A. Extend the same labelling discipline to /assistant/settings, and add focus management + Escape to this dialog to complete the pattern.
- **Evidence:** `{"dlgAttrs":{"modal":"true","label":"New deployment"},"closeBtnName":{"al":"Close"}}`, `unlabeled: 0`.

### [LOW] "Success rate" shows an em-dash while sibling cards show 0
- **Page:** /deploy → stat cards
- **What happens:** The four cards read "Deployments 0", "Deployed this week 0", "**Success rate —**", "Failed 0" — three zeros and one em-dash for the same underlying condition (nothing has ever been deployed).
- **Why it's a problem:** Mixing `0` and `—` for one state makes the reader hunt for a distinction that does not exist; here `—` is arguably the honest one (a rate over zero runs is undefined) and the zeros are the misleading ones. Violates Nielsen #4.
- **Fix:** Pick one convention for "no data yet" and apply it across all cards in a group; where a derived metric is undefined, say so ("No runs yet").
- **Evidence:** Page text `Deployments 0 / Deployed this week 0 / Success rate — / Failed 0 / No deployments yet.`

---
## /remote — Remote Control

### [HIGH] "Reason" is optional on an audited, consent-gated remote session
- **Page:** /remote → "Start remote session" modal → "Reason" text input (placeholder `e.g. helpdesk ticket 4821`)
- **What happens:** The Reason field carries `required: false` and submitting the dialog with it blank produces no error. The page's own description states: "Audited RDP, VNC, and SSH sessions brokered through the control plane… Every session is consent-gated and attended by default."
- **Why it's a problem:** The justification for remotely accessing an employee's machine is the single most important field in the audit record — it is what an internal investigation, a works council or a data-protection review will ask for. Making it optional guarantees a proportion of sessions will have no recorded reason, which silently degrades the audit trail the module is built to provide. The placeholder points at a ticket number but the field is free text, so even when filled it cannot be verified or linked. Violates Nielsen #5 (error prevention) and the product's own stated security model.
- **Fix:** Make Reason mandatory and validate it is non-trivial. Better, offer a ticket picker that links the session to a real Service Desk ticket (the product has one) and fall back to free text with a minimum length. Surface the reason in the session list and the immutable audit record.
- **Evidence:** `{"lab":"Reason","type":"text","ph":"e.g. helpdesk ticket 4821","req":false,"val":""}`; submitting with all fields default left the dialog open with no error.

### [HIGH] A remote session can be started against a device that is offline, and "Unattended" mode is one dropdown click away with no additional confirmation
- **Page:** /remote → "Start remote session" modal → "Device" and "Mode" selects
- **What happens:** The stat card reads "**Online devices 0**", yet the Device dropdown offers `Toeshee-sentinel` — the fleet's only device, which has been offline since 8/18. Nothing marks it as offline in the dropdown, which lists it as a plain selectable option. Separately, the **Mode** select defaults to "Attended (requires operator consent)" but "**Unattended**" is a peer option selected the same way, with no warning, no confirmation step and no additional authorisation.
- **Why it's a problem:** (a) Offering an offline device for a *live interactive* session guarantees a failed or indefinitely hanging connection with no explanation — this is the same offline-dispatch defect found on /devices "Run job", now on a synchronous feature where it is more visibly broken. (b) Switching from attended to unattended changes the action from "screen-share with the user's consent" to "silently access the employee's machine". That is a privacy-significant escalation and it is presented as an ordinary dropdown value with equal weight to the safe default. Violates Nielsen #1, #5, and the principle that consequential mode changes require deliberate confirmation.
- **Fix:** Disable offline devices in the picker (or annotate them "offline — cannot connect") and show agent state inline. For Unattended, require an explicit confirmation naming the consequence, restrict it by role, and consider requiring a stated reason plus notification to the end user.
- **Evidence:** Stat card "Online devices 0"; `{"lab":"Device","opts":["Select a device…","Toeshee-sentinel"]}`; `{"lab":"Mode","val":"attended","opts":["Attended (requires operator consent)","Unattended"]}`.

### [MEDIUM] Start session with no device selected is a silent no-op
- **Page:** /remote → "Start remote session" modal → "Start session" button
- **What happens:** Clicking **Start session** with Device left at "Select a device…" and Reason blank leaves the dialog open, produces **no error message**, no live region (`live: 0`), and no network request. The dialog text is unchanged.
- **Why it's a problem:** Identical to /deploy, /health "Create profile" and /assistant/settings "Save" — the fourth independent instance of "primary action does nothing and says nothing when the form is incomplete". Violates Nielsen #9 and WCAG 3.3.1/3.3.3.
- **Fix:** Mark Device and Reason `required`, block submit with inline field errors, and move focus to the first invalid control.
- **Evidence:** `{"open":true,"live":0}` with the dialog's text identical before and after the click.

### [MEDIUM] "Credential (optional)" offers only "None" — the credential vault is empty and unexplained
- **Page:** /remote → "Start remote session" modal → "Credential (optional)" select
- **What happens:** The select contains a single option, "None". The page description promises "vaulted credentials decrypted only at point of use", but no vaulted credential is selectable and nothing explains the absence or links to /credentials to add one.
- **Why it's a problem:** A third instance of the empty-dropdown dead end (after the AI provider select and the deployment package select). Because this one is labelled "(optional)" it does not block the flow, but it silently removes the module's headline security feature — sessions will be started without vaulted credentials and the user will never know that was a choice. Violates Nielsen #1 and #9.
- **Fix:** When the vault is empty, show "No credentials vaulted — add one" linking to /credentials, rather than a lone "None". Apply one consistent empty-dropdown treatment across the product.
- **Evidence:** `{"lab":"Credential (optional)","opts":["None"]}`.

### [LOW] Four stat cards read 0 with no distinction between "never used" and "none right now"
- **Page:** /remote → stat cards
- **What happens:** "Online devices 0", "Sessions 0", "Active now 0", "Recorded 0", above the empty state "No remote sessions yet."
- **Why it's a problem:** "Online devices 0" is a live fleet condition, "Sessions 0" is an all-time total, and "Active now 0" is a point-in-time count — three different temporal meanings rendered identically, so the row cannot be read at a glance. Violates Nielsen #4.
- **Fix:** Group and label by timeframe, or add a qualifier line to each card as the /security cards attempt (though see the "(loaded)" finding for how not to word it).
- **Evidence:** Page text `Online devices 0 / Sessions 0 / Active now 0 / Recorded 0 / No remote sessions yet.`

---
## /service-desk — Service Desk

### [HIGH] Creating a ticket is silently blocked by an undeclared required field ("Requester ID")
- **Page:** /service-desk → "New ticket" modal → "Save"
- **What happens:** I filled Category = Software, Subject = `<b>Audit A test ticket</b>` and Description = "Created by UI audit. Safe to delete." and clicked **Save**. The dialog stayed open, showed **no error**, exposed no live region (`live: 0`), and issued **no POST** — the only `tickets` traffic is `GET /api/v1/service-desk/tickets?limit=200` and `GET …?assigned_to=06a8438d-…&limit=200`. The blocker is evidently the empty "Requester ID", but **no field is marked required** (every control returns `req: false`), nothing is starred, and no message appears.
- **Why it's a problem:** Raising a ticket is the core action of a service desk, and it fails silently. The user has filled in everything visibly meaningful and is given no reason for the refusal, no indication which field is at fault, and no error to search for. This is now the **fifth** independent instance of the same defect (/assistant/settings Save, /health Create profile, /deploy Create, /remote Start session, here). Violates Nielsen #9 and #1; fails WCAG 3.3.1 (Error Identification), 3.3.2 (Labels or Instructions) and 4.1.3 (Status Messages).
- **Fix:** Mark required fields with `required` and a visible indicator, block submit with inline per-field errors, move focus to the first invalid field, and announce a summary in a `role="alert"`. Default Requester ID to the signed-in user (the app already knows their ID — it passes `assigned_to=06a8438d-a07f-7256-8000-c2331b4dd204` in its own queries).
- **Evidence:** Post-Save probe `{"open":true,"live":0}` with dialog text unchanged; network shows GETs only.

### [HIGH] Ticket "Description" is a single-line `<input type="text">`, not a textarea
- **Page:** /service-desk → "New ticket" modal → "Description"
- **What happens:** The Description control is `type: "text"` and the dialog contains **no `<textarea>` at all** (`hasTextarea: false`). It renders as a one-line box identical in size to "Subject".
- **Why it's a problem:** A ticket description is the primary free-text field of the entire module — users paste error messages, steps to reproduce and log excerpts into it. A single-line input cannot show more than a few words at a time, does not accept newlines, offers no resize handle and makes reviewing or editing a long description effectively impossible. It also visually signals "short value expected", so users will under-describe their issues — degrading every downstream process the service desk exists to support. Violates Nielsen #7 (flexibility and efficiency of use) and #2.
- **Fix:** Use a `<textarea>` with a sensible default height and resize enabled (or a rich-text/markdown editor consistent with the rest of the product), and allow attachments.
- **Evidence:** `{"lab":"Description","type":"text","ph":"","req":false}`; `hasTextarea: false` for the whole dialog.

### [HIGH] Three raw ID fields in one dialog — Requester ID, Assign to, Device ID — all free text with no picker
- **Page:** /service-desk → "New ticket" modal; also the "Assignee" filter on the ticket list (placeholder "User ID")
- **What happens:** The dialog asks the user to hand-type **"Requester ID"**, **"Assign to (optional)"** and **"Device ID (optional)"** as plain `<input type="text">` fields with no placeholder, no autocomplete, no search, no validation and no `required`. The list view's "Assignee" filter is likewise a text input with placeholder "User ID". These are UUIDs in this system (e.g. `06a8438d-a07f-7256-8000-c2331b4dd204`, `06a843da-c3e1-7908-8000-e503456f8459`).
- **Why it's a problem:** No human knows a colleague's UUID or a laptop's UUID, and there is no in-product way to look one up short of navigating elsewhere and copying from an address bar. In practice these fields will be left blank — which for the mandatory Requester ID means tickets simply cannot be created (see above). This corroborates and extends Auditor B's finding that /audit renders bare UUIDs for every actor and target: the product does not merely *display* raw identifiers, it **requires users to supply them**. Violates Nielsen #6 (recognition rather than recall) — the textbook definition.
- **Fix:** Replace all three with type-ahead pickers searching by name/hostname/email and resolving to IDs internally; default Requester to the current user; scope Assignee to members of the selected queue; scope Device to the requester's devices. Never expose a UUID as a user-entered value anywhere in the product.
- **Evidence:** `[{"lab":"Requester ID","type":"text","ph":"","req":false},{"lab":"Assign to (optional)","type":"text","ph":""},{"lab":"Device ID (optional)","type":"text","ph":""}]`; list filter `{"lab":"Assignee","type":"text","ph":"User ID"}`.

### [MEDIUM] The "Status" filter is a free-text box while every other filter beside it is a dropdown
- **Page:** /service-desk → filter bar (Type, Status, Queue, Assignee, Priority, Search, "Show all tickets")
- **What happens:** **Type** (`All / incident / request`), **Queue** (`All / Helpdesk / Infrastructure / Security`) and **Priority** (`All / low / normal / high / urgent`) are `<select>` elements with fixed vocabularies. **Status**, sitting between them, is an `<input type="text">` with placeholder "Any status" — the user must guess and type the exact status string.
- **Why it's a problem:** Status is a closed enumeration exactly like Type and Priority, so presenting it as free text is both inconsistent and unusable — a typo or a wrong tense silently returns nothing, and the user cannot discover the valid values. Violates Nielsen #4 (consistency and standards) and #6.
- **Fix:** Make Status a `<select>` (or multi-select) populated from the same enumeration the backend uses, matching its siblings.
- **Evidence:** `{"lab":"Status","tag":"INPUT","type":"text","ph":"Any status"}` alongside `{"lab":"Type","opts":["All","incident","request"]}` and `{"lab":"Priority","opts":["All","low","normal","high","urgent"]}`.

### [MEDIUM] Enum values shown raw and lowercase throughout the module
- **Page:** /service-desk → Type, Priority filters and the "New ticket" Type select
- **What happens:** Dropdown options render as `incident`, `request`, `low`, `normal`, `high`, `urgent` — bare lowercase database values. By contrast the Category and Queue selects in the same dialog use properly capitalised human labels ("Access", "Hardware", "Network", "Other", "Software", "Helpdesk", "Infrastructure", "Security").
- **Why it's a problem:** Two labelling conventions appear inside a single dialog, so the interface visibly cannot decide whether it is speaking to a person or a database. This is the same defect as `not_applicable` on the device Patch tab and `inventory.collect`/`hello` in the job-type dropdown — a product-wide missing presentation layer for enums. Violates Nielsen #2 and #4.
- **Fix:** Add a single display-label map for every enum in the product ("Incident", "Request", "Low", "Normal", "High", "Urgent") and use it wherever enums are rendered.
- **Evidence:** `{"lab":"Type","opts":["All","incident","request"]}`, `{"lab":"Priority","opts":["All","low","normal","high","urgent"]}` versus `{"lab":"Category","opts":["Select a category","Access","Hardware","Network","Other","Software"]}` in the same dialog.

### [LOW] "Show all tickets" is an unexplained checkbox that silently changes the query
- **Page:** /service-desk → filter bar → "Show all tickets" checkbox
- **What happens:** An unchecked checkbox labelled "Show all tickets" sits at the end of the filter row. Unchecked, the app queries `?assigned_to=06a8438d-…&limit=200`; the label does not say what the default scope is (tickets assigned to me), and there is no helper text.
- **Why it's a problem:** The user cannot tell what they are currently *not* seeing, which is dangerous on a queue where unassigned tickets are the ones most likely to be missed. The Tickets / "My work" / Admin buttons above already imply a scope, so the checkbox's relationship to them is ambiguous. Violates Nielsen #1 and #6.
- **Fix:** Restate the default in the label ("Showing tickets assigned to me — show all tickets") or replace with an explicit scope selector (Mine / My queues / All) that reflects into the URL.
- **Evidence:** `{"lab":"Show all tickets","type":"checkbox","val":"on"}`; network `GET …/tickets?assigned_to=06a8438d-a07f-7256-8000-c2331b4dd204&limit=200` while unchecked.

### [LOW] Hard-coded `limit=200` with no pagination control
- **Page:** /service-desk → ticket list
- **What happens:** Every list query is `?limit=200`. There is no pagination UI, no "load more", and no total count displayed.
- **Why it's a problem:** Once an organisation exceeds 200 tickets the list silently truncates with nothing telling the user rows are missing — a queue that appears complete but is not. Untestable in this environment (0 tickets exist), so flagged from the query shape rather than observed behaviour. Violates Nielsen #1.
- **Fix:** Add server-side pagination with a visible total and page controls, or infinite scroll with an explicit "showing X of Y" indicator.
- **Evidence:** `GET /api/v1/service-desk/tickets?limit=200` on every load; no pagination controls present in the DOM.

---
## /credentials — Credentials (vault)

### [HIGH] The Secret field sets no `autocomplete`, so browser password managers will treat the vault form as a login
- **Page:** /credentials → "New credential" modal → "Secret" (`input[type=password]`)
- **What happens:** The Secret input is `type="password"` with **`autocomplete: null`** — no `autocomplete="new-password"`, no `autocomplete="off"`. The "Name" field beside it likewise sets no autocomplete. The dialog is not a `<form>` (`form: false`).
- **Why it's a problem:** With no autocomplete hint, Chrome, Edge, Safari and third-party password managers apply their heuristics: they may autofill the operator's *own* saved password into the vault's Secret box, and — more damaging — offer to **save the privileged credential into the operator's personal password store** on submit. That silently copies a domain-admin or SSH key out of the audited vault into an unmanaged browser profile, defeating the module's stated guarantee that secrets are "never logged, returned by the API, or shown outside an audited checkout". Violates Nielsen #5 (error prevention) and the product's own security model; relates to WCAG 1.3.5 (Identify Input Purpose) applied correctly.
- **Fix:** Set `autocomplete="new-password"` on the Secret field and `autocomplete="off"` on the dialog, wrap it in a real `<form>`, and add `data-1p-ignore`/`data-lpignore` hints for common managers. Consider a paste-only control with an explicit "reveal" toggle so the value is never a conventional login field.
- **Evidence:** `{"lab":"Secret","type":"password","req":false,"autocomplete":null}`; `{"form":false}`.

### [HIGH] "Store" with every field empty is a silent no-op
- **Page:** /credentials → "New credential" modal → "Store"
- **What happens:** Clicking **Store** with Name and Secret blank leaves the dialog open with **no error**, no live region (`live: 0`) and unchanged text (`New credential | NameType | windows_admin | ssh_key | domain_join | Secret | Cancel | Store`). No field carries `required`.
- **Why it's a problem:** The **sixth** independent occurrence of this exact defect (/assistant/settings, /health, /deploy, /remote, /service-desk, here). On a vault, silence is worse than elsewhere: the user cannot tell whether the secret was stored, rejected, or dropped — and the module's design deliberately never echoes the secret back, so there is no other way to confirm. Violates Nielsen #1 and #9; fails WCAG 3.3.1/3.3.2/4.1.3.
- **Fix:** Mark Name and Secret required, validate inline, and on success show an explicit confirmation naming the stored credential (never the secret).
- **Evidence:** Post-click probe `{"open":true,"live":0}` with identical dialog text.

### [MEDIUM] Credential types are raw snake_case identifiers
- **Page:** /credentials → "New credential" modal → "Type" select
- **What happens:** The options are `windows_admin`, `ssh_key`, `domain_join` — unmapped database enum values with underscores.
- **Why it's a problem:** The same missing enum presentation layer seen on the device Patch tab (`not_applicable`), the job-type dropdown (`inventory.collect`, `hello`) and the Service Desk Type/Priority filters. Here it also hides meaning that matters: an operator choosing between "ssh_key" and "domain_join" gets no explanation of what each type implies for how the secret is used or where it can be checked out. Violates Nielsen #2.
- **Fix:** Map to "Windows administrator", "SSH key", "Domain join" with a one-line description of each type's use.
- **Evidence:** `{"lab":"Type","opts":["windows_admin","ssh_key","domain_join"]}`.

### [LOW] Strong security copy, no way to act on it
- **Page:** /credentials → "Vault policy" / "Point-of-use boundary" panels
- **What happens:** The page carries excellent, specific security explanation — "An authorized checkout requests a credential reference and states a reason, which is written to the audit log. The secret is decrypted only for that checkout and is shown in this console exactly once." — but with zero credentials stored there is no checkout affordance anywhere, and the stat cards read "Stored credentials 0 / Never checked out 0 / Credential types 0".
- **Why it's a problem:** Minor, but "Credential types 0" is misleading given the Type dropdown offers three; the card appears to count *used* types while reading as a capability count. The excellent policy copy is also disconnected from any action the user can take.
- **Fix:** Clarify the card ("Types in use"), and once credentials exist surface the checkout flow prominently next to the policy text so the explanation and the action sit together.
- **Evidence:** Page text `Stored credentials 0 / Never checked out 0 / Credential types 0 / … / No credentials stored.` alongside a Type select offering three values.

### [LOW] Not tested: storing a real secret and the audited checkout flow
- **Page:** /credentials
- **What happens:** I did not enter any value into the Secret field and did not complete a credential store, so the success path, the "shown exactly once" reveal, and the audited checkout flow are unverified.
- **Why it's a problem:** N/A — recorded for coverage honesty. Entering credentials/secrets is outside what I will do.
- **Fix:** N/A — the owner should verify the store → checkout → audit-log round trip manually.
- **Evidence:** Only the empty-submit path was exercised.

---
## Coverage log — Run 3 (tab 1442057179)

**Audited (4 routes):**
- **/deploy** — probe; "New deployment" modal opened (**the product's first true `role=dialog`**); dialog ARIA attributes read; **Escape tested twice** (real keypress + synthetic dispatch at document and dialog — no handler exists); body scroll-lock checked; all 6 controls enumerated; empty Create submitted (silent, no request).
- **/remote** — probe; "Start remote session" modal opened; all 6 controls enumerated (Device/Protocol/Mode/Credential/Reason); empty submit tested (silent); offline-device selectability and Unattended mode recorded.
- **/service-desk** — probe; filter bar enumerated (7 controls); "New ticket" modal opened; all 8 controls enumerated; **ticket creation attempted with Category + Subject + Description filled** → silently blocked, no POST issued (`GET …/tickets` only).
- **/credentials** — probe; "New credential" modal opened; 3 controls enumerated including the `type=password` Secret field and its missing `autocomplete`; empty Store submitted (silent). **No secret value was ever entered.**

**Never audited (6 of 16 assigned routes):** /portal, /notifications, /email-intake, /backup, /provisioning, /site-servers.

**Reason:** the browser extension returned `Not connected` on two consecutive calls while navigating to /portal, which is the standing stop-and-report condition.

**Cross-checks requested by the coordinator (against Auditor B's findings):**
- **Bare UUIDs — CORROBORATED and extended.** B found /audit *displays* raw UUIDs. My routes are worse: they *require the user to type them*. Four raw-ID text inputs found across two modules — /deploy "Target ID", /service-desk "Requester ID", "Assign to (optional)", "Device ID (optional)", plus the /service-desk list's "Assignee" filter (placeholder "User ID"). No picker, no search, no validation on any of them. The app demonstrably knows the current user's ID (it sends `assigned_to=06a8438d-a07f-7256-8000-c2331b4dd204` in its own queries) yet still makes the user supply it.
- **Timezone corruption — NOT REPRODUCED, but the precondition is present.** I found no datetime *picker* on my run-3 routes, so B's −5h shift could not be reproduced directly. However /deploy schedules maintenance windows via a raw cron string ("Window (cron, optional)", `e.g. 0 2 * * SAT`) with **no timezone stated anywhere** — the same missing-timezone precondition, on a field that controls when software installs reach production endpoints. Recorded as its own MEDIUM finding. /service-desk and /credentials exposed no date/time input at all.
- **Silent mutations — CORROBORATED, and B's refinement is right.** `[role=alert]/[role=status]/[aria-live]` was **0 on every run-3 page and inside every modal**, including after submits. I now have **six** independent silent-failure instances (/assistant/settings Save, /health Create profile, /deploy Create, /remote Start session, /service-desk Save, /credentials Store) and **three** silent-success instances (/devices job dispatch 201, /patch scan 202, /health delete **503**). Against that, exactly two ad-hoc error paths exist in my routes — /health settings renders a raw Pydantic JSON blob, and nothing else renders anything. So B's framing holds: it is not uniform silence but "no notification system plus a handful of mutually inconsistent ad-hoc error paths", and the /health JSON dump is the same shape as the raw JSON B saw on /docs.
- **/jobs has no cancel/retry — CORROBORATED.** Independently confirmed on the /devices/:id Jobs tab: four job rows, `rowBtns: [0,0,0,0]`, `cursor: auto` on every row. No cancel, retry, or detail link anywhere in the product.

**Passes still not completed:** the responsive sweep (1280x800 / 768x900 / 390x844) and the systematic keyboard/focus pass. **Untested — no responsive findings are claimed anywhere in this report.** Partial keyboard results were obtained on the /deploy modal (Escape has no handler — reported); focus-trap and focus-on-open could **not** be measured because the automation window lacked OS focus, making `document.activeElement` unreliable (it stayed `BODY` even after an explicit `.focus()` call), so no focus-management claims are made.
---
# RUN 4

## /portal + /portal/new — End-user Portal

### [HIGH] Ticket Description is a single-line `<input>` here too — the defect is systemic to ticket creation
- **Page:** /portal/new → "Description" field
- **What happens:** As on the staff-side /service-desk dialog, the Description control is `type: "text"` and there is **no `<textarea>` anywhere on the page**. Subject and Description render as identical one-line boxes.
- **Why it's a problem:** This is the *end-user* reporting form — the page where a non-technical employee describes what is broken, pastes an error message, or lists what they tried. A single-line input cannot hold or display that, accepts no newlines, and signals "a few words will do". Poor descriptions here degrade every downstream triage and SLA process. Second independent occurrence confirms it is a shared ticket-form defect, not a one-off. Violates Nielsen #7 and #2.
- **Fix:** Use a `<textarea>` (with a reasonable default height) in both ticket forms; ideally share one form component between /portal/new and the staff dialog so the fix lands once.
- **Evidence:** `{"lab":"Description","tag":"INPUT","type":"text","req":false}`; `textareas: 0` for the page.

### [HIGH] Submit with an empty form is a silent no-op — the seventh occurrence
- **Page:** /portal/new → "Submit"
- **What happens:** Clicking **Submit** with Category unselected and Subject/Description blank leaves the user on /portal/new with **no error message**, no live region (`live: 0`), and page text unchanged. No control carries `required` (all five report `req: false`), and the page is not a `<form>` (`form: false`) despite Submit being `type="submit"`.
- **Why it's a problem:** This is the seventh independent instance of the same defect (/assistant/settings, /health, /deploy, /remote, /service-desk, /credentials, here) and the most damaging one for adoption: it is the first screen an ordinary employee ever touches. A user who cannot file a ticket and is given no reason will conclude the tool is broken and go back to email — which undermines the entire service-desk deployment. Violates Nielsen #9 and #1; fails WCAG 3.3.1/3.3.2/4.1.3.
- **Fix:** Mark Category, Subject and Description `required`, wrap in a real `<form>`, validate on submit with inline field errors, move focus to the first invalid control, and announce a summary via the `role="alert"` component that already exists elsewhere in the product.
- **Evidence:** Post-click probe `{"p":"/portal/new","live":0}` with identical `main` text; `{"form":false,"btnType":"submit"}`.

### [MEDIUM] The Category list interleaves incident and request categories in one flat alphabetical list, creating near-duplicate pairs
- **Page:** /portal/new → "Category" select
- **What happens:** The options are, in order: `Select a category, Access, Access request, Equipment request, Hardware, Network, Other, Other request, Software, Software request`. Incident categories (Access, Hardware, Network, Other, Software) and request categories (Access request, Equipment request, Other request, Software request) are merged into one alphabetical list, producing the confusable adjacent pairs **"Access" / "Access request"**, **"Other" / "Other request"** and **"Software" / "Software request"**.
- **Why it's a problem:** An end user asking for new software must distinguish "Software" from "Software request" with no explanation of either, and the two sit next to each other. Picking wrong routes the ticket to the wrong queue and the wrong SLA. Note the staff-side dialog solves this properly — it has a separate **Type** select (`incident` / `request`) and a shorter Category list — so the portal has been given a flattened, worse version of the same taxonomy. Violates Nielsen #6 (recognition rather than recall) and #4.
- **Fix:** Mirror the staff form: ask "Is this something broken, or something you need?" first, then show only the categories valid for that answer. Failing that, use `<optgroup>` headings ("Report a problem" / "Request something") and add one-line descriptions.
- **Evidence:** Select options verbatim: `Select a category | Access | Access request | Equipment request | Hardware | Network | Other | Other request | Software | Software request`, versus the staff dialog's `Type: incident|request` plus `Category: Access|Hardware|Network|Other|Software`.

### [MEDIUM] Priority is exposed to end users as raw lowercase enums, with no guidance
- **Page:** /portal/new → "Priority" select (`low`, `normal`, `high`, `urgent`)
- **What happens:** End users choose their own ticket priority from bare lowercase database values, with no definitions, no default guidance, and no indication that priority affects SLA.
- **Why it's a problem:** Two problems compound. First, the same missing enum display-label layer found in five other modules. Second, self-selected priority with no definitions reliably produces priority inflation — every user picks "urgent" — which destroys the queue's signal. Violates Nielsen #2 and #6.
- **Fix:** Capitalise and define each level inline ("Urgent — I cannot work at all"), default to Normal, and consider replacing user-set priority with an impact/urgency question that the system maps to priority.
- **Evidence:** `{"lab":"Priority","opts":["low","normal","high","urgent"]}` — identical raw values to the /service-desk filter.

### [LOW] Attachment input accepts any file type, one at a time, with no stated limits
- **Page:** /portal/new → "Attachment (optional)" (`input[type=file]`)
- **What happens:** The file input sets **`accept: null`** and **`multiple: false`** — any file type, exactly one file. No maximum size, permitted formats, or count is stated anywhere on the page.
- **Why it's a problem:** Users troubleshooting an issue typically have several screenshots; being able to attach only one, with no explanation, forces them to file multiple tickets or omit evidence. Absent size/type guidance, oversized or rejected uploads will fail — and given this page's silent-failure behaviour, they will fail without a message.
- **Fix:** Allow multiple files, set `accept` to the supported types, state limits next to the control ("Up to 5 files, 10 MB each — PNG, JPG, PDF, TXT"), and show per-file upload progress and errors.
- **Evidence:** `{"fileAccept":null,"fileMultiple":false}`.

### [LOW] Positive: the portal form correctly omits the requester and does not ask for any ID
- **Page:** /portal/new
- **What happens:** The end-user form asks only for Category, Subject, Description, Priority and an attachment. There is **no "Requester ID" field** — the requester is inferred from the session — and no raw ID input of any kind (`uuids: 0`).
- **Why it's a problem:** N/A — this is the correct design, and it directly **contradicts and contextualises** the staff-side finding: the platform clearly knows how to infer the current user. That makes /service-desk's mandatory hand-typed "Requester ID" an outlier rather than a platform limitation, and confirms Auditor B's observation that /licenses, /subscriptions and /enrollment use proper lookups. /deploy and /service-desk are the two outliers.
- **Fix:** N/A — apply this page's approach to the staff dialog.
- **Evidence:** `inputs` for /portal/new contain no ID field; `uuids: 0` on both /portal and /portal/new.

---
## /notifications — Notifications

### [HIGH] Webhook channels accept any string as a URL — no validation, and the field is not even `type="url"`
- **Page:** /notifications → "Add channel" modal → "URL" field
- **What happens:** The URL control is `type: "text"` (not `type="url"`), with no `pattern`, no `required` and no client validation — `checkValidity()` returned **true** for the literal value `not a url at all`. I submitted a webhook channel named `audit-a-test` with that URL and it was **created successfully**: the dialog closed and the row now appears in the Channels table as `audit-a-test | Webhook | None | active`.
- **Why it's a problem:** This module is described as "Reusable outbound SMTP and webhook delivery… gated by a per-org egress allowlist that denies internal and cloud-metadata addresses by default" — i.e. URL handling is explicitly a security surface. A channel that cannot possibly work is stored in an `active` state, and every event routed to it will fail silently later, at the moment an alert actually needed to be delivered. Validation exists server-side (see the scheme rejection below) but only fires on an explicit Test, never on save. Violates Nielsen #5 (error prevention) and #1.
- **Fix:** Use `type="url"` with `required`, validate scheme and host on blur, run the allowlist check at save time and refuse to store an unusable channel, and mark channels that have never delivered successfully as "unverified" rather than `active`.
- **Evidence:** `{"urlType":"text","urlValid":true,"urlVal":"not a url at all"}`; post-save `{"open":false,"mainHasChannel":true}`; table row `audit-a-test | Webhook | None | active`.

### [HIGH] Delivery failure is reported as a raw enum code — a fourth, distinct error-presentation style
- **Page:** /notifications → Channels table → "Test" row action
- **What happens:** Clicking **Test** on the invalid channel appended this text to the table row: **`Not delivered: scheme_not_allowed`**. The message is a bare snake_case backend enum with no explanation of what a scheme is, why it was not allowed, or what to do. It is rendered inline in the row, not in a live region (`live: 0`), so assistive technology is never told the test failed.
- **Why it's a problem:** Combined with Auditor B's findings, the product now has **four mutually inconsistent error presentations**: raw Pydantic JSON (/health settings, and B's /subscriptions and /docs), a real human message (B's /reports), this bare enum code, and — by far the most common — complete silence (seven silent-submit instances). A user cannot learn one error convention and apply it. The specific message is also unhelpful: the correct fix ("URLs must start with https://") is knowable and unstated. Violates Nielsen #9 and #4; fails WCAG 4.1.3.
- **Fix:** Route every error through one component: human sentence, cause, and remedy ("Not delivered — only https:// webhook URLs are permitted. Update the channel URL."), rendered in a `role="alert"` live region. The `role="alert"` component already exists in the product (B found it on /enrollment's token reveal) — adopt it everywhere.
- **Evidence:** Row text after Test: `audit-a-test | Webhook | None | active | Disable | Test |  | Not delivered: scheme_not_allowed`; `live: 0`.

### [HIGH] Notification channels cannot be deleted — only disabled
- **Page:** /notifications → Channels table row actions
- **What happens:** The only row actions are **Disable** and **Test** (`btns: ["Disable","Test"]`). There is no Delete, no Edit and no overflow menu. A channel created by mistake — such as the invalid `audit-a-test` one this audit just created — is permanent.
- **Why it's a problem:** Misconfigured channels accumulate forever, cluttering the rule-routing UI and leaving broken endpoints in the org's configuration. Because the URL cannot be edited either, an operator who mistypes a webhook URL must abandon a permanently-listed dead channel and create a second one. Violates Nielsen #3 (user control and freedom) and #5.
- **Fix:** Add Edit and Delete row actions (with confirmation for Delete, and a warning if rules reference the channel). Note the inverse problem exists on /health, where Delete is available with *no* confirmation — the product needs one consistent destructive-action pattern.
- **Evidence:** Row action probe returned exactly `["Disable","Test"]`. **Cleanup note for the owner: I could not remove the test channel `audit-a-test` (webhook, URL `not a url at all`) because no delete affordance exists. It will need removing server-side.**

### [MEDIUM] A failed delivery test is not recorded in the Deliveries log
- **Page:** /notifications → "Deliveries" panel
- **What happens:** Immediately after a Test that visibly failed with `scheme_not_allowed`, the Deliveries panel still reads **"0 dead"** and its table shows **"No deliveries yet."**
- **Why it's a problem:** The page promises "retry and dead-lettering per delivery", so the Deliveries panel is the module's observability surface. A user who sees a failure in one panel and "No deliveries yet" in the panel directly below cannot tell whether failures are being recorded at all — which matters when diagnosing why an alert never arrived. Even if the request was rejected before becoming a delivery record, the UI should say so rather than imply nothing happened. Violates Nielsen #1 and #4.
- **Fix:** Record pre-flight rejections as failed deliveries with the reason, or add an explicit "Test results" area distinct from the delivery log and explain the difference.
- **Evidence:** Panel text after the failed Test: `Deliveries | 0 dead | … | RECIPIENT STATUS ATTEMPTS LAST ERROR NEXT ATTEMPT | No deliveries yet.`

### [MEDIUM] "Add channel" modal is the same shared component — same four gaps as /deploy
- **Page:** /notifications → "Add channel" modal
- **What happens:** The dialog exposes `aria-modal="true"`, `aria-label="Add channel"` and a close button with `aria-label="Close"` — and reproduces every gap found on /deploy: **not a `<form>`** (`form: false`), **no Escape handler** (synthetic dispatch: `1->1` dialogs), **no background scroll lock** (`body overflow: auto`), and **no `required`** on any control.
- **Why it's a problem:** Independently confirms Auditor B's conclusion that /licenses, /subscriptions, /bcp, /deploy and now /notifications all render one shared modal component with one shared set of defects. That is good news for remediation — a single component fix repairs Escape, focus management, scroll lock and form semantics across at least six dialogs — but it also means every dialog in the product currently fails the same keyboard and dialog-pattern checks. Fails the WAI-ARIA dialog pattern; violates Nielsen #3 and #4.
- **Fix:** Fix the shared modal component once: bind Escape to close, move focus to the first control on open and restore it on close, trap Tab within the dialog, lock body scroll, and wrap children in a `<form>` so Enter submits.
- **Evidence:** `{"modal":"true","label":"Add channel","form":false,"closeBtn":true,"bodyOverflow":"auto","escTest":"1->1"}` — identical shape to the /deploy probe.

### [MEDIUM] "Bearer token" is a password field with no `autocomplete`, repeating the /credentials defect
- **Page:** /notifications → "Add channel" modal → "Bearer token (optional)"
- **What happens:** The field is `type="password"` with **`autocomplete: null`**, inside a dialog that is not a `<form>`.
- **Why it's a problem:** As on /credentials, browser password managers may autofill the operator's own password here and will offer to save the webhook bearer token into the operator's personal credential store. Second independent occurrence of the same pattern. Violates Nielsen #5.
- **Fix:** Set `autocomplete="new-password"` (or `off`) on every secret-bearing input product-wide and audit for `type="password"` without an autocomplete hint.
- **Evidence:** `{"lab":"Bearer token (optional)","type":"password","req":false,"ac":null}`.

### [LOW] Channel kind is lowercase in the dropdown and capitalised in the table
- **Page:** /notifications → "Kind" select vs Channels table KIND column
- **What happens:** The select offers `webhook` and `email`; the table renders the same value as **`Webhook`**. The delivery-status filter likewise uses raw lowercase `pending / sent / failed / dead`.
- **Why it's a problem:** The same value is displayed two different ways within one screen, so the product visibly applies a display-label mapping in one place and not the other. Violates Nielsen #4.
- **Fix:** Apply the single enum display-label map (recommended repeatedly in this report) at every render site.
- **Evidence:** `{"lab":"Kind","opts":["webhook","email"]}` versus table row `audit-a-test | Webhook | None | active`.

### [LOW] Best heading structure in the audited product, and no datetime fields to corrupt
- **Page:** /notifications
- **What happens:** The page exposes `H1: Notifications` plus five section headings — `H2: Channels`, `H2: Rules`, `H2: Queue members`, `H2: Allowlist`, `H2: Deliveries` — the richest heading outline of any page audited (most pages have a lone `H1`). Separately, checked for Auditor B's timezone bug: this page contains **zero** `datetime-local`, `date` or `time` inputs (`dtl: 0`), so the −5h shift could not occur or be reproduced here.
- **Why it's a problem:** N/A — recorded as a positive and as the model the rest of the product should follow, plus a negative result for the timezone cross-check.
- **Fix:** N/A — apply this page's heading discipline to /dashboard, /devices, /security and others that expose only an `H1`.
- **Evidence:** `h: ["1:Notifications","2:Channels","2:Rules","2:Queue members","2:Allowlist","2:Deliveries"]`; `dtl: 0`.

---
## Responsive behaviour — static analysis

**Method note (important):** `resize_window` reported success for 1280x800, 768x900 and 390x844, but the viewport never actually changed — `window.innerWidth` stayed **1313** at every step and the screenshots were identical. I therefore **could not visually verify** small-viewport rendering, and **no finding below is based on a claimed screenshot at a narrow width**. Instead the findings are derived from the stylesheet and computed layout, which are deterministic and sufficient to establish what the CSS can and cannot do. This is a partial substitute for the visual sweep, not a replacement.

### [HIGH] The application declares `width=device-width` but ships no tablet or phone layout — 2 of 498 CSS rules are responsive, at a single 1300px breakpoint that only adjusts grid column counts
- **Page:** Global (verified on /devices; the CSS is app-wide)
- **What happens:** The document declares `<meta name="viewport" content="width=device-width, initial-scale=1.0">`, promising a mobile-adaptive layout. Enumerating every rule in every stylesheet gives **498 total rules** and **13 media-query blocks**, of which only **two** are the application's own responsive CSS — both `(width <= 1300px)`. The remaining eleven are framework scaffolding and preference queries: `(width >= 40rem)`, `(width >= 48rem)`, `(width >= 64rem)`, `(width >= 80rem)`, `(width >= 96rem)` (Tailwind's default `sm/md/lg/xl/2xl` container tiers), `(hover: hover)`, `(prefers-color-scheme: dark)` and `(prefers-reduced-motion: reduce)`. The entire contents of the `<= 1300px` breakpoint are five declarations:
  `.stats-grid { grid-template-columns: repeat(3, minmax(0px, 1fr)); }`, `.grid-2 { grid-template-columns: minmax(0px, 1fr) 330px; }`, `.cards { grid-template-columns: repeat(2, minmax(0px, 1fr)); }`, `.profile-copy { display: none; }`, `.tenant-switcher { min-width: 165px; }`.
  Nothing in that breakpoint touches the sidebar, the tables, the content offset, or type scale — and there is **no second breakpoint** below 1300px at all.
- **Why it's a problem:** The viewport meta tag tells the browser "this site is designed for small screens", which suppresses the desktop-zoom fallback mobile browsers would otherwise apply. With no layout below 1300px, phone and tablet users get the full desktop layout at device width with that safety net switched off — the worst of both options. For an IT operations console this matters concretely: acknowledging an alert or checking a device from a phone during an out-of-hours incident is a core use case. This corroborates Auditor B's finding, with the refinement that the count of *app-authored* responsive rules is two, not three, and the `40rem–96rem` queries are inert framework defaults rather than real breakpoints.
- **Fix:** Either implement genuine breakpoints (collapse the sidebar to an off-canvas drawer under ~1024px, switch tables to stacked cards under ~768px, raise tap targets) — or, if desktop-only is a deliberate product decision, **remove `width=device-width`** so mobile browsers apply their zoom-out fallback, and state the supported minimum width in the product.
- **Evidence:** `{"totalRules":498,"mediaQueryCount":13,"mediaQueries":["(width <= 1300px)","(width <= 1300px)","(width >= 40rem)","(width >= 48rem)","(width >= 64rem)","(width >= 80rem)","(width >= 96rem)","(hover: hover)","(width >= 40rem)","(width >= 48rem)","(width >= 64rem)","(prefers-color-scheme: dark)","(prefers-reduced-motion: reduce)"],"viewport":"width=device-width, initial-scale=1.0"}` plus the five extracted `<= 1300px` declarations above.

### [HIGH] The sidebar is `position: fixed` at a hard-coded 258px and there is no mobile navigation control anywhere in the product
- **Page:** Global chrome (`<aside>`)
- **What happens:** The sidebar computes to `position: fixed; width: 258px`, and **no media query alters it at any width**. A DOM search for any menu/nav/hamburger toggle returns **zero** (`hamburger: 0`); the only sidebar control is the "Collapse sidebar" chevron, which measures **30x30 px**.
- **Why it's a problem:** On a 390px-wide phone a fixed 258px sidebar occupies **66% of the viewport**, leaving roughly 132px for all content, and because it is `position: fixed` it does not scroll away. The only means of reclaiming that space is a 30x30 target — itself well below the 44x44 minimum — and its collapsed state is not persisted anywhere I could observe. Every route in the product inherits this. Fails WCAG 2.5.5 (Target Size, AAA) and 2.5.8 (Target Size Minimum, AA, 24px — the 30x30 button passes 2.5.8 but fails 2.5.5); violates Nielsen #7.
- **Fix:** Below ~1024px, convert the sidebar to an off-canvas drawer opened by a ≥44px hamburger in the header, closed by Escape and by tapping the scrim, with the open/closed state remembered.
- **Evidence:** `{"asideWidth":"258px","asidePosition":"fixed","hamburger":0}`; `"Collapse sidebar 30x30"` in the small-target list; no sidebar selector appears in the extracted `<= 1300px` rules.

### [HIGH] Data tables cannot scroll within their container — `main` is `overflow-x: visible` and tables are `table-layout: auto` with no `min-width`
- **Page:** /devices (7-column table, measured 1002px wide); the same table component is used across modules
- **What happens:** The devices table has **7 columns** (`DEVICE, OPERATING SYSTEM, ARCHITECTURE, AGENT, CERTIFICATE, LAST SEEN, ACTIONS`), computes to `table-layout: auto`, `min-width: 0px`, and currently renders **1002px** wide. Its containing `<main>` computes to `overflow-x: visible` — there is no horizontal scroll container around the table, and no media query converts the table to a stacked layout.
- **Why it's a problem:** With `overflow-x: visible` and no wrapper, a table too wide for the viewport pushes the **whole document** into horizontal scrolling rather than scrolling within its own card — which drags the fixed sidebar and header around with it and is the classic cause of "the page is wider than the screen". With `table-layout: auto` the browser will instead try to compress columns, wrapping timestamps like `8/18/2026, 6:25:52 AM` and the OS string `linux Ubuntu 26.04 LTS` into unreadable stacks. Either way the primary data view degrades badly below roughly 1000px, and there is no CSS anywhere to prevent it. Violates Nielsen #8; relates to WCAG 1.4.10 (Reflow), which requires content to be usable at 320px without two-dimensional scrolling.
- **Fix:** Wrap every table in a container with `overflow-x: auto` and give the table a sensible `min-width` so columns keep their shape and the table — not the page — scrolls. Below ~768px switch to a stacked card-per-row layout, and prioritise which columns survive at each width.
- **Evidence:** `{"tableLayout":"auto","tableMinW":"0px","tableW":1002,"cols":7,"mainOverflowX":"visible"}`.

### [MEDIUM] 42 interactive elements fall below the 44x44 tap-target minimum, including every sidebar navigation item
- **Page:** /devices (chrome is global, so the sidebar figures apply to every route)
- **What happens:** Measuring every `button` and `a[href]` with a non-zero box, **42 elements** are under 44px in at least one dimension. These include all 15+ sidebar navigation links at **227x39**, and the "Collapse sidebar" control at **30x30**.
- **Why it's a problem:** 39px-tall nav rows and a 30px chevron are comfortable with a mouse but error-prone with a thumb, and this product declares `width=device-width` so it is presenting itself as touch-ready. The nav links fail the 44px guideline only on height, so the fix is a small padding change; the 30x30 chevron fails on both axes. Fails WCAG 2.5.5 (Target Size, AAA); the chevron is the main practical risk.
- **Fix:** Raise the minimum interactive height to 44px at touch widths (increase vertical padding on nav items rather than font size), and enlarge the sidebar toggle's hit area with padding or a transparent `::before` overlay while keeping its 30px visual size.
- **Evidence:** `{"smallTargetsCount":42,"smallTargets":["Collapse sidebar 30x30","Dashboard 227x39","AI Admin Copilot 227x39","Devices & Inventory 227x39","Health & Alerts 227x39","Security Logs 227x39","Patch Management 227x39","Software Deployment 227x39","Remote Control 227x39","Credentials 227x39","Backup & Restore 227x39","Provisioning & Imaging 227x39","Site Servers 227x39","Service Desk 227x39"]}`.

### [LOW] Positive: `prefers-reduced-motion` and `prefers-color-scheme` are both honoured
- **Page:** Global stylesheet
- **What happens:** The stylesheet includes `(prefers-reduced-motion: reduce)` and `(prefers-color-scheme: dark)` media blocks, and a `(hover: hover)` query that scopes hover styling to devices that actually support hover.
- **Why it's a problem:** N/A — recorded as a positive. Respecting reduced-motion is a WCAG 2.3.3 (Animation from Interactions) courtesy that many products skip, and gating `:hover` styles behind `(hover: hover)` is exactly the right technique for avoiding sticky hover states on touch devices — which makes the absence of any *layout* breakpoints more striking, since the author clearly knows how media queries work.
- **Fix:** N/A — extend the same care to viewport breakpoints.
- **Evidence:** Media-query inventory includes `(prefers-color-scheme: dark)`, `(prefers-reduced-motion: reduce)` and `(hover: hover)`.

### [LOW] Untested: actual rendering at 1280x800, 768x900 and 390x844
- **Page:** /devices, /service-desk, /dashboard
- **What happens:** The intended visual responsive sweep could not be performed — `resize_window` returned success at all three sizes but `window.innerWidth` remained **1313** throughout and screenshots were unchanged.
- **Why it's a problem:** N/A — recorded so the gap is not mistaken for a clean result. The findings above establish what the CSS *can* do; they do not substitute for seeing the pages render at those widths.
- **Fix:** N/A — the owner should verify visually in Chrome DevTools device emulation at 1280, 768 and 390, focusing on the devices/tickets tables and the fixed sidebar.
- **Evidence:** Three resize calls each returned "Successfully resized"; subsequent probes returned `{"innerWidth":1313,"innerHeight":887}`, `{"innerWidth":1313,"innerHeight":888}`.

---
## /backup — Backup & Restore

### [HIGH] Production page copy tells the user to read the page source to find out what works
- **Page:** /backup → page description, directly beneath the H1
- **What happens:** The page description reads verbatim: "File-level backup points captured from protected endpoints, with per-point verification and scoped restore. **See the module notes in this page's source for what has, and has not, run end to end.**"
- **Why it's a problem:** This is a developer note shipped into production UI copy. It instructs an end user — who may be an IT manager, an auditor, or a customer evaluating the product — to open the browser's view-source to discover which parts of the **backup and restore** module have actually been tested end to end. It simultaneously (a) exposes that some of the module is unproven, (b) provides no way for a normal user to act on that, and (c) undermines confidence in the one module whose entire value proposition is trustworthiness. This is the clearest instance of placeholder/dev content in the shipped product, alongside the `hello` job type in the /devices dispatch dropdown. Violates Nielsen #10 (help and documentation — help must be findable and usable) and #2.
- **Fix:** Remove the sentence from the UI. If parts of the module are not production-verified, say so explicitly and actionably in the interface ("Restore has not been verified end to end in this deployment"), or gate the unverified paths behind a feature flag. Move engineering caveats to the changelog or docs.
- **Evidence:** Page text, verbatim, as quoted above.

### [HIGH] "Run now" with an empty Device ID is a silent no-op — the eighth occurrence
- **Page:** /backup → "Device ID" text input + "Run now" button
- **What happens:** Clicking **Run now** with the Device ID field empty produces no error, no live region (`live: 0`), and no visible change — the page text is byte-identical before and after. The input carries `required: false`.
- **Why it's a problem:** Eighth independent instance of the identical defect across the audited routes (/assistant/settings, /health, /deploy, /remote, /service-desk, /credentials, /portal/new, here). On a backup module this is particularly poor: an operator triggering an ad-hoc backup before risky work has no way to know whether it started, and the stat cards ("Backup success 0%", "Protected endpoints 0 / 1", "Failed jobs 0") would not change either way. Violates Nielsen #1 and #9; fails WCAG 3.3.1/4.1.3.
- **Fix:** Mark the field required, validate before dispatch, and on success show a confirmation with a link to the queued job. Given the shared nature of this bug, fix it once in the form/submit layer rather than per page.
- **Evidence:** Post-click probe `{"live":0}` with `main` text unchanged from `"CONTINUITY\nBackup points\n…\nNo backup points recorded yet."`.

### [HIGH] A fifth hand-typed raw ID field — "Device ID" is the only way to target a backup
- **Page:** /backup → "Device ID" input (`aria-label="Device ID"`, placeholder "Device ID")
- **What happens:** The sole targeting control for "Run now" is a free-text input requiring a device UUID, with no picker, no search, no validation and no `required`. The stat card directly beside it reads "Protected endpoints **0 / 1**", so the system knows there is exactly one device — but will not offer it.
- **Why it's a problem:** This extends the raw-UUID problem to a fifth field across a third module (/deploy "Target ID", /service-desk "Requester ID"/"Assign to"/"Device ID", here). Auditor B found /licenses, /subscriptions and /enrollment use proper `<select>` lookups, and /portal/new correctly infers the user — so the platform demonstrably has the capability and applies it inconsistently. Roughly half the modules ask humans to type UUIDs. Violates Nielsen #6 and #4.
- **Fix:** Replace with the same device picker recommended for /deploy — and since /remote already implements exactly that (its Device select lists `Toeshee-sentinel` by hostname), reuse that component here.
- **Evidence:** `{"lab":null,"al":"Device ID","tag":"INPUT","type":"text","ph":"Device ID","req":false}`; stat card "Protected endpoints 0 / 1"; contrast with /remote's `{"lab":"Device","opts":["Select a device…","Toeshee-sentinel"]}`.

### [MEDIUM] "Backup success 0%" and "Protected endpoints 0 / 1" present an unprotected fleet as measured fact
- **Page:** /backup → stat cards
- **What happens:** The cards read "Backup success **0%**", "Protected endpoints **0 / 1**", "Failed jobs 0", "Needs verification 0", above the empty state "No backup points recorded yet."
- **Why it's a problem:** "0%" is a computed rate over zero runs — mathematically undefined, and displayed identically to a genuine 0% success rate, which would be a catastrophic condition. Meanwhile "Failed jobs 0" and "Needs verification 0" read as reassuring when the truth is that nothing has ever been backed up on any endpoint. The device-level Backup tab handles this better (it shows "—" and badges "not verified"/"not tested"), so the module contradicts itself between views. This is the same false-confidence pattern as /patch's four zeros, and again the device Health tab's honest phrasing ("This is not the same as healthy") shows the product knows better. Violates Nielsen #1.
- **Fix:** Show "—" or "Never run" instead of 0/0% until at least one backup has executed, and surface "0 of 1 endpoints protected" as an alert state rather than a neutral statistic.
- **Evidence:** Stat cards `Backup success 0% / Protected endpoints 0 / 1 / Failed jobs 0 / Needs verification 0` above `No backup points recorded yet.`

### [LOW] Timezone cross-check: no date/time inputs on this page
- **Page:** /backup
- **What happens:** Checked specifically for Auditor B's confirmed −5h `datetime-local` shift: this page contains **zero** `datetime-local`, `date` or `time` inputs (`dtl: 0`). Scheduling appears to live behind the "Policies" tab, which I did not reach before the run ended.
- **Why it's a problem:** N/A — a negative result recorded so the cross-check is not left ambiguous. The bug is neither confirmed nor refuted for this module.
- **Fix:** N/A — the /backup "Policies" tab should be checked for `datetime-local` fields and the −5h shift.
- **Evidence:** `dtl: 0` on /backup (Backup points tab); same probe returned `dtl: 0` on /notifications and /portal/new. No `datetime-local` field was encountered anywhere across my 16 audited pages.

---
## /email-intake — Email Intake

### [HIGH] `autocomplete="new-password"` is set correctly here but omitted on the two other secret fields in the product
- **Page:** /email-intake → "Add mailbox" modal → "Password" field
- **What happens:** The IMAP Password input is `type="password"` with **`autocomplete: "new-password"`** — exactly the correct hardening. The same product ships two other secret-bearing inputs with **`autocomplete: null`**: /credentials → "Secret" (`type=password`, the privileged vault secret) and /notifications → "Bearer token (optional)" (`type=password`). The "Username" field beside this password also sets `autocomplete: null`.
- **Why it's a problem:** This *sharpens* the earlier /credentials and /notifications findings from "the team didn't know" to "the team knows and applied it inconsistently" — which makes it a cheap, well-understood fix rather than a design question. The inconsistency is the defect: the least sensitive of the three secrets (an IMAP mailbox password) is protected, while the most sensitive (a domain-admin/SSH vault secret on /credentials) is not, so password managers may autofill and offer to save the privileged credential into the operator's personal browser profile. Violates Nielsen #4 (consistency) and #5.
- **Fix:** Apply `autocomplete="new-password"` to every `type="password"` input product-wide and `autocomplete="username"` (or `off`) to adjacent username fields; add a lint rule asserting no `type="password"` lacks an autocomplete attribute.
- **Evidence:** Here: `{"lab":"Password","type":"password","req":false,"ac":"new-password"}`. Compare /credentials: `{"lab":"Secret","type":"password","ac":null}` and /notifications: `{"lab":"Bearer token (optional)","type":"password","ac":null}`.

### [MEDIUM] The Mailboxes table exposes a "PASSWORD" column
- **Page:** /email-intake → Mailboxes table header
- **What happens:** The table's columns are `ADDRESS, SERVER, TRANSPORT, FOLDER, **PASSWORD**, ACTIVE, LAST POLLED, LAST ERROR`. No mailboxes are configured in this environment, so what the column actually renders (a mask, a status, or a value) could **not** be observed and is not claimed here.
- **Why it's a problem:** A column headed PASSWORD in a list view is a design smell regardless of its contents: it invites shoulder-surfing, it will appear in screenshots and screen shares, and it implies the value is retrievable — which contradicts the vault model /credentials describes ("never logged, returned by the API, or shown outside an audited checkout"). Even a masked column spends a table column on information that should be a boolean. Violates Nielsen #8 and the product's own security posture.
- **Fix:** Replace with a "Credentials" status column showing "Set" / "Not set" / "Rejected at last poll", and move any reveal/rotate action behind an audited per-row action.
- **Evidence:** Table header row: `ADDRESS SERVER TRANSPORT FOLDER PASSWORD ACTIVE LAST POLLED LAST ERROR`. **Untested:** the rendered cell contents, because no mailbox exists.

### [MEDIUM] Mailbox "Address" is `type="text"`, not `type="email"`
- **Page:** /email-intake → "Add mailbox" modal → "Address"
- **What happens:** The field that holds the mailbox's email address is a plain `type="text"` input with no `required` and no validation — mirroring the /notifications webhook URL field, which is `type="text"` rather than `type="url"` and accepted the literal string `not a url at all`.
- **Why it's a problem:** A malformed address will be stored and then fail silently at poll time, surfacing (if at all) in the "LAST ERROR" column rather than at the point of entry. Using the correct input type also gives mobile users the right keyboard. Second instance of the same "wrong input type for a structured value" pattern. Violates Nielsen #5.
- **Fix:** Use `type="email"` with `required`, validate on blur, and offer a "Test connection" action that verifies host, port, transport and credentials before the mailbox is saved as active.
- **Evidence:** `{"lab":"Address","type":"text","req":false,"ac":null}`; compare /notifications `{"lab":"URL","type":"text","urlValid":true}` for `not a url at all`.

### [MEDIUM] "Add mailbox" is the same shared modal — third confirmation of the four shared gaps
- **Page:** /email-intake → "Add mailbox" modal
- **What happens:** `aria-modal="true"`, `aria-label="Add mailbox"`, and the same defects as /deploy and /notifications: **not a `<form>`** (`form: false`), **no Escape handler** (`esc: "1->1"`), **no background scroll lock** (`body overflow: auto`), and **no `required`** on any of its nine controls.
- **Why it's a problem:** Third independent confirmation from my routes (plus Auditor B's /licenses, /subscriptions and /bcp) that every dialog in the product is one shared component carrying one shared set of defects. Nine controls here and not one is required, on a form that configures authenticated IMAP polling. Fails the WAI-ARIA dialog pattern; violates Nielsen #3 and #4.
- **Fix:** As stated for /deploy and /notifications — one fix to the shared modal component repairs Escape, initial focus, focus trapping, scroll lock and form semantics across at least six dialogs.
- **Evidence:** `{"modal":"true","label":"Add mailbox","form":false,"bodyOverflow":"auto","esc":"1->1"}`; all nine inputs `req: false`.

### [LOW] Disposition filter exposes `refused_loop` and other raw enum values
- **Page:** /email-intake → "Filter by disposition" select
- **What happens:** Options are `All, created, commented, reopened, quarantined, **refused_loop**` — lowercase database values, one with an underscore.
- **Why it's a problem:** `refused_loop` is meaningless to a user; it presumably denotes a mail loop the system declined to process, which is exactly the kind of event an admin needs to understand. Sixth module exhibiting the missing enum display-label layer (after /devices patch states, job types, /service-desk, /credentials, /notifications). Violates Nielsen #2.
- **Fix:** Map to human labels with tooltips ("Refused — mail loop detected"), via the single shared enum label map recommended throughout this report.
- **Evidence:** `{"lab":"Filter by disposition","opts":["All","created","commented","reopened","quarantined","refused_loop"]}`.

### [LOW] "Reopen window (days)" carries its unit in the label — the pattern /health should adopt
- **Page:** /email-intake → "Add mailbox" modal → "Reopen window (days)"
- **What happens:** This numeric field states its unit directly in the label.
- **Why it's a problem:** N/A — recorded as a positive and as a direct internal precedent for the /health Health-settings finding, where eight numeric fields carry **no** unit and mix seconds with days ("Alert retention 400" is days; its seven neighbours are seconds).
- **Fix:** N/A — apply this label convention to every numeric field in /health and elsewhere.
- **Evidence:** `{"lab":"Reopen window (days)","type":"number","req":false}` versus /health's `{"lab":"Alert retention","val":"400","min":"90","max":"3650"}` with no unit.

### [LOW] Double-hyphen used where an em dash is used elsewhere
- **Page:** /email-intake and /notifications page descriptions
- **What happens:** Both read "... before anything is written **--** verifying sender identity" / "... other control-plane modules **--** gated by a per-org egress allowlist", using a double hyphen, while /devices, /dashboard, /health and /credentials all use a true em dash "—" in the same construction.
- **Why it's a problem:** Minor typographic inconsistency visible in the first paragraph of two modules; suggests copy authored in different passes without a style pass. Violates Nielsen #4 and #8.
- **Fix:** Normalise to the em dash used elsewhere and add it to the content style guide.
- **Evidence:** `"...quarantining what it cannot trust rather than dropping it silently."` preceded by `"before anything is written --"`; compare /credentials `"decrypted only at the point of use — never logged"`.

---
## /provisioning — Provisioning & Imaging

### [BLOCKER] Provisioning cannot be started — the Profile dropdown is empty
- **Page:** /provisioning → "Provision" → "Provision a device" modal → "Profile" select
- **What happens:** The Profile select contains exactly one option, `"Select a profile…"` — there are no provisioning profiles to choose. The dialog's only other field is "Target ref (MAC / asset tag)" free text. With no profile selectable, the "Start" action cannot be completed, and the page behind reads "No provisioning runs yet." with "Machines provisioned 0". Nothing on the page or in the dialog explains that profiles must be created first, and there is no link to the "Profiles" tab that sits just above.
- **Why it's a problem:** The **fourth** empty-dropdown dead end in the product (after /assistant/settings "Model provider", /deploy "Package", /remote "Credential"), and the second that fully blocks a module's primary action. Endpoint provisioning is unreachable from its own entry point, and the user is given no diagnosis and no next step. Violates Nielsen #1, #9 and #5.
- **Fix:** Detect the empty state and replace the select with guidance plus a primary action ("No provisioning profiles exist. Create a profile →" linking to the Profiles tab). Disable Start with a stated reason. Apply one shared empty-dropdown pattern across all four occurrences.
- **Evidence:** `{"lab":"Profile","type":"select-one","req":false,"opts":["Select a profile…"]}`; page text `Success rate 0% / Machines provisioned 0 / Failed runs 0 / Median build time — / No provisioning runs yet.`

### [MEDIUM] "Provision a device" is the same shared modal — fourth confirmation
- **Page:** /provisioning → "Provision a device" modal
- **What happens:** `aria-modal="true"`, `aria-label="Provision a device"`, and again: **not a `<form>`** (`form: false`), **no Escape handler** (`esc: "1->1"`), **no scroll lock** (`body overflow: auto`), no `required` on either control.
- **Why it's a problem:** Fourth confirmation from my routes (with /deploy, /notifications, /email-intake), alongside Auditor B's /licenses, /subscriptions and /bcp — at least seven dialogs share one component and one defect set. Fails the WAI-ARIA dialog pattern.
- **Fix:** Single fix to the shared modal component, as described under /deploy.
- **Evidence:** `{"modal":"true","label":"Provision a device","form":false,"bodyOverflow":"auto","esc":"1->1"}`.

### [LOW] Methodology note: synthetic `.click()` failed where a real mouse click succeeded
- **Page:** /provisioning → "Provision" button
- **What happens:** A programmatic `element.click()` on the Provision button produced no dialog, but a real mouse click at (1246, 153) opened it correctly (`dialogs: 1`). The button is `type="submit"` with class `btn primary`.
- **Why it's a problem:** Not a product defect — recorded to document audit rigour and to protect the /patch/policies BLOCKER from doubt. That finding was confirmed with **both** a synthetic click and a real mouse click at (1240, 172), with no dialog, no navigation and no POST after either, so it is a genuine dead control rather than an artefact of the click method.
- **Fix:** N/A.
- **Evidence:** Synthetic click → `{"dialogs":0}`; real click at (1246,153) → `{"dialogs":1,"inputs":2}`.

---
## /site-servers — Site Servers

### [HIGH] Internal spec reference shipped in production UI copy
- **Page:** /site-servers → page description
- **What happens:** The description reads verbatim: "LAN-local helpers for authorized Wake-on-LAN and IPAM sweep preference today. A site server is a promoted Device; **cache and PXE delivery are specified but not yet built (site-server spec §4.4)**."
- **Why it's a problem:** A citation to an internal specification section number is meaningless to every user of the product and, like /backup's "See the module notes in this page's source", tells the reader that advertised functionality does not exist while giving them nothing to do about it. Two separate modules now carry engineering-internal caveats in their primary description — a pattern, not an accident. It also reads as an unfinished product in exactly the place a buyer or auditor would look. Violates Nielsen #2 (speak the users' language) and #10.
- **Fix:** Remove the spec citation. If the limitation must be disclosed, phrase it for users and place it where it is actionable ("Content caching and PXE delivery are not available in this release"), and track the spec reference in the changelog, not the UI.
- **Evidence:** Page description text, verbatim, as quoted above.

### [MEDIUM] The "Promote a device" dialog is a different variant — it has a real `<form>` but its controls are unlabeled and it has no Cancel
- **Page:** /site-servers → "Promote a device to site server" modal
- **What happens:** Uniquely in the product, this dialog **does** contain a real `<form>` (`form: true`) — the only one I found across seven dialogs. But its two primary controls have **no labels** (`lab: null` for both the device `<select>` and the text input), the checkbox's label is the raw lowercase string **`wake`**, and the button row is just an icon close plus **"Promote"** — there is **no Cancel button**, unlike every other dialog in the product. Escape still does nothing (`esc: "1->1"`) and there is still no scroll lock.
- **Why it's a problem:** Three inconsistencies at once. A screen-reader user hears an unlabeled combo box and an unlabeled text field in a dialog that promotes a device to infrastructure. The absence of Cancel — present on /deploy, /remote, /service-desk, /credentials, /notifications and /email-intake — removes the expected non-destructive exit, and with no Escape handler the only way out is the icon button. And the presence of a `<form>` here proves the shared modal *can* be form-based, making its absence elsewhere a straightforward fix. Fails WCAG 1.3.1, 3.3.2 and 4.1.2; violates Nielsen #3 and #4.
- **Fix:** Label both controls ("Device", "Site name"), sentence-case the checkbox label ("Enable Wake-on-LAN"), add a Cancel button matching the other dialogs, and bind Escape. Roll the `<form>` wrapper from this dialog into the shared component.
- **Evidence:** `{"label":"Promote a device to site server","form":true,"bodyOverflow":"auto","inputs":[{"lab":null,"type":"select-one","opts":["Select a device…","Toeshee-sentinel"]},{"lab":null,"type":"text","req":false},{"lab":"wake","type":"checkbox","req":false}],"btns":["(icon)","Promote"],"esc":"1->1"}`.

### [LOW] Positive: the best empty state in the product
- **Page:** /site-servers → main panel
- **What happens:** With no site servers configured the page reads: "**No site servers. Promote a device to add one.**" — it names the condition and states the exact next action, matching the "Promote device" button above it.
- **Why it's a problem:** N/A — recorded as a positive and as the model for the product's many inferior empty states, which variously say nothing actionable ("No compliance data yet.", "No deployments yet."), stuff the message into a table row with trailing empty cells (/notifications Deliveries, device Deploy tab), or use bare em-dashes and the lowercase word `none`.
- **Fix:** N/A — adopt this pattern (condition + next action) in the shared `<EmptyState>` component recommended throughout this report.
- **Evidence:** Page text: `No site servers. Promote a device to add one.`

### [LOW] Device selector here uses a proper dropdown, unlike /deploy and /backup
- **Page:** /site-servers → "Promote a device" modal → device select
- **What happens:** The device is chosen from a `<select>` listing `Select a device…` and `Toeshee-sentinel` by hostname — no UUID required.
- **Why it's a problem:** N/A — a positive that further isolates the raw-ID problem. Of the modules I audited, /site-servers, /remote and /portal/new resolve entities properly, while /deploy, /service-desk and /backup demand hand-typed UUIDs. Combined with Auditor B's finding that /licenses, /subscriptions and /enrollment use proper lookups, the raw-ID modules are a clear minority and the correct component already exists in at least three places.
- **Fix:** N/A — reuse this select in /deploy "Target ID", /backup "Device ID" and /service-desk "Device ID".
- **Evidence:** `{"type":"select-one","opts":["Select a device…","Toeshee-sentinel"]}`.

---
# FINAL COVERAGE — all runs

**Audited: 16 of 16 assigned routes (plus 6 sub-pages).**
Run 1: /login, /dashboard, /devices, /devices/:id (10 tabs), /assistant, /assistant/settings.
Run 2: /health, /security, /patch, /patch/policies.
Run 3: /deploy, /remote, /service-desk, /credentials.
Run 4: /portal, /portal/new, /notifications, /email-intake, /backup, /provisioning, /site-servers.

**Not reached:** the /backup "Policies" and "Repositories" tabs, the /provisioning "Images"/"Profiles" tabs, and the /notifications "Rules"/"Queue members"/"Allowlist" sub-panels — the parent pages were audited but these secondary tabs were not opened.

**Records I created that could not be cleaned up:**
- `/notifications` → webhook channel **`audit-a-test`** with URL `not a url at all`. **No delete affordance exists for channels** (only Disable/Test), so this must be removed server-side. It is disabled-capable but currently `active` and will fail every delivery with `scheme_not_allowed`.
- `/health` → profile `<b>audit-a</b>` was created and then deleted; the DELETE returned **503** while the UI reported success, so its true server-side state is unconfirmed.
- `/devices` → one `hello` job dispatched with an empty message; it is queued against an offline agent and cannot be cancelled (no cancel affordance exists).

**Cross-checks against Auditor B — final:**
- **Timezone `datetime-local` bug: NOT REPRODUCED in my 16 routes.** I probed every page for `datetime-local`/`date`/`time` inputs and found **zero across all of them** (`dtl: 0` on /notifications, /portal/new, /backup, /provisioning, /email-intake and everywhere else). B's bug is real but appears confined to the governance/planning modules (/changes, /bcp). The nearest equivalent in my half is /deploy's raw cron "Window" field, which states no timezone — same root cause (no timezone discipline), different symptom.
- **Raw-Pydantic-JSON error path: CONFIRMED once in my half** (/health settings). Combined with B's /subscriptions and /docs that is three modules. My half also produced a **fourth** distinct style: /notifications renders a bare enum, `Not delivered: scheme_not_allowed`. So the product has four error presentations — JSON dump, bare enum, one real human message (B's /reports), and silence.
- **`[role=alert]` unused: CONFIRMED.** Zero live regions on every one of my 16 routes and inside all seven dialogs, including after successful writes. B found the component exists on /enrollment. None of my routes is the exception.
- **Hand-typed UUIDs: CONFIRMED as a minority pattern.** Raw ID inputs in /deploy (1), /service-desk (4) and /backup (1). Proper entity pickers in /remote, /site-servers and /portal/new — plus B's /licenses, /subscriptions, /enrollment. The correct component exists in six places; six raw-ID fields remain.
- **Shared modal component: CONFIRMED four times** (/deploy, /notifications, /email-intake, /provisioning) — all `aria-modal=true` with a labelled close button, all missing Escape, initial focus, scroll lock and a `<form>`. One exception found: /site-servers' dialog **does** have a `<form>` but is missing labels and a Cancel button. One shared-component fix repairs at least seven dialogs.
- **No job cancel/retry: CONFIRMED** independently on /devices/:id.

**Passes still incomplete:**
- **Visual responsive sweep — attempted and blocked.** `resize_window` reported success at 1280x800, 768x900 and 390x844 but `window.innerWidth` stayed 1313 and screenshots were unchanged, so no narrow-viewport rendering was ever observed. Substituted a full stylesheet/computed-layout analysis (see the Responsive section) which is deterministic evidence about what the CSS can do, but is **not** a substitute for seeing the pages render.
- **Keyboard/focus pass — partial.** Escape was tested on all five dialogs I opened (no handler on any). Focus-on-open and focus-trapping could **not** be measured: the automation window lacked OS focus, so `document.activeElement` stayed `BODY` even after an explicit `.focus()` call. No focus-management claims are made anywhere in this report.
