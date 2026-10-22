# Module Spec — Odoo purchase sync into Budget actuals

> An extension to the Budget module (`docs/modules/budget/spec.md`), not a new
> module. It builds the feature that spec's §14 deferred — *"pull confirmed
> invoice amounts back; map cost centers to analytic accounts"* — in the
> **read-only direction only**, per the operator's decision of 2026-08-16.
>
> Control-plane only. No agent capability, no job type, no signed-job path, no
> change to the gateway or the wire protocol, and therefore **no agent
> idle-budget impact** — the idle budget is not touched, not claimed, and not
> measured here because nothing in this spec runs on an endpoint.
>
> **Odoo remains the accounting system of record.** Nothing in this design
> writes to Odoo. There is no write path, no `create`, no `write`, no `unlink`,
> and none is designed here even as a pinned contract — see §0.1 for why
> pinning one would be worse than leaving it absent.

---

## 0. Resolved design decisions

Each of these rules out a family of designs, so they are recorded before the
design rather than discovered inside it.

### 0.1 One direction, and the write path is not even pinned

The pull is `search_read` and `fields_get` against an Odoo user that should hold
read-only accounting rights. The connector exposes **no method that mutates
Odoo state**, and `execute_kw` is reached only through a call helper whose
allowed method names are a frozen set (`search_read`, `search_count`,
`fields_get`, `read`) — checked at the call site, not by convention (§2.2).

Change Management pinned an adapter contract it did not implement, and that was
right there: the workflow needed call *sites* fixed so a later adapter would
drop in. Here the opposite holds. A pinned `push_expense` would be a designed,
reviewed, documented path from this platform into an accounting ledger, sitting
unimplemented next to a working connector that already authenticates and
already holds the credential. The distance between "pinned" and "someone wires
it up because the ticket said sync" is one function body. This platform is
domain-admin-equivalent over a fleet; it should not also acquire an unused,
pre-authorised route into the customer's books. When two-way sync is genuinely
wanted, it is a design with an approval model, a conflict policy, and its own
review — not a stub someone fills in.

### 0.2 The connector is shared platform infrastructure, and the change-mgmt Odoo target is already dead

The instruction behind this spec was: share **one** Odoo connector with the one
`docs/modules/change-mgmt/spec.md` pinned. Reading that spec as it stands today,
the connector it pinned **no longer has a consumer**:

> §5, *"Revision, made when Service Desk shipped"*: the Odoo Helpdesk adapter
> target "is superseded — `docs/00-vision.md`'s reversal on owning ticketing
> means Odoo is no longer this platform's ticketing system at all, so an Odoo
> *change-ticket* adapter would integrate with a system this platform is
> actively replacing. The real backend is instead
> `app.modules.service_desk.adapters.ServiceDeskChangeTicketAdapter`."

So there is no live Odoo client in this repo to reuse, and no second one is
being created: **`ITS_CHANGE_TICKET_BACKEND` gains no `odoo` branch, and
`app/modules/change_mgmt/ticketing.py` is not touched by this work.** What
change-mgmt actually left behind is a *shape* — JSON-RPC, vault-sealed API
credential, outbound authenticated client (§7.2, §7.3 there) — and this spec
implements exactly that shape, once, in a place change-mgmt could import if
`00-vision.md` ever reverses again.

**Placement.** The transport lives in **`app/core/odoo/`**, module-neutral, for
the reason `app/core/events.py` states about itself in its own module docstring:

> "This lives in `app/core/` rather than in `app/modules/notification/` because
> Phase 16 publishes to the same facility. A module registers into this; it
> does not own it."

`app/core/ai/`, `app/core/health/` and `app/core/errors/` are the same pattern.
The split is strict:

| Lives in `app/core/odoo/` | Lives in `app/modules/budget/` |
| --- | --- |
| JSON-RPC transport, egress guard, TLS, timeouts, byte bounds | Which Odoo models to read |
| Authentication and credential checkout | What "confirmed" means |
| The `fields_get` compatibility probe (§2.6) | Every field mapping and every business rule |
| The per-org `odoo_connection` row and its CRUD | Everything written to `expense` |
| A frozen set of read-only RPC methods | Reconciliation, the review queue, scheduling |

A reviewer's test for whether a change belongs in core: *would a hypothetical
second consumer need it, and does it encode any opinion about money?* Both
answers must be yes and no respectively.

### 0.3 Vendor bills, not purchase orders

**What is pulled: posted vendor bills and vendor credit notes** (`account.move`
with `move_type` in `in_invoice` / `in_refund`). Purchase orders are **not**
pulled, and that is a decision, not an omission.

A purchase order is a *commitment*, not a cost. The Budget module already has a
column for commitments — `committed` in §7's variance report — and it is already
fed, by materialization over subscription and licence renewals (§6.3 there).
Pulling POs would put a second, uncoordinated source into that column: a SaaS
renewal would appear once as a materialized `expected` row and again as an Odoo
PO, and the two are not correlatable, because the platform's materialized row
carries a `subscription` link and the PO carries an Odoo product. The result is
a doubled commitment figure that no amount of reconciliation logic can safely
un-double, since the two rows are genuinely about the same money and genuinely
have no shared identifier.

Bills have no such conflict: nothing else in the module writes `actual`.

A PO feed is a defensible future feature, but it needs a settlement model
(PO → bill → the same money, counted once) before it can be built. It is
deferred in §14 with that reason attached.

### 0.4 "Confirmed" means `state = 'posted'`, and nothing looser

An Odoo vendor bill in `draft` is a work in progress — it can be edited freely,
its lines deleted, its amounts changed, and it can be deleted outright. A bill
in `cancel` is repudiated. Only `posted` is an assertion by the accounting
function that this cost is real.

The domain filter is therefore `state = 'posted'` **at read time**, and the
reconciliation pass (§5.4) exists precisely because `posted` is not permanent:
Odoo permits resetting a posted move back to draft.

### 0.5 Unmapped spend is queued, never dropped, and never quietly bucketed

A pulled line whose analytic account maps to no cost center is **not** written
as an expense with `cost_center_id = NULL`, and **not** discarded. It is held in
`budget_odoo_purchase_line` with `state = 'unmapped'` and surfaced in a queue.

Both rejected alternatives fail in the same way — silently:

- **Drop it.** The operator's Odoo says the company spent €40k; the platform's
  budget says €31k; nothing anywhere explains the €9k. This is the single worst
  outcome available, because the number *looks* complete.
- **Write it with `cost_center_id = NULL`.** `expense.cost_center_id` is
  nullable and that NULL already means something else: "org-wide spend, not
  attributable to a cost center", a legitimate state a human can choose. Reusing
  it for "we could not work out where this goes" makes the two
  indistinguishable in every report, and the misattributed money silently
  inflates org-wide spend rather than the department that incurred it.

Queuing costs the operator a decision. That is the correct price: the platform
does not know the answer, and the only honest thing a system can do with a
number it cannot place is show it to somebody who can.

**And the queue is disclosed on every aggregate.** The Budget spec already
established this convention for a different unknowable — §3's rule that every
aggregate summing `amount_base` also returns `unconvertible_count` and a
per-currency breakdown, so "a total is never quietly understated". Unmapped
spend understates totals in exactly the same way, so it gets exactly the same
treatment: `GET /budget/budgets/{id}/variance` and
`GET /budget/budgets/{id}/position` gain an `unmapped_spend` disclosure
(`{count, by_currency: {CUR: amount}}`), non-negotiable and additive (§10.3).

### 0.6 A human's confirmation is never overwritten by a machine

When Odoo changes a line the platform already pulled, what happens depends
entirely on whether a human in *this* platform has asserted anything about it:

- The linked expense is still `expected` → the sync updates it in place.
- The linked expense is `confirmed` → **the sync does not touch it.** It records
  a drift, shows old versus new, and waits for a person.

Budget §0.3 is explicit that "a human always confirms, and the confirmed amount
may differ from the projected one", and §5 is explicit that a confirmed row's
edit is an audited, deliberate act. A background task rewriting a confirmed
figure would make every confirmed number in the system provisional — an operator
could no longer point at a report and say "I checked that". §5.3 works this
through case by case.

### 0.7 Currency: the existing `fx_rate` table, and no feed is introduced

Budget §0.1 gave the module a per-org base currency and an **org-maintained**
`fx_rate` table, and §14 deferred an external feed deliberately. This spec adds
no feed and no guessed rate. A foreign-currency Odoo line converts through the
existing `fx.resolve_rate` / `fx.convert` helpers, and where no rate resolves,
the expense is created with `amount_base = NULL` and disclosed as unconvertible
by the machinery that already exists (§6).

Odoo *does* carry `res.currency.rate` rows, and importing them would give the
module a feed almost for free. That is deferred, with its reason recorded in
§14: an `fx_rate` row is an **operator assertion** that history is valued at,
and importing them changes who owns that number without anybody deciding to.

### 0.8 An Odoo line becomes an ordinary `expense` row — no parallel actuals table

