# The settings console speaks the operator's language

Direction for the settings rework, 2026-09-11. The structural pass (groups,
chips, pickers) shipped and did not solve the problem the operator reported:
*"everything still looks like db tables and too technical."* They are right,
and the cause is one thing, not five.

## The rule

**The console names the decision the operator is making. The system's own
vocabulary never reaches the screen.**

Three kinds of leak, all present today:

1. **Wire values rendered as labels.** `NotificationsTab.tsx:507` prints
   `rule.event_type` — literally `ticket.assigned` — and prints it in `mono`,
   so it reads as a database column twice over. Same at `:558` (`EVENT_TYPES`
   as option text) and `:589` (`queue_members` as option text).
2. **Internal nouns as UI nouns.** `grant`, `relay`, `flap`, `turns`,
   `transcript`, `scope`. Each is the correct word *inside* the system and the
   wrong word on screen. An administrator thinks "who can do what", not
   "a grant at a client scope".
3. **Mechanism where the operator needs consequence.** Health explains what
   the engine does (`clears as unavailable_timeout, which is not a fix`)
   instead of what the number decides.

## One word per concept, everywhere

| Internal | On screen |
|---|---|
| grant, assignment, role assignment | access |
| organization-wide, every-client scope | everywhere (the grant picker's choice reads **Every client**) |
| client scope | limited to *client* |
| relay | mail server |
| event type | what happens |
| recipient kind | who gets told |
| channel | where it goes |
| transcript retention | how long conversations are kept |
| turn | question |
| flap, flapping | repeating |
| suppression | paused |
| remediation | fix |

Never vary these for rhythm. Same concept, same word, every tab.

## Durations stop being seconds

**Corrected 2026-09-11: seven, not eight.** This section first said eight
Health fields take seconds. `alert_retention_days` does not — `sweep.py:673`
builds `timedelta(days=…)` from it and its bounds are 90 to 3650 **days**.
Putting it through a control that stores seconds would have shown a 400-day
retention policy as six minutes and saved it as 400 seconds. The implementer
checked the backend instead of trusting this table, which is the right order;
the count here was arrived at by reading labels ending in "(seconds)" and
assuming the eighth matched.

Seven Health fields take a raw integer in seconds, and
the operator does the arithmetic: `86400`, `3600`, `900`. Replace with a
duration control — a number beside a unit — that **stores seconds unchanged**
and displays the largest whole unit that divides it. `900` shows as
`15 minutes`; `86400` as `1 day`.

The wire format does not change. Only the operator stops converting.

## Exact rewrites

### Health

| Now | Label | Help |
|---|---|---|
| Offline after (seconds) | Mark a device offline after | How long a device can go quiet before we raise an alert. |
| Escalate after (seconds) | Escalate an unanswered alert after | If nobody acknowledges it, the alert escalates once — never twice. |
| Unavailable clears after (seconds) | Give up on an unanswerable check after | The alert closes as "couldn't be checked", which is not the same as fixed. |
| Flap window (seconds) | Look for repeat alerts over | The period repeats are counted in. |
| Flap threshold (opens) | Treat a check as repeating after | *(unit: times)* Past this many alerts in the window, automatic actions pause. The alert itself still appears. |
| Flap cooldown (seconds) | Keep automatic actions paused for | How long actions stay paused once a check is repeating. |
| Approval window (seconds) | Approvals expire after | How long a fix waits for someone to approve it. |
| Alert retention (days) | Delete closed alerts after | Closed alerts are removed after this long. |

Badges: `never configured, running on defaults` → **Using the defaults**.
`alerts cannot open tickets: no default queue or category` → **Alerts can't
raise tickets yet — pick a queue and a category below**.

Drop `agent.offline` and `unavailable_timeout` from all help text. The
operator cannot act on an event name.

### Notifications

Display-name maps, in `frontend/src/modules/notification/labels.ts`, keyed off
the existing `EVENT_TYPES` and `RECIPIENT_KINDS` so an unmapped value is a
type error rather than a raw string on screen:

- `ticket.assigned` → Ticket assigned
- `ticket.status_changed` → Ticket status changed
- `ticket.comment_added` → Public reply added
- `ticket.internal_note_added` → Internal note added
- `ticket.approval_requested` → Approval requested
- `ticket.approval_decided` → Approval decided
- `ticket.resolved` → Ticket resolved
- `queue_members` → Everyone in the queue
- `assignee` → Whoever it's assigned to
- `requester` → The person who raised it
- `channel_default` → The channel's own default

Remove `className="mono"` from the event-type cell. Column `Replace
credential` → **New token**, with its placeholder *Leave blank to keep the
current one*. `Test send` → **Send a test**.

### Assistant

The "what leaves this network" block is security-critical and stays — but it
is eleven bullets above the controls, so it currently buries them. Summarise
it in one line with the detail behind a disclosure: **"Questions and the data
needed to answer them go to your model provider. Passwords, licence keys,
recordings and other organizations' data never do."** Disclosure keeps the
full list verbatim.

The provider warning is addressed to the wrong reader — the org admin seeing
it cannot set an environment variable. Rewrite: **"The assistant is off for
everyone on this server because no model provider is set up. Someone with
server access has to configure one."** Keep the exact variable names
(`ITS_AI_PROVIDERS`, `ITS_AI_<KEY>_KIND`, `_BASE_URL`, `_MODEL`) inside a
disclosure titled *What the server administrator needs to set*.

`Max turns per user per minute` → **Questions per person, per minute**.
`Max turns per organization per hour` → **Questions per organization, per
hour**. `Transcript retention` → **Keep conversations for** (duration, days).
`Daily token budget` → **Daily usage limit**, help *Leave blank for no limit*.

### Users

`org-admin — organization-wide` is two internal facts joined by an em dash.
Render the role as a chip and the scope as its quiet suffix: **Org admin ·
everywhere**, or **Org admin · limited to Warehouse**. `(3 identical grants)`
→ **×3**.

Panel subtitle `A grant is organization-wide unless it names a device group`
→ **Each role applies to every client, or to the one client you choose**
(MSP clients: a grant is scoped to a client, or to every client; device
groups no longer scope grants). Fieldset `Roles to grant` → **Give them
access to**. Field `Role to grant` → **Role**. The scope field is
**Client**, whose first choice is **Every client** -- a choice like any
client, never what an empty box means -- and the button names the scope it
grants: **Assign role on Warehouse**, **Assign role on every client**.

Six equal-weight buttons per row read as a toolbar with no primary. Keep
*Roles* and *Permissions* as the two ordinary buttons; render *Disable*,
*Reset password* and *Resend enrolment* as quiet secondary actions, and
*Delete* as a danger-tinted text action separated from the rest — not a
bordered button competing for the same attention as the others.

### Email

`Relay host` → **Mail server**. `Sign-in username` → **Username**.
`Relay password` → **Password**. `Save email settings` → **Save**.
Encryption chips keep their port hints but lose the hedge: `StartTLS (usually
port 587)` → **StartTLS · port 587**.

**Do not rewrite this one — raise it.** The password help reads *"It is passed
straight to the identity provider and never stored by this platform."* For an
SMTP relay password, "identity provider" appears to be the wrong system
(that is Keycloak's word). Changing it would change a factual claim about
where a credential goes, which is exactly the kind of edit that needs the
operator, not a copy pass.

### Roles

`RolesTab.tsx:185,188` set counts in a monospace face. Numbers in a table want
tabular figures, not a code font. Permission codes at `:256` and
`UsersTab.tsx:608` keep `mono` — those genuinely are identifiers.

## Second surfaces carrying the old words

Three places outside the settings tabs render the same concepts in the old
vocabulary. Each was found by the agent doing the tab beside it, which is the
only reason they are written down:

- `frontend/src/modules/health/HealthSettingsPanel.tsx` duplicates the Health
  fields entirely — old copy, raw seconds, no duration control.
- `frontend/src/modules/ai-admin/AiSettingsPage.tsx` and its `provider.ts`
  still carry the provider hint addressed to the wrong reader, and a sentence
  naming fields that have been renamed.
- `notificationState.ts` and `RulesPanel.tsx` had their own label maps with
  different wording. **This one is already fixed** — they consume `labels.ts`
  now, and the duplicate maps are deleted rather than synced, because two maps
  that agree today disagree the first time an event type is added to one.

The pattern is worth naming: a settings tab is rarely the only surface for its
concept, and fixing the tab alone produces a console that contradicts itself.

## What this pass does not do

The **Connect with Microsoft** button the operator asked for is not a copy
change: it needs the Keycloak 26.2 upgrade and the outgoing-OAuth work already
scoped as A+B in `docs/superpowers/specs/2026-09-10-microsoft-oauth-findings.md`.
Shipping a button that cannot connect anything would be worse than its
absence. It lands in that cycle, which follows this one.
