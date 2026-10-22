# Patch module UI — the decision bar, the device lens, and the compliance panel

Status: built (2026-08-27). Operator report, from the deployed console:

> *"regarding the patch management page a few things to fix, i can now see the
> grouped pending updates per host, but when i open that host i can't approve
> the updates for it, i have to go back and approve 25 updates per page, that's
> not ideal, also two tiles on the bottom that shouldn't be there and don't look
> good and don't have the same theme of the other tiles, in general patch
> management module UI needs a full re-work"*

Refines `view-redesign.md` (2026-08-25), which is **not** superseded. Its three
central decisions are untouched and were the reason this was a refinement rather
than another rebuild: one row per **patch**, every count aggregated in SQL, and
tiles that cannot disagree with the table because they read the same response
objects. No change to `spec.md` §4 (data model), §7 (approvals) or §15
(invariants). **No backend change at all** — every route this uses shipped on
2026-08-25.

---

## 1. Approving from a device, without inventing an approval the database cannot hold

### 1.1 The tension, stated rather than papered over

`patch_approvals` is `(org_id, patch_id, ring_name)`. There is **no device
column**, and `approve_patch` takes no device id. So *"approve the updates for
this host"* is not a thing this system can express, and a console that appeared
to offer it would be offering a guarantee the apply path does not honour: the
approval it wrote would clear those updates for every machine in the ring, and
the screen would have said otherwise.

Three options were on the table and only one is honest:

| | What it would mean | Verdict |
|---|---|---|
| Add `device_id` to `patch_approvals` | A real per-device approval. A schema change, a migration, an apply-path change, and an approval model with two shapes. | **Rejected.** The operator asked for a UI fix, not a new authorisation model, and `spec.md` §7 is settled. |
| Leave the drill-down read-only | The complaint stands. | **Rejected.** It is a fair complaint. |
| **The device is a lens, the ring is the scope** | The operator picks updates *off this machine's list* and approves those patches into a ring. | **Built.** |

The third is also, nearly always, what an operator actually wants: they know
*this* machine needs *these* updates, and clearing them fleet-wide for the ring
is the outcome they were after. What they must not do is believe they scoped it
to the host.

### 1.2 So the blast radius is on screen at the moment of the decision

The drill-down's decision bar carries, before anything is ticked and again in
the confirmation:

> You are choosing these from **srv-dc01**, but an approval covers **all 12
> devices in the test ring** — not srv-dc01 alone.

`deviceLensWarning()` in `patchCopy.ts`, re-rendered whenever the ring select
changes, so the number is the chosen ring's and never the one it opened on.
`patch.test.tsx` pins both placements and pins that the POST carries
`{patch_ids, ring_name}` and **no device id** — the assertion that would fail if
anybody later "fixed" this by inventing one.

### 1.3 What the drill-down became

It was `DeviceUpdates`, a read-only `<ul>` of badge + identifier + title, capped
at the first 25 with *"Open this device for the rest"*. It is now the same shape
as the fleet list: a selection column, `Update / Severity / Also missing on` —
the columns `DevicePatchTab` already uses, so drilling in from the fleet and
opening the machine speak one language — its own paging, its own decision bar,
and a link out to the device for the things patch does not own (reboot state,
job history, inventory).

It is still `GET /patch/updates?device_id=…`, still never severity-filtered, and
still mounted only while the row is expanded — which is also what discards the
selection on collapse, because a tick that survived a collapse would be a
decision the operator can no longer see.

---

## 2. Selecting everything a filter matches, without giving up the guarantee

### 2.1 What the request type already decided

`BulkApprovalCreateRequest.patch_ids` is an explicit list rather than a filter,
and its docstring says why:

> A filter ("approve everything critical") is evaluated on the server AFTER the
> operator has read the screen, so the set that gets cleared is not the set they
> looked at; a list is exactly what they ticked.

That is correct and it holds. The defect is not the request shape — it is that
the **only** way to build the list was ticking 25 rows at a time.

### 2.2 The resolution: resolve the filter in the browser, show the count, send the list

