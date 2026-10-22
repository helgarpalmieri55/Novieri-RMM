"""Matching an enrolling agent to a record the inventory already has (§4).

GETTING THIS WRONG IS THE DEFECT THAT MAKES THE WHOLE FEATURE WORSE THAN NOT
HAVING IT: two rows for one machine, and no way to tell which is real. So the
rules are asymmetric on purpose, and the asymmetry is the design:

  * a physical MAC matches exactly one agentless record -> LINK, because a
    manufacturer-assigned address is the strongest thing enrolment can carry;
  * anything weaker -- a shared hostname, a shared address, two records at
    once -- CREATES the device and files a suggestion, because auto-linking on
    a hostname merges two real machines that share a name across sites, and
    that merge is not recoverable.

A suggestion queue is mildly annoying. A bad merge is data loss discovered
months later.

LINKING REUSES THE EXISTING ROW rather than merging two. The `agents` row
attaches to the record already there, so no second `devices` row is ever
created and there is nothing to merge afterwards.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import org_session
from app.models import Agent, AuditEvent, Device, SuggestedAssetMatch
from app.services.enrollment import DeviceFacts
from app.services.matching import MatchOutcome, resolve_enrolment_target
from tests.conftest import ApiEnv

#: `source` -> the `provenance` 0078's CHECK constraint pairs it with. A test
#: that names one and forgets the other gets a CheckViolationError from the
#: database, which is the constraint working -- this is how a fixture asks for
#: "an operator's row" or "a promoted row" without restating that pairing.
_PROVENANCE_FOR = {"agent": "agent", "manual": "operator", "discovery": "promoted"}

#: A manufacturer-assigned unicast address (Intel). The one shape that links.
PHYSICAL = "00:1b:21:aa:bb:cc"
#: Docker's range: locally administered, so two containers on different hosts
#: can carry it. The one shape that must never link.
SOFTWARE_ASSIGNED = "02:42:ac:11:00:02"


def facts(**kw: Any) -> DeviceFacts:
    return DeviceFacts(
        hostname=kw.pop("hostname", "wks-001"),
        os_family=kw.pop("os_family", "linux"),
        **kw,
    )


async def _asset(api_env: ApiEnv, org_id: UUID, **kw: Any) -> UUID:
    """An agentless record: what an operator typed in, or promoted."""
    macs = kw.pop("macs", [])
    async with org_session(org_id, engine=api_env.app_engine) as session:
        source = kw.pop("source", "discovery")
        device = Device(
            org_id=org_id,
            source=source,
            # PAIRED, because 0078's CHECK constraint makes any other
            # combination unrepresentable: `discovery` here means the row an
            # operator promoted, which is what this helper's docstring says it
            # is. A swept row is exercised in `test_ipam_sweep_autoasset.py`.
            provenance=kw.pop("provenance", _PROVENANCE_FOR[source]),
            nics={"items": [{"identifier": mac, "mac": mac} for mac in macs]}
            if macs
            else None,
            **kw,
        )
        session.add(device)
        await session.flush()
        device_id = device.id
        await session.commit()
    return device_id


async def _managed(api_env: ApiEnv, org_id: UUID, **kw: Any) -> UUID:
    device_id = await _asset(api_env, org_id, source="agent", **kw)
    async with org_session(org_id, engine=api_env.app_engine) as session:
        session.add(
            Agent(
                org_id=org_id,
                device_id=device_id,
                cert_fingerprint=f"fp-{device_id.hex[:16]}",
                cert_serial="01",
                cert_not_after=datetime.now(UTC) + timedelta(days=365),
                enrolled_at=datetime.now(UTC),
            )
        )
        await session.commit()
    return device_id


async def _resolve(api_env: ApiEnv, org_id: UUID, device_facts: DeviceFacts) -> MatchOutcome:
    async with org_session(org_id, engine=api_env.app_engine) as session:
        return await resolve_enrolment_target(session, org_id, device_facts)


async def test_a_matching_physical_mac_links_to_the_existing_record(
    api_env: ApiEnv,
) -> None:
    """Rule 2. The record was promoted from a discovery last week; the agent is
    installed today. One row, enriched — not two."""
    org_id = await api_env.create_org("match")
    existing = await _asset(api_env, org_id, hostname=None, macs=[PHYSICAL])

    outcome = await _resolve(api_env, org_id, facts(macs=[PHYSICAL]))
    assert outcome.device_id == existing, outcome
    assert outcome.suggestions == []


async def test_a_locally_administered_mac_never_links(api_env: ApiEnv) -> None:
    """THE DESTRUCTIVE CASE the filter exists for: two containers on different
    hosts carry the same software-assigned address, so linking on one merges
    two unrelated machines.

    Asserted SERVER-SIDE even though the agent filters too. The field arrives
    over the wire; a server that trusted the sender's filtering would be one
    compromised or buggy agent away from a merge.
    """
    org_id = await api_env.create_org("match")
    await _asset(api_env, org_id, hostname=None, macs=[SOFTWARE_ASSIGNED])

    outcome = await _resolve(api_env, org_id, facts(macs=[SOFTWARE_ASSIGNED]))
    assert outcome.device_id is None, outcome
    # And not even a suggestion: a MAC two machines can share is not evidence
    # that these two ARE the same machine.
    assert outcome.suggestions == []


async def test_a_hostname_match_suggests_and_does_not_link(api_env: ApiEnv) -> None:
    """Rule 3, and the one to defend. Two sites, one machine name. Renames are
    routine and duplicates across sites are routine, so this is the case that
    would merge two real machines if it linked."""
    org_id = await api_env.create_org("match")
    existing = await _asset(api_env, org_id, hostname="wks-001")

    outcome = await _resolve(api_env, org_id, facts(hostname="wks-001"))
    assert outcome.device_id is None, "a hostname match must never link"
    assert [s.candidate_device_id for s in outcome.suggestions] == [existing]
    assert outcome.suggestions[0].basis == "hostname"


async def test_two_matching_records_never_auto_link(api_env: ApiEnv) -> None:
    """Rule 4. Ambiguity is not resolved by picking the first row — which is
    what an `ORDER BY` and a `LIMIT 1` would quietly do."""
    org_id = await api_env.create_org("match")
    first = await _asset(api_env, org_id, hostname=None, macs=[PHYSICAL])
    second = await _asset(api_env, org_id, hostname=None, macs=[PHYSICAL])

    outcome = await _resolve(api_env, org_id, facts(macs=[PHYSICAL]))
    assert outcome.device_id is None, "two candidates must not auto-link"
    assert {s.candidate_device_id for s in outcome.suggestions} == {first, second}
    assert {s.basis for s in outcome.suggestions} == {"multiple"}


async def test_a_managed_device_is_never_a_link_target(api_env: ApiEnv) -> None:
    """`agents.device_id` is unique, so this would surface as an IntegrityError
    during enrolment rather than as a merge — but the query excludes managed
    rows so the refusal is a deliberate filter with a reason, not a 500 an
    operator has to interpret.

    The realistic shape: a machine is rebuilt and re-enrols while its old
    record still carries the old agent.
    """
    org_id = await api_env.create_org("match")
    await _managed(api_env, org_id, hostname="wks-001", macs=[PHYSICAL])

    outcome = await _resolve(api_env, org_id, facts(hostname="wks-001", macs=[PHYSICAL]))
    assert outcome.device_id is None, outcome
    assert outcome.suggestions == []


async def test_another_orgs_record_with_the_same_mac_is_not_a_candidate_under_rls(
    api_env: ApiEnv,
) -> None:
    """Another org's record with the same MAC is not a candidate.

    WHAT THIS PINS, AND WHAT IT DOES NOT. `_resolve` goes through the app
    engine, so RLS has already hidden org A's asset from org B's session before
    the matcher's own predicate is consulted: this stays green with that
    predicate deleted, and it is the "no candidate, no suggestion" OUTCOME that
    it holds, not the filter. The admin-engine read afterwards is a seed check
    -- it proves org A really did hold a matching row, so the empty answer is a
    refusal rather than an empty fixture. It is not a second, RLS-free
    observation of the predicate, and an earlier version of this docstring said
    it was."""
    org_a = await api_env.create_org("match-a")
    org_b = await api_env.create_org("match-b")
    in_a = await _asset(api_env, org_a, hostname=None, macs=[PHYSICAL])

    outcome = await _resolve(api_env, org_b, facts(macs=[PHYSICAL]))
    assert outcome.device_id is None, outcome
    assert outcome.suggestions == []

    async with AsyncSession(api_env.admin_engine) as session:
        owner = (
            await session.execute(select(Device.org_id).where(Device.id == in_a))
        ).scalar_one()
    assert owner == org_a


async def test_nothing_matching_is_simply_a_new_device(api_env: ApiEnv) -> None:
    """NON-VACUITY for every test above: the ordinary case still creates a row
    with no suggestion. A matcher that returned "no link, no suggestion" for
    everything would pass most of this file."""
    org_id = await api_env.create_org("match")
    await _asset(api_env, org_id, hostname="something-else")

    outcome = await _resolve(api_env, org_id, facts(hostname="wks-001", macs=[PHYSICAL]))
    assert outcome.device_id is None
    assert outcome.suggestions == []


# --------------------------------------------------------------------------
# End to end, through `enroll()` -- the only thing that proves the outcome is
# ACTED ON rather than merely computed.
# --------------------------------------------------------------------------


async def _enrol(api_env: ApiEnv, org_id: UUID, admin: str, **fact_kw: Any) -> dict[str, Any]:
    from tests.test_enrollment import make_csr_pem

    created = await api_env.client.post(
        "/api/v1/enrollment-tokens",
        json={"name": "match", "expires_in_hours": 24, "max_uses": 1},
        headers=await api_env.headers(org_id, admin),
    )
    token = created.json()["token"]
    response = await api_env.client.post(
        "/enroll/v1",
        json={
            "token": token,
            "csr_pem": make_csr_pem(),
            "device_facts": {"hostname": "wks-001", "os_family": "linux", **fact_kw},
        },
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_enrolling_onto_a_discovered_record_does_not_create_a_second_row(
    api_env: ApiEnv,
) -> None:
    """The whole point of part 3, end to end."""
    org_id = await api_env.create_org("match")
    admin = await api_env.create_user(org_id, "org-admin")
    existing = await _asset(api_env, org_id, hostname=None, macs=[PHYSICAL])

    body = await _enrol(api_env, org_id, admin, macs=[PHYSICAL])
    assert body["device_id"] == str(existing), "enrolment created a second row"

    async with org_session(org_id, engine=api_env.app_engine) as session:
        total = (
            await session.execute(select(func.count()).select_from(Device))
        ).scalar_one()
    assert total == 1


async def test_the_linked_row_keeps_its_source_and_gains_the_agent_facts(
    api_env: ApiEnv,
) -> None:
    """`source` records how the record CAME TO EXIST. An agent enrolling onto a
    discovered asset does not retroactively make it agent-created, and an
    operator asking "where did this row come from" must still get the true
    answer. The machine-reported facts DO arrive: that is the enrichment."""
    org_id = await api_env.create_org("match")
    admin = await api_env.create_user(org_id, "org-admin")
    existing = await _asset(api_env, org_id, hostname=None, macs=[PHYSICAL])

    await _enrol(api_env, org_id, admin, macs=[PHYSICAL])

    async with org_session(org_id, engine=api_env.app_engine) as session:
        device = await session.get(Device, existing)
        assert device is not None
        assert device.source == "discovery"
        assert device.hostname == "wks-001"
        assert device.os_family == "linux"


async def test_a_hostname_collision_creates_the_device_and_files_a_suggestion(
    api_env: ApiEnv,
) -> None:
    org_id = await api_env.create_org("match")
    admin = await api_env.create_user(org_id, "org-admin")
    existing = await _asset(api_env, org_id, hostname="wks-001")

    body = await _enrol(api_env, org_id, admin)
    assert body["device_id"] != str(existing)

    async with org_session(org_id, engine=api_env.app_engine) as session:
        rows = list((await session.execute(select(SuggestedAssetMatch))).scalars())
    assert len(rows) == 1, rows
    assert rows[0].candidate_device_id == existing
    assert rows[0].basis == "hostname"
    assert rows[0].resolved_at is None


async def test_linking_writes_an_audit_event_naming_both_rows(
    api_env: ApiEnv,
) -> None:
    """Invariant 4. This is the moment two identities become one, and it is the
    single hardest thing to reconstruct afterwards if nobody wrote it down."""
    org_id = await api_env.create_org("match")
    admin = await api_env.create_user(org_id, "org-admin")
    existing = await _asset(api_env, org_id, hostname=None, macs=[PHYSICAL])

    await _enrol(api_env, org_id, admin, macs=[PHYSICAL])

    async with org_session(org_id, engine=api_env.app_engine) as session:
        rows = list(
            (
                await session.execute(
                    select(AuditEvent.action, AuditEvent.meta).order_by(AuditEvent.id)
                )
            ).all()
        )
    linked = [meta for action, meta in rows if action == "asset.enrolment_linked"]
    assert linked, [action for action, _ in rows]
    assert linked[0]["device_id"] == str(existing)
    assert linked[0]["matched_on"] == "mac"
