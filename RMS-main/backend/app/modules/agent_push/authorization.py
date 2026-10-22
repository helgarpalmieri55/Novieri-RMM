"""`push_authorization`: where this platform may present an administrative
credential at all (spec §3 condition 2, §9; `CLAUDE.md` invariant 2a (d)).

**SECURITY-CRITICAL. THIS IS ONE OF THE MODULE'S TWO CONTROL SURFACES.** A row
here is an operator's statement that a prefix may receive credentialed logins
from this platform. `net.sweep`'s equivalent decides where ICMP echoes go; this
one decides where a domain-capable secret is presented to a machine nobody has
enrolled and nothing can authenticate.

**DENY BY DEFAULT, AND NOT IMPLIED BY ANYTHING ELSE.** No active row covering
an address means no push to that address, ever. In particular an
`ipam_scan_authorization` covering the same prefix grants NOTHING here (spec
§3, D9): authorizing an ICMP echo is not authorizing an administrative login,
and an operator who authorized a /8 sweep last month has not consented to
credentialed logins across sixteen million addresses. That is
`ipam/authorization.py`'s own argument -- "documenting is a filing action,
authorizing packets against a range is a different decision by a different
person" -- applied one notch up.

**`authorized_by` IS NOT NULL: THERE IS NO SYSTEM-AUTHORIZED PUSH SCOPE.** The
service refuses `actor_id=None` before the database has to, so a background
job cannot come to authorize an administrative login with nobody's name on it.

**ROWS ARE NEVER HARD-DELETED.** Revocation stamps `revoked_at`/`revoked_by`,
so "who authorized presenting credentials on that segment in March" stays
answerable. The unique index is PARTIAL on `revoked_at IS NULL`, which is what
lets a revoked prefix be authorized again -- and which means every upsert
against it must INFER (`index_elements=` + `index_where=`); `ON CONFLICT ON
CONSTRAINT` fails at runtime against a partial index.

**REVOCATION CANNOT RECALL A SIGNED JOB, WHICH IS WHY REFUSAL SITS ON THE NEAR
SIDE OF THE SIGNING BOUNDARY** (`preflight.py`). What revocation does is stop
the NEXT dispatch, immediately and permanently, and leave the record.

**EVERY QUERY CARRIES AN EXPLICIT `org_id` PREDICATE**, in addition to RLS
(invariant #5). RLS MASKS AN APPLICATION PREDICATE: through `org_session` a
`SELECT` with no tenant predicate returns exactly the rows a correct one
would, because `app_user` is NOBYPASSRLS and Postgres filters first -- so
deleting `WHERE org_id` here turns no route test red.
`tests/test_agent_push_authorization.py` pins these on the BYPASSRLS admin
engine, where the application predicate is the only thing scoping anything.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import text

from app.core.audit import audit_service
from app.core.i18n import tr
from app.modules.agent_push.errors import (
    PushConflictError,
    PushNotFoundError,
    PushValidationError,
)
from app.modules.agent_push.models import PushAuthorization
from app.modules.agent_push.net import parse_prefix, prefix_size

__all__ = [
    "create_authorization",
    "list_authorizations",
    "revoke_authorization",
]

_MAX_NOTE = 500


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def create_authorization(
    session: AsyncSession,
    org_id: UUID,
    *,
    actor_id: UUID | None,
    prefix: str,
    note: str = "",
    expires_at: datetime | None = None,
) -> PushAuthorization:
    """Authorize credentialed pushes inside `prefix`. HIGH PRIVILEGE.

    Gated on `agent.push.scope.manage` at the API -- deliberately separate from
    `agent.push.run`, because pushing inside an already-authorized prefix and
    deciding what may be authorized at all are different decisions by
    different people. Collapsing them would let anyone who can run a push also
    decide where pushes may go, which is the whole control.
    """
    if actor_id is None:
        raise PushValidationError(
            tr(
                "authorized_by is required: there is no system-authorized push scope (spec "
                "§3). A human with agent.push.scope.manage puts their name on every "
                "administrative login this platform causes"
            )
        )
    network = parse_prefix(prefix)
    if not isinstance(note, str) or len(note) > _MAX_NOTE:
        raise PushValidationError(
            tr("note must be a string of at most {max_note} characters", max_note=_MAX_NOTE)
        )
    if expires_at is not None and expires_at.tzinfo is None:
        raise PushValidationError(tr("expires_at must be timezone-aware"))

    now = _utcnow()
    inserted_id = (
        await session.execute(
            pg_insert(PushAuthorization)
            .values(
                org_id=org_id,
                prefix=str(network),
                note=note,
                authorized_by=actor_id,
                authorized_at=now,
                expires_at=expires_at,
            )
            # INFERENCE, not `ON CONFLICT ON CONSTRAINT`:
            # `uq_push_authorization_active` is a PARTIAL unique index
            # (`WHERE revoked_at IS NULL`) and a partial index has no
            # `pg_constraint` row to name. The predicate is written exactly as
            # migration 0077 wrote it -- Postgres infers the matching index
            # from the column list plus the predicate, and a
            # differently-shaped-but-equivalent predicate infers nothing.
            .on_conflict_do_nothing(
                index_elements=["org_id", "prefix"],
                index_where=text("revoked_at IS NULL"),
            )
            .returning(PushAuthorization.id)
        )
    ).scalar_one_or_none()
    if inserted_id is None:
        raise PushConflictError(
            tr(
                "{network} is already authorized for push install in this organization; "
                "revoke the existing authorization before creating another",
                network=network,
            )
        )

    row = (
        await session.execute(
            select(PushAuthorization).where(
                PushAuthorization.org_id == org_id,
                PushAuthorization.id == inserted_id,
            )
        )
    ).scalar_one()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="push.authorization_created",
        target_type="push_authorization",
        target_id=str(row.id),
        metadata={
            "prefix": str(network),
            "requested_prefix": str(prefix),
            # IN ADDRESSES, not in mask lengths (spec §6.3, §10.4). An auditor
            # reading "10.0.0.0/8" a year from now should not have to convert
            # it in their head to see that somebody authorized credentialed
            # logins across sixteen million machines.
            "address_count": prefix_size(network),
            "note": note,
            "expires_at": expires_at.isoformat() if expires_at else None,
        },
    )
    await session.flush()
    return row


async def revoke_authorization(
    session: AsyncSession,
    org_id: UUID,
    *,
    actor_id: UUID | None,
    authorization_id: UUID,
) -> None:
    """Stamp `revoked_at`/`revoked_by`. THE ROW STAYS.

    Revocation cannot recall an already-signed in-flight job. In cycle B1 no
    job can exist at all, and in B2 the exposure window is the push job's TTL,
    which spec §4.3 pins at 300s -- shorter than `net.sweep`'s 600s, because
    here the TTL bounds a decrypted CREDENTIAL rather than a target list.
    """
    row = (
        await session.execute(
            select(PushAuthorization).where(
                PushAuthorization.org_id == org_id,
                PushAuthorization.id == authorization_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise PushNotFoundError(
            tr("push authorization {authorization_id} not found", authorization_id=authorization_id)
        )
    if row.revoked_at is not None:
        raise PushConflictError(
            tr(
                "push authorization {authorization_id} is already revoked",
                authorization_id=authorization_id,
            )
        )
    row.revoked_at = _utcnow()
    row.revoked_by = actor_id
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="push.authorization_revoked",
        target_type="push_authorization",
        target_id=str(row.id),
        metadata={
            "prefix": str(row.prefix),
            "note": row.note,
            "authorized_by": str(row.authorized_by),
        },
    )
    await session.flush()


async def list_authorizations(
    session: AsyncSession, org_id: UUID, *, include_revoked: bool = False
) -> list[PushAuthorization]:
    """Active authorizations, or every one ever written.

    "Active" here is `revoked_at IS NULL` only -- an EXPIRED row is still
    listed, because an operator looking at this list needs to see the grant
    that lapsed last night as much as the one that never existed. `preflight`
    applies the expiry; the list surfaces it.
    """
    query = select(PushAuthorization).where(PushAuthorization.org_id == org_id)
    if not include_revoked:
        query = query.where(PushAuthorization.revoked_at.is_(None))
    return list(
        (await session.execute(query.order_by(PushAuthorization.prefix))).scalars().all()
    )