`selection.ts` walks `GET /patch/updates` under the criterion currently on
screen, at the route's own 200-row ceiling (`UPDATE_PAGE_MAX`), and returns an
explicit id list. 500 updates is three requests. The console then states the
exact count — *"300 updates selected of 300 matching."* — before the operator
commits, the confirmation repeats it, and the POST carries the list.

So the property survives: **the set approved is the set they decided on.** The
server never evaluates a criterion of its own, and nothing can change between
the read and the write. What was removed is the twelve page-loads.

Two details are load-bearing:

* **The criterion, not the page.** `resolveMatchingUpdateIds` drops the caller's
  `offset`/`limit`. Resolving "everything matching" from page 3 would silently
  drop pages 1 and 2, which is the same class of defect as a filtered count read
  as a total.
* **Ids are deduped.** The walk is several requests against a table other people
  are writing to; a scan completing mid-walk can move a row across a page
  boundary and hand back the same patch twice. A duplicate is not fatal
  server-side, but it makes the count on screen disagree with the count in the
  request, and this module's whole history is that disagreement.

### 2.3 Past the ceiling: named, not truncated

`BULK_APPROVAL_MAX` is 500 and `approve_patches` refuses above it. Quietly
selecting the first 500 of 812 would commit the operator to a number the screen
never showed them, so:

* the control's own label reads **"Select the first 500 of 812"**, never
  "Select all";
* a sentence beside it does the arithmetic — *"This filter matches 812 updates
  and one approval carries at most 500. Selecting takes the first 500 in the
  order shown; the remaining 312 need a second pass or a narrower filter."*;
* after selecting, a `role="status"` line states *"500 of 812 matching updates
  are selected — the most one approval can carry."*

**The cap was not raised, and the argument for raising it does not hold.** It is
not a performance limit; it is the size of a decision, and 500 rows is already
more than anyone reads. What made 500 unreachable was the 25-row selection, and
that is what changed.

### 2.4 The guard that used to be the intersection

`UpdatesPanel` intersected the selection with the rows on screen on every
render. That was the right guard when a page was the only way to build a
selection — a tick that survived a filter change would let an operator approve
something they could no longer see. It is the wrong guard for a control whose
entire job is selecting rows that are *not* on screen.

The guard moved rather than being dropped. `FleetPatchPage` owns the selection
and **discards it whenever the criterion changes**, keeping it when only the
page changes:

* ticking six rows on page 1 and four on page 2 is one decision, and paging
  keeps it;
* narrowing the severity filter is a different question being asked, and
  carrying an answer to the old one across is how a batch comes to contain rows
  nobody looked at.

The bar states the selection's size and what it was drawn from on every render,
which is the fact the intersection was silently enforcing.

### 2.5 Two selection controls, two clearly different promises

| Control | Selects |
|---|---|
| The header checkbox, *"Select every update on this page"* | The 25 rows rendered. |
| The bar's *"Select all N updates"* | Everything the filter matches, resolved to ids and counted first. |

The header checkbox was named *"Select every update shown"*, which was true when
the page was the whole selection and is ambiguous now.

---

## 3. The decision bar

One component, `DecisionBar.tsx`, used by both the fleet list and every device
drill-down, so the two lenses cannot drift on the sentences, the ring order, the
confirmation or the ceiling arithmetic.

**It is on screen whenever there is something to decide**, which is the change
from the old bulk bar. That bar appeared only once a row was ticked, and three
things were wrong with it: the ring chooser and the blast radius — the two facts
that make the decision readable — were invisible until *after* the operator had
committed to a selection; the table jumped down a row's height on the first
tick; and "select all N matching" had nowhere to live, because it is by
definition reached for *before* selecting anything.

Its idle state carries a real, enabled action rather than a control that spends
its life disabled. `Approve` still renders only once a selection exists, so the
screen still has **exactly one magenta control** (DESIGN.md, Commit Magenta),
and the idle bar's only button is secondary — selecting reveals, it does not
commit (The Two Voices Rule).

---

## 4. The two panels at the bottom

