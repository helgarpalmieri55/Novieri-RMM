# Browser + axe pass over the rendered console

**Date:** 2026-08-21
**Branch:** `claude/handoff-spec-review-f45idd`
**Subject:** the dev-only harness at `/__preview` (`frontend/src/preview/PreviewPage.tsx`)
**Engine:** Chromium 1194 (Playwright 1.56.1), headless, `deviceScaleFactor: 1`
**Tooling:** `axe-core` 4.13.0 + a composited-pixel sweep, both in
`frontend/scripts/a11y-browser.mjs`

## Why this document exists

`docs/product.md` §11 makes WCAG 2.2 AA binding, lists the four CI guards that
hold it, and then says exactly what they do not reach:

> **What none of that covers, stated so nobody mistakes green for accessible.**
> These read tokens and stylesheets as text. jsdom implements no cascade and
> evaluates no media query, so no test here can see a colour composited by
> opacity, painted by a gradient, or the actual rendered result at a real
> viewport. A browser pass with axe over the running console is the complement
> and **has not been done**.

It has now. Everything below was measured in a real browser at a real viewport.
Every finding carries the number, the viewport and the selector that produced
it.

The headline is not a contrast ratio. **The console's destructive-confirmation
dialog does not render as a dialog** — it lays out as a full-width block in the
document flow, below the fold, underneath its own dimming scrim. Twelve unit
tests assert its contents and all twelve pass, because jsdom has no cascade and
no layout. That is the same class of defect that created `shell/base.css`
(`th` and `td` shipping with no styling at all past a green suite and a clean
`tsc`), and it is the argument for the preview harness restated with a worse
example.

**Read the findings below as the measurements that were taken, not as the
current state.** [B1], [H1], [H2], [H3], [M4] and the accessibility half of
[M3] were closed in a second pass the same day; each carries its own **FIXED**
entry with the re-measurement, and *What was changed* at the end lists the
files. What is still open is [M3]'s four gradients, [M5] — which is now the
harness's entire output, and is proposed there rather than taken — and [L1],
[L2], [L3].

## How the measurements were taken

**axe-core** was injected into the live page and run against
`wcag2a, wcag2aa, wcag21a, wcag21aa, wcag22aa, best-practice` at four viewports
and in five states (default, confirm dialog open, command palette open, drawer
open, account menu open). The first pass ran only the default state and found
two violations; the states axe never reached held three more, one of them
`critical`. A pass that does not open the overlays is not a pass over the
console.

**The composited-pixel sweep** exists because axe cannot resolve a background
it cannot find an opaque ancestor for. At 1440px it returned `52 incomplete`
nodes — the whole rail, every filled chip, both gradient buttons — which is
precisely the set no other guard in this repo can see either. The sweep:

1. records each text run's computed paint — `color` for HTML, **`fill` for
   SVG**, which is not the same property and not the same cascade; a first
   version of this sweep graded every chart axis label against `--ink`, which
   nothing paints them with — plus its accumulated ancestor `opacity`;
2. blanks every glyph and screenshots the viewport, leaving the exact
   **backplate**: gradients resolved, translucent washes composited, shadows
   painted;
3. samples that backplate inside each run's own **line boxes** (Range rects,
   not the element's layout box), hit-testing every point with
   `elementFromPoint` and clipping to the element's rounded border box;
4. composites the foreground through that opacity over the worst-case backplate
   pixel.

Steps 3 and 4 are not fastidiousness. Two intermediate versions of this sweep
produced **49 and then 14 "failures"**, every one of them an artefact: pixels
from a clipping scroll container, pixels outside a `border-radius`, pixels of
the element's own antialiased edge. The last artefact to go was the
notification dot — white on `--primary`, a true **5.35:1** — reported at
**3.79:1** against `#4f82da`, a colour painted nowhere on the page, because a
999px radius cuts several pixels into the corners of a 25×21 box. A measurement
harness that invents failures gets ignored, and then it gets deleted.

**The self-check runs first, every time.** `scripts/a11y-browser.mjs` repaints
the table headers at `#878590` — the exact literal auditor A measured from the
live DOM — and requires the sweep to report it. It does, at **3.49:1**, which
is auditor A's number to the second decimal. The same check was re-run after
the rounded-box guard was added, using a *rounded* element (`.status.online` at
`#7fbf9f`, reported at 1.92:1), because a guard that suppresses artefacts on
pills could just as easily have suppressed real failures on pills.

Coverage is reported rather than assumed: at 1440px, **166 of 204** text runs
were measured. The 38 that were not are the charts' `sr-only` data tables, the
`sr-only` spans, the skip link before it is focused, and two collapsed
`<details>` bodies — all genuinely unpainted. Nothing is claimed about them.

---

# Findings

## [B1] BLOCKER — the confirm dialog is not a modal; it renders in the document flow, under its own scrim · **FIXED**

*Every viewport · `section.modal.open.confirm[role="dialog"][aria-modal="true"]`*

`ConfirmDialog` (`frontend/src/components/confirm.tsx:79-113`) renders
`<section className="modal open confirm">`. **There is no `.modal` rule in this
application.** `grep -rn '\.modal' frontend/src/shell/*.css` returns four hits
and all four are the comma-joined child rules — `.drawer-head, .modal-head`,
`.drawer-title, .modal-title`, `.drawer-foot, .modal-foot`. `.modal` itself,
`.modal.open` and `.modal-body` are absent.

Measured in Chromium at 1440×900 with the dialog open:

