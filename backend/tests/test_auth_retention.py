"""Own authentication's retention sweep (final review A-M6; plan 2026-11-06,
Task 3; operator decision C7): what goes after how long, what never goes,
the audit row of a sweep that deleted something, and one organization's
sweep never touching another's rows -- by RLS, and by its own `org_id`
predicate on a session RLS does not filter."""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.auth import retention
from app.core.config import Settings, override_settings
from app.models import AuditEvent, Client, Organization, User
from app.models.auth import AuthActionToken, AuthSession, AuthTrustedDevice, AuthWebauthnChallenge

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
#: Older than the 30 days of devices and links (C7).
OLD = NOW - retention.RETENTION - timedelta(days=1)
#: Older than the 90 days of sessions (C7).
SESSION_OLD = NOW - retention.SESSION_RETENTION - timedelta(days=1)
RECENT = NOW - timedelta(days=1)


@dataclass(frozen=True)
class _World:
    org_id: UUID
    staff_id: UUID
    contact_id: UUID


async def _world(admin_engine: AsyncEngine) -> _World:
    slug = f"ret-{uuid4().hex[:8]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name=slug, slug=slug)
        s.add(org)
        await s.flush()
        client = Client(org_id=org.id, name=f"Acme-{slug}", kind="customer")
        s.add(client)
        await s.flush()
        staff = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"t@{slug}.test")
        contact = User(
            org_id=org.id,
            kind="contact",
            client_id=client.id,
            email=f"c@{slug}.test",
            external_subject=f"contact-pending:{uuid4()}",
        )
        s.add_all([staff, contact])
        await s.commit()
        return _World(org.id, staff.id, contact.id)


def _hash() -> bytes:
    return secrets.token_bytes(32)


