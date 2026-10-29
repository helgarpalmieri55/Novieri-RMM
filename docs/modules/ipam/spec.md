# Module Spec — IP Address Management (IPAM)

> Phase 15 (`docs/06-roadmap.md`, "Note on IPAM"). Depends only on **Inventory**
> (Phase 1) and the kernel. Derived from `docs/modules/ipam.md` via the
> brainstorming skill.
>
> Adds **one** agent capability — `net.sweep` — and it is the first capability in
> this platform that puts packets on **someone else's** wire. That single fact
> shapes §0.9, §5, §6 and most of §11.
>
> This module is the **address plan of record and its reconciliation**. It is not
> network monitoring. §0.10 defines "last seen" precisely enough that the
> difference is a property of the schema, not of anyone's restraint.

---

## 0. Resolved design decisions

Eighteen decisions, each ruling out a family of alternatives. Where a decision
accepts a failure mode, the failure mode is stated rather than left to be
discovered.

§0.1–§0.14 were settled before the design. **§0.15–§0.18 are amendments.** The
implementation plan (`docs/modules/ipam/plan.md`) read this spec against the
live code and found four points left under-determined — places where a builder
would have had to guess, and where the guess would have been load-bearing. Each
is resolved here, in the same voice and to the same standard: the decision, the
alternative it rules out, and the failure mode it accepts. Two further factual
corrections the plan found are made in place and flagged where they occur:
§11.1's table count and §14.4's file paths.

### 0.1 Native `inet`/`cidr`, not text

`ipam_subnet.prefix` is `cidr`; `ipam_address.address` is `inet`;
`ipam_range.start_address`/`end_address` are `inet`; MACs are `macaddr`.

The entire module is containment and ordering queries — "which subnet holds
10.4.7.9", "does this new /24 overlap an existing one", "the next free host in
this prefix", "every observation inside this segment". In text those are string
gymnastics that are wrong at every boundary (`10.4.7.9` sorts before `10.4.7.10`;
`10.4.70.0/24` `LIKE '10.4.7%'`). In `inet`/`cidr` they are `>>=`, `<<=`, `&&`,
`<`, all GiST-indexable, all correct for IPv4 and IPv6 without a second code
path. `macaddr` likewise normalises `A4-BB-6D-11-22-33`, `a4:bb:6d:11:22:33`
and `a4bb.6d11.2233` to one value — which matters more than it looks, because
§4's MAC-mismatch drift class is a string comparison between something a Windows
agent formatted and something an ARP table formatted, and in `text` that
comparison is a false positive generator.

Rejected: **text with a computed integer column**. It works for IPv4 and falls
apart at IPv6 (128 bits does not fit in `bigint`), and it means every write path
must keep two representations agreeing.

No Postgres extension is required: `inet`, `cidr`, `macaddr` and the
`inet_ops` GiST opclass are all core. **This module adds no deploy change.**

### 0.2 Flat rows; the hierarchy is *derived* from containment

There is one `ipam_subnet` table. `parent_id` exists but is a **cache**,
recomputed on every write as "the smallest other prefix in the same VRF that
contains this one". Nobody sets it by hand.

A hand-maintained hierarchy can disagree with the addresses it describes —
somebody parents `192.168.1.0/24` under `10.0.0.0/8` and the tree renders
happily while every containment query says otherwise. Deriving the parent from
`prefix` makes that state unrepresentable, and the derivation is one indexed
query.

Consequence accepted: inserting a supernet **between** existing rows re-parents
its new children, so a subnet write is not a single-row write. It is bounded
(only rows contained by the new prefix, in the same VRF) and it happens under
the §0.3 lock.

`kind ∈ {container, assignable}` separates a pure-hierarchy prefix (a /8 that
exists so the tree makes sense) from one that holds addresses. Only
`assignable` subnets have utilisation, next-free, and sweeps. Without this, the
first thing an operator does is ask for the next free address in their /8.

### 0.3 Overlap is a service-layer rule under an advisory lock, not an exclusion constraint

Uniqueness of `(org_id, vrf_id, prefix)` is a plain unique index — the same
prefix cannot be registered twice in one routing domain.

Overlap is different. A supernet *must* overlap its children; that is the
hierarchy. The rule that matters is "no two **siblings** overlap", and a GiST
`EXCLUDE` constraint cannot express it: keying on `parent_id` leaves top-level
rows (`parent_id IS NULL`) unconstrained, because exclusion constraints do not
fire on NULL, and the `COALESCE(parent_id, org_id)` sentinel that would fix it
is a trick that a later reader has no way to interpret.

So: sibling overlap is checked in `subnets.create/update` with an explicit
containment query, and the check plus the insert run under
`pg_advisory_xact_lock(hashtext('ipam:' || org_id || ':' || vrf_id))` — the
same serialisation technique `audit_service.record` already uses to keep the
hash chain linear. Two concurrent overlapping inserts serialise; the second
sees the first and is refused.

Cost accepted: the guarantee is application-strength, not constraint-strength.
A direct SQL insert bypasses it. That is true of every business rule in this
codebase; the rules that must survive a bypass are the **tenancy** rules, and
those are RLS (§11.1), not this.

### 0.4 Addresses are **sparse**, and a released address keeps its row

An `ipam_address` row exists only once something has been said about that
address — assigned, reserved, imported, or observed. Free addresses are the
**absence** of a row plus the absence of a covering range; they are computed,
never stored.

Materialising is arithmetically hopeless at the sizes this module must survive.
A /16 is 65,534 rows; a /8 is 16,777,214; and a real estate has several, per
tenant, of which ~99% would carry no information whatsoever. Utilisation and
next-free do not need those rows — they need a count and an anti-join.

Consequence accepted, and it is the real one: **enumeration does not scale, so
enumeration is capped.** `Policy(module="ipam").config.enumeration_max_hosts`
(default 65,536) is the ceiling. Next-free and per-address grid listing are
refused above it with `422 subnet too large to enumerate`, and utilisation
above it is reported as counts (`assigned`, `reserved`, `observed`) with a
**null** percentage rather than a number nobody can compute. An operator with a
/8 gets a hierarchy, drift, and search — not an address grid. That is the
correct answer; the alternative is a grid that times out.

Release does **not** delete the row: it sets `state='free'`, clears the
claimant, and stamps `released_at`. Sparse means "no row until first
mentioned", not "rows evaporate". History lives in the audit log (§0.13).

### 0.5 IPv6 is stored, documented and reconciled in v1 — but never *enumerated*

The schema is dual-family from the first migration (§0.1 gives that for free).
IPv6 subnets, IPv6 address records, IPv6 inventory observations and every drift
class in §4 work in v1.

What is refused for IPv6, with an explicit error rather than a wrong answer:
**next-free, utilisation percentage, address-grid enumeration, and `net.sweep`.**
Not because of the type system — because the operations are meaningless. A /64
holds 1.8×10^19 addresses; "next free" is not a hard query there, it is a
category error, and an ICMP sweep of a /64 is a thought experiment. The v1
constraint is therefore not "no IPv6" but **"no enumeration"**, which is
exactly the same rule §0.4 already imposes on large IPv4 prefixes. One rule,
two applications, one error message.

Failure mode accepted: an operator running a real IPv6 estate gets a registry
and drift detection but no allocator, and will ask for one. The deferred item
is named in §15 (EUI-64/token-based v6 allocation), and nothing here is
designed to make it hard.

### 0.6 `state` is documented intent; `in_use`, `stale`, `conflicting` are *derived*

The brief lists six things an address "moves through": free, reserved,
assigned, in-use, stale, conflicting. They are not one enum, and collapsing them
into one is the mistake that ruins this category of product.

- **`ipam_address.state ∈ {free, reserved, assigned, deprecated}`** — what a
  **human** asserted. Only an operator with `ipam.assign` ever writes it.
- **Observations** (§4.1) — what the **fleet** reported. Written only by
  ingestion, never by a human.
- **`status`** — the thing rendered in the grid and returned by the API:
  `free | reserved | assigned | in_use | stale | conflicting | dhcp |
  undocumented | dormant`. It is a **pure function** of (state, covering
  ranges, current observations, claimant liveness), computed at read time, and
  it is stored nowhere.

If `in_use` were a value of `state`, then an agent reporting an address would
have to write the `state` column — and a device coming online would silently
overwrite what an operator documented. That is not a hypothetical; it is what
"the IPAM changed my reservation by itself" means. Under this split, **the
agent's report can create an address row and can never change its `state`.**
That sentence is also an untrusted-input control (§6.4).

### 0.7 A default VRF row per org, and `vrf_id` is NOT NULL

`ipam_vrf` gets a `global` row created lazily on first use; every subnet
carries a non-null `vrf_id`.

A nullable `vrf_id` produces two uniqueness rules, because `NULL` is distinct
from `NULL` in a unique index — `(org, NULL, 10.0.0.0/24)` could be inserted
twice, and the duplicate-prefix bug this table exists to prevent walks straight
in through the default case. A mandatory column with a default row costs one
join and closes it.

VLANs are `ipam_vlan (org_id, site_id NULL, vid 1..4094, name)`, unique
`(org_id, site_id, vid)` — VLAN 10 legitimately exists at every site.

Sites are a first-class `ipam_site` table rather than a free-text label,
because scoping ("everything at Manila") must be a foreign key and an index,
not a string comparison that quietly splits into `Manila` and `manila`.
`ipam_site.doc_record_id` optionally links to a documentation `location`
record so the two are not re-typed.

### 0.8 Drift is **materialised** with a stable identity, and needs two detections to speak

`ipam_drift` rows are recomputed by a daily Celery-beat sweep, keyed
`(org_id, vrf_id, address, drift_class)`. Recomputation **preserves**
`first_detected_at`, acknowledgement, and suppression; a resolved item is
stamped `resolved_at`, not deleted.

Computing drift live at read time was rejected for one reason: without a stored
identity there is nowhere to hang "I have seen this and it is fine", and an
un-acknowledgeable list is a list nobody opens twice. Persisting resolution
also makes "we closed 40 address-plan defects this quarter" an evidence export
rather than an anecdote.

A drift item is **published as an event** only after
`consecutive_detections >= 2`. One sweep that ran while a switch was rebooting
is not news.

### 0.9 The sweep schedule lives on the **server**, never in the agent's local scheduler

The kernel gives agents a local cron (`docs/05-agent-spec.md`, "Local
scheduler"), and inventory legitimately uses it. `net.sweep` does not. Every
sweep is dispatched by the Celery beat sweep as an individually created,
individually signed, individually audited job, with a short TTL (§5.2).

Because the authorized target list is computed at dispatch time and **carried
inside the signed params** (§6.2), a locally scheduled sweep would be a
standing authorization: the agent would keep probing a scope that was signed
once, weeks ago, and revoking a subnet's authorization would not stop it until
some future policy refresh. Signed-once-run-forever is precisely the shape
invariant #1 exists to prevent. The cost is that sweeps do not run while an
agent is disconnected — which is correct, since a disconnected agent also
cannot be told to stop.

### 0.10 "Last seen" is documentation currency, and the schema is what stops it becoming up/down monitoring

`ipam_observation.last_seen_at` is the timestamp of the most recent
**evidence-producing event**, where the evidence set is exactly two things: an
`inventory.collect` result that listed the address on a NIC, and a `net.sweep`
result that listed the address as responding. It answers *"how stale is this
documentation?"*, not *"is this host up?"*.

Six schema-level properties make that distinction structural rather than a
matter of discipline (§7 restates them as the boundary contract):

1. **No single-address probe exists.** There is no API, job type or parameter
   that targets one address. Evidence is a by-product of a subnet-wide sweep
   and of inventory that runs anyway.
2. **No `up`/`down` field, anywhere.** Absence is expressed only as an age, and
   an age below the drift threshold is not surfaced at all.
3. **No transition events.** There is no `address.down`. The only event is
   `ipam.drift_detected`, and only after two consecutive detections across a
   window measured in days (§0.8).
4. **No history.** Observations are **upserted in place**. There is no row per
   probe, no time series, no availability percentage. High-volume telemetry is
   explicitly out of this platform's scope (`docs/01-architecture.md`,
   "Scaling notes").
5. **No latency.** The `net.sweep` result document carries **no RTT field**
   (§5.4). It is absent deliberately: if it were present, someone would chart
   it, and the chart would be the monitoring product.
6. **The sweep interval is clamped server-side to ≥ 1 hour**, default 168h
   (weekly), and the API refuses anything shorter. A one-minute sweep interval
   is the exact mechanism by which an IPAM becomes a ping monitor; the clamp is
   the thing that prevents it.

### 0.11 Not-looked-at is never drift, and neither is a switched-off laptop

This is the decision the module lives or dies on. Every IPAM that gets ignored
got ignored because its drift list was mostly laptops that were asleep.

Two gates precede every absence-based drift class (§4.3 states them as
predicates):

- **Evidence-availability gate.** An address can only be "missing" if its
  segment was actually *looked at* inside the observation window — a successful
  sweep, or a fresh inventory ingest from some device documented in it. No
  evidence source ran ⇒ no drift, for the whole subnet. Absence of evidence is
  not evidence of absence.
- **Claimant-liveness gate.** For an address documented to a managed device,
  absence is drift only when the **device itself** has checked in more recently
  than the address was last observed. If the agent has been offline past the
  window, the address is `dormant`, which is a separate, quiet bucket — not on
  the drift list, not counted in totals, never eventful. If the agent *is*
  online and its current NIC set no longer contains the documented address,
  that is a **direct contradiction from the machine itself** and it is drift at
  high confidence.

Note the asymmetry, because it is the whole idea: silence is not information,
but a live device that disagrees with the plan is.

**Verified code detail that this gate depends on:** `Agent.status` /
`Agent.last_seen_at` are the connection-liveness signals, maintained by the
gateway (`app/gateway/main.py`); `Device.last_seen_at` is written **only** by
inventory ingestion (`app/modules/inventory/ingest.py:85`) and by enrollment,
so it means "last inventory sweep", not "last contact". The liveness gate reads
`Agent`. Reading `Device.last_seen_at` instead would mark every connected
machine that has not swept recently as dormant.

### 0.12 Sweep authorization is deny-by-default, per prefix, and admin-tier

No `ipam_scan_authorization` row covering a target ⇒ that target is never
swept. There is no org-wide "allow everything" switch and no implicit
authorization derived from "we documented this subnet, so presumably we own
it" — documenting a subnet is a filing action; authorizing packets against it
is a different decision by a different person, and conflating them is how a
customer's IPAM ends up scanning a segment their contract does not cover.

