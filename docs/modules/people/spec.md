# Module Spec — People (one identity per person, starters and leavers)

> Answers `docs/reviews/2026-09-23-platform-critique.md` §2 finding 5 and
> reviewer-3 §1: a person appeared as a `users` row, a free-text
> `devices.owner`, a free-text seat assignee and a polymorphic licence target,
> so nobody could answer "what does Maria have?" and there was no joiner or
> leaver workflow. Control-plane only: no agent capability, no job type, no
> wire-protocol change. The agent's `console_user` fact already exists
> (`docs/modules/inventory/spec.md` §5).

---

## 0. Decisions

1. **A `person` table, linked optionally to `users`.** Not "a `users` row for
   everyone".
   - `users` is an authentication projection. `external_subject` is NOT NULL
     and unique per org. Every admission rule in `app/api/deps.py` (the domain
     allowlist, `status_refusal`, the email refresh) reads it. So does RBAC
     through `user_roles`, and so do 89 foreign keys.
   - Putting people who never sign in into it means one of two things. We
     could invent a subject for them, which is a row that looks like an
     identity and is not one. Or we could make `external_subject` nullable,
     which makes every one of those paths decide what a null subject means.
     Either change reaches into the kernel's security code.
   - A module-owned `person` table changes nothing that exists. A person may
     have no account (most employees), one account (a technician or portal
     user), or later a directory object (Graph sync, §7).
   - This is the least disruptive option and the only one that keeps "who
     can sign in" and "who works here" as separate facts.
2. **Every existing account gets a person.** The migration creates one person
   per `users` row. Rows that share an address, compared case-insensitively,
   become one person, linked to the oldest account. After that, the People
   list mirrors new accounts idempotently every time it is read (§3.1). So a
   portal user who signs in for the first time appears without a kernel hook
   in `deps.py`.
3. **The device → person link lives in the module's own table
   (`device_person`), not as a new column on `devices`.** It is the
   `devices.primary_person_id` the review asked for, one row per device. It
   also holds the observed sign-in name and where the link came from. Keeping
   it here means the kernel's `devices` table and `devices` API are not forked
   (CLAUDE.md, build discipline 3). The free-text `devices.owner` column is
   left alone.
4. **Seats and licence allocations get a nullable `person_id`.** Those are
   the owning modules' own rows. The free-text `assignee_name`/`assignee_email`
   stay, because they are what the seat was recorded against and a report may
   still want them. Licence allocations gain the target type `person`. The
   existing `user` target keeps working and fills `person_id` in from the
   account's person.
5. **Checklists are records, not automation.** Each step links to the page
   that does the work. It ticks itself when live data shows the work is done,
   and otherwise a person marks it. Nothing on a checklist changes another
   module's data. The destructive leaver actions (turning off a sign-in,
   releasing a seat or licence) are buttons on the Person page. Each one opens
   a confirmation and calls the owning module's existing endpoint, under that
   module's permission and audit event.

## 1. Data model (migration `0115_people`, down_revision `0100`)

All five new tables carry `org_id`, `ENABLE` + `FORCE ROW LEVEL SECURITY` and
the standard `tenant_isolation` policy on `org_id`.

**`person`**: `id`, `org_id`, `display_name` str(200), `email` str(320)
null, `user_id` → `users` null (unique), `status` str(12)
(`starting | active | leaving | left`, CHECK), `department` str(200) null,
`job_title` str(200) null, `start_date` date null, `end_date` date null,
`source` str(12) (`account | operator`, CHECK), `created_by` → `users` null,
`created_at`, `updated_at`. Unique index on `(org_id, lower(email)) WHERE
email IS NOT NULL`: one address, one person.

**`person_login`**: sign-in names that identify this person at a computer.
`id`, `org_id`, `person_id`, `login` str(256) (stored lower-case),
`created_by` null, `created_at`. `UNIQUE (org_id, login)`.

**`device_person`**: `device_id` → `devices` (PK), `org_id`, `person_id` →
`person` null, `source` str(12) (`inventory | operator`, CHECK),
`observed_login` str(256) null, `observed_at` null, `set_by` → `users` null,
`updated_at`.