async def _seed(admin_engine: AsyncEngine, w: _World) -> dict[str, UUID]:
    """Every case of decision 1, named; returns name -> row id. Ids are set
    here, not by the model's default, so they exist before the flush."""
    ids: dict[str, UUID] = {}
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:

        def device(name: str, user: UUID, *, expires: datetime, revoked: datetime | None) -> None:
            row = AuthTrustedDevice(
                id=uuid4(),
                org_id=w.org_id,
                user_id=user,
                token_hash=_hash(),
                created_at=expires - timedelta(days=15),
                expires_at=expires,
                revoked_at=revoked,
            )
            s.add(row)
            ids[name] = row.id

        device("device_old_free", w.staff_id, expires=OLD, revoked=OLD)
        device("device_old_named", w.staff_id, expires=OLD, revoked=None)
        device("device_live", w.contact_id, expires=NOW + timedelta(days=10), revoked=None)
        await s.flush()

        def session(
            name: str,
            user: UUID,
            kind: str,
            *,
            expires: datetime,
            ended: datetime | None = None,
            on_device: str | None = None,
        ) -> None:
            row = AuthSession(
                id=uuid4(),
                org_id=w.org_id,
                user_id=user,
                token_hash=_hash(),
                kind=kind,
                created_at=expires - timedelta(hours=12),
                last_seen_at=expires - timedelta(hours=12),
                expires_at=expires,
                ended_at=ended,
                ended_reason="signed_out" if ended is not None else None,
                trusted_device_id=None if on_device is None else ids[on_device],
            )
            s.add(row)
            ids[name] = row.id

        session(
            "session_ended_old",
            w.staff_id,
            "staff",
            expires=SESSION_OLD + timedelta(hours=1),
            ended=SESSION_OLD,
        )
        session("session_expired_old", w.contact_id, "contact", expires=SESSION_OLD)
        # A later bulk act (sign out everywhere, a password change) stamps `ended_at`
        # on rows that had already expired: the older of the two still counts.
        session(
            "session_expired_old_ended_recent",
            w.staff_id,
            "staff",
            expires=SESSION_OLD,
            ended=RECENT,
        )
        # Ended 31 days ago: past the 30 days of the other rows, within the 90 of sessions.
        session("session_ended_month", w.staff_id, "staff", expires=OLD, ended=OLD)
        session("session_ended_recent", w.staff_id, "staff", expires=NOW, ended=RECENT)
        session("session_live", w.contact_id, "contact", expires=NOW + timedelta(hours=1))
        session(
            "session_live_on_device",
            w.staff_id,
            "staff",
            expires=NOW + timedelta(days=1),
            on_device="device_old_named",
        )

        def link(
            name: str, user: UUID, purpose: str, *, expires: datetime, used: datetime | None = None
        ) -> None:
            row = AuthActionToken(
                id=uuid4(),
                org_id=w.org_id,
                user_id=user,
                purpose=purpose,
                token_hash=_hash(),
                created_at=expires - timedelta(hours=1),
                expires_at=expires,
                used_at=used,
            )
            s.add(row)
            ids[name] = row.id

        link("staff_invite_pending_old", w.staff_id, "invite", expires=OLD - timedelta(days=60))
        link("contact_invite_expired_old", w.contact_id, "invite", expires=OLD)
        link("contact_invite_expired_recent", w.contact_id, "invite", expires=RECENT)
        link("reset_expired_old", w.staff_id, "reset", expires=OLD)
        link("recover_used_old", w.staff_id, "recover", expires=OLD, used=OLD)
        link("invite_used_old", w.contact_id, "invite", expires=OLD, used=OLD)
        link("reset_expired_recent", w.staff_id, "reset", expires=RECENT)

        def challenge(
            name: str, user: UUID | None, *, expires: datetime, used: datetime | None = None
        ) -> None:
            row = AuthWebauthnChallenge(
                id=uuid4(),
                org_id=w.org_id,
                user_id=user,
                purpose="authenticate",
                challenge=secrets.token_bytes(32),
                expires_at=expires,
                used_at=used,
            )
            s.add(row)
            ids[name] = row.id

        two_days = NOW - timedelta(days=2)
        challenge("challenge_used_old", w.staff_id, expires=two_days, used=two_days)
        challenge("challenge_expired_hours", None, expires=NOW - timedelta(hours=2))
        challenge("challenge_live", w.staff_id, expires=NOW + timedelta(minutes=5))
        await s.commit()
    return ids


async def _left(admin_engine: AsyncEngine, org_id: UUID) -> set[UUID]:
    async with AsyncSession(admin_engine) as s:
        found: set[UUID] = set()
        for model in (AuthSession, AuthTrustedDevice, AuthActionToken, AuthWebauthnChallenge):
            rows = await s.execute(select(model.id).where(model.org_id == org_id))
            found |= set(rows.scalars())
        return found


GONE = {
    "device_old_free",
    "session_ended_old",
    "session_expired_old",
    "session_expired_old_ended_recent",
    "contact_invite_expired_old",
    "reset_expired_old",
    "recover_used_old",
    "invite_used_old",
    "challenge_used_old",
}


