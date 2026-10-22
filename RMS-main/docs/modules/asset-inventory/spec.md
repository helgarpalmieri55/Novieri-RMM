# Asset inventory — spec

**Status:** **built**, 2026-08-18 — all three parts, to the plans at
`docs/superpowers/plans/2026-08-18-asset-inventory-part{1,2,3}.md`. Approved
2026-08-18.

**Amended 2026-08-26 on the operator's ruling: a sweep records everything it
finds.** §3 said the opposite and argued it at length; the operator was shown
that argument and its consequences and decided the other way. §3 now carries
both the ruling and the reasoning it replaced, the `provenance` column that
answers the half of the objection that was load-bearing, and the four
consequential decisions (identity on re-sweep, naming, what a later inventory
report does, and what the Discovered page is still for). §6.1's falsifier is
inverted rather than deleted.

**What part 2 turned out to be.** §3 reads as though network discovery had to
be built. It did not: `net.sweep`, per-prefix scan authorizations (deny by
default), sweep runs and `ipam_observation` all already exist and are governed
by `docs/modules/ipam/spec.md` §6 — including `state='no_agent'` as a
first-class outcome, which is falsifier 8 already served by the data. So part 2
is a **read** over those observations that subtracts what the inventory
already knows, a promote route, and a screen. It dispatches no job, opens no
socket and widens no authorization.

**The three parts.** This spec covers three deliverables that depend on each
other in one direction only, and each is its own build cycle:

| Part | Scope | State |
|---|---|---|
| 1 | Agentless assets: the columns, manual entry, the honest UI, the offline sweep | **Built** |
| 2 | Discovery → candidates → promote (§3) | **Built** |
| 3 | Enrolment matching and the suggestion queue (§4) | **Built**, after amending §4 — see below |

**Which falsifiers have tests, and which cannot yet.**

- **§6.5** (a `None` hostname or `os_family` breaks a read path) —
  `backend/tests/test_asset_read_paths.py`, and `mypy` caught three more
  null-flow sites at the point of the schema change: the backup dispatcher's
  snapshot-provider lookup, the security-event serializer and the health
  deep-link template.
- **§6.6** (Patch/Backup/Health render for an agentless asset) —
  `frontend/src/modules/inventory/DeviceDetailPage.assets.test.tsx`.
- **§6.7** (`health_offline_sweep` alerts on a `stock` asset) —
  `backend/tests/test_health_offline_lifecycle.py`. Note what was already safe:
  the sweep joins `agents`, so a typed-in asset could never reach it. The
  reachable case is a machine that WAS deployed and has gone back to the
  warehouse with its agent still installed.
- **§6.9** (tenancy) — `backend/tests/test_asset_api.py` and
  `test_asset_read_paths.py`, both asserting on the BYPASSRLS admin engine as
  well as through the API.
- **§6.1** — INVERTED on 2026-08-26. It read "a discovered candidate becomes a
  device with no operator action" and `test_asset_promote.py`'s first test
  observed three addresses and asserted the device table was empty, naming "a
  sweep-completion hook or a schedule appearing later" as what it would catch.
  That hook now exists on purpose. The same test completes a real `net.sweep`
  reporting the same three addresses and asserts three asset rows with
  `provenance = 'sweep'`, and that the candidate list is empty afterwards;
  `backend/tests/test_ipam_sweep_autoasset.py` carries the rest — identity on
  re-sweep, naming, the audit actor, and the tenancy case on the BYPASSRLS
  admin engine.
- **§6.8** (a subnet with no enrolled agent shows an empty discovery result) —
  `test_asset_promote.py` for the count and
  `frontend/src/modules/devices/CandidatesPage.test.tsx` for the sentence on
  screen.
- **§6.2** (two rows for one machine after enrolment) —
  `backend/tests/test_enrollment_matching.py`, end to end through `enroll()`:
  a matching physical MAC produces one row, not two, and the row keeps its
  `source`.
- **§6.3** (a hostname-only match auto-links) — the same file. This is the
  destructive case, and it was falsified by making hostname link: exactly the
  two tests that guard it went red.
- **§6.4** (a `veth`/`docker` MAC is used for matching) — the same file, and
  the check is applied **server-side** rather than trusting the agent's own
  filter, because the field arrives over the wire and the consequence of a bad
  entry is a merge.

**Every falsifier in §6 now has a test.** That is not the same as every claim
being verified — see the closing paragraph.

