# Settings: port the vocabulary v3 already draws

**Operator, 2026-09-10, on the deployed console:** *"all sub menus in settings
need UI re-work, all look like a db table with some style."*

They are right, and the cause is not taste. **The design file already draws a
settings page, and the console dropped almost every device it uses.** This is a
transcription gap, so the fix is a port, not an invention.

## The evidence

`docs/design/itms-prototype-v3.html` line 984, `P['settings']`:

| v3 draws | the console built |
| --- | --- |
| nav as `.chip` links in a padded panel, the selected one filled | **already ported faithfully** — see §2 |
| a panel subtitle carrying live context — *"Production · database. Falls back to the org default where no group matches."* | a static description |
| icon sub-headings inside the panel — *"⊞ Maintenance window"* | nothing; one flat grid |
| `field(label, control, help)` — a **third argument** | `<Field label>`; there is no help slot |
| `chips([...])` for a small enumeration | `<select>` everywhere |
| a `.facts` block of consequences — *"Applies to 34 devices · Would install 212 updates · Would restart 19 devices"* | nothing |

Two measurements make the gap concrete rather than arguable:

1. **Eleven field labels carry their help inside parentheses** — "Transcript
   retention (days)", "Daily token budget (blank for none)", "Base currency
   (ISO 4217)", "Bearer token (optional)". Units, defaults and optionality are
   help text. They are in the label because there is nowhere else to put them.
2. **Three `*_id` fields are free-text inputs**: "Default alert queue id",
   "Default ticket category id", "Queue id (blank for any queue)". A settings
   page that asks an operator to type a UUID *is* a database table with some
   style on it.

## What gets built

### 1. `Field` gains a help slot — and this is the one dangerous change

**THE TRAP, AND IT HAS ALREADY BITTEN THIS PROJECT ONCE.** `Field` wraps its
control inside the `<label>`:

```tsx
<label className="field"><span>{label}</span>{children}</label>
```

Any text placed inside that element joins the field's **accessible name**.
`shell/components.css` defines `.field .help`, which invites exactly that, and
`/agent-push/authorizations` shipped with its "Expires (optional)" field
announcing an entire echo sentence as its name. It was found on 2026-09-09 only
because an added date echo turned an unrelated guard red.

So the help slot renders **outside** the `<label>`, wired with
`aria-describedby`:

```tsx
export function Field({ label, help, full = false, children }: {
  label: string; help?: ReactNode; full?: boolean; children: ReactNode;
}) {
  const id = useId();
  return (
    <div className={full ? "field-wrap full" : "field-wrap"}>
      <label className="field">
        <span>{label}</span>
        {help ? cloneElement(children, { "aria-describedby": id }) : children}
      </label>
      {help ? <p className="help" id={id}>{help}</p> : null}
    </div>
  );
}
```

A test asserts the accessible name of a field WITH help equals its label
exactly — falsified by moving the help back inside the `<label>`.

Then the eleven parenthetical labels lose their parentheses: "Transcript
retention" with help "Days", "Daily token budget" with help "Blank for none".

### 2. The nav reports state

**A correction to this spec, made while planning from it.** Its first draft
said the nav "becomes chips". It already is chips: `SettingsPage.tsx:110`
renders `className={on ? "chip on" : "chip"}` with `NAV_ITEM`, and the comment
there records a measured correction to v3's 34px row pitch. That port was done
more carefully than the draft credited, and none of it changes.

What is missing is state. A nine-tab page where **Email reports "not set up"
only after you click it** makes an operator tour the tabs to find the work.
Each chip gains its state from the query the page already runs.

Not colour alone: a dot AND a text state, so the chip never carries meaning in
hue only.

### 3. Each tab groups its fields under icon sub-headings

Grouped by concern, not by schema order. Email is the worked example:

| group | fields |
| --- | --- |
| Connection | Relay host, Port, Encryption |
| Sender identity | From address, Sender name, Reply-to address |
| Authentication | Sign-in username, Relay password |

