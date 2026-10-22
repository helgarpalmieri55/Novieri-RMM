# Asset inventory, part 3: enrolment matching — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** When an agent enrols on a machine the inventory already has a record for — one somebody typed in, or promoted from a discovery — that record is **enriched, not duplicated**; and where the evidence is too weak to be sure, an operator is asked instead of a merge being guessed.

**Architecture:** The enrolment request gains an optional list of physical MACs (additive wire change, both sides in-tree). `enroll()` looks for an **agentless** device in the same org carrying one of those MACs: exactly one match links, anything weaker creates the device and records a `suggested_asset_match` row for a human. Nothing merges two existing rows, ever.

**Tech Stack:** Go 1.24 (agent), Python 3.12 / FastAPI / SQLAlchemy 2.x / Alembic (server), React + TypeScript + vitest (console), pytest against a real Postgres.

## Global Constraints

Copied from `docs/modules/asset-inventory/spec.md` §4 and its 2026-08-18 amendment.

- **Getting this wrong is the defect that makes the whole feature worse than not having it**: two rows for one machine, and no way to tell which is real.
- **Rule 1 (serial) stays inert.** Nothing collects a serial at enrolment. Do not add a per-OS serial collector in this cycle, and do not substitute a weaker rule in its place.
- **Rule 2 (physical MAC) → auto-link.** "Physical" means: not loopback, not multicast, **not locally administered** — the bit, not the interface name. `app/modules/ipam/oui.py::is_locally_administered` already implements it.
- **Rule 3 (hostname or IP matches) → DO NOT LINK.** Create the device and record a *suggested match*. This is the one to defend: auto-linking on hostname silently merges two real machines that share a name across sites, and the merge is destructive — you cannot recover which inventory data belonged to which. **A suggestion queue is mildly annoying; a bad merge is data loss you discover months later.**
- **Rule 4: more than one candidate matches → never auto-link, always suggest.**
- **Linking reuses the existing row.** The `agents` row attaches to the record already there; no second `devices` row is created, so there is nothing to merge later.
- **`source` is not rewritten on link.** It records how the record came to exist; an agent enrolling onto a discovered asset does not retroactively make it agent-created.
- **Only agentless rows are link targets.** `agents.device_id` is unique, so linking to an already-managed device is impossible at the database anyway — but the query must exclude them so the *reason* is a deliberate filter rather than an IntegrityError.
- **Additive on the wire.** An older agent sends no MACs; the server sees an empty list and falls through to the suggestion path. `docs/kernel/wire-protocol/` rules apply: a field a new control plane *requires* and an old agent does not send is an outage.
- **Tenancy:** matching is `org_id`-scoped; the cross-org test asserts on the BYPASSRLS admin engine.

---

## File Structure

| File | Responsibility |
|---|---|
| `agent/internal/enroll/enroll.go` | Collect physical MACs; send them |
| `agent/internal/enroll/enroll_test.go` | The filter, on real `net.Interface` values |
| `backend/app/services/enrollment.py` | `DeviceFacts.macs`; the matching rules; link-or-create |
| `backend/app/models/core.py` | `SuggestedAssetMatch` |
| `backend/migrations/versions/0063_suggested_asset_match.py` | Table, RLS, grants |
| `backend/app/modules/ipam/candidates.py` | `promote()` stores the candidate's MAC so a discovered row is matchable |
| `backend/app/api/v1/devices.py` | List and resolve suggestions |
| `backend/tests/test_enrollment_matching.py` | Rules 2–4, tenancy, wire compatibility |
| `backend/tests/test_asset_suggestions.py` | The queue: list, accept, reject, audit |
| `frontend/src/modules/devices/*` | The badge and the queue |
| `docs/modules/asset-inventory/spec.md` | Status: part 3 built |

---

### Task 1: Physical MACs on the wire

**Files:** `backend/app/services/enrollment.py`, `agent/internal/enroll/enroll.go`, both test files.

**Interfaces:** Produces `DeviceFacts.macs: list[str] = []` (server) and `deviceFacts.MACs []string \`json:"macs"\`` (agent). Task 3 consumes the server side.

- [x] **Step 1: The server field, and a test that an old agent still enrols**

The compatibility case is the one worth a test: a request body with no `macs` key at all must enrol exactly as it does today.

```python
async def test_an_agent_that_sends_no_macs_still_enrols(api_env: ApiEnv) -> None:
    """THE WIRE-COMPATIBILITY CASE, and the reason `macs` is optional with a
    default. A fleet is always mixed-version: an agent in the field is older
    than the control plane it talks to. A field a new server REQUIRES and an
    old agent does not send is an outage that no test in this suite would
    otherwise show, because every fixture here is written against today's
    server."""
```

- [x] **Step 2: The agent collector, and its filter**

```go
// physicalMACs returns the hardware addresses that identify THIS MACHINE.
//
// FILTERED ON THE LOCALLY-ADMINISTERED BIT, not on interface name. Name
// matching (`veth*`, `docker*`, `br-*`) is fragile across three operating
// systems and misses what matters: Docker, KVM, most hypervisor defaults and
// phone MAC randomisation all set that bit, and two containers on different
// hosts can carry the SAME address. The server auto-links on these, so a
// collision here would merge two real machines.
//
// The reference host for this project reported nine `veth*` interfaces.
```

Rules: skip `FlagLoopback`, skip empty `HardwareAddr`, skip multicast (bit 0), skip locally administered (bit 1). Sorted and de-duplicated, so the same machine sends the same list every time.

- [x] **Step 3: Go tests over synthetic interfaces, then `go test ./...` and `gofmt`**

- [x] **Step 4: Commit**

---

### Task 2: `suggested_asset_match`

**Files:** model, migration 0063, a model test.