**What part 2 deliberately does not do**, restating §8 here because a reader
who skips to the end should still find it: no SNMP, no port or service
scanning, no agent auto-deploy from a candidate, and no auto-promotion of any
kind. `net.sweep` is ICMP and ARP; turning this platform into a port scanner is
a much larger security conversation, and the estate already has a vulnerability
scanner for it.

**§4 AMENDED, 2026-08-18, before part 3 was planned.** §4 assumed the
enrolment request carries a serial and a set of MACs. **It carries neither.**
`DeviceFacts` has `hostname`, `os_family`, `os_version`, `architecture` and
`serial` — and the agent sends `Serial: nil`, because collecting one needs a
per-OS collector (WMI, ioreg, DMI) that belongs to `inventory.collect`
(`agent/internal/enroll/enroll.go`). So as written, rule 1 could never fire and
rule 2 had nothing to read. The amendment, in three parts:

1. **Physical MACs are added to the enrolment facts**, as an optional field.
   This is an additive wire change and the compatibility case is the good one:
   an older agent sends nothing, the server sees an empty list, and matching
   falls through to the suggestion path exactly as if no MAC matched. MACs are
   also the one strong-ish identifier the agent can collect **portably** at
   enrolment — `net.Interfaces()`, no per-OS code, no privileges — which is
   what makes this cheap where a serial is not.

2. **A "physical" MAC is defined by the locally-administered bit, not by
   interface name.** `veth*`/`docker*`/`br-*` name matching is fragile across
   three operating systems and misses the case that matters: Docker, KVM,
   most hypervisor defaults and phone MAC randomisation all set the
   locally-administered bit, and two containers on different hosts can carry
   the same address. The bit is defined by the standard and is already
   implemented (`app/modules/ipam/oui.py::is_locally_administered`, written for
   this). Loopback and multicast are excluded for the same reason.

3. **Rule 1 (serial) stays inert, and is described that way rather than
   claimed.** Nothing collects a serial at enrolment, so serial matching cannot
   run there. It is not replaced by a weaker rule: an enrolling agent either
   matches on a physical MAC or produces a suggestion.

**Linking means reusing the existing row, not merging two.** The enrolling
agent attaches its `agents` row to the record that is already there and
enriches it; no second `devices` row is created, so there is nothing to merge
later. That is what keeps the spec's own warning — "a bad merge is data loss
you discover months later" — from ever applying. `source` is left as it was:
it records how the record CAME TO EXIST, and an agent enrolling onto a
discovered asset does not retroactively make it agent-created.

**§9's open question is answered in the console only.** "Managed endpoints"
counts records with an agent and a second card counts total assets, which is the
recommendation §9 made. Whether an agentless asset consumes a licence seat is a
commercial decision and is untouched.

**NOTHING IN ANY PART HAS RUN AGAINST A REAL DEPLOYMENT.** No asset has been
typed into a running install, no sweep has produced a candidate anybody
promoted, and no agent has ever enrolled onto a discovered record outside this
test suite. The Go MAC collector has run only in `go test`, and on CI that
means a container whose only non-loopback interface is a veth — precisely the
case its filter removes — so its behaviour on a real NIC is asserted by its
unit tests and by nothing else.

**Two things part 3 deliberately did not do.** It did not add a per-OS serial
collector, so §4's rule 1 stays inert and is described that way rather than
replaced by something weaker. And nothing anywhere merges two rows
automatically: `linked` in the suggestion queue is the only path, it requires
`assets.manage`, and it retires rather than deletes — because a delete would
take the row's job history, documentation links and audit-trail target with
it.

**What this is.** Making the fleet list an *asset* inventory: machines with an
agent, machines without one yet, hardware in a warehouse, and devices that can
never run an agent — switches, printers, appliances — in one place, with network
discovery to populate it quickly.

**The operator's framing, which is the right one:** an inventory that only holds
machines running an agent is not an inventory. It leaves a spreadsheet beside the
platform, which is the thing the platform is supposed to replace.

---

## 1. The good news: the schema is already shaped for this

`agents.device_id` is a **unique foreign key to `devices.id`**. So "managed"
already means "has an `agents` row", and **a `devices` row with no agent is
structurally legal today** — nothing in the data model has to be invented.

This is why the operator's preference (one fleet list, records enriched when an
agent later enrols) is also the cheaper design. The alternative considered — a
separate `assets` table — would have duplicated hostname, serial, group, tags and
tenancy, and then needed a link table to reunite them.