```
overlay  z-index=70  position=fixed  background=rgba(10,9,13,.38)  backdrop-filter: blur(3px)
modal    z-index=auto  position=static  rect = 284, 613, 1130, 244
elementFromPoint at the modal's own header -> "overlay open"
```

`position: static`, `z-index: auto`, no background, no width, no radius, no
shadow. The dialog is a 1130px-wide block sitting at y=613 — below the fold at
900px tall — with the 38%-black blurred scrim painted **on top of it**. A
screenshot confirms it: the heading, the subject, the consequence copy, the
type-to-confirm field and both buttons are all rendered behind the dim.

The contrast consequences, measured from those pixels:

| element | measured | floor |
|---|--:|--:|
| `button.btn.primary.danger "Delete device"` | **2.11:1** | 4.5 |
| `p.confirm-body "Its inventory, job history…"` | **2.14:1** | 4.5 |
| `label.confirm-typing > span "Type … to confirm"` | **2.14:1** | 4.5 |
| `label.confirm-typing > span > strong "ws-alpha.corp.local"` | **2.14:1** | 4.5 |

Those four are not four findings. They are one finding, and this is it.

**Root cause, and it is recorded in the file that has the gap.**
`overlays.css:13-16` says so in as many words:

> `.drawer-head`/`.drawer-title`/`.drawer-foot` are joined to `.modal-head`/
> `.modal-title`/`.modal-foot` by a comma in the source rule. **Modal is not
> one of this task's three components**, but the rule is transcribed exactly as
> the design writes it rather than split to fit this file's scope.

The brace-counting extraction was scoped to Drawer, CommandPalette and Toaster.
It carried the comma-joined `.modal-*` children across as a side effect and
left `.modal` behind. Cycle 8 then wrote `ConfirmDialog` against class names
that were half-present.

**The design has the missing rules and they are unambiguous** —
`docs/design/itms-prototype.html:753-771` declares `.modal` (`position: fixed;
inset: 50% auto auto 50%; z-index: 95; width: min(650px, 90vw); max-height:
88vh;` plus the white fill, 17px radius, shadow, and the opacity/transform
transition), `.modal.open`, and `.modal-body { padding: 18px; overflow-y:
auto; }`.

**FIXED**, and it was larger than the confirmation. The shared `Dialog` in
`components/ui.tsx` renders `class="modal open"` too, so the six module dialogs
its own docblock counts out — Add channel, Add mailbox, Provision a device,
Promote a device, New license, New subscription — were laying out the same way
and were never measured. One transcription closed all of them.

The three rules are now in `frontend/src/shell/overlays.css`, in the design's
own order (they follow `.close-btn` there too), copied declaration for
declaration with no tidying, and the file header no longer says modal is out of
scope. Nothing else was left behind: `.overlay` was already present and
correct, and `.confirm` has no counterpart in the design at all — the
`.confirm-*` children are this application's own and are in `components.css`.

Measured again in Chromium at 1440×900, same harness, dialog open:

```
before  modal  z-index=auto  position=static  rect = 284, 613, 1130, 244   scrim ON TOP
after   modal  z-index=95    position=fixed   rect = 395, 309,  650, 282   scrim beneath
```

Centred to the pixel, wholly above the fold, and `elementFromPoint` at its own
header returns the dialog rather than `"overlay open"`. All four contrast
failures went with it — they were one finding and they had one cause. The
harness now asserts that geometry (`position`, on-screen, centred, and
`elementFromPoint` hit-testing the dialog above its scrim) rather than the
class name, and `src/test/base-styles.test.ts` asserts the three rules still
match the design declaration for declaration and that `.modal`'s z-index
exceeds `.overlay`'s — comparing the two, not hardcoding 95, since a scrim
raised to 200 would pass a hardcoded check.

**Why nothing caught it.** `confirm.test.tsx` asserts DOM contents in jsdom,
which implements no cascade and no layout, so a `<section>` with no matching
rule is indistinguishable from a centred modal. `contrast.test.ts` reads token
pairs out of stylesheets and cannot be about a rule that does not exist.

---

## [H1] HIGH — the charts' screen-reader tables scroll the whole page sideways, at every viewport · **FIXED**

*1440, 1024, 768 and 390 · `figure.chart > table.sr-only`*

| viewport | `documentElement.scrollWidth` | `clientWidth` | overflow |
|---|--:|--:|--:|
| 1440×900 | 1754 | 1440 | **314px** |
| 1024×768 | 1068 | 1024 | 44px |
| 768×1024 | 812 | 768 | 44px |
| 390×844 | 469 | 390 | 79px |

**Causation, not correlation.** Setting `display: none` on the two
`table.sr-only` elements and nothing else takes `scrollWidth` from **1754 to
exactly 1440** — `clientWidth` — at 1440×900.

The mechanism is a cascade-layer defeat that no text-reading guard can see:

```
table.sr-only  computed: width=1440px  height=188.078px  position=absolute  clip-path=inset(50%)
span.sr-only   computed: width=1px                                  <- the same utility, non-table
```

`sr-only` is Tailwind's utility and lives in the `utilities` cascade layer.
`shell/base.css` declares `table { width: 100%; border-collapse: collapse; }`
**unlayered**, and unlayered author CSS beats any layered rule regardless of
specificity. So `width: 1px` loses, the table lays out at the full width of its
containing block, `white-space: nowrap` sets its min-content width to 863px at
narrow viewports, and `clip-path` hides it visually while leaving every pixel
of it in the document's scrollable overflow.