The pulled line lands in `expense` with `source = 'odoo'`. It is filtered,
exported, reported on, edited and voided by the code that already exists, and
the dashboards do not learn a new concept.

The alternative — a separate `odoo_actual` table unioned into the reports — was
rejected because *every* aggregate in §7 of the Budget spec would need to learn
about it, and the first one anybody forgot would understate actuals with no
error.

The mirror table (§8.3) holds the Odoo-side facts and the reconciliation state;
it does not hold money that any report reads.

### 0.9 The CSV export and this pull must not both carry the same spend

`GET /budget/expenses/export/odoo` pushes expense rows *out* as CSV for import
into Odoo. This spec pulls vendor bills *in*. Run both over the same spend and
the money makes a loop: platform expense → CSV → Odoo bill → pulled back as a
second platform expense.

**The platform cannot detect this by identity.** The CSV contract (pinned in
Budget §7 and `deploy/README.md`) has no column that survives into Odoo as a
reference the pull can recognise, and adding one would be a change to a pinned
contract *and* would depend on the operator's Odoo import mapping preserving it
— which this platform cannot verify.

So the answer is in three parts, and only the first is load-bearing:

1. **A deployment picks one direction.** Stated in `deploy/README.md` (§13) and
   in the connection admin panel, next to the enable switch.
2. **A heuristic duplicate flag**, explicitly heuristic: a pulled line whose
   `(vendor, amount, currency)` matches a non-`odoo` non-void expense within ±3
   days is queued as `state = 'possible_duplicate'` rather than created. The
   false-positive cost is one click; the false-negative cost is a silently
   doubled cost line. It does **not** make double entry impossible and is not
   described as if it does.
3. **Nothing is auto-suppressed.** A `possible_duplicate` is never dropped — it
   sits in the same queue as unmapped spend and counts toward the same
   disclosure until a human resolves it.

### 0.10 Where an Odoo-sourced expense lands: `confirmed`, by default

This is the one place this spec **narrows a decision already recorded in
`docs/modules/budget/spec.md`**, so it is called out rather than slipped in, and
it is listed in §15 as needing the operator's explicit agreement.

Budget §0.3 says expected entries never auto-confirm, because "auto-confirming
would make the module assert a figure nobody verified". A **posted vendor bill
has been verified** — by the accounting function, in the system that
`docs/modules/budget/spec.md` §1 names as the system of record. The verification
exists; it happened in Odoo, and the mirror row names the exact `account.move`
that carries it.

The technical argument is decisive on top of the principled one. If Odoo lines
landed as `expected`, they would land in the **same** `committed` column as the
module's own materialized subscription and licence rows — so a SaaS
subscription would be committed twice, once by materialization and once by its
own bill, which is precisely the double-count §0.3 rejects PO pulling to avoid.

Therefore: `land_as` is a per-connection setting, `expected` | `confirmed`,
**default `confirmed`**, with `confirmed_by = NULL` and `confirmed_at` = the
sync's run timestamp. `confirmed_by = NULL` is meaningful and is queryable: it
is exactly the set of rows no human in this platform asserted.

**The residual double-count this does not solve, stated plainly.** A materialized
`expected` subscription row for August and the Odoo bill for that same August
subscription are two rows: one in `committed`, one in `actual`. A human
confirming a materialized row converts it in place, so the manual path never had
this problem. The sync therefore emits a **settlement suggestion** — an unresolved
`expected` row for the same category and `period_key` whose amount is within 10%
— shown in the review queue, resolvable in one action (void the expected row) or
dismissable. It is a suggestion. Nothing is voided automatically: voiding a
budget row on a fuzzy amount match is a silent financial rewrite, which is the
class of act this whole spec refuses.

---

## 1. Purpose & boundary

Keep the Budget module's *actuals* current from the ledger that already holds
them, so a category's plan-versus-actual reflects what was really spent without
anybody retyping it — and do so without the platform acquiring any authority
over the books.

**In scope:** the shared Odoo connector and its credentials (§2); what is
pulled and how it maps onto `expense` (§3, §4); idempotency and reconciliation
(§5); currency (§6); scheduling and failure (§7); data model (§8); tenancy,
RBAC and audit (§9); API (§10); frontend (§11); tests (§12).

**Out of scope, hard:** anything that writes to Odoo; payments, aging, tax, or
journal entries; the general ledger; purchase orders (§0.3); Odoo Helpdesk or
any ticket integration (`docs/modules/service-desk/spec.md` §0.1 — this platform
owns ticketing now).

**Boundary vs the Budget module.** This adds no new reporting concept, no new
expense status, and no second definition of an actual. It is a *source*. Every
rule about how money is stored, converted, aggregated and disclosed is
`docs/modules/budget/spec.md`'s, unchanged, and where this spec needed to bend
one (§0.10) it says so and asks.

---

## 2. The connector (`app/core/odoo/`)

### 2.1 What it is

An outbound-only, authenticated JSON-RPC client. Invariant #2 governs agents,
not integrations, but its posture applies unchanged: the platform opens the
connection, nothing listens, and Odoo is never given a way in.

### 2.2 Transport — **UNVERIFIED AGAINST A LIVE INSTANCE**

Everything in this subsection is written from documentation knowledge and
**must be confirmed against the operator's actual Odoo before code depends on
it** (§2.6 is the mechanism that makes that confirmation structural rather than
a promise).

The shape believed correct:

- Endpoint `POST {base_url}/jsonrpc`, `Content-Type: application/json`.
- Envelope `{"jsonrpc": "2.0", "method": "call", "id": <n>, "params": {...}}`.
- Authentication: `params = {"service": "common", "method": "authenticate",
  "args": [db, login, key, {}]}` → an integer `uid`, or `false` on failure.
- Calls: `params = {"service": "object", "method": "execute_kw",
  "args": [db, uid, key, model, method, args_list, kwargs_dict]}`.
- An Odoo error arrives as HTTP 200 with an `"error"` member — **a 200 is not
  success** and the client checks the member, never the status code alone.

**The frozen method set.** `execute_kw`'s `method` argument is checked against
`_READ_METHODS = frozenset({"search_read", "search_count", "fields_get",
"read"})` inside the client, and anything else raises before a request is
built. This is what makes §0.1 a property of the code rather than of the
caller's good intentions.

Bounds, all explicit:

| Bound | Value | Why |
| --- | --- | --- |
| `RPC_CONNECT_TIMEOUT_S` | 10 | matches `egress`'s connect timeout |
| `RPC_DEADLINE_S` | 60 | a **wall-clock deadline on the whole exchange**, not per-phase — `egress.py`'s property 5: per-phase timeouts reset on progress, so a trickling server never trips them |
| `MAX_RESPONSE_BYTES` | 8 MiB | read stops early; the body is never fully buffered and then trimmed |
| `MAX_MOVES_PER_RUN` | 500 | §7.3 |
| `MAX_LINES_PER_MOVE` | 500 | a pathological bill cannot become an unbounded transaction |

### 2.3 Credentials (invariant #3)