What `devices` cannot yet express:

| Column | Problem for a non-agent asset |
|---|---|
| `hostname` | `NOT NULL`. A boxed laptop has no hostname; a switch has a name but not a hostname in this sense |
| `os_family` | `NOT NULL`, `windows\|linux\|macos`. A switch is none of those |
| `os_version`, `architecture`, `cpu` | default `""` — fine, but the UI must not render an empty string as fact |

And what it has no column for at all: a description, a primary IP, an asset type,
an owner, a location, or a lifecycle state.

---

## 2. Two different questions, deliberately not one column

It is tempting to add a single `management_state`. That conflates two facts that
change independently:

1. **Does this record have an agent?** Derived, never stored — it is
   `EXISTS (SELECT 1 FROM agents WHERE device_id = devices.id)`. Storing it
   alongside the `agents` row guarantees they eventually disagree, and the
   disagreement is silent.
2. **What does the operator intend this asset to be?** Stored, because nothing
   can derive it: `in_service`, `stock` (in the warehouse, not deployed),
   `unmanageable` (a switch — will never have an agent, and that is not a
   defect), `retired`.

So: `lifecycle` is a stored column; *managed* is a join. A screen that says
"unmanaged" means "no agents row"; a screen that says "unmanageable" means the
operator said so. The distinction matters because the first is a to-do list and
the second must never appear on one.

### Schema additions

```
ALTER TABLE devices
  ADD COLUMN lifecycle      text NOT NULL DEFAULT 'in_service',
  ADD COLUMN asset_type     text NOT NULL DEFAULT 'computer',
  ADD COLUMN description    text,
  ADD COLUMN primary_ip     inet,
  ADD COLUMN owner          text,
  ADD COLUMN location       text,
  ADD COLUMN source         text NOT NULL DEFAULT 'agent';
-- and, from 2026-08-26 (§3), who -- if anyone -- stood behind the row:
ALTER TABLE devices
  ADD COLUMN provenance     text NOT NULL DEFAULT 'agent';
ALTER TABLE devices ALTER COLUMN hostname   DROP NOT NULL;
ALTER TABLE devices ALTER COLUMN os_family  DROP NOT NULL;
```

`asset_type`: `computer | server | network | printer | phone | appliance | other`.
`source`: `agent | manual | discovery` — how the record came to exist, which is
the question an operator asks first when a row looks wrong.
`provenance`: `agent | operator | promoted | sweep` — *who* stood behind it,
which became a separate question the moment a sweep could write rows on its own
(§3). Paired with `source` by a CHECK constraint so the two cannot disagree.

**Dropping two `NOT NULL`s is the risky part of this migration**, and not because
of the migration. Every existing query and template that assumes `hostname` and
`os_family` are present becomes a potential `None` render. The migration is
trivial; the audit of read paths is the work. §6 makes that a falsifier.

Forward-only, new migration, per `CLAUDE.md`.

---

## 3. Network discovery: already half-built, and it does not touch invariant 2

`net.sweep` exists as an agent capability, and `IpamObservation` is already a
current-state table of what the fleet has seen, keyed on `mac_key`, with
`confidence` fixed by method (`inventory/nic → high`, a sweep → lower).

**Crucially, discovery runs from an enrolled agent sweeping its own LAN, not from
the control plane.** `netsweep.go`'s own header spells out why that is still
invariant-2-clean: the raw ICMP socket is opened inside a verified job, closed in
a defer when the job ends, carries no port, accepts no connection, and reads only
echo replies to requests it just sent. There is no socket between jobs.

That is what makes discovery compose with the no-push-install decision in
`docs/modules/agent-distribution/spec.md` §1: **the platform never originates a
connection to an unmanaged machine.** One enrolled agent per subnet is the
bootstrap, and it is also the honest limit — a subnet with no agent cannot be
discovered, and the UI must say that rather than showing an empty result that
looks like "nothing there".

### Discovery creates assets — the operator's ruling, 2026-08-26

**This section said the opposite until 2026-08-26, and the argument it made is
preserved below because it was not wrong about the risk.** It said: an echo
reply is not an asset; a ping sweep on a corporate LAN returns printers,
phones, a colleague's laptop on the wrong VLAN, the coffee machine and the
firewall; auto-creating device rows from that produces an inventory nobody
trusts, and the untrustworthiness is invisible because every row looks like the
ones an operator entered. So discovery produced candidates and an operator
promoted them; bulk promote was allowed, bulk auto-promote was not.