`body { overflow-x: auto }` (`index.css`) then turns that into a real
horizontal scrollbar on every screen of the console that draws a chart. WCAG
2.2 SC 1.4.10 Reflow is the criterion; a phone user panning sideways to reach
nothing is the symptom.

**FIXED, and the diagnosis above was half of it.** Both halves landed, because
the layer defeat turned out not to be the only mechanism.

**(1) The layer defeat.** `.sr-only` is now redeclared **unlayered** in
`shell/base.css`, three rules below the `table` rule it has to beat. In the
same layer a class (0,1,0) out-ranks a type selector (0,0,1) by specificity —
and out-ranks any *future* unlayered element rule for the same reason, which is
the property option (a) did not have. Option (a) was rejected on measurement
rather than taste: wrapping this file in `@layer base` would also put its
element rules into source-order competition with Tailwind's own preflight,
which declares several of the same elements, so `table { width: 100% }` would
start depending on import order. `base-styles.test.ts` now asserts the rule is
there, that the file contains no `@layer` at all, and — the non-vacuity guard —
that `table { width: 100% }` is still in the same file, so the test stops being
about anything the moment its subject leaves.

**(2) A table cannot be made narrow, and that is not a cascade problem.** With
the utility fully applied and winning, the element still measured **427.78px**:
a table's used width is `max(specified, min-content)`, so `width: 1px` on a
`<table>` is a floor the browser ignores, and `overflow: hidden` on a table box
does not reliably clip either. This is why option (b) was also needed and why
option (c) would not have worked at all — `!important` on `width` would have
changed nothing. `DataTable` now renders `<div className="sr-only"><table>…`.
A div has no such floor, and clipping is a paint concern, so caption,
`scope="col"` and `scope="row"` all survive intact; `chart.test.tsx` asserts
both the wrapper and the surviving column header.

Measured again, all four viewports, `documentElement.scrollWidth` against
`clientWidth`:

| viewport | before | after |
|---|--:|--:|
| 1440×900 | 1754 / 1440 | **1440 / 1440** |
| 1024×768 | 1068 / 1024 | **1024 / 1024** |
| 768×1024 | 812 / 768 | **768 / 768** |
| 390×844 | 469 / 390 | **390 / 390** |

The 390 row is worth a sentence, because the audit reported it under one cause
and it had a second. Layer fix alone left it at 469 — Chromium's mobile
emulation does not give a too-wide document a scrollbar, it lays the page out
wider and scales it down to fit, so `innerWidth` read 469 against a 390
`clientWidth` and the whole console rendered at 83%. That was the 427.78px
table, and only the wrapper closed it. The harness's overflow check now prints
`innerWidth` alongside the scroll numbers and names the widest boxes crossing
the right edge, skipping anything inside a scroll container — finding this one
by hand took a bisection that the check should have printed.

---

## [H2] HIGH — the confirm dialog has no focus trap and does not give focus back · **FIXED**

*1440×900 · `section.modal.open.confirm`*

`aria-modal="true"` is declared. Nothing behind the dialog is `inert` or
`aria-hidden`, and the keyboard walks straight out of it.

```
trigger focused:            BUTTON.btn "Delete device"
after Enter:                BUTTON.btn "Cancel"          <- correct, and deliberate (confirm.tsx:63-66)
Tab ×4, dialog still open:  A.nav-item "Dashboard"       <- in the rail, behind the scrim
background inertness:       main[inert]=false  main[aria-hidden]=null  sidebar[inert]=false
```

Escape does close it — the handler is on `document` (`confirm.tsx:67-71`), so
it fires from anywhere, which is right. But focus is not restored:

```
trigger before open:  BUTTON.btn "Delete device"
after open:           BUTTON.btn "Cancel"
after Escape:         BODY                              <- focus is lost; the next Tab restarts at the skip link
```

For a sighted keyboard user this means the destructive confirmation can be
tabbed past and anything behind it activated, while `aria-modal="true"` tells
assistive technology the opposite. WCAG 2.2 SC 2.4.3 Focus Order.

The dialog's accessible **name** is fine — `aria-label={title}` is set
(`confirm.tsx:79`); `aria-labelledby`/`aria-describedby` are null but are not
needed given the label.

**FIXED, by extending the shared machinery rather than writing a third copy
of it.** `Dialog` already had focus-in, Escape and scroll lock, and its
comments already carried two lessons paid for in tests. Those are now a hook,
`useDialogSurface` in `components/ui.tsx`, and all three surfaces use it:
initial focus, a Tab trap, document-level Escape, scroll lock, and focus
restored on close. Both preserved lessons are still in it and still explained
at the site.

**The trap is keyed on containment, not on a stack of open surfaces**, and that
is a deliberate choice: a `ConfirmDialog` raised over a `Dialog` puts two
handlers on the document at once, each acts only while focus is inside its own
root, and the inner one is bound later so it wins the overlap. A module-level
stack would instead depend on effect ordering, which React runs child-first —
exactly backwards for a confirmation nested inside the form it is confirming.
`disabled` controls are excluded from the cycle, so the type-to-confirm
dialog's blocked button is not a stop.

Re-measured, same harness:

```
before  Tab x4, dialog open:  A.nav-item "Dashboard"     after  14 Tabs stay inside
before  after Escape:         BODY                       after  BUTTON.btn "Delete device"
```