The Odoo secret is an **API key** where the instance supports them, falling back
to a password. Both occupy the same argument slot, so the connector does not
branch; the admin UI recommends a key and says why (a key is scoped and
revocable without changing a human's login).

- Stored through the existing `VaultService.store` on a `Credential` row with
  `type = 'odoo_api_key'`. No new vault code, no new envelope format.
- `odoo_connection.credential_id` is **NOT NULL** — there is no unauthenticated
  Odoo path.
- Checked out at point of use inside the worker (or inside the connection-test
  request), held for one run, and never written to the connection row, a log
  line, `last_error`, an audit metadata field, or any API response.
- **Every checkout writes an audit event** — `budget.odoo_credential_used`, in
  the same transaction as the checkout. This is not the vault's behaviour and
  must not be assumed to be: `app/core/vault.py` contains no reference to audit
  at all, `checkout` decrypts and returns. `docs/modules/email-intake/spec.md`
  §3 records this exact correction after the mistake was made there; the caller
  writes the event.

A scheduled sync checking out a credential on every interval is a high-volume
audit path. That is intentional, and it is stated here rather than discovered
later as noise.

### 2.4 Egress: reuse of `app/core/egress.py`, and the honest limits of it

An admin-typed URL that the server then fetches is an SSRF primitive by
construction — the reasoning in `egress.py`'s module docstring applies verbatim.
The connector **must** reuse that module's guarantees, and cannot reuse its
entry point:

- **Reused, mandatory:** resolve every DNS record and reject if any is denied;
  connect to the validated IP and never re-resolve (rebinding); refuse redirects
  outright; the unconditional platform-host denial in both layers — by name
  (`_platform_layer1_denies`) and by resolved address+port
  (`_platform_layer2_denies`). *A deployment must never be able to point the
  Odoo connector at its own vault, gateway, or database, and no allowlist entry
  can grant that.*
- **Not reused:** `post_json` itself. It caps the returned body at
  `_BODY_MAX_BYTES` (64 KiB) and returns only a 500-char excerpt — correct for a
  webhook whose response nobody reads, useless for a JSON-RPC call whose
  response *is* the data. The connector needs its own reader with
  `MAX_RESPONSE_BYTES`, and it must **stop reading early** rather than buffer
  and trim, for the reason `egress.py` property 4 gives.

**This is a core change, and it is the only one.** `egress.py`'s guard
(resolve → check → pin → platform-deny → build transport) is factored into a
reusable helper that `post_json` then calls, with no behaviour change to
`post_json`. Per CLAUDE.md, modules do not modify core security except through
defined extension points; extracting an existing guard so a second caller uses
*the same code* rather than a copy is the least-bad form of that touch, and the
alternative — a second implementation of DNS pinning and platform denial — is
strictly worse. The existing egress tests must stay green unmodified, and the
new path gets its own platform-denial test (§12, R4).

**The private-address question, answered rather than avoided.** `egress._is_denied`
denies private, loopback and link-local addresses unless the `(host, port)` pair
is allowlisted. An on-prem Odoo is almost always on a private address, so
without an allowlist entry the connector could never reach a realistic
deployment. The Odoo connection row **is** its own allowlist entry: the
connector passes `allowlisted_hosts = {(connection.host, connection.port)}`,
derived from the row an admin deliberately created.

Two things follow, and both are stated rather than glossed:

- It does **not** reuse `webhook_allowlist_entry` (notification's table). Those
  two grants mean different things, and making one imply the other would let a
  webhook allowlist entry silently authorise an ERP destination.
- The residual is real: a holder of `odoo.manage` can aim the connector at any
  non-platform host on the LAN and learn something from the failure reason. It
  is bounded — one `POST /jsonrpc`, a bounded body that is never returned, and a
  fixed error vocabulary (§2.5) rather than a raw server response — and it is
  the **same** residual an org-admin already has through notification's webhook
  configuration. It is a property of the admin-tier grant, not a defect
  introduced here.

### 2.5 TLS, and the on-prem self-signed case

- **HTTPS only.** `base_url` must be `https://`; `http://` is rejected at the
  schema boundary with no override flag. The credential goes over this
  connection, and email-intake's rule applies unchanged: there is no code path
  that puts a credential on an unverified channel.
- **Certificate verification is always on.** For the very common on-prem case of
  an internal CA, the connection carries an optional `ca_cert_pem` (a public
  certificate, not a secret, stored in the clear) and verification runs against
  that bundle. There is no `verify=False`, no flag that produces one, and no
  configuration that reaches one.

### 2.6 The compatibility probe — how "I might have the field name wrong" becomes a mechanism

§2.2 and §3.2 are written from documentation knowledge, and Odoo's accounting
schema genuinely changed across recent major versions (§15 names the specific
ones). A spec that guesses a field name and a codebase that then depends on the
guess produces the worst possible failure: a `search_read` that returns zero
rows, a sync that reports success, and actuals that quietly stop updating.

So the field list is **checked, not assumed**:

1. `REQUIRED_FIELDS: dict[str, frozenset[str]]` — model → the fields this
   integration reads — lives in one module-level constant in
   `app/modules/budget/odoo_sync.py`. Nothing reads a field not in it.
2. `fields_get` is called for each model **on the connection test and at the
   start of every sync run** (it is cheap and cached for the run).
3. Any required field absent → the run **refuses** with
   `reason = 'schema_incompatible'`, naming the model and the missing field, and
   writes **nothing**. It does not pull a partial result.
4. The probe's output for the operator's instance is captured into a test
   fixture (`tests/fixtures/odoo_fields_<version>.json`) during implementation,
   and the mapping tests run against that fixture. The first task of the
   implementation plan is to run the probe and record what came back —
   replacing every "unverified" in §3.2 with an observation.

A zero-row sync is thereby distinguishable from a broken one, which is the whole
point.

### 2.7 Per-org configuration and the connection test

One `odoo_connection` row per org (PK is `org_id`; §8.1). `POST
/budget/odoo/connection/test` performs, in order: URL validation → egress guard
→ TLS handshake → `authenticate` → `fields_get` probe → a bounded
`search_count` against the configured company. It returns a **structured
result**, never a raw server response:

`{ok: bool, stage: str, reason: str | None, detail: {...}}` where `stage` ∈
`{url, egress, tls, auth, schema, query}` and `reason` is drawn from a fixed
vocabulary — `dns | timeout | connection-refused | certificate-invalid |
http-error | not-jsonrpc | auth-rejected | db-unknown | company-unknown |
schema-incompatible | rate-limited`. The vocabulary exists for two reasons: an
operator must be able to tell a wrong password from a wrong database name from a
firewall (email-intake §2.1's `last_error` rule), and a raw error body is both
an information leak and an SSRF oracle.

The test is `odoo.manage`-gated, rate-limited per org, and audited.

---

## 3. What is pulled

### 3.1 The domain

Per run, against the configured company:

```
account.move  WHERE  company_id  = <connection.odoo_company_id>
              AND    move_type   IN ('in_invoice', 'in_refund')
              AND    state       = 'posted'
              AND    write_date  >= <cursor - CURSOR_OVERLAP>
              [AND   journal_id  IN <connection.odoo_journal_ids>]   # if set
```

Then, for each move, its `invoice_line_ids`.

**Why the query is driven by the move and not the line.** Three reasons, and the
third is the one that would have caused a bug:

1. A bill is amended as a unit — reset to draft, edited, re-posted — and the
   move's `write_date` moves when it happens.
2. Reading `invoice_line_ids` gets the *product* lines, avoiding the need to
   filter tax and payable counterpart lines out of `line_ids` by
   `display_type` / `exclude_from_invoice_tab`, whose names and values differ
   across versions (§15).
3. **A line deleted during an amendment has no `write_date` to find it by.**
   Only by re-reading the move's *current* line set can the platform notice that
   a line it holds is gone (§5.4). A line-driven query cannot see a deletion at
   all, and would leave a phantom expense on the books forever.

**Multi-company is mandatory, not optional.** `odoo_company_id` is NOT NULL. An
Odoo database routinely holds several legal entities, and a connector that
omitted the filter would import another entity's spend into this org's budget —
a tenancy-shaped failure that this platform's own RLS cannot catch, because it
happens on the other side of the wire.

### 3.2 Field map — **every row unverified; §2.6 is what verifies it**

| Platform field | Odoo source (believed) | Confidence | Note |
| --- | --- | --- | --- |
| — | `account.move.name` | high | bill number; stored on the mirror, used in `expense.invoice_ref` and in the description |
| `expense.expense_date` | `account.move.invoice_date`, falling back to `date` | high | `invoice_date` is the bill date; `date` is the accounting date. The fallback is used when `invoice_date` is false, which happens on some imported moves |
| `expense.amount` | `account.move.line.price_subtotal` | **medium** | **excludes tax** — see below |
| sign | negated when `move_type = 'in_refund'` | **low** | a credit note's `price_subtotal` is believed positive in its own document while reducing cost. §12 R9 gates this against a captured fixture |
| `expense.currency` | `account.move.currency_id` → `res.currency.name` | medium | must be the ISO code; if `name` is not the code on the target instance, this is the field to re-map |
| `expense.vendor_id` | `account.move.partner_id` via §4.4 | high | never auto-created |
| `expense.description` | `account.move.line.name`, prefixed with the move name | high | bounded to the column's width, truncated with an ellipsis |
| `expense.invoice_ref` | `account.move.name` | high | Budget §8: a reference string, **not** a credential |
| cost center | `account.move.line.analytic_distribution` via §4.1 | **low** | **the highest-risk mapping in this spec** — see §4.2 |
| category | §4.3 | n/a | platform-side rules |
| change detection | `account.move.write_date` | high | |

**Tax is excluded, deliberately.** `price_subtotal` is net; `price_total` is
gross. `docs/modules/budget/spec.md` §1 is explicit — "no tax handling" — and
recoverable VAT is not a cost to the business, so a tax-inclusive actual would
overstate spend against a plan built ex-tax. This is listed in §15 because an
org in a jurisdiction with non-recoverable tax may want the opposite, and it is
a one-field change made deliberately rather than discovered.

### 3.3 The cursor

`odoo_connection.cursor_write_date` (nullable; NULL means "never synced —
perform the initial pull described below").

- The query uses `write_date >= cursor - CURSOR_OVERLAP` where
  `CURSOR_OVERLAP = 1 hour`, absorbing clock skew between Odoo's server and this
  one. Re-reading a move is free: §5.2's content hash makes it a no-op.
- The cursor advances to the `write_date` of the **last fully-processed move**,
  never past a move the run did not finish. A run truncated by
  `MAX_MOVES_PER_RUN` simply continues next time — the same posture as
  email-intake's "the remainder is still `UNSEEN`; there is no cursor to keep".
- **The first run is bounded by a date, not by everything.**
  `odoo_connection.initial_from_date` is NOT NULL and set by the admin at
  configuration time. Without it, the first sync of a ten-year-old Odoo pulls a
  decade of bills into a budget module that has one fiscal year defined, and
  every one of them lands in the unmapped queue.

---

## 4. Mapping

### 4.1 Cost center ↔ analytic account, through the `external_ref` that already exists

`docs/modules/budget/spec.md` §0.2 decided this in advance:

> Cost centers are free-standing, not modelled on Odoo analytic accounts. They
> carry a nullable `external_ref` string from day one so a later Odoo mapping is
> a data change, not a migration.

That decision holds and is used exactly as written. `cost_center.external_ref`
holds the **Odoo `account.analytic.account` id as a decimal string**, and the
resolution is a lookup on `(org_id, external_ref)`.

**One migration is nevertheless required, and the reason is a correctness bug
waiting to happen.** `external_ref` today has no uniqueness constraint, so two
cost centers in one org can carry the same value, and the mapping would then be
ambiguous — resolvable only by picking one, which silently attributes a
department's spend to another department. Migration adds:

- `UNIQUE (org_id, external_ref) WHERE external_ref IS NOT NULL` — a partial
  index, because NULL is the common case and Postgres treats NULLs as distinct
  anyway. This is the same NULL-distinctness reasoning the Budget spec applies
  to `budget_line`'s two partial indexes; the trap is identical.

The id was chosen over the analytic account's `code` or `name` because ids are
stable and codes are edited. The admin UI shows the account's name next to the
ref (fetched live, not stored) so an operator can see what they mapped.

### 4.2 `analytic_distribution` — the split-line problem, and the highest-risk assumption here

**Believed, unverified, version-dependent:** in Odoo 16 and later,
`account.move.line.analytic_distribution` is a JSON field shaped
`{"<analytic_account_id>": <percentage>}`, replacing the pre-16 many2one
`analytic_account_id`. Odoo 17 further generalises analytic *plans*, so a line
may carry distributions across more than one plan.

Consequences the design must handle regardless of which form the instance uses:

- **One Odoo line can produce several platform expense rows** — one per analytic
  account it distributes to. The mirror table's natural key is therefore
  `(connection, model, odoo_id, analytic_ref)`, not `(connection, model,
  odoo_id)` (§5.1).
- **The split must reconcile to the penny.** Parts are allocated by
  **largest-remainder**: each part is `floor(amount × share)` to two decimals,
  and the remaining cents are handed to the parts with the largest fractional
  remainders, ties broken by ascending `analytic_ref` so the result is
  deterministic. The parts sum to the line total exactly, for any share vector.
  §12 R10 gates it.
- **Base-currency parts may not sum to `convert(total)`.** Each part is converted
  independently through `fx.convert`, which rounds once at the end of each
  conversion, so the sum of `amount_base` across n parts can differ from
  converting the whole line by up to n−1 cents. This is stated rather than
  hidden. Converting the total and then allocating in base was rejected: the
  document-currency amounts are what the ERP asserts, and those must be exact.
- **A line with no distribution at all** → `state = 'unmapped'`,
  `reason = 'no_analytic'`.
- **A distribution naming an account no cost center claims** → `state =
  'unmapped'`, `reason = 'unknown_analytic'`, with the analytic id recorded so
  the queue can offer "map this account".
- **A distribution whose percentages do not sum to 100** (Odoo permits partial
  distribution) → the unallocated remainder becomes its own `unmapped` row with
  `reason = 'partial_analytic'`. The allocated part is **not** scaled up to
  100%: that would invent a cost attribution nobody entered.

If the target instance turns out to use the pre-16 many2one, the same design
holds with a single distribution at 100% — which is why the split model is the
general case rather than a special one.

### 4.3 Category

`expense.category_id` is NOT NULL, and an Odoo bill line does not carry a
platform category. Resolution, in order:

1. `budget_odoo_category_rule` — ordered rules matching on
   `match_type ∈ {product_category, account_code}` against
   `account.move.line.product_id.categ_id` or the line's account code, mapping
   to an `expense_category`. First match wins; order is explicit
   (`sort_order`), not incidental.
2. `odoo_connection.default_category_id` — **NOT NULL**, exactly as
   `email_mailbox.default_category_id` is NOT NULL and for the identical reason:
   the destination column is NOT NULL, the source carries nothing, so without a
   configured default *no line could ever become an expense* and the feature is
   unreachable.

A category that has been deactivated or deleted since configuration makes the
run refuse with `reason = 'category_missing'` naming it — the same
fail-loudly-never-guess rule `feeds.materialize` applies to `system_key`
resolution in Budget §6.1. It does not fall back.

**Category never blocks; cost center does.** The asymmetry is deliberate and
worth stating: a wrong category misfiles a cost between two lines of the same
budget, and is trivially corrected in the expense editor that already exists. A
wrong cost center attributes one department's money to another. So category has
a default and cost center has a queue.

### 4.4 Vendor, and why the sync does not create one

`budget_odoo_vendor_map` maps `(org_id, connection_id, odoo_partner_id)` →
`vendor_id`. An unmapped partner is **not** blocking: the expense is created
with `vendor_id = NULL` and the Odoo partner's name preserved in the mirror row
and in the expense description, and the queue offers a mapping action that
back-fills every expense already created from that partner.

**Vendors are never auto-created.** `vendor` is the Licenses module's shared
registry (Budget §Preamble: "reuses the shared `vendor` entity from Licenses"),
and a sync that creates rows in it would populate another module's registry
from an external source with no dedupe — "Microsoft", "Microsoft Ltd" and
"MICROSOFT IRELAND OPERATIONS" as three vendors, breaking spend-by-vendor for
Licenses and Subscriptions as well as Budget. Writing into a registry this
module does not own is exactly what "modules register into the kernel; they do
not fork it" is about, one level down.

### 4.5 The review queue is a view, not a table

`state` on `budget_odoo_purchase_line` is the queue. There is no separate queue
table, deliberately: two tables holding "what needs attention" drift, and the
one that drifts is always the one the operator is looking at.

`state` vocabulary (verbatim, and the frontend's badge map is keyed on exactly
these — Budget §11's rule about never inventing vocabulary):

| `state` | Meaning | Expense row? |
| --- | --- | --- |
| `linked` | mapped and written; Odoo and platform agree | yes |
| `unmapped` | cannot be placed (§4.2 reasons) | no |
| `possible_duplicate` | §0.9's heuristic flag | no |
| `drifted` | Odoo changed under a **confirmed** expense (§5.3) | yes, untouched |
| `voided_upstream` | cancelled or vanished in Odoo; the `expected` expense was voided | yes, voided |
| `dismissed` | a human said "do not import this" | no |

`reason` is a bounded string from a fixed vocabulary
(`no_analytic | unknown_analytic | partial_analytic | unmapped_currency |
duplicate_suspected | amount_changed | date_changed | line_deleted |
move_cancelled | move_vanished | dismissed_by_user`), never `str(exc)`.

---

## 5. Idempotency and reconciliation

### 5.1 The natural key, and why `expense`'s existing guard cannot carry it

Budget §2 has an idempotency guard for auto-fed rows:

```
UNIQUE (org_id, source, link_id, period_key) WHERE source <> 'manual'
```

**It cannot express the Odoo key**, for a concrete reason: `expense.link_id` is
a `UUID` column, and an Odoo record id is an integer. There is no honest
encoding of one into the other, and inventing a deterministic UUID from an Odoo
id would produce a column whose values look like platform ids and are not.

So the key lives in the mirror table:

```
UNIQUE (org_id, connection_id, odoo_model, odoo_id, analytic_ref)
```

on `budget_odoo_purchase_line`, with `expense_id` as a nullable FK to the row it
produced. The mirror row *is* the link; there is no separate link table.

**Odoo-sourced expenses set `link_id = NULL` and `link_type = NULL`, and that is
load-bearing.** With `link_id` NULL, Postgres's NULL-distinctness means the
existing partial unique index cannot collide two Odoo rows in the same period —
which is what we want, since their real uniqueness is enforced above. If a later
change ever set `link_id` on Odoo rows, two bill lines in one month would
collide and the second would be silently rejected. §12 R6 pins this with a test
that fails if `link_id` is populated.

`period_key` **is** set (`YYYY-MM` of `expense_date`), for consistency with the
auto-feed convention and because the settlement suggestion (§0.10) matches on
it.

### 5.2 Change detection: a content hash, not a timestamp

`write_date` moves for reasons that change nothing the platform reads (a
follower added, a note edited). So each mirror row stores
`content_hash = sha256` over the canonical tuple the platform actually mapped:
`(move_state, invoice_date, amount, currency, partner_id, analytic_ref,
analytic_share, description)`.

- Hash unchanged → no-op. No expense write, no audit event, no counter but
  `unchanged`.
- Hash changed → §5.3.

This is what makes the cursor overlap free and makes a manual re-sync safe. §12
R1 gates it: running a sync twice over identical Odoo data must produce exactly
one expense and write no second audit event.

### 5.3 An invoice amended in Odoo after it was pulled

The case the operator asked about directly. The rule is one sentence: **the sync
updates what no human has asserted, and reports what one has.**

| Mirror `state` | Linked expense status | What the sync does |
| --- | --- | --- |
| `linked` | `expected` | **Updates in place** — amount, date, currency, vendor, description; re-runs FX conversion against the new `expense_date`; audits `budget.expense_updated` with **changed field names only, never values** (Budget §8's Phase-12 convention); mirror stays `linked` |
| `linked` | `confirmed` | **Touches nothing.** Mirror → `drifted`, `reason` names what changed, and the mirror row carries both the previously-mapped values and the new ones for side-by-side display. Counted in the queue. |
| `linked` | `void` | Mirror → `dismissed` with `reason = 'dismissed_by_user'`. A human voided this deliberately (Budget §5: voiding "permanently suppresses re-materialization"), and the same rule applies here — Odoo changing its mind does not resurrect a row a person killed. |
| `unmapped` / `possible_duplicate` | none | The queued row is refreshed to the new values. Nothing to reconcile. |

A `drifted` row is resolved by a human, two ways, both audited:

- **Accept** (`expense.confirm` — it edits a confirmed amount, which is exactly
  what that permission governs): the expense is updated to the Odoo values, FX
  re-runs, `confirmed_at` is refreshed, mirror → `linked`.
- **Dismiss** (`expense.confirm`): the expense stands as confirmed, the mirror
  records the divergence and returns to `linked` with the *current* content
  hash — so the same drift is not re-raised every hour, while a *further*
  change in Odoo is.

That last detail matters more than it looks. Re-raising an accepted divergence
on every run turns the queue into noise, and a noisy queue is an ignored queue.

### 5.4 Cancelled, deleted, or vanished in Odoo

Three distinct upstream events, one rule:

- **`state` moved to `cancel`** — seen by the write_date cursor, since cancelling
  posts a write.
- **A line was deleted during an amendment** — seen because the move is re-read
  in full (§3.1) and the line is absent from the current `invoice_line_ids`.
- **The whole move vanished** (reset to draft, then deleted) — **not** seen by
  the cursor, because a deleted record has no `write_date`. This is why the run
  includes an **existence re-check**: `search_read` on the ids of moves the
  mirror holds whose `expense_date` falls within the re-check window
  (`RECHECK_WINDOW_DAYS`, default 120 — long enough to cover a quarter-end
  adjustment), bounded by `MAX_RECHECK_MOVES` per run, oldest-checked first so
  the window is covered over successive runs.

  Without this pass, a bill deleted in Odoo stays on the platform's books
  forever, and the sync reports success every hour while doing so.

And then the same asymmetry as §5.3:

| Linked expense status | What the sync does |
| --- | --- |
| `expected` | **Voids it** — `voided_at` stamped, `voided_by = NULL` (no human did this), audited `budget.expense_voided` with `actor_type = "system"`. Mirror → `voided_upstream`. |
| `confirmed` | **Does not void it.** Mirror → `drifted`, `reason = 'move_cancelled'` or `'move_vanished'`. A confirmed cost is a figure somebody in this platform asserted and may already have reported; retracting it silently is the failure this whole section exists to prevent. The queue says: *Odoo no longer has this bill; your platform still counts it — decide.* |

### 5.5 Credit notes are rows, not edits

A vendor credit note (`in_refund`) is pulled as its **own** expense with a
negative `amount`, linked to its own mirror row. It does not modify the expense
of the bill it credits.

This matches how the ledger itself treats it, keeps the audit trail intact
(the original cost happened; the credit happened; both are true), and avoids
needing to resolve `reversed_entry_id` chains that may point at moves outside
the sync window or outside the configured company.

Negative amounts are already permissible: Budget's money field is
`Field(max_digits=14, decimal_places=2)` with **no** `gt=0` (unlike `rate`,
which has one), and `expense` carries no check constraint on `amount`. So no
schema change is needed — but the aggregates must be checked to behave, which
§12 R11 does.

---

## 6. Currency

No FX feed is added (§0.7). The rules are Budget §3's, applied:

- `expense.currency` is the Odoo move's currency ISO code.
- Where it equals the org's base currency, `fx_rate = 1` and no lookup occurs.
- Otherwise `fx.resolve_rate(session, org_id, currency, expense_date)` — the
  greatest `effective_date <= expense_date`. **Never extrapolated backwards.
  Never inverted from the opposite pair.** Both prohibitions are Budget §3's and
  neither is relaxed here.
- **No rate resolves** → the expense is still created, with `fx_rate = NULL` and
  `amount_base = NULL`, and it is counted in the run's
  `created_unconvertible` and disclosed by the existing `unconvertible_count`
  machinery on every aggregate. The row is *not* queued as unmapped: it is
  correctly placed, merely unconvertible, and those are different problems with
  different fixes (add a rate versus map an account).
- A currency code Odoo returns that is not a 3-letter ISO code → the line is
  queued `unmapped`, `reason = 'unmapped_currency'`, rather than written with a
  garbage code.

Conversion is captured at write time and never recomputed on read (Budget §3).
Adding a corrective rate later does not re-value a row the sync already wrote,
and the sync never re-values one either — a re-conversion is a human's edit.

---

## 7. Scheduling and failure

### 7.1 The beat entry

One entry in `app/worker.py`'s `beat_schedule`:

```
"budget-odoo-sync-dispatch": {
    "task": "app.worker.budget_odoo_sync_dispatch",
    "schedule": odoo_sync_module.SYNC_DISPATCH_INTERVAL_S,   # 3600.0
}
```

The interval is a **module constant read from the module**, not a literal in
`worker.py` — the convention the health and IPAM entries already follow, because
a literal there drifts from the spec with nothing noticing.

Hourly is a *dueness check*, not a sync: it asks which connections are due, the
same way `ipam-sweep-dispatch` does. Per-connection cadence is
`sync_interval_minutes`, **default 1440** (daily), **minimum 60**. Daily is the
right default because bills post on a business rhythm, not a machine one, and an
hourly poll of an ERP buys latency nobody asked for at the cost of 24× the audit
volume on the credential-checkout path (§2.3).

### 7.2 Claiming

`SELECT … FOR UPDATE SKIP LOCKED` over due connections, stamping `claimed_at`,
cleared in a `finally` on the **failure** path as well as the success path — the
precedent, and the bug it prevents, are both recorded in
`docs/modules/email-intake/spec.md` (Amendment A4). Stuck claims are released by
the same sweep that covers job dispatch and notification delivery
(`STUCK_POLL_CLAIM_AFTER`, ten minutes).

Per-org failure is **contained**: one org's unreachable Odoo must not stop the
others' syncs. `app/worker.py`'s existing `_OrgCycleFailures.contain` is the
mechanism, unchanged.

### 7.3 Bounds

`MAX_MOVES_PER_RUN` (500) and `MAX_RECHECK_MOVES` (500). A run that hits either
cap is **not** a failure: it records `truncated = true` on the sync run, does not
advance the cursor past what it finished, and the next run continues. The
operator sees "truncated" in the run list, which is how a first-enrolment
backlog is diagnosed rather than mistaken for a stall.

### 7.4 What the operator sees when a sync fails

Failure surfaces in four places, and the design assumption is that an operator
looks at exactly one of them:

1. **`odoo_connection.last_error` / `last_error_at` / `last_success_at` /
   `consecutive_failures`** — `last_error` is the bounded, sanitized
   `"<stage> failed: <reason>"` of §2.7's vocabulary, never `str(exc)`, never a
   raw server response, never anything derived from the credential.
2. **A `budget_odoo_sync_run` row for every attempt**, including failed ones,
   with counts, `truncated`, `stage`, `reason` and duration. A run that fails
   at authentication still gets a row — an integration whose failures leave no
   trace is one whose failures are invisible.
3. **A domain event**, `budget.odoo_sync_failed`, published through the kernel
   outbox (`app/core/events.py`) on the transition from healthy to failing and
   again at `consecutive_failures` crossing 3 — **not on every failure**, which
   would page somebody hourly for one down ERP. It joins `EVENT_TYPES` and
   `STAFF_ONLY_EVENT_TYPES` (it has no ticket and no external requester;
   the IPAM events' precedent applies verbatim). Delivery is the notification
   module's job; this spec ships no mail and no webhook.
4. **A banner on the Budget dashboard** while `last_success_at` is older than
   `3 × sync_interval_minutes`, because the specific danger of a silently dead
   sync is that the numbers still *look* fine.

**The connection is never auto-disabled.** A disabled sync stops being a source
of financial truth while continuing to look configured, and nothing in the UI
would distinguish "off because it broke three weeks ago" from "off on purpose".
It keeps retrying and keeps saying it is failing.

---

## 8. Data model and migration

One new revision, the next in sequence after the head at implementation time
(`0058_created_at_defaults` as of writing — confirm the head, do not assume it).
All new tables are org-scoped and get RLS `ENABLE + FORCE` plus the
`tenant_isolation` policy (`USING (org_id = current_setting('app.current_org',
true)::uuid)` with a matching `WITH CHECK`), exactly as every prior module, with
a **behavioural** cross-org test per table (§12 R18).

### 8.1 `odoo_connection` — shared, in `app/core/odoo/`

`id (uuid7 PK), org_id (FK organizations), base_url (str 255), odoo_db (str 128),
username (str 320), credential_id (uuid FK credentials, NOT NULL),
odoo_company_id (int, NOT NULL), odoo_journal_ids (int[], default '{}'),
ca_cert_pem (text | NULL), active (bool, default false),
created_by, created_at, updated_by, updated_at`.

Plus the budget-consumer columns, which live here because the connection is the
unit an operator configures and splitting them across two tables buys nothing:
`sync_interval_minutes (int, default 1440)`, `initial_from_date (date, NOT
NULL)`, `land_as (str 10, default 'confirmed')`,
`default_category_id (uuid FK expense_category, NOT NULL)`,
`cursor_write_date (tz-aware | NULL)`, `claimed_at (tz-aware | NULL)`,
`last_success_at`, `last_error (str 200 | NULL)`, `last_error_at`,
`consecutive_failures (int, default 0)`.

`UNIQUE (org_id)` — one connection per org in v1. It is a **surrogate key plus a
uniqueness constraint** rather than `org_id` as the primary key, deliberately:
the three tables below carry a real `connection_id` FK that means something, and
relaxing to several connections per org later is dropping one constraint and
adding a resolution rule, not re-keying four tables and every row in them.

`active` defaults to **false**: a connection is created, tested, and then turned
on. A row that starts syncing the moment it is saved would run its initial pull
before anyone had checked the mapping.

*If a second consumer of `odoo_connection` ever appears, the six budget-specific
columns move to a `budget_odoo_settings` table keyed on `connection_id`. That is a
forward migration and a small one; doing it now, for a consumer that does not
exist, would be speculative.*

### 8.2 `budget_odoo_sync_run`

`id (uuid7 PK), org_id, connection_id (FK odoo_connection),
trigger (str 10: 'schedule' | 'manual'), actor_id (uuid FK users | NULL),
started_at, finished_at (| NULL), status (str 10: 'running' | 'ok' | 'failed'),
stage (str 16 | NULL), reason (str 64 | NULL), truncated (bool, default false),
moves_read (int), lines_read (int), created (int), updated (int),
unchanged (int), queued_unmapped (int), queued_duplicate (int),
drifted (int), voided_upstream (int), created_unconvertible (int)`.

Index `(org_id, started_at DESC)`.

The counters are separate rather than a JSON blob so the run list can be sorted
and filtered on them without parsing. Twelve counters is a lot; each one answers
a question an operator actually asks when a number looks wrong.

### 8.3 `budget_odoo_purchase_line` — the mirror, the link, and the queue

`id (uuid7 PK), org_id, connection_id, odoo_model (str 64),
odoo_id (bigint), odoo_move_id (bigint), odoo_move_name (str 128),
analytic_ref (str 64, NOT NULL, '' when none),
analytic_share (Numeric(9,6) | NULL),
odoo_write_date (tz-aware), content_hash (str 64),
state (str 20), reason (str 32 | NULL),
expense_id (uuid FK expense | NULL),
line_date (date), amount (Numeric(14,2)), currency (str 3),
partner_name (str 255 | NULL), odoo_partner_id (bigint | NULL),
description (str 500), prior_values (JSONB | NULL),
first_seen_at, last_seen_at, resolved_by (uuid FK users | NULL), resolved_at`.

- `UNIQUE (org_id, connection_id, odoo_model, odoo_id, analytic_ref)` — the
  natural key of §5.1. `analytic_ref` is `''` rather than NULL for
  no-distribution **precisely because Postgres treats NULLs as distinct**: with
  NULL, one un-distributed line could be inserted twice and the constraint would
  not stop it. This is the same trap Budget §2 documents for `budget_line`, and
  it is avoided here by making the column NOT NULL rather than by adding a
  second partial index.
- Index `(org_id, state)` — the queue query.
- Index `(org_id, connection_id, odoo_move_id)` — the per-move re-read of §3.1
  and §5.4.
- `prior_values` holds the previously-mapped tuple for a `drifted` row, so the
  UI can show old versus new without a second RPC. It contains money, so it is
  never included in an audit metadata field (Budget §8: field names, never
  values).

### 8.4 `budget_odoo_category_rule`

`id, org_id, connection_id, match_type (str 20), match_value (str 128),
category_id (FK expense_category), sort_order (int, default 0), created_by,
created_at`. Unique `(org_id, connection_id, match_type, match_value)`.

### 8.5 `budget_odoo_vendor_map`

`id, org_id, connection_id, odoo_partner_id (bigint), vendor_id (FK vendor),
created_by, created_at`. Unique `(org_id, connection_id, odoo_partner_id)`.

Two small tables instead of one polymorphic `odoo_mapping` with a `kind` column,
because each keeps a **real foreign key** to its target. A polymorphic target
would trade FK integrity for one fewer table, and this repo has already paid for
app-level polymorphic resolution once (`change_link.action_id`, whose spec notes
the absence of a DB-level FK as a compromise, not a preference).

### 8.6 Two alterations to existing tables

Both are forward-only, additive in effect, and neither edits a shipped
migration.

1. **`cost_center`** gains
   `UNIQUE (org_id, external_ref) WHERE external_ref IS NOT NULL` — §4.1's
   ambiguity fix. *This can fail on an existing deployment that has already
   entered duplicate refs.* The migration therefore checks first and raises a
   named error listing the conflicting rows, rather than dying on an index
   build. Practically the risk is near zero — `external_ref` has had no
   consumer until now — but "practically zero" is not a reason to skip the
   check.
2. **`expense.entered_by`** drops NOT NULL, with a check constraint added in the
   same revision:
   `CHECK (entered_by IS NOT NULL OR source = 'odoo')`.

   An Odoo-sourced expense has no human enterer, and the alternatives were both
   worse. Attributing it to the operator who configured the connection makes
   `entered_by` a lie in the one column whose entire job is to say who entered
   the row. Inventing a system user row adds a principal to the identity model
   for one integration's convenience. NULL says the true thing — *no person in
   this platform entered this* — and the check constraint means it can only ever
   say it for this one source, so no manual row can lose its enterer to a bug.

   Budget §8's precedent is exact: for the lazily-seeded categories, the audit
   event's actor is system while "`created_by` on the seeded rows still records
   the acting user". Here there is no acting user at all, and the column is
   allowed to admit it.

`test_schema_drift.py` compares `Base.metadata` to the live schema and must pass
with all of the above.

---

## 9. Security invariants

### #3 Secrets

Covered in §2.3. Restated as obligations a reviewer can check: the Odoo key
exists in the database only as vault ciphertext; it is decrypted at point of use
and held for one run; it appears in **no** API response, **no** log line, **no**
`last_error`, and **no** audit metadata; every checkout writes
`budget.odoo_credential_used`. `ca_cert_pem` is a public certificate and is
deliberately *not* vault-held — treating a public cert as a secret trains people
to treat the vault as a place to put things rather than a place for secrets.

### #4 Audit (hash-chained)

Written for: connection create / update / delete / test; credential checkout;
sync run start and finish (one event each, carrying the counters — not one event
per row, which would put thousands of entries in the chain per run); every
expense create, update and void the sync performs; every queue resolution (map,
dismiss, accept-drift, dismiss-drift); every mapping-rule and vendor-map
mutation.

- `target_type` ∈ `{odoo_connection, budget_odoo_sync_run,
  budget_odoo_purchase_line, expense, cost_center, vendor}`.
- `actor_type = "system"`, `actor_id = NULL` for anything a scheduled run did.
  `actor_type = "user"` for a manual sync, a connection change, or a queue
  resolution.
- **Expense audit metadata carries changed field names only, never values** —
  Budget §8's Phase-12 convention, and this path is the one most likely to
  violate it by accident, since the sync has both old and new amounts in hand.
- **No audit on view.** The queue, the run list and the connection status write
  nothing. Budget's `budget.export` events are unaffected.

### #5 Tenancy

RLS `ENABLE + FORCE` on all five new tables, plus an explicit `org_id` predicate
in every query (never relying on RLS alone). Every cross-reference —
`credential_id`, `default_category_id`, `category_id`, `vendor_id`,
`cost_center_id`, `expense_id`, `connection_id` — is resolved `WHERE org_id AND
id`, so another org's id is indistinguishable from a nonexistent one.

**The tenancy boundary this platform cannot enforce is on the other side of the
wire**, and it is named rather than assumed: a connection configured with the
wrong `odoo_company_id` imports another legal entity's spend, and no RLS policy
can see that. The mitigation is that `odoo_company_id` is required, is shown
with its resolved company **name** in the admin UI (fetched live at test time),
and is part of the audited connection record.

### #6 RBAC

**One new permission**, `odoo.manage` — module-neutral, matching the connector's
placement in `app/core/odoo/`:

> `odoo.manage` — "Configure, test, and manually run the Odoo connection.
> **A holder can cause expense rows to be created.**"

The description says so for the same reason `notification.admin`'s does: a grant
whose real reach is indirect must announce it. Seeded to **super-admin and
org-admin only**; technician none; auditor none; read-only none. Both seeded
holders already hold all four Budget permissions, so the seeded grants create no
new reach — a *custom* role granted `odoo.manage` alone would gain indirect
expense creation, which is why it is stated in the description rather than in a
comment.

Everything else reuses Budget's existing four, and the reuse is exact rather
than convenient:

| Action | Permission | Why that one |
| --- | --- | --- |
| View connection status, runs, queue | `budget.read` | these are budget surfaces |
| Configure / test / delete connection, manual sync | `odoo.manage` | admin-tier, holds the credential |
| Map an unmapped line, dismiss a line, map a vendor, edit category rules | `expense.record` | it creates or shapes expense rows — exactly what `expense.record` governs |
| Accept or dismiss a drift on a **confirmed** expense | `expense.confirm` | it changes a confirmed amount, which is exactly what `expense.confirm` governs |

`PERMISSIONS` in `app/core/rbac.py` gains `odoo.manage`, and the perm-set tests
(`test_rbac`, `test_api_routes`) are updated **in the same task** that adds the
routes — the Phase-7 lesson, restated in Budget §8.

### #1 / #2

No agent surface: no job type, no gateway registration, no capability, no
signed-job path, no wire-protocol schema. The agent idle budget is untouched and
nothing about it is claimed here.

### Errors and bounds

Typed `BudgetValidationError` / `BudgetNotFoundError`, mapped to 400/404 **by
isinstance, subclass first, never by message string, never 500** (Budget §8, the
Phase-9 lesson). Schema-boundary bounds on everything crossing it: every string
`max_length`, `sync_interval_minutes` bounded 60–43200 on **both** ends,
`odoo_company_id` and Odoo ids bounded to positive int64, `initial_from_date`
bounded to a sane range, money `max_digits=14, decimal_places=2`. Nothing reaches
Postgres as an out-of-range `DataError`.

**Untrusted input from Odoo.** Every string the connector reads is bounded and
truncated at the mapping boundary before it reaches a column, and it is treated
as untrusted: a partner name or a line description is attacker-controllable by
anyone who can create a bill in the customer's Odoo. It is stored as text,
rendered as text, never as markup, and never interpolated into a
`Content-Disposition` or any other header.

---

## 10. API

Under `/api/v1/budget/odoo`, literal segments before `{id}` parameters, and
declared **before** any `/budget/{id}`-shaped route (Budget §9's route-ordering
caution).

| Method | Path | Permission |
| --- | --- | --- |
| GET | `/budget/odoo/connection` | `budget.read` |
| PUT | `/budget/odoo/connection` | `odoo.manage` |
| DELETE | `/budget/odoo/connection` | `odoo.manage` |
| POST | `/budget/odoo/connection/test` | `odoo.manage` |
| POST | `/budget/odoo/sync` | `odoo.manage` |
| GET | `/budget/odoo/runs` | `budget.read` |
| GET | `/budget/odoo/runs/{id}` | `budget.read` |
| GET | `/budget/odoo/lines` | `budget.read` |
| POST | `/budget/odoo/lines/{id}/map` | `expense.record` |
| POST | `/budget/odoo/lines/{id}/dismiss` | `expense.record` |
| POST | `/budget/odoo/lines/{id}/drift/accept` | `expense.confirm` |
| POST | `/budget/odoo/lines/{id}/drift/dismiss` | `expense.confirm` |
| GET/PUT | `/budget/odoo/category-rules` | `budget.read` / `expense.record` |
| GET/PUT | `/budget/odoo/vendor-map` | `budget.read` / `expense.record` |

`org_id` and `actor_id` come from `AuthContext`, never from a body or a path.

### 10.1 What the connection response does and does not contain

Returned: `base_url`, `odoo_db`, `username`, `odoo_company_id`, journal ids,
`active`, `land_as`, cadence, cursor, `last_success_at`, `last_error`,
`consecutive_failures`, and a `has_credential: bool`.

Never returned: the key, `credential_id` (an internal identifier of a vault
row), `ca_cert_pem`'s private counterpart (there is none — it is a public cert
and is returned), or any raw server response.

### 10.2 `POST /budget/odoo/sync`

Rate-limited per org (one in-flight run, enforced by the same claim as the
scheduler — a manual run competes for `claimed_at` and returns 409 if a
scheduled run holds it). Returns the `budget_odoo_sync_run` id; the run itself
is queued to the worker rather than executed in the request, because an HTTP
request must never hold an ERP conversation open.

### 10.3 The two additive report fields

`GET /budget/budgets/{id}/variance` and `GET /budget/budgets/{id}/position` each
gain `unmapped_spend: {count: int, by_currency: {str: str}}` — §0.5. Additive,
at the end of the response model, alongside the existing `unconvertible_count`
disclosure it is modelled on. This is the only change this spec makes to an
existing endpoint's response, and it exists because the alternative is a total
that is quietly wrong.

The pinned CSV column contract of Budget §7 is **not** changed: future columns
there are additive at the end of the row, and this spec adds none.

---

## 11. Frontend (`frontend/src/modules/budget/`)

Added to the existing feature folder — not a new module folder.

- **`OdooConnectionPanel`** — configure, test (showing the staged result of
  §2.7 verbatim, so a failing stage is legible), enable. `odoo.manage`-gated;
  the controls are **hidden** without it, and the gating is presentational only
  — the server is the authority (Budget §11).
- **`OdooReviewQueue`** — one page, tabs by `state`: Unmapped, Possible
  duplicates, Drifted. Each row shows the Odoo bill number, date, vendor,
  amount, and the reason. Actions: map to cost center (which offers "map this
  analytic account to a cost center" as well as "assign just this line"),
  dismiss, accept/dismiss drift with old-versus-new side by side.
- **`OdooSyncRuns`** — run history with counters, `truncated`, and failure
  stage/reason.
- **Dashboard additions** — the `unmapped_spend` disclosure rendered
  **explicitly rather than hidden** (Budget §11's rule for the unconvertible
  disclosure, applied to the same class of problem), and the stale-sync banner
  of §7.4.

Conventions carried forward without exception: money typed `string`, never
`Number()`; badge variant maps keyed on **exactly** the backend's `state` and
`reason` vocabularies (§4.5) with a `?? "muted"` fallback and a comment citing
the backend source; every mutation hook's `invalidateQueries` keys match the
registered query keys; strict TS, no `any`.

**All three new surfaces must be reachable from navigation** — the Phase-12
lesson Budget §11 records, where two report pages shipped routed but unlinked. A
review queue nobody can navigate to is worse than no queue, because the
disclosure on the dashboard will point at it.

---

## 12. Testing requirements

Test-first per CLAUDE.md. Each row names a **falsifier**: the observation that
would show the requirement is not met. A requirement whose falsifier cannot be
observed by a test in this repo is marked as such rather than dressed up.

| # | Requirement | Falsifier — what observation would show it broken |
| --- | --- | --- |
| R1 | A sync over unchanged Odoo data is a no-op | Running the sync twice against the same fixture produces two expense rows, or a second `budget.expense_created` audit event, or a `created` count > 0 on the second run |
| R2 | The natural key survives a re-key | Deleting `UNIQUE (org_id, connection_id, odoo_model, odoo_id, analytic_ref)` leaves R1 green — i.e. the test passes for a reason other than the constraint |
| R3 | `analytic_ref = ''` (not NULL) blocks a duplicate un-distributed line | Inserting the same un-distributed line twice succeeds |
| R4 | The connector cannot reach a platform host | A connection whose host resolves to the configured vault/gateway/database address (by name **or** by a different name resolving to the same address, with the pair allowlisted) completes a request |
| R5 | No credential reaches any output | The key appears in an API response, a log line, `last_error`, an audit metadata field, or a `budget_odoo_sync_run` row, under a test that plants a recognisable sentinel value as the key |
| R6 | Odoo expenses leave `link_id` NULL | Two Odoo lines in one month collide on `uq_expense_autofeed`; or a test asserting `link_id IS NULL` for `source='odoo'` fails |
| R7 | An unmapped line creates no expense and is not dropped | A line with an unknown analytic account produces an `expense` row, **or** produces no `budget_odoo_purchase_line` row |
| R8 | Unmapped spend is disclosed on aggregates | `variance` / `position` return `unmapped_spend.count = 0` while queued rows exist for the org |
| R9 | The credit-note sign is correct | A captured `in_refund` fixture produces a positive `amount`. *Gated against the fixture captured in §2.6 task 1 — this test is only as true as that capture, and says so in its docstring* |
| R10 | A split line's parts sum exactly to the line total | Any share vector (including 33.33/33.33/33.34 and 1/3 thirds) where the allocated parts do not sum to the document amount to the cent |
| R11 | A negative expense aggregates correctly | A credit note leaves `actual` unchanged, or makes an aggregate raise, or is excluded from a total that includes its originating bill |
| R12 | A confirmed expense is never mutated by the sync | An amended Odoo bill changes the `amount`, `expense_date`, `fx_rate` or `amount_base` of a `confirmed` expense; or fails to produce a `drifted` mirror row |
| R13 | A cancelled bill voids only an `expected` expense | A `confirmed` expense is voided by the sync; or an `expected` one is left standing after its move goes to `cancel` |
| R14 | A vanished move is detected | Deleting a move from the fixture between runs leaves the mirror `linked` and the expense standing — i.e. the existence re-check did not run or did not cover it |
| R15 | An accepted drift is not re-raised | The next run over unchanged Odoo data returns the same row to `drifted` |
| R16 | A missing Odoo field refuses the run | Removing a required field from the `fields_get` fixture yields a successful run with zero rows instead of `reason='schema_incompatible'` |
| R17 | The frozen method set holds | A call with `method='write'` (or `create`, `unlink`) constructs an HTTP request instead of raising |
| R18 | Cross-org isolation, per table, behavioural | Removing the `org_id` predicate from any query leaves every test green (each of the five tables gets its own test, mutation-verified) |
| R19 | RBAC completeness, router-derived | Any route in this spec reachable without its permission; technician reaching any of them; `expense.record` accepting a drift; `odoo.manage` absent from `PERMISSIONS` |
| R20 | Audit actor is `system` for scheduled work | A scheduled run's expense events carry `actor_type='user'`; or a manual run's carry `system` |
| R21 | Audit metadata carries no amounts | An expense-update event's metadata contains a value rather than a field name |
| R22 | A run bounded by `MAX_MOVES_PER_RUN` resumes | The cursor advances past an unprocessed move, so the next run skips it |
| R23 | One org's failure does not stop another's | A connection raising at authentication leaves a second org's due connection unsynced in the same dispatch |
| R24 | `http://` cannot be configured | A `base_url` with scheme `http` is accepted by the schema, or any code path reaches `verify=False` |

**Not gated, and stated rather than implied:** the correctness of §3.2's field
names against a *real* Odoo instance. Every mapping test runs against the
fixture captured in §2.6, so the suite proves the mapping is consistent with
what the probe saw — not that the probe saw the right instance. The
`schema_incompatible` refusal (R16) is what converts a wrong field name into a
loud failure in production instead of a silent zero-row sync; it is a
containment, not a verification.

---

## 13. Deploy notes

To add to `deploy/README.md` and `.env.example`:

- **Control-plane only.** One new Celery beat entry
  (`budget-odoo-sync-dispatch`, hourly dueness check), no new compose service,
  and one optional env var: `ITS_ODOO_SYNC_ENABLED` (default `true`) as a global
  kill switch. Everything else is per-org database configuration, because a URL
  and a credential are per-tenant and an env var cannot be.
- **Pick one direction.** `GET /budget/expenses/export/odoo` (CSV out) and this
  sync (bills in) **must not both carry the same spend** — §0.9. The platform
  cannot detect the loop by identity; the heuristic duplicate flag reduces it and
  does not prevent it.
- **The Odoo user should be read-only.** The connector calls only read methods
  (§2.2), but the platform cannot enforce the rights on the Odoo side. Create a
  dedicated Odoo user with read access to Accounting, and give it an **API key**
  rather than a password.
- **`odoo_company_id` is required and is a tenancy control.** A wrong value
  imports another legal entity's spend, and no RLS policy on this side can see
  it.
- **Odoo-sourced expenses land `confirmed` by default** with `confirmed_by =
  NULL` (§0.10). `confirmed_by IS NULL AND source = 'odoo'` is the query for
  "figures no human in this platform asserted".
- **Actuals are ex-tax** (`price_subtotal`, §3.2).
- **HTTPS only**, certificate always verified; an internal CA goes in
  `ca_cert_pem` on the connection.
- **`odoo.manage` is admin-tier and can indirectly create expense rows.**

---

## 14. Deferred (explicitly not in v1)

- **Any write to Odoo.** Not built and not pinned — §0.1.
- **Purchase orders as a commitment feed.** Needs a PO → bill settlement model
  first, or it double-counts against materialization (§0.3).
- **FX rates from `res.currency.rate`.** Technically easy and deliberately not
  done: an `fx_rate` row is an operator assertion that history is valued at, and
  importing them changes who owns that number. If it is ever built, imported
  rates must be distinguishable from operator-entered ones in the table.
- **Automatic vendor creation** from `res.partner` (§4.4) — needs a dedupe model
  for a registry this module does not own.
- **Tax handling** of any kind, per Budget §1's boundary.
- **Payments, aging, and payment state.** Budget tracks cost, not cash.
- **More than one connection per org**, and therefore more than one Odoo company
  per org. The schema is *shaped* for more (§8.1's surrogate key) but
  constrained to one; lifting it needs a rule for which connection a queued line
  and a cost-center mapping belong to, which is a design, not a flag.
- **Odoo analytic *plans*** (Odoo 17+) beyond reading the default plan's
  distribution — a line distributing across two plans is pulled per its default
  plan and flagged, not modelled.
- **Automatic settlement** of a materialized `expected` row by a matching Odoo
  bill. The suggestion is built (§0.10); acting on it automatically is not,
  because a fuzzy amount match must not void a budget row.
- **Anything Odoo Helpdesk / ticketing.** Superseded by
  `docs/modules/service-desk/spec.md` — see §0.2.
- **A push-based trigger** (Odoo webhook / automated action calling this
  platform). It needs an inbound authenticated endpoint, which is a security
  design in itself and contradicts the outbound-only posture this integration
  currently keeps.

---

## 15. Open questions — operator decision or external verification required

Listed separately because none of them should be settled by an implementer
guessing.

**Needs the operator's decision:**

1. **`land_as` default = `confirmed`** (§0.10). This narrows
   `docs/modules/budget/spec.md` §0.3 ("expected entries never auto-confirm").
   The argument is that a posted vendor bill *was* verified, in the system of
   record, and that `expected` would double-count against materialized
   subscription rows. **If accepted, Budget §0.3 should be amended to say so** —
   a decision recorded in `docs/` should not be quietly narrowed by a second
   document.
2. **Ex-tax actuals** (§3.2). `price_subtotal` excludes tax. Correct where VAT
   is recoverable; wrong where it is not.
3. **Sync cadence.** Daily default (§7.1). Hourly is available and costs 24× the
   credential-checkout audit volume for latency the accounting rhythm does not
   need.
4. **The one-direction rule** (§0.9). If any org is currently pointing an Odoo
   import job at `GET /budget/expenses/export/odoo`, enabling this sync for that
   org will double-count, and the heuristic will not catch every case.
5. **`RECHECK_WINDOW_DAYS = 120`** (§5.4) — how far back an amendment or a
   deletion is still expected to be found. Too short and a quarter-end
   adjustment is missed permanently.

**Needs verification against the target Odoo instance before implementation
(§2.6 task 1 exists to do exactly this):**

6. **The Odoo major version**, which decides everything below.
7. **`analytic_distribution` (JSON, 16+) versus `analytic_account_id` (many2one,
   ≤15)** on `account.move.line` — §4.2. The highest-risk assumption in this
   spec.
8. **The credit-note sign convention** on `in_refund` lines — §3.2, R9.
9. **`invoice_line_ids` availability and semantics** — the whole line-selection
   strategy of §3.1 depends on it returning product lines only.
10. **`res.currency.name` being the ISO code** on the target instance.
11. **The `authenticate` versus `login` method name** on the `common` service,
    and whether the instance is behind a proxy that rewrites `/jsonrpc`.
12. **`account.analytic.account`'s human-readable identifier** (`code`? `name`?
    plan-qualified?) — needed only for the admin UI's display, not for
    correctness of the mapping, which uses ids.
13. **Whether the instance enforces API keys** or still accepts passwords, which
    decides what the admin UI recommends.

---

## 16. Related documents

- `docs/modules/budget/spec.md` — the parent module. §0.1 (FX), §0.2
  (`external_ref`), §0.3 (confirmation), §2 (data model), §3 (currency and the
  disclosure convention), §7 (reports and the CSV contract), §8 (invariants),
  §14 (this feature's deferral).
- `docs/modules/change-mgmt/spec.md` §5 — the pinned Odoo adapter shape, and the
  revision that superseded its Odoo target (§0.2 here).
- `docs/modules/service-desk/spec.md` §0.1 — why Odoo is no longer this
  platform's ticketing system.
- `docs/modules/email-intake/spec.md` — the closest structural precedent: an
  external system, polled, with vault credentials, per-org configuration,
  bounded sanitized errors, claim-based scheduling, and idempotency as the hard
  part. §3's credential-audit correction is reused verbatim here.
- `backend/app/core/egress.py` — the outbound guard reused in §2.4, and the
  reasoning for each of its five properties.
- `backend/app/core/vault.py`, `docs/kernel/vault.md` — the envelope the
  credential lives in.
- `docs/03-security-model.md`, `CLAUDE.md` — the invariants applied throughout.