The convention this follows is the **notification module's webhook allowlist**
(`docs/modules/notification/spec.md` §2.6): explicit entries, deny by default,
an admin-tier permission, every add and remove audited. One deliberate
difference: notification refuses CIDR entries because "allowlisting a range is
how an allowlist quietly becomes a blanket permit"; here the unit of
authorization **is** a prefix, because the thing being authorized is a network
range and there is no host-level equivalent. The safeguard that replaces
host-granularity is that a prefix authorization can never *widen* a sweep — the
target set is always `subnet.prefix ∩ authorized prefixes` minus exclusions
(§6.2), so an over-broad authorization still cannot make the agent probe
outside a registered subnet.

> **Reality check, stated because the brief asked for consistency with it.**
> The task framing cites Sentinel's scan-governance/authorized-scope convention
> as the model. `docs/modules/security-logs.md` contains no such convention:
> Sentinel appears in this repository only as an **external** vulnerability
> platform (`docs/00-vision.md`, `docs/02-tech-stack.md`, and the patch spec's
> nullable `priority` seam), and the security-logs brief mentions it once, to
> say vulnerability management is not its job. There is nothing to be
> consistent with. The notification allowlist is the nearest **shipped**
> in-repo governance pattern, so this spec follows it, and §14.3 records
> `ipam_scan_authorization` as the table a future Sentinel integration should
> read rather than duplicate — which is the direction the IPAM brief's
> "one address plan feeding both platforms" actually points.

### 0.13 Assignment history is the audit log, exposed through one narrow projection

There is no `ipam_address_history` table. Assign, release, reserve and
state-change all write hash-chained audit events (invariant #4), which is
tamper-evident; a second history table would be neither, and would be a second
thing to keep in sync.

`GET /ipam/addresses/{id}/history` is gated on **`ipam.read`** and returns a
projection of `audit_events` filtered to `target_type='ipam_address'` and this
address's id — timestamp, actor, action, from/to, and the event's remaining
metadata as `detail`. This is a deliberate, narrow read of the audit log for
people who do not hold `audit.read`; the general audit API keeps requiring
`audit.read`. Recorded as a decision because "IPAM read implies a slice of
audit read" is exactly the kind of thing that should not be discovered in a
review.

**`detail` is part of the projection, not an extra.** §11.3 specifies claimant
and allocation method on `ipam.address_assigned` and the previous claimant on
`ipam.address_released`, and without them the history says an address changed
state while refusing to say who to — which is the question the page exists to
answer. It carries the event's metadata minus `from_state`/`to_state`, which
are columns of the projection already. Including it does not widen the grant:
`target_type` and `target_id` are both still pinned, the `org_id` predicate is
unchanged, and every identifier the three writers put in that metadata
(`address`, `subnet_id`, `vrf_id`, `claimant`) was resolved in the caller's own
org, so no field in it can name another tenant.

### 0.14 IPAM absorbs documentation's `network` records; it does not delete them

Verified against shipped code, not inferred: `doc_records` with
`record_type='network'` exists today (migration `0007`), and
`app/modules/documentation/schemas.py` defines `NetworkFields` as
`{cidr: str|None (validated parseable, stored as text in jsonb), vlan: int|None,
gateway: str, dns: str}` with `extra="allow"`. No seed data creates such rows,
so whether any exist is deployment-specific — this spec therefore **ships a
converter and assumes nothing** (§8.3).

- `network` stays a legal `record_type` (removing it would break existing rows
  and migrations are forward-only), but `POST /docs/records` **refuses**
  `record_type='network'` — **unconditionally, from this migration onward**,
  with an error pointing at `POST /api/v1/ipam/subnets`. Two places to write
  the same fact is how a plan of record rots. Reading, updating and archiving
  existing rows still works. (This clause originally read "once IPAM is
  installed". There is no per-module install state in this codebase; §0.18
  records the verification and the resolution.)
- A one-time, operator-invoked, dry-runnable import (§8.3) converts each
  parseable `network` record into an `ipam_subnet` and archives the source. It
  is **not** a data migration: a silent conversion would hit unparseable CIDRs
  and duplicate prefixes with no operator to ask.
- `doc_links.target_type` gains `ipam_subnet` and `ipam_address` so articles
  link to the real entity instead of a jsonb copy (§14.4).

### 0.15 A nullable column never appears in a uniqueness or upsert key

**This is §0.7, stated as a rule instead of as one table's exception.** §0.7
made `vrf_id` `NOT NULL` with a lazily created `global` row for exactly one
reason: `NULL` is distinct from `NULL` in a unique index, so a nullable column
in a key silently disables the key for every row that leaves it empty. That
reasoning is not specific to VRFs, and the spec as first written broke the same
rule twice more, in §2.3 and §2.7. All three are the same defect. The rule, for
this module and anything added to it later:

> **A column that participates in a `UNIQUE` index or in an `ON CONFLICT`
> inference clause is `NOT NULL`.** Where the underlying value is genuinely
> optional, either the key carries a `NOT NULL` sentinel *derived* from it, or
> the uniqueness is split into a pair of partial indexes that between them
> cover both cases. There is no third option, and "we only insert one of those
> anyway" is not one.

**§2.7 `ipam_observation` — the serious one.** The key was
`(org_id, vrf_id, address, source, mac)` and `mac` is nullable. Every ICMP-only
sweep observation has `mac IS NULL`, so no ICMP observation ever conflicts with
its predecessor: the weekly sweep of a /24 would **insert a fresh row per
responding host per sweep, forever**. The upsert that §0.10 point 4 names as
the schema property keeping this module out of the monitoring business would
have been an append, and the current-state table would have become the probe
time series that point exists to forbid — quietly, with nothing failing.

Resolution: `ipam_observation` gains
`mac_key text NOT NULL GENERATED ALWAYS AS (coalesce(mac::text, '')) STORED`,
and the unique index is `(org_id, vrf_id, address, source, mac_key)`.
**Generated, not defaulted at write time**, deliberately: a stored generated
column cannot be bypassed by a write path that forgets it or by a second
ingestion path added later, which is the whole point of moving the guarantee
into the schema rather than into a service. The write path never sets
`mac_key`; the read path never uses it for anything but conflict inference.

Rejected: **`UNIQUE NULLS NOT DISTINCT`**. It is available — the deploy pins
`postgres:16` (`deploy/docker-compose.yml:203`) and the feature is PG15+ — so
the objection is not capability. It is that NULL-collision then depends on a
non-default index modifier that appears nowhere in the column list, nowhere in
the ORM attribute, and nowhere at the call site: a reader of the model, of a
query, or of an `ON CONFLICT` clause cannot tell that `NULL` means "a value"
in this one index and "unknown" everywhere else in this schema. Hanging §0.10
point 4 on an invisible flag is worse than hanging it on a `NOT NULL` column
that says what it does. Also rejected: a select-then-insert in the service
(racy under two concurrent sweep results), and dropping `source` from the key
(it would collapse `arp` and `icmp` evidence into one row and destroy the
`confidence` distinction §2.7 depends on).

Failure mode accepted: when the MAC observed at an address *changes* — a NIC
replaced, a new host at a recycled address — the new evidence lands as a
**second** row rather than updating the first, because `mac_key` differs. That
is intended: §4.3's `mac_mismatch` and `conflict` predicates both read the set
of current observations at an address, and they need both MACs to be able to
say anything. The old row stops being current after `W_obs` (§4.2) and is
dropped by the 365-day retention pass. The bounded cost is at most one row per
distinct MAC ever seen at an address per source, not one row per probe.

**§2.3 `ipam_vlan` — the same hole, a different fix.** `UNIQUE (org_id,
site_id, vid)` with `site_id` nullable permits **two org-wide VLAN 10 rows**,
which is the duplicate the constraint exists to prevent. Resolution is a
partial index pair: `UNIQUE (org_id, site_id, vid) WHERE site_id IS NOT NULL`
plus `UNIQUE (org_id, vid) WHERE site_id IS NULL`. Together they are total.

The sentinel-row fix §0.7 chose for VRFs is **rejected here**, and the reason
the same defect gets two different fixes is worth stating: a VRF sentinel names
a real thing — `global` is the default routing domain, and every subnet is
genuinely in one. A site sentinel would not: a site is a physical place, and
inventing a fake one to satisfy an index puts a row that is not a place into
every site picker, every site-scoped query, and every "everything at Manila"
filter §0.7 created `ipam_site` to make honest. Where the optional scope is
real optionality rather than a missing default, partial indexes are the fix.

Failure mode accepted: two indexes instead of one, and **`ON CONFLICT ON
CONSTRAINT` does not work against either** — a partial unique index is not a
named constraint. Every upsert in this module uses the inference form
(`index_elements=[...]`, plus `index_where=` for the partial pair), and the
upsert path itself is tested, not merely the constraint. §13 carries both
tests: two ICMP-only upserts of the same address must yield **one** row with
`sighting_count = 2`, and a second org-wide VLAN 10 must be refused.

### 0.16 The `status` table is evaluated top to bottom, and `dormant` is separated from `assigned` by *history*, not by the window

§3.1 as first written listed nine `status` values as independent predicates,
and two of them overlapped: `dormant` fired on `¬(Gate E ∧ Gate L)`, which
includes "nothing has ever looked at this segment" — the exact case the
`assigned` row documents. Read as an unordered set, the table is contradictory;
read as an ordered chain, `assigned` is unreachable because `dormant` sits
above it. A builder would have resolved it by whichever order they happened to
write the branches in, and the resolution would have been invisible.

Resolution, in two parts:

1. **The table is a chain, not a set.** It is evaluated **strictly top to
   bottom, first match wins** — a sequence of `elif`s, and the frontend's
   `status.ts` mirror is table-tested against the same cases in the same order
   so the two cannot drift.
2. **The two rows are separated by history, and the ordering is a safety net
   rather than the mechanism.** A third gate joins the two windowed ones:

   > **Gate H (has this segment *ever* been looked at?)** — subnet `S` has ever
   > had an evidence source iff there exists an `ipam_sweep_run` for `S` with
   > `state='succeeded'` (**ever**, not within `W_obs`) **or** any
   > `ipam_observation` with `subnet_id = S.id` (**ever**, current or aged).

   `assigned` is `¬Gate H`: nothing has ever looked, so there is nothing to be
   dormant about — the documented state is all the information that exists.
   `dormant` is `Gate H ∧ ¬(Gate E ∧ Gate L)`: something has looked before,
   nothing can look now. With Gate H written into both rows they are disjoint,
   and the ordering guarantee stops a future edit from re-introducing the
   overlap silently.

Note what Gate H is *not*: it is not a fourth thing to keep in sync. Gate E
already queries `ipam_sweep_run` and `ipam_observation` for the subnet; Gate H
is the same two queries with the `W_obs` predicate dropped, computed once per
subnet per evaluation pass alongside it.

Rejected: leaving the table unordered and disambiguating by widening
`dormant`'s predicate alone. That works arithmetically and produces a table
where every row silently carries the negation of every row above it — which is
unreadable, and is how the overlap got in. Rejected also: dropping the
`assigned` row and letting the never-looked case render as `dormant`. Dormant
means "we cannot currently verify this", and telling an operator that about a
segment nobody ever configured a sweep for or ingested inventory from is
noise, precisely of the kind §0.11 exists to eliminate.

Failure mode accepted: a subnet whose only sweep run **failed**, or whose
single ancient observation was dropped by the 365-day retention pass (§4.2),
reads as `assigned` when `dormant` is arguably more accurate. Both are quiet,
non-eventful buckets — neither is drift, neither is counted, neither is
published — so the misclassification costs a label in one cell of the address
grid and nothing else. §4.3's `dormant` row carries Gate H too, so the drift
list and the grid agree.

### 0.17 IPAM reaches inventory's result through a hook module **inventory** owns, and every hook runs in a SAVEPOINT

§4.1 says observations are derived "after each `inventory.collect` result" and
never said how, which leaves two wrong implementations equally available.

Resolution: **inventory gains a two-function extension point,
`app/modules/inventory/hooks.py`** — `register_post_ingest_hook(fn)` and
`run_post_ingest_hooks(session, device, doc)` — and
`app/modules/inventory/handler.py` calls it after `ingest_inventory` and
before `queue_ui_event`. IPAM registers its hook from `ipam.register(app)`.
The dependency arrow points **ipam → inventory**, and inventory keeps owning
its own ingest path. §14.6 records the change as a change to another module's
files.

Rejected: **IPAM re-registering `inventory.collect` with its own result
handler.** `app.jobs.registry.register` overwrites by name, so this "works" —
and makes correctness a property of `register_all`'s call order (verified:
`app/modules/__init__.py::register_all` calls `inventory.register(app)` first,
so an IPAM registration appended at the end would win, an ordering nobody
editing that file would know to preserve). It also forks inventory's ingest:
IPAM's copy of the handler would have to keep re-implementing whatever
inventory's does next. Rejected also: **inventory importing IPAM directly**,
which inverts the arrow and makes a Phase 1 module unbuildable without a Phase
15 one.

**Each hook runs inside `async with session.begin_nested()` — a SAVEPOINT —
with `except Exception` logged.** The SAVEPOINT is not belt-and-braces around
the `except`; the `except` alone cannot deliver the guarantee §4.1 promises.
If the hook raises a **database** error — an integrity violation, a bad cast, a
deadlock — Postgres puts the enclosing transaction into an aborted state at the
moment the statement fails. Catching the Python exception does not undo that:
every subsequent statement in that transaction fails with "current transaction
is aborted", so inventory's own already-written device, software and
`hardware_components` rows are lost with it, and `JobEngine.complete`
(verified, `app/jobs/engine.py:145-180` — it wraps the registered
`result_handler` in `try/except Exception` and on any exception flips the job
to `failed` with `{"error": ...}`) marks the `inventory.collect` job failed.
**A `try/except` around a database error is a comment, not a control.** The
SAVEPOINT is what bounds the rollback to the hook.

One ordering detail that makes this correct rather than merely hopeful:
`session.begin_nested()` autoflushes already-pending dirty state the instant it
opens the subtransaction (documented from an empirically verified ordering bug
at `app/modules/notification/channels.py:288`). Because the hook runs *after*
`ingest_inventory`, that autoflush lands inventory's writes **outside** the
savepoint, before it opens — so rolling the savepoint back discards the hook's
work and keeps inventory's. Precedents for the pattern:
`app/modules/notification/channels.py:325`,
`app/modules/email_intake/mailboxes.py:277`,
`app/modules/sla/policies.py:499`.