**One thing this surfaced that the audit could not have.** Making
`ConfirmDialog` lock scrolling broke three deploy-module tests immediately,
including one asserting merely that no dialog was open. Every surface used to
remember the `overflow` it found and put that value back; a confirmation over
an open dialog remembers `"hidden"`, so when the two unmount in the same commit
the **order of the two cleanups** decides whether the page stays locked
forever. The lock is now reference-counted — saved once at 0→1, restored once
at 1→0 — and `dialog.test.tsx` opens a stacked pair and closes both in one
commit, which is the case the old code could only get right by luck.

---

## [H3] HIGH — the command palette: an invalid listbox, an unnamed listbox, no trap, and Escape that only works from one element · **FIXED**

*1440×900 · `section.palette[aria-label="Global search"]`, `#palette-results`*

axe, with the palette open and a query typed — three violations that the
default-state pass never saw:

| rule | impact | SC | node |
|---|---|---|---|
| `aria-required-children` | **critical** | 1.3.1 | `#palette-results` — "Element has children which are not allowed: h3" |
| `aria-input-field-name` | serious | 4.1.2 | `#palette-results` — "aria-label attribute does not exist or is empty" |
| `color-contrast` | serious | 1.4.3 | `.palette-search > kbd` at 3.44:1 — see [M2], now fixed |

The `critical` one is structural: `CommandPalette.tsx:186-217` renders the
no-results empty state — `<div className="empty">` containing an `<h3>` —
**inside** the `role="listbox"` container. A listbox may contain only `option`
and `group`; a heading inside one is undefined to a screen reader, and the
empty state is the case a searching user hits most often.

Keyboard, measured:

```
open with "/" -> focus:  INPUT "Search the whole platform"   (autoFocus, correct)
Escape immediately:      palette closes; focus -> BODY       (not restored to the trigger)
Tab once:                BUTTON.btn "Enroll device"          (out of the palette, page behind)
Escape after that Tab:   palette still open                  (handler is on the input only)
```

`section.palette` carries `role=null` and `aria-modal=null`, so it is not a
dialog to assistive technology at all — unlike `ConfirmDialog`, which declares
both. Escape is `onKeyDown` on the input (`CommandPalette.tsx:123-126`); once
Tab moves focus out, the palette can no longer be dismissed by keyboard.

**Positive, and worth recording:** `aria-activedescendant` and `aria-controls`
are both wired, `role="option"` and `aria-selected` are on each result, and
ArrowUp/ArrowDown move the selection — all of which the file header says the
prototype has none of. The defects above are the remaining half of that
pattern, not a rejection of it.

**The palette itself renders correctly** — `position: fixed; z-index: 100;
background: #fff; rect = 370,88,700,334` — which is what makes [B1] a
transcription gap rather than a systemic one.

**FIXED, all four.** The empty state is now a sibling of the listbox rather
than a child of it, so `role="listbox"` owns options and nothing else; the
listbox stays mounted when empty so the input's `aria-controls` keeps pointing
at something real, and it carries `aria-label="Search results"`. The section
declares `role="dialog"` and `aria-modal="true"` — which it can now honestly
claim, because it uses the same `useDialogSurface` as [H2]: Escape is on the
document, Tab is trapped, and focus returns to the top bar's search trigger.
Both axe violations are gone at every state; the `kbd` was already fixed in
[M2].

**The `autoFocus` trap, which cost a measurement to find and is worth
recording.** With the hook wired in, the palette trapped Tab and answered
Escape correctly and *still* closed onto `<body>`. React applies `autoFocus`
during the commit, which runs **before** passive effects — so by the time the
hook read `document.activeElement` to remember what to restore, the answer was
the palette's own input, and Escape faithfully restored focus to a node React
had just detached. `autoFocus` is gone (the hook owns initial focus, after it
has recorded the opener) and the hook now refuses to record an opener that is
inside its own surface, so a future `autoFocus` degrades to no restore rather
than to a silent wrong one. `overlays.test.tsx` fails on either half being
reverted; verified both ways.

---

## [M1] MEDIUM — the global search field's placeholder measured 4.26:1 · **FIXED**

*1440×900 · `header.topbar > button.topbar-search > span "Find or ask…"`*

Found independently by both halves of the pass, to the same number:

```
axe   color-contrast [serious] wcag2aa,wcag143
      "insufficient color contrast of 4.26 (foreground #777581, background #f8f8fa,
       font size 10.5pt (14px), font weight normal). Expected 4.5:1"
sweep FAIL 4.26:1 (floor 4.5) fg=#777581 bg=#f8f8fa 14px/400
```

**Why the token guard could not see it.** `.topbar-search` painted *both*
halves as hex literals — `color: #777581; background: #f8f8fa` — and
`contrast.test.ts` computes pairs of `:root` tokens. Neither colour was a
token, so no row in `TEXT_ON_BACKGROUND` could have covered it, and the guard
was not failing to check it so much as unable to express it. This is the same
shape as the `th` case that file already carries an exception for.

**Fixed**, design-file-first per the discipline in `index.css` and the
precedent of `24694e5`/`12aa11a`: `docs/design/itms-prototype.html:239-254`
first, then `frontend/src/shell/shell.css`. `--muted` `#5f6470` measures
**5.59:1** on `#f8f8fa` and **5.93:1** on the `#fff` the control lifts to on
hover, so it clears the floor in both states rather than the convenient one.

Guarded by two new tests in `frontend/src/test/contrast.test.ts`: one computing
`--muted` against `#f8f8fa` and against `--surface`, and one asserting the rule
paints `var(--muted)` rather than any literal. The second is the load-bearing
one — that file already learned, at the cost of a red test, that a guard
scraping whatever literal a rule holds is grading the rule against itself.
Reverting the CSS turns it red with `expected '#777581' to be 'var(--muted)'`;
verified.

