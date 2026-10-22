# Asset inventory, part 2: discovery candidates — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** An operator sees what the fleet has observed on the network but the inventory does not know about, and turns the rows they recognise into assets — one click per candidate, or a selection at a time, never automatically.

**Architecture:** No new scanning. `net.sweep`, per-prefix scan authorizations, sweep runs and `ipam_observation` all exist and are governed (`docs/modules/ipam/spec.md` §6). This cycle adds a *read* over those observations that subtracts what the inventory already knows, and a promote route that writes `devices` rows with `source='discovery'`. **No new table and no new permission**: the OUI lookup is a module-level dict (a table would imply it is maintained, and it is not), and `assets.manage` already covers writing an asset row.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x (typed), Alembic, Pydantic v2, pytest against a real Postgres; React + TypeScript (strict), TanStack Query, vitest.

## What already exists, and why that shrinks this cycle to almost nothing

I expected to build a network scan. It is built:

| Piece | Where | State |
|---|---|---|
| ICMP/ARP sweep as an agent capability | `agent/internal/capabilities/netsweep` | Works. Runs inside a verified job; the raw socket is closed in a defer, carries no port and accepts no connection — so it does not touch invariant 2 |
| Per-prefix scan authorization, deny by default | `app/modules/ipam/authorization.py` | Works, and is the module's security surface: no `ipam_scan_authorization` row means no packets, with three separate gates pinned by tests |
| Sweep runs, including `state='no_agent'` | `IpamSweepRun` | Works. **A subnet nobody can sweep is already a first-class outcome**, which is the spec's falsifier 8 already served |
| Current-state observations keyed on `(org, vrf, address, source, mac_key)` | `IpamObservation` | Works, with confidence fixed by method: `inventory/nic → high`, `sweep/arp → medium`, `sweep/icmp → low` |
| Addresses observed inside no registered subnet | `GET /api/v1/ipam/orphans` | Works |

So this cycle is: **a candidate view, a promote action, and an OUI lookup.** Nothing here dispatches a job, opens a socket, or widens an authorization.

## Global Constraints

- **Discovery produces candidates; an operator promotes them.** An echo reply is not an asset. A sweep on a corporate LAN returns printers, phones, a colleague's laptop on the wrong VLAN and the coffee machine; auto-creating rows from that produces an inventory nobody trusts, and the untrustworthiness is invisible because every row looks like one somebody entered (spec §3).
- **Bulk promote is allowed. Bulk *auto*-promote is not.** The difference is whether a human saw the list.
- **A promoted row is `source='discovery'`**, never `manual` and never `agent`.
- **No new permission.** `assets.manage` covers promotion (it creates asset rows) and `ipam.read` covers seeing observations. `assets.discover` was named in spec §7 for *requesting a sweep* — that already exists as `ipam.sweep.run`, so adding a second code for the same action would be two ways to say one thing.
- **Currency, not history.** Candidates use the same `W_obs` window every other "is this true now" question in IPAM uses (`_currency_cutoff`). An address nobody has seen in three months is not a candidate.
- **Tenancy:** every query `org_id`-scoped at the database layer, cross-org tests asserted on the BYPASSRLS admin engine.
- **The OUI table states what it does not know.** An unrecognised prefix renders as the prefix itself, never as a guessed manufacturer.

---

## The rule for "the inventory does not know about this", stated precisely

This is the one piece of real design in the cycle, and getting it wrong makes the screen useless in the obvious way: **every managed machine appears as a candidate.**