> *"two tiles on the bottom that shouldn't be there and don't look good and
> don't have the same theme of the other tiles"*

Right on all three counts. They were two `<Panel>`s each rendering a bare `<dl>`
of flex rows — label left, percentage right — at the foot of a long page whose
top is four stat tiles. That is what a stat tile looks like once its treatment
has been lost, which is why they read as broken ones.

### 4.1 Deleting them was a real option, and here is what it would have cost

The four tiles count **updates** and **machines**. None of them answers *"Linux
is at 71% while Windows is at 94%"*, which is the question that decides which
platform gets the next maintenance window — and none answers *"we are at 96% on
critical and 84% on important"*, which is the question that decides whether the
ring cadence is working. `GET /patch/report` computes both splits from the same
query the exposure counts come from, so the data costs no extra request.

Nothing else linked to them: `CompliancePanels` was imported by
`FleetPatchPage` and nowhere else, `compliant_pct_by_os_family` /
`compliant_pct_by_severity` are read by no other component, and the CSV export
is a separate route.

### 4.2 So it is one panel, in the console's own language

A percentage in this product is a `Progress` bar — that is what the device
table's Patch column has always used — and a bar cannot be mistaken for a stat
tile. Two labelled groups inside one panel, because the two splits are two
readings of one measurement. Each row is `term / bar / figure`, in tabular
figures, and the bar announces its own value, so the tone, the length and the
digits all say the same thing and none is load-bearing alone (SC 1.4.1).

**Only the rows held to a target are toned**, and the first version got this
wrong: it painted *"low 55.0%"* red. Nobody is chasing 100% on low-severity
updates, so an alarm colour there reports a problem that does not exist —
against DESIGN.md's Rationed Hue Rule, which is that a colour must be carrying
information. The severities that *are* a decision are the two the console's own
filter defaults to, so `DEFAULT_SEVERITIES` decides which rows get a threshold
tone; the rest take the neutral bar, and the group's own hint says so.

### 4.3 The prior audit finding survives the merge