Tabs with one or two fields (Organization, Service levels, Budget) get **no**
sub-headings — a heading over a single field is furniture. Notifications, at
thirteen fields across three unrelated concerns, gains the most.

### 4. Small enumerations become chips

Where the choice set is small, fixed and nameable, `chips()` replaces
`<select>`: Encryption (None / StartTLS / SSL), Fiscal year start,
Organization's own per-key closed-vocabulary settings (`date_format` and
kin), and Notifications' channel Kind (webhook / email) and rule Recipients.
A `<select>` stays wherever the list is long, dynamic or server-fed — the
`/security` event-type ruling from 2026-09-08 applies unchanged, and it is
why **Queue scope is not on this list**: it is fed from `queues.data`, a
list that grows as the org creates queues, and is a picker (§5), not chips,
for exactly the reason a growing list is a menu rather than an
enumeration. (An earlier draft of this section named Queue scope as a chips
example; that was wrong the moment §5 gave it a picker instead, and the
inconsistency shipped in both places for a cycle — Kind and Recipients sat
as `<select>`s beside an already-chips Encryption and Fiscal year with no
stated reason, until this correction settled it: convert what the
criterion actually covers, narrow the criterion for nothing that doesn't.)

Twelve is a lot of tab stops for one field (Fiscal year's chip row: every
month is its own focus stop and its own "button" announcement, not "1 of
12"). That is a real cost of `chips()` on an enumeration this size, noted
here rather than resolved: nothing in this rework changes `Chips` beyond
giving it a `disabled` state, and a roving-tabindex or radiogroup variant
for a double-digit enumeration is future work, not a blocker on this
section's ruling.

### 5. The three `*_id` fields become pickers

"Default alert queue id" becomes **Default alert queue** — a picker over the
queues the org has, storing the id it always stored. Same for the ticket
category. "Queue id (blank for any queue)" becomes a picker with an explicit
"Any queue" option, because blank-means-any is a value, not an absence.

If a stored id no longer resolves, the picker says so and keeps the id rather
than silently clearing it: a settings page must not discard a value it cannot
render.

## What must not regress

- **The Email password semantics.** The server never returns it; an untouched
  box sends nothing; "Password: set" is the honest report. `EmailTab`'s header
  comment explains why, and the behaviour is load-bearing — a form that sent
  `password: ""` would blank a working relay on the first port correction, and
  the only symptom would be invitations quietly not arriving.
- **`SaveBar`'s dirty semantics** and the `/profile` vs `/settings`
  `requireDirty` ruling from the review wave.
- **RBAC gating.** Every tab already renders read-only without its manage
  permission, and says which code is missing. Regrouping must not turn a
  disabled control into a hidden one.
- **The 640px fold.** `FOLDING_FORM_GRID` and its reasoning stay; sub-headings
  sit above grids, they do not become grid items.
- **Design-file-first.** `.chip`, `.help`, `.facts` and the sub-heading shape
  all exist in v3 already. Any value that does not gets added to the design
  file before `components.css`, per `base-styles.test.ts`.

## Out of scope

The Microsoft OAuth work (Keycloak 26.2 XOAUTH2 for the outgoing relay, and
delegated sign-in for Email Intake's IMAP). Sequenced after this by the
operator on 2026-09-10; findings recorded in
`docs/superpowers/specs/2026-09-10-microsoft-oauth-findings.md`.

## Testing

Per tab: the accessible name of every field is its label alone; every field
that had a parenthetical keeps that information as a description; the nav chip
reports the tab's state; a picker with an unresolvable stored id keeps the id.
Plus the existing suites for all nine tabs, which pin the RBAC and dirty
behaviour and must pass unchanged.

The browser gates (`a11y`, `harmony`) cover what unit tests cannot: contrast on
the chips in both states, the 640px fold, and no route overflowing.