`ipam_observation.device_id` is set only for `source='inventory'` — a device reporting its own NICs. A sweep observation never carries one, by design (spec §6.4: letting a sweep assert identity would be a compromised agent's cheapest way to re-point an assignment). So a managed workstation that also answers a ping has **two** observation rows: one with `device_id`, one without. Filtering on `device_id IS NULL` alone would list the entire managed fleet.

An observed address is a **candidate** when all of the following hold:

1. it was seen within the currency window;
2. **no** observation of that same address, in the same org, carries a `device_id`;
3. when it has a MAC: **no** observation of that same MAC, in the same org, carries a `device_id` (a machine that moved address is still known);
4. **no** `devices` row in the org has that address as its `primary_ip` (an asset an operator already typed in).

Rules 2 and 3 are what make this "the inventory has never tied this to a device", rather than "this particular row has no device id".

**What this deliberately does not do:** match on hostname. A reverse-DNS name is the weakest signal there is and the spec refuses it even for *linking* (§4 rule 3). Using it to *hide* a candidate is the same mistake pointed the other way — it would silently drop a real unknown machine because it happens to share a name with something in the inventory.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/app/modules/ipam/oui.py` | MAC → vendor, and the facts a MAC states about itself |
| `backend/app/modules/ipam/candidates.py` | The candidate query and `promote()` |
| `backend/app/api/v1/devices.py` | `GET /devices/candidates`, `POST /devices/candidates/promote` |
| `backend/tests/test_asset_candidates.py` | The query's four rules, tenancy, currency |
| `backend/tests/test_asset_promote.py` | Promotion, audit, bulk, idempotence |
| `backend/tests/test_oui.py` | Vendor lookup and what it refuses to guess |
| `frontend/src/modules/devices/CandidatesPage.tsx` | The screen |
| `frontend/src/modules/devices/CandidatesPage.test.tsx` | Three states, the no-agent case, promote |
| `frontend/src/App.tsx`, `src/shell/nav.ts` | Route and nav entry |
| `docs/modules/asset-inventory/spec.md` | Status: part 2 built |

---

### Task 1: What a MAC address says about itself

**Files:**
- Create: `backend/app/modules/ipam/oui.py`
- Create: `backend/tests/test_oui.py`

**Interfaces:**
- Produces: `vendor(mac: str) -> str | None`, `oui_prefix(mac: str) -> str`, `is_locally_administered(mac: str) -> bool`, `is_multicast(mac: str) -> bool`. Task 2's projection calls `vendor` and `oui_prefix`; part 3's matching will call `is_locally_administered`.

- [x] **Step 1: Write the failing test**

`backend/tests/test_oui.py`:

```python
"""What a MAC address states about itself, and what it merely hints at.

TWO DIFFERENT KINDS OF ANSWER LIVE HERE, and conflating them is the failure to
avoid. The locally-administered and multicast bits are DEFINED BY THE STANDARD:
they are read off the address and are true or false. The manufacturer is a
LOOKUP against a table this repository cannot hold in full -- the IEEE registry
is tens of thousands of entries and deployments have no egress to fetch it.

So the vendor function returns `None` for anything it does not recognise, and
the console renders the raw prefix in that case. A guessed manufacturer on an
inventory screen is worse than no manufacturer: an operator triaging fifty
candidates uses the vendor column to decide what a thing IS, and a wrong answer
sends them to the wrong conclusion faster than a blank one.
"""

import pytest

from app.modules.ipam.oui import (
    is_locally_administered,
    is_multicast,
    oui_prefix,
    vendor,
)


class TestFactsTheAddressStates:
    def test_a_locally_administered_address_is_recognised(self) -> None:
        # Bit 1 of the first octet. Docker, KVM and every randomising phone set
        # it, which is why part 3 must never match on one.
        assert is_locally_administered("02:42:ac:11:00:02") is True
        assert is_locally_administered("00:50:56:aa:bb:cc") is False

    def test_multicast_is_recognised(self) -> None:
        assert is_multicast("01:00:5e:00:00:fb") is True
        assert is_multicast("00:50:56:aa:bb:cc") is False

    @pytest.mark.parametrize("bad", ["", "not-a-mac", "00:50", "zz:zz:zz:zz:zz:zz"])
    def test_a_malformed_address_raises_nothing_and_claims_nothing(
        self, bad: str
    ) -> None:
        # These strings arrive from a column, and a screen that 500s because one
        # observation is malformed is worse than one that shows it plainly.
        assert vendor(bad) is None
        assert oui_prefix(bad) == ""
        assert is_locally_administered(bad) is False


class TestVendor:
    def test_it_recognises_a_prefix_it_knows(self) -> None:
        assert vendor("00:50:56:aa:bb:cc") == "VMware"

    def test_it_is_case_and_separator_insensitive(self) -> None:
        # asyncpg returns MACADDR as `00:50:56:aa:bb:cc`; an import may carry
        # `0050.56aa.bbcc` or `00-50-56-AA-BB-CC`.
        assert vendor("00-50-56-AA-BB-CC") == "VMware"
        assert vendor("0050.56aa.bbcc") == "VMware"

    def test_it_returns_None_rather_than_guessing(self) -> None:
        # A prefix outside the table. The console shows `oui_prefix` instead,
        # which an operator can look up -- and which claims nothing.
        assert vendor("a4:bb:6d:11:22:33") is None
        assert oui_prefix("a4:bb:6d:11:22:33") == "A4:BB:6D"

    def test_every_entry_in_the_table_is_a_well_formed_prefix(self) -> None:
        """The table is hand-maintained, so the shape is asserted rather than
        trusted: a typo'd key silently never matches anything."""
        from app.modules.ipam.oui import OUI

        for prefix in OUI:
            assert len(prefix) == 8, prefix
            assert prefix == prefix.upper(), prefix
            assert prefix.count(":") == 2, prefix
```

- [x] **Step 2: Run to verify it fails**

Run: `cd backend && .venv/bin/pytest tests/test_oui.py -q`
Expected: FAIL — `ModuleNotFoundError: app.modules.ipam.oui`

- [x] **Step 3: Implement**

`backend/app/modules/ipam/oui.py`:

```python
"""MAC address facts, and a deliberately small vendor table.

WHY THE TABLE IS SMALL AND SAYS SO. The IEEE OUI registry is tens of thousands
of assignments and changes continuously. This platform deploys on-prem, often
with no egress at all (`docs/product.md` §5), so there is no build step that
could fetch it and no runtime lookup that could stay current. The alternative
to a small table is not a complete one -- it is a table that is wrong in ways
nobody can see.

So: the entries below are the prefixes that actually recur in a corporate
estate, and ANYTHING ELSE RETURNS `None`. The console then shows the raw
prefix, which an operator can look up and which claims nothing. A guessed
manufacturer would be worse than a blank: the vendor column is what somebody
triaging fifty candidates uses to decide what a thing is, and a wrong answer
sends them to a conclusion faster than an absent one.

Adding an entry is a one-line change and a welcome one. Guessing is not.
"""

import re

#: Prefix (upper case, colon separated) -> manufacturer. Sourced from the IEEE
#: registry; each is an assignment stable for decades. Kept short on purpose --
#: see the module docstring.
OUI: dict[str, str] = {
    "00:50:56": "VMware",
    "00:0C:29": "VMware",
    "00:1C:14": "VMware",
    "00:05:69": "VMware",
    "08:00:27": "Oracle VirtualBox",
    "52:54:00": "QEMU/KVM",
    "00:15:5D": "Microsoft Hyper-V",
    "00:1C:42": "Parallels",
    "00:00:0C": "Cisco",
    "00:1B:0D": "Cisco",
    "00:1A:A1": "Cisco",
    "00:03:93": "Apple",
    "00:1B:63": "Apple",
    "3C:07:54": "Apple",
    "00:14:22": "Dell",
    "00:21:9B": "Dell",
    "B8:2A:72": "Dell",
    "00:1B:78": "HP",
    "00:23:7D": "HP",
    "3C:D9:2B": "HP",
    "00:1B:21": "Intel",
    "00:1E:67": "Intel",
    "00:0D:B9": "PC Engines",
    "00:80:77": "Brother",
    "00:00:48": "Seiko Epson",
    "00:26:73": "Ricoh",
    "00:1E:8F": "Canon",
    "00:04:F2": "Polycom",
    "00:0B:82": "Grandstream",
}

_HEX = re.compile(r"[0-9A-Fa-f]")


def _octets(mac: str) -> list[int]:
    """The address as integers, or `[]` if it is not one.

    Tolerant of separator style -- `:`/`-`/`.`/none -- because these strings
    come from a database column populated by three different code paths, and a
    screen that raises because one row is odd is worse than one that shows the
    row plainly.
    """
    digits = "".join(_HEX.findall(mac or ""))
    if len(digits) != 12:
        return []
    return [int(digits[i : i + 2], 16) for i in range(0, 12, 2)]


def oui_prefix(mac: str) -> str:
    """`AA:BB:CC`, or `""` for anything that is not a MAC."""
    octets = _octets(mac)
    if not octets:
        return ""
    return ":".join(f"{o:02X}" for o in octets[:3])


def vendor(mac: str) -> str | None:
    """The manufacturer, or `None` -- never a guess. See the module docstring."""
    return OUI.get(oui_prefix(mac))


def is_locally_administered(mac: str) -> bool:
    """Bit 1 of the first octet: the address was assigned by software, not by a
    manufacturer.

    Docker, KVM, most hypervisor defaults and every phone doing MAC
    randomisation set it. Part 3 of this module must never auto-link on one --
    two containers on different hosts can collide.
    """
    octets = _octets(mac)
    return bool(octets) and bool(octets[0] & 0b10)


def is_multicast(mac: str) -> bool:
    """Bit 0 of the first octet. Never an endpoint's own address."""
    octets = _octets(mac)
    return bool(octets) and bool(octets[0] & 0b1)
```

- [x] **Step 4: Run the tests**

Run: `cd backend && .venv/bin/pytest tests/test_oui.py -q`
Expected: PASS (8 tests)

- [x] **Step 5: Commit**

```bash
git add backend/app/modules/ipam/oui.py backend/tests/test_oui.py
git commit -m "asset-inventory: what a MAC states, and a vendor table that admits its limits"
```

---

### Task 2: The candidate query

**Files:**
- Create: `backend/app/modules/ipam/candidates.py`
- Create: `backend/tests/test_asset_candidates.py`

**Interfaces:**
- Consumes: `oui.vendor`, `oui.oui_prefix` (Task 1); `observations._currency_cutoff`.
- Produces: `AssetCandidate` (frozen dataclass) and `async def list_candidates(session, org_id, *, limit) -> list[AssetCandidate]`. Task 3's route serializes it; Task 5's page renders it.

- [x] **Step 1: Write the failing test**

`backend/tests/test_asset_candidates.py`:

```python
"""What the fleet has seen and the inventory does not know (spec §3).

THE ONE FAILURE THAT MAKES THIS SCREEN USELESS is listing the managed fleet as
candidates, and it is the DEFAULT outcome of the obvious query.
`ipam_observation.device_id` is set only for `source='inventory'` -- a device
reporting its own NICs -- and never for a sweep, deliberately (ipam spec §6.4:
a sweep asserting identity would be a compromised agent's cheapest way to
re-point an assignment). So a managed workstation that answers a ping has TWO
observation rows, one with a device id and one without, and `WHERE device_id IS
NULL` returns the whole estate.

The rules are therefore about the ADDRESS and the MAC across all observations,
not about one row. Each has a test here, and each test's fixture is the shape
that would break the naive version.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import org_session
from app.models import Device
from app.modules.ipam.candidates import list_candidates
from tests.conftest import ApiEnv
from tests.ipam_fixtures import seed_vrf, upsert_observation_row


async def test_a_swept_address_nobody_owns_is_a_candidate(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("cand")
    vrf_id = await seed_vrf(api_env, org_id)
    await upsert_observation_row(
        api_env, org_id, vrf_id, address="172.22.104.55", mac="00:50:56:aa:bb:cc",
        source="sweep", method="arp", device_id=None,
    )
    async with org_session(org_id, engine=api_env.app_engine) as session:
        found = await list_candidates(session, org_id, limit=50)
    assert [c.address for c in found] == ["172.22.104.55"]
    # The vendor is the single most useful hint for "what is this" when
    # triaging fifty rows.
    assert found[0].vendor == "VMware"


async def test_a_managed_device_is_not_a_candidate_even_though_the_sweep_saw_it(
    api_env: ApiEnv,
) -> None:
    """THE TEST THAT MATTERS. Two rows for one machine: the inventory row
    carrying the device id, and the sweep row that cannot carry one."""
    org_id = await api_env.create_org("cand")
    vrf_id = await seed_vrf(api_env, org_id)
    device_id = await _managed_device(api_env, org_id)
    await upsert_observation_row(
        api_env, org_id, vrf_id, address="172.22.104.10", mac="00:1b:21:aa:bb:cc",
        source="inventory", method="nic", device_id=device_id,
    )
    await upsert_observation_row(
        api_env, org_id, vrf_id, address="172.22.104.10", mac="00:1b:21:aa:bb:cc",
        source="sweep", method="arp", device_id=None,
    )
    async with org_session(org_id, engine=api_env.app_engine) as session:
        found = await list_candidates(session, org_id, limit=50)
    assert found == [], [c.address for c in found]


async def test_a_machine_that_moved_address_is_not_a_candidate_at_its_new_one(
    api_env: ApiEnv,
) -> None:
    """Rule 3: the MAC is known even though this ADDRESS has never carried a
    device id. DHCP makes address a lease, not an identity."""
    org_id = await api_env.create_org("cand")
    vrf_id = await seed_vrf(api_env, org_id)
    device_id = await _managed_device(api_env, org_id)
    await upsert_observation_row(
        api_env, org_id, vrf_id, address="172.22.104.10", mac="00:1b:21:aa:bb:cc",
        source="inventory", method="nic", device_id=device_id,
    )
    await upsert_observation_row(
        api_env, org_id, vrf_id, address="172.22.104.99", mac="00:1b:21:aa:bb:cc",
        source="sweep", method="arp", device_id=None,
    )
    async with org_session(org_id, engine=api_env.app_engine) as session:
        found = await list_candidates(session, org_id, limit=50)
    assert found == [], [c.address for c in found]


async def test_an_asset_an_operator_already_typed_in_is_not_a_candidate(
    api_env: ApiEnv,
) -> None:
    """Rule 4. The switch was entered by hand in part 1; the sweep then found
    it. Offering it as a candidate would invite a duplicate row for the one
    record whose whole point is that a human curated it."""
    org_id = await api_env.create_org("cand")
    vrf_id = await seed_vrf(api_env, org_id)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        session.add(
            Device(
                org_id=org_id, hostname=None, os_family=None, source="manual",
                asset_type="network", lifecycle="unmanageable",
                primary_ip="172.22.104.1",
            )
        )
        await session.commit()
    await upsert_observation_row(
        api_env, org_id, vrf_id, address="172.22.104.1", mac="00:00:0c:11:22:33",
        source="sweep", method="icmp", device_id=None,
    )
    async with org_session(org_id, engine=api_env.app_engine) as session:
        found = await list_candidates(session, org_id, limit=50)
    assert found == []


async def test_something_nobody_has_seen_for_months_is_not_a_candidate(
    api_env: ApiEnv,
) -> None:
    """Currency, using the same `W_obs` window every other 'is this true now'
    question in IPAM uses. An address last seen in March is not a machine to go
    and find today."""
    org_id = await api_env.create_org("cand")
    vrf_id = await seed_vrf(api_env, org_id)
    await upsert_observation_row(
        api_env, org_id, vrf_id, address="172.22.104.77", mac=None,
        source="sweep", method="icmp", device_id=None,
        last_seen_at=datetime.now(UTC) - timedelta(days=400),
    )
    async with org_session(org_id, engine=api_env.app_engine) as session:
        found = await list_candidates(session, org_id, limit=50)
    assert found == []


async def test_candidates_are_tenant_scoped(api_env: ApiEnv) -> None:
    org_a = await api_env.create_org("cand-a")
    org_b = await api_env.create_org("cand-b")
    vrf_a = await seed_vrf(api_env, org_a)
    await upsert_observation_row(
        api_env, org_a, vrf_a, address="172.22.104.55", mac=None,
        source="sweep", method="icmp", device_id=None,
    )
    async with org_session(org_b, engine=api_env.app_engine) as session:
        found = await list_candidates(session, org_b, limit=50)
    assert found == []
```

`_managed_device` creates a `Device` plus an `Agent`, as `tests/test_asset_api.py` already does — reuse that helper's body rather than importing across test modules, following this suite's own convention for fixtures.

`tests/ipam_fixtures.py` already carries `seed_vrf`, `seed_device`, `seed_agent` and `upsert_observation_row` — use them. `upsert_observation_row` is keyword-only after `org_id`: `(env, org_id, *, vrf_id, address, source='sweep', method='icmp', confidence='low', mac=None, **kw)`, where `**kw` reaches `values()` directly (so `device_id=`, `hostname=`, `last_seen_at=` and `now=` all work). It exists precisely so the migration test and the service test exercise the same statement shape — do not hand-build `IpamObservation` rows here.

- [x] **Step 2: Run to verify it fails**

Run: `cd backend && .venv/bin/pytest tests/test_asset_candidates.py -q`
Expected: FAIL — `ModuleNotFoundError: app.modules.ipam.candidates`

- [x] **Step 3: Implement**

`backend/app/modules/ipam/candidates.py`, with the query written as SQL (following `observations.orphan_groups`, which groups in SQL for the same reason: the row set is unbounded by anything the caller controls and the answer is small):

```python
"""Observed addresses the inventory has never tied to a device (spec §3).

READ THE RULE IN `docs/superpowers/plans/2026-08-18-asset-inventory-part2.md`
before changing this query. The short version: `device_id` is set only on
`source='inventory'` rows, so a managed machine that answers a ping has one row
with a device id and one without -- and `WHERE device_id IS NULL` therefore
returns the entire managed fleet. The exclusions below are about the ADDRESS
and the MAC across ALL of an org's observations, not about one row.

DISCOVERY DOES NOT CREATE ASSETS. This function is a read; `promote` is the
only thing that writes, and only for rows an operator selected. An echo reply
is not an asset -- a sweep on a corporate LAN returns printers, phones, a
colleague's laptop on the wrong VLAN and the coffee machine, and auto-creating
rows from that produces an inventory nobody trusts, invisibly, because every
row looks like one somebody entered.
"""
```

The SQL, parameterised on `org`, `cutoff` and `limit`:

```sql
SELECT o.address                    AS address,
       max(o.mac::text)             AS mac,
       max(o.hostname)              AS hostname,
       min(o.first_seen_at)         AS first_seen_at,
       max(o.last_seen_at)          AS last_seen_at,
       sum(o.sighting_count)        AS sightings,
       max(o.confidence)            AS confidence,
       max(o.subnet_id::text)       AS subnet_id
  FROM ipam_observation o
 WHERE o.org_id = :org
   AND o.last_seen_at >= :cutoff
   AND NOT EXISTS (SELECT 1 FROM ipam_observation k
                    WHERE k.org_id = o.org_id
                      AND k.address = o.address
                      AND k.device_id IS NOT NULL)
   AND (o.mac IS NULL OR NOT EXISTS (
            SELECT 1 FROM ipam_observation k
             WHERE k.org_id = o.org_id
               AND k.mac = o.mac
               AND k.device_id IS NOT NULL))
   AND NOT EXISTS (SELECT 1 FROM devices d
                    WHERE d.org_id = o.org_id
                      AND d.primary_ip = o.address)
 GROUP BY o.address
 ORDER BY max(o.last_seen_at) DESC
 LIMIT :limit
```

`AssetCandidate` is a frozen dataclass carrying those columns plus `vendor` and `oui_prefix`, computed in Python from `mac` via Task 1.

**`LIMIT` is not optional and its default is not large.** A `/16` sweep can observe sixty thousand addresses; a route that returned all of them would be a memory event in the API process and an unusable screen. Default 200, maximum 1000, and the response says how many were not returned so the console can say so too.

- [x] **Step 4: Run the tests**

Run: `cd backend && .venv/bin/pytest tests/test_asset_candidates.py -q`
Expected: PASS (6 tests)

- [x] **Step 5: Falsify the exclusion**

The whole value of this query is the three `NOT EXISTS` clauses. Prove each can fail: delete one at a time, run, and confirm the matching test goes red — `test_a_managed_device_is_not_a_candidate...` for the first, `test_a_machine_that_moved_address...` for the second, `test_an_asset_an_operator_already_typed_in...` for the third. **Confirm each edit is on disk before reading the result.**

- [x] **Step 6: Commit**

```bash
git add backend/app/modules/ipam/candidates.py backend/tests/test_asset_candidates.py
git commit -m "asset-inventory: what the fleet has seen and the inventory does not know"
```

---

### Task 3: Promotion, and the routes

**Files:**
- Modify: `backend/app/modules/ipam/candidates.py` (add `promote`)
- Modify: `backend/app/api/v1/devices.py` (two routes)
- Create: `backend/tests/test_asset_promote.py`

**Interfaces:**
- Consumes: `list_candidates` (Task 2), `assets.manage` (part 1's permission).
- Produces: `GET /api/v1/devices/candidates` and `POST /api/v1/devices/candidates/promote`, both returning JSON the console consumes directly.

**Route placement note:** `/devices/candidates` must be declared **before** `/devices/{device_id}`, or FastAPI matches the literal path against the UUID parameter and returns a 422 for a route that exists. Add a test asserting the GET returns 200, not 422 — this is the kind of ordering bug that only shows up in a browser.

- [x] **Step 1: Write the failing tests**

`backend/tests/test_asset_promote.py`:

```python
"""Promoting a candidate to an asset (spec §3).

BULK PROMOTE IS ALLOWED; BULK AUTO-PROMOTE IS NOT. The difference is whether a
human looked at the list -- so there is a route that takes a list of addresses
an operator selected, and there is nothing anywhere that promotes on a
schedule, on a sweep completing, or on a count threshold. The first test in
this file is the one that would catch such a thing appearing.
"""
```

The cases:

```python
async def test_a_sweep_alone_creates_no_devices(api_env: ApiEnv) -> None:
    """§6.1's falsifier, and the property the whole design rests on. Observe
    three addresses, run nothing else, and assert the device table is empty."""


async def test_promoting_requires_assets_manage(api_env: ApiEnv) -> None:
    """403 for a reader. Promotion writes an inventory row."""


async def test_a_promoted_candidate_is_marked_discovery(api_env: ApiEnv) -> None:
    """`source='discovery'`, never `manual`: how a row came to exist is the
    first thing an operator checks when it looks wrong, and 'a human typed
    this' and 'a sweep saw this and somebody clicked' are different claims."""


async def test_promotion_carries_what_the_operator_supplied_on_top(
    api_env: ApiEnv,
) -> None:
    """Address and MAC come from the observation; hostname, description, type
    and lifecycle come from the operator if they gave them."""


async def test_promoting_the_same_address_twice_does_not_duplicate(
    api_env: ApiEnv,
) -> None:
    """Two operators looking at the same list, or one double-click. The second
    call must not produce a second row -- and must not 500 either, because the
    operator has no way to tell which of the two happened."""


async def test_promotion_is_audited_naming_the_address(api_env: ApiEnv) -> None:
    """Invariant 4. A row appearing in the inventory is a change to what the
    platform believes the estate contains."""


async def test_promotion_is_tenant_scoped(api_env: ApiEnv) -> None:
    """An address observed in org A cannot be promoted by org B -- asserted on
    the BYPASSRLS admin engine, because through a tenant session the
    observation is invisible either way."""


async def test_the_candidates_route_is_not_shadowed_by_the_device_route(
    api_env: ApiEnv,
) -> None:
    """`/devices/candidates` vs `/devices/{device_id}`. Declared in the wrong
    order, the literal path is parsed as a UUID and the route 422s -- a failure
    that appears only in a browser and looks like a frontend bug."""
    resp = await api_env.client.get("/api/v1/devices/candidates", headers=headers)
    assert resp.status_code == 200, resp.text
```

- [x] **Step 2: Run to verify they fail**

Expected: 405/422 on both routes.

- [x] **Step 3: Implement**

`promote()` takes `(session, org_id, actor_id, address, overrides)` and:

1. re-derives the candidate for that address (never trusts a MAC or hostname posted by the caller — the client could send anything, and the point of the row is that it came from an observation);
2. returns the existing device if one already has that `primary_ip`, so a double-click is idempotent rather than a duplicate or a 500;
3. creates the `devices` row with `source='discovery'`, `primary_ip` from the observation, `hostname` from reverse DNS if there is one, plus the operator's overrides;
4. writes `asset.promoted` to the audit log with the address and the candidate's confidence.

The bulk route takes a list of addresses and returns per-address results, so one bad address does not fail the batch silently.

- [x] **Step 4: Run the tests, then mypy and ruff**

Run: `cd backend && .venv/bin/pytest tests/test_asset_promote.py -q && .venv/bin/mypy app && .venv/bin/ruff check app tests`

- [x] **Step 5: Check the route sweeps**

Run: `cd backend && .venv/bin/pytest tests/test_rbac.py tests/test_ai_tool_surface.py tests/test_route_gating.py -q`

`GET /devices/candidates` is a new GET route, so **`test_ai_tool_surface` will go red** — that is the guard working. Re-measure, read which property the route fails, and update the record in the same commit, exactly as the file instructs. Expect P4 (a bare list) unless the response model wraps the items; wrapping it in an object carrying `items` and `truncated` is better anyway, because the console has to tell the operator when the list was cut.

- [x] **Step 6: Commit**

```bash
git add backend/app/modules/ipam/candidates.py backend/app/api/v1/devices.py backend/tests/test_asset_promote.py backend/tests/test_ai_tool_surface.py
git commit -m "asset-inventory: promote a candidate, and nothing promotes without an operator"
```

---

### Task 4: The console screen

**Files:**
- Modify: `frontend/src/lib/api.ts`
- Create: `frontend/src/modules/devices/CandidatesPage.tsx`
- Create: `frontend/src/modules/devices/CandidatesPage.test.tsx`
- Modify: `frontend/src/App.tsx`, `frontend/src/shell/nav.ts`

- [x] **Step 1: Write the failing tests**

The three states, plus what is specific to this screen:

```tsx
it("says a subnet has nobody to sweep it rather than showing an empty list", async () => {
  // Spec falsifier 8. An empty result and "no agent in this subnet can sweep
  // it" look identical on screen and mean opposite things: the first says the
  // segment is clean, the second says the platform has no idea. IPAM already
  // records `state='no_agent'` on the sweep run -- this screen has to surface
  // it rather than rendering the absence as a result.
});

it("shows the vendor when known and the raw prefix when not", async () => {
  // Never a guessed manufacturer: the vendor column is what an operator uses
  // to decide what a thing IS.
});

it("promotes only the rows the operator selected", async () => {
  // Bulk promote, never bulk auto-promote.
});

it("says how many candidates were not shown", async () => {
  // A /16 sweep observes tens of thousands of addresses. A truncated list that
  // does not say it is truncated reads as "that is all of them".
});
```

- [x] **Step 2–5: implement, run `npx vitest run && npx tsc --noEmit`, wire the route and a nav entry under Devices, commit**

Nav: this belongs beside the fleet list rather than under Platform — it is inventory work, and the operator doing it is the operator looking at devices. `permission: "devices.read"`, matching the fleet list it feeds.

---

### Task 5: Close part 2 in the docs

- [x] **Step 1: Update the spec status** — part 2 built; falsifiers 1 and 8 now have tests; falsifiers 2, 3 and 4 remain part 3's (enrolment matching), untested because that code does not exist.

- [x] **Step 2: Record what discovery does NOT do**, in the spec rather than only in code: no SNMP, no port scanning, no agent auto-deploy from a candidate. All three are already in §8; the status block should point at them so a reader who skips to the end still finds them.

- [x] **Step 3: Commit**

---

## Verification before claiming completion

- `cd backend && .venv/bin/pytest -q` — green, the whole suite
- `cd backend && .venv/bin/mypy app && .venv/bin/ruff check app tests` — clean
- `cd frontend && npx vitest run && npx tsc --noEmit` — green
- `test_ai_tool_surface` re-measured in the same commit as the new GET route, with the failing property named
- Falsifiers 1 and 8 map to named tests; 2, 3 and 4 are recorded in the spec status as part 3's and untested today
- Each `NOT EXISTS` clause in the candidate query was falsified by deleting it and watching the matching test go red