---

## [M2] MEDIUM — the keyboard-shortcut hints measured 3.44:1, and the rule is declared twice · **FIXED**

*1440×900 · `button.topbar-search > kbd.kbd "/"` and `.palette-search > kbd "Esc"`*

```
sweep FAIL 3.44:1 (floor 4.5) fg=#8b8994 bg=#ffffff 12px/700
axe   color-contrast [serious] .palette-search > kbd — 3.44 (fg #8b8994, bg #ffffff, 9.0pt bold)
```

These two glyphs are the whole of how a keyboard user discovers that `/` opens
the palette and `Esc` closes it, so they are text and hold 4.5:1.

**`.kbd` is declared twice** — `shell/components.css:16` and
`shell/shell.css:254` — both transcribed from the same block of the design,
both carrying the same literal. Which one wins is import order. The first fix
attempt changed `shell.css` only and the browser still reported 3.44:1, which
is how the duplicate surfaced. Both now paint `var(--muted)` (**5.93:1** on
`#fff`), the duplication is noted at both sites, and the new guard checks both
files by name for exactly that reason.

---

## [M3] MEDIUM — the progress bar has no accessible value at all, and two of its three fills miss 3:1 against their track · **PARTLY FIXED**

*Every viewport · `.progress` / `.progress > span`*

`Progress` (`shell/components.tsx:325-331`) renders
`<div class="progress"><span style="width:94%"/></div>` — no `role`, no
`aria-valuenow`, no `aria-label`, no text. In the harness it is the entire
contents of the table's **Patch** column, so that cell is empty to a screen
reader and the only expression of "94%" is the pixel width of a bar. WCAG 2.2
SC 1.1.1.

The fills, measured against the `#eceaf0` track they are drawn in (SC 1.4.11,
floor 3:1 — a progress bar's fill/track boundary is the graphical object that
carries the value):

| bar | fill sweep | left end | right end |
|---|---|--:|--:|
| `.progress` (azure) | `#2062d3 → #2e79ee` | 4.69:1 | **3.46:1** |
| `.progress.gold` | `#b46d02 → #d68912` | 3.43:1 | **2.36:1** ✗ |
| `.progress.red` | `#cc384d → #e86071` | 4.15:1 | **2.77:1** ✗ |

`.progress.green` is not rendered by the harness; from its declared literals
(`#148a55 → #39b97a`, components.css:351) its light stop computes **2.10:1**
against the same track — the worst of the four.

The gradients run light-to-right, so the failing end is the leading edge — the
part that says how far along the bar is.

**The bigger half is FIXED; the gradients are still reported.** `Progress` now
renders `role="progressbar"` with `aria-valuenow`, `aria-valuemin`,
`aria-valuemax`, `aria-valuetext` (`"94%"` — some screen readers announce
`aria-valuenow` as a bare "94") and an accessible name. `label` defaults to
`"Progress"` rather than being required, because a bar with no name is an axe
`aria-progressbar-name` failure and a dull name beats a silent control; both
call sites pass a real one. The pixels are unchanged — the unrounded clamped
value still drives the width, only `aria-valuenow` rounds.

`src/shell/progress.test.tsx` is new, and the honest note about it is that
**jsdom could have caught this on any day of the programme**. Nothing here
needs a cascade or a layout. Nothing did, because no test had ever been written
against this primitive at all. That is the gap that was closed, not the
browser's.

The four gradients are unchanged and still fail 1.4.11 at their light ends.
Darkening the second stop of each is a visual-system decision across the whole
palette and wants the same design-file-first treatment [M1] got.

---

## [M4] MEDIUM — a disabled button is indistinguishable from an enabled one, and still says "click me" · **FIXED**

*1440×900 · `.confirm-actions .btn` with `requireTyping` and nothing typed*

```
"Cancel"        disabled=false  opacity=1  color=rgb(17,17,22)   cursor=pointer  border=rgb(130,136,149)
"Delete device" disabled=true   opacity=1  color=rgb(193,45,66)  cursor=pointer  border=rgb(198,43,61)
```

`grep -rn ':disabled|\[disabled\]' frontend/src/shell/*.css frontend/src/components/*.tsx` returns
**nothing**. The shell's `.btn` primitive has no disabled treatment of any
kind: same fill, same border, same text colour, and `cursor: pointer` actively
signalling the opposite of the truth.

This matters most exactly where it was found. The type-to-confirm dialog's
whole design is "you cannot press this until you type the hostname", and the
button gives the operator no signal that it is inert. WCAG 1.4.11 exempts
inactive components from the *contrast* floor — that exemption is already
recorded in `contrast.test.ts` — but it does not license a disabled control
that is visually identical to an active one.

Note for the record: the `disabled:opacity-50` pattern the brief asked about
(`ReportView.tsx:426`, `TicketCopilotPanel.tsx:68`, and three others) exists
only on module pages that `/__preview` does not render, so it was not measured
here. The shell primitive's state is the one above, and it is worse than a 50%
wash: there is no state at all.

**FIXED**, design file first: `itms-prototype.html` carries the rule at the end
of its own `.btn` block and `components.css` follows it.

```css
.btn:disabled { cursor: not-allowed; transform: none; color: var(--muted-2);
                background: var(--surface-2); border-color: var(--line-strong);
                box-shadow: none; }
```