`CompliancePanels.tsx` carried Auditor A's [MEDIUM]: two panels sharing one
glyph and one sentence for their empty state, neither explaining *why* there was
no data. Collapsing two panels into one collapses the duplication that finding
was about. What is still asserted, and still tested: the cause is named ("only
once a scan has matched the update catalog against machines"), the way out is
named, the person who can take it is named when the reader cannot, and the glyph
is `chart` rather than the Devices module's monitor. `QueryState` still owns
loading and failure — its `role="alert"` is the announced read-path error — and
`empty={false}` still says "the empty case is mine". There is still no Scan
button in the card, for the reason recorded there: a fleet-wide scan queued from
mid-page with nothing on screen saying it happened is the click-again loop that
produced the dashboard's duplicate `patch.scan` jobs.

---

## 5. The page's hierarchy

**State → the work → the record.**

| | | |
|---|---|---|
| **Tiles** | what is true right now | four numbers, four destinations |
| **Outstanding updates** | the decision, by update | filters, the decision bar, one row per patch |
| **Devices needing patches** | the same decision, by machine | worst first, expandable into a working list |
| — 28px — | | |
| **Approvals** | the record | what was cleared, by whom, what was withdrawn |
| **Compliance** | the evidence | where the outstanding work is concentrated |

The two working panels sit 16px apart; the break before the record group is
28px. **The break is spacing, not a heading, and that is deliberate**: `Panel`
renders its title as an `<h2>` and lives in `shell/`, so a section heading above
it would have to be a second `<h2>` claiming to be the parent of the ones below
it — a heading-order lie for the sake of a visual rule that twelve extra pixels
state honestly.

Two smaller corrections that belong to the same pass:

* **The device tiles land on the device list, which is on this page.** They
  pointed at `/devices`: the tile says "4 devices needing patches" and the
  destination was every one of the org's 286 machines, four of which were the
  ones it counted. That is a tile/table disagreement one level out from the ones
  this module has already shipped twice. They now carry `#devices-needing-patches`,
  the page scrolls *and moves focus* there (React Router does not scroll to a
  hash, and a keyboard user left in the tile row has been moved on screen and
  nowhere in the tab order), and the panel's summary line states the critical
  count the fourth tile prints — read off the same `GET /patch/report` — so both
  tiles' numbers are findable on the list they land on.
* **The page description says what only the page can say.** It listed the
  module's features, which is a scope statement from the spec. A first rewrite
  repeated the Updates panel's "an approval is about an update and a ring, not a
  device", which is a page telling the reader something twice before they have
  read it once. It now carries the gate: nothing in these lists will ever
  install until somebody approves it.

---

## 6. Severity is a ramp, and only one rung is red

`critical` and `important` both mapped to `destructive`, so the two severities
this page exists to separate — the default filter is literally *"critical &
important"* — rendered as the same red pill in the Updates table, the Worst
severity column and the drill-down. A hue covering half the ramp is not carrying
information.

It was also the only module doing it. `health` maps critical → destructive,
warning → warning, the rest → info; `bcp` ramps critical/high/medium/low onto
destructive/warning/info/muted; `ipam` the same shape. Patch is now on that
ramp: `critical` red, `important`/`high` orange, `moderate`/`medium` azure,
`low`/`informational`/anything unrecognised grey. `unknown` falling through to
grey is deliberate — Windows Update leaves most cumulative updates unrated
(`winupdate.go`), so it is the commonest value on a Windows fleet, and it means
"nobody said".

---

## 7. Verification

`jsdom lays nothing out`, so the visual claims above are browser claims.

* **Browser (Chromium, `scripts/a11y-browser.mjs`)**: `--falsify` first, then the
  full pass. **Exit 0, 0 findings**, at 1440 / 1024 / 768 / 390 / 320px, in the
  default, dialog and palette states. axe (wcag2a/2aa/21a/21aa/22aa) reports no
  violations at any of them.
* **Browser (screenshots)**: the whole page, the updates panel, the expanded
  drill-down, the committed decision bar and the compliance panel, at 1440 and
  390px, before and after.
* **Unit**: `npx vitest run` — 128 files, 2967 tests. `npx tsc --noEmit` clean.
  `npx vite build` clean, and `preview.test.ts` confirms no harness fixture
  reaches `dist/`.

`/patch` is rendered whole in `frontend/src/preview/PreviewPage.tsx`, and the
harness now also mounts `DevicesPanel` with one row **already expanded** and
`DecisionBar` with a selection standing — because a harness that renders a page
renders its resting state, and both of the controls this rework is about live
past a click.

---

## 8. Found and not fixed

* **`FormActions` hard-codes the primary variant, so `Scan` is magenta.** By The
  Two Voices Rule a scan *reveals* and should be secondary; `Approve` is the
  control that changes the fleet. Two magentas can therefore be on screen at
  once — but only while a selection stands, because the bar's idle state has no
  Approve button. The fix is a `variant` prop on `FormActions`
  (`components/form.tsx`), outside this rework's file scope, and forms must use
  the kernel rather than hand-roll a submit button.
* **`Progress` has no neutral-grey tone.** The compliance rows that are not held
  to a target take the azure bar, which is DESIGN.md's primary chart series and
  semantically right ("measured, not judged") but visually louder than the gold
  beside it. A `.progress.muted` rule in `shell/components.css` plus the design
  file would settle it; both are outside this scope, and `tokens.test.ts`
  requires the two to move together.
* **Below ~1024px the device drill-down scrolls sideways with its parent table.**
  `.table-wrap > table` is `min-width: 560px`, so the expansion — including its
  blast-radius sentence — sits inside that scroller and needs a horizontal
  scroll to read at 390px. No WCAG criterion is broken (the page body does not
  scroll; the a11y pass's overflow probe is clean) and the whole table already
  behaved this way, but the most important sentence on the screen should not be
  the one off the right edge. Fixing it means changing `.table-wrap`, in
  `shell/`.
* **Hostname links in the device table are underlined but not azure.** DESIGN.md
  gives links Signal Azure; these render near-black. It predates this pass and
  is a console-wide convention question rather than a patch one.
