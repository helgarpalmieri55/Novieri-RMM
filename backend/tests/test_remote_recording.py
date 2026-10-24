"""Tests for session-recording finalize + per-org retention (docs/modules/remote-control/spec.md).

Finalize uploads the guacd recording to object storage and links the ref/size
onto the RemoteSession row. Retention deletes recordings past the org's
recording_retention_days (resolved from the org-default RemotePolicy, default
90) and audits the sweep once with the deleted count.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.config import Settings
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.models import Device, Organization, RemotePolicy, RemoteSession, User
from app.modules.remote.recording import apply_recording_retention, finalize_recording


@pytest.fixture()
def store(tmp_path):
    settings = Settings(
        database_url="postgresql+asyncpg://x:x@l/x",
        database_admin_url="postgresql+asyncpg://x:x@l/x",
        oidc_issuer="https://i",
        oidc_jwks_url="https://i/certs",
        ca_dir=tmp_path,
        s3_access_key="test",
        s3_secret_key="test",
        s3_bucket="it-suite-test",
    )
    with mock_aws():
        yield ObjectStore(settings)


async def _org_device_user(admin_engine: AsyncEngine):
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name="rc", slug=f"rc-{uuid4().hex[:6]}")
        s.add(org)
        await s.flush()
        dev = Device(
            org_id=org.id, hostname="h", os_family="linux", client_id=org.id, site_id=org.id
        )
        s.add(dev)
        usr = User(org_id=org.id, external_subject=f"s{uuid4().hex[:6]}", email="a@b.c")
        s.add(usr)
        await s.commit()
        return org.id, dev.id, usr.id


async def test_finalize_uploads_and_links(
    store: ObjectStore, admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id, user_id = await _org_device_user(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        row = RemoteSession(
            org_id=org_id, device_id=device_id, technician_id=user_id,
            protocol="rdp", consent_mode="attended", state="active",
        )
        s.add(row)
        await s.flush()
        sid = row.id
        ref, size = await finalize_recording(
            s, store, org_id=org_id, session_id=sid, data=b"GUAC-REC"
        )
        assert size == 8 and ref.endswith(f"{sid}.guac")
    assert await store.get_remote_recording(ref) == b"GUAC-REC"

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        got = (
            await s.execute(select(RemoteSession).where(RemoteSession.id == sid))
        ).scalar_one()
        assert got.recording_ref == ref and got.recording_bytes == 8


async def test_retention_deletes_expired(
    store: ObjectStore, admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id, user_id = await _org_device_user(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        s.add(RemotePolicy(org_id=org_id, device_group_id=None, recording_retention_days=30))
        old = RemoteSession(
            org_id=org_id, device_id=device_id, technician_id=user_id,
            protocol="rdp", consent_mode="attended", state="ended",
            ended_at=datetime.now(UTC) - timedelta(days=40),
        )
        s.add(old)
        await s.flush()
        sid = old.id
        ref, _ = await finalize_recording(s, store, org_id=org_id, session_id=sid, data=b"OLD")

    deleted = await apply_recording_retention(org_id, admin_engine, app_engine, store)
    assert deleted == 1

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        row = (
            await s.execute(
                select(RemoteSession.recording_ref, RemoteSession.recording_bytes).where(
                    RemoteSession.id == sid
                )
            )
        ).one()
        assert row.recording_ref is None and row.recording_bytes is None

    with pytest.raises(ClientError):
        await store.get_remote_recording(ref)


async def test_retention_ignores_recent_sessions_and_defaults_to_90_days(
    store: ObjectStore, admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, device_id, user_id = await _org_device_user(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        recent = RemoteSession(
            org_id=org_id, device_id=device_id, technician_id=user_id,
            protocol="rdp", consent_mode="attended", state="ended",
            ended_at=datetime.now(UTC) - timedelta(days=10),
        )
        s.add(recent)
        await s.flush()
        sid = recent.id
        await finalize_recording(s, store, org_id=org_id, session_id=sid, data=b"RECENT")

    # no RemotePolicy row at all -> default retention is 90 days, well past 10
    deleted = await apply_recording_retention(org_id, admin_engine, app_engine, store)
    assert deleted == 0

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        row = (
            await s.execute(
                select(RemoteSession.recording_ref).where(RemoteSession.id == sid)
            )
        ).scalar_one()
        assert row is not None


async def test_retention_does_not_expire_a_neighbour_orgs_recording(
    store: ObjectStore, admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """THE SWEEP RUNS ON THE BYPASSRLS ADMIN ENGINE, SO ITS TENANT PREDICATE IS
    THE ONLY THING SCOPING IT.

    `apply_recording_retention` selects the expired sessions and clears
    `recording_ref`/`recording_bytes` through `AsyncSession(admin_engine)`, and
    deletes the object out of the store on the way. Postgres will not reject a
    cross-tenant row on that engine, so `RemoteSession.org_id == org_id` is the
    whole isolation boundary -- and the object delete makes a miss here
    IRREVERSIBLE, unlike a read that merely returns too much.

    The three tests above cannot see that: each seeds a single org, so the
    returned count and the surviving row are identical with or without the
    predicate -- verified by deleting it, at which point all four tests in this
    file and `test_remote_retention_worker.py` still passed. Here the neighbour
    holds a recording of exactly the age the victim's 30-day window sweeps but
    its own 90-day default does not, so the predicate is the only thing that can
    tell the two apart. Both directions are asserted, so it cannot pass by
    sweeping nothing at all.
    """
    org_id, device_id, user_id = await _org_device_user(admin_engine)
    other_id, other_device, other_user = await _org_device_user(admin_engine)
    old = datetime.now(UTC) - timedelta(days=40)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        s.add(RemotePolicy(org_id=org_id, device_group_id=None, recording_retention_days=30))
        mine = RemoteSession(
            org_id=org_id, device_id=device_id, technician_id=user_id,
            protocol="rdp", consent_mode="attended", state="ended", ended_at=old,
        )
        s.add(mine)
        await s.flush()
        mine_id = mine.id
        await finalize_recording(s, store, org_id=org_id, session_id=mine_id, data=b"MINE")

    # No policy for the neighbour: its own window is the 90-day default, so this
    # recording is NOT expired for them -- only for the org being swept.
    async with org_session(other_id, engine=app_engine, clients=ClientScope.all()) as s:
        theirs = RemoteSession(
            org_id=other_id, device_id=other_device, technician_id=other_user,
            protocol="rdp", consent_mode="attended", state="ended", ended_at=old,
        )
        s.add(theirs)
        await s.flush()
        theirs_id = theirs.id
        their_ref, _ = await finalize_recording(
            s, store, org_id=other_id, session_id=theirs_id, data=b"THEIRS"
        )

    deleted = await apply_recording_retention(org_id, admin_engine, app_engine, store)
    assert deleted == 1

    async with org_session(other_id, engine=app_engine, clients=ClientScope.all()) as s:
        row = (
            await s.execute(
                select(RemoteSession.recording_ref, RemoteSession.recording_bytes).where(
                    RemoteSession.id == theirs_id
                )
            )
        ).one()
    assert row.recording_ref == their_ref, "retention expired a neighbour org's recording"
    assert row.recording_bytes == 6
    assert await store.get_remote_recording(their_ref) == b"THEIRS"

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        cleared = (
            await s.execute(
                select(RemoteSession.recording_ref).where(RemoteSession.id == mine_id)
            )
        ).scalar_one()
    assert cleared is None