Columns, from spec §4: `(id, org_id, device_id, candidate_device_id, basis, created_at, resolved_at, resolution, resolved_by)`.

- `basis`: `hostname | ip | multiple` — **why** this was a suggestion rather than a link, which is the first thing the operator resolving it needs.
- `resolution`: `null | linked | rejected`.
- RLS `org_id` policy and `GRANT` to `app_user`, following 0059's pattern.
- A **partial unique index** on `(device_id, candidate_device_id) WHERE resolved_at IS NULL`, so a device re-enrolling does not stack duplicate open suggestions.

- [x] Steps: failing model test → migration → model → `test_schema_drift` → round-trip `upgrade/downgrade/upgrade` against a real Postgres → commit.

---

### Task 3: The matching rules

**Files:** `backend/app/services/enrollment.py`, `backend/tests/test_enrollment_matching.py`.

**The function**, to be written beside `enroll()` and called before the `Device` is created:

```python
async def resolve_enrolment_target(session, org_id, facts) -> MatchOutcome
```

returning one of: `Link(device_id)`, `CreateWithSuggestions(candidate_ids, basis)`, or `Create()`.

- [x] **Step 1: Write the failing tests** — one per rule, and each fixture is the shape that breaks the naive version:

```python
async def test_a_matching_physical_mac_links_to_the_existing_record() -> None:
    """Rule 2. The record was promoted from a discovery last week; the agent is
    installed today. One row, enriched -- not two."""

async def test_the_linked_row_keeps_its_source() -> None:
    """`source` records how the record CAME TO EXIST. An agent enrolling onto a
    discovered asset does not retroactively make it agent-created, and an
    operator asking "where did this row come from" must still get the true
    answer."""

async def test_a_locally_administered_mac_never_links() -> None:
    """THE DESTRUCTIVE CASE the filter exists for: two containers on different
    hosts can carry the same software-assigned MAC. Asserted server-side as
    well as in the agent's collector, because the server must not trust a field
    it did not compute."""

async def test_a_hostname_match_suggests_and_does_not_link() -> None:
    """Rule 3, and the one to defend. Two sites, one machine name. Auto-linking
    merges two real machines and the merge is not recoverable."""

async def test_two_matching_records_never_auto_link() -> None:
    """Rule 4. Ambiguity is not resolved by picking the first row."""

async def test_a_managed_device_is_never_a_link_target() -> None:
    """`agents.device_id` is unique, so this would be an IntegrityError rather
    than a merge -- but the query excludes managed rows so the refusal is a
    deliberate filter with a reason, not a database error surfacing as a 500
    during enrolment."""

async def test_matching_is_tenant_scoped() -> None:
    """On the BYPASSRLS admin engine: through a tenant session another org's
    device is invisible whether or not the predicate is there."""

async def test_linking_writes_an_audit_event_naming_both_rows() -> None:
    """Invariant 4. This is the moment two identities become one, and it is the
    single hardest thing to reconstruct afterwards if nobody wrote it down."""
```

- [x] **Step 2–5:** run to fail, implement, run to pass, **falsify each rule by removing its clause and confirming exactly one test goes red**, commit.

---

### Task 4: A promoted row carries its MAC

**Files:** `backend/app/modules/ipam/candidates.py`, `backend/tests/test_asset_promote.py`.

Without this, part 3's whole premise fails for the case it was designed for: `promote()` stores `primary_ip` and `hostname` but **not the MAC**, so a discovered row has nothing an enrolling agent could match on.

Write it into `Device.nics` in the shape inventory already uses —
`{"items": [{"identifier": <mac>, "mac": <mac>}]}` — so one reader (`ipam/nic.py::parse_mac` and the matcher) serves both sources.

- [x] Steps: failing test (`promote` then match) → implement → pass → commit.

---

### Task 5: The suggestion queue

**Files:** `backend/app/api/v1/devices.py`, `backend/tests/test_asset_suggestions.py`.

- `GET /api/v1/devices/suggestions` — open suggestions with both rows' identifying fields, gated `devices.read`.
- `POST /api/v1/devices/suggestions/{id}/resolve` — `{"resolution": "linked" | "rejected"}`, gated `assets.manage`.

**Resolving as `linked` is the destructive direction**, and it is the only place in this platform where two device rows become one. It must: refuse when either row now has an agent that the other also has; move the `agents` row; and write an audit event naming both ids and what was discarded. If that cannot be done safely in this cycle, **implement `rejected` only and say so in the spec** rather than shipping a merge whose failure modes are unexamined.

- [x] Steps as above, plus: the tool-surface record is re-measured in the same commit as the new GET route.

---

### Task 6: The console

- A badge on the device page when a row has an open suggestion.
- A small queue under Fleet → Discovered (a second tab), showing the two rows side by side with what matched, because the operator's whole job here is comparing them.
- Tests: the three states; a suggestion renders **both** rows' identifying fields; rejecting removes it from the list.

---

### Task 7: Close part 3

Spec status → parts 1–3 built. Falsifiers 2, 3 and 4 map to Task 3's tests. State plainly what is still not verified: **no agent has ever enrolled onto a discovered record on a real deployment**, and the Go collector has run only in tests.

---

## Verification before claiming completion

- `cd backend && .venv/bin/pytest -q` — green, whole suite
- `cd backend && .venv/bin/mypy app && .venv/bin/ruff check app tests` — clean
- `cd agent && go test ./... && gofmt -l .` — green, no output from gofmt
- `cd frontend && npx vitest run && npx tsc --noEmit` — green
- Migration 0063 round-trips against a real Postgres
- Each of rules 2, 3 and 4 falsified by removing its clause, one test red each time
- The old-agent compatibility case has a test that would fail if `macs` became required