The operator was shown that reasoning and its consequence — a sweep that finds
forty machines leaves forty rows of work for a person, and an inventory that is
only as complete as somebody's afternoon — and ruled: **a sweep records
everything it finds.** This is their estate. The decision is not reopened here.

**What the ruling changes.** `handle_sweep_result` (IPAM §6.4), after every
containment and identity check has passed and the observations are written,
creates one `devices` row per host the sweep reported, with `source =
discovery`. Nothing is selected by anybody. A rejected result creates nothing,
because the creation is downstream of the rejection.

**What answers the old argument is a column, not a refusal.** The half of the
objection that was load-bearing is "indistinguishable". So `devices` gains:

```
ALTER TABLE devices ADD COLUMN provenance text NOT NULL DEFAULT 'agent';
```

`provenance`: `agent | operator | promoted | sweep` — *who, if anyone, stood
behind this row*, as opposed to `source`, which is *which mechanism produced
it*. They differ exactly once: a `discovery` row is `promoted` when an operator
picked it off the Discovered screen and `sweep` when the sweep recorded it.
Migration 0078 backfills (`manual → operator`, `discovery → promoted` — every
`discovery` row that existed came through the promote route) and adds a CHECK
constraint pairing the two columns, so they cannot drift into disagreeing. The
console renders it as a **Recorded by** column on the fleet list and a filter
(`/devices?provenance=sweep`), and the API takes it as a query parameter.

`promote` remains, and is not a disabled remnant: it is how an operator records
that they vouch for a row, and it is still the only path that takes their asset
type, lifecycle, owner and location.

**Four decisions the ruling forces, each answered in code.**

1. **Identity on re-sweep — MAC first, address second.** A sweep runs hourly.
   Keyed on address alone, a laptop that takes a new DHCP lease overnight
   becomes a second row every morning. So: a sighting carrying a MAC is matched
   against every MAC the inventory holds (`devices.nics`, plus MACs an agent
   has reported for itself), and a match means "already recorded". If the
   matched row is itself `provenance = sweep` and has no agent, its
   `primary_ip` is **corrected** to the new address. A row an operator or an
   agent owns is never edited by a ping. An ICMP-only sighting has nothing to
   key on but its address, and can therefore still be recorded twice if a
   MAC-less machine moves — that is stated rather than papered over.
2. **What an auto-created row is called — nothing, unless reverse DNS answered.**
   `hostname` takes the sweep's reverse-DNS name when there is one and stays
   NULL otherwise. A fabricated name (`unknown-10.0.0.7`) is indistinguishable
   from a real hostname to every screen, export and match rule downstream; a
   null one renders as the address. `asset_type` is `other`, not `computer`: an
   echo reply does not say what answered.
3. **A later inventory report corrects the row, it does not duplicate it** —
   because the row carries the MAC. §4's matcher links an enrolling agent to
   exactly one agentless record on a physical MAC, so the agent lands on the
   row the sweep created and replaces its guesses with reported facts. An
   ICMP-only row has no MAC, so enrolment files a *suggestion* against it
   rather than linking — which is §4 rule 3 working as designed.
4. **The Discovered page keeps three jobs and loses one.** It is no longer the
   gate. It still reports subnets whose most recent sweep found no agent
   (`no_agent`) — an empty table and an unswept segment are the same pixels
   meaning opposite things, and nothing else in the console separates them; it
   still lists and promotes whatever remains unaccounted for, which is now
   normally nothing; and it links to `/devices?provenance=sweep`, where the
   rows the sweep wrote are reviewed. The list of what discovery recorded is a
   filter on the fleet list, not a second copy kept here.

**Audit (invariant 4).** A bulk automatic creation still writes one
`asset.auto_created` event per row, with `actor_type = "system"` and
`actor_id = NULL`. The actor is not the user who scheduled the sweep:
attributing these to a person would put a human's name against rows no human
chose. Each event carries the address, MAC, confidence and sweep run, so an
auditor can judge what the row is worth. A correction writes
`asset.auto_readdressed`.