This is **two independent layers**, and §13 tests them separately because a
test of either alone passes against an implementation missing the other:
(1) the SAVEPOINT plus `except`, proven by registering a deliberately raising
hook and asserting the job still succeeds *and* the inventory rows are present;
(2) IPAM's own NIC parser being total — it returns `[]` for garbage rather than
relying on layer 1 to catch it.

Failure mode accepted: a hook that fails does so **quietly**, leaving a log
line and no observations for that device. That is the correct trade — the
alternative is failing a fleet-wide inventory job for a documentation
side-effect — and it is not silent downstream: a subnet whose only evidence
source is a hook that keeps failing stops satisfying Gate E, and §0.11's rule
is that no evidence means no drift, so the module goes quiet rather than
wrong.

### 0.18 There is no per-module install state, so the `network` refusal is unconditional

§0.14 and §8.3 as first written conditioned the `record_type='network'` refusal
on IPAM being "installed". **Verified: no such state exists.**
`app/modules/__init__.py::register_all` imports every module and calls its
`register(app)` unconditionally, in every deployment; there is no enabled-module
registry, no per-org module flag, and no `installed` column anywhere in the
schema (searched for `enabled_modules`, `module_enabled`, `is_installed`,
`installed_modules` — zero hits). "Once IPAM is installed" therefore names no
condition a builder could evaluate, and any implementation of it would be an
invented one.

Resolution: the refusal is **unconditional**. `POST /docs/records` refuses
`record_type='network'` from the migration onward, raised in
`documentation/service.py::create_record` with a message naming
`POST /api/v1/ipam/subnets`. It is refused in the **service**, not by narrowing
`RecordCreate.record_type`'s regex (`documentation/api.py:65`, built from
`RECORD_TYPES`): `network` must stay in `RECORD_TYPES`
(`documentation/schemas.py:13`) so existing rows keep validating, reading,
updating and archiving — migrations are forward-only and those rows exist in
whatever deployments created them.

Rejected: **inventing per-module install state to make the original sentence
true.** That is a kernel feature — a table, a migration, an admin surface, RBAC
codes, and a test matrix over every module's registration — introduced to
condition one error message. A half-built version of it is worse than none: a
switch that changes what a security-relevant module does, that nobody audits
and no test covers. Rejected also: leaving `network` creatable alongside
`ipam_subnet`, which is the two-write-paths-for-one-fact problem §0.14 exists
to close.

Failure mode accepted: a deployment that uses documentation's `network` records
and does not care about IPAM loses the ability to create new ones the day this
migration lands, with no opt-out. Existing rows are untouched and remain fully
editable; §8.3's dry-runnable converter is the migration path; the error names
the endpoint to use instead. This is a deliberate behaviour change to a shipped
module, and §14.4 records it as one.

---

## 1. Purpose & boundary

Maintain the organisation's address plan — VRFs, sites, VLANs, subnets, ranges
and addresses — with an assignment workflow, and **continuously reconcile it
against what the fleet actually reports**, so the plan's defects are a list
somebody can work rather than a discovery made during an outage.

**In scope:** the registry (§2), assignment workflow and allocator (§3),
observations and reconciliation (§4), the `net.sweep` capability (§5), sweep
governance (§6), the monitoring boundary (§7), import/export and the
documentation absorption (§8), API (§9), events (§10), tenancy/RBAC/audit
(§11), frontend (§12), testing (§13), required cross-module changes (§14).

**Not in scope, and not by accident:** up/down status, latency, availability,
per-address probing, SNMP of any kind, interface counters, topology. §7 states
the contract; §0.10 states why it is structural.

**Dependency:** Inventory (Phase 1) is hard. §14.1 records the one contract
extension it needs.

---

## 2. Data model

Migration expected **`0040`**, chained from the actual head at build time —
confirm the head, never hard-code it elsewhere (the tree already carries an
uncommitted `0039` from concurrent work).

Every table below is org-scoped, gets `ENABLE ROW LEVEL SECURITY` **and**
`FORCE ROW LEVEL SECURITY`, and a `tenant_isolation` policy carrying **both**
`USING (org_id = current_setting('app.current_org', true)::uuid)` and the
matching `WITH CHECK` — invariant #5, with a behavioural cross-org test per
table (§13). The Phase 0 RLS-completeness test that walks `pg_tables` /
`pg_policies` covers them automatically.

> **Amended 2026-10-29 (MSP clients plan 2026-10-26, Tasks 10 and 11): IPAM
> is per client** (migration 0240). `ipam_vrf` and `ipam_vlan` are C
> (existing rows went to the internal client). `ipam_subnet` is H from its
> VRF: `vrf_id` is NOT NULL (§0.7), so a subnet's client is its VRF's.
> `ipam_range` and `ipam_sweep_run` inherit from the subnet;
> `ipam_address`, `ipam_observation`, `ipam_drift` and
> `ipam_scan_authorization` from the VRF. All nine carry the client policy of
> `CLAUDE.md` invariant 5a. A VRF's name is unique per client, and each client
> has its own `global` VRF. **`ipam_site` (§2.2) no longer exists** (plan
> decision C3): 0240 dropped it without copying rows. A VLAN, a subnet and a
> site server name one of the client's own sites in `sites` (`site_id`, held
> to the row's client by `fk_<table>_site_client`); a VLAN id is unique per
> site, or per client for a VLAN with no site. **A sweep stays inside its
> subnet's client**: the sweeper, a designated sweeper and the preferred site
> server are machines of that client. **Candidates and promotion stay inside
> it too**: an address is a candidate of one client, and promoting it needs
> its subnet to have a site, whose client and site the new machine takes
> (decision C7). A candidate whose subnet has no site is refused with "assign
> the subnet to a site before adding its machines", and a sweep's automatic
> creation leaves it on the Discovered list. **An import names its VRF**
> (`?vrf_id=`) **or, without one, its client** (`?client_id=`, whose `global`
> VRF it fills), and the apply step runs only against the VRF the preview
> was for. Converting documentation `network` records (§8.3) files each one
> under its record's client, or the internal client for a global record. The
> plan is `docs/superpowers/plans/2026-10-26-msp-clientes-fase2-modulos.md`.

### 2.1 `ipam_vrf`
`id (uuid7 PK), org_id FK, name (str 100), rd (str 64, nullable), description,
created_by, created_at, updated_at`. Unique `(org_id, name)`. A `global` row is
created lazily on first subnet write (§0.7).

### 2.2 `ipam_site`
*Dropped by migration 0240 (MSP clients plan 2026-10-26, Task 10): sites are
the clients' `sites`. Kept below as history.*

`id, org_id FK, name (str 200), doc_record_id (FK → doc_records, nullable),
description, created_at, updated_at`. Unique `(org_id, name)`.

### 2.3 `ipam_vlan`
`id, org_id FK, site_id FK NULL, vid (int), name (str 100), description`.
CHECK `vid BETWEEN 1 AND 4094`. Uniqueness is a **partial index pair**, not one
index (§0.15 — `site_id` is nullable, and a nullable column in a unique key is
not a unique key):
- `UNIQUE (org_id, site_id, vid) WHERE site_id IS NOT NULL` — VLAN 10
  legitimately exists at every site, once each.
- `UNIQUE (org_id, vid) WHERE site_id IS NULL` — and exactly once org-wide.

Neither is a named constraint, so upserts infer
(`index_elements=[...], index_where=...`); `ON CONFLICT ON CONSTRAINT` fails
against a partial index.

### 2.4 `ipam_subnet`
```
id, org_id FK, vrf_id FK NOT NULL, prefix cidr NOT NULL,
kind (str 12: container|assignable), name (str 200), description,
site_id FK NULL, vlan_id FK NULL,
gateway inet NULL, dns_servers jsonb (default []),
parent_id FK → ipam_subnet NULL,          -- derived cache, §0.2
sweep_enabled bool default false,
sweep_device_id FK → devices NULL,        -- designated sweeper, §5.3
sweep_interval_hours int NULL,            -- NULL = on-demand only; clamped ≥1, §0.10
last_swept_at timestamptz NULL,
doc_record_id FK → doc_records NULL,      -- provenance of an absorbed record, §8.3
created_by, created_at, updated_at
```
- Unique `(org_id, vrf_id, prefix)`.
- CHECK `kind IN ('container','assignable')`.
- CHECK `gateway IS NULL OR gateway << prefix` — a gateway outside its own
  subnet is a typo, and it is the typo that makes provisioning hand out a
  broken static config.
- CHECK `sweep_interval_hours IS NULL OR sweep_interval_hours >= 1` — §0.10's
  clamp restated in SQL, because the application check is one refactor away
  from being the only one.

**Indexes.** `USING gist (prefix inet_ops)` — this is the index that serves
containment (`>>=`, `<<=`) and overlap (`&&`), i.e. parent derivation, "which
subnet holds this observation", and §0.3's sibling check. Plus btree
`(org_id, vrf_id, prefix)` (the unique), `(org_id, site_id)`,
`(org_id, parent_id)`, and a partial `(org_id, sweep_enabled, last_swept_at)
WHERE sweep_enabled` for the dispatch sweep. No composite `(org_id, prefix)`
GiST — that would need `btree_gist`, and RLS already narrows every scan to one
org before the GiST predicate is applied, so it buys a dependency for nothing.

### 2.5 `ipam_range`
`id, org_id FK, subnet_id FK, kind (str 12: dhcp_pool|reserved|excluded),
start_address inet, end_address inet, name, description, created_by,
created_at`.
- CHECK `start_address <= end_address`; CHECK both `<< subnet.prefix` is
  enforced in the service (a CHECK cannot reach the parent row).
- Index `(org_id, subnet_id, start_address)`.
- This is how "the DHCP pool is .100–.200" is expressed **without 101 address
  rows** — §0.4's sparseness would be defeated immediately otherwise.

### 2.6 `ipam_address`
```
id, org_id FK, subnet_id FK NULL, vrf_id FK NOT NULL,
address inet NOT NULL,
state (str 12: free|reserved|assigned|deprecated) default 'free',
origin (str 12: manual|imported|discovered),
allocation_method (str 12: static|dhcp|slaac|discovered|unknown),
device_id FK → devices NULL,
asset_label (str 200, default ''),
mac macaddr NULL, hostname (str 255, default ''), description,
assigned_by FK → users NULL, assigned_at NULL, released_at NULL,
created_at, updated_at
```
- Unique `(org_id, vrf_id, address)` — one row per address per routing domain.
  A duplicate **claim** is a §4 conflict, not a duplicate row.
- CHECK `state <> 'assigned' OR (device_id IS NOT NULL) <> (asset_label <> '')`
  — an assignment has **exactly one** claimant. Both, or neither, is how
  "assigned to nothing" gets into a plan of record.
- CHECK `state IN ('free','reserved','assigned','deprecated')` — §0.6: no
  `in_use`, no `stale`, no `conflicting` in this column, ever.
- `subnet_id` is nullable on purpose: an observation may land in no registered
  subnet (§4.2 orphans).
- Indexes: the unique above; `(org_id, subnet_id, address)` for the grid;
  `(org_id, device_id) WHERE device_id IS NOT NULL`; `(org_id, mac) WHERE mac
  IS NOT NULL` for global MAC search; `USING gist (address inet_ops)` for
  cross-subnet containment search.

### 2.7 `ipam_observation`
```
id, org_id FK, vrf_id FK, subnet_id FK NULL, address inet NOT NULL,
mac macaddr NULL, hostname (str 255, default ''),
source (str 12: inventory|sweep|import|dhcp),
method (str 10: nic|arp|icmp|manual),
device_id FK → devices NULL,        -- set only for source='inventory'
confidence (str 6: high|medium|low),
allocation_origin (str 12: static|dhcp|slaac|link_local|unknown) default 'unknown',
sweep_run_id FK NULL,
mac_key text NOT NULL GENERATED ALWAYS AS (coalesce(mac::text, '')) STORED,
first_seen_at, last_seen_at, sighting_count int default 1
```
- Unique `(org_id, vrf_id, address, source, mac_key)` — **upserted**, bumping
  `last_seen_at` and `sighting_count`. This is a **current-state** table, not a
  time series; §0.10 point 4 is this constraint, and `mac_key` is what makes it
  hold. **Not** `(..., mac)`: `mac` is nullable, every ICMP-only observation has
  `mac IS NULL`, NULLs do not collide, and that key would append a row per host
  per sweep forever — §0.15 states the rule and the reasoning, which is §0.7's.
- `mac_key` is a stored generated column: no write path sets it, and none can
  forget it. It exists only as the conflict-inference target.
- `allocation_origin` is §14.1's `origin` for this address — **how the endpoint
  said the address was allocated** — and it is the input to §3.2's
  `allocation_method`. Added by migration **0051**.
  - It is **not** called `origin`: `ipam_address.origin` already means how a ROW
    got into the plan (`manual | imported | discovered`), which is a fact about
    an operator, and the two columns are in scope together in `address_grid` and
    `is_conflicting`.
  - **Not part of `uq_ipam_obs_key`.** A NIC that moved from a DHCP lease to a
    static address is the same sighting with a new answer, not a second row, so
    the upsert refreshes it exactly as it refreshes `hostname`.
  - `unknown` for every `source='sweep'` row, permanently and by design: ARP and
    ICMP establish that an address answers, never how it was configured — the
    same limit `confidence` records for the same evidence.
  - CHECK-constrained to the five values, which is why
    `backend/app/modules/ipam/nic.py` **normalises** an unrecognised origin to
    `unknown` rather than passing it through: on the `inventory.collect` result
    path an `IntegrityError` is a database error, and §0.17 layer 2 cannot
    contain one.
- The upsert infers on the index (`index_elements=[org_id, vrf_id, address,
  source, mac_key]`); the index is total, so no `index_where` is needed here.
- Indexes: the unique; `(org_id, last_seen_at)` for ageing; `(org_id, device_id)`;
  `USING gist (address inet_ops)`.
- Confidence is a property of the method, fixed at write time:
  `inventory/nic → high` (the OS reported about itself, over mTLS, as the
  result of a signed job), `sweep/arp → medium` (authoritative for
  address↔MAC on-segment, not for identity), `sweep/icmp → low` (something
  answered; that is all it means).