**`person_checklist`**: `id`, `org_id`, `person_id`, `kind`
(`starter | leaver`), `status` (`open | completed | cancelled`),
`started_by`, `started_at`, `closed_by` null, `closed_at` null. A partial
unique index on `person_id WHERE status = 'open'` allows one open checklist
per person. Nobody is starting and leaving at once.

**`person_checklist_step`**: `id`, `org_id`, `checklist_id`, `key` str(40),
`position`, `state` (`pending | done | skipped`), `note` str(500),
`updated_by` null, `updated_at` null. `UNIQUE (checklist_id, key)`.

**`seat_assignment.person_id`** and **`license_allocation.person_id`**:
nullable FK → `person`, indexed `(org_id, person_id)`.

### 1.1 Backfill (in the same migration, forward only)

1. `person` from `users`. One person per distinct `(org_id, lower(email))`,
   named from `display_name` or, if that is empty, the address. The person
   links to the oldest account with that address.
2. `seat_assignment.person_id`: match `lower(assignee_email)` to
   `lower(person.email)` in the same org.
3. `license_allocation.person_id`: where `target_type = 'user'`, the person
   linked to that account, or else the person holding its address.
4. `device_person`: where `devices.owner` equals a person's address,
   case-insensitively, the link is created with `source = 'operator'`,
   because a person typed it.

Anything unmatched is left exactly as it was. `GET /people/unmatched` lists
it for review (§3). Nothing is guessed from names.

## 2. Primary user from inventory

People registers `people.inventory.post_ingest` into inventory's
post-ingest extension point (`app/modules/inventory/hooks.py`). It registers
in the API app from `register()` and in the gateway from
`register_module_job_types()`, the same way IPAM does, because results
complete in the gateway.

For each ingest with a non-empty `facts.console_user`:

1. Record `observed_login` and `observed_at`.
2. If the row's `source` is `operator`, stop. An override is never replaced.
3. Resolve the login to a person, taking the first rule that gives exactly
   one match:
   - a `person_login` row equal to the lower-cased login;
   - if it contains `@`, a person's email;
   - the part after `\`, compared with the local part of a person's email
     after removing everything that is not a letter or digit. This rule is
     only used when exactly one person matches. It covers `CORP\alice`
     against `alice@corp.com`, and `AzureAD\MariaGarcia` against
     `maria.garcia@corp.com`.
4. Set `person_id` to the match, or to NULL if nobody matched. Someone we do
   not know is using the machine, and it goes on the review list.

An absent `console_user` (nobody signed in) changes nothing. A change of
person writes `people.device_person_detected` (actor `system`). A repeat of
the same answer writes nothing. The hook runs inside inventory's savepoint,
so if it fails, inventory's own writes are unaffected.

**Operator override.** `PUT /people/devices/{device_id}` with `person_id`
(or null) sets `source = 'operator'`. If the device has an observed login,
the override also records that login as a `person_login` for the chosen
person, so that the next machine they use resolves without help.
`DELETE /people/devices/{device_id}/override` returns the device to
automatic and resolves again at once. Both are audited.

## 3. API (`/api/v1/people`, module `app.modules.people`)

| Route | Permission |
|---|---|
| `GET /people?q=&status=&limit=` — list (mirrors new accounts first) | `people.read` |
| `POST /people` — add a person | `people.manage` |
| `GET /people/{id}` — the Person page payload (§4) | `people.read` |
| `PATCH /people/{id}` — name, email, department, title, dates | `people.manage` |
| `PUT /people/{id}/account` — link or unlink a `users` row | `people.manage` |
| `POST /people/{id}/logins`, `DELETE /people/{id}/logins/{login_id}` | `people.manage` |
| `GET /people/by-user/{user_id}` | `people.read` |
| `GET /people/devices/{device_id}` — who uses this computer | `people.read` |
| `PUT /people/devices/{device_id}`, `DELETE …/override` | `people.manage` |
| `GET /people/unmatched` — seats, devices, allocations with no person | `people.read` |
| `POST /people/unmatched/seats/{seat_id}/link` — link a seat, creating the person if asked | `people.manage` |
| `POST /people/{id}/checklists` `{kind}` | `people.manage`; a leaver also needs `people.offboard` |
| `PATCH /people/{id}/checklists/{cid}/steps/{key}` `{state, note}` | as above, by kind |
| `POST /people/{id}/checklists/{cid}/complete` / `cancel` | as above, by kind |

Permissions (migration 0115): `people.read` goes to every staff role
(super-admin, org-admin, technician, auditor, read-only). `people.manage`
goes to super-admin, org-admin and technician. `people.offboard` goes to
super-admin and org-admin only.

Every write is audited under `people.*`. Every query carries an explicit
`org_id` predicate, with RLS as the backstop. An id from another org is
reported as not found. No route here is registered as an AI tool.

### 3.1 Mirroring accounts

`sync_accounts(session, org_id)` runs at the start of `GET /people` and
`GET /people/by-user/{id}`:

- It links an unlinked person whose address matches an account.
- It creates a person for any other account that has none.

It is idempotent and makes no change on a second run. When it does change
something, it writes one `people.accounts_mirrored` event (actor `system`)
with the counts. Doing this on read is deliberate: the alternative is an
edit to the kernel's admission path.

## 4. The Person page

`GET /people/{id}` returns everything the page draws, in one request:

- **Who**: name, email, department, title, status, dates.
- **Sign-in**: the linked account's status, last sign-in, and whether it can
  use the console (holds any role other than `requester`). Role names are
  included only if the caller also holds `users.read`.
- **Computers**: every `device_person` row for the person, with hostname,
  lifecycle, group, how the link was made and the observed sign-in.
- **Licences**: active allocations with `person_id`, plus allocations on
  their computers (marked "through their computer").
- **Subscriptions**: active seats.
- **Tickets**: open tickets they raised (status not closed, cancelled,
  rejected, resolved or fulfilled).
- **Checklist**: the open one, if any, with each step's effective state.

Seats, licences and ticket details are included only if the caller holds
`subscriptions.read`, `licenses.read` or `ticket.read` respectively.
Otherwise the section is `null` and the page says it is hidden, not empty.

## 5. Checklists

The steps are defined in code (`app/modules/people/checklists.py`). A
step's *effective* state is:

- `done` if a person marked it, or if its check passes;
- `skipped` if a person skipped it;
- `pending` otherwise.

The check is computed when the checklist is read and never stored. A step
whose check still fails can be marked done only with a note that says why.
The server enforces this.

**New starter** (`people.manage`). Starting it sets the person to
`starting`; completing it sets them to `active`.

| key | On screen | Ticks itself when | Links to |
|---|---|---|---|
| `account` | Give them a sign-in | a linked account is active | Settings → Users |
| `device` | Give them a computer | a computer is linked to them | Devices |
| `group` | Put their computer in the right group | every linked computer has a group | Device Groups |
| `licences` | Assign their licences | they hold an active licence | Licences |
| `seats` | Add them to the subscriptions they need | they hold an active seat | Subscriptions |
| `baseline` | Install their standard software | never; shows installs that succeeded on their computers | Software Deployment |

**Leaver** (`people.manage` and `people.offboard`). Starting it sets the person to `leaving`;
completing it sets them to `left` and fills `end_date` with today's date if
it is empty.

| key | On screen | Ticks itself when | Links to |
|---|---|---|---|
| `access` | Turn off their sign-in | no account, or the account is not active | Settings → Users |
| `seats` | Take back their subscriptions | no active seats | Subscriptions |
| `licences` | Take back their licences | no active allocations to them | Licences |
| `devices` | Collect or reassign their computers | every linked computer is in stock or retired, or linked to someone else | Devices |
| `credentials` | Change passwords they have seen | every credential they revealed (`credential.checked_out` by their account) was changed or deleted afterwards | Credentials |
| `tickets` | Close their open tickets | none open | Service Desk |

Completing a checklist needs every step done or skipped. Cancelling returns
the person to `active`. Starting, each step change, completing and
cancelling are each audited (`people.checklist_*`).

## 6. Frontend

The feature lives in the `frontend/src/modules/people/` folder.
*Superseded in part on 2026-09-23 — see §9: the People list is gone and "Used
by" is "Assigned to".*

- **People list** (`/people`, nav "People" under Assets & Spend, gated
  `people.read`): search, a status filter, and "Add person". A banner links
  to the review list when anything is unmatched.
- **Person page** (`/people/:personId`): the sections from §4, each with a
  link to the page that owns the data. It has the checklist panel, and
  "Release" and "Turn off sign-in" buttons that confirm before calling the
  owning module.
- **Needs a person** (`/people/unmatched`): seats, computers and licence
  holders with no person, each with "Link to…" or "Add as a new person".
- **Links in from other pages**: the device page shows "Used by", with a
  change control; the ticket page links the requester; the licence page
  links a person holder; the subscription page links a seat holder.

Wording follows `docs/design/settings-language.md`. Screens never show wire
values (`leaver`, `operator`, `console_user`). They say "Leaving", "Set by
you", "Signed in at the computer".

## 7. What this does not do

- It does not sync people from Microsoft 365. The operator runs M365
  without Intune, so Microsoft Graph users and SKUs are the natural next
  source (reviewer-3 improvement 8). `person` has room for an external id
  when that lands. It is not added now, because nothing would write it.
- It does not create Keycloak accounts from a checklist. The step links to
  Settings → Users, which already does, under `users.manage`.
- It does not disable a Microsoft 365 account on leave. That is the M365
  admin centre's job until Graph lands.
- It makes no automatic change to anything owned by another module.

## 8. Tests

- Migration: `upgrade → downgrade → upgrade`, and a backfill test covering
  a case-insensitive seat match, an unmatched seat left alone, a `user`
  allocation linked, and duplicate account addresses becoming one person.
- RLS and the tenant sweeps: all five tables appear in
  `test_rls*.py`/`test_tenant_column_sweep.py` without exceptions.
- The resolver's three rules, including ambiguity leaving it unset and an
  operator override surviving an ingest.
- API: RBAC on every route (read-only gets 403 on writes, technician gets 403
  on leaver routes), a cross-org id returns 404, and each write produces its
  audit event.
- Checklist: the automatic ticks from live data, the note required on a
  forced tick, and completion refused while a step is pending.

## 9. Operator decision (2026-09-23): People folds into Devices

The operator, in their words: "People still makes no sense to me, is that
part of the inventory? if so just keep devices and add a field to set the
owner or holder of said equipment." Beside Settings → Users (the accounts
that sign in to ITMS, unchanged), a second list of humans read as a
duplicate. What they needed from it was one fact per device: who holds it.

So this is a **presentation change plus one additive field**, and it
supersedes the People list and the "Used by" wording in §6:

- **The menu.** "People" is off the rail. `/people` redirects to `/devices`
  (`<Navigate replace>`), so a bookmark still lands. `/people/:personId` stays
  routed but unlisted — licence, subscription and device pages link to a
  holder, and it is still where what they hold and their checklists are shown.
  It and `/people/unmatched` sit under Devices in the crumb. The list page
  component is gone.
- **The device page.** "Used by" becomes **"Assigned to"** and reads as a
  field of the device: "Assigned to: Ana Ruiz · set by hand", or "Assigned to:
  nobody yet · last signed in as TOESHEE\aruiz". Change and Go back to
  automatic are unchanged. The picker gains **"Someone new…"** (name
  required, email optional), which creates the person (`POST /people`) and
  then assigns them; a failed create assigns nothing, and a failed assignment
  retried does not create them twice. The "Add them on the People page first"
  hint is gone.
- **The fleet list.** An **"Assigned to"** column. `DeviceOut` gains an
  additive, nullable `assigned_to: {id, display_name}` — the person the
  device's `device_person` row names, whichever source set it — resolved by
  two outer joins in `device_page`'s one page query (no per-row requests),
  each join carrying `org_id` in its `ON` clause. The `q` search also matches
  the holder's name. `state_counts` and `fleet` keep their meanings; the
  joins are one-to-one and inflate nothing.
- **Who sees a name.** `GET /devices` is gated `devices.read`; a person's name
  is `people.read`. The route resolves `people.read` for the caller and, when
  it is absent, neither sends `assigned_to` nor searches holder names — a
  search that matched would disclose the association the null withholds. The
  console shows the column only to the same callers. Only the list resolves
  it: `GET /devices/{id}` leaves it null, and the device page asks
  `/people/devices/{id}`, which also says who set it.

Unchanged: no RBAC code was renamed or added (reading needs `people.read`,
changing `people.manage`), no table was dropped or migrated, no migration was
added, and every `/api/v1/people` route remains — licences and subscriptions
still link seats to persons.