async def test_the_sweep_removes_what_can_never_serve_again_and_nothing_else(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    w = await _world(admin_engine)
    ids = await _seed(admin_engine, w)
    swept = await retention.sweep_org(w.org_id, app_engine, now=NOW)
    left = await _left(admin_engine, w.org_id)
    assert swept == len(GONE)
    assert {name for name, row in ids.items() if row not in left} == GONE


async def test_sessions_are_kept_ninety_days_and_the_rest_thirty(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """C7: a session ended 31 days ago keeps its address and browser; a
    reset link expired as long ago does not."""
    w = await _world(admin_engine)
    ids = await _seed(admin_engine, w)
    await retention.sweep_org(w.org_id, app_engine, now=NOW)
    left = await _left(admin_engine, w.org_id)
    assert ids["session_ended_month"] in left
    assert ids["reset_expired_old"] not in left


async def test_a_technicians_pending_invitation_survives_however_old(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Decision 1: `_open_invites` reads a technician's unused invitation,
    expired or not, as pending; sweeping it would strand the person. A
    contact's expired one goes (A2-13)."""
    w = await _world(admin_engine)
    ids = await _seed(admin_engine, w)
    await retention.sweep_org(w.org_id, app_engine, now=NOW + timedelta(days=3650))
    left = await _left(admin_engine, w.org_id)
    assert ids["staff_invite_pending_old"] in left
    assert ids["contact_invite_expired_recent"] not in left


async def test_a_device_a_session_still_names_is_kept(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    w = await _world(admin_engine)
    ids = await _seed(admin_engine, w)
    await retention.sweep_org(w.org_id, app_engine, now=NOW)
    assert ids["device_old_named"] in await _left(admin_engine, w.org_id)


async def test_a_sweep_that_deleted_something_is_audited_and_one_that_did_not_is_not(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """B2-15: deleting evidence is a privileged act (invariant 4)."""
    w = await _world(admin_engine)
    await _seed(admin_engine, w)
    await retention.sweep_org(w.org_id, app_engine, now=NOW)
    await retention.sweep_org(w.org_id, app_engine, now=NOW)
    async with AsyncSession(admin_engine) as s:
        events = (
            await s.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == w.org_id, AuditEvent.action == "auth.retention_applied"
                )
            )
        ).scalars().all()
    assert len(events) == 1
    [event] = events
    assert retention.AUDIT_ACTION == event.action == "auth.retention_applied"
    assert (event.actor_type, event.actor_id) == ("system", None)
    assert event.meta == {
        "at": NOW.isoformat(),
        "sessions": 3,
        "devices": 1,
        "links": 4,
        "challenges": 1,
    }


async def test_one_organizations_sweep_leaves_another_alone(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    mine, theirs = await _world(admin_engine), await _world(admin_engine)
    await _seed(admin_engine, mine)
    their_ids = await _seed(admin_engine, theirs)
    await retention.sweep_org(mine.org_id, app_engine, now=NOW)
    assert set(their_ids.values()) <= await _left(admin_engine, theirs.org_id)


async def test_the_sweep_carries_its_own_org_predicate(
    admin_engine: AsyncEngine, bypassrls_session: AsyncSession
) -> None:
    """Decision 3: on a session RLS does not filter, the `org_id` predicate
    is the only thing that keeps one organization's sweep off another's rows.
    Delete it from any step and this goes red."""
    mine, theirs = await _world(admin_engine), await _world(admin_engine)
    await _seed(admin_engine, mine)
    their_ids = await _seed(admin_engine, theirs)
    swept = await retention.sweep(bypassrls_session, mine.org_id, now=NOW)
    await bypassrls_session.commit()
    assert swept.total == len(GONE)
    assert set(their_ids.values()) <= await _left(admin_engine, theirs.org_id)


def test_the_operators_retention_periods_are_pinned() -> None:
    """C7, 2026-11-06: sessions 90 days, devices and links 30, challenges 1."""
    assert (
        retention.SESSION_RETENTION,
        retention.RETENTION,
        retention.CHALLENGE_RETENTION,
    ) == (timedelta(days=90), timedelta(days=30), timedelta(days=1))


def test_the_registry_is_ordered_sessions_first() -> None:
    """A device is kept while a remaining session names it, so sessions go
    first. Phase 3 appends its own steps after these four."""
    assert [name for name, _step in retention.STEPS][:4] == [
        "sessions",
        "devices",
        "links",
        "challenges",
    ]


def test_the_sweep_runs_daily(tmp_path: Path) -> None:
    from app.worker import create_celery

    settings = Settings(
        database_url="postgresql+asyncpg://x:x@l/x",
        database_admin_url="postgresql+asyncpg://x:x@l/x",
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
    )
    with override_settings(settings):
        entry = create_celery().conf.beat_schedule["auth-retention"]
    assert entry["task"] == "app.worker.auth_retention"
    assert entry["schedule"] == retention.SWEEP_INTERVAL_S == 86400.0