**Last in the block on purpose, and that is the entire mechanism.**
`.btn:disabled`, `.btn.primary`, `.btn.gold`, `.btn.danger` and `.btn:hover`
are all (0,2,0), so source order is what flattens the magenta and gold
gradients and the danger wash to one inert surface and cancels the hover lift —
without an `!important` and without restating each variant. `base-styles.test.ts`
asserts that ordering, because moving the rule up the file would break the fix
in complete silence.

**Not a contrast fix, and it is not listed in `CONTROL_BOUNDARY_RULES`.** The
finding above is right that 1.4.11 exempts inactive components from the 3:1
floor, and `contrast.test.ts` already records that exemption; inventing a ratio
requirement here would be inventing a criterion. What is owed is that the state
be *distinguishable*, and it now differs in four independent ways: the edge
drops from `--line-control` (3.56:1 on white — still findable at 1.41:1,
plainly inert), the label from `--ink` (18.17:1) to `--muted-2` (5.00:1), the
shadow and the hover lift go, and the cursor stops claiming the button can be
pressed.

---

## [M5] MEDIUM — the heading outline is `h1` then `h3`, and no panel is in it

*Every viewport and every state · axe `heading-order` [moderate]*

The only `heading-order` violation on the page, present in all nine
viewport/state combinations that were run. `Panel` renders its title as
`<div className="panel-title">` (`shell/components.tsx:292`), so none of the
page's seven panels is a heading. The document outline is `h1` "Devices"
followed by orphan `h3`s from `EmptyState` (`emptystate.tsx:45`) and the
palette's empty state.

axe tags this `best-practice` rather than a WCAG SC, and it is right to: 1.3.1
does not mandate a heading per section. But a screen-reader user navigating a
console by heading gets one landmark for a page of eight regions.

Promoting `.panel-title` to `<h2>` touches every page in the product and its
CSS. Reported.

**Still reported, and now it is the ONLY thing the harness fails on.** With
[B1], [H1], [H2] and [H3] closed, `scripts/a11y-browser.mjs` exits 1 on six
findings and all six are this one rule at the six viewport/state combinations
it renders in. That is the shape a heading decision should be made in — visible,
counted, and not mixed in with anything else — but it is a decision about every
page in the product, so it is proposed rather than taken.

**The proposal, concretely.** `Panel`'s title becomes an `<h2>` carrying the
same `.panel-title` class, so nothing about the rendering changes: the design
styles it by class, exactly as `Dialog`'s title already does (`ui.tsx` renders
`<h2 className="modal-title">` over the prototype's `div`, and the comment
there gives the same reasoning). The outline then reads `h1` page title → `h2`
per panel → the `h3`s that already exist inside them, which is what a
screen-reader user navigating by heading needs. The risk is not visual, it is
that ~40 pages' worth of tests query panels by text and some may match a
heading role differently; that is a sweep, not a spike, and it should be its
own change with its own green run rather than a rider on this one.

---

## [L1] LOW — chart axis labels render at 5.78px on a phone and 17.51px on a laptop

*`figure.chart > svg.chart-canvas` — `viewBox="0 0 640 260"`, `preserveAspectRatio="xMidYMid meet"`*

| viewport | rendered SVG width | scale | rendered size of a 12px axis label |
|---|--:|--:|--:|
| 1440×900 | 674px | 1.053 | 12.64px |
| 1024×768 | 934px | 1.459 | **17.51px** |
| 768×1024 | 678px | 1.059 | 12.71px |
| 390×844 | 308px | 0.481 | **5.78px** |

The chart scales as an image, so its type scales with it. On a handset every
tick label, category name and axis unit is under 6 CSS px; on a 1024 laptop the
same label is nearly 18px, larger than the body copy beside it. Both charts
ship a full `sr-only` data table, so no information is unreachable — this is
legibility for a sighted low-vision user, not an SC 1.1.1 gap, and browser zoom
does scale it. Reported because "the chart is unreadable on the phone the
operator opens during an incident" is worth a decision.

---

## [L2] LOW — `<summary>` targets are 17px tall under a coarse pointer

*768×1024 and 390×844 · `details.attribute-raw > summary "View raw"`*

Measured `678×17` at 768 and `308×17` at 390. WCAG 2.2 SC 2.5.8 Target Size
(Minimum) is **AA** and asks 24×24 CSS px; the inline exception does not
obviously apply to a disclosure control on its own line.

The `@media (pointer: coarse)` block at `shell.css:585-618` is working and its
reasoning is sound — `.top-icon-btn` measured `39×44`, `.search-field` and
`.menu-item` all clamp to 44 — but `summary` is not in the selector list. The
17 controls under 44×44 at coarse pointer are otherwise all `.btn.small`
(31px), which is the design's deliberate density and is above the 24px AA
floor; `summary` is the one that is under both.

---

## [L3] LOW — one control's focus indicator is 1px of border where every other is a 2px ring

*1440×900 · `.search-field:focus`*

Sixty tab stops were walked at 1440×900. **Every one shows an indicator** —
that check is now in the script and reported zero. But the fields take a
different path from everything else:

```
.btn.primary   outline: 2px solid rgb(30,95,208) offset 2px
               left-edge pixels: -8:#f2eef4  -7:#f2edf3  -6:#f1ebf2  -5:#f2ebf2
                                 -4:#1e5fd0  -3:#1e5fd0   <- the ring, ON THE PAGE
                                 -2:#efe5ee  -1:#f0e4ee  +0:#9c1259  +1:#bd176c
               strongest indicator pixel #1e5fd0 vs --canvas #f4f5f8 = 5.35:1

.search-field  outline: 0px none; box-shadow: rgba(30,95,208,0.1) 0 0 0 3px; border-color: rgb(30,95,208)
               left-edge pixels: -3:#e5ebf8  -2:#e5ebf8  -1:#e5ebf8   <- the 10% glow
                                 +0:#1e5fd0                            <- the 1px border
               glow #e5ebf8 vs page #f4f5f8 = 1.10:1 ; border #1e5fd0 vs page = 5.35:1
```

**The `outline-offset: 2px` claim in `components.css:89-95` is now verified in
pixels**, not just in a comment: the ring is painted at −4 and −3 from the
button's box, on the page, and never touches the magenta fill. `--primary` on
`--action` is 1.01:1, so a ring drawn against the fill would have been
invisible on the button an operator is most likely tabbing to. That reasoning
holds and the geometry matches it.

The field is compliant — 1.4.11 asks 3:1 of the indicator against adjacent
colour and the border gives 5.35:1 — but it is 1px doing what 2px-at-offset
does everywhere else, and the 3px glow beside it contributes 1.10:1, which is
decoration. Unfocused-to-focused the border goes `#828895 → #1e5fd0`, a
**1.64:1** change. Noted, not changed: `.search-field { outline: none }` is a
deliberate transcription and the fix is a design call.

---

# What was measured and holds

Recorded with numbers because these are exactly the claims that were previously
written in CSS comments and never checked against a rendered pixel.

**The primary button's white label on its magenta gradient** —
`linear-gradient(135deg, var(--action), var(--action-ink))`. Sampled across the
fill: `#bc176c → #a1125c`. White measures **6.04:1** on the lightest stop and
**7.62:1** on the darkest. `components.css:100-109` asserts this; it is true.
Its edge `--action-ink` measures **7.28:1** against `--canvas`.

**The active rail item's translucent wash** —
`linear-gradient(90deg, rgba(30,95,208,.34), rgba(30,95,208,.16))` over the
teal rail. Composited: `#132851 → #101a31`. The white label measures
**14.48:1** and **17.32:1** at the two ends.

The wash **alone** does not distinguish selected from resting: measured against
the resting rail ground at the same x it is **1.29:1** at the left end and
**1.14:1** at the right. Selection is carried instead by the 3px inset marker
— sampled at the item's left edge as `#6aa6ff` for exactly three pixels — which
measures **6.76:1** against the composited wash beside it and **7.66:1**
against the resting rail. Plus white text against `--rail-label`. SC 1.4.11 is
satisfied by the marker, not by the tint; worth knowing before anyone
"simplifies" the box-shadow away.

**The nav badges**, `rgba(30,95,208,.42)` and `rgba(209,154,24,.32)` over the
rail. Composited to `#163063` and `#4b3a12`; their text measures **11.20:1**
(`--primary-soft`) and **9.06:1** (`#ffe8a3`).

**The drawer genuinely leaves the tab order below 1024**, which was the
specific question asked:

| viewport | `.sidebar` | rail controls in the first 24 tab stops |
|---|---|--:|
| 1440×900 | `left=0 visibility=visible` | 23 |
| 1024×768 | `transform=translate(-258px) **visibility=hidden**` | **0** |
| 768×1024 | same | **0** |
| 390×844 | same | **0** |

`visibility: hidden` is the correct mechanism — a transform alone would leave
32 links off-screen and tabbable. Opened at 390 it renders correctly
(`position: fixed; z-index: 40; rect = 0,0,258,844`).

**Chart series are distinguishable without hue, and the hatches survive
rendering.** The two adjacent bar fills are `--primary-ink` `#174cab` and
`--blue-ink` `#0a5c6e` — **1.04:1** against each other, which in greyscale is
one colour. The hatch is what carries the distinction, and it is real at every
viewport, measured as the share of pixels inside a bar clearly lighter than its
flat fill:

| viewport | series 0 (declared solid) | series 1 (diagonal hatch) |
|---|--:|--:|
| 1440×900 | 0% | **54.6%** |
| 1024×768 | 0% | 51.3% |
| 768×1024 | 0% | 53.0% |
| 390×844 | 0% | **46.7%** (in a 17×6px bar) |

Each fill measures 7.90:1 and 7.58:1 against the white plot ground. The line
chart distinguishes its three series by `strokeDasharray` as well as colour.

**The chart legend never collides with an axis label** — at 1440, 1024, 768 or
390. It is a separate `<ul class="chart-legend">` below the `<svg>`, not
positioned inside it, so there is nothing to collide with; it stays on one line
at all four widths with a 14px swatch. That was the question the brief asked
about 640px, and the answer is that the architecture makes it unaskable.

**Status pill washes** measure 1.11:1 (`--green-soft`) and 1.14:1
(`--red-soft`) against the white row. Not a finding: every pill carries its
status as a text label, so the wash is not the sole means and 1.4.11 does not
apply to it. The ink on the wash is what matters and `contrast.test.ts` already
computes all seven pairs.

**Every one of 60 tab stops at 1440×900 shows a focus indicator**, and the skip
link is the first of them.

---

# What was changed

