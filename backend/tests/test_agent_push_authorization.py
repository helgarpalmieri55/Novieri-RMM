"""`push_authorization`: the grant that decides where this platform may present
an administrative credential (spec §3 condition 2, §9).

**SECURITY-CRITICAL.** A row here is the second of invariant 2a clause (d)'s
three authorizations. Every fixture below is built so that a mutation which
WIDENS what may be pushed to, or which lets an unauthorized address through,
fails loudly.

WHAT THESE FIXTURES DELIBERATELY VARY -- fixture blindness is this
repository's defining defect, so each guard is paired with a fixture in which
the guarded and the unguarded path produce DIFFERENT OBSERVABLE VALUES:

* **THE TENANCY TESTS RUN ON THE BYPASSRLS ADMIN ENGINE.** `org_session` runs
  as `app_user`, which is NOBYPASSRLS under a forced `tenant_isolation`
  policy, so a `SELECT` carrying no tenant predicate at all returns exactly
  the rows a correct one would -- deleting `WHERE org_id` from a service turns
  NO route test and NO `org_session` test red. On the admin engine Postgres
  stops filtering and the application's own predicate is the only thing
  scoping anything. Each of these was falsified by deleting the predicate it
  names; the failures are in the task report.
* **THE "NOT AUTHORIZED" CASES SEED A REVOKED OR EXPIRED ROW FOR THE EXACT
  PREFIX**, never nothing. A test that seeds nothing and asserts a refusal
  passes against a function that refuses everything AND against one that never
  looks. The revoked row is what makes the refusal evidence.
* **THE RE-GRANT TEST ASSERTS TWO ROWS SURVIVE**, not that no exception was
  raised: the defect a total unique index causes is an operator unable to
  re-authorize a segment, and "it did not raise" cannot see it.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.models import AuditEvent, PushAuthorization
from app.modules.agent_push.authorization import (
    create_authorization,
    list_authorizations,
    revoke_authorization,
)
from app.modules.agent_push.errors import (
    PushConflictError,
    PushNotFoundError,
    PushValidationError,
)
from tests.agent_push_fixtures import seed_org, seed_push_authorization

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Creating a grant
# --------------------------------------------------------------------------- #


async def test_a_grant_is_created_canonicalised_and_audited(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The host bits are rounded, and the audit event states the reach in
    ADDRESSES -- 256, not "/24". An auditor reading this a year from now must
    not have to convert a mask length in their head to see how far the grant
    went."""
    org = await seed_org(admin_engine, "push-create")
    async with org_session(org.org_id, engine=app_engine) as session:
        row = await create_authorization(
            session,
            org.org_id,
            actor_id=org.user_id,
            prefix="10.4.7.9/24",
            note="branch onboarding",
        )

    assert str(row.prefix) == "10.4.7.0/24"

    async with AsyncSession(admin_engine) as session:
        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org.org_id,
                    AuditEvent.action == "push.authorization_created",
                )
            )
        ).scalar_one()
    assert event.meta["prefix"] == "10.4.7.0/24"
    assert event.meta["requested_prefix"] == "10.4.7.9/24"
    assert event.meta["address_count"] == 256
    assert event.meta["note"] == "branch onboarding"
    assert str(event.actor_id) == str(org.user_id)


async def test_a_grant_with_no_actor_is_refused_before_the_database(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """There is no system-authorized push scope. The service refuses first so
    the message names the rule rather than a constraint name."""
    org = await seed_org(admin_engine, "push-noactor-svc")
    async with org_session(org.org_id, engine=app_engine) as session:
        with pytest.raises(PushValidationError) as exc:
            await create_authorization(
                session, org.org_id, actor_id=None, prefix="10.0.0.0/24"
            )
    assert "no system-authorized push scope" in str(exc.value)


@pytest.mark.parametrize(
    "prefix", ["", "  ", "not-a-prefix", "10.0.0.0/33", "10.0.0.256/24"]
)
async def test_a_malformed_prefix_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, prefix: str
) -> None:
    org = await seed_org(admin_engine, "push-badprefix")
    async with org_session(org.org_id, engine=app_engine) as session:
        with pytest.raises(PushValidationError):
            await create_authorization(
                session, org.org_id, actor_id=org.user_id, prefix=prefix
            )


