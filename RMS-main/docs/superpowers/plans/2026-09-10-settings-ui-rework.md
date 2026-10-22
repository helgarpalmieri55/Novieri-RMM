# Settings UI Rework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the vocabulary `docs/design/itms-prototype-v3.html` already draws for settings into the console's nine settings tabs, so they stop reading as a schema dump.

**Architecture:** Additive kit changes first (`Field` gains a help slot; a new `FieldGroup` heading), then per-tab application. Every kit piece this needs — `Chips`, `Facts`, `EntityPicker`, `.field .help` — already exists; only the help slot and the group heading are new.

**Tech Stack:** React 19 + TypeScript (strict), Vitest + Testing Library, CSS in `src/shell/components.css`, design authority `docs/design/itms-prototype-v3.html`.

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-09-10-settings-ui-rework-design.md`.
- **`Field` help renders OUTSIDE the `<label>`, wired with `aria-describedby`.** `Field` wraps its control inside the label, so text placed there joins the accessible NAME. `/agent-push/authorizations` shipped exactly that defect. Non-negotiable.
- **Design file first.** Any CSS value not already in `docs/design/itms-prototype-v3.html` goes there before `src/shell/components.css`; `src/test/base-styles.test.ts` compares them declaration-for-declaration.
- **Type ramp:** 26, 22, 15, 13.5, 12, 10.5px only. **Radii:** 0, 2, 3, 6, 10, 999px, 50%. **Colours:** `var(--…)` tokens only. A raw hex or off-ramp px fails the harmony gate.
- **`--t-cap` stays 10.5px** — operator ruling 2026-09-09. Do not "fix" small labels.
- **Copy rule:** one short line, no teaching (`docs/superpowers/specs/2026-09-09-console-copy-voice.md`). Help text is one clause, not a lesson.
- **Must not regress:** `EmailTab`'s password semantics (untouched box sends nothing; "Password: set" is the report); `SaveBar` dirty semantics; RBAC read-only gating on every tab; the 640px fold via `FOLDING_FORM_GRID`.
- **Falsify every guard.** Break the fix, confirm the test goes red, restore. A test not seen failing is not a guard.
- **Do not run the full suite or the browser gates** — the controller runs those.

---

## File Structure

| file | responsibility | change |
| --- | --- | --- |
| `frontend/src/components/ui.tsx` | `Field` | **modify** — add `help` |
| `frontend/src/shell/components.tsx` | `FieldGroup` | **create** — icon sub-heading |
| `frontend/src/shell/components.css` | `.field-wrap`, `.field-group` | **modify** |
| `docs/design/itms-prototype-v3.html` | design authority | **modify** — the two new rules |
| `frontend/src/modules/settings/SettingsPage.tsx` | nav state | **modify** |
| `frontend/src/modules/settings/*Tab.tsx` | grouping, help, chips, pickers | **modify** |

---

### Task 1: `Field` gains a help slot that cannot poison the accessible name

**Files:**
- Modify: `frontend/src/components/ui.tsx:719-734`
- Modify: `frontend/src/shell/components.css` (add `.field-wrap`)
- Modify: `docs/design/itms-prototype-v3.html` (add `.field-wrap` first)
- Test: `frontend/src/components/attributes.test.tsx` (or a new `field.test.tsx`)

**Interfaces:**
- Produces: `Field({ label, help?, full?, children })` — `help?: ReactNode`. When `help` is given, the control receives `aria-describedby` pointing at a `<p className="help">` rendered **outside** the `<label>`.

- [ ] **Step 1: Write the failing test**

```tsx
it("keeps help out of the field's accessible name", () => {
  render(
    <Field label="Transcript retention" help="Days">
      <input className="input-field" />
    </Field>,
  );
  const control = screen.getByRole("textbox", { name: "Transcript retention" });
  expect(control).toBeInTheDocument();
  expect(control).toHaveAccessibleDescription("Days");
  // THE DEFECT THIS EXISTS FOR: help inside the <label> joins the NAME.
  expect(control).not.toHaveAccessibleName(/Days/);
});

it("adds no describedby when there is no help", () => {
  render(<Field label="Port"><input className="input-field" /></Field>);
  expect(screen.getByRole("textbox")).not.toHaveAttribute("aria-describedby");
});

it("keeps a describedby the caller already set", () => {
  render(
    <Field label="Port" help="1-65535">
      <input className="input-field" aria-describedby="port-error" />
    </Field>,
  );
  const id = screen.getByRole("textbox").getAttribute("aria-describedby") ?? "";
  expect(id.split(" ")).toContain("port-error");
  expect(id.split(" ").length).toBe(2);
});
```

- [ ] **Step 2: Run it and watch it fail**

Run: `npx vitest run src/components/attributes.test.tsx`
Expected: FAIL — `Field` does not accept `help`.

- [ ] **Step 3: Implement**

```tsx
export function Field({
  label,
  help,
  full = false,
  children,
}: {
  label: string;
  /** One clause, outside the label. See the header comment. */
  help?: ReactNode;
  full?: boolean;
  children: ReactNode;
}) {
  const helpId = useId();
  // HELP IS A DESCRIPTION, NOT A NAME, and this component makes that
  // impossible to get wrong. `Field` wraps its control INSIDE the <label>, so
  // anything rendered there joins the accessible NAME --
  // /agent-push/authorizations shipped a field announcing its whole help
  // sentence as its name. The help sits outside the label and is wired with
  // `aria-describedby`; an existing describedby is preserved, not clobbered.
  const described =
    help && isValidElement(children)
      ? cloneElement(children as ReactElement<{ "aria-describedby"?: string }>, {
          "aria-describedby": [
            (children.props as { "aria-describedby"?: string })["aria-describedby"],
            helpId,
          ]
            .filter(Boolean)
            .join(" "),
        })
      : children;

  const field = (
    <label className={full && !help ? "field full" : "field"}>
      <span>{label}</span>
      {described}
    </label>
  );

  if (!help) return field;
  return (
    <div className={full ? "field-wrap full" : "field-wrap"}>
      {field}
      <p className="help" id={helpId}>
        {help}
      </p>
    </div>
  );
}
```

Add `useId, isValidElement, cloneElement, type ReactElement` to the existing `react` import.

- [ ] **Step 4: Design file first, then the CSS**

In `docs/design/itms-prototype-v3.html`, in the console-additions block, add:

```css
.field-wrap { margin-bottom: 14px; }
.field-wrap > .field { margin-bottom: 4px; }
.field-wrap.full { grid-column: 1 / -1; }
```

Then transcribe the same three rules into `frontend/src/shell/components.css` beside the existing `.field` rules.

- [ ] **Step 5: Run the tests**

Run: `npx vitest run src/components src/test/base-styles.test.ts`
Expected: PASS.

- [ ] **Step 6: Falsify**

Move the `<p className="help">` inside the `<label>`. Expected: the "keeps help out of the accessible name" case goes RED. Restore, confirm green.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/ui.tsx frontend/src/components/attributes.test.tsx \
        frontend/src/shell/components.css docs/design/itms-prototype-v3.html
git commit -m "kit: a field can carry help without renaming itself"
```