| file | change |
|---|---|
| `docs/design/itms-prototype.html` | `.topbar-search` and `.kbd` colour → `var(--muted)`, design-file-first |
| `frontend/src/shell/shell.css` | the same two rules follow the design |
| `frontend/src/shell/components.css` | the duplicate `.kbd` follows too |
| `frontend/src/test/contrast.test.ts` | two guards (+2 tests) and two falsifier rows |
| `frontend/scripts/a11y-browser.mjs` | new — the pass itself, runnable and falsifiable |
| `frontend/package.json` | `axe-core@4.13.0` added as a devDependency |

### The second pass, closing [B1], [H1], [H2], [H3], [M3]'s a11y half and [M4]

| file | change |
|---|---|
| `docs/design/itms-prototype.html` | `.btn:disabled`, design-file-first — the shell had no disabled state of any kind |
| `frontend/src/shell/overlays.css` | `.modal`, `.modal.open`, `.modal-body` transcribed; the header no longer says modal is out of scope |
| `frontend/src/shell/base.css` | `.sr-only` redeclared unlayered, beside the `table` rule that was out-cascading it |
| `frontend/src/shell/components.css` | `.btn:disabled` follows the design, last in the `.btn` block |
| `frontend/src/components/ui.tsx` | `useDialogSurface` — one focus/Escape/trap/lock/restore implementation for three surfaces; the scroll lock is reference-counted |
| `frontend/src/components/confirm.tsx` | uses it; keeps focus-on-Cancel |
| `frontend/src/shell/CommandPalette.tsx` | uses it; `role="dialog"`, named listbox, empty state outside it, no `autoFocus` |
| `frontend/src/components/chart.tsx` | `sr-only` moves from the `<table>` to a wrapper `<div>` |
| `frontend/src/shell/components.tsx` | `Progress` gains `role="progressbar"` and a value |
| `frontend/src/modules/dashboard/DashboardPage.tsx`, `frontend/src/preview/PreviewPage.tsx` | pass a real `label` to `Progress` |
| `frontend/src/test/base-styles.test.ts` | the modal rules, the unlayered `.sr-only`, `.btn:disabled` and its ordering (+11) |
| `frontend/src/components/dialog.test.tsx` | trap, cycling, and the stacked scroll lock (+3) |
| `frontend/src/components/confirm.test.tsx` | trap, the excluded disabled control, focus restore (+3) |
| `frontend/src/shell/overlays.test.tsx` | dialog role, named listbox, empty state placement, Escape from anywhere, trap, restore (+6) |
| `frontend/src/components/chart.test.tsx` | `sr-only` on the wrapper, semantics intact (+1) |
| `frontend/src/shell/progress.test.tsx` | new — the primitive had no test at all (+6) |
| `frontend/scripts/a11y-browser.mjs` | asserts dialog geometry, the Tab trap and focus restore on both overlays; the overflow check names the culprit and reports `innerWidth` |

`npx tsc --noEmit` clean; `npx vitest run` **108 files / 2469 tests passing**
(baseline 107 / 2439, +1 file, +30). `node scripts/a11y-browser.mjs` went from
**16 findings to 6**, and all six are [M5]'s `heading-order` at the six
viewport/state combinations it renders in.

Nothing was wired into `.github/workflows/ci.yml`; that remains the operator's
call, and the costs below are unchanged.

**One dependency was added: `axe-core@4.13.0`**, devDependency, pure JavaScript,
no binaries, no postinstall. Outbound npm install works in this environment.
**Playwright was deliberately not added** — it is present here as a global
install (1.56.1) with Chromium at `/opt/pw-browsers/chromium`, and adding it to
`package.json` would make every `npm ci` in this repo download a browser. The
script resolves it from `node_modules` or from a global path and fails with
instructions if it finds neither.

---

# If this becomes a CI job

**It would cost about 45 seconds of wall clock and one browser download.** The
full run — self-check, then axe plus the pixel sweep at 1440/1024/768/390, plus
the dialog and palette states, plus the tab walk, the drawer tab-order check
and the overflow check — measured **44.8 seconds real** on this machine,
including starting its own Vite dev server. Add `npx playwright install
--with-deps chromium`: roughly 170MB and 30–60s uncached, near-zero with the
runner's cache keyed on the Playwright version.

**What it would have caught, on the evidence above:** [B1], [H1], [H2] and [H3]
are all reproduced by the script today and it exits 1 on them. So is [M1] and
[M2]'s `kbd`. It would not have caught [M3]'s missing `aria-valuenow`, [M4],
[L1] or [L3] — those needed a person deciding what to point it at, which is the
honest boundary of any automated pass.

*Updated after the fixes.* The four it caught are closed and it no longer
reports them, so the script now carries the assertions that keep them closed:
dialog geometry hit-tested against its own scrim, a Tab walk that must stay
inside each overlay, and a focus-restore check on both. Those three are exactly
the ones jsdom cannot express, and one of them — the `autoFocus` ordering race
in [H3] — was found only because the script was pointed at focus rather than at
markup. [M5] is the whole of its current output.

**What it costs to keep.** The sweep is sensitive to rendering: it took three
iterations to stop reporting artefacts, and a Chromium bump could in principle
shift an antialiased edge. The mitigations are already in it — hit-testing,
rounded-box clipping, worst-case-pixel rather than average — and the self-check
fails loudly if the method ever stops being able to see a known 3.49:1 defect,
which is the failure mode that actually matters. A green run that measures
nothing is the outcome to fear, and that is the one thing it is built to make
noisy.

**What it would not replace.** `contrast.test.ts` runs in under a second on
every push and covers every token pair in the product, including the ones the
harness never renders. This pass covers one page, in one engine, with no
assistive technology attached. They are complements, and §11 already says so.