### 2.8 `ipam_drift`
```
id, org_id FK, vrf_id FK, subnet_id FK NULL, address inet NOT NULL,
drift_class (str 24), severity (str 8: low|medium|high),
confidence (str 6), detail jsonb,
address_id FK NULL, device_id FK NULL,
first_detected_at, last_evaluated_at, consecutive_detections int default 1,
resolved_at NULL,
acknowledged_by FK NULL, acknowledged_at NULL, suppressed_until NULL, note
```
- Unique `(org_id, vrf_id, address, drift_class)` — the stable identity of
  §0.8. Recomputation upserts.
- Indexes: the unique; `(org_id, drift_class, severity) WHERE resolved_at IS
  NULL`; `(org_id, subnet_id) WHERE resolved_at IS NULL`.
- `detail` carries the evidence that produced the finding (documented claimant,
  observing source, MACs seen, ages) so the UI can explain itself without
  re-deriving.

### 2.9 `ipam_scan_authorization`
`id, org_id FK, vrf_id FK, prefix cidr NOT NULL, note (str 500),
authorized_by FK → users NOT NULL, authorized_at NOT NULL, expires_at NULL,
revoked_at NULL, revoked_by FK NULL`.
- Unique `(org_id, vrf_id, prefix) WHERE revoked_at IS NULL`.
- `USING gist (prefix inet_ops)` — the dispatch path asks "which authorizations
  contain or intersect this subnet", which is a containment query.
- `authorized_by` is **NOT NULL**: there is no system-authorized scope. A human
  with `ipam.scope.manage` put their name on every packet this module emits.
- Rows are never hard-deleted; revocation stamps `revoked_at`, so the record of
  what was authorized when survives for evidence.

### 2.10 `ipam_sweep_run`
`id, org_id FK, subnet_id FK, job_id FK → jobs NULL, device_id FK → devices
NULL, state (str 12: queued|dispatched|succeeded|failed|refused|no_agent),
trigger (str 10: scheduled|manual), target_count int, scope_digest (str 64),
hosts_found int default 0, truncated bool default false, requested_by FK NULL,
started_at, finished_at NULL, error (str 500, default '')`.
- Index `(org_id, subnet_id, started_at DESC)`.
- `state='no_agent'` is a first-class outcome, not an error swallowed in a log:
  a subnet nobody can sweep is a fact the subnet page must show, and it is also
  what §4.3's evidence gate reads.

---

## 3. Assignment workflow

### 3.1 The derived `status` (§0.6)

Computed for an address `A` in subnet `S`, given its row `D` (possibly absent),
the covering ranges, and its **current** observations (§4.2).

**The table is evaluated strictly top to bottom, first match wins** (§0.16) — a
chain of `elif`s, not a set of independent predicates. `status.ts` (§12) mirrors
it in the same order and is table-tested against the same cases.

| # | `status` | Predicate |
|---|---|---|
| 1 | `dhcp` | `A` ∈ a `dhcp_pool` range (takes precedence over everything below) |
| 2 | `conflicting` | §4.3's conflict predicate holds — the *same implementation* (`reconcile.is_conflicting`), never a second copy: two definitions is how the grid and the drift page come to disagree about one address on one afternoon |
| 3 | `in_use` | `D.state='assigned'` **and** a current observation exists |
| 4 | `stale` | `D.state ∈ {assigned,reserved}`, no current observation, and both §4.3 gates pass (**E ∧ L**) |
| 5 | `assigned` | `D.state='assigned'`, no current observation, and **¬Gate H** — `S` has *never* had an evidence source (§0.16). Nothing has ever looked; there is nothing to be dormant about |
| 6 | `dormant` | `D.state ∈ {assigned,reserved}`, no current observation, **Gate H** holds, and Gate E or Gate L blocks now (§0.11) |
| 7 | `reserved` | `D.state='reserved'` and none of the above |
| 8 | `undocumented` | no `D`, or `D.state ∈ {free,deprecated}`, and a current observation exists |
| 9 | `free` | everything else, including "no row at all" |

Rows 5 and 6 are **disjoint by predicate** (Gate H appears negated in one and
asserted in the other), not merely separated by their position. The ordering is
the safety net; Gate H is the mechanism. Earlier drafts had row 6 firing on
`¬(E ∧ L)` alone, which swallowed row 5 entirely — §0.16 records why that was
resolved this way rather than the other.

`deprecated` renders as `reserved`-like (excluded from next-free) but is
exempt from absence drift — it is the state for "we know this is gone, stop
telling us".

### 3.2 Transitions on `state`

Every transition requires **`ipam.assign`** (§11.2), runs inside the tenant
session, and writes one audit event (§11.3). `X` is the target address.