---

### Task 2: `FieldGroup`, the icon sub-heading v3 draws

**Files:**
- Modify: `frontend/src/shell/components.tsx`
- Modify: `frontend/src/shell/components.css`, `docs/design/itms-prototype-v3.html`
- Test: `frontend/src/shell/components.test.tsx`

**Interfaces:**
- Consumes: `Icon` from `../shell/icons`.
- Produces: `FieldGroup({ title, icon, children })` — renders `<h3>` + its fields.

- [ ] **Step 1: Write the failing test**

```tsx
it("names the group as a heading under the panel's own", () => {
  render(<FieldGroup title="Connection" icon="plug">{<p>x</p>}</FieldGroup>);
  // h3, because a titled Panel renders the h2 above it.
  expect(screen.getByRole("heading", { name: "Connection", level: 3 })).toBeInTheDocument();
});

it("hides the glyph from the accessible name", () => {
  render(<FieldGroup title="Connection" icon="plug">{<p>x</p>}</FieldGroup>);
  const heading = screen.getByRole("heading", { name: "Connection" });
  expect(heading.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
});
```

- [ ] **Step 2: Run it, watch it fail** — `npx vitest run src/shell/components.test.tsx`

- [ ] **Step 3: Implement**

```tsx
/**
 * v3's settings panel names its sections: `<div style="font-weight:500">
 * <icon> Maintenance window</div>`. A heading here rather than a styled div,
 * because it is what a screen reader navigates by, and `h3` because a titled
 * `Panel` renders the `h2` above it.
 */
export function FieldGroup({
  title,
  icon,
  children,
}: {
  title: string;
  icon: IconProps["name"];
  children: ReactNode;
}) {
  return (
    <section className="field-group">
      <h3>
        <Icon name={icon} size={14} aria-hidden="true" />
        {title}
      </h3>
      {children}
    </section>
  );
}
```

