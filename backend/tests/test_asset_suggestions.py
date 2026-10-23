"""The suggestion queue: listing, and resolving (asset-inventory spec §4).

WHAT RESOLVING MEANS, AND WHAT IT DELIBERATELY DOES NOT. `rejected` closes the
question: these are two different machines, stop asking. `linked` is the
destructive direction — it is the ONLY place in this platform where two device
rows become one — so it moves the `agents` row onto the record the operator
kept and retires the other rather than deleting it.

RETIRE, NOT DELETE, and that is the decision worth defending. A deleted row
takes its job history, its documentation links and its audit trail's target
with it; `lifecycle='retired'` leaves every one of those answerable while
removing the row from every list an operator works from. The spec's own rule
for assets applies here too: "deleting an asset with history is a destructive
act on evidence."
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import Agent, AuditEvent, Device, SuggestedAssetMatch
from tests.conftest import ApiEnv

#: `source` -> the `provenance` 0078's CHECK constraint pairs it with. A test
#: that names one and forgets the other gets a CheckViolationError from the
#: database, which is the constraint working -- this is how a fixture asks for
#: "an operator's row" or "a promoted row" without restating that pairing.
_PROVENANCE_FOR = {"agent": "agent", "manual": "operator", "discovery": "promoted"}


async def _pair(api_env: ApiEnv, org_id: UUID, **kw: Any) -> tuple[UUID, UUID, UUID]:
    """A just-enrolled device, an older record, and an open suggestion."""
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        enrolled = Device(org_id=org_id, hostname="wks-001", os_family="linux")
        candidate_source = kw.pop("candidate_source", "manual")
        older = Device(
            org_id=org_id,
            hostname="wks-001",
            source=candidate_source,
            provenance=_PROVENANCE_FOR[candidate_source],
            description="the one somebody typed in",
        )
        session.add_all([enrolled, older])
        await session.flush()
        session.add(
            Agent(
                org_id=org_id,
                device_id=enrolled.id,
                cert_fingerprint=f"fp-{enrolled.id.hex[:16]}",
                cert_serial="01",
                cert_not_after=datetime.now(UTC) + timedelta(days=365),
                enrolled_at=datetime.now(UTC),
            )
        )
        suggestion = SuggestedAssetMatch(
            org_id=org_id,
            device_id=enrolled.id,
            candidate_device_id=older.id,
            basis="hostname",
        )
        session.add(suggestion)
        await session.flush()
        ids = (suggestion.id, enrolled.id, older.id)
        await session.commit()
    return ids


async def test_listing_requires_only_devices_read(api_env: ApiEnv) -> None:
    """Seeing that two records might be the same machine is not a wider
    privilege than seeing the records."""
    org_id = await api_env.create_org("sug")
    reader = await api_env.create_user(org_id, "read-only")
    resp = await api_env.client.get(
        "/api/v1/devices/suggestions", headers=await api_env.headers(org_id, reader)
    )
    assert resp.status_code == 200, resp.text


async def test_a_suggestion_names_both_rows_and_why(api_env: ApiEnv) -> None:
    """The operator's whole job here is COMPARING two records, so a response
    carrying only ids would make the screen useless — they would be looking
    two rows up by hand for every question in the queue."""
    org_id = await api_env.create_org("sug")
    admin = await api_env.create_user(org_id, "org-admin")
    _, enrolled, older = await _pair(api_env, org_id)

    resp = await api_env.client.get(
        "/api/v1/devices/suggestions", headers=await api_env.headers(org_id, admin)
    )
    assert resp.status_code == 200, resp.text
    [row] = resp.json()["items"]
    assert row["basis"] == "hostname"
    assert row["device"]["id"] == str(enrolled)
    assert row["candidate"]["id"] == str(older)
    assert row["candidate"]["description"] == "the one somebody typed in"


async def test_resolving_requires_assets_manage(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("sug")
    reader = await api_env.create_user(org_id, "read-only")
    suggestion_id, _, _ = await _pair(api_env, org_id)
    resp = await api_env.client.post(
        f"/api/v1/devices/suggestions/{suggestion_id}/resolve",
        json={"resolution": "rejected"},
        headers=await api_env.headers(org_id, reader),
    )
    assert resp.status_code == 403, resp.text


async def test_rejecting_closes_the_question_and_changes_no_rows(
    api_env: ApiEnv,
) -> None:
    """These are two different machines. Nothing about either changes — the
    only thing that happens is that the platform stops asking."""
    org_id = await api_env.create_org("sug")
    admin = await api_env.create_user(org_id, "org-admin")
    suggestion_id, enrolled, older = await _pair(api_env, org_id)

    resp = await api_env.client.post(
        f"/api/v1/devices/suggestions/{suggestion_id}/resolve",
        json={"resolution": "rejected"},
        headers=await api_env.headers(org_id, admin),
    )
    assert resp.status_code == 200, resp.text

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        row = await session.get(SuggestedAssetMatch, suggestion_id)
        assert row is not None
        assert row.resolution == "rejected"
        assert row.resolved_at is not None
        # Both devices still exist, both unchanged.
        assert (await session.get(Device, older)) is not None
        keeper = await session.get(Device, enrolled)
        assert keeper is not None and keeper.lifecycle == "in_service"

    listed = await api_env.client.get(
        "/api/v1/devices/suggestions", headers=await api_env.headers(org_id, admin)
    )
    assert listed.json()["items"] == [], "a resolved question is still being asked"


async def test_linking_moves_the_agent_and_retires_the_duplicate(
    api_env: ApiEnv,
) -> None:
    """The destructive direction, and the only place two device rows become one.

    The operator keeps the record they curated: the `agents` row moves onto it,
    and the row enrolment created is RETIRED rather than deleted. A delete
    would take its job history, its documentation links and its audit trail's
    target with it; retirement leaves all of that answerable while removing the
    row from every list an operator works from.
    """
    org_id = await api_env.create_org("sug")
    admin = await api_env.create_user(org_id, "org-admin")
    suggestion_id, enrolled, older = await _pair(api_env, org_id)

    resp = await api_env.client.post(
        f"/api/v1/devices/suggestions/{suggestion_id}/resolve",
        json={"resolution": "linked"},
        headers=await api_env.headers(org_id, admin),
    )
    assert resp.status_code == 200, resp.text

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        agent = (
            await session.execute(select(Agent).where(Agent.org_id == org_id))
        ).scalar_one()
        assert agent.device_id == older, "the agent did not move onto the kept record"

        retired = await session.get(Device, enrolled)
        assert retired is not None, "the duplicate was deleted, taking its history"
        assert retired.lifecycle == "retired"

        kept = await session.get(Device, older)
        assert kept is not None
        # The kept row gains what the machine reported.
        assert kept.os_family == "linux"


async def test_linking_is_audited_naming_both_rows(api_env: ApiEnv) -> None:
    """Invariant 4. Two identities became one; without this nobody can say
    which row survived, which was retired, or who decided."""
    org_id = await api_env.create_org("sug")
    admin = await api_env.create_user(org_id, "org-admin")
    suggestion_id, enrolled, older = await _pair(api_env, org_id)

    await api_env.client.post(
        f"/api/v1/devices/suggestions/{suggestion_id}/resolve",
        json={"resolution": "linked"},
        headers=await api_env.headers(org_id, admin),
    )
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        rows = list(
            (
                await session.execute(
                    select(AuditEvent.action, AuditEvent.meta).order_by(AuditEvent.id)
                )
            ).all()
        )
    linked = [meta for action, meta in rows if action == "asset.match_resolved"]
    assert linked, [action for action, _ in rows]
    assert linked[0]["resolution"] == "linked"
    assert linked[0]["kept_device_id"] == str(older)
    assert linked[0]["retired_device_id"] == str(enrolled)


async def test_an_unknown_resolution_is_refused(api_env: ApiEnv) -> None:
    org_id = await api_env.create_org("sug")
    admin = await api_env.create_user(org_id, "org-admin")
    suggestion_id, _, _ = await _pair(api_env, org_id)
    resp = await api_env.client.post(
        f"/api/v1/devices/suggestions/{suggestion_id}/resolve",
        json={"resolution": "merged"},
        headers=await api_env.headers(org_id, admin),
    )
    assert resp.status_code == 422, resp.text


async def test_resolving_twice_is_refused_rather_than_silently_redone(
    api_env: ApiEnv,
) -> None:
    """The second call must not move an agent a second time. A resolved
    question is closed, and reopening it by accident is how a row that was
    retired for a reason comes back."""
    org_id = await api_env.create_org("sug")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    suggestion_id, _, _ = await _pair(api_env, org_id)

    first = await api_env.client.post(
        f"/api/v1/devices/suggestions/{suggestion_id}/resolve",
        json={"resolution": "rejected"},
        headers=headers,
    )
    second = await api_env.client.post(
        f"/api/v1/devices/suggestions/{suggestion_id}/resolve",
        json={"resolution": "linked"},
        headers=headers,
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text


async def test_another_orgs_suggestion_is_neither_listed_nor_resolvable_under_rls(
    api_env: ApiEnv,
) -> None:
    """Org B must not see, or resolve, org A's question.

    Both halves run through the route on the app engine, so RLS is what
    produces the empty list and the 404 and both survive deleting the read
    path's own `org_id` filter. The admin-engine read matters for the second
    half anyway, because resolving is the DESTRUCTIVE direction: it reads org
    A's agent row back with RLS out of the way, so a resolve that half-applied
    before answering 404 cannot hide behind a tenant-scoped query."""
    org_a = await api_env.create_org("sug-a")
    org_b = await api_env.create_org("sug-b")
    admin_b = await api_env.create_user(org_b, "org-admin")
    suggestion_id, enrolled, _ = await _pair(api_env, org_a)

    listed = await api_env.client.get(
        "/api/v1/devices/suggestions", headers=await api_env.headers(org_b, admin_b)
    )
    assert listed.json()["items"] == []

    resolved = await api_env.client.post(
        f"/api/v1/devices/suggestions/{suggestion_id}/resolve",
        json={"resolution": "linked"},
        headers=await api_env.headers(org_b, admin_b),
    )
    assert resolved.status_code == 404, resolved.text

    async with AsyncSession(api_env.admin_engine) as session:
        agent = (
            await session.execute(select(Agent).where(Agent.org_id == org_a))
        ).scalar_one()
    assert agent.device_id == enrolled, "org B moved org A's agent"