**`GET /api/v1/devices` pages, filters and counts in SQL, and it has to.**
*(Amended 2026-08-26, the same day as the sweep ruling above and because of
it.)* Inventory used to grow by hand, so the console fetched one capped page and
did the search, the provenance filter and every tab count in the browser over
those rows. A /24 sweep now adds ~200 rows in one act and a /16 far more: past
the page limit the counts were wrong, the search missed rows it should have
found, and `?provenance=sweep` — the one control that answers "which of these
did a person vouch for?" — reported nothing on an estate full of swept rows.
Each of those failures renders as a correct-looking empty answer, which is why
none was ever reported.

The response carries three counts answering three different questions, and
conflating any two is how a tile comes to disagree with the table under it:

| Field | Obeys |
|---|---|
| `total` | every filter, including `state` — what to page through |
| `state_counts` | the search and `provenance`; **not** `state` — the tab strip |
| `fleet` | nothing at all — the stat tiles, which are claims about the estate |

`total` is not counted separately: it **is** `state_counts[state]`, so the strip
and the table cannot disagree. Query parameters: `q`, `state`
(`all|online|offline|assets|certs`), `provenance`, `os_family`, `status`,
`limit`, `offset`. `state_counts` and `fleet` are additive and defaulted — a
caller reading only `items` and `total` is unaffected.

**The other routes are unchanged in shape.**

- `GET /api/v1/devices/candidates` — derived from `IpamObservation` rows that
  have no matching device, carrying IP, MAC, MAC vendor prefix, hostname if
  reverse DNS gave one, first/last seen, sighting count. Normally empty now.
- `POST /api/v1/devices/candidates/promote` — creates a device row with
  `source = discovery`, `provenance = promoted`, taking whatever the operator
  supplies on top.

The MAC vendor prefix is worth carrying because it is the single most useful hint
for "what is this" — an OUI lookup turns `3c:a8:2a` into a manufacturer and lets
an operator triage fifty candidates quickly. A small embedded OUI table, not a
network lookup.

---

## 4. The hard part: matching an enrolling agent to an existing record

When an agent enrols on a machine that already has a manual or discovered record,
the record must be **enriched, not duplicated**. Getting this wrong is the defect
that makes the whole feature worse than not having it: two rows for one machine,
and no way to tell which is the real one.

Candidate keys, strongest first:

| Key | Strength | Failure mode |
|---|---|---|
| `serial` | **Strong.** Stable across reinstalls and renames | Absent on VMs and on this project's own reference host, where `dmidecode` reported nothing — the Serial card renders `—` |
| MAC (any NIC) | **Strong-ish.** From `nics` | Docker/veth interfaces pollute it (the reference host reported nine `veth*` NICs); virtual and randomised MACs; a replaced NIC |
| `hostname` | **Weak.** Renames are routine; duplicates across sites are routine | |
| IP | **Weakest.** DHCP makes it a lease, not an identity | |

### The rule

1. Match on `serial` when both sides have one → **auto-link**.
2. Otherwise match on any **physical** MAC (exclude `veth*`, `docker*`, bridges,
   and locally-administered addresses) → **auto-link**.
3. Otherwise, if hostname or IP matches → **do not link.** Create the device, and
   record a *suggested match* for an operator to confirm or reject.
4. More than one candidate matches → **never auto-link**, always suggest.

Rule 3 is the one to defend. Auto-linking on hostname silently merges two real
machines that happen to share a name across sites, and the merge is destructive —
you cannot recover which inventory data belonged to which. A suggestion queue is
mildly annoying; a bad merge is data loss you discover months later.

`suggested_asset_match` rows: `(device_id, candidate_device_id, basis, created_at,
resolved_at, resolution)`. Surfaced as a badge on the device and a small queue.

---

## 5. The UI must not lie about what it can do

The Devices page already refuses to render Site/Patch/Backup/Risk columns
because no field backs them — `DevicesPage.test.tsx` calls that out: "a screen
that looks finished and lies is worse than one that is visibly incomplete, and it
lies most convincingly on the demo where somebody decides to trust it."

The same standard applies here, and it is the objection that was raised against
the operator's chosen design. The answer is that the **device detail page adapts**:

- **Has an agent** → every tab as today.
- **No agent** (`source = manual | discovery`, or agent removed) → Hardware
  (whatever is known), Docs, Changes, Jobs (empty, with a reason). **Patch,
  Deploy, Backup, Provisioning, Health are not rendered** — they cannot ever
  populate without an agent, and a zeroed Patch card is exactly the lie above.
  In their place, one panel: "No agent on this asset" and a link to the install
  command.
