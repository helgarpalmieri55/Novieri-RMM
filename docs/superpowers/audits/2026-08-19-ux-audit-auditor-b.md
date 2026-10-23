# ITMS UI/UX Audit — Auditor B
Date: 2026-08-19
Routes: /sla, /docs, /changes, /bcp, /reports, /audit, /licenses, /subscriptions, /budget, /ipam, /jobs, /enrollment, /assistant/autonomy, /platform-status, /settings, /error-monitor

## /sla — SLA Engine

### [BLOCKER] "Add policy" Save is a silent no-op when no calendar is selected
- **Page:** /sla (SLA policies → Add policy → Save button)
- **What happens:** With the form at its defaults (Any queue / Any priority / Calendar = "Select a calendar" / response 60 / resolution 480), clicking **Save** does nothing at all. No row is added to the SLA policies table, no inline error, no toast, no field turns red, the form stays open with the same values. `read_network_requests` after the click shows **zero** outbound requests — the click never reaches the server. Clicked twice to confirm; identical result both times.
- **Why it's a problem:** Nielsen #1 (visibility of system status) and #9 (help users recognize/diagnose/recover from errors). The user has no way to learn that a Business Calendar is a prerequisite. Since the page ships with zero calendars, a brand-new install cannot create its first SLA policy and gets no explanation why — the module is effectively dead on arrival for a new tenant.
- **Fix:** Validate on submit and render an inline error on the Calendar field ("Select a business calendar — a policy can't compute deadlines without one"). Better: disable the Save button while calendar is empty with an adjacent hint, and when zero calendars exist replace the Calendar select with an inline "Create your first business calendar" link.
- **Evidence:** `read_network_requests` (limit 20) returned "No network requests found" after two Save clicks; DOM query for `[role=alert],[aria-live]` returned `[]`; Calendar `<select>` has exactly 1 option ("Select a calendar").

### [HIGH] Calendar dropdown in the policy form is empty with no explanation or escape hatch
- **Page:** /sla (Add policy → "Calendar" select)
- **What happens:** The Calendar `<select>` contains a single option, the placeholder "Select a calendar". There is no "no calendars available" text, no disabled state, and no link to create one. The Business calendars section that would fix this is ~300px further down the page, below the form.
- **Why it's a problem:** Dead-end control. Nielsen #6 (recognition rather than recall) — the user must already know the dependency exists and scroll past the form to satisfy it.
- **Fix:** When the option list is empty, disable the select and show helper text with an inline "Add calendar" action that scrolls to / opens the calendar form.
- **Evidence:** JS `document.querySelectorAll('select')` → the calendar select reports `1 opts, val=""`.

### [MEDIUM] "Add policy" button does not reflect its toggled state; heading duplicates the button label
- **Page:** /sla (SLA policies card header)
- **What happens:** Clicking "Add policy" expands an inline form *below the empty-state row*, but the button keeps the label "Add policy" and gains no pressed/active styling. The expanded form is then introduced by a plain-text line that also reads "Add policy", so the label appears twice ~300px apart.
- **Why it's a problem:** Ambiguous affordance — it's unclear whether clicking again adds a second form or closes this one. Duplicated labels also make the screen-reader experience confusing (no `aria-expanded`).
- **Fix:** Toggle the button to "Cancel" (or add `aria-expanded="true"` and a pressed style) while the form is open, and drop the duplicated inline "Add policy" text since the button already names the action.
- **Evidence:** Screenshot shows "Add policy" as a card-header button at y≈273 and again as body text at y≈382.

### [MEDIUM] Inline form is injected below the empty-state message rather than replacing it
- **Page:** /sla (SLA policies)
- **What happens:** With the Add-policy form open, the table still shows "No policies yet — with none configured, no ticket gets an SLA at all." directly above the form the user is filling in.
- **Why it's a problem:** The empty state contradicts the user's current action; the message is about a table that is no longer the focus. Minor but repeated (same pattern in Business calendars).
- **Fix:** Hide the empty-state row while the create form is open, or render the create form as a real dialog/drawer.
- **Evidence:** Screenshot at 1440x900 with form open.

### [LOW] Field labels sit inline and wrap mid-phrase, splitting label from control
- **Page:** /sla (Add policy form)
- **What happens:** The labels flow inline with the inputs, so at 1440px the string "Response target in minutes (1h)" breaks across two lines — "Response target" ends line 1 next to the Calendar select, and "in minutes (1h)" starts line 2 next to its input.
- **Why it's a problem:** Visually associates the wrong label fragment with the wrong control; harms scanability (Gestalt proximity).
- **Fix:** Use a stacked label-over-input grid (or CSS grid with fixed label column) so a label never wraps away from its field.
- **Evidence:** Screenshot at 1440x900 — "Response target" at x≈1200 y≈432, "in minutes (1h)" at x≈350 y≈469.

### [LOW] Page `<title>` is the constant string "ITMS" on every route
- **Page:** /sla (and every route checked so far: /login, /devices, /sla)
- **What happens:** `document.title` === "ITMS" regardless of route.
- **Why it's a problem:** WCAG 2.4.2 Page Titled (A). Browser tabs, history, and bookmarks are indistinguishable; screen readers announce the same title on every navigation so route changes are not announced.
- **Fix:** Set a per-route title, e.g. "SLA Engine — ITMS".
- **Evidence:** JS `document.title` → "ITMS" on /sla; page text extraction confirmed same on /login and /devices.

### [HIGH] Destructive Delete executes immediately with no confirmation and no undo
- **Page:** /sla (SLA policies row → "Delete"; Business calendars row → "Delete")
- **What happens:** Clicking "Delete" on the SLA policy I had just created removed the row instantly. No confirm dialog, no "are you sure", no toast, no Undo affordance. DOM query for `dialog, [role=dialog], [role=alertdialog]` returned **0** elements. The table reverted straight to the empty state.
- **Why it's a problem:** Nielsen #3 (user control and freedom) and #5 (error prevention). An SLA policy encodes contractual response/resolution commitments; deleting one silently strips SLAs from every ticket that resolved to it. "Delete" sits directly beside "Edit" in the same row with identical size and no destructive colour, so a mis-click is one pixel of travel away from irreversible data loss.
- **Fix:** Add a confirmation step naming the record ("Delete policy for Any queue / Any priority? Tickets currently using it will lose their SLA clocks.") or an undoable toast with a 10s window. Style Delete as a destructive (red/outline-danger) action distinct from Edit.
- **Evidence:** Verified on the policy I created (Any queue / Any priority / 30m / 4h). Same undifferentiated Close|Edit|Delete trio appears on the Business calendars row.

### [HIGH] Number fields accept negative and zero SLA targets; label echoes the nonsense value
- **Page:** /sla (Add policy → "Response target in minutes" / "Resolution target in minutes")
- **What happens:** Typing `-50` into Response target is accepted by the input and the **field label live-updates to "Response target in minutes (-50m)"**, presenting a negative service-level duration as if it were a normal value. Typing letters (`abc`) into Resolution target is rejected by the number input, leaving it blank — and the label then reads "Resolution target in minutes (0m)", i.e. an empty field is presented as a 0-minute (instantly-breached) target. Neither field shows an error at any point.
- **Why it's a problem:** Nielsen #5 (error prevention) and #9. The helper text actively confirms an impossible configuration, which is worse than staying silent — it looks like the system understood and accepted it. A 0-minute resolution target would breach every ticket the moment it is created.
- **Fix:** Set `min="1"` on both inputs, validate on blur with an inline message, and render the parenthetical hint as "—" rather than "(-50m)"/"(0m)" when the value is out of range.
- **Evidence:** Screenshot at 1440x900 showing "Response target in minutes (-50m)" beside an input containing `-50`, and "Resolution target in minutes (0m)" beside an empty input.

### [HIGH] Same silent-no-op failure repeats on every form in the module
- **Page:** /sla (Add calendar → Save; Add holiday → "Add holiday"; Add policy → Save)
- **What happens:** Three separate empty/invalid submits, three identical dead clicks:
  1. "Add calendar" Save with an empty Name → nothing happens, no error.
  2. "Add holiday" with an empty Holiday date → nothing happens, holiday table still says "No holidays on this calendar."
  3. "Add policy" Save with `-50` response / blank resolution and a valid calendar selected → nothing happens, policies table still empty.
  The same forms succeed instantly once given valid values, proving the validation runs — it simply never surfaces.
- **Why it's a problem:** This is a systemic pattern, not a one-off. Nielsen #1/#9. A user who hits it concludes the app is broken or the button is dead; the only recovery is guesswork.
- **Fix:** Introduce one shared form-error component used by every create form in the module — inline per-field messages plus `aria-live="polite"` so assistive tech announces the failure. Never let a submit handler return without rendering either a success or a failure state.
- **Evidence:** DOM query for `[role=alert],[aria-live]` returned `[]` after each failed submit; `read_network_requests` recorded zero requests for the policy submit.