async def test_a_second_active_grant_for_one_prefix_is_a_conflict(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Not an `IntegrityError` leaking out of the ORM: an operator sees a 409
    naming the remedy. Without uniqueness, revoking "the" grant for a prefix
    leaves a second one nobody was looking at still authorizing it."""
    org = await seed_org(admin_engine, "push-dupe-svc")
    async with org_session(org.org_id, engine=app_engine) as session:
        await create_authorization(
            session, org.org_id, actor_id=org.user_id, prefix="10.5.0.0/24"
        )
    async with org_session(org.org_id, engine=app_engine) as session:
        with pytest.raises(PushConflictError) as exc:
            await create_authorization(
                session, org.org_id, actor_id=org.user_id, prefix="10.5.0.0/24"
            )
    assert "revoke the existing authorization" in str(exc.value)


async def test_a_revoked_prefix_may_be_authorized_again(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """THE BEHAVIOUR THE PARTIAL INDEX EXISTS FOR, through the service.

    Asserted as TWO SURVIVING ROWS rather than "no exception": with a total
    unique index the second grant raises, and with a hard DELETE on revoke the
    evidence of the first grant is gone -- two different defects that "it did
    not raise" cannot tell apart.
    """
    org = await seed_org(admin_engine, "push-regrant-svc")
    # THREE SEPARATE TRANSACTIONS, deliberately: `org_session` commits on exit,
    # and the sequence being tested is grant -> revoke -> grant as three
    # operator acts, not three statements one operator ran without leaving.
    async with org_session(org.org_id, engine=app_engine) as session:
        first = await create_authorization(
            session, org.org_id, actor_id=org.user_id, prefix="10.6.0.0/24"
        )
        first_id = first.id
    async with org_session(org.org_id, engine=app_engine) as session:
        await revoke_authorization(
            session, org.org_id, actor_id=org.user_id, authorization_id=first_id
        )
    async with org_session(org.org_id, engine=app_engine) as session:
        await create_authorization(
            session, org.org_id, actor_id=org.user_id, prefix="10.6.0.0/24"
        )

    async with AsyncSession(admin_engine) as session:
        total = (
            await session.execute(
                select(func.count())
                .select_from(PushAuthorization)
                .where(
                    PushAuthorization.org_id == org.org_id,
                    PushAuthorization.prefix == "10.6.0.0/24",
                )
            )
        ).scalar_one()
    assert total == 2


# --------------------------------------------------------------------------- #
# Revoking
# --------------------------------------------------------------------------- #


async def test_revocation_stamps_and_the_row_stays(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """"Who authorized presenting credentials on that segment in March" must
    stay answerable, which a DELETE destroys."""
    org = await seed_org(admin_engine, "push-revoke")
    row_id = await seed_push_authorization(admin_engine, org, "10.7.0.0/24")
    async with org_session(org.org_id, engine=app_engine) as session:
        await revoke_authorization(
            session, org.org_id, actor_id=org.user_id, authorization_id=row_id
        )

    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                select(PushAuthorization).where(PushAuthorization.id == row_id)
            )
        ).scalar_one()
        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org.org_id,
                    AuditEvent.action == "push.authorization_revoked",
                )
            )
        ).scalar_one()
    assert row.revoked_at is not None
    assert row.revoked_by == org.user_id
    assert event.meta["prefix"] == "10.7.0.0/24"


async def test_revoking_twice_is_a_conflict(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org = await seed_org(admin_engine, "push-revoke2")
    row_id = await seed_push_authorization(admin_engine, org, "10.8.0.0/24", revoked=True)
    async with org_session(org.org_id, engine=app_engine) as session:
        with pytest.raises(PushConflictError):
            await revoke_authorization(
                session, org.org_id, actor_id=org.user_id, authorization_id=row_id
            )


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


async def test_an_expired_grant_is_listed_and_a_revoked_one_is_not(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """THREE ROWS, DIFFERING ONLY IN WHAT SHOULD HAPPEN TO THEM. A fixture
    with one row cannot tell "the filter works" from "everything is filtered"
    or "nothing is". An operator needs to SEE the grant that lapsed last night
    -- hiding it makes "expired" and "never existed" the same blank list."""
    org = await seed_org(admin_engine, "push-list")
    await seed_push_authorization(admin_engine, org, "10.20.0.0/24")
    await seed_push_authorization(
        admin_engine, org, "10.21.0.0/24", expires_at=NOW - timedelta(days=1)
    )
    await seed_push_authorization(admin_engine, org, "10.22.0.0/24", revoked=True)

    async with org_session(org.org_id, engine=app_engine) as session:
        active = {str(r.prefix) for r in await list_authorizations(session, org.org_id)}
        everything = {
            str(r.prefix)
            for r in await list_authorizations(
                session, org.org_id, include_revoked=True
            )
        }
    assert active == {"10.20.0.0/24", "10.21.0.0/24"}
    assert everything == {"10.20.0.0/24", "10.21.0.0/24", "10.22.0.0/24"}


# --------------------------------------------------------------------------- #
# Tenancy, ON THE ADMIN ENGINE (see the module docstring)
# --------------------------------------------------------------------------- #


async def test_listing_does_not_return_another_orgs_grants_on_the_admin_engine(
    bypassrls_session: AsyncSession, admin_engine: AsyncEngine
) -> None:
    """FALSIFIED BY DELETING `PushAuthorization.org_id == org_id` from
    `list_authorizations`. Through `org_session` that deletion changes nothing
    at all, because RLS rejects org B's rows before the application sees them.
    """
    a = await seed_org(admin_engine, "push-ten-a")
    b = await seed_org(admin_engine, "push-ten-b")
    await seed_push_authorization(admin_engine, a, "10.30.0.0/24")
    await seed_push_authorization(admin_engine, b, "10.31.0.0/24")

    seen = {
        str(r.prefix) for r in await list_authorizations(bypassrls_session, a.org_id)
    }
    assert seen == {"10.30.0.0/24"}, (
        "org A's list returned org B's grant: the application `org_id` predicate "
        "is gone and RLS is not there to hide it"
    )


async def test_revoking_another_orgs_grant_is_not_found_on_the_admin_engine(
    bypassrls_session: AsyncSession, admin_engine: AsyncEngine
) -> None:
    """404 AND NOT 403, and not a successful revocation.

    FALSIFIED BY DELETING `PushAuthorization.org_id == org_id` from
    `revoke_authorization`'s lookup: org A then revokes org B's authorization,
    which is a silent, cross-tenant change to where org B may push.
    """
    a = await seed_org(admin_engine, "push-ten-rev-a")
    b = await seed_org(admin_engine, "push-ten-rev-b")
    b_row = await seed_push_authorization(admin_engine, b, "10.32.0.0/24")

    with pytest.raises(PushNotFoundError):
        await revoke_authorization(
            bypassrls_session,
            a.org_id,
            actor_id=a.user_id,
            authorization_id=b_row,
        )

    row = (
        await bypassrls_session.execute(
            select(PushAuthorization).where(PushAuthorization.id == b_row)
        )
    ).scalar_one()
    assert row.revoked_at is None, "org A revoked org B's authorization"


async def test_a_grant_created_for_one_org_is_invisible_to_the_other_on_the_admin_engine(
    bypassrls_session: AsyncSession, admin_engine: AsyncEngine
) -> None:
    """The write side of the same predicate. `create_authorization` takes
    `org_id` as an argument, so a row written under org A must never appear in
    org B's list even with Postgres filtering nothing."""
    a = await seed_org(admin_engine, "push-ten-w-a")
    b = await seed_org(admin_engine, "push-ten-w-b")
    await create_authorization(
        bypassrls_session, a.org_id, actor_id=a.user_id, prefix="10.33.0.0/24"
    )
    await bypassrls_session.commit()

    assert await list_authorizations(bypassrls_session, b.org_id) == []


async def test_a_uuid_that_does_not_exist_is_not_found(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org = await seed_org(admin_engine, "push-missing")
    async with org_session(org.org_id, engine=app_engine) as session:
        with pytest.raises(PushNotFoundError):
            await revoke_authorization(
                session, org.org_id, actor_id=org.user_id, authorization_id=uuid4()
            )
