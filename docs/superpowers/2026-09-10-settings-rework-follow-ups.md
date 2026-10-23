# Settings rework — deferred work

Recorded 2026-09-10, at the close of the seven-task settings-UI rework
(branch `claude/handoff-spec-review-f45idd`). Each item below was found by a
review on that branch, triaged, and deliberately **not** fixed there. Each
says what was found, the evidence, and why it was deferred — so the next
cycle does not re-derive any of it.

---

## 1. Ten latent `console.error` defects, and a test gate that is scoped narrower than it should be

Task 1 gave the `Field` kit a `help` slot. When the help paragraph cannot
reach its control, `frontend/src/components/ui.tsx` chose *console.error and
degrade* over throwing, arguing that an unwired help paragraph is otherwise
"perfectly invisible". The final review found that **nothing observed the
console**, so it still was: a future call site passing an array, a Fragment,
or a prop-dropping component to `help=` would render a paragraph describing
nothing and leave every test green.

A gate was added — any test where an unexpected `console.error` fires now
fails. Installed globally it turned up **11 failures**. One was in settings
and was fixed at the source (`organization.test.tsx`'s "saves on Enter":
`user.type(…, "{Enter}")` fires the implicit-submit click as a side effect of
user-event's keypress plugin rather than as its own awaited interaction, so a
dialog's focus-stealing effect ran in the same tick and left React's act queue
unflushed; fixed with `await act(async () => { await user.type(…) })`).

The other ten are pre-existing defects in modules the rework never touched, so
the gate was **scoped** to `frontend/src/components/field.test.tsx` and
`frontend/src/modules/settings/**` via vitest `test.projects` in
`frontend/vite.config.ts`, with `src/test/setup.gated.ts` for the gated project
and `src/test/setup.ts` for the default one.

That scoping is a decision, not a fix. The ten:

| Module | Test |
|---|---|
| `dashboard` | points every tile at a route App.tsx serves |
| `bcp` | names the clock the DR test is scheduled in |
| `bcp` | marks the two fields that really are required, and says what the mark means |
| `bcp` | reads a DR test's type and outcome as words, not as wire values |
| `bcp` | offers the test types as words while sending the backend's values |
| `ipam` | renders the last-swept column as an age, and `never` when it has none |
| `ipam` | still reports a measured zero once one subnet exists |
| `ipam` | says the filters are why there is nothing to total, not the fleet |
| `ipam` | (one further failure in this module) |
| `provisioning` | starts the run on Enter, which the modal could not do before it was a form |
| `provisioning` | counts a job that has sat queued for a day as needing attention |

**The work:** fix all ten at the source, then widen the gate to the whole suite
and delete the project split. Never silence one by exempting it or loosening
the gate; where a `console.error` is genuinely expected, spy on it locally so
it is *asserted on* rather than tolerated — `field.test.tsx`'s case "when the
describedby cannot reach the control" is the pattern.

Two traps already paid for once, recorded in `setup.ts`'s own comment:
`vi.spyOn` on an already-mocked property returns the same mock rather than
layering, and two same-level `afterEach` hooks silently drop the second when
the first throws.

**Related, same pass:** the full suite reports a post-teardown unhandled error
— `window is not defined` from a deferred `setTimeout` at
`frontend/src/components/picker.tsx:121`, surfacing via
`remote/StartSessionDialog.test.tsx`. Exit code is still 0, so it fails
nothing, but it is a timer outliving its component and touching globals after
unmount. Clear it on unmount.

---

## 2. `UsersTab` and `RolesTab` were never inventoried

Both have **zero lines changed** on the rework branch, and neither is on the
spec's list of deliberately-exempt tabs. The spec's evidence counts are
undercounts as a result — "eleven field labels carry their help inside
parentheses" and "three `*_id` fields are free-text inputs" both come from an
inventory that never opened `UsersTab.tsx`.

`frontend/src/modules/settings/UsersTab.tsx:516` is a **fourth** raw-id
free-text box — `Device group id (optional)` — with a parenthetical label *and*
a detached `<p className="note">` at `:525-529` doing help's job. Task 3's rule
applies verbatim: label becomes `Device group id`, note becomes
`help="Optional"`. A picker cannot be built for it — there is no device-group
endpoint, and the comment at `:527` says so.

This is the same class of miss as Task 4's Health 3-of-11 gap: an inventory
that enumerated by pattern (`<Field label="…">` with parentheses) could not see
what it did not enumerate. It was caught on Health and not here.

---

## 3. The chips rule is split 2–2 with no stated reason

Spec §4's criterion is "small, fixed and nameable"; the `/security` exclusion
of 2026-09-08 is "long, dynamic or server-fed" — *a growing list is a menu*. As
shipped:

- Encryption, 3 fixed (`EmailTab.tsx`) → chips
- Fiscal year start, 12 fixed (`BudgetTab.tsx`) → chips
- Channel **Kind**, 2 fixed literals (`NotificationsTab.tsx:364`) → `<select>`
- **Recipients**, 4 fixed (`RECIPIENT_KINDS`, `NotificationsTab.tsx:49`) → `<select>`

Kind and Recipients are not long, not dynamic and not server-fed. The plan put
Kind on a do-not-convert list without a reason and never inventoried
Recipients. An operator sees a twelve-item chip row and a two-item dropdown on
the same page. Convert both, or narrow the criterion in the spec — but one rule
should decide all four.

---

## 4. The chips-in-a-field idiom wants extracting

~40 lines are copy-pasted between `EmailTab.tsx:206-249` and
`BudgetTab.tsx:119-167` — near-verbatim comment blocks, the same
`<div className="field"><span>{caption}</span><Chips label={caption}/></div>`,
and the same `editable ? <Chips/> : <Field><select disabled/></Field>` twin. A
third variant sits at `OrganizationTab.tsx:270-291`.

All three carry the same two defects simultaneously:

- **The caption is announced twice** — a visible `<span>` *and* `Chips`'s own
  `aria-label` (`shell/viz.tsx:551`), duplicated rather than referenced.
  `OrganizationTab.tsx:274` already carries an unused `id` on its span, which
  looks like the `aria-labelledby` someone intended.
- **Read-only needs a whole parallel `<select>`** because `Chips` has no
  `disabled` prop.

One `ChipsField` closes all three call sites; a `disabled` prop on `Chips`
removes the parallel branches. For contrast, `FieldGroup` — the abstraction the
rework *did* extract — earned its keep at ten call sites.

While in `Chips` (`shell/viz.tsx:537`): every option is an independent
`<button aria-pressed>` inside a `role="group"`, so assistive tech hears twelve
separate toggles rather than "1 of 12". A roving-tabindex or radiogroup variant
is the same piece of work as the `disabled` prop. Predates the rework and
affects the existing `OrganizationTab` usage.

---

## 5. Two single-field groups break the spec's own furniture rule

Spec §3: *"a heading over a single field is furniture"* — which is why
Organization, Service levels and Budget correctly got no sub-headings. But
`AssistantTab.tsx:198` (Retention, one field) and `HealthTab.tsx:408`
(Monitoring, one field) each grew one anyway.

It shows: `FOLDING_FORM_GRID` is `auto-fit`, so the empty second track
collapses and the lone field draws at the full 560px. "Transcript retention" —
a day count — renders twice as wide as the three rate-limit boxes beneath it.
Health's deep-link template legitimately wants the width; Assistant's does not.

Related per-tab drift: `.field-group:last-child { margin-bottom: 0 }` matches
on Assistant (its Rate limits *is* the last child) but not on Email or Health,
where a note and a checkbox follow the final group. The gap below the last
group is 12px on Assistant, 32px on Email, 36px on Health.

---

## 6. Smaller items, ruled ship-as-is

- `HealthTab.tsx`'s auto-resolve checkbox is the one control on a grouped tab
  left outside every group; its structural twin, Notifications' "Use TLS", is
  inside one. Health's completeness guard documents the exception by name, so
  it is pinned rather than missed.
- `NotificationsTab.tsx:510-513`: the rules table resolves `rule.queue_id`
  against the active-only queue list, so a rule scoped to a since-deactivated
  queue shows a raw UUID. Pre-existing; degrades to an id rather than losing
  data, and there is no edit path that could drop the value (`RuleUpdate` is
  `{active?: boolean | null}` and `queueId` is create-only state reset on
  success).
- `SettingsPage.tsx`'s `sectionState` collapses pending, failed and 403 into
  one `!data` branch. That is the correct answer for all three and the pending
  case is tested; a rejection test would guard the other two.
- `SettingsNav`'s arrow/Home/End keyboard handler has no test anywhere.
  Pre-existing, neither created nor worsened by the rework — but the rework
  changed what those chips render, so it is the natural moment.
- The `useTicketCategories` `includeInactive` query-key split is unguarded
  (dropping both keys fails only the queues test), but categories have exactly
  one consumer, so no collision is currently expressible.

---

## A tooling warning that cost one wrong claim

`frontend/src/modules/settings/types.ts` trips grep's binary-file detection, so
a plain `grep -rn` **silently skips its matches** — no error, no output, just
absence. Use `grep -a` on it. This produced a confidently wrong factual claim
during the rework (that only one type carried a `configured` flag, when three
do), which an implementer caught and corrected.