### [MEDIUM] Timezone field's placeholder doubles as the silently-applied default value
- **Page:** /sla (Add calendar → "Timezone (IANA name)")
- **What happens:** The field renders greyed "Europe/London", which is a `placeholder`, not a value (`input.value` was empty). Saving with the field untouched produced a calendar whose Timezone column reads **Europe/London** — so the placeholder was applied as the real default.
- **Why it's a problem:** Placeholder-as-default is a classic dark pattern for accidental data entry (WCAG 3.3.2 Labels or Instructions). A US or APAC operator would create a London-timezone SLA calendar without ever noticing, and every deadline computed from it would be hours off.
- **Fix:** Pre-fill the field with an actual value (ideally the browser's `Intl.DateTimeFormat().resolvedOptions().timeZone`) so the user sees committed text, or make the field required. Use a searchable IANA timezone picker rather than free text.
- **Evidence:** `input.placeholder === "Europe/London"` with empty `value`; created row shows Timezone = Europe/London.

### [MEDIUM] Timezone free-text field has no validation or autocomplete for IANA names
- **Page:** /sla (Add calendar → Timezone)
- **What happens:** The timezone is a plain text input labelled "Timezone (IANA name)". Nothing constrains it to a real zone; there is no dropdown, no autocomplete, no format example beyond the placeholder.
- **Why it's a problem:** Nielsen #6. IANA names are exact strings ("America/New_York", not "EST" or "New York"); a typo silently produces a calendar that cannot compute deadlines. Recall-based entry of a 400-item enumeration is an anti-pattern.
- **Fix:** Replace with a combobox populated from `Intl.supportedValuesOf('timeZone')`, defaulting to the browser zone.
- **Evidence:** `read_page` shows `textbox [ref_41] placeholder="Europe/London"` with no `list`/`datalist` attribute.

### [MEDIUM] Time inputs in the Working hours editor are too narrow; the value is visibly clipped
- **Page:** /sla (calendar detail → Working hours → Monday time range)
- **What happens:** The start/end time inputs render "12:00 AM" but the trailing "M" is cut off by the input's right edge (reads "12:00 AN" / "12:00 AM" truncated), with the clock icon overlapping the text area.
- **Why it's a problem:** Truncated values are ambiguous — AM vs PM is exactly the distinction that matters for a business calendar, and it's the character being clipped. Getting it wrong shifts every SLA deadline by 12 hours.
- **Fix:** Widen the time inputs by ~24px or move the clock affordance outside the field box.
- **Evidence:** Screenshots at 1440x900 consistently show the meridiem character clipped in both the start and end inputs.

### [MEDIUM] Date format is US-locale ("8/19/2026, 6:51:22 AM") on a calendar whose default timezone is Europe/London
- **Page:** /sla (Business calendars table → CREATED column)
- **What happens:** The created timestamp renders as `8/19/2026, 6:51:22 AM` — M/D/YYYY with 12-hour time and seconds precision.
- **Why it's a problem:** Internally inconsistent with the module's own Europe/London default, and ambiguous internationally (8/19 vs 19/8). Second-level precision is noise in a "created" column.
- **Fix:** Use one app-wide date utility with an unambiguous format (`19 Aug 2026, 06:51`) or a relative "2 minutes ago" with the absolute value in a tooltip. Track in the cross-cutting section.
- **Evidence:** Table cell text `8/19/2026, 6:51:22 AM`.

### [LOW] Row action buttons are right-aligned with unequal widths, producing a ragged left edge
- **Page:** /sla (Working hours day rows: "Add window (Monday)" … "Add window (Wednesday)")
- **What happens:** Each day row's buttons embed the day name, so the pair's total width varies per row; because they are right-aligned the left edge steps in and out by ~40px down the list.
- **Why it's a problem:** Visual noise; the eye can't establish a single scan column (Gestalt continuity). The day name is already the row header, so it is redundant in the label.
- **Fix:** Fix the action column width and label the buttons "Add window" / "24-hour day", keeping the day name only in an `aria-label`.
- **Evidence:** Screenshot — Monday's "Add window" starts at x≈969, Wednesday's at x≈947, Saturday's at x≈962.
### [HIGH] No "skip to content" link — 36 tab stops before reaching the main content, on every route
- **Page:** /sla (and every route; the sidebar is global)
- **What happens:** The first focusable element is the sidebar collapse button, followed by ~28 navigation links, the org switcher, the account menu and Sign out. Measuring the focusable order in the DOM, the first element inside `<main>` is at index **36**. There is no skip link (`document.querySelectorAll('a')` filtered for /skip/i → **0** results).
- **Why it's a problem:** WCAG 2.4.1 Bypass Blocks (Level A) failure. A keyboard-only or switch-access user must traverse 36 controls to reach the page they navigated to, on every single page load. Screen-reader users can use landmarks (MAIN/NAV are present, which helps), but keyboard-only sighted users have no escape.
- **Fix:** Add a visually-hidden-until-focused `<a href="#main">Skip to main content</a>` as the first element in the DOM, and give `<main>` `tabindex="-1"` so focus lands there.
- **Evidence:** JS audit — `totalFocusable: 69`, `tabsBeforeMainContent: 36`, `skipLink: 0`, landmarks present `[ASIDE, NAV, HEADER, MAIN, NAV]`.

### [HIGH] Table column headers fail WCAG contrast at a 9px font size
- **Page:** /sla (all three tables: SLA policies, Business calendars, Holidays) — and repeated app-wide
- **What happens:** Column headers ("Queue", "Priority", "Calendar", "Response", "Resolution", "On breach", "Active", "Name", "Timezone", "Health", "Created", "Date") render as `rgb(135, 133, 144)` on `rgb(251, 250, 252)` at **9px**, uppercase. Measured contrast **3.49:1** against a 4.5:1 requirement.
- **Why it's a problem:** WCAG 1.4.3 Contrast (Minimum), Level AA failure. Compounded by a 9px type size and uppercase letterforms, which are the hardest combination to read. Column headers are the only thing that make a table's data interpretable.
- **Fix:** Darken to at least `rgb(90, 88, 100)` (≈6.5:1) and raise the size to 11–12px. Uppercase micro-type should be the exception, not the table-header default.
- **Evidence:** Computed-style contrast audit over `<main>` returned 25 failing nodes, of which 12 are table headers all at 3.49:1 / 9px.

### [MEDIUM] Sidebar section labels fail contrast (3.9:1) at 9px
- **Page:** all routes (sidebar: "OVERVIEW", "FLEET", "CONTINUITY", "SERVICE", "GOVERNANCE", "ASSETS & SPEND", "PLATFORM")
- **What happens:** Section labels are `rgb(119, 115, 128)` at 9px/800 weight on the sidebar's dark gradient (`linear-gradient(rgb(11,11,15), rgb(17,16,22))`). Measured ≈**3.9:1** against the 4.5:1 small-text requirement.
- **Why it's a problem:** WCAG 1.4.3 AA. These labels are the only grouping cue in a 28-item navigation list; if they're hard to see, the list reads as one undifferentiated column.
- **Fix:** Lighten to ≈`rgb(150, 146, 162)` (≈5.6:1) or increase size to 11px.
- **Evidence:** Computed `color: rgb(119,115,128)`, `font-size: 9px`, `font-weight: 800`; sidebar `background-image: linear-gradient(rgb(11,11,15) 0%, rgb(17,16,22) 100%)`.

**Verification note:** an initial naive contrast pass flagged ~70 nodes, but most were false positives caused by backgrounds painted via `background-image` gradients (the sidebar, and the purple primary buttons). I re-resolved those manually — the primary button is white on `rgb(111,54,217)` ≈ **6.6:1** and passes. Only the two items above are confirmed failures.

## /docs — IT Documentation

### [BLOCKER] Raw API validation JSON is rendered to the user as the error message, and overflows the card
- **Page:** /docs (New article → Title → Save)
- **What happens:** Saving an article with a 512-character title renders this literal string in red below the editor:
  `Error: {"detail":[{"type":"string_too_long","loc":["body","title"],"msg":"String should have at most 300 characters","input":"AuditB Long LLLLLLLL…(the entire 512-char input echoed back)…","ctx":{"max_length":300}}]}`
  The echoed input is one unbroken token, so the block breaks out of the editor card and runs to the right edge of the 1440px viewport.
- **Why it's a problem:** Nielsen #9 — error messages must be in plain language, precisely indicate the problem, and suggest a fix. This does none of those; it exposes the backend framework (Pydantic/FastAPI shape), the internal field path `["body","title"]`, and dumps the user's own input back at them. It also breaks the layout. WCAG 3.3.1 Error Identification is not met: the error is not programmatically associated with the Title field, and 3.3.3 Error Suggestion is not met.
- **Fix:** Map API errors to per-field messages. Render inline under Title: "Title must be 300 characters or fewer (currently 512)." Never `JSON.stringify` a server error into the DOM; log it to the console instead. Add `overflow-wrap: anywhere` as a belt-and-braces guard.
- **Evidence:** Screenshot at 1440x900 shows the JSON string spanning x≈645 to x≈1298 across three wrapped lines with the third line clipped at the viewport edge.

### [HIGH] Title field has no maxlength, so the 300-character limit is only discoverable by failing
- **Page:** /docs (New article → Title)
- **What happens:** `input.maxLength` is **-1** (unset). The field silently accepted 512 characters; the 300-char limit exists only server-side and is revealed only by the raw-JSON error above. There is no character counter and no hint text.
- **Why it's a problem:** Nielsen #5 (error prevention beats error messages). A user pasting a long title loses the round-trip and gets a hostile error.
- **Fix:** Set `maxlength="300"` and show a live "n/300" counter that turns amber near the limit.
- **Evidence:** `document.querySelector(title input).maxLength === -1`; server responded `{"max_length":300}`.

### [HIGH] Error surfacing is inconsistent within the same form: empty submit is silent, over-long submit dumps JSON
- **Page:** /docs (New article → Save)
- **What happens:** Clicking Save with an entirely empty form (no title, no body) does nothing — no error, no toast, editor stays open, article count stays at 1, `[role=alert]` query returns `[]`. Clicking Save with an over-long title renders the raw JSON error. Two failure modes of the same button behave completely differently.
- **Why it's a problem:** The user cannot form a mental model of what the Save button does when it fails. Nielsen #4 (consistency and standards).
- **Fix:** Route every failure — client-side and server-side — through one error component anchored to the offending field.
- **Evidence:** Two consecutive tests on the same form, described above.

### [HIGH] Markdown article headings, lists and links are rendered with no typographic styling
- **Page:** /docs (article body pane, and the live Preview pane)
- **What happens:** Markdown is correctly *parsed* into semantic HTML — the DOM contains `<h1>Heading</h1>`, `<p>…</p>`, `<ul><li>item one</li><li>item two</li></ul>` — but almost no CSS is applied to it. The article `<h1>` computes to **`font-size: 14px; font-weight: 400; margin: 0px`**, byte-identical to body paragraph text. The `<ul>` renders with no bullets and no indentation. The result is a flat stack of same-size lines with no visual hierarchy at all. (For contrast, the page's own `<h1>` "IT documentation" is 27px.)
- **Why it's a problem:** This is the core value of a documentation module — a runbook that renders as an undifferentiated wall of text cannot be skimmed under incident pressure, which is exactly when runbooks are read. It also breaks the WCAG 1.3.1 Info and Relationships intent in the visual channel: structure exists in the markup but is invisible on screen.
- **Fix:** Add a scoped typography stylesheet for the rendered-markdown container: h1 24px/600, h2 19px/600, h3 16px/600 with top margins, `ul{list-style:disc;padding-left:1.25rem}`, `p{margin-block:0.75rem}`, styled `blockquote`, `pre` and `code`.
- **Evidence:** `getComputedStyle` on the article `<h1>` → `14px / 400 / margin 0px`; `innerHTML` of the body container → `<h1>Heading</h1>\n<p>bold html</p>\n<p><a>link</a></p>\n<ul>\n<li>item one</li>\n<li>item two</li>\n</ul>`.

### [MEDIUM] The global table style force-uppercases markdown table headers, altering the author's content
- **Page:** /docs (rendered markdown tables in article body and Preview)
- **What happens:** A markdown table authored with lowercase headers `| a | b |` renders as **A** and **B**. Computed style on the `<th>` is `text-transform: uppercase; font-size: 9px; color: rgb(135,133,144)`. The app's chrome table style is being applied to user-authored content.
- **Why it's a problem:** Documentation frequently contains case-sensitive headers — `pid`, `iPhone`, `kubectl`, `DNSName`. Silently uppercasing them makes the documentation wrong. Author intent should never be overridden by chrome styling, and 9px grey is unreadable inside body copy.
- **Fix:** Scope the uppercase/9px table-header rule to `.data-table th` rather than a bare `th`, and give markdown tables their own readable style (12–13px, sentence case, normal weight contrast).
- **Evidence:** `th.textContent === "a"` while `getComputedStyle(th).textTransform === "uppercase"`; rendered pixel shows "A".

### [MEDIUM] Markdown editor textarea is 190px wide inside a ~650px card
- **Page:** /docs (New article / Edit → Markdown textarea)
- **What happens:** `getBoundingClientRect()` on the textarea returns `width: 190, height: 256` while the editor card is roughly 650px wide, leaving a ~70px empty gutter to its left and the Preview pane to its right. A markdown table wraps after ~9 characters (`| a | b |` occupies a full line).
- **Why it's a problem:** Authoring markdown tables, code blocks or long links in a 190px column is impractical — line wrapping destroys the visual alignment markdown depends on.
- **Fix:** Make the textarea fill its column (`width: 100%`), and give the editor/preview a 50/50 split with a minimum editor width of ~360px.
- **Evidence:** `{"ta":{"x":709,"y":387,"w":190,"h":256}}`.

### [MEDIUM] "Markdown" label sits below and to the left of its textarea while "Preview" sits above its pane
- **Page:** /docs (editor)
- **What happens:** The label "Markdown" is rendered at y≈629, i.e. near the *bottom* of a textarea that spans y 387–643, and horizontally to its left. The adjacent "Preview" label is at the top of its pane in the conventional position.
- **Why it's a problem:** Two labels for two side-by-side panes placed by two different rules. Nielsen #4. A user scanning left-to-right reads the editor before finding out what it is.
- **Fix:** Place both labels above their panes, same style, same baseline.
- **Evidence:** measured `textarea` rect y 387–643 vs "Markdown" label y 629; screenshot.

### [MEDIUM] Article editor is not a dialog: Escape does not close it and focus is never moved into it
- **Page:** /docs (New article / Edit)
- **What happens:** Opening the editor leaves `document.activeElement` on the "New article" button (`BUTTON.btn primary`) — focus is not moved to the Title field or the editor container. Pressing **Escape** with the editor open leaves it open (`!!document.querySelector('textarea') === true` after Escape).
- **Why it's a problem:** WCAG 2.1.2 and standard dialog conventions. Keyboard users must Tab forward blindly to reach the first field, and the universally-expected Escape-to-dismiss does nothing. (Note: the editor is an inline panel, not a modal overlay, so focus *trapping* is not required — but Escape-to-cancel and initial focus still are.)
- **Fix:** Focus the Title input on open; bind Escape to the same handler as Cancel; return focus to the triggering button on close.
- **Evidence:** `document.activeElement` → `BUTTON/btn primary` immediately after opening; textarea still present after Escape keypress.

### [MEDIUM] Selecting an article does not change the URL — articles are not linkable
- **Page:** /docs (article list → article detail pane)
- **What happens:** Clicking "AuditB Runbook" loads the article into the right pane but `location.pathname` remains `/docs`. There is no article id in the URL, no query param, no hash.
- **Why it's a problem:** A documentation system whose articles cannot be linked to is severely reduced in value — you cannot paste a runbook link into a ticket, an incident channel, or another article. Browser Back also cannot return to the previously-viewed article, and refresh loses the selection.
- **Fix:** Route to `/docs/:articleId` (or `?article=`) and restore the selection on load. Add a "Copy link" affordance in the article header.
- **Evidence:** `location.pathname === "/docs"` after selecting the article.

### [LOW] Blocked `javascript:` links render as indistinguishable plain text with no indication they were stripped
- **Page:** /docs (article body)
- **What happens:** The stored article contains `[link](javascript:alert(2))`. The renderer correctly refuses the dangerous scheme and emits `<a>link</a>` **with no href** — so the word "link" appears as ordinary body text, unstyled and inert.
- **Why it's a problem:** The security behaviour is right, but the author gets no feedback that their link was rejected; they see a link in the source and a word in the output and will assume a rendering bug. (Reported as LOW precisely because the sanitisation itself is correct — see Strengths.)
- **Fix:** In the Preview pane, render stripped links with a strikethrough/warning affordance and a tooltip: "Link removed — unsupported URL scheme."
- **Evidence:** rendered `innerHTML` contains `<p><a>link</a></p>` with no `href` attribute; source markdown is `[link](javascript:alert(2))`.

### [BLOCKER] The default value of the "New record" Type dropdown can never be saved, and the error tells the user to call the REST API by hand
- **Page:** /docs (Records → New record → Type)
- **What happens:** The Type `<select>` has four options — network, service, vendor, location — and defaults to **`network` (selectedIndex 0)**. Submitting with that default returns:
  `Error: {"detail":"record_type 'network' is superseded by the IPAM address plan: create the subnet with POST /api/v1/ipam/subnets instead. Existing network records remain readable, editable and archivable, and POST /api/v1/ipam/import/doc-records converts them"}`
- **Why it's a problem:** Two compounding defects. (1) The **default** option of the primary field in this form is a value the backend always rejects — every user who doesn't change the dropdown hits a wall. (2) The remediation offered to a UI user is to issue raw HTTP calls (`POST /api/v1/ipam/subnets`, `POST /api/v1/ipam/import/doc-records`) — a console user has no way to act on that. It is also rendered as a raw JSON blob (`{"detail":…}`) rather than a message. Nielsen #9 and #10.
- **Fix:** Remove `network` from the options (or render it disabled with the label "network — moved to IPAM"), default the select to `service`, and replace the message with plain text plus a real in-app link: "Network records now live in IP Address Management. **Create a subnet →** /ipam". Never print `{"detail":…}` to the DOM.
- **Evidence:** `select.options` → `["network|network","service|service","vendor|vendor","location|location"]`, `selectedIndex: 0`; error string captured verbatim above.

### [HIGH] Server error text persists after the user corrects the input and is never cleared
- **Page:** /docs (New record form)
- **What happens:** After the `network` error appeared, I changed Type to `service` and filled a valid Name and valid `{"vlan":42}` Fields. The stale error about `record_type 'network'` **remained on screen unchanged**, still contradicting the form's current state.
- **Why it's a problem:** Nielsen #1. A persistent, now-false error makes the user believe their correction didn't register, and there's no way to distinguish "old error" from "new error" because the text never changes.
- **Fix:** Clear the form-level error on any field change, and re-render it only from the latest submit response.
- **Evidence:** DOM read after the change showed `{type:"service", name:"AuditB TempRecord", fields:'{"vlan":42}'}` while the `record_type 'network'` error was still in `main.innerText`.

### [HIGH] Invalid JSON in the "Fields (JSON)" editor produces no error at all
- **Page:** /docs (New record → Fields (JSON))
- **What happens:** The Fields control is a bare textarea pre-filled with `{}`. Entering the unparseable string `{not valid json` together with an empty Name and clicking **Create** does nothing — no error, no record, no highlight. (Contrast: the `network` type error *does* render.)
- **Why it's a problem:** Same silent-no-op class as /sla, plus the deeper issue that a structured-record editor is asking end users to hand-write JSON with no syntax validation, no formatting, no schema hint and no example of what keys a `service` record should contain.
- **Fix:** Validate JSON on blur with an inline parse error including the character offset; better, replace the raw textarea with a key/value field editor driven by the record type's schema. At minimum add a "Format JSON" button and a monospace font.
- **Evidence:** `Create` clicked with `{not valid json` and empty Name → record count stayed at 1, no `[role=alert]` node, no visible message.

### [MEDIUM] Search renders an empty "Articles" group header when a query matches only records
- **Page:** /docs (search box)
- **What happens:** Searching `a` renders a group header "Articles" with **no items beneath it**, followed by a "Records" group containing "AuditB Net". Searching `zzzznomatch` correctly collapses to a single "No documentation matches that search." message.
- **Why it's a problem:** An empty group heading reads as a loading state or a bug. Inconsistent with the well-handled zero-results case right next to it.
- **Fix:** Suppress a group header when its result set is empty.
- **Evidence:** `main.innerText` while searching "a" → `"Articles\n\nRecords\n\nAuditB Netservice"`.

### [LOW] Search field has no clear ("×") affordance
- **Page:** /docs (Search documentation…)
- **What happens:** Once a query is typed the only way back to the full list is to select the text and delete it manually. There is no clear button and the input is not `type="search"` (which would give the native clear control).
- **Why it's a problem:** Minor friction on a control users will hit constantly.
- **Fix:** Use `type="search"` or add an × button that also returns focus to the input.
- **Evidence:** `read_page` reports `textbox "Search documentation…"` with no adjacent button.

### [LOW] Record type appears twice on every record chip
- **Page:** /docs (Records list)
- **What happens:** In search results a record renders as `AuditB Net` followed immediately by a `service` pill; in the Records list below it renders as a `service` pill followed by `AuditB Net`. Same two pieces of information, opposite order in two lists 100px apart.
- **Why it's a problem:** Nielsen #4 (consistency). Also makes the two lists look like different object types.
- **Fix:** Pick one order (pill first reads better as a type prefix) and use one shared record-chip component.
- **Evidence:** `main.innerText` contains both `"AuditB Netservice"` (search result) and `"service\nAuditB Net"` (records list).

---

## Strengths

1. **Consequence-first empty states.** Empty tables don't just say "no data" — they say what the absence *means*: "No policies yet — with none configured, no ticket gets an SLA at all."; "No calendars yet — a policy needs one before any ticket can get an SLA." This is genuinely good microcopy and rare in admin tooling.
2. **Health is surfaced as a first-class column, not hidden.** A business calendar with no working windows gets a `no working windows` pill in the table *and* an explanatory red banner in the detail view *and* a warning in the Working hours header — three tiers of the same warning, escalating in detail. After adding a 24-hour Monday it flipped to `ok` immediately.
3. **Unsaved-changes state on the Working hours editor.** Modifying a day renders "Unsaved changes" beside the Save week / Revert pair, and a Revert action is provided. This is the one place in the audit where the app got optimistic-UI state management right.
4. **Explanatory copy sits next to the decision it affects.** "Resolution is first match wins, most specific first…", "Saving replaces the whole week and recomputes every still-running deadline on this calendar.", "A breach escalation deliberately does not recompute the deadline — a system priority bump is the engine reacting to its own output, not new information about the work." This is design-rationale-as-help-text and it substantially reduces the need for external docs.
5. **HTML injection is correctly neutralised everywhere I probed.** A calendar named `<b>xss-b</b> AuditB Calendar` renders as literal text in the table, the detail heading and the policy dropdown (`document.querySelector('td b, td strong')` → null). In markdown, `<img src=x onerror=alert(1)>` and `<b>bold html</b>` are stripped, and `[link](javascript:alert(2))` is emitted as an `<a>` with the dangerous href removed. No alert ever fired.
6. **Live markdown preview is accurate and instant.** Typing into the editor updates the Preview pane with no perceptible delay, and the preview is rendered by the same pipeline as the saved article — including GFM tables — so what you preview is what you get.
7. **Form controls are properly labelled.** A programmatic check on /sla (`input`/`select`/`textarea` without `aria-label`, `aria-labelledby`, wrapping `<label>`, or `label[for]`) returned **0 unlabelled controls of 3**. Landmarks are present (`ASIDE`, `NAV`, `HEADER`, `MAIN`, `NAV`) and heading order on /sla is clean (one H1, then H2s).
8. **Focus rings are real and visible.** Tabbing to "Add window (Sunday)" produced a clearly visible dark ring (`outline: rgb(17,17,17) auto`) around the button. Focus visibility is implemented, not suppressed with `outline: none`.
9. **The "Which policy applies?" simulator.** A queue+priority picker that answers "which policy would a ticket in here resolve to?" is exactly the right affordance for a first-match-wins rule engine — it turns an opaque precedence system into something testable before you rely on it.
10. **Zero-results search state is well handled** on /docs: an illustrative icon plus "No documentation matches that search." replaces the list entirely, rather than showing an empty box.

## Cross-cutting patterns

1. **Silent no-op is the default failure mode.** Confirmed on five separate submits across two modules: /sla Add policy (empty), /sla Add policy (invalid numbers), /sla Add calendar (empty name), /sla Add holiday (empty date), /docs New article (empty), /docs New record (invalid JSON). In every case the button click produced no error, no toast, no field highlight, no `[role=alert]` node — and in the /sla policy case, no network request at all. Users cannot distinguish "validation rejected me" from "the button is broken".
2. **When errors *do* appear, they are raw backend JSON.** `Error: {"detail":[{"type":"string_too_long","loc":["body","title"],…}]}` and `Error: {"detail":"record_type 'network' is superseded by… POST /api/v1/ipam/subnets…"}`. Two different modules, same anti-pattern: `JSON.stringify(serverError)` dropped into the DOM. These also leak internal API paths and the framework's validation shape, and they overflow their containers.
3. **Errors are never cleared.** A stale error on /docs stayed on screen after every field it referred to had been corrected.
4. **Inline label/control layout wraps unpredictably.** Labels flow inline with their inputs rather than sitting in a grid, so at 1440px "Response target in minutes (1h)" splits across two lines and lands next to the wrong control; the /docs "Markdown" label ends up at the *bottom* of its textarea while its sibling "Preview" label sits at the top of its pane.
5. **9px grey micro-type is used for all table headers app-wide** at 3.49:1 contrast — a WCAG 1.4.3 AA failure repeated on every table on every page. The same rule leaks into user-authored markdown tables and force-uppercases the author's own header text.
6. **Destructive actions are undifferentiated and unconfirmed.** "Delete" sits beside "Edit"/"Close" in the same neutral pill style, at the same size, with no confirmation step and no undo.
7. **Create forms are inline panels dressed as dialogs.** They don't take focus on open, don't close on Escape, don't toggle their trigger button's label or `aria-expanded`, and are injected *below* the empty-state message they contradict.
8. **`document.title` is the constant "ITMS" on every route** (/login, /devices, /sla, /docs) — WCAG 2.4.2.
9. **No skip link; 36 tab stops from page top to main content** on every route.
10. **Mixed date formatting.** /sla renders `8/19/2026, 6:51:22 AM` — US M/D/YYYY, 12-hour, seconds precision — on a module whose default timezone is Europe/London.
11. **Selection state is not reflected in the URL.** Choosing an article on /docs leaves the path at `/docs`, so records and articles cannot be linked, bookmarked, or reached with Back.
12. **Raw JSON is exposed as a primary input control.** /docs asks users to hand-author a record's `Fields (JSON)` in a plain textarea seeded with `{}` — no schema, no validation, no formatting, no examples.

## Coverage log

| Route | Status | What I exercised | Not tested / why |
|---|---|---|---|
| **/sla** | **Complete** | Read DOM + text; opened Add policy and Add calendar inline forms; empty submit ×2; invalid submit (`-50`, letters, blank numeric); valid submit (created a calendar and a policy); calendar detail drill-in; "Add 24-hour day" + "Save week" (success verified via health badge `no working windows` → `ok`); "Add holiday" empty submit; Delete on the policy and on the calendar (both executed, no confirm); queue/priority/calendar/escalation dropdown enumeration; keyboard Tab focus check; contrast audit; label audit; heading audit; console (clean) and network capture | "Revert" button not clicked; the Edit flow on an existing policy was not opened (Delete was tested instead); /sla "Save settings" was inspected but not submitted |
| **/docs** | **Complete** | Read DOM + text; opened an existing article; inspected rendered markdown HTML and computed styles; opened Edit and read raw source; live-preview test with headings/bold/code/GFM table; Cancel; New article empty submit; New article with 500-char title (raw JSON error captured); Escape-to-close test; initial-focus test; search with a matching and a non-matching query; New record form — enumerated Type options, invalid-JSON submit, default-type submit (blocking error captured) | Article "History" and "Archive" buttons not opened — the session dropped before I reached them; record detail view not opened; no test record was successfully created (blocked by the `network` default, then by the session loss) |
| /changes, /bcp, /reports, /audit, /licenses, /subscriptions, /budget, /ipam, /jobs, /enrollment, /assistant/autonomy, /platform-status, /settings, /error-monitor | **Not started** | — | Blocked: browser tab frozen (see below) |

### Responsive testing
Not yet performed. Window was set to 1440x900 for all testing above. The 1280/768/390 passes were scheduled after the first four modules and were not reached before the tab froze.

### Testing I deliberately did not perform
- Did **not** enter any credentials. Sign-in used an existing SSO session — clicking "Sign in with SSO" landed straight on /devices with no password prompt.
- Per the brief, /settings and /enrollment were to be inspected read-only with no saves touching authentication, certificates, signing keys or connectivity. Neither route was reached.

### Environment note
The /docs module already contained records named `AuditB Runbook <b>x</b>` and `AuditB Net`, and the runbook's markdown source contained deliberate XSS probes (`<img src=x onerror=alert(1)>`, `[link](javascript:alert(2))`). These are pre-existing artefacts of an earlier audit run, not data I created. I created and then deleted: one business calendar (`<b>xss-b</b> AuditB Calendar`) and one SLA policy (Any queue / Any priority / 30m / 4h). Neither remains.

### Blocking issue — browser tab frozen
Tab **1442057191** is in `document.visibilityState === "hidden"` and has been frozen by Chrome. Confirmed by three independent symptoms:
- `setTimeout(…, 300)` set in one call had still not fired when read back in the next call (`window.__t2 === "pending"`), while `Date.now()` continued to advance — timers are suspended.
- `fetch()` from the page never settles (two probes, both still `pending` after minutes), so React data-loading and the SSO sign-in handler cannot complete. The tab was signed out and cannot sign back in.
- `computer` (screenshot/click-with-ref), `find`, and `read_page` all return "Tool failed in the extension"; only `javascript_tool`, `navigate`, `resize_window` and blind coordinate clicks still respond.

Retried repeatedly over several minutes including `navigate`, `F5`, `resize_window` and blind clicks on the SSO button — no recovery. This is an environment/browser-harness fault, **not** a defect in ITMS.

---
# RESUMED RUN (new tab 1442057179)
Sections above were written before the browser harness froze. Findings below continue from /changes.
Coordinator asked me to corroborate or contradict Auditor A's systemic finding: "no toast/snackbar system anywhere; `[role=alert]`/`[role=status]` returns 0 on every page, so writes succeed or fail with no user feedback." I probe for this explicitly on every route below.

## /changes — Change Management

### [BLOCKER] A change request can be created but can never leave `draft` — there is no approval, submit, schedule or close control anywhere in the UI
- **Page:** /changes/:id (change detail)
- **What happens:** I created a change and opened its detail page. It shows state `draft` and an "Approval history" section whose table reads "No approval decisions yet." An exhaustive enumeration of every interactive element on the page returns exactly four controls: `BUTTON "Export evidence"`, a `SELECT` of action types, a text `INPUT` for an action id, and `BUTTON "Link"`. There is **no** Submit for approval, Approve, Reject, Schedule, Implement, Close or Cancel control, and no state dropdown. The list page's only button is "New change".
- **Why it's a problem:** The module's stated purpose is "Change requests, **approvals**, and scheduled windows". The approval workflow — the entire reason a change-management module exists, and the thing an ISO 27001 / SOC 2 auditor asks for — cannot be exercised. The "Awaiting approval" KPI on the list page can therefore never be anything but 0, and the Approval history table can never be populated. Records accumulate in `draft` indefinitely.
- **Fix:** Add the state-transition actions to the detail header, gated by role: Submit for approval → Approve/Reject (with comment) → Schedule → Implement → Close. Reflect the allowed transitions from current state so the user sees what's next.
- **Evidence:** `[...main.querySelectorAll('button,a,select,input,textarea')]` on /changes/06a85c19-… returned only `["BUTTON|Export evidence","SELECT|opts=patch_run,deployment,provision_run,patch_approval","INPUT","BUTTON|Link"]`.

### [HIGH] An inverted maintenance window (end before start) is accepted and displayed as-is
- **Page:** /changes (New change → Window start / Window end)
- **What happens:** I deliberately set Window start = `2026-08-25T09:00` and Window end = `2026-08-20T09:00` — an end five days *before* the start. The record saved with no complaint and the list renders the window as `8/25/2026, 4:00:00 AM – 8/20/2026, 4:00:00 AM`. The detail page repeats it verbatim.
- **Why it's a problem:** Nielsen #5. A negative-duration maintenance window is meaningless; anything downstream computing "is this change in-window right now?" or "how long is the outage?" will produce a negative or empty result. Neither field is marked `required` either (`input.required === false` on all seven fields), so a change can also be created with no window at all.
- **Fix:** Validate `end > start` on blur and on submit with an inline message on the end field; set `min` on the end input to the current start value. Mark Title and the window fields required.
- **Evidence:** list row `WINDOW` cell reads `8/25/2026, 4:00:00 AM – 8/20/2026, 4:00:00 AM`.

### [HIGH] Times are shifted by 5 hours between entry and display — what you type is not what you see
- **Page:** /changes (Window start/end round-trip)
- **What happens:** I typed `09:00` into both `datetime-local` inputs. Both render back as `4:00:00 AM` in the list and on the detail page — a **-5 hour** shift. The `datetime-local` input carries no timezone, so the value is evidently being treated as one zone on write and rendered in another on read. Nothing in the UI states which timezone either the input or the output is in.
- **Why it's a problem:** For a maintenance-window field this is severe: an engineer schedules a 09:00 outage and the record says 04:00. Change windows are coordinated across teams and vendors from exactly this text. Compounded by /sla defaulting business calendars to Europe/London while dates elsewhere render in US format — there is no single, stated timezone anywhere in the product.
- **Fix:** Label both fields with the timezone in use ("Window start (UTC)" or the org's configured zone), store ISO-8601 with an explicit offset, and render with the same zone plus a `title` tooltip showing UTC. Never round-trip a naive local datetime through a differently-zoned renderer.
- **Evidence:** entered `2026-08-25T09:00`, displayed `8/25/2026, 4:00:00 AM`; entered `2026-08-20T09:00`, displayed `8/20/2026, 4:00:00 AM`.

### [HIGH] "Link action" requires the user to know and hand-type a raw UUID
- **Page:** /changes/:id (Linked actions → Action type + Action ID + Link)
- **What happens:** To link a change to the patch run or deployment that carried it out, the user picks a type from a dropdown (`patch_run`, `deployment`, `provision_run`, `patch_approval`) and then types the **Action ID** into a bare text input. There is no picker, no search, no recent-actions list. Clicking "Link" with the field empty does nothing at all — no error, table still reads "No linked actions."
- **Why it's a problem:** Nielsen #6 (recognition rather than recall) taken to its extreme: the ID format is a 36-character UUID like `06a85c19-55e2-7f3c-8000-831c7bdba357`, which the user must find in another module and transcribe. This is the evidence trail that makes the change record auditable, so friction here means it simply won't be filled in.
- **Fix:** Replace the ID input with a typeahead that searches the selected action type by name/date and stores the id behind the scenes; offer "recent actions in this change's window" as default suggestions.
- **Evidence:** field enumeration shows a plain `INPUT` with no `list`/`datalist`; empty "Link" click produced `live: 0`, no error text, `No linked actions.` unchanged.

### [MEDIUM] Seven-field create form has zero required fields and no field-level guidance
- **Page:** /changes (New change)
- **What happens:** All seven controls (Type, Title, Description, Risk, Window start, Window end, Rollback plan) report `required === false` and none has a placeholder or hint. Clicking Save on the completely empty form is a silent no-op — the form stays open, `[role=alert]` count is 0, "Open changes" stays at 0.
- **Why it's a problem:** The user cannot tell which fields matter before submitting, and submitting teaches them nothing. Same silent-no-op class documented on /sla and /docs.
- **Fix:** Mark Title and the window fields required with visible indicators; validate on submit with inline messages.
- **Evidence:** `input.required` false on all 7; empty Save → no error, no state change.

### [MEDIUM] Change detail page exposes a raw UUID in the URL and offers no in-page link back to the list
- **Page:** /changes/:id
- **What happens:** The detail URL is `/changes/06a85c19-55e2-7f3c-8000-831c7bdba357`. Within `<main>` there is no "back to changes" affordance; the only route back is the global header breadcrumb ("Change Management → /changes"), which sits outside the content area.
- **Why it's a problem:** Minor navigational friction, and the opaque UUID makes URLs unreadable and unshareable in conversation. (Positive: the row *is* a real `<a href>`, so it is linkable and middle-clickable — better than /docs.)
- **Fix:** Add a back link at the top of the content area; consider a human-readable change number (CHG-1042) in the URL and as the record's primary identifier.
- **Evidence:** `location.pathname === "/changes/06a85c19-55e2-7f3c-8000-831c7bdba357"`; breadcrumb link found only in `header`.

### [LOW] Change detail H1 is the user-supplied title, so the page has no stable identity
- **Page:** /changes/:id
- **What happens:** The `<h1>` is `AuditB Test Change <b>x</b>` — the raw title (correctly escaped, no HTML injection). There is no change number or type prefix.
- **Why it's a problem:** Combined with the constant `document.title` of "ITMS", a user with several change tabs open cannot tell them apart.
- **Fix:** Render `CHG-1042 · AuditB Test Change` and set `document.title` to match.
- **Evidence:** `h1` text captured above; `document.title === "ITMS"`.

**Corroborates Auditor A:** `[role=alert] / [role=status] / [aria-live]` count was **0** on the /changes list, on the open create form, after a successful create, and on the detail page after a failed Link. A change request was created successfully with no confirmation of any kind.

**Not tested (policy):** "Export evidence" on the change detail page was not clicked — it initiates a file download, which I don't perform without explicit user approval.

## /jobs — Signed Job Engine

### [BLOCKER] Jobs cannot be cancelled, retried or requeued — the detail panel has no actions at all
- **Page:** /jobs (job detail panel)
- **What happens:** Opening any job renders a panel containing only: a monospace UUID, a status pill, a "Result" block and a "Logs" block. Enumerating buttons on the page returns **only the five "Detail" buttons from the list** — there is no Cancel, Retry, Requeue, Re-sign, or Delete control anywhere on the list or the detail panel.
- **Why it's a problem:** Four of the five jobs are stuck in `queued` (see next finding). With no cancel and no retry, an operator who dispatches a bad job to their fleet has no way to stop it from the console, and no way to re-drive a job that failed. For a module whose whole purpose is dispatching instructions to endpoints, the absence of a kill switch is a serious operational gap.
- **Fix:** Add Cancel (for `queued`/`running`) and Retry (for `failed`/`succeeded`) to the detail panel, with a confirmation on Cancel. Surface a bulk-cancel on the list for queue drain.
- **Evidence:** `[...main.querySelectorAll('button')].map(…)` → `["Detail","Detail","Detail","Detail","Detail"]` with a job detail open; detail panel `innerHTML` contains no `<button>` at all.

### [HIGH] Four of five jobs have sat `queued` with 0 attempts for over 24 hours and nothing flags it
- **Page:** /jobs (list + KPI tiles)
- **What happens:** The list shows `patch.scan` queued since `8/18/2026, 11:04:18 AM` and `8/18/2026, 11:04:14 AM` — over 24 hours before the current date of 19 Aug 2026 — both with **Attempts 0**. Two more are queued from earlier today. The KPI tiles read `Jobs 5`, `Running 0`, `Failed 0`, i.e. the dashboard presents an entirely healthy picture.
- **Why it's a problem:** A queue that has dispatched nothing in 24 hours is the definition of an incident, and the UI's headline metrics actively conceal it — "Failed 0" reads as success. Nielsen #1. There is no "oldest queued job" metric, no age column, no stale-job warning.
- **Fix:** Add an "Age"/"Waiting" column, and a KPI for "Queued > 15 min" that turns red. When `Running = 0` and queued jobs are older than a threshold, show a banner: "No jobs have been dispatched in 24h — the dispatcher may be down."
- **Evidence:** list rows captured verbatim: `patch.scan | queued | 0 | 8/18/2026, 11:04:18 AM` and `… 11:04:14 AM`; tiles `Jobs 5 / Running 0 / Failed 0`.

### [HIGH] The "Signed job engine" never shows any signature information
- **Page:** /jobs (list and detail)
- **What happens:** The page description states "Every instruction the platform sends an endpoint is signed here and verified there." The list columns are Type, Status, Attempts, Issued. The detail panel shows id, status, Result, Logs. There is **no** signature, key id, algorithm, signed-payload, or verification-status field anywhere.
- **Why it's a problem:** The module's headline value proposition — cryptographic signing and verification — is invisible, so an operator cannot confirm a job was signed, which key signed it, or whether the endpoint verified it. That is exactly the evidence a security reviewer would come to this page for.
- **Fix:** Add a "Signature" section to the detail panel: key id, algorithm, signed-at, and a verification result per attempt. Add a verification-status column or icon to the list.
- **Evidence:** full detail-panel `innerHTML` captured — three blocks only (id+status, Result, Logs), no signature markup.

### [HIGH] Job detail shows a raw JSON `{}` as the Result and identifies the job only by UUID
- **Page:** /jobs (job detail panel)
- **What happens:** The panel's heading area is `<span class="font-mono text-xs">06a85b5d-74a0-713e-8000-506439b07adb</span>` — the job's UUID and nothing else. The Result block renders `<pre class="code">{}</pre>`, i.e. a literal empty JSON object. The job **type** (`patch.scan`), its target device, its payload and its issued time are all present in the list row but none is repeated in the detail panel.
- **Why it's a problem:** Two problems. (1) Once the panel opens, the user has lost the only human-readable identity the job had; they must cross-reference the row they clicked. (2) `{}` is developer output, not a user-facing empty state — it reads as "something is broken" rather than "this job hasn't produced a result yet". Same raw-JSON-to-the-user pattern found on /docs.
- **Fix:** Title the panel with the job type and issued time, keep the UUID as a small copyable secondary line, and replace `{}` with "No result yet — this job hasn't run." Show the payload that was dispatched.
- **Evidence:** panel HTML quoted above; `pre.textContent === "{}"`.

### [MEDIUM] Selecting a job does not change the URL, and rows use buttons instead of links
- **Page:** /jobs
- **What happens:** Clicking "Detail" loads the job into the right-hand pane while `location.pathname` stays `/jobs`. The row action is a `<button>`, so it cannot be middle-clicked, opened in a new tab, bookmarked or pasted into an incident channel. There are zero `<a>` elements in `<main>`.
- **Why it's a problem:** Same non-linkable-detail defect as /docs — and here it is *inconsistent within the product*, because /changes uses real `<a href="/changes/:id">` rows. A user learns one interaction model per page.
- **Fix:** Route to `/jobs/:id` and render rows as links, matching /changes.
- **Evidence:** `main a` count 0; `location.pathname` unchanged after opening a job; contrast with `/changes` row `<a href="/changes/06a85c19-…">`.

### [MEDIUM] Job list has no filter, search, sort, pagination or refresh control
- **Page:** /jobs
- **What happens:** The only interactive elements on the page are the per-row "Detail" buttons (`inputs: 0` — not a single input or select on the page). With five records this is survivable; a job engine for a fleet will hold thousands.
- **Why it's a problem:** No way to answer "show me failed jobs", "show me this device's jobs", or "what ran last night". No auto-refresh either, so a queue being worked cannot be watched.
- **Fix:** Add status/type filters, a device search, sortable columns, pagination, and a live-refresh toggle.
- **Evidence:** page-wide `input,select,textarea` count = 0; button list = five "Detail".

### [LOW] A junk job type named `hello` sits in the production queue
- **Page:** /jobs (list, row 2)
- **What happens:** Alongside `patch.scan` and `inventory.collect` there is a queued job of type **`hello`**, issued `8/19/2026, 6:55:39 AM`.
- **Why it's a problem:** Test/debug data in a live operational queue erodes trust in the list and pollutes any metrics built from job types. Flagging it because the UI gives no way to distinguish real job types from test ones — and no way to delete it.
- **Fix:** Remove it; more usefully, constrain job types to a registered enum so ad-hoc types can't be dispatched, and provide a delete action.
- **Evidence:** list row `hello | queued | 0 | 8/19/2026, 6:55:39 AM`.

**Corroborates Auditor A:** `[role=alert]/[role=status]/[aria-live]` = **0** on /jobs list and job detail.

## /reports — Reporting & Evidence

### [HIGH] Report cards in the catalogue have no titles — the link text is a 200-word paragraph
- **Page:** /reports (catalogue)
- **What happens:** The catalogue lists four reports. Enumerating headings on the page returns **only `H1: Reports`** — there is no `h2`/`h3` per report. Each card is a single `<a>` whose accessible name is the report's entire prose description, e.g. `"Every device with the backup policy that applies to it and how protected it is. One row per device: whether a backup policy applies and whether it is switched on, which repository it writes to, when the last backup point was taken and in what state…"`. The actual report names exist only in the URLs — `/reports/backup-coverage`, `/reports/fleet-posture`, `/reports/patch-compliance`, `/reports/sla-attainment`.
- **Why it's a problem:** WCAG 2.4.4 Link Purpose — a screen-reader user listing links hears four paragraph-length names and cannot distinguish them. Sighted users get no scannable index: to find "the patch one" they must read four essays. WCAG 1.3.1 is also weakened by the missing heading structure.
- **Fix:** Give each card an `<h3>` with the report's human name ("Patch compliance"), make that the link, and demote the description to supporting text with the first sentence shown and the rest behind "More detail".
- **Evidence:** `document.querySelectorAll('h1,h2,h3')` → `["H1:Reports"]`; the four `<a>` elements' text begins "Every device with the backup policy that…", "Every device with its agent contact stat…", "Every device with its outstanding approv…", "SLA attainment by queue and priority ove…".

### [HIGH] There is no data visualisation anywhere in the reporting module
- **Page:** /reports and /reports/sla-attainment
- **What happens:** I checked for chart primitives on both the catalogue and a fully-run report: `main svg` = 3 on the catalogue (all 17px KPI-tile icons) and **0** on the report page; `main canvas` = **0** on both. Output is a single HTML table plus a row of nine numeric tiles. There are no bars, lines, sparklines, axes, legends or trend indicators in the module described as "Evidence & Analytics".
- **Why it's a problem:** SLA attainment, patch compliance and backup coverage are all fundamentally comparative and time-series questions — "is attainment improving?", "which queue is worst?", "how much of the fleet is uncovered?". A flat table answers none of those without the reader doing the arithmetic. This is the one module where a chart is the deliverable.
- **Fix:** For SLA attainment, a grouped bar of met vs breached per queue with a target line, plus a trend line over the selected window. For backup/patch coverage, a stacked bar of covered/uncovered with the percentage called out. Label axes with units, put the metric name and time window in the chart title, and don't encode meaning in colour alone.
- **Evidence:** `svg: 0, canvas: 0, tables: 1` on `/reports/sla-attainment` after a successful run.

### [HIGH] A zero-row result still renders nine metric tiles all showing "0"
- **Page:** /reports/sla-attainment (after Run)
- **What happens:** Running the report for 1 Jul – 19 Aug 2026 returned `As of 8/19/2026, 10:02:58 AM — 0 matching rows.` Below that, nine tiles render: `QUEUE PRIORITY PAIRS 0`, `TICKETS 0`, `RESPONSE MET 0`, `RESPONSE BREACHED 0`, `RESOLUTION MET 0`, `RESOLUTION BREACHED 0`, `VOID 0`, `STILL RUNNING 0`, `BREACHED AFTER RETRIAGE 0`.
- **Why it's a problem:** Nine zeros are indistinguishable from nine genuine measurements of zero. "0 tickets breached" reads as a perfect score when in fact nothing was measured. This is a data-integrity risk in a module explicitly built as compliance evidence — someone will screenshot this as proof of 100% attainment.
- **Fix:** When the row count is 0, suppress the tiles and show one empty state: "No tickets resolved in this window — nothing to measure." If tiles must stay, render "—" rather than 0 and label them "no data".
- **Evidence:** result text captured verbatim above, all nine tiles reading 0 alongside "0 matching rows".

### [MEDIUM] Report parameters expose raw API concepts: a free-text "Queue Id" UUID, plus Limit and Offset
- **Page:** /reports/sla-attainment (parameter form)
- **What happens:** The six parameters are `From *` and `To *` (datetime-local), **`Queue Id`** as a plain text input, `Priority` (a proper select: Any/low/normal/high/urgent), and **`Limit`** and **`Offset`** as number inputs.
- **Why it's a problem:** Queue Id repeats the /changes "Action ID" defect — the user must find a UUID in another module and transcribe it, when a queue picker already exists elsewhere in the product (/sla has a populated queue dropdown). Limit/Offset are pagination internals leaking into a business-user form; nobody running an SLA report thinks in offsets.
- **Fix:** Replace Queue Id with the same queue `<select>` used on /sla. Replace Limit/Offset with ordinary pagination controls on the results table, or a "max rows" preset.
- **Evidence:** control enumeration → `["INPUT/datetime-local/From *","INPUT/datetime-local/To *","INPUT/text/Queue Id","SELECT/Priority…","INPUT/number/Limit","INPUT/number/Offset"]`.

### [MEDIUM] Validation message names raw API parameters instead of the visible field labels, and is not announced
- **Page:** /reports/sla-attainment (Run with empty dates)
- **What happens:** Clicking Run with From/To empty renders: **"This report needs a value for from, to before it can run."** The message uses the lowercase API parameter names `from, to` rather than the on-screen labels "From \*" and "To \*", is a comma-joined list rather than a sentence, sits at the bottom of the page away from the fields, and is **not** inside a live region (`[role=alert]/[role=status]/[aria-live]` = 0).
- **Why it's a problem:** WCAG 3.3.1 — the error is not programmatically associated with the offending fields and is never announced to assistive tech. Naming internal parameters is the same leak seen in /docs' raw JSON errors, just milder.
- **Fix:** Mark the two fields invalid with `aria-invalid` and inline messages, and put the summary in `role="alert"`.
- **Evidence:** message text captured verbatim; `live: 0` immediately after it appeared.

### [LOW] Unrendered markdown backticks leak into report descriptions
- **Page:** /reports (catalogue, fleet-posture description)
- **What happens:** One description ends "…it is a statement about the record at \`meta.generated_at\`." with literal backtick characters visible. The parallel sentence in the backup-coverage description renders the same phrase as plain "meta.generated_at" with no backticks — so the two are inconsistent.
- **Why it's a problem:** Raw markup visible to users; also a `meta.generated_at` field path is developer vocabulary in business-facing copy.
- **Fix:** Render these descriptions through the markdown pipeline (or strip the backticks) and rephrase to "as of the generation time shown on the report".
- **Evidence:** regex for `` `[a-z_.]+` `` over `main.innerText` → `["`meta.generated_at`"]`.

### [LOW] Descriptions use `--` where an em dash is intended
- **Page:** /reports (all four descriptions)
- **What happens:** Six occurrences of a spaced double-hyphen ` -- ` in the catalogue prose, e.g. "Devices with no agent are reported as stale and never_enrolled rather than omitted -- so the device total is the fleet". The same pattern appears in /sla's help text.
- **Why it's a problem:** Typographic polish; reads as unconverted plain-text source.
- **Fix:** Use `—` (em dash) consistently, or run the copy through the markdown renderer which does this conversion.
- **Evidence:** 6 matches for ` -- ` in `main.innerText` on /reports.

**Contradicts Auditor A (partially) — worth flagging:** /reports is the one place I have found *any* user-visible validation feedback. Clicking Run with required fields empty produced the message "This report needs a value for from, to before it can run." — a real, rendered error, unlike the silent no-ops on /sla, /docs and /changes. So the product is not uniformly silent; it has at least one working error-surfacing path. It is still **not** announced (no live region), which is why Auditor A's `[role=alert]` = 0 measurement holds everywhere including here. The accurate framing is: *rendered* errors exist in exactly one module; *announced* errors exist nowhere.

**Strength (recording here, rolled up later):** the provenance block on a run report is exceptional — it states "Not reproducible: a source of this report is mutable…", names each source module with its permission (`service_desk (ticket.read)`, `sla (sla.read)`), and explains *why* each is mutable ("a human re-triage recomputes a deadline from ticket start, so a ticket that read 'met' last month can read 'breached' today"). I have not seen a reporting tool disclose result instability this honestly.

## /budget — Budget (Dashboard / Planner / Expenses / Confirm queue / Admin)

### [HIGH] The "Budget dashboard" for plan-vs-actual-vs-committed spend contains no chart
- **Page:** /budget (Dashboard tab)
- **What happens:** The page describes itself as "Plan versus actual versus committed IT spend for one fiscal-year budget". Chart-primitive check: `main svg` = **2** (both KPI-tile icons), `main canvas` = **0**, `main table` = **0**. The entire view is the sentence above plus two empty-state lines.
- **Why it's a problem:** Plan vs actual vs committed is the canonical three-series comparison — it is a bar-with-target or waterfall, and reading it as numbers costs the user the whole point of a "dashboard". Combined with /reports having zero charts too, the product has **no data visualisation anywhere in its two analytics modules**.
- **Fix:** Render plan/actual/committed as a grouped or stacked bar per category with a variance callout, and a cumulative burn line against the fiscal calendar. Label the axis with the currency and unit; do not rely on colour alone to separate the three series.
- **Evidence:** `{svg: 2, canvas: 0, tables: 0}` on /budget with the Dashboard tab active.

### [HIGH] Fiscal year is an unbounded number input — it accepted `-3000`
- **Page:** /budget/planner (New budget → Fiscal year)
- **What happens:** The Fiscal year control is `INPUT[number]` pre-filled `2026` with **`min=""` and `max=""`** (both unset). Setting it to `-3000` was accepted by the field with no complaint and no visual error.
- **Why it's a problem:** Same class as the negative SLA target on /sla — numeric fields across the product carry no bounds. A fiscal year is a tightly-constrained value; an out-of-range one will produce nonsense periods in the planner and in every downstream variance calculation.
- **Fix:** Set `min="2000" max="2100"` (or current year ±10) and validate on blur. Better: replace with a select of plausible fiscal years.
- **Evidence:** `{"yearVal":"-3000","min":"","max":""}` read back from the input after setting.
- **Not fully verified:** I could not complete the Save round-trip for this value — the browser extension began failing before the submit landed. The field-level acceptance is confirmed; whether the server also accepts `-3000` is **untested**.

### [MEDIUM] Tab bar is built from plain buttons with no tab semantics
- **Page:** /budget (Dashboard | Planner | Expenses | Confirm queue | Admin)
- **What happens:** All five tabs are `<button class="btn">`. Checking ARIA: `role` = **null** on every tab, `aria-selected` = **null** on every tab, and the container's `role` = **null** (no `tablist`).
- **Why it's a problem:** WCAG 4.1.2 Name, Role, Value. A screen-reader user hears five unrelated buttons with no indication that they form a set, which one is active, or how many there are. Arrow-key navigation between tabs (the expected pattern) is unavailable.
- **Fix:** Apply `role="tablist"` to the container, `role="tab"` + `aria-selected` + `aria-controls` to each tab, `role="tabpanel"` to the panel, and implement arrow-key roving tabindex. Since the tabs already change the URL, rendering them as `<a>` would be better still.
- **Evidence:** `tabs.map(t => role + '/' + aria-selected + '/' + className)` → `["null/null/btn", ×5]`, `parentRole: null`.

### [MEDIUM] Two adjacent empty states on one dashboard, each phrased as a dead end
- **Page:** /budget (Dashboard tab)
- **What happens:** With no budget configured, the Dashboard renders "Year-to-date position → **No position data yet.**" and "Variance → **No variance data yet.**" — two separate, similarly-worded dead ends, neither of which mentions that the prerequisite is creating a budget in the adjacent Planner tab.
- **Why it's a problem:** Same first-run dead-end as /sla's calendar-less policy form. The user is told twice that there's nothing here and never told what to do about it. Notably this module's empty states are *weaker* than /sla's, which do explain consequences — so the treatment is inconsistent across the product.
- **Fix:** Collapse to one first-run state on the Dashboard: "No budget for FY2026 yet — create one in the Planner to see position and variance," with the action inline.
- **Evidence:** Dashboard text captured verbatim: `"Budget\nYear-to-date position\nNo position data yet.\nVariance\nNo variance data yet."`

### [MEDIUM] New budget form has no required fields
- **Page:** /budget/planner (New budget)
- **What happens:** All three controls report `required === false`: Name (text, empty), Fiscal year (number, `2026`), Granularity (select: month/quarter, defaulting to `month`).
- **Why it's a problem:** Consistent with /changes (7 of 7 not required) and /sla — the product never marks a required field, so users have no pre-submit signal and the silent-no-op failure mode gives no post-submit signal either.
- **Fix:** Mark Name required with a visible indicator and validate before submit.
- **Evidence:** `["INPUT/text/req=false/lbl=Name","INPUT/number/req=false/lbl=Fiscal year","SELECT/req=false/lbl=Granularity…"]`.

### [LOW] Tabs change the URL but are not links
- **Page:** /budget
- **What happens:** Clicking "Planner" navigates to `/budget/planner` — good, the state is addressable. But the control is a `<button>`, so it cannot be middle-clicked, opened in a new tab, or copied via right-click.
- **Why it's a problem:** Loses the benefit of the routing work that has already been done. Also inconsistent: /changes uses real anchors for rows, /jobs uses buttons, /budget uses buttons that route.
- **Fix:** Render routed tabs as `<a href>` with click-handled SPA navigation.
- **Evidence:** `location.pathname` changed from `/budget` to `/budget/planner` on click; element is `BUTTON`.

**Corroborates Auditor A:** `[role=alert]/[role=status]/[aria-live]` = **0** on /budget Dashboard, /budget/planner, and with the New budget form open.

**Coverage gap:** the Expenses, Confirm queue and Admin tabs were **not opened** — the browser extension began failing repeatedly at this point and I stopped per the run's hard rules. The Save round-trip on New budget was likewise not completed.

## /ipam — IP Address Management

### [HIGH] An impossible CIDR (`999.999.999.999/99`) is accepted by the field and Register is a silent no-op
- **Page:** /ipam (Register a subnet → Prefix)
- **What happens:** I entered `999.999.999.999/99` into the Prefix field (placeholder `10.4.7.0/24`) and clicked **Register subnet**. Nothing happened: no error text anywhere in `<main>` (regex for `Error|error|invalid|must` returned `[]`), no live region, and the list still reads "No subnets registered yet." The field itself showed no validation state — it is a plain `type="text"` with no `pattern` attribute.
- **Why it's a problem:** CIDR is a strictly-formatted value and this is the module's primary create action. The user gets no signal that the format is wrong, which format is expected, or that anything was rejected at all. Same silent-no-op class as /sla, /docs, /changes.
- **Fix:** Validate the prefix client-side on blur with an inline message naming the expected form ("Enter an IPv4 or IPv6 network in CIDR notation, e.g. 10.4.7.0/24"), add a `pattern` attribute, and surface server rejections in the same place.
- **Evidence:** post-click DOM read → `{live: 0, err: [], "No subnets registered yet": true}`.

### [MEDIUM] KPI tiles read 0/0/0/0 with no subnets registered, presenting an empty install as a clean one
- **Page:** /ipam (header tiles)
- **What happens:** With zero subnets the tiles read `Managed subnets 0`, `Assignable 0`, `With drift 0`, `Never swept 0`.
- **Why it's a problem:** Identical to the /reports nine-zeros defect — "With drift 0" and "Never swept 0" read as healthy findings when nothing has been measured. In a drift-detection module this is the most misleading possible zero.
- **Fix:** Render "—" for derived metrics when the population is empty, or replace the tile row with the first-run guidance until at least one subnet exists.
- **Evidence:** tile text captured verbatim with `No subnets registered yet.` below it.

### [LOW] Six tabs on /ipam are unlabelled as a tab set (same defect as /budget)
- **Page:** /ipam (Subnets | Drift | Search | Registry | Scan authorizations | Import / export)
- **What happens:** Same construction as /budget's tab bar — plain buttons, no `role="tablist"`/`role="tab"`/`aria-selected`.
- **Why it's a problem:** WCAG 4.1.2, repeated. Noting it here to establish it as a product-wide pattern rather than a /budget one-off.
- **Fix:** As per the /budget finding — one shared, correctly-roled Tabs component.
- **Evidence:** consistent with the /budget measurement; tab labels captured from `main.innerText`.

**Strength — best accessibility work in the audit:** every one of /ipam's five filter controls carries a real `aria-label` — `"Search subnets"`, `"Filter by VRF"`, `"Filter by site"`, `"Filter by VLAN"`, `"Filter by contained address"` — even though none has a visible `<label>`. Placeholders are genuine examples (`10.4.7.0/24`, `Contains address, e.g. 10.4.7.9`) rather than substitutes for labels. Programmatic check: **0 of 12** controls on the page lack an accessible name. This is the standard the rest of the product should be held to.

**Strength — honest scope statement:** the page header explicitly says "Ages here say how current the documentation is; nothing on this page is an up/down signal", pre-empting the most likely misreading of the data.

**Corroborates Auditor A:** `[role=alert]/[role=status]/[aria-live]` = **0** on /ipam including immediately after a rejected create.

## /audit — Immutable Audit

### [HIGH] The audit log identifies every actor and target by raw UUID only — you cannot tell who did what
- **Page:** /audit (event table, ACTOR and TARGET columns)
- **What happens:** Every row renders the actor as a type word plus a bare UUID, e.g. `user 06a8438d-a07f-7256-8000-c2331b4dd204`, and the target the same way: `change 06a85c19-55e2-7f3c-8000-831c7bdba357`, `device 06a843da-c3e1-7908-8000-e503456f8459`, `health_profile 06a85b23-…`, `sla_calendar 06a8598b-…`. No display name, no email, no hostname, no record title appears anywhere in the table. Neither column is a link.
- **Why it's a problem:** This is the module a compliance auditor or incident responder reads, and in its current form it answers "something happened to some object" rather than "who did what to which thing". Establishing that `06a8438d…` is Ops Admin requires querying another system. Nielsen #6 in the most consequential place in the product. With 24 distinct action types and 37 events it is already unusable at this size; at production volume it is noise.
- **Fix:** Resolve actor and target to display names server-side (`Ops Admin (ops@…)`, `SLA calendar "London Business Hours"`), keep the UUID as a `title` tooltip or a small copyable secondary line, and make the target a link to the record.
- **Evidence:** table rows captured verbatim, e.g. `8/19/2026, 9:45:41 AM | change.created | user 06a8438d-a07f-7256-8000-c2331b4dd204 | change 06a85c19-55e2-7f3c-8000-831c7bdba357 | d2e71d7d057bbdfb…`

### [MEDIUM] The "Chain integrity" tab does not visibly change the view
- **Page:** /audit (Event search | Chain integrity)
- **What happens:** Clicking "Chain integrity" left the page rendering the same single event table (`main table` count stayed at 1, the header block and the event rows were unchanged, `location.pathname` stayed `/audit`). The only integrity information available is the KPI tile reading "Chain integrity — Verified" and "Events in verified chain 37".
- **Why it's a problem:** A tab that appears to do nothing when clicked is indistinguishable from a broken control (Nielsen #1). If verification detail exists it isn't reachable; if it doesn't, the tab shouldn't be there.
- **Fix:** Either render a real integrity panel (last verification run, chain head hash, any breaks with the offending sequence number, a "Verify now" action) or remove the tab and keep the KPI.
- **Caveat:** I could not screenshot this page (the extension's capture was failing), so this is based on DOM comparison before and after the click rather than a visual diff. Worth a manual re-check.
- **Evidence:** `main.innerText.slice(0,300)` and `main table` count identical before and after the click.

### [MEDIUM] Chain hashes are truncated with no way to see or copy the full value
- **Page:** /audit (CHAIN HASH column)
- **What happens:** Hashes render as `d2e71d7d057bbdfb…`, `ca8721cfe54ea88c…`, `7654b57f2a6575f3…` — first 16 hex characters plus an ellipsis. There is no expand, no copy button, and the cell is not a link.
- **Why it's a problem:** The hash chain is the entire evidentiary value of an "immutable audit" module. A truncated hash cannot be independently verified or quoted in an audit response, so the column is decorative.
- **Fix:** Add a copy-to-clipboard affordance per row and expose the full hash on hover/expand; offer an export of the chain for offline verification.
- **Evidence:** hash cells captured verbatim showing 16 chars + `…`.

**Strengths:** the audit trail is genuinely working and complete — every mutation I performed during this audit appears in it with the correct action type and a chain hash: `change.created`, `sla.calendar_deleted`, `sla.policy_deleted`, `health.profile_created/deleted`, `patch.scan_dispatched`. Coverage across modules is real, the chain reports **Verified** with 37 of 37 events in the verified chain, and the action-type vocabulary (`module.verb_past_tense`) is consistent. This is the strongest-implemented module I audited.

---
## Run 2 termination

Navigating from /audit to **/licenses** redirected to **/login** (`h1: ITMS`, single "Sign in with SSO" button, zero inputs). Per the coordinator's standing instruction I stopped immediately and did **not** attempt to re-authenticate. This is consistent with the ~20-minute session self-termination the coordinator warned about; it was not triggered by any control I clicked (I never went near the profile chip or Sign out).

### Run 2 coverage
| Route | Status | What I exercised |
|---|---|---|
| /changes | **Complete** | list, KPIs, New change form (all 7 fields), empty submit, inverted-window submit, successful create, detail drill-in at `/changes/:id`, full interactive-element enumeration, Link-action empty submit |
| /jobs | **Complete** | list, KPIs, job detail panel, full button enumeration, detail-panel HTML inspection |
| /reports | **Complete** | catalogue, link/heading structure, drill-in to `/reports/sla-attainment`, parameter enumeration, empty Run (validation captured), successful Run with a 7-week window, result rendering |
| /budget | **Partial** | Dashboard tab, Planner tab, tab ARIA semantics, New budget form fields, Fiscal-year bounds probe. **Not opened:** Expenses, Confirm queue, Admin tabs; New budget Save round-trip |
| /ipam | **Mostly complete** | Subnets tab, filters + their accessible names, KPI tiles, Register-a-subnet form, invalid-CIDR submit. **Not opened:** Drift, Search, Registry, Scan authorizations, Import/export tabs |
| /audit | **Complete** | event table, columns, KPI tiles, Chain integrity tab click + DOM diff |
| /licenses, /subscriptions, /error-monitor, /platform-status, /enrollment, /settings, /assistant/autonomy, /bcp | **Not reached** | Session expired at the /licenses navigation |

### Verdict on Auditor A's systemic finding
**Corroborated, with one refinement.** `[role=alert] / [role=status] / [aria-live]` measured **0** on every page and every state I checked in run 2: /changes (list, open form, after a *successful* create, detail page, after a failed Link), /jobs (list + detail), /reports (catalogue, parameter form, after a rendered validation error, after a successful run), /budget (Dashboard, Planner, open form), /ipam (list, after a rejected create), /audit. A change request was created successfully with no confirmation of any kind.

The refinement: the product is **not uniformly silent**. /reports renders a real validation message ("This report needs a value for from, to before it can run.") and /docs renders server errors (as raw JSON). So *rendered* feedback exists in two modules. What is universal is that **no feedback is ever announced** — there is no live region anywhere, so every one of these messages is invisible to assistive technology, and the majority of failures render nothing at all. The accurate one-line framing is: *the product has no notification system, and its three ad-hoc error paths are inconsistent with each other and inaccessible to all of them.*

### Additional cross-cutting patterns confirmed in run 2
13. **No required-field marking anywhere.** /changes 7 of 7, /budget 3 of 3, /sla and /ipam likewise — `required` is `false` on every control I inspected across five modules.
14. **Numeric inputs are unbounded across the product.** `-50` accepted for an SLA target (/sla); `-3000` accepted for a fiscal year (/budget, `min`/`max` both empty). No numeric field I tested carried a range.
15. **Raw UUIDs are the product's default identifier in user-facing surfaces.** Audit actor/target columns, job detail heading, change detail URL, and two hand-typed-UUID inputs (/changes "Action ID", /reports "Queue Id"). Names are almost never resolved for display.
16. **Zero data visualisation product-wide.** `canvas` = 0 and no chart `svg` on /reports, on a fully-run report, and on the Budget dashboard — the two modules explicitly branded "Evidence & Analytics" and "Budget dashboard".
17. **Derived metrics render 0 instead of "no data" when the population is empty** — nine zero tiles on a 0-row SLA report, four on an empty IPAM install, three on an empty /changes. Empty consistently masquerades as healthy.
18. **Detail-view addressability is inconsistent per module.** /changes uses real `<a href="/changes/:id">`; /budget tabs route but are buttons; /jobs and /docs don't change the URL at all.

---
# RUN 3

## /licenses — Licenses

### [HIGH] "New license" cannot be completed on a fresh install: both required lookup dropdowns are empty
- **Page:** /licenses → New license modal (Product, Vendor)
- **What happens:** The Product `<select>` contains exactly **1 option** — the placeholder "Select a product…". The Vendor select contains exactly **1 option** — "None". Only the Type select is populated (`perpetual | volume | oem | cal | per_device | per_user`). With no product selectable, Save is a silent no-op and no license can ever be created. The tab bar offers a "Products" and a "Vendors" tab that would seed these lists, but the modal neither says so nor links to them.
- **Why it's a problem:** Exact repeat of the /sla "Add policy has no calendar" blocker — a create form whose mandatory lookup is empty on a new install, with no explanation and no inline escape hatch. This is now a **product-wide first-run pattern**, not a one-module bug.
- **Fix:** Disable Save while Product is unset and show why; when the product list is empty replace the select with "No products yet — add one in the Products tab" as an inline link. Best: allow creating a product from within the modal.
- **Evidence:** `select.options` → `["Select a product…"]` (1), `["None"]` (1), and the 6-option Type list; `main.innerText` still reads "No licenses yet." after Save.

### [HIGH] Empty submit in the license modal is a silent no-op
- **Page:** /licenses → New license → Save
- **What happens:** Clicking Save with every field at its default did nothing: the modal stayed open, no error rendered, `[role=alert]/[role=status]/[aria-live]` = **0**, and the list still reads "No licenses yet."
- **Why it's a problem:** Same silent-failure class as /sla, /docs, /changes, /ipam — now confirmed in a sixth module. Not one of the six fields is marked `required`.
- **Fix:** Per-field inline validation on submit, surfaced in a live region.
- **Evidence:** post-click read `{dialogStillOpen: true, live: 0, listStill: true}`.

### [MEDIUM] The license modal has correct dialog semantics but no Escape, no initial focus, no scroll lock and no close button
- **Page:** /licenses → New license modal
- **What happens:** The modal is **correctly formed**: `<section role="dialog" aria-modal="true" aria-label="New license">`. But: pressing **Escape** leaves it open (`!!document.querySelector('[role=dialog]')` still true); `document.activeElement` remains **BODY** after opening, so focus is never moved in; `getComputedStyle(document.body).overflow` is `auto`, so the page behind still scrolls; and the only controls are Cancel and Save — there is **no labelled close (×) button**.
- **Why it's a problem:** The hard part (roles and accessible name) is done and the cheap parts are missing. Escape-to-dismiss is a universal expectation; without initial focus a keyboard user must tab blindly from the top of the document into the dialog.
- **Fix:** On open, focus the first control and store the trigger; bind Escape and a labelled × to the same close handler; set `overflow: hidden` on the body while open.
- **Evidence:** attributes and measurements quoted above.

### [MEDIUM] Entitlements is an unbounded number field
- **Page:** /licenses → New license → Entitlements
- **What happens:** `INPUT[number]` with `min=""` and `max=""` — no bounds, consistent with the negative SLA target (/sla) and the `-3000` fiscal year (/budget).
- **Why it's a problem:** A negative or zero entitlement count would corrupt the compliance calculation that is this module's whole purpose ("reconciles purchased entitlements against installed software").
- **Fix:** `min="1"`, integer step, validate on blur.
- **Evidence:** `{entMin: "", entMax: ""}`.

### [LOW] License key is masked as a password field with no reveal toggle
- **Page:** /licenses → New license → "Key (optional)"
- **What happens:** The key field is `input[type="password"]`, so characters render as dots. There is no show/hide toggle.
- **Why it's a problem:** License keys are long alphanumeric strings that are usually pasted and then visually verified; masking without a reveal makes transcription errors undetectable. Masking is also arguably the wrong default — a license key is not a credential in the same sense as a password.
- **Fix:** Add a reveal toggle, or use a plain text field given this is an entitlement record rather than a secret.
- **Evidence:** `input[type=password]` present in the dialog.

**Cross-check answered — CONTRADICTS Auditor A's UUID pattern (positive):** /licenses does **not** ask users to hand-type identifiers. Product and Vendor are proper `<select>` lookups and Type is an enumerated select. The problem here is that the selects are *empty*, not that they're free text. So the raw-UUID-input defect A found on /deploy and /service-desk is **not universal** — /licenses shows the product already has the correct lookup pattern implemented and it should be back-ported to those modules.

**Cross-check answered — modal quality:** /licenses matches the /deploy dialog A praised (`role="dialog"`, `aria-modal="true"`, `aria-label`). It is slightly *worse* in that it has no labelled close button. **Recommendation for standardisation: take /deploy's dialog markup (it has the labelled close) and add Escape + initial focus + scroll lock, then apply it everywhere** — two modules are already 70% of the way there, so this is a cheap consolidation.

**Corroborates Auditor A:** live-region count **0** on the /licenses list and inside the open modal.

**Focus-trap testing:** not attempted — recorded as untestable per A's finding that the automation window lacks OS focus and `document.activeElement` stays BODY.

## /subscriptions — Subscriptions

### [BLOCKER] Raw Pydantic JSON errors confirmed in a THIRD module — this is a product-wide error-handling failure, not a /docs bug
- **Page:** /subscriptions → New subscription → Save
- **What happens:** Submitting `Cost per cycle = "not-a-number!!"` and `Currency = "ZZZZZ"` renders this literal string inside the modal:
  `Error: {"detail":[{"type":"decimal_parsing","loc":["body","cost_per_cycle"],"msg":"Input should be a valid decimal","input":"not-a-number!!"},{"type":"string_too_long","loc":["body","currency"],"msg":"String should have at most 3 characters","input":"ZZZZZ","ctx":{"max_length":3}}]}`
  Two distinct field errors are concatenated into one unreadable blob; neither is attached to its field.
- **Why it's a problem:** I previously logged this on /docs (twice). Finding the identical `JSON.stringify(serverError)` output in a second, unrelated module confirms it is the **default error path for the whole application**, so every module with server-side validation will do this. It exposes the backend framework, internal field names (`cost_per_cycle`), and validation internals (`ctx.max_length`). WCAG 3.3.1/3.3.3 fail; Nielsen #9 fail. Raising to BLOCKER on the strength of the pattern.
- **Fix:** One shared error adapter that maps `detail[].loc` to the corresponding form field and renders `detail[].msg` inline beneath it, in a live region. This single fix would resolve the /docs, /subscriptions and any other module's error rendering at once — the highest-leverage change available in this audit.
- **Evidence:** error string captured verbatim above from `main.innerText`.

### [HIGH] The two headline KPIs cannot be populated by the only create form
- **Page:** /subscriptions (KPI tiles vs New subscription modal)
- **What happens:** The page's tiles are `Active subscriptions`, **`Renewals in 90 days`** and **`Notice deadlines due`**, and the page description promises "billing cycles, seats, owners, and cancellation-notice deadlines". The New subscription modal contains exactly five fields: Service name, Billing cycle (monthly/quarterly/annual), Cost per cycle, Currency, Seats. There is **no renewal date, no term end, no notice period and no owner field**.
- **Why it's a problem:** Two of three headline metrics — and the module's stated differentiator, cancellation-notice deadlines — depend on data the UI provides no way to enter. As with the /changes approval gap and the /jobs cancel gap, the promised capability has no control behind it. Auto-renewing SaaS contracts lapsing unnoticed is precisely the business risk this module exists to prevent.
- **Fix:** Add Term start / Term end (or Next renewal) and Notice period (days) to the create form, plus an Owner lookup; derive both KPIs from them.
- **Evidence:** modal field enumeration → `["text/Service name","select/Billing cycle","text/Cost per cycle","text/Currency","number/Seats"]`; tiles read `Active subscriptions 0 / Renewals in 90 days 0 / Notice deadlines due 0`.
- **Caveat:** the page also has "Spend" and "Renewals" sub-views which I did not open (time). It is possible renewal dates are editable there; the defect that the *create* form omits them stands regardless.

### [HIGH] Money and currency are unvalidated free-text fields
- **Page:** /subscriptions → New subscription (Cost per cycle, Currency)
- **What happens:** Both are `input[type="text"]` with no `pattern`, no `inputmode`, no `maxlength`. "Cost per cycle" accepted `not-a-number!!`; "Currency" accepted `ZZZZZ`. Both were rejected only by the server, which revealed that Currency is capped at 3 characters — i.e. it is an ISO-4217 code, a closed 180-value set, implemented as free text.
- **Why it's a problem:** A currency code typed as "USD " or "usd" or "Dollars" will either fail opaquely or store dirty data that breaks any spend aggregation across subscriptions. Cost as free text invites "1,200.00" and "£1200" which won't parse.
- **Fix:** Currency → `<select>` of ISO-4217 codes defaulted to the org currency. Cost → `type="number"` with `step="0.01"`, `min="0"`, `inputmode="decimal"`, and the currency symbol shown as a field prefix.
- **Evidence:** field types captured; server responses `decimal_parsing` on cost and `string_too_long {"max_length":3}` on currency.

### [MEDIUM] Same modal pattern, same four gaps
- **Page:** /subscriptions → New subscription
- **What happens:** `role="dialog"`, `aria-modal="true"`, `aria-label="New subscription"` — correct semantics, identical to /licenses. And identically: no Escape handling, no initial focus, no scroll lock, no labelled close button. None of the five fields is `required`.
- **Why it's a problem:** Confirms the dialog implementation is a shared component with a consistent set of omissions — which is good news, because one fix to that component repairs every modal in the product.
- **Fix:** As per /licenses.
- **Evidence:** dialog attributes captured; `required === false` on all five fields.

**Cross-check answered — timezone shift:** **could not be tested here.** The subscription create form contains **no date or datetime control at all**, so there is nothing to round-trip. The −5h shift I found on /changes therefore remains confirmed in one module only. I attempt the third data point on /bcp next.

**Cross-check answered — hand-typed UUIDs:** none on /subscriptions. Like /licenses, this module uses selects and plain names, no identifier inputs.

**Corroborates Auditor A:** live-region count **0** on the list, inside the open modal, and — notably — **at the moment the raw JSON error rendered inside the modal**. So even the product's most prominent error is unannounced.

## /error-monitor — Error Monitor

### [HIGH] "Resolve" is a one-click, unconfirmed, irreversible state change — there is no way to reopen a group
- **Page:** /error-monitor (Error groups → ACTIONS → Resolve)
- **What happens:** Clicking **Resolve** on the `IntegrityError` group immediately flipped the "Open" KPI from **1 → 0**. No confirmation dialog appeared (`[role=dialog]` = 0), no toast, no undo. Afterwards the row's action cell is **empty** — enumerating buttons in `<main>` returns `[]`. There is no Reopen, Unresolve or Undo control anywhere on the page.
- **Why it's a problem:** Nielsen #3 (user control and freedom). A misclick permanently dismisses a production error group from the "Open" count, and the operator cannot put it back. Because resolution is the only triage state this module has, an accidentally-resolved recurring error silently disappears from the operator's attention.
- **Fix:** Make the action reversible — render "Resolved ✓ · Reopen" in the actions cell after resolving, and show an undoable confirmation. At minimum, confirm before resolving a group whose `LAST SEEN` is recent.
- **Evidence:** Open KPI `1` before, `0` after; `main` button list `[]` after resolving; no dialog at any point.

### [MEDIUM] No filtering, search, sorting or time range on the error list
- **Page:** /error-monitor
- **What happens:** The page has zero inputs. There is no filter by `WHERE` (api/worker/gateway), no open-vs-resolved toggle, no search on raise site, no date range, and — now that the only group is resolved — **no way to view resolved groups at all**, since the list appears to show open groups and offers no state filter.
- **Why it's a problem:** Once anything is resolved it becomes unreachable, compounding the irreversibility above. At production volume, triaging by service or by frequency is the core workflow and none of it is supported.
- **Fix:** Add state (Open / Resolved / All), `WHERE`, and time-range filters, plus sort by Events and Last seen.
- **Evidence:** page-wide `input,select,textarea` count 0; no state filter present.

### [MEDIUM] Correlation columns are the module's stated purpose but every value is empty
- **Page:** /error-monitor (Recent events table)
- **What happens:** Selecting a group reveals "Recent events" with columns `OCCURRED | REQUEST ID | ORG | USER | JOB | DEVICE`. The single captured event has a real request id (`73a47f0a0e5c40e2a8924e364abf1404`) but **ORG, USER, JOB and DEVICE all render `—`**. The page description promises "Correlate a group's request, org, user, job, and device ids against the logs to investigate."
- **Why it's a problem:** Four of the five correlation dimensions the module advertises carry no data for the only error it has captured, so the investigation workflow can't actually be exercised. It's unclear from the UI whether this means "not applicable to this error" or "capture is broken" — and that ambiguity is the real defect.
- **Fix:** Distinguish "not applicable for this error type" from "not captured" in the cell treatment, and document which errors carry which correlation ids.
- **Evidence:** event row `8/18/2026, 5:50:05 AM | 73a47f0a0e5c40e2a8924e364abf1404 | — | — | — | —`.

### [LOW] Raise site exposes internal library paths and line numbers
- **Page:** /error-monitor (RAISE SITE column)
- **What happens:** The group is identified as `sqlalchemy.dialects.postgresql.asyncpg:797` / `AsyncAdapt_asyncpg_connection._handle_exception`.
- **Why it's a problem:** Fingerprinting on a third-party library's internal frame groups *all* asyncpg connection failures into one bucket regardless of what the application was doing — so the group is unlikely to be actionable. (Showing internals is defensible here: this is an operator-facing monitor for a self-hosted platform.)
- **Fix:** Fingerprint on the deepest *application* frame with the library frame as supporting detail, so groups map to application behaviour.
- **Evidence:** RAISE SITE cell text captured verbatim.

**Strengths — two genuinely good decisions here:**
1. **Deliberate privacy design, clearly stated.** "Fingerprinted by raise site — **never exception text**" and "No exception text is captured or shown — only type, location, counts, and resolve state." Exception messages routinely contain credentials and PII; excluding them by design, and telling the user so on the page, is the correct call and is explained rather than silently imposed.
2. **This is the one module that renders missing data as `—` instead of `0`.** The ORG/USER/JOB/DEVICE cells use an em dash for "no value". Everywhere else in the product (nine zero tiles on a 0-row /reports run, four on an empty /ipam, three on /changes) absent data is rendered as a literal `0` that reads as a real measurement. **/error-monitor already implements the correct pattern — it should be the house style.**

**Corroborates Auditor A:** live-region count **0** on load, after row selection, and — a new *silent success* instance — after the Resolve mutation succeeded and changed the Open count.

## /platform-status — Platform Status

### [HIGH] The platform is genuinely DOWN and the only global indicator is a 51×23px, 9px-type pill in the far corner
- **Page:** /platform-status (global status pill + Dependencies table)
- **What happens:** The `objectstore` dependency probe reports **down / unreachable / 0 ms**, and "Probes not OK" = 1. The page's overall verdict is a pill reading "down" measuring **51 × 23 px at 9px font**, positioned at **x≈1221, y≈149** — the far top-right of the content area, visually detached from both the page title and the KPI row, and rendered in the same muted pill style as every "ok" badge in the table below.
- **Why it's a problem:** A whole-platform outage verdict is the highest-priority information this page can convey and it is given the least visual weight on the screen — smaller than the body text, in the corner, at the same size as eight passing badges. Nielsen #1. An operator scanning the page will read "Dependency probes 8 / Registered modules 25" long before noticing it.
- **Fix:** Promote the overall verdict to a full-width banner above the KPI row when anything is not OK: "1 of 8 probes failing — objectstore unreachable", in a red alert treatment, with the failing probe named and linked. Reserve the small pill for the healthy state.
- **Evidence:** measured `getBoundingClientRect()` → `{x: 1221, y: 149, w: 51, h: 23, fontSize: "9px"}`; `objectstore | down | 0 ms | unreachable` in the Dependencies table.

### [HIGH] Status pills fail WCAG contrast — including the "down" state
- **Page:** /platform-status (STATUS column and global pill)
- **What happens:** Measured computed contrast at 9px:
  - `down` — `rgb(214, 63, 82)` on `rgb(255, 240, 242)` = **4.05:1**
  - `ok` — `rgb(20, 138, 85)` on `rgb(232, 247, 239)` = **3.96:1**
  Both are below the 4.5:1 requirement for text under 18.66px.
- **Why it's a problem:** WCAG 1.4.3 AA failure on the single most important word on an infrastructure status page. Low-contrast red on pale pink at 9px is also the worst case for the ~8% of men with red-green colour vision deficiency, who rely most on the text rather than the hue. (Positive: status is conveyed by text *and* colour, so WCAG 1.4.1 Use of Colour is satisfied.)
- **Fix:** Darken to ≈`rgb(175, 30, 50)` on the same pink (≈6:1) and ≈`rgb(13, 105, 64)` on the same green (≈5.6:1), and raise pill type to 11px. Same 9px/low-contrast pattern I measured on table headers app-wide.
- **Evidence:** computed-style contrast calculation quoted above, run against the actual rendered pills.

### [MEDIUM] No "as of" timestamp, no refresh control, and no auto-refresh on a live status page
- **Page:** /platform-status
- **What happens:** The page contains **zero buttons** and zero inputs. A regex for `as of|updated|refreshed` over `main.innerText` returns **false** — there is no indication of when the probes last ran. Durations ("10 ms", "3 ms") and heartbeat ages ("1 s", "4 s", "10 s") are shown as though live, but nothing says whether the page is current or was rendered ten minutes ago.
- **Why it's a problem:** A status page whose freshness is unknowable cannot be trusted during an incident — the operator can't tell if "objectstore down" is happening now or was true when they opened the tab. The relative heartbeat ages ("1 s") actively imply live data while being frozen at render time.
- **Fix:** Show "Probed at 10:14:03, refreshing every 15s", add a manual Refresh, and either poll or convert relative ages to absolute timestamps so they can't silently go stale.
- **Evidence:** `main button` count 0; no freshness string in page text.

### [MEDIUM] A failing dependency offers no next step
- **Page:** /platform-status (Dependencies → objectstore row)
- **What happens:** The row states `objectstore | down | 0 ms | unreachable`. "unreachable" is the entire diagnostic. There is no retry/re-probe action, no link to configuration, no link to the relevant logs or to /error-monitor, and no indication of which features degrade when objectstore is down.
- **Why it's a problem:** Nielsen #9 — the page diagnoses but doesn't help recover. Object storage typically backs backups and evidence export, so the blast radius matters and isn't stated.
- **Fix:** Add a "Re-probe" action, link the probe name to its settings, link to filtered logs, and list the dependent capabilities ("Backup & Restore and evidence export are affected").
- **Evidence:** row text captured; page has no interactive elements.

**Strength — the page is scrupulously honest about what it does and doesn't know.** The header says "…without overstating what has not run", and the Registered modules table is captioned "**Registration, not health.** Each row is what the platform found registered at startup — nothing here is probed, so nothing here implies a module is working." That is exactly the disclaimer most status pages omit, and it prevents the reader from mistaking a 25-module registry for 25 healthy modules. The `—` treatment for empty REASON cells is again correct (matching /error-monitor, unlike the zero-tiles elsewhere).

**Corroborates Auditor A:** live-region count **0** — notably, a platform-down condition is not announced to assistive technology anywhere.

## /bcp — BCP / DR Tests

### [BLOCKER] TIMEZONE SHIFT REPRODUCED IN A THIRD MODULE — every datetime in the product is displayed 5 hours earlier than it was entered
- **Page:** /bcp/plans/:id → New test → "Scheduled for" (and previously /changes → Window start/end)
- **What happens:** I entered `2026-09-10T09:00` into the DR test's `Scheduled for` field. The saved test renders as **`Scheduled: 9/10/2026, 4:00:00 AM`** — a **−5 hour** shift, byte-for-byte the same offset I measured on /changes (`09:00` → `4:00:00 AM`, twice). Two unrelated modules, two different entities, identical behaviour: the `datetime-local` input's naive local value is written as one zone and rendered in another. No timezone is stated on the input, on the output, or anywhere on either page.
- **Why it's a problem:** Per the coordinator's escalation rule this reproduces on a third module and is therefore **product-wide**, affecting every `datetime-local` field in the application. The consequences are concrete: a DR test scheduled for 09:00 tells the continuity team 04:00, and a change window booked for 09:00 tells the ops team 04:00. These are the two modules whose entire output is *coordinating humans around a time*. Worse, /bcp is a compliance artefact — a DR test evidence record with the wrong execution time is not defensible to an auditor. Related: /sla defaults business calendars to `Europe/London` while dates render in US M/D/YYYY format, so there is no single, stated timezone anywhere in the product.
- **Fix:** Fix once, centrally. Store all datetimes as ISO-8601 with an explicit offset (or UTC); render through one formatter using the org's configured timezone; label every datetime input and output with that zone ("Scheduled for (Europe/London)"). Add a regression test asserting round-trip equality on a `datetime-local` value.
- **Evidence:** `/bcp` — entered `2026-09-10T09:00`, rendered `Scheduled: 9/10/2026, 4:00:00 AM`. `/changes` — entered `2026-08-25T09:00` and `2026-08-20T09:00`, rendered `8/25/2026, 4:00:00 AM` and `8/20/2026, 4:00:00 AM`. Consistent −5h in all three cases.

### [HIGH] Negative RTO and RPO are accepted and displayed as recovery objectives
- **Page:** /bcp → New plan → RTO (minutes) / RPO (minutes)
- **What happens:** I entered RTO `-99` and RPO `-5`. The plan saved and now renders in the list as **`RTO -99 min · RPO -5 min`** and repeats it on the plan detail header. Both fields are `input[type=number]` with `min=""` and `max=""` — no bounds, like every other numeric field in the product.
- **Why it's a problem:** A negative Recovery Time Objective asserts that recovery completes before the outage begins. RTO/RPO are the two numbers that define a continuity plan and are quoted directly into audit evidence and customer contracts; the system publishes them without a murmur. Fourth confirmed instance of unbounded numerics (SLA targets `-50`, fiscal year `-3000`, license entitlements, now RTO/RPO).
- **Fix:** `min="0"` on both with inline validation; add a sanity warning when RTO < RPO, which is also incoherent.
- **Evidence:** list row `AuditB DR Plan | active | -99 min | -5 min | up to date | 8/19/2026, 11:14:40 AM`; `min`/`max` both empty on all three number inputs in the create dialog.

### [HIGH] A plan reviewed *never* is reported as "up to date", and an overdue review can never be detected
- **Page:** /bcp (REVIEW column) and /bcp/plans/:id
- **What happens:** The plan I created has `Last reviewed: never · Review interval: 1 days`. The list's REVIEW column nonetheless reads **"up to date"**, and the "Review overdue" KPI reads **0**. So a continuity plan that has never been reviewed, with a one-day review interval, is classified as current.
- **Why it's a problem:** The review-currency calculation is evidently anchored to the creation date rather than to the last review, so a plan that is never reviewed will only ever go overdue relative to when it was created — and a plan reviewed once and then abandoned behaves unpredictably. "Review overdue: 0" is the metric a compliance officer trusts, and it is wrong in the one case that matters most (never reviewed).
- **Fix:** Treat `last_reviewed = null` as **overdue** (or as a distinct "never reviewed" state), and surface it in the KPI and the column. Never render "up to date" for a plan with no review on record.
- **Evidence:** detail page text `Last reviewed: never · Review interval: 1 days`; list REVIEW cell `up to date`; KPI `Review overdue 0`.

### [MEDIUM] Six-field plan form has no required fields and no explanation of RTO/RPO
- **Page:** /bcp → New plan
- **What happens:** All six controls report `required === false`: Name, Description, RTO (minutes), RPO (minutes), "Procedure refs (comma-separated)", Review interval (days). No hint text explains RTO vs RPO, and procedure refs are a comma-separated free-text string rather than a repeatable field or a link picker.
- **Why it's a problem:** RTO and RPO are routinely confused even by practitioners; a form that offers neither definition nor example invites incorrect data into a compliance record. Comma-separated free text is a classic source of dirty data (trailing spaces, inconsistent separators) and cannot be validated or linked.
- **Fix:** Mark Name/RTO/RPO required; add one-line definitions under each ("RTO — how long recovery may take"; "RPO — how much data loss is tolerable"). Make procedure refs a repeatable field or a picker over /docs articles, which already exist.
- **Evidence:** field enumeration with `req=false` on all six.

### [MEDIUM] "Archive" sits beside "Edit plan" with no confirmation
- **Page:** /bcp/plans/:id (header actions: Record review | Edit plan | Archive)
- **What happens:** The three header buttons are visually identical. I did not click Archive (it would destroy my ability to finish testing the plan), but the pattern matches the unconfirmed Delete actions I verified on /sla and the unconfirmed Resolve on /error-monitor, neither of which prompted.
- **Why it's a problem:** Consistent with the product-wide absence of destructive-action confirmation.
- **Fix:** Confirm before archiving, and style the action distinctly.
- **Evidence:** button enumeration `["Record review","Edit plan","Archive","New test"]`, all sharing the same styling. **Not clicked — marked untested by choice.**

**Strength:** the DR test workflow is the most complete lifecycle I found in the product — a test moves through `pending` → Record outcome (Executed at, RTO met, RPO met, Outcome pass/partial/fail, Notes) → per-step results (Ordinal, Description, Result, Evidence ref) → Findings (Description, Severity low/medium/high/critical, Remediation), plus a "View report" that promises "the frozen backup-evidence snapshot captured at that moment". This is exactly the state machine /changes is missing, and it proves the team knows how to build one — /changes should adopt this module's pattern.

**Corroborates Auditor A:** live-region count **0** on the list, in both dialogs, and after two successful creates (plan and DR test) — two more silent successes.

## /assistant/autonomy — AI Automation (Assistant autonomy)

### [BLOCKER] A security control is mutated and persisted by a single dropdown change with no Save, no confirmation and no feedback of any kind
- **Page:** /assistant/autonomy (persona × risk-tier matrix)
- **What happens:** The page is a 3×3 matrix of `<select>` controls setting the AI assistant's autonomy ceiling per persona (end_user / technician / admin) against risk tier (LOW / MEDIUM / HIGH), each offering `off | suggest | require_approval`. There is **no Save button anywhere on the page** (`main button` count = **0**). I changed technician/LOW from `off` to `suggest`. Result: no toast, no "Saved", no "Unsaved changes", no live region (`[role=alert]/[role=status]/[aria-live]` = 0), no text matching `saved|saving|unsaved` anywhere on the page. I then performed a **full page navigation** back to the route and the value read back as **`suggest`** — the change had been written and persisted. I restored it to `off` and verified the restore the same way.
- **Why it's a problem:** This is the most consequential instance of the product's silent-mutation pattern. The control governs what an AI assistant is permitted to do inside an IT management platform — an obvious target for both accidental change and, given the audit trail records only that *something* changed, for deniability. A user who bumps a dropdown with a scroll wheel while reading the page permanently widens the AI's autonomy ceiling and receives no indication whatsoever. There is also no confirmation step for *raising* a ceiling, which is the direction that carries risk.
- **Fix:** For a security control, auto-save is the wrong pattern — add explicit Save/Cancel with a dirty-state indicator. If auto-save is retained, it must produce a confirmation in a live region ("technician / LOW set to *suggest*") plus an Undo, and *raising* a ceiling should require a confirmation naming the new capability. Record before/after values in the audit log.
- **Evidence:** `main button` count 0; after change `{live: 0, anyDirtyText: false}`; after full reload `selects = ["suggest","off","off","off","off","off"]`; after restore and reload `["off","off","off","off","off","off"]`.

### [MEDIUM] The matrix has no row/column explanation of what the three settings actually permit
- **Page:** /assistant/autonomy
- **What happens:** Cells offer `off`, `suggest`, `require_approval` with no legend, tooltip or description of the difference. The `end_user` row is `off (fixed)` across all three tiers with no explanation of why it cannot be changed. The only guidance is a single sentence above the table: "A ceiling can only ever lower what an approved action already permits — the effective setting is the minimum of the two, and there is no autonomous position: every action an assistant prepares is a proposal a human must confirm," plus "An unset cell means `off`."
- **Why it's a problem:** The header copy is excellent on *policy* but says nothing about *these three values*. The practical difference between `suggest` and `require_approval` — when both still require human confirmation — is genuinely unclear, and a user setting a security ceiling should not have to guess. The `(fixed)` state gives no reason.
- **Fix:** Add a short legend defining each value with an example, and a tooltip on `(fixed)` explaining that end users can never be granted autonomy.
- **Evidence:** page text captured in full; the only options text is `off | suggest | require_approval` with no accompanying definitions.

### [LOW] What LOW / MEDIUM / HIGH risk mean is never defined
- **Page:** /assistant/autonomy (column headers)
- **What happens:** Columns are labelled LOW, MEDIUM, HIGH with no indication of which platform actions fall into each tier.
- **Why it's a problem:** The administrator is setting a policy over a classification they cannot see. "Is deploying software MEDIUM or HIGH?" is unanswerable from this page.
- **Fix:** Link each column header to the list of actions in that tier, or show representative examples on hover.
- **Evidence:** column headers are bare text; no legend present.

**Strength:** the policy model is well designed and honestly stated — a ceiling can only *lower* what a permission already grants (effective = min of the two), there is no fully-autonomous setting, and unset defaults to `off`. Fail-safe defaults and a least-privilege intersection are the right architecture, and the page says so plainly. The 6 configurable selects all carry accessible names (0 of 6 unlabelled).

**Corroborates Auditor A — strongest silent-success instance found:** a persisted write to a security control with zero user feedback and no live region.

## /enrollment — Enrollment & Certificates

### [BLOCKER] Direct load or refresh of /enrollment returns raw API JSON with no application shell
- **Page:** /enrollment (hard navigation / refresh / bookmark)
- **What happens:** Navigating the browser directly to `https://itms.toeshee.com/enrollment` renders the browser's raw JSON viewer showing **`{"detail":"Not Found"}`** — black background, "Pretty-print" checkbox, no sidebar, no header, no application chrome, no way back except the browser Back button. Reaching the same route by **clicking the sidebar link works perfectly** (`location.pathname === "/enrollment"`, full shell, `h1: Enrollment & certificates`, working tabs). So the SPA route exists; the server has no HTML fallback for this path.
- **Why it's a problem:** Any user who bookmarks this page, refreshes it, opens it in a new tab, or is sent the URL by a colleague hits a raw API 404 and reasonably concludes the platform is broken. This is the certificate and agent-enrollment module — the page an engineer is most likely to keep open and refresh while enrolling endpoints. It is also the only route of the 14 I loaded directly that behaves this way: /sla, /docs, /changes, /jobs, /reports, /budget, /ipam, /audit, /licenses, /subscriptions, /error-monitor, /platform-status, /bcp and /assistant/autonomy all hard-load correctly.
- **Fix:** Add the SPA history fallback for `/enrollment` (and audit the server route table for any other path missing it). Separately, the application should render its own styled 404 page rather than letting an API JSON body reach the browser.
- **Evidence:** screenshot of the raw JSON viewer showing `{"detail":"Not Found"}`; sidebar link confirmed as `Enrollment & Certificates -> /enrollment`; SPA click to the same path renders the full page.

### [HIGH] "Create token" mints a live enrollment credential instantly — no form to fill, no confirmation
- **Page:** /enrollment → Enrollment tokens → "Create token"
- **What happens:** I clicked "Create token" expecting it to open a form. It **immediately created an active enrollment token** (`06a85d98-…`, expiring 8/22/2026, `0 / ∞` uses) using default values, and only *then* revealed the input fields (name, a `72` hour lifetime, a max-uses field) beneath. There was no confirmation step and no opportunity to set the name, lifetime or use limit before the credential existed.
- **Why it's a problem:** An enrollment token is a bootstrap credential that lets a new machine join the fleet. Creating one should be deliberate. Here a single exploratory click on a button that looks like it opens a form produces a live, **unlimited-use** credential. The button label describes the intent, not the immediate irreversible effect — Nielsen #5.
- **Fix:** "Create token" should reveal the form; a separate "Issue token" inside the form performs the creation after the user has set name, lifetime and max uses. Confirm before issuing.
- **Evidence:** token list before the click began `06a84404-…`; immediately after it began `06a85d98-7363-7a12-8000-35972359f76c | 8/22/2026, 11:27:51 AM | 0 / ∞ | active`. I revoked this token and confirmed the revoke; Active enrollment tokens returned from 2 to 1.

### [HIGH] Enrollment tokens default to unlimited uses and are all "(unnamed)"
- **Page:** /enrollment (token table: TOKEN / EXPIRES / USES / STATE)
- **What happens:** Every token in the list — four of them — displays as **`(unnamed)`** followed by a raw UUID, and every one shows uses as **`0 / ∞`** or **`1 / ∞`**: unlimited redemptions until expiry. The token I accidentally created inherited the same `∞` default.
- **Why it's a problem:** An unlimited-use bootstrap credential is a standing risk — one leaked token enrolls an unbounded number of machines until it expires. Combined with every token being unnamed, an operator looking at this table cannot tell what any token was for, who issued it, or whether it is still needed, so the safe action (revoke) is indistinguishable from the destructive one.
- **Fix:** Default max uses to 1 and require an explicit opt-in for higher; make the name required at issue time and show issuer and purpose in the table.
- **Evidence:** four token rows captured, all `(unnamed)` with `0 / ∞` or `1 / ∞` uses.

### [MEDIUM] Token secret is shown once in an alert the user can dismiss before copying, with no copy button
- **Page:** /enrollment (post-creation reveal)
- **What happens:** After creation a `role="alert"` panel appears reading: *"Copy this token now — it is shown once. The control plane stores only a hash of it and cannot show it again."* followed by the id, the secret value, and an "I have copied it" button. There is **no copy-to-clipboard button** — the user must manually select a long high-entropy string — and no confirmation step guarding the dismiss.
- **Why it's a problem:** The warning is correct and well written, but the interaction works against it: manual selection of a ~43-character secret is error-prone, and one click on "I have copied it" destroys the only copy. Nielsen #5/#6.
- **Fix:** Add a prominent "Copy" button that must be used (or explicitly overridden) before "I have copied it" enables; show a copied-confirmation.
- **Evidence:** live-region text captured verbatim; only button present is "I have copied it".

**CONTRADICTS Auditor A — two genuine positives found here, the only ones in the product:**
1. **The first and only live region in the entire audit.** `[role=alert]` count on /enrollment after token creation is **1**, not 0 — a `<div role="alert">` carrying the token-reveal warning. Every other page and modal I measured across 14 routes returned 0. So the product does have exactly one correctly-announced message, on its most security-sensitive action. This is the pattern to generalise: the component already exists.
2. **The first and only destructive-action confirmation in the product.** Clicking "Revoke" does **not** revoke immediately — it swaps the cell to an inline **"Confirm revoke | Cancel"** pair, and only the second click performs the action (verified: state went `active` → `revoked`, Active tokens 2 → 1). Contrast /sla Delete, /error-monitor Resolve and /bcp Archive, which have no confirmation at all. **/enrollment's inline two-step confirm is the pattern to standardise on** — it is lightweight, requires no modal, and is already built.

**Care rules observed:** I did not modify certificates, signing keys, SSO or any connectivity setting. The one write I made was an enrollment token created **unintentionally** by a button whose label implied it opened a form (finding above); I revoked it immediately and verified the system returned to its prior state (Active enrollment tokens back to 1). The Certificates and Enrollment flow tabs were not opened — time ran out.

## /settings — Settings

### [MEDIUM] Org profile is a one-field form whose only control is a Save button with no feedback path
- **Page:** /settings → Organization tab
- **What happens:** The tab contains exactly one input (Organization name = "Toeshee") and a "Save changes" button, with the note "This server allows no org settings keys beyond the name." `[role=alert]/[role=status]/[aria-live]` = **0**, so a save here will follow the product-wide silent-mutation pattern.
- **Why it's a problem:** Renaming the organization is a tenant-wide change with no confirmation and, based on every other module measured, no success feedback.
- **Fix:** Add a dirty-state indicator, a confirmation for tenant-level identity changes, and a success message in a live region.
- **Evidence:** single `text` input labelled "Organization name"; live-region count 0.
- **Not saved:** per the care rules I inspected but did not submit this form.

### [LOW] Only 6 of the platform's 25 registered modules have a settings tab
- **Page:** /settings (tab bar)
- **What happens:** Tabs are Organization, Assistant, Notifications, Service levels, Health, Budget. /platform-status lists **25 registered modules**. The page promises "**Every** configuration this platform serves, edited here rather than on the server."
- **Why it's a problem:** The header's claim is broader than what the page delivers, so a user hunting for (say) IPAM or backup configuration will search here and conclude it doesn't exist.
- **Fix:** Soften the copy to name the scope, or list configurable-but-server-only modules with an explanatory note.
- **Evidence:** 6 tabs enumerated; Registered modules table on /platform-status shows 25 rows.

**CONTRADICTS Auditor A and my own earlier findings — /settings is the quality high-water mark of the product. Three patterns that are broken everywhere else are correct here:**

1. **Tabs are properly implemented.** `parentRole: "tablist"`, and all six tabs report `role="tab"` with `aria-selected` correctly set (`["tab/true","tab/false","tab/false","tab/false","tab/false","tab/false"]`). This directly contradicts my /budget and /ipam findings, where the equivalent tab bars had `role: null` and no `aria-selected`. **A correct Tabs component already exists in this codebase — /budget and /ipam simply aren't using it.** That reframes those two findings from "build accessible tabs" to "adopt the component you already have", which is a much cheaper fix.

2. **Numeric inputs are bounded — the only place in the product where they are.** On the Assistant tab: `Transcript retention (days) min=1`, `Max turns per user per minute min=1`, `Max turns per organization per hour min=1`, `Daily token budget min=0`. Every other numeric field I tested across five modules had `min=""` and `max=""` and accepted nonsense (`-50` SLA target, `-3000` fiscal year, `-99` RTO, `-5` RPO). Again: the right pattern exists in the codebase and is simply not applied consistently.

3. **Best-in-class data-egress disclosure.** The Assistant tab enumerates precisely what leaves the network — "The system prompt… The tool schemas… The administrator's question text… **Tool results: real customer data.** Hostnames, ticket subjects and bodies, software inventory, alert text, license and vendor names, spend figures, audit action names" — and what never does: "**Vault material — stored credentials and license keys.**" It further states "Provider API keys and base URLs are server configuration. They are never returned by the API, never shown here and never entered here." Requiring an explicit "I understand what leaves this network, as listed above" checkbox alongside the enable toggle is informed consent done properly, and keeping API keys entirely out of the UI is the correct security posture.

## Responsive sweep — attempted, partially blocked

**`resize_window` does not work in this environment.** Calls to resize tab 1442057179 to 390×844 returned "Successfully resized" but `window.innerWidth` remained **1313** and the rendered screenshot was unchanged at desktop width. The browser window appears to be managed/maximised, so viewport-based responsive testing could not be performed by either auditor. **Recording as untestable via automation — this needs a manual pass or a device-emulation harness.**

What I *could* measure, by inspecting the stylesheets directly, is more damning than a screenshot would have been:

### [HIGH] The application has essentially no responsive CSS — one breakpoint at 1300px and nothing below it
- **Page:** global (all routes)
- **What happens:** Enumerating every CSS rule across all loaded stylesheets finds **3 media-query rules in total**, and the only pixel breakpoint used anywhere is **`1300px`**. There is no tablet breakpoint, no mobile breakpoint, and nothing in the 320–1024px range. The `<meta name="viewport">` tag *is* correctly set (`width=device-width, initial-scale=1.0`), so a phone will render the desktop layout at device width rather than zooming out — which is the worst of both worlds.
- **Why it's a problem:** The product is desktop-only in practice while declaring itself mobile-ready. The fixed sidebar occupies 260px of a 390px phone viewport (67% of the screen) and there is no collapse-at-breakpoint rule to move it out of the way — the sidebar's collapse is a manual button, not responsive behaviour. Every data table in the product (audit log with 5 columns, jobs, licenses, IPAM, /reports output) will overflow horizontally. For an IT management console this matters concretely: on-call engineers triage from phones, and /platform-status, /jobs and /error-monitor are exactly the pages someone opens at 2am from a handset.
- **Fix:** Add breakpoints at ~1024px and ~640px: collapse the sidebar to an overlay drawer below 1024px, switch KPI tile rows from 4-up to 2-up then 1-up, and give tables a horizontal-scroll container with a sticky first column (or a card layout below 640px). Verify the 44×44px tap-target minimum at the same time.
- **Evidence:** stylesheet scan → `{mediaQueryRules: 3, breakpoints: ["1300px"], viewportMeta: "width=device-width, initial-scale=1.0"}`.

### [MEDIUM] 49 interactive controls are under the 44px minimum tap-target height on a single page
- **Page:** /settings (measured); pattern is global
- **What happens:** Measuring every visible `button, a, input, select` on /settings, **49** have a rendered height below 44px.
- **Why it's a problem:** WCAG 2.5.5 Target Size (Enhanced, AAA) and the 2.5.8 Minimum (AA, 24px) baseline. Combined with the absence of mobile breakpoints, touch use is impractical. Note this count is at desktop width — the sidebar's 28 nav links make up a large share of it.
- **Fix:** Set a 44px minimum height on interactive controls, or at minimum 24px plus adequate spacing to satisfy 2.5.8.
- **Evidence:** `[...document.querySelectorAll('button,a,input,select')].filter(r.height < 44).length` → **49** on /settings at 1313px viewport.