| From | To | Predicate (all must hold) |
|---|---|---|
| *(absent)* | `reserved` | `X` is contained by an `assignable` subnet in-org; `X` ∉ any `dhcp_pool` range; `X` is not the network or broadcast address of its subnet (IPv4, masklen ≤ 30) |
| *(absent)* / `free` | `assigned` | as above, **plus** exactly one claimant supplied (`device_id` XOR `asset_label`), **plus** no unresolved `conflict` drift on `X` that is neither acknowledged nor currently suppressed (§4.3's last note), **plus** if `device_id` is given it resolves in-org |
| `reserved` | `assigned` | claimant rule above |
| `assigned` | `free` (release) | none beyond RBAC; clears claimant + `mac`, stamps `released_at` |
| `assigned` | `reserved` | none beyond RBAC; clears claimant |
| `reserved` | `free` | none beyond RBAC |
| any | `deprecated` | none beyond RBAC |
| any | any | **the actor is never an agent.** No ingestion path writes `state`. |

**Assignment inside a `dhcp_pool` is refused (409)** with guidance to shrink the
pool or use a `reserved` range — the brief requires DHCP ranges blocked from
static assignment, and modelling "DHCP reservation" as a static row inside a
dynamic pool creates a record that is true in the IPAM and false on the DHCP
server. A DHCP reservation is expressed by carving it out of the pool.

#### `allocation_method` is answered at read time, from two sources, and the operator wins

**CLOSED.** This section previously read "operator-set for manual assignments
and **permanently `unknown` for anything inventory-derived**", and recorded that
the agent had supplied the fact since §14.1 while nothing in this module read it.
It is read now.

`allocation_method` answers "how is this address allocated", and it has two
possible speakers:

| Speaker | Where it lives | Written by |
| --- | --- | --- |
| The operator | `ipam_address.allocation_method` (`static` by default on assignment) | `ipam.assign` holders, and imports |
| The fleet | `ipam_observation.allocation_origin` (§2.7, migration 0051) | ingestion only, from §14.1's `origin` |

**The answer is DERIVED, at read time, and stored nowhere** —
`allocation.derive_allocation_method`. The grid carries it as
`allocation_method` beside `allocation_method_source ∈ {documented, observed,
unknown}`. Precedence, in order:

1. **The operator's assertion, when there is one.** §0.6 governs this column as
   much as it governs `state`: an ingestion path that could overwrite what an
   operator documented is what "the IPAM changed my reservation by itself"
   means. A device reporting `dhcp` for an address documented `static` does not
   rewrite the plan. It is not discarded either — `source` says `documented`, so
   both halves are available to a caller that wants to surface the
   disagreement. Materialising that as a drift class is §4.3 work and is **not**
   done.
2. **Otherwise the most recent observation whose origin maps to a method.** A
   stored `unknown` is the column's DEFAULT, not an assertion, so it yields —
   which is what makes the field real for documented addresses and not only for
   undocumented ones. "Most recent that maps" rather than "most recent" is what
   stops a weekly sweep, which always reports `unknown`, erasing what inventory
   established.
3. **Otherwise `unknown`**, which the UI renders as an absence rather than as a
   value.

**The origin → method mapping is not an alias table**
(`allocation.ALLOCATION_METHOD_BY_ORIGIN`). Two of the five differ:

- `link_local` → `unknown`. **Link-local is not an allocation method**: an
  address a NIC gave itself because DHCP failed was not allocated by anybody.
  §4.1's discard rule means such an address is normally never observed at all,
  so the entry exists for an endpoint reporting `link_local` on a *documentable*
  address — a bug on the endpoint, answered with "unknown" rather than with a
  method nobody chose.
- `discovered` is a legal `allocation_method` and is **never produced from an
  observation**. It is what an import may carry (§8.1) — an operator saying
  "this came off a scan of the old estate" — and inventory evidence is not that.

**Why the observation and not the address row.** This section previously named
"a decision about how an observation's origin reaches an `ipam_address` row
given §0.6" as part of the cost of closing this. The answer is that **it does
not reach one.** §0.6 forbids ingestion writing an operator-asserted column, and
`status` already establishes the pattern for exactly this shape of question:
derive at read time from (what a human asserted, what the fleet reported), store
nothing. `observations.py`'s "writes `ipam_observation` rows and nothing else"
is therefore unchanged, and remains structurally asserted.

**What an unrecognised `origin` costs.** Nothing, and that is deliberate: the
address keeps its observation and only the origin degrades to `unknown`.
Refusing it would be tightening inbound, which
`docs/kernel/wire-protocol/README.md` forbids doing unilaterally — and would do
it silently, because `nic.py` is total. See §14.1.

### 3.3 Next-free and atomic allocation

`GET /ipam/subnets/{id}/next-free?count=N` returns the first N addresses
ascending that are **all** of:
- not the network or broadcast address (IPv4, masklen ≤ 30; `/31` and `/32` are
  special-cased per RFC 3021 — both hosts usable in a `/31`, the single address
  usable in a `/32`);
- not inside any `dhcp_pool`, `reserved` or `excluded` range;
- without an `ipam_address` row in state `reserved`, `assigned` or `deprecated`;
- **without a current observation.**

That last clause is the reconciliation paying for itself in the allocator: an
address that answers on the wire is not free, whatever the spreadsheet says,
and handing it out is an outage. It is also why `undocumented` findings matter
before anyone reads the drift page.

Implemented as a SQL anti-join over `generate_series` added to the network
address (`network(prefix) + i::bigint`), bounded by §0.4's ceiling and
short-circuited by `LIMIT`. Refused with `422` above the ceiling or for IPv6
(§0.5).

**Next-free is advisory.** Two operators who both call it and then both assign
race for `.42`. The non-racing path is `POST /ipam/subnets/{id}/allocate`,
which picks and assigns in **one** transaction under the §0.3 advisory lock and
returns the address it took. The UI uses `allocate`; `next-free` exists for
preview and for planning.

---

## 4. Reconciliation — the differentiator

### 4.1 Where observations come from

**Inventory (passive, no new agent work).** After each `inventory.collect`
result — specifically, from a hook registered into inventory's own post-ingest
extension point and run inside a SAVEPOINT, §0.17 and §14.6, never by
re-registering the `inventory.collect` job type — IPAM reads that device's
`hardware_components` rows with `type='nic'`
(the normalized, wholesale-replaced current state — `ingest.py` step 4) and
derives observations. Reading `hardware_components` rather than `Device.nics`
is deliberate: `nics` is a flattened jsonb convenience copy, while
`hardware_components` is typed, indexed, and unique per `(device_id, type,
identifier)`.

**What the shipped payload actually contains, verified rather than assumed:**
`ComponentDoc` is `{type, identifier (str ≤200), attributes: dict[str, Any]}`
and the exported wire contract
(`docs/kernel/wire-protocol/InventoryDocument.schema.json`) declares
`attributes` as `"additionalProperties": true` — **there is no NIC attribute
contract in the schema at all**, so §14.1's block is the contract and
`backend/app/modules/ipam/nic.py`'s module docstring is where it is written
down. That block is now what the agent emits
(`agent/internal/capabilities/inventory/nic.go`) and the **only** address key
this module reads.

**A second, transitional key existed and is gone.** When this module was built,
the only IP-bearing NIC shape in the repository was a single test fixture,
`{"type": "nic", "identifier": "a4:bb:6d:11:22:33", "attributes": {"ip": "10.1.2.42"}}`,
and `attributes.ip` was read as a fallback so the module was useful before the
agent-side collector existed. Once the agent emitted the block, keeping both
readers was insurance for a control plane deployed before it. There is no such
control plane — the platform is not live — so the fallback was removed from both
sides on 2026-08-05 (`docs/modules/agent/open-questions.md` OQ-2, closed there
with the argument intact). Two readers for one fact is how two implementations
of one thing drift; reinstating one is a wire-protocol decision, not a
tolerance fix.

Four consequences of the *legacy* shape shaped this design. Three are now
closed by §14.1's block and the fourth is not:

1. **No prefix length.** *(Closed — the block carries one.)* Subnet membership
   is nevertheless still derived by containment against the registry
   (`address <<= prefix`) rather than taken from the device, which is fine —
   arguably better — but it means an observed address inside no registered
   subnet has nowhere to go. §4.2 gives it one.
2. **One address per NIC.** *(Closed — `addresses[]` gives one observation per
   address, both families, secondaries included.)*
3. **No origin.** *(Closed, on the wire and in this module.)* The agent
   supplies `origin ∈ {static, dhcp, slaac, link_local, unknown}` on every
   entry; `nic.py` reads it into `NicObservation.allocation_origin`,
   `ipam_observation.allocation_origin` stores it (migration 0051), and §3.2's
   `allocation_method` is derived from it at read time. An `origin` this side
   does not recognise degrades the FIELD to `unknown` and costs the address
   nothing — refusing it would be tightening inbound, and silently, since this
   parser is total.
4. **`identifier` is documented as "e.g. NIC MAC" but not constrained to be
   one.** *(Still true, and still handled.)* IPAM treats `identifier` as a MAC
   **only when it parses as one**, and otherwise records the observation with
   `mac = NULL`. §14.1's `attributes.mac` is preferred over it when present,
   which is what lets §4.3's `mac_mismatch` fire on a Windows NIC whose
   identifier is "Ethernet0".

**Parsing rules, applied to every derived observation:** the value must parse as
an address; loopback (`127.0.0.0/8`, `::1`), link-local (`169.254.0.0/16`,
`fe80::/10`), multicast and unspecified addresses are **discarded**, never
stored — a NIC reporting `169.254.x` is reporting that DHCP failed, which is
not an IPAM fact. A NIC attribute block that fails to parse **degrades to
"no observation"**; it never raises, because a raising handler would fail the
`inventory.collect` job (`JobEngine.complete` converts a raising result handler
to `failed`), and this module must not be able to break Phase 1.

**`Agent.last_ip` is not an observation source.** It is the TCP peer address of
the WebSocket connection as seen by the gateway (`app/gateway/main.py:243`),
which behind NAT is an egress address belonging to a firewall, not to the
endpoint. Ingesting it would document the wrong machine at the wrong address.

**Sweep (active).** `net.sweep` results (§5.4), after the §6.4 scope check.

### 4.2 Ageing, currency, and orphans

`W_obs` = `Policy(module="ipam").config.observation_ttl_days`, default **14**
(≈ 14 inventory sweeps at the 24h default cadence, and ≥ 2 sweeps at the weekly
`net.sweep` default — so a single missed cycle never ages anything out).

- An observation is **current** iff `last_seen_at >= now() - W_obs`.
- Observations are **not deleted** when they age out; they stop being current.
  A retention pass drops observations with `last_seen_at < now() - 365 days` to
  keep the table bounded.

**The retention pass bounds three tables, not one**
(`app/modules/ipam/retention.py`, daily beat `ipam-retention`). §4.2 as first
written named only `ipam_observation`, which left the other two append-mostly
tables in this module with no delete path at all:

| Table | Window | Predicate |
|---|---|---|
| `ipam_observation` | 365 days | `last_seen_at <` cutoff |
| `ipam_drift` | 365 days | `resolved_at <` cutoff — **resolved rows only** |
| `ipam_sweep_run` | 365 days | `started_at <` cutoff, for `succeeded`/`failed`/`dispatched`/`queued` |
| `ipam_sweep_run` | **30 days** | `started_at <` cutoff, for `refused`/`no_agent` |

- **An OPEN drift finding is never deleted**, whatever its age. It is a finding,
  not a record; deleting one would be the module quietly forgetting a problem
  nobody fixed. Same rule, same reason, as endpoint-health's `health_alert`.
- **`refused` and `no_agent` get a shorter window because they are a
  configuration fact restated hourly, not work performed.**
  `handle_sweep_result` is the only writer of `last_swept_at`, so a
  `sweep_enabled` subnet with no authorization covering its prefix is re-offered
  by `due_subnet_ids` on every beat and records a `refused` run every time —
  8,760 rows per subnet per year, for a subnet whose configuration never
  changes. The hundredth identical restatement carries nothing the first did
  not. Recording the refusal remains **mandatory** (§2.10): suppressing the
  duplicate at dispatch time was rejected, because "no run was recorded" and
  "no beat fired" must not become the same observation.
- **The newest run for a subnet is never deleted**, whatever its state or age.
  A persistently-unauthorized subnet therefore stays *visibly* refusing rather
  than going silent: `GET /subnets/{id}/sweep-runs` still answers with a
  `refused` run stamped within the last hour. An empty run list means "never
  swept"; a refusal means "we tried and were told no", and those must not
  collapse into one answer.
- **A run still referenced by a surviving `ipam_observation.sweep_run_id` is
  skipped**, and observations are deleted before runs — the FK carries no
  `ON DELETE`, so a run and the sightings it produced age out together or not
  at all.
- Audited `ipam.retention_applied` (system actor, per-table counts) **only when
  something was deleted**: a daily "0" per org is the volume §11.3 excludes
  routine ingestion for.
- §0.16's accepted failure mode covers what this does to Gate H: a subnet whose
  only ancient evidence was collected reads as `assigned` rather than `dormant`,
  which costs a label in one cell of the address grid.
- An observation whose address is contained by **no** `ipam_subnet` is stored
  with `subnet_id = NULL` and surfaced as an **orphan**: `GET /ipam/orphans`
  groups them by inferred /24 (IPv4) or /64 (IPv6) with counts, so
  "24 addresses in 10.7.3.x and no subnet registered" becomes a one-click
  "register this subnet". This is not drift — nothing is wrong with the plan;
  the plan is *absent* — so it is a separate list with its own page.

### 4.3 Drift classes as predicates

Notation: `D` = the `ipam_address` row; `O_cur(A)` = current observations at
address `A`; `dev(D)` = `D.device_id`; `live(dev)` = the agent for `dev` has
`Agent.status='online'` **or** `Agent.last_seen_at >= now() - W_obs` (§0.11's
verified signal).

**Gate E (evidence available for subnet S):** a `net.sweep` run for `S`
succeeded within `W_obs`, **or** some device with a current inventory
observation inside `S` was ingested within `W_obs`.

**Gate L (claimant liveness):** `dev(D) IS NULL` **or** `live(dev(D))`.

**Gate H (evidence has *ever* existed for subnet S):** some `ipam_sweep_run`
for `S` has `state='succeeded'`, **or** some `ipam_observation` has
`subnet_id = S.id` — in both cases **ever**, with no window (§0.16). Gate H is
Gate E's two queries with the `W_obs` predicate dropped, and is computed in the
same pass. It separates `dormant` ("something looked before, nothing can look
now") from the never-looked-at case, which is not dormant and is not drift.

| Class | Severity | Predicate |
|---|---|---|
| `documented_absent` | medium | `D.state ∈ {assigned,reserved}` ∧ `O_cur(D.address) = ∅` ∧ **Gate E** ∧ **Gate L** |
| `present_undocumented` | low | `O_cur(A) ≠ ∅` ∧ (no `D` ∨ `D.state ∈ {free,deprecated}`) ∧ `A` ∉ any `dhcp_pool` range ∧ `A` ∉ any `excluded` range ∧ `A` is in a registered `assignable` subnet |
| `mac_mismatch` | medium | `D.mac IS NOT NULL` ∧ ∃ `o ∈ O_cur(D.address)` with `o.mac IS NOT NULL` ∧ `o.confidence ≥ medium` ∧ `o.mac ≠ D.mac` |
| `claimant_mismatch` | high | `dev(D) = X` ∧ ∃ `o ∈ O_cur(D.address)` with `o.source='inventory'` ∧ `o.device_id = Y ≠ X` |
| `device_moved` | medium | `dev(D) = X` ∧ `O_cur(D.address)` contains no observation for `X` ∧ `X` has a current inventory observation at `A′ ≠ D.address` where `A′` is in a registered subnet |
| `conflict` | high | `\|{o.mac : o ∈ O_cur(A), o.mac IS NOT NULL}\| > 1` ∨ `claimant_mismatch` ∨ (≥2 `ipam_address` rows in different VRFs claim `A` **and** those VRFs share a swept segment) |
| `dormant` | — | `D.state ∈ {assigned,reserved}` ∧ `O_cur = ∅` ∧ **Gate H** ∧ ¬(**Gate E** ∧ **Gate L**) |

Notes that matter:

- **`mac_mismatch` can never fire on ICMP-only evidence** — an ICMP observation
  carries no MAC, so the predicate is unsatisfiable. This is why `confidence`
  is a column and not a comment.
- **`device_moved` is the collapse of two findings into one action.** Left
  apart, the operator sees a `documented_absent` at `.42` and a
  `present_undocumented` at `.57` and has to notice they are the same laptop.
  The class exists so the UI can offer "move the assignment" instead.
- **`dormant` is not a drift class**; it is the bucket that makes the drift list
  readable. It is excluded from counts, excluded from exports of "drift", and
  never published as an event. It gets its own tab, because "37 addresses we
  cannot currently verify" is worth knowing and is not the same information as
  "37 addresses are wrong". **Gate H is why the tab stays worth reading:**
  without it, every address in every subnet nobody has ever swept or ingested
  would be dormant on day one, and the tab would be the whole registry. With
  it, those addresses are simply `assigned` (§3.1 row 5) and produce no
  `ipam_drift` row at all.
- **`documented_absent` is a reclaim *candidate*, never an automatic reclaim.**
  Nothing in this module ever changes `state` by itself. The brief's
  recommendation and this spec agree: flag only.
- **`conflict`'s cross-VRF disjunct: "share a swept segment" means one shared
  `ipam_vlan` row that Gate H holds for.** A VLAN is this schema's name for a
  broadcast domain (§2.3), so two subnets in different VRFs carrying the same
  `vlan_id` are the same wire and one address presented on it twice is a real
  duplicate. `site_id` is *not* the key — a building is not a segment — and a
  subnet with no VLAN recorded shares no segment with anything. Gate H is the
  "swept" half: two rows in a table nothing has ever looked at are a
  documentation question, not a high-severity finding. **This half of the clause
  is the whole clause.** Read as "≥2 VRFs claim `A`", it fires on two unrelated
  routing domains that both use `10.0.0.0/24` — which is the ordinary reason
  VRFs exist — with an empty evidence payload, and §3.2's last predicate then
  turns that false positive into a permanent block on assigning `A` in *either*
  VRF.
- **§3.2's conflict block is lifted by `acknowledge` and by an unexpired
  `suppress`, not only by `resolved_at`.** Both are `ipam.assign`-gated, audited
  acts and neither writes `resolved_at` (§0.8 requires that they survive every
  recompute), so a block reading `resolved_at IS NULL` alone leaves an operator
  who has judged a finding with no way to proceed except deleting a correct row.
  The finding stays open and stays on the drift page; only the block lifts.

### 4.4 The reconcile sweep

Celery beat task `ipam-reconcile-sweep`, daily (86400s), following the
established `*_sweep_all_orgs` shape in `app/worker.py`: enumerate orgs on the
admin engine, then one `org_session` per org.

Per org: recompute every class above, upsert `ipam_drift` on
`(org_id, vrf_id, address, drift_class)`, increment `consecutive_detections`
for items detected again, stamp `resolved_at` on items no longer detected,
reset `consecutive_detections` to 0 on resolution. Publish
`ipam.drift_detected` (§10) **only** for items whose `consecutive_detections`
just reached 2 and which are neither acknowledged nor within
`suppressed_until`.

Acknowledgement (`ipam.assign`, audited) sets `acknowledged_by/at`;
suppression sets `suppressed_until` with a required note. Both survive
recomputation — that is what §0.8's stable key is for.

---

## 5. The `net.sweep` agent capability

### 5.1 Registration

`app/modules/ipam/handler.py` registers
`JobType("net.sweep", NetSweepParams, result_handler=handle_sweep_result)` via
`app.jobs.registry.register`, exactly as `patch/handler.py` does for
`patch.scan` — the module registers into the kernel and forks nothing. Params
and result models are exported to `docs/kernel/wire-protocol/` by
`scripts/export_wire_schema.py` as `NetSweepParams.schema.json` and
`NetSweepDocument.schema.json`; that export is the contract the Go `net.sweep`
capability is written against. **That capability is unbuilt.** It belongs under
`agent/` in this repository (`CLAUDE.md`, commit `a32cead`, which moved the
agent in-tree from the former `it-suite-agent` repository); no cycle has written
it, and this module ships without it.

### 5.2 Params (`extra="forbid"`, matching every other params model)

```
subnet_id:    UUID          # echoed back; binds the result to a registry row
sweep_run_id: UUID          # echoed back; binds the result to its ipam_sweep_run
targets:      list[str]     # explicit CIDRs — the authorized set, computed server-side (§6.2)
scope_digest: str           # sha256 over the sorted, canonical `targets` list
method:       "icmp" | "icmp+arp"
rate_pps:     int  1..200,  default 50
probe_timeout_ms: int 100..5000, default 500
max_targets:  int  1..65536, default 4096
budget_s:     int  10..900,  default 300
```

`targets` is an **explicit enumerated list**, never "the subnet" — the agent is
never asked to decide what it may touch. `expires_at` for a `net.sweep` job is
pinned to **600s** rather than the kernel's 3600s default (`JobEngine.create`
takes `ttl_s`), because it bounds how long a revoked authorization can still be
executed by an in-flight signed job (§6.3).

### 5.3 Dispatch: who sweeps

In order:

1. `subnet.sweep_device_id`, if set, if that device's agent is `online`, and if
   `agents.capabilities` advertises `net.sweep`. Designation is an operator
   act, gated on `ipam.sweep.manage`, and audited.
2. Otherwise **auto-select**, deterministically: among devices whose *current*
   inventory observations include an address inside the subnet, order by
   (`Agent.status='online'` desc, **site-server preference** desc,
   `Agent.last_seen_at` desc, `Device.id`) and take the first. The chosen device
   is written to `ipam_sweep_run.device_id`, so "who probed this segment" is
   answerable after the fact.
3. Otherwise the run is recorded `state='no_agent'` and **nothing is
   dispatched**. This surfaces on the subnet page and feeds Gate E.

`agents.capabilities` is a shipped jsonb column but nothing in this repository
populates it today (it is written at enrollment by the agent). So dispatch
tolerates an empty capability map: it attempts dispatch, and a job that comes
back `refused` (a real `JobStatus`, already in the engine's transition table)
marks that device ineligible for sweeps for 7 days. Capability advertisement is
the fast path; refusal is the correct fallback.

**Site servers — DELIVERED.** This section previously read "there is no
site-server entity in this codebase", and that is no longer true: migration
`0050` created `site_server`, and `docs/modules/site-server/spec.md` §0.1
resolved the modelling question the old note worked around — **a site server IS
a `Device`**, promoted by an operator, so it was already a row in this
function's candidate set and nothing about `Job.target_type` had to change.

The change to `select_sweeper` is **one term prepended to the existing
`ORDER BY`** and nothing else. Untouched: the `EXISTS` inventory-observation
predicate that defines candidacy, the `NOT EXISTS` refusal-cooldown predicate,
`_CANDIDATE_LIMIT`, `_advertises_net_sweep`, the `_designated` short-circuit and
the `no_agent` outcome. There is deliberately **no second selection function**:
two would be two answers to "who probed this segment", diverging on the first
bug fixed in one of them.

```sql
ORDER BY (a.status = 'online') DESC,
         (EXISTS (SELECT 1 FROM site_server s
                  WHERE s.org_id = :org AND s.device_id = d.id
                    AND s.enabled
                    AND EXISTS (SELECT 1 FROM unnest(s.segments) seg
                                WHERE CAST(:prefix AS cidr) <<= seg))) DESC,
         a.last_seen_at DESC NULLS LAST,
         d.id
```

**The preference sits BELOW liveness, and this corrects the site-server spec.**
That document's §6.1 writes the new term *first*, while its own §6.2 fallback
table says an offline site server "loses the preference; an ordinary endpoint
sweeps" because "`(a.status='online') DESC` is already the second term". Both
cannot be true. With preference first, an **offline** site server outranks an
**online** ordinary endpoint and the sweep is addressed to a machine that cannot
receive it: the job sits `queued` until the 600s TTL expires it and the segment
goes unswept for the cycle. §6.2's behaviour is what is built, on this section's
own precedent — `_designated` treats designation as "not liveness" and falls
through when the designated agent is offline, and an *inferred* preference
cannot outrank liveness where an operator's *explicit* designation does not.
`tests/test_ipam_site_server_preference.py::test_liveness_outranks_preference`
is red under §6.1's literal ordering, deliberately.

`<<=` is **containment, not equality**: a branch server declared for
`10.20.0.0/16` is preferred for `10.20.7.0/24`, because a branch server serves a
branch and not one VLAN.

**Preference reorders the candidate set; it never adds to it.** A site server
must still clear the inventory-observation `EXISTS` above. A `site_server` row
is an operator's *claim* and an `ipam_observation` is *evidence*: a claim may
earn preference, but only evidence may earn candidacy — otherwise a typo'd CIDR
in the registry sends a probe onto a segment nobody authorized. No kernel change
was required, then or now.

**RAISED AND NOT FIXED: `_designated` has no subnet predicate.** `_designated`
checks that the designated device's agent is online, advertises the capability,
and has not recently refused — and **never checks that the device is on the
subnet**, even though `select_sweeper`'s own docstring gives the safety argument
for exactly that check: *"A machine that is not on that wire cannot sweep it,
and dispatching anyway emits somebody's probe from the wrong place."* Step 1 of
this section does not state the check either, so it reads as emergent rather
than chosen.

The consequence is real: an operator who designates a machine that has been
moved off the segment gets a probe emitted from the wrong place, silently, and
the auto-select path that *does* carry the guard is skipped entirely by the
short-circuit. Adding `AND EXISTS (observation inside prefix)` to `_designated`
would align designation with auto-selection — but it is a **behaviour change to
shipped code**: a designation that works today would start falling through to
auto-selection. That is IPAM's call to make, not the site-server module's, and
the site-server module works either way. Recorded here rather than fixed.

### 5.4 Result document (`NetSweepDocument`)

```
subnet_id:    UUID    # must equal params.subnet_id
sweep_run_id: UUID    # must equal params.sweep_run_id
scope_digest: str     # must equal params.scope_digest
method:       str
started_at, finished_at: RFC3339
probed_count: int
truncated:    bool                  # budget or max_targets hit
hosts: [ { address: str,            # parses as inet
           mac: str | null,         # parses as macaddr; null for icmp-only
           responded_via: "icmp" | "arp" | "both",
           hostname: str | null } ] # agent-side reverse DNS, display-only
errors: [str]                       # bounded to 20 entries, 200 chars each
```

**There is no RTT field, no per-probe timing, no retry count, and no
`reachable: false` entry.** A host that did not answer simply is not in
`hosts`. Those absences are §0.10 points 2 and 5 expressed as a schema; adding
any of them converts this module into the monitoring product one field at a
time.

`hostname` is display-only: it is agent-side reverse DNS, it is never used as a
reconciliation predicate, and it never matches an address to a device. Identity
comes from MACs and from inventory, both of which have a chain of custody.

**`errors` describes the sweep that happened, not the target list that was
sent.** The agent stops enumerating once it has planned `max_targets` addresses
(§5.5), so a target sitting past that point is never parsed and never
contributes an entry. A malformed or IPv6 target behind the cap is therefore
absent from `errors` rather than reported — deliberately, because an entry
describing an address that was never in the probed set describes work that did
not happen.

This changed in `agent/internal/capabilities/netsweep`: the earlier
implementation expanded every signed prefix in full and applied `max_targets`
afterwards, so it parsed the whole target list on the way past. Recording the
change here because it is a change to what a result document *means*, however
small. **It is safe specifically because `errors` is advisory**:
`handle_sweep_result` validates it against this schema's 20 × 200 bounds and
then never reads it. No observation, no reconciliation, no `sweep_run` column
and no acceptance decision depends on it. **If that ever stops being true — if
the server begins consuming `errors` — this bound has to start parsing the tail
again**, and that is the falsifier for the paragraph above.

`truncated` is unaffected: it still means "the plan held more addresses than
were probed", and the agent asks its enumeration for `max_targets + 1` so the
comparison that produces the flag stays satisfiable. A sweep cut short by the
cap still reports `truncated: true`; one whose subnet is exactly `max_targets`
addresses still reports `false`.

### 5.5 The idle budget (hard requirement, `docs/05-agent-spec.md`)

The contract the `net.sweep` capability satisfies. It lives at
`agent/internal/capabilities/netsweep`, and the numbers below are **measured by
`budget_test.go` on every push**, not asserted — per CLAUDE.md, a footprint
claim without a gate that runs is a design target and must be described as one.

- **Dormant by default.** The capability is a compiled-in package that
  allocates nothing until a `net.sweep` job arrives, and releases its socket,
  worker pool and result buffer when the job completes. No resident scanner, no
  background thread, no cache. *Falsifier 1: any allocation or goroutine on the
  registry-lookup path. Measured: 0 allocs/op, 0 B/op.*
- **No local schedule.** §0.9 — the cadence is server-side. The agent's timer
  wheel gains no `net.sweep` entry.
- **Bounded by construction, and the bound is on the work.** `rate_pps` (≤200),
  `max_targets` (≤65,536) and `budget_s` (≤900) are all in the **signed**
  params, so the ceiling on what a sweep can cost is set by the server and
  covered by the signature. A sweep that hits its budget returns
  `truncated: true` rather than running longer.

  **`max_targets` bounds the enumeration itself, not only the result.** This is
  called out because the first implementation got it wrong in a way nothing
  caught: it expanded every address of every signed prefix and applied the cap
  afterwards. Measured before the fix — a `/12` cost **295,863,936 bytes** and a
  `/8` cost **5,035,381,632 bytes** to probe at most 65,536 addresses, against
  `docs/05-agent-spec.md`'s "low-tens-of-MB". Two orders of magnitude, and
  invisible to a resident-memory reading because the peak is transient: the plan
  is freed before the handler returns. After: 1,007,456 B and 18,548,400 B.

  *Falsifier: planning N addresses out of a large prefix costing materially more
  than planning N out of a small one. Gated dimensionlessly — the same bound
  against two prefixes 256× apart must cost the same. Clean is a ratio of 1.00;
  the unbounded implementation was 294.*

  The probed set is unchanged by this: stopping at N keeps the same first N
  addresses in the same order that truncating to N did. The one visible
  difference is `errors` for targets past the cap, in §5.4 above.
- **Raw sockets only inside the job.** ARP requires them and the agent already
  runs as SYSTEM/root; the privilege is not new. The socket is opened at job
  start and closed at job end. *Falsifier: a goroutine or an unclosed emitter
  surviving a sweep; a bound TCP port during one.*
- **Nothing accumulates across sweeps.** *Falsifier 3: 100 sweeps retaining
  materially more than 1, compared across a hundredfold spread because a
  tenfold one was measured not to catch a planted per-tick leak.*
- **Nothing regresses idle.** If the capability cannot be implemented without a
  resident component, that is a spec violation to raise, not to work around.

**What is not asserted: `~0% CPU`.** A shared runner's variance exceeds the
signal, so a threshold loose enough not to flake is loose enough to pass a real
regression. It stays a design target, and is described as one here rather than
gated by a test that would be skipped. One residual is known and stated rather
than left to be found: enumeration still costs an iteration per already-seen
address, so a list of heavily overlapping prefixes burns CPU without producing
addresses. It is bounded by `max_targets` rather than by the prefix — worst case
~2 × `max_targets` iterations per target instead of the prefix's full size.

Nothing in §5 requires a kernel change: it is a job type, a params model, a
result handler and a Celery beat entry — the same four things `patch.scan` is.

---

## 6. Sweep governance and untrusted input

### 6.1 Authorization

Deny by default (§0.12). `ipam_scan_authorization` rows are created and revoked
only by a holder of **`ipam.scope.manage`** — an admin-tier permission whose
description says so in the same terms `notification.admin` uses, because it is
the same class of grant: it decides where the platform will emit traffic.

### 6.2 The authorized target set

At dispatch, for subnet `S`:

```
targets = ( S.prefix ∩ ⋃{ a.prefix : a ∈ active authorizations in S.vrf } )
          minus ⋃{ r : r ∈ S's excluded ranges }
```
expressed as a **canonically sorted, minimal list of CIDRs**, capped at
`max_targets`. `scope_digest = sha256(canonical_json(targets))`.

- An empty target set is **never dispatched** — the run records `state='refused'`
  with `error='not authorized'`.
- The set is placed in the **signed** params, so what the agent may touch is
  covered by the job signature (invariant #1). The agent does not consult any
  local configuration to decide scope; there is nothing for it to consult.
- An authorization broader than the subnet cannot widen anything: the
  intersection with `S.prefix` is taken first.

### 6.3 "Observe-passive", in code terms

For a segment with **no** active authorization:

**Permitted:**
- Ingesting inventory-derived observations for addresses in it (§4.1) — the
  device reported about itself; this module emitted no packet.
- Creating, editing and deleting `ipam_subnet`, `ipam_range` and
  `ipam_address` rows in it. Documenting is not probing.
- Every drift class in §4.3, computed on inventory evidence alone. Gate E is
  satisfied by inventory, not only by sweeps — which is exactly why an
  unauthorized segment still gets useful reconciliation.
- Next-free, allocation, utilisation, import, export, search.

**Forbidden:**
- `create_sweep()` raises `IpamAuthorizationError` **before** calling
  `JobEngine.create`, so for an unauthorized target no job row is created, no
  canonical payload is built, and **no signature is ever produced**. The
  refusal happens on the near side of the signing boundary.
- Accepting sweep observations for it (§6.4).
- Any probing by the control plane itself. The server never sends a packet at
  an endpoint; that is invariant #2's posture and this module does not become
  the exception.

The distinction in one sentence: **passive is recording what devices tell us
about themselves; active is putting packets on someone's wire — authorization
gates the second only.**

Revocation cannot recall an already-signed in-flight job. The exposure window is
therefore the job's TTL, which §5.2 pins at 600s for exactly this reason, and
`publish_revoke` still drops a revoked *agent* immediately.

### 6.4 An out-of-scope result is rejected **whole**

The result handler runs inside the job's tenant transaction
(`JobEngine.complete`'s module extension point). It:

1. Recomputes the permitted set **from the job's own signed `params.targets`**,
   not from the current database. The check is against exactly what was
   authorized when the job was signed — recomputing from live authorizations
   would let a scope granted *after* dispatch retroactively bless results.
2. Verifies `scope_digest`, `subnet_id` and `sweep_run_id` echo the params.
3. Verifies `len(hosts) <= params.max_targets`, that every `address` parses,
   that its family matches the subnet, and that every non-null `mac` parses.
4. Verifies **every** `hosts[].address` is contained by `params.targets`.

Any failure ⇒ `IpamSweepRejected` is raised, which flips the job to `failed`
and — because the handler runs in the job's transaction — writes **zero**
observations. Two audit events are recorded: `ipam.sweep_rejected` and, for a
scope violation specifically, `ipam.sweep_out_of_scope` with
`actor_type='agent'`, the device id, the offending count, and the first 10
offending addresses (bounded). A `ipam.sweep_rejected` **domain event** is
published (§10) so it can be routed to an operator.

**Why the whole result and not just the bad rows.** This follows the shipped
precedent in `app/modules/security_logs/ingest.py`, which rejects an entire
shipped batch on a single event outside the taxonomy. Filtering optimises for
keeping data; rejecting optimises for *detecting the compromise*. An agent
returning addresses it was not authorized to probe is either buggy or
controlled by someone else, and quietly dropping the bad rows keeps its good
rows flowing while hiding the tell. The cost accepted is real and small: one
malformed address wastes one sweep, and the next scheduled run recovers it.

Three further properties, stated because they are the ones that get lost in a
refactor:

- `org_id` on every written observation comes from the **job**, never from the
  payload. The result document has no org field to be believed.
- `device_id` on a sweep-derived observation is **always NULL**. A sweep sees
  addresses and MACs; it does not see identity, and letting it assert one would
  be a compromised agent's cheapest way to re-point an assignment.
- Ingestion writes `ipam_observation` rows only. It **cannot** write
  `ipam_address.state`, `device_id`, or `assigned_by` — §0.6's split is the
  control, and §13 tests it directly.

### 6.5 A successful sweep also records assets (added 2026-08-26)

On the operator's ruling of 2026-08-26
(`docs/modules/asset-inventory/spec.md` §3), a sweep that passes every check
above then creates one `devices` row per host it reported, with `source =
discovery` and `provenance = sweep`. This is the one place where `net.sweep`
writes outside IPAM, and three properties keep it inside this module's rules:

- **It runs last.** Every §6.4 check has passed and the observations are
  already written, so a rejected result creates nothing — an agent reporting
  outside its authorization cannot write inventory rows for addresses nobody
  authorized it to touch.
- **§0.6's split is untouched.** It still cannot write `ipam_address`, and the
  asset row asserts no address *assignment*. It records that the estate
  contains a machine, which is the inventory's question, not IPAM's.
- **`org_id` comes from the job**, exactly as it does for the observations, and
  every already-recorded lookup carries an `org_id` predicate of its own —
  a missing one does not fail loudly, it silently concludes that another
  tenant's device already covers the address and writes nothing.

Identity on a re-sweep is the MAC where the sighting has one and the address
otherwise; a matched row that is itself `provenance = sweep` and has no agent
has its `primary_ip` corrected, and a row an operator or an agent owns is never
edited by a sweep. The asset-inventory spec §3 carries the full reasoning.

---

## 7. Boundary with the monitoring product

Restated as a contract, since it is the boundary most likely to erode:

**This module does not ship, and must not acquire:** up/down state, uptime or
availability percentages, latency or RTT, jitter, packet loss, per-address
polling, any polling interval under one hour, response-time history, SNMP of
any kind (`GET`, walk, or trap), interface counters, bandwidth, topology or
link discovery, per-address alerts, or a status dashboard tile expressing
reachability.

**It ships:** a last-seen age per address, whose only consumers are the drift
predicates in §4.3 and the `status` derivation in §3.1.

The later-phase SNMP item in the brief (switch ARP/MAC tables) remains later
phase and, when specced, is **address↔MAC↔port enrichment only** — a second
observation source with `confidence='medium'`, subject to §6's authorization
model. Performance polling stays on the other side of the line.

---

## 8. Import, export, and absorbing documentation's network records

### 8.1 CSV/Excel import with column mapping (a v1 requirement, per the brief)

Two steps, because a one-shot import into a plan of record is how a plan of
record gets destroyed:

- `POST /ipam/import/preview` — multipart CSV (≤10 MB, ≤50,000 rows) plus a
  `{csv_column: ipam_field}` mapping. Returns per-row parse results, per-row
  validation errors, and a **diff** against the current registry classified as
  create / update / conflict. Nothing is written.
- `POST /ipam/import/apply` — the digest the preview returned, plus the same
  file and the same mapping, applied in **one** transaction. Audited
  (`ipam.import_applied`) with the row counts, the mapping and the file's
  sha256, so the import is reproducible from the audit log.

**What the digest is, precisely.** It is `sha256` over the **mapping and the
file together** (`importer.preview_digest`), not over the file alone — the
mapping decides which column becomes `asset_label` and which becomes
`hostname`, so the same bytes under two mappings are two different imports, and
a digest over the bytes alone accepted the second one carrying the first one's
value. And it is a **consistency check over one request's inputs, not proof
that a preview happened**: no preview is persisted server-side, so a caller that
skips step one computes the digest itself. That is a judged trade, not an
oversight — `ipam.manage` is admin-tier (§11.2), the caller is authorized to
write every row the apply writes, the apply is one transaction, every input row
comes back with a verdict, and the audit entry makes a never-previewed import
fully visible afterwards. **If import ever becomes reachable by a
lower-privilege role, the preview must be persisted and `apply` bound to the
stored record.** The frontend clearing the preview when the file or mapping
changes is an affordance, not a control (invariant #6).

Rules: an imported address with a claimant lands `state='assigned'`,
`origin='imported'`, `allocation_method` from the mapping or `unknown`.
Unparseable rows are **reported, never silently skipped** — silent skipping is
how an import "succeeds" and loses a third of the sheet. Import never creates
authorizations and never enables sweeps.

### 8.2 Export

`GET /ipam/export/subnets.csv`, `/addresses.csv`, `/drift.csv` — streamed,
flat, Power-BI-friendly columns, `text/csv`, gated on `ipam.read`, following
the existing convention of `app/modules/inventory/api.py`'s device/software
exports.

### 8.3 The documentation absorption (§0.14)

- `POST /docs/records` refuses `record_type='network'` **unconditionally**
  (§0.18, §14.4) — the refusal lives in
  `documentation/service.py::create_record`, not in `RecordCreate`'s regex, so
  `network` stays in `RECORD_TYPES` and existing rows stay readable, editable
  and archivable.
- `POST /ipam/import/doc-records` (`ipam.manage`, `?dry_run=true` default)
  converts each non-archived `network` record whose `fields.cidr` parses:
  `name ← record.name`, `prefix ← fields.cidr` (normalised via
  `ip_network(..., strict=False)` — the same call the documentation validator
  already makes), `vlan_id ←` the org's VLAN with `vid = fields.vlan` if one
  exists (else left null and reported), `gateway ← fields.gateway` if it parses
  and falls inside the prefix, `dns_servers ← fields.dns` split on commas,
  `kind='assignable'`, `doc_record_id ←` the source row. The source record is
  then **archived**, not deleted, so `doc_links` and `doc_versions` survive
  (`doc_versions.record_id` is a real FK).
- Rows that do not convert — unparseable CIDR, a prefix already registered, a
  gateway outside its prefix — are returned in a `skipped` list with reasons.
  The operator fixes or ignores them; nothing is guessed.
- Audited: `ipam.import_previewed` / `ipam.import_applied` with counts.

---

## 9. API (`/api/v1/ipam/...`)

Read = `ipam.read` unless noted. All routes carry a `require_permission(...)`
dependency, which is what makes RBAC completeness derivable from the router
(`app/core/rbac.py`'s `route_permission`) rather than from a list someone has
to remember to update.

| Method | Path | Permission |
|---|---|---|
| GET/POST | `/vrfs`, `/sites`, `/vlans` | read / `ipam.manage` |
| GET | `/subnets` (`?vrf&site&vlan&contains=<ip>&q=`, tree or flat) | `ipam.read` |
| POST | `/subnets` | `ipam.manage` |
| GET/PUT/DELETE | `/subnets/{id}` | read / `ipam.manage` |
| GET | `/subnets/{id}/addresses` (the grid; refused above the ceiling) | `ipam.read` |
| GET | `/subnets/{id}/utilization` | `ipam.read` |
| GET | `/subnets/{id}/next-free?count=` | `ipam.read` |
| POST | `/subnets/{id}/allocate` | `ipam.assign` |
| GET/POST/DELETE | `/subnets/{id}/ranges` | read / `ipam.manage` |
| GET | `/addresses/{id}` | `ipam.read` |
| POST | `/addresses` (create/reserve), `PUT /addresses/{id}` | `ipam.assign` |
| POST | `/addresses/{id}/release` | `ipam.assign` |
| GET | `/addresses/{id}/history` (audit projection, §0.13) | `ipam.read` |
| GET | `/search?q=` (IP, CIDR, MAC, hostname, device) | `ipam.read` |
| GET | `/drift` (`?class&severity&subnet&include_resolved`) | `ipam.read` |
| POST | `/drift/{id}/acknowledge`, `/drift/{id}/suppress` | `ipam.assign` |
| GET | `/orphans` | `ipam.read` |
| GET | `/scan-authorizations` | `ipam.read` |
| POST/DELETE | `/scan-authorizations`, `/{id}` | **`ipam.scope.manage`** |
| POST | `/subnets/{id}/sweep` (dispatch now) | `ipam.sweep.run` |
| PUT | `/subnets/{id}/sweep-settings` (enable, device, interval) | `ipam.sweep.manage` |
| GET | `/subnets/{id}/sweep-runs` | `ipam.read` |
| POST | `/import/preview`, `/import/apply` | `ipam.manage` |
| POST | `/import/doc-records` | `ipam.manage` |
| GET | `/export/{subnets,addresses,drift}.csv` | `ipam.read` |

`/search` accepts a bare IP (containment: which subnet, which record, which
observations), a CIDR (everything inside it), a MAC (every address that MAC has
been seen at — the query that answers "where did this printer go"), a hostname,
and a device name. It is one endpoint because operators do not know in advance
which of those they are holding.

Cross-org ids return **404**, never 403 — the existing convention, and RLS
makes it the natural outcome rather than a hand-written check.

---

## 10. Events

Two new entries in the kernel event registry `app/core/events.py`
(`EVENT_TYPES` is currently ticket-only — this is an additive change, §14.3):

- **`ipam.drift_detected`** — published by the reconcile sweep when an item
  reaches `consecutive_detections = 2` (§0.8), unacknowledged and unsuppressed.
  Payload: `{drift_class, severity, confidence, address, subnet_id,
  subnet_prefix, subnet_name, device_id?, drift_id}`. Deliberately **no
  aggregate**: one event per item, deduplicated by the two-detection rule
  rather than by batching, so a rule can route `conflict` differently from
  `documented_absent`.
- **`ipam.sweep_rejected`** — published by §6.4 when a result is rejected.
  Payload: `{device_id, subnet_id, sweep_run_id, reason,
  offending_count, sample: [≤10 addresses]}`. This is a security signal, not an
  operations one.

Both are published on the caller's transaction via `app.core.events.publish`,
which writes the kernel transactional outbox `domain_events` — so an event can
never describe a fact that rolled back.

**Routing constraint, verified against shipped code.** Notification's
`ck_rule_recipient_kind` restricts `recipient_kind` to
`queue_members | assignee | requester | channel_default`, and `assignee` /
`requester` / `queue_members` all resolve from a **ticket-shaped** payload
(`app/modules/notification/routing.py`). IPAM events carry no ticket, so in v1
they are routable **only** through `channel_default` rules — a webhook or an
ops mailbox. That is a real limitation and it is stated rather than papered
over by inventing a fifth recipient kind, which would be this module changing
shared notification structure. An IPAM→ticket path is deferred (§15) and, when
it arrives, belongs in the same alert→ticket machinery Phase 16 defines.

Neither event is added to `STAFF_ONLY_EVENT_TYPES` — that set is about not
mailing a ticket's external requester, and these events have no requester.

---

## 11. Tenancy, RBAC, audit

### 11.1 Tenancy (invariant #5)

Every table in §2 carries `org_id` with `ENABLE + FORCE` RLS and a
`tenant_isolation` policy carrying both `USING` and `WITH CHECK`. All service
work runs inside `org_session`. The Phase 0 completeness test that walks
`pg_tables` / `pg_policies` and fails on any `org_id`-bearing table without a
forced policy covers all **ten** tables automatically; §13 adds a behavioural
cross-org test per table on top, since the structural test proves a policy
exists, not that it works.

(This read "eleven" as first written. **§2 is right and the count was wrong** —
there are ten, enumerated there and again here so a builder never has to
recount looking for a table that does not exist: `ipam_vrf`, `ipam_site`,
`ipam_vlan`, `ipam_subnet`, `ipam_range`, `ipam_address`, `ipam_observation`,
`ipam_drift`, `ipam_scan_authorization`, `ipam_sweep_run`. Nothing was dropped
from the design to make the numbers agree; §0.13 already rules out an eleventh
by putting assignment history in the audit log.)

Two module-specific tenancy notes:

- **Observation ingestion takes `org_id` from the job/device, never from the
  payload** (§6.4). The result documents have no org field.
- **Nothing in this module runs on the `portal_user` role.** The end-user portal
  has no IPAM surface, so no grants are added to it — the least-privilege
  posture the portal spec establishes stays untouched.

### 11.2 RBAC (invariant #6)

Six new permission codes, added to `app/core/rbac.py`'s `PERMISSIONS` and
seeded by the migration:

| Code | Meaning |
|---|---|
| `ipam.read` | View subnets, addresses, drift, utilisation, sweep runs, exports |
| `ipam.assign` | Assign / reserve / release addresses; acknowledge and suppress drift |
| `ipam.manage` | Create and edit VRFs, sites, VLANs, subnets and ranges; run imports |
| `ipam.sweep.run` | Dispatch a `net.sweep` job now, within already-authorized scope |
| `ipam.sweep.manage` | Enable sweeps on a subnet, designate the sweeping agent, set the interval |
| `ipam.scope.manage` | **HIGH PRIVILEGE.** Grant or revoke authorization for the fleet to probe a prefix. This grant decides where this platform emits network traffic — admin tier, never technician. |

`ipam.sweep.run` and `ipam.scope.manage` are deliberately **separate**: running
a sweep inside an already-authorized scope is routine technician work, and
deciding what may be scoped at all is not. Collapsing them would let anyone who
can run a scan also decide what may be scanned, which is the whole control.

Default grants: **org-admin** all six. **Technician** `ipam.read`,
`ipam.assign`, `ipam.sweep.run`. **Auditor** and **read-only** `ipam.read`.
Nobody but org-admin gets `ipam.scope.manage` or `ipam.manage` by default.

### 11.3 Audit (invariant #4)

Written through `audit_service.record` inside the tenant transaction:

| Action | Target | Notes |
|---|---|---|
| `ipam.subnet_created` / `_updated` / `_deleted` | `ipam_subnet` | includes prefix, vrf |
| `ipam.vrf_*`, `ipam.site_*`, `ipam.vlan_*`, `ipam.range_*` | respective | |
| `ipam.address_assigned` | `ipam_address` | from-state, to-state, claimant, method |
| `ipam.address_released` | `ipam_address` | previous claimant |
| `ipam.address_reserved` / `ipam.address_state_changed` | `ipam_address` | from/to |
| `ipam.scope_authorized` / `ipam.scope_revoked` | `ipam_scan_authorization` | prefix, actor, note — **the high-privilege pair** |
| `ipam.sweep_settings_changed` | `ipam_subnet` | enable/disable, device, interval |
| `ipam.sweep_dispatched` | `device` | run id, job id, target count, `scope_digest`, trigger — mirrors `patch.scan_dispatched` |
| `ipam.sweep_rejected` | `device` | `actor_type='agent'`, reason |
| `ipam.sweep_out_of_scope` | `device` | `actor_type='agent'`, offending count + bounded sample |
| `asset.auto_created` | `device` | `actor_type='system'`, `actor_id=NULL` — one per row a sweep recorded (§6.5), with address, MAC, confidence and sweep run |
| `asset.auto_readdressed` | `device` | `actor_type='system'` — a swept row whose MAC reappeared at a new address |
| `ipam.drift_acknowledged` / `ipam.drift_suppressed` | `ipam_drift` | note |
| `ipam.import_previewed` / `ipam.import_applied` | `organization` | counts, file digest |

**Not audited:** reads, and routine successful sweep/inventory observation
ingestion. The rows a sweep *creates* (§6.5) are a different matter and are
audited individually: an asset appearing in the inventory changes what the
platform believes the estate contains, and every later report, sweep and
licence count reads it. Three hosts therefore produce three `asset.auto_created`
events and no observation events. Volume, and `jobs.dispatched` plus the terminal-state audit already
record that the job happened — the same reasoning inventory used for a routine
successful sweep. A **rejected** ingest is always audited.

No credential, secret, or vault reference is involved anywhere in this module
(invariant #3 is satisfied vacuously — worth stating so a later "sweep with SNMP
community strings" idea is recognised as a change that needs the vault, not a
small extension).

---

## 12. Frontend (`frontend/src/modules/ipam/`)

- **Subnet tree** (`/ipam`) — the derived hierarchy (§0.2) with per-node
  utilisation bar, site/VLAN chips, and a drift badge. Filters by VRF, site,
  VLAN. Container prefixes render without a utilisation bar.
- **Subnet detail** — the classic address grid (paginated; replaced by a
  "too large to enumerate" panel with counts above the ceiling, §0.4), each row
  showing the derived `status`, claimant, MAC, hostname, and last-seen **age**
  (never a timestamp presented as liveness). Ranges shown as banded rows.
  Actions: allocate, assign, reserve, release, edit. Sweep panel showing
  authorization state, designated agent, last run and its outcome — including
  `no_agent`.
- **Drift page** — tabbed by class, with `dormant` and `orphans` as *separate*
  tabs outside the drift count, because §0.11's whole point is that they are
  not the same information. Each item shows its evidence (`detail`), and offers
  the class's action: document, release, move the assignment, acknowledge,
  suppress with a note.
- **Global search** — one box, `/ipam/search` (§9).
- **Device detail** gains an **Addresses** tab: documented addresses for the
  device plus its current observations, so the disagreement is visible where
  someone is already looking.
- Mutation UI is hidden without the permission; the server enforces regardless
  (invariant #6 — the frontend never gates security).

---

## 13. Testing (test-first; security paths explicit)

- **Data model:** containment and overlap queries return correct results across
  IPv4 and IPv6; the GiST index is used (`EXPLAIN` assertion) for containment;
  duplicate `(org, vrf, prefix)` is refused; a gateway outside its prefix is
  refused; `sweep_interval_hours = 0` is refused by the CHECK; the
  exactly-one-claimant CHECK refuses both-and-neither.
- **Uniqueness keys that contain an optional value (§0.15)** — these test the
  *upsert path*, not the constraint, because a partial or generated-column key
  can be present and still be inferred wrongly:
  - **Two ICMP-only observations of the same address, upserted in sequence,
    yield exactly ONE row with `sighting_count = 2`** and a bumped
    `last_seen_at`. Assert the row count and the counter — not the absence of
    an error. With the key on nullable `mac` instead of `mac_key`, this
    observes **2 rows, each with `sighting_count = 1`**, which is the whole
    defect: it fails nothing, it just turns §2.7 into a probe log.
  - The same address observed by `icmp` and by `arp` still yields two rows —
    `source` is in the key on purpose, and a fix for the above that collapses
    them is a different bug.
  - An `arp` observation whose MAC *changes* yields a second row, and both are
    current, so `mac_mismatch` can see them (§4.3).
  - A second **org-wide** VLAN 10 (`site_id IS NULL`) is refused; VLAN 10 at
    each of two different sites is accepted; the site-scoped and org-wide
    indexes are both exercised through the service, not by raw insert.
- **Hierarchy:** inserting a supernet between existing rows re-parents exactly
  the contained children and nothing else; concurrent overlapping sibling
  inserts serialise under the advisory lock and the second is refused.
- **Sparseness / ceiling:** a /24 enumerates; a /12 is refused with 422;
  utilisation above the ceiling returns counts with a null percentage; an IPv6
  subnet refuses next-free but accepts address records and produces drift.
- **Allocator:** next-free skips network/broadcast, DHCP pools, reserved and
  excluded ranges, existing rows, **and currently observed addresses**; `/31`
  and `/32` behave per RFC 3021; two concurrent `allocate` calls return
  different addresses.
- **Transitions:** each row of §3.2's table, positive and negative; static
  assignment inside a `dhcp_pool` returns 409; **no ingestion path can change
  `state`** (assert directly against a sweep and an inventory ingest that both
  target an assigned address).
- **Reconciliation:** one test per drift class, each constructed from a real
  documented row plus a real observation; **and the negative tests that matter
  most** — a device whose agent has been offline past `W_obs` produces
  `dormant`, not `documented_absent`; a subnet with no evidence in the window
  produces no absence drift at all; an ICMP-only observation can never produce
  `mac_mismatch`; a single detection publishes no event and the second one does;
  acknowledgement and `first_detected_at` survive recomputation; a resolved item
  is stamped, not deleted. **Gate H (§0.16):** an assigned address in a subnet
  that has never been swept and never carried an observation renders `assigned`
  and produces **no** `ipam_drift` row; the same address after one successful
  sweep run that then goes stale renders `dormant`. The two fixtures must
  differ only in the sweep run's existence — if a test can pass with Gate H
  deleted, it is testing the ordering and not the gate.
- **Inventory parsing:** §14.1's block produces one observation per address;
  loopback, link-local and multicast entries **inside the block** produce none;
  a NIC whose `identifier` is not a MAC produces an observation with
  `mac = NULL`; the removed `attributes.ip` key produces **nothing**, asserted
  with a well-formed NIC in the same call as a positive control; **a malformed
  NIC attribute block does not fail the `inventory.collect` job** (the whole
  ingest still succeeds).

  *Every "produces none" fixture must express itself in the key the parser
  actually reads.* Phrased against `attributes.ip` those assertions stay green
  no matter what the parser does with an address, because the key is never
  touched — they would be tests of nothing. This is not hypothetical: it is
  what the 2026-08-05 removal of that key exposed across this suite.
- **The hook's two layers (§0.17), tested separately** — a test of either alone
  passes against an implementation missing the other:
  1. *Layer 1, the SAVEPOINT.* Register a deliberately raising post-ingest hook
     — one that raises a **database** error, not a `ValueError`, since that is
     the case a bare `except` cannot handle — and assert the job is
     `succeeded` **and** the device's software and `hardware_components` rows
     are present. Deleting the `begin_nested` (keeping the `except`) must make
     this go red on the *rows*, not merely on the job state.
  2. *Layer 2, the total parser.* `nic.observations_from_components` returns
     `[]` for every malformed shape, called directly, with no session and no
     hook in play. Assert `== []`; "no exception was raised" is not an
     observation.
- **Sweep governance (the security tests):** dispatch to an unauthorized
  subnet raises before `JobEngine.create` and creates **no job row**; the signed
  params contain the intersected target list and a matching `scope_digest`; a
  result containing one out-of-scope address fails the job, writes **zero**
  observations, and records both audit actions; a result with a mismatched
  `scope_digest`, a `hosts` array over `max_targets`, an unparseable address, or
  a wrong address family is rejected the same way; a sweep observation never
  sets `device_id`; `org_id` is taken from the job even when the payload tries
  to supply one.
- **RBAC:** the router-derived completeness test covers every new route; a
  technician cannot create a scan authorization (403) but can run a sweep in an
  authorized scope; a read-only user cannot assign; cross-org ids 404.
- **Tenancy:** a behavioural cross-org read/write test per table.
- **Audit:** each action in §11.3 fires exactly once with the expected
  target and metadata; the address-history projection returns only that
  address's events and is reachable with `ipam.read` alone.
- **Import:** preview writes nothing; apply is transactional; unparseable rows
  are reported not skipped; the doc-record converter archives sources, sets
  `doc_record_id`, and lists skips with reasons.
- **Frontend:** vitest over the `status` derivation and the utilisation
  formatter; `tsc` strict + build.

---

## 14. Changes outside this module (surfaced, not designed around)

### 14.1 Inventory NIC attribute contract — the one hard prerequisite (BUILT)

**Status: built on both sides.** The agent emits this block
(`agent/internal/capabilities/inventory/nic.go`) and `nic.py` reads it. It is
also now the **only** address key: the transitional `attributes.ip` fallback was
removed from both sides on 2026-08-05 (OQ-2). Everything below is the contract
as shipped.

`ComponentDoc.attributes` is `dict[str, Any]` and the exported wire schema says
`"additionalProperties": true`. There is **no** contract for how a NIC reports
its addresses in the schema, and this module's entire passive stream depends on
one — so this block is the contract, and it is additive to inventory's
ingestion, which it does not touch:

```jsonc
// hardware.components[] where type == "nic"
{ "type": "nic",
  "identifier": "a4:bb:6d:11:22:33",       // MAC where the OS exposes one
  "attributes": {
    "name": "Ethernet0",
    "mac": "a4:bb:6d:11:22:33",
    "addresses": [
      { "address": "10.1.2.42/24", "family": "ipv4", "origin": "dhcp" },
      { "address": "fe80::1%4/64", "family": "ipv6", "origin": "link_local" }
    ]
  } }
```

- `origin ∈ {static, dhcp, slaac, link_local, unknown}` — this is what makes
  §3.2's `allocation_method` real instead of permanently `unknown`, and **it is
  read**: `nic.py` normalises it into `NicObservation.allocation_origin`,
  migration 0051 stores it on `ipam_observation`, and §3.2 derives the method
  from it. An `origin` outside the enum does **not** cost the address its
  observation — dropping it would be tightening inbound
  (`docs/kernel/wire-protocol/README.md`), and the enum is the agent's contract
  to honour, which `agent/internal/capabilities/inventory/origin.go` does. What
  it costs is the FIELD, which reads `unknown`: normalising is not refusing, and
  it is also what keeps an unrecognised string away from the column's CHECK,
  where an `IntegrityError` on the inventory result path is a failure §0.17
  layer 2 structurally cannot contain.
- **The origin is read PER ENTRY, not per NIC.** One interface routinely holds a
  DHCP v4 lease beside a SLAAC v6 address — which is exactly what the example
  block above shows.
- **There is no `attributes.ip`, and no fallback.** A NIC that omits
  `addresses[]` produces no observation. That is deliberate: the fallback
  existed to protect a control plane deployed before this block, and none
  exists — the platform is not live (OQ-2, closed 2026-08-05). Adding a second
  address key back is a wire-protocol decision, versioned and reviewed, not a
  leniency knob.
- IPAM parses the block **leniently and totally**, so a malformed NIC degrades
  to "no observation" and can never fail an `inventory.collect` job. That
  guarantee is independent of which keys are read and survived the fallback's
  removal untouched: removing a *key* is not removing the *tolerance*.
- The block moves **no byte** of
  `docs/kernel/wire-protocol/InventoryDocument.schema.json`, because
  `attributes` is an untyped object. The usual control ("the exported schema
  changed, so somebody looked") therefore does not apply to anything in this
  section, in either direction — the compatibility analysis is the only control
  there is.

### 14.2 Kernel: none required

Job-type registration, a params model, a result handler, a Celery beat entry, a
migration and API routes are all defined extension points. `net.sweep` uses the
same four hooks `patch.scan` uses. Two adjacent facts recorded so they are not
rediscovered: `JobEngine.create` hard-sets `Job.target_type = "device"`, so a
job target is always a `Device` (§5.3's site-server note), and `JobStatus.refused`
already exists, which is what makes §5.3's capability fallback work without a
kernel change.

### 14.3 Kernel event registry — additive

`app/core/events.py`'s `EVENT_TYPES` is currently ticket-only; §10 adds
`ipam.drift_detected` and `ipam.sweep_rejected`. `publish` raises
`UnknownEventType` for anything unregistered, so this is required, small, and
safe. No `STAFF_ONLY_EVENT_TYPES` entry and therefore no new notification CHECK
constraint. The `channel_default`-only routing limitation is §10's, not a
change request.

### 14.4 Documentation module — two small changes, in two files

**File locations corrected.** This section originally placed both changes in
`app/modules/documentation/api.py`. Verified against the code: they are in
different files, and a builder following the original would have edited a
symbol that is not there.

- `ipam_subnet` and `ipam_address` are added in **two** places:
  `_LINK_TARGET_MODELS` at `app/modules/documentation/service.py:33` (the
  dict that resolves a link target to an ORM model — currently `device`,
  `device_group`, `credential`, `record`), and the `target_type` pattern at
  `app/modules/documentation/api.py:60`
  (`LinkCreate.target_type: str = Field(pattern="^(device|device_group|credential|record)$")`).
  Adding either alone is a half-change: the pattern without the dict gives a
  404 from `LinkTargetNotFound`, the dict without the pattern gives a 422.
  (`doc_links.target_type` is `String(20)`; both values fit.)
- `POST /docs/records` refuses `record_type='network'` **unconditionally**
  (§0.14, §0.18), in `documentation/service.py::create_record`. `network`
  remains a valid stored `record_type` in `documentation/schemas.py:13` —
  migrations are forward-only and existing rows must keep working. This is a
  behaviour change to a shipped module: a create call that succeeds today
  starts returning an error naming `POST /api/v1/ipam/subnets`.

### 14.5 Deployment — no change

No new Postgres extension (`inet`, `cidr`, `macaddr`, `inet_ops` GiST are all
core), no new service, no new compose entry. One new Celery beat schedule entry
per §4.4 and §5.3 inside the existing worker.

### 14.6 Inventory — a post-ingest hook module inventory owns (§0.17)

Added with the §0.17 amendment, because §4.1 said observations are derived
"after each `inventory.collect` result" without saying through what.

- **New: `app/modules/inventory/hooks.py`** — two functions and a module-level
  list. `register_post_ingest_hook(fn)` appends;
  `run_post_ingest_hooks(session, device, doc)` runs each hook inside
  `async with session.begin_nested()`, catching `Exception` and logging it with
  the device id. It returns nothing and never raises.
- **Modified: `app/modules/inventory/handler.py`** — one call to
  `run_post_ingest_hooks`, placed after `ingest_inventory` and before
  `queue_ui_event`, so the UI event still reflects a completed ingest and the
  hook's writes are inside the same tenant transaction. That call plus the new
  file is the entire diff to inventory; nothing in its ingestion path changes.
- **IPAM side:** `ipam.register(app)` calls `register_post_ingest_hook` with
  its own function. IPAM imports inventory; inventory imports nothing from
  IPAM.

The extension point is deliberately generic — it takes the device and the
validated document, not anything IPAM-shaped — so the next module that needs to
observe an inventory result does not have to invent a second mechanism. The
SAVEPOINT belongs to `run_post_ingest_hooks`, not to any individual hook, for
the same reason: the guarantee "a module's hook cannot fail an
`inventory.collect` job" is inventory's to make, not each hook author's to
remember. §0.17 states why a bare `try/except` cannot make it.

---

## 15. Explicitly deferred

Named so they are choices, not oversights:

- **Windows DHCP lease import** (`dhcp.export` agent job) and scope
  synchronisation; AD DNS record awareness. It needs a new agent capability and
  enrolled DHCP servers — a phase of its own.
- **SNMP enrichment** of switch ARP/MAC tables and port mapping, under §7's
  boundary and §6's authorization model.
- **IPv6 allocation workflows** (EUI-64 / token-based), and any IPv6
  enumeration (§0.5).
- **IP request/approval workflow** for non-IT requesters — it wants the Service
  Desk's approval machinery rather than a second one here.
- **IPAM → ticket** on drift, and any aggregate/digest event shape (§10).
- **Subnet planning tools:** split/merge calculators, growth projection.
- **A shared `Site` entity** across modules; `ipam_site` is the migration source
  if one arrives.
- **Site-server-originated sweeps** (§5.3) — contract-only until the
  site-server workstream exists.
- **Promoting `asset_label` to a shared "unmanaged asset" record.** Free text in
  v1, per the brief's recommendation; promote if the need proves out.
- **Automatic reclaim** of stale addresses. Never — §4.3. Flag only.