- **`lifecycle = unmanageable`** → the same, minus the install-command link,
  because offering an agent for a switch is noise.
- **`lifecycle = stock`** → also suppress Health and last-seen staleness. A boxed
  laptop is not offline, and an offline alert for one is a false positive that
  trains operators to ignore the alert.

That last point connects to a live subsystem: `health_offline_sweep` must exclude
`stock` and `unmanageable` records, or the first thing this feature does is
generate alerts for warehouse hardware.

---

## 6. Falsifiers

1. **A sweep's results do NOT become device rows.** *(Inverted 2026-08-26 on
   the operator's ruling; it previously read "a discovered candidate becomes a
   device with no operator action", and asserted that promotion was required.)*
   Complete a sweep reporting three hosts, run nothing else, and assert three
   asset rows exist carrying `provenance = 'sweep'`. Two more that come with
   it: a second sweep of the same segment creating a fourth row, and a swept
   row that cannot be told apart from a promoted one.
2. **Two rows for one machine after enrolment.** Enrol against a record with a
   matching serial; assert one row. Repeat with a matching physical MAC.
3. **A hostname-only match auto-links.** Assert it produces a *suggestion*, not
   a link. This is the destructive case.
4. **A `veth`/`docker` MAC is used for matching.** The reference host has nine of
   them; two containers on different hosts can collide. Assert virtual and
   locally-administered addresses are excluded.
5. **A `None` hostname or `os_family` renders as blank fact or 500s.** Sweep every
   read path after the `NOT NULL` drop — §2 says the audit is the real work.
6. **Patch/Backup/Health tabs render for an agentless asset.** The
   platform-honesty test, modelled on `DevicesPage.test.tsx`'s column test.
7. **`health_offline_sweep` alerts on a `stock` asset.**
8. **A subnet with no enrolled agent shows an empty discovery result** rather than
   "no agent in this subnet can sweep it".
9. **Tenancy.** Every new route and the candidate derivation are `org_id`-scoped
   at the database layer, with the cross-org test pinned on the BYPASSRLS admin
   engine — RLS masks a missing application predicate otherwise.

---

## 7. Permissions

- `assets.read` — implied by existing `devices.read`; do not add a second read gate
- `assets.manage` — create, edit, promote, resolve a suggested match
- `assets.discover` — request a sweep (this dispatches a job; it is a fleet
  action, not a read)

Retire/delete: deleting an asset with history is a destructive act on evidence.
Prefer `lifecycle = retired`; hard delete requires `assets.manage` and writes an
audit event naming what was removed.

---

## 8. Explicitly out of scope

- **SNMP.** The obvious next step for network gear, and a different protocol,
  credential store and polling model. Its own cycle.
- **Agent auto-deploy from a candidate.** Still out of scope, and *from a
  candidate* is now the load-bearing half of that sentence.

  *(Required amendment, `docs/modules/agent-distribution/spec-push-install.md`
  §10.5 item 3, on the operator's approval of `CLAUDE.md` invariant 2a on
  2026-08-26.)* Push install was reopened and approved in narrowed form, so
  "rejected in agent-distribution §1" is no longer the whole story — but the
  refusal this bullet records survives it intact, because
  `spec-push-install.md` §3 condition 1 refuses to push to a candidate:

  > A candidate is an ICMP echo reply … Promotion is the moment a human
  > asserted *this is a machine we own and manage*. Pushing to a candidate
  > would present administrative credentials to a coffee machine on the
  > strength of its having answered a ping.

  So the path is unchanged where it matters — candidate → promote → **then**
  either an install command the operator distributes, or (cycle B2, not built)
  a push against the promoted asset. Nothing may ever push from this module's
  candidate list, and `backend/app/modules/agent_push/preflight.py` refuses it
  with a named reason (`no_asset`) rather than relying on nobody trying.
- **Port/service scanning.** `net.sweep` is ICMP. Turning ITMS into a port
  scanner is a much larger security conversation, and the estate already has a
  vulnerability scanner for it.
- **Warranty/purchase data.** Belongs with the budget module's Odoo pull, not here.

---

## 9. Open question

**Does an agentless asset consume a licence seat, and does it count in "Managed
endpoints"?** The Devices page stat card currently counts rows. Once rows include
warehouse stock, that number stops meaning what an operator thinks it means. My
recommendation: the card counts records **with an agent**, and a second card
counts total assets — but the licensing implication is a commercial decision, not
a technical one.