- [ ] **Step 4: Design file first, then CSS**

```css
.field-group { margin-bottom: 22px; }
.field-group > h3 { display: flex; align-items: center; gap: 7px; margin: 0 0 10px; font-size: var(--t-body); font-weight: 500; color: var(--ink); }
.field-group:last-child { margin-bottom: 0; }
```

- [ ] **Step 5: Run** — `npx vitest run src/shell src/test/base-styles.test.ts`. Expected PASS.

- [ ] **Step 6: Falsify** — change `h3` to a `div`. Expected: both cases RED. Restore.

- [ ] **Step 7: Commit**

```bash
git commit -am "kit: a panel's fields can be grouped and named"
```

---

### Task 3: The eleven parenthetical labels become label + help

**Files:**
- Modify: `AssistantTab.tsx`, `HealthTab.tsx`, `BudgetTab.tsx`, `ServiceLevelsTab.tsx`, `NotificationsTab.tsx`
- Test: each tab's existing test file under `frontend/src/modules/settings/__tests__/`

**Interfaces:** Consumes `Field({ help })` from Task 1.

- [ ] **Step 1: Write the failing test** (in the Assistant tab's test; repeat the shape per tab)

```tsx
it("puts units in the description, not in the label", async () => {
  renderSettings();
  const control = await screen.findByRole("spinbutton", { name: "Transcript retention" });
  expect(control).toHaveAccessibleDescription("Days");
});
```

- [ ] **Step 2: Run it, watch it fail** — the current name is `"Transcript retention (days)"`.

- [ ] **Step 3: Apply the mapping**

| file | before | label | help |
| --- | --- | --- | --- |
| Assistant | Transcript retention (days) | Transcript retention | Days |
| Assistant | Max turns per user per minute | *(unchanged)* | — |
| Assistant | Daily token budget (blank for none) | Daily token budget | Blank for none |
| Health | Default alert queue id (blank for none) | *(Task 6)* | — |
| Health | Default ticket category id (blank for none) | *(Task 6)* | — |
| Health | Monitoring deep-link template (blank for none) | Monitoring deep-link template | Blank for none |
| Budget | Base currency (ISO 4217) | Base currency | ISO 4217, e.g. EUR |
| Service levels | Auto-close resolved tickets after (days) | Auto-close resolved tickets after | Days |
| Notifications | Bearer token (optional) | Bearer token | Optional |
| Notifications | SMTP password (optional) | SMTP password | Optional |
| Notifications | Queue id (blank for any queue) | *(Task 6)* | — |

- [ ] **Step 4: Run** — `npx vitest run src/modules/settings`. Fix any test that asserted the old parenthetical name; re-point it at the label plus a description assertion. **Do not** weaken an assertion to make it pass.

- [ ] **Step 5: Falsify** — put one parenthetical back. Expected: that tab's case RED. Restore.

- [ ] **Step 6: Commit**

```bash
git commit -am "settings: units and defaults are descriptions, not part of the name"
```

---

### Task 4: Group each tab's fields under `FieldGroup`

**Files:** Modify `EmailTab.tsx`, `NotificationsTab.tsx`, `HealthTab.tsx`, `AssistantTab.tsx`. **Leave `OrganizationTab`, `ServiceLevelsTab`, `BudgetTab` ungrouped** — one or two fields each; a heading over a single field is furniture.

**Interfaces:** Consumes `FieldGroup` from Task 2.

- [ ] **Step 1: Write the failing test** (Email)

```tsx
it("groups the relay's fields by what they are for", async () => {
  renderSettings();
  for (const [group, field] of [
    ["Connection", "Relay host"],
    ["Sender identity", "From address"],
    ["Authentication", "Sign-in username"],
  ] as const) {
    const heading = await screen.findByRole("heading", { name: group, level: 3 });
    const section = heading.closest("section");
    expect(within(section as HTMLElement).getByLabelText(field)).toBeInTheDocument();
  }
});
```

- [ ] **Step 2: Run it, watch it fail.**

- [ ] **Step 3: Apply the grouping**

| tab | group | icon | fields |
| --- | --- | --- | --- |
| Email | Connection | `network` | Relay host, Port, Encryption |
| Email | Sender identity | `mail` | From address, Sender name, Reply-to address |
| Email | Authentication | `key` | Sign-in username, Relay password |
| Notifications | Channel | `bell` | Kind, Name, URL, Bearer token, SMTP host, SMTP port, From address, SMTP password |
| Notifications | Routing | `git-branch` | Event type, Channel, Recipients, Queue id, Queue scope |
| Health | Timing and thresholds | `adjustments` | the eight bounded integers in `NUMERIC_FIELDS` |
| Health | Alerting | `heartbeat` | Default alert queue, Default ticket category |
| Health | Monitoring | `activity` | Monitoring deep-link template |
| Assistant | Retention | `history` | Transcript retention |
| Assistant | Rate limits | `clock` | Max turns per user per minute, Max turns per organization per hour, Daily token budget |

Each `FieldGroup` wraps the existing `<div className="form-grid" style={FOLDING_FORM_GRID}>`; the grid is **inside** the group, never a grid item.

**A gap in this table, found by Task 4's implementer and fixed here.** The
Health rows originally named three fields; that tab renders **eleven**. The
other eight live in a `NUMERIC_FIELDS` array and were missed because the
inventory that built this table grepped for a literal `<Field label="…">`,
which a mapped array does not match. So the tab most in need of grouping was
getting the least of it. The implementer applied the table verbatim and said
so rather than inventing a group name — the right call, and the reason this
was caught at all.

**Two decisions that come with those eight fields:**

1. **Their labels keep their units in parentheses** — "Offline after
   (seconds)", "Flap threshold (opens)". This is a deliberate exception to the
   rule that emptied the other eleven. That rule exists because a label was the
   only place to put anything; these fields already carry a `help` string, and
   it holds the *meaning* ("No contact for this long opens an agent.offline
   alert"). Prefixing the unit onto that would make it worse, and a unit in
   parentheses on a numeric input is conventional. The rule was never "no
   parentheses"; it was "help does not belong in the name".

2. **They migrate to `Field help=`.** `HealthTab` hand-rolls the help as a
   sibling `<p>` in a wrapper `<div>`, and its header comment at line 20 says
   why: *"The kit's `Field` has no `help` slot yet (reported as a kit gap)"*.
   Task 1 closed that gap, so the comment is now false and the workaround is
   now the long way round. Delete both.

Every name above was verified present in `frontend/src/shell/tabler.ts` on
2026-09-10. **Two of this table's original names did not exist** — `plug` for
Email/Connection and `gauge` for Assistant/Rate limits — and Task 2 hit `plug`
first, which is how they were found. They are corrected above. Verify anyway
before using one: `grep -oE '"[a-z0-9-]+":' src/shell/tabler.ts | tr -d '":' | sort -u`.

- [ ] **Step 4: Run** — `npx vitest run src/modules/settings`. Expected PASS.

- [ ] **Step 5: Falsify** — remove one `FieldGroup`. Expected RED. Restore.

- [ ] **Step 6: Commit**

```bash
git commit -am "settings: fields group by what they are for"
```

---

### Task 5: Small fixed enumerations become chips

**Files:** Modify `EmailTab.tsx` (Encryption), `BudgetTab.tsx` (Fiscal year starts in), `NotificationsTab.tsx` (Queue scope).

**Interfaces:** Consumes `Chips({ items, active, onSelect, label })` from `../../shell/viz` — **index-based**, so map value↔index.

**Do NOT convert:** Kind, Event type, Channel, or anything server-fed or long. The `/security` ruling of 2026-09-08 stands: a growing list is a menu.

- [ ] **Step 1: Write the failing test**

```tsx
it("offers the three TLS choices as a group, not a menu", async () => {
  renderSettings();
  const group = await screen.findByRole("group", { name: "Encryption" });
  for (const name of ["None", "StartTLS", "SSL"]) {
    expect(within(group).getByRole("button", { name })).toBeInTheDocument();
  }
  expect(within(group).getByRole("button", { name: "None" })).toHaveAttribute("aria-pressed", "true");
});
```

- [ ] **Step 2: Run it, watch it fail** — it is a `<select>` today.

- [ ] **Step 3: Implement (Email shown; the other two follow the same shape)**

```tsx
<Field label="Encryption">
  <Chips
    label="Encryption"
    items={ENCRYPTION_CHOICES.map((c) => c.label)}
    active={ENCRYPTION_CHOICES.findIndex((c) => c.value === form.encryption)}
    onSelect={(at) => set("encryption", ENCRYPTION_CHOICES[at].value)}
  />
</Field>
```

If `Chips` has no disabled state and the tab is read-only, keep the `<select>` for that branch rather than rendering a live control to a reader who may not save — and report it as a kit gap.

- [ ] **Step 4: Run** — `npx vitest run src/modules/settings`. Expected PASS.

- [ ] **Step 5: Falsify** — revert Encryption to a `<select>`. Expected RED. Restore.

- [ ] **Step 6: Commit**

```bash
git commit -am "settings: a choice of three is a choice, not a menu"
```

---

### Task 6: The three `*_id` boxes become pickers

**Files:** Modify `HealthTab.tsx` (alert queue, ticket category), `NotificationsTab.tsx` (queue id).

**Interfaces:** Consumes `EntityPicker` from `../../components/picker`. Read its props before writing; match the existing call sites.

- [ ] **Step 1: Write the failing test**

```tsx
it("chooses a queue by name, and keeps an id it cannot resolve", async () => {
  renderSettings({ health: { default_alert_queue_id: "b0000000-0000-0000-0000-00000000dead" } });
  // A SETTINGS PAGE MUST NOT DISCARD A VALUE IT CANNOT RENDER: a deleted
  // queue is a configuration to correct, not a field to silently blank.
  expect(await screen.findByText(/b0000000/)).toBeInTheDocument();
  expect(screen.getByText(/no longer exists/i)).toBeInTheDocument();
});
```

- [ ] **Step 2: Run it, watch it fail** — it is a text box today.

- [ ] **Step 3: Implement.** Replace the `<input>` with `EntityPicker` over the org's queues, storing the id it always stored. When the stored id is not in the list, render the raw id plus "This queue no longer exists" and keep it in the form state, so saving an unrelated field does not clear it. "Queue id (blank for any queue)" becomes **Queue** with an explicit "Any queue" option — blank-means-any is a value, not an absence.

- [ ] **Step 4: Run** — `npx vitest run src/modules/settings`. Expected PASS.

- [ ] **Step 5: Falsify** — make the unresolvable branch clear the value. Expected RED. Restore.

- [ ] **Step 6: Commit**

```bash
git commit -am "settings: choose a queue by name, and never discard an id we cannot render"
```

---

### Task 7: The nav chip reports each tab's state

**Files:** Modify `frontend/src/modules/settings/SettingsPage.tsx`; test in its existing test file.

**Interfaces:** Consumes each tab's existing query. **The nav is already `.chip`** — do not restyle it.

- [ ] **Step 1: Write the failing test**

```tsx
it("says which sections are not set up, before you click them", async () => {
  renderSettings({ email: {} });          // an unconfigured relay
  const tab = await screen.findByRole("tab", { name: /Email/ });
  expect(tab).toHaveAccessibleName(/not set up/i);
  // NOT COLOUR ALONE.
  expect(within(tab).getByText(/not set up/i)).toBeInTheDocument();
});
```

- [ ] **Step 2: Run it, watch it fail.**

- [ ] **Step 3: Implement.** Each nav item takes `state?: "set" | "unset" | "forbidden"`. Render a `<span className="dot">` plus a `--t-cap` text state inside the chip. `forbidden` reads "no access" and is not an error. Where a tab's state is not yet known, render **nothing** — an unknown state must not read as "set".

- [ ] **Step 4: Run** — `npx vitest run src/modules/settings`. Expected PASS.

- [ ] **Step 5: Falsify** — render the dot without the text. Expected: the not-colour-alone case RED. Restore.

- [ ] **Step 6: Commit**

```bash
git commit -am "settings: the section list says where the work is"
```

---

## Self-review

- **Spec coverage.** §1 help slot → Task 1. §2 nav state → Task 7. §3 grouping → Tasks 2, 4. §4 chips → Task 5. §5 pickers → Task 6. The eleven parentheticals → Task 3. The `.facts` consequence block from the evidence table is **deliberately not a task**: v3 draws it for a patch policy, where "would install 212 updates" is computable. No tab in this console has a comparable consequence to compute, and inventing one would be furniture. Recorded here so its absence is a decision.
- **Placeholders.** None: every step carries its code or its exact mapping table.
- **Type consistency.** `Field({ label, help?, full?, children })` (Task 1) is consumed unchanged by Tasks 3–6. `FieldGroup({ title, icon, children })` (Task 2) by Task 4. `Chips` is used at its existing index-based signature.
