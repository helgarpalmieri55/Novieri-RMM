"""Migration 0032: shape, RLS, and permission seeding (spec §2, §9, §12).

The two RLS tests are BEHAVIOURAL -- they insert two orgs' rows through the
RLS-constrained app engine and assert on what each org can see. A test that
merely asserts `relrowsecurity` is true passes against a policy whose
predicate has been deleted.

NOTE ON THE MODEL IMPORT. `EmailMailbox` is imported from `app.models` (the
aggregator), not `app.modules.email_intake.models` directly. `email_intake/
models.py` itself does `from app.models.base import Base, uuid7_pk`; if this
file imported that submodule path before anything had imported the
`app.models` package as a whole, Python would start executing
`app/models/__init__.py` (needed to reach `app.models.base`) -- which itself
imports `app.modules.email_intake.models` to register the new tables on
`Base.metadata` for `test_schema_drift.py` -- and re-enter the
still-executing `email_intake/models` module, raising a genuine circular
`ImportError`. Importing via `app.models` (as every other test file in this
suite does) avoids it entirely.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import EmailMailbox, InboundMessage


async def test_ticket_has_a_unique_nullable_email_token(admin_engine) -> None:
    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                text(
                    "SELECT is_nullable, character_maximum_length "
                    "FROM information_schema.columns "
                    "WHERE table_name = 'ticket' AND column_name = 'email_token'"
                )
            )
        ).one()
    assert row.is_nullable == "YES"
    assert row.character_maximum_length == 43


async def test_users_has_a_lower_email_index(admin_engine) -> None:
    """Spec §5.1: sender resolution runs on every inbound message and would
    otherwise sequentially scan the org's users on the poller's hot path."""
    async with AsyncSession(admin_engine) as session:
        indexdefs = (
            (
                await session.execute(
                    text("SELECT indexdef FROM pg_indexes WHERE tablename = 'users'")
                )
            )
            .scalars()
            .all()
        )
    # Postgres normalizes the functional index expression to
    # `lower((email)::text)` (an explicit cast, since `email` is varchar), so
    # a literal "lower(email" substring never appears even for a perfectly
    # correct index -- match on the looser, still-unambiguous substrings.
    assert any("lower(" in d and "email" in d and "org_id" in d for d in indexdefs), indexdefs


@pytest.mark.parametrize("table", ["email_mailbox", "inbound_message"])
async def test_rls_is_enabled_and_forced(admin_engine, table: str) -> None:
    async with AsyncSession(admin_engine) as session:
        row = (
            await session.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = :t"
                ),
                {"t": table},
            )
        ).one()
    assert row.relrowsecurity is True
    assert row.relforcerowsecurity is True


async def test_a_mailbox_is_invisible_to_another_org(api_env) -> None:
    """Behavioural: fails if the tenant_isolation USING predicate is removed."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    category_id = await _seed_category(api_env, org_a)
    actor_id = await _seed_user(api_env, org_a)

    async with org_session(org_a, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        session.add(
            EmailMailbox(
                org_id=org_a,
                address="helpdesk@a.test",
                host="imap.a.test",
                port=993,
                username="helpdesk@a.test",
                credential_id=await _seed_credential(session, org_a, actor_id),
                default_category_id=category_id,
                created_by=actor_id,
            )
        )

    async with org_session(org_a, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        assert len((await session.execute(select(EmailMailbox))).scalars().all()) == 1
    async with org_session(org_b, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        assert (await session.execute(select(EmailMailbox))).scalars().all() == []


async def test_a_mailbox_cannot_be_inserted_under_another_orgs_id(api_env) -> None:
    """WITH CHECK, not just USING. Without it the row above would insert
    happily under org_b's id while being invisible to org_b's reads."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    category_id = await _seed_category(api_env, org_a)
    actor_id = await _seed_user(api_env, org_a)

    with pytest.raises(ProgrammingError):
        async with org_session(
            org_a, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            credential_id = await _seed_credential(session, org_a, actor_id)
            session.add(
                EmailMailbox(
                    org_id=org_b,  # <-- the other org
                    address="helpdesk@b.test",
                    host="imap.b.test",
                    port=993,
                    username="helpdesk@b.test",
                    credential_id=credential_id,
                    default_category_id=category_id,
                    created_by=actor_id,
                )
            )


async def test_inbound_message_is_unique_per_org_and_message_id(api_env) -> None:
    """Spec §4 rule 1: this constraint IS the idempotency mechanism.

    Asserts the actual column list AND order, not substrings -- a substring
    check (`"org_id" in d and "message_id" in d`) is equally satisfied by a
    unique index on `(message_id, org_id)`, or on a three-column index that
    would silently destroy the idempotency guarantee this test exists to
    protect.
    """
    async with AsyncSession(api_env.admin_engine) as session:
        indexdef = (
            await session.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE indexname = 'uq_inbound_message_org_message_id'"
                )
            )
        ).scalar_one()
    assert "UNIQUE" in indexdef
    columns = indexdef.split("(", 1)[1].rsplit(")", 1)[0]
    assert [c.strip() for c in columns.split(",")] == ["org_id", "message_id"], indexdef


async def test_email_mailbox_rejects_a_duplicate_org_and_address(api_env) -> None:
    """F4: the unique constraint must be proven behaviourally, not merely by
    reading `pg_indexes` text -- insert a duplicate `(org_id, address)` and
    assert the database rejects it."""
    org_id = await api_env.create_org("dup")
    category_id = await _seed_category(api_env, org_id)
    actor_id = await _seed_user(api_env, org_id)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        session.add(
            EmailMailbox(
                org_id=org_id,
                address="helpdesk@dup.test",
                host="imap.dup.test",
                port=993,
                username="helpdesk@dup.test",
                credential_id=await _seed_credential(session, org_id, actor_id),
                default_category_id=category_id,
                created_by=actor_id,
            )
        )

    with pytest.raises(IntegrityError):
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            session.add(
                EmailMailbox(
                    org_id=org_id,
                    address="helpdesk@dup.test",  # <-- same (org_id, address)
                    host="imap.dup2.test",
                    port=993,
                    username="helpdesk@dup.test",
                    credential_id=await _seed_credential(session, org_id, actor_id),
                    default_category_id=category_id,
                    created_by=actor_id,
                )
            )


async def test_inbound_message_disposition_check_rejects_invalid_value(api_env) -> None:
    """F6: `ck_inbound_message_disposition_valid` is a strengthening beyond
    spec §2.2's stated set of seven; prove the database actually enforces
    it rather than trusting the model's/migration's literal lists to agree."""
    org_id = await api_env.create_org("chk")
    category_id = await _seed_category(api_env, org_id)
    actor_id = await _seed_user(api_env, org_id)

    with pytest.raises(IntegrityError):
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            mailbox = EmailMailbox(
                org_id=org_id,
                address="helpdesk@chk.test",
                host="imap.chk.test",
                port=993,
                username="helpdesk@chk.test",
                credential_id=await _seed_credential(session, org_id, actor_id),
                default_category_id=category_id,
                created_by=actor_id,
            )
            session.add(mailbox)
            await session.flush()
            session.add(
                InboundMessage(
                    org_id=org_id,
                    mailbox_id=mailbox.id,
                    message_id=f"<{uuid4().hex}@chk.test>",
                    from_address="user@chk.test",
                    received_at=datetime.now(UTC),
                    disposition="bogus",  # <-- not one of the seven
                )
            )


async def test_an_inbound_message_is_invisible_to_another_org(api_env) -> None:
    """F2: behavioural cross-org test for `inbound_message`, mirroring the
    `email_mailbox` pair above (spec §9, §11.11 require one per table).
    Fails if the tenant_isolation USING predicate is removed."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    category_id = await _seed_category(api_env, org_a)
    actor_id = await _seed_user(api_env, org_a)

    async with org_session(org_a, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        mailbox = EmailMailbox(
            org_id=org_a,
            address="helpdesk@a.test",
            host="imap.a.test",
            port=993,
            username="helpdesk@a.test",
            credential_id=await _seed_credential(session, org_a, actor_id),
            default_category_id=category_id,
            created_by=actor_id,
        )
        session.add(mailbox)
        await session.flush()
        session.add(
            InboundMessage(
                org_id=org_a,
                mailbox_id=mailbox.id,
                message_id=f"<{uuid4().hex}@a.test>",
                from_address="user@a.test",
                received_at=datetime.now(UTC),
                disposition="created",
            )
        )

    async with org_session(org_a, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        assert len((await session.execute(select(InboundMessage))).scalars().all()) == 1
    async with org_session(org_b, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        assert (await session.execute(select(InboundMessage))).scalars().all() == []


async def test_an_inbound_message_cannot_be_inserted_under_another_orgs_id(api_env) -> None:
    """F2: WITH CHECK, not just USING. Without it the row above would insert
    happily under org_b's id while being invisible to org_b's reads."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    category_id = await _seed_category(api_env, org_a)
    actor_id = await _seed_user(api_env, org_a)

    with pytest.raises(ProgrammingError):
        async with org_session(
            org_a, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            mailbox = EmailMailbox(
                org_id=org_a,
                address="helpdesk2@a.test",
                host="imap.a.test",
                port=993,
                username="helpdesk2@a.test",
                credential_id=await _seed_credential(session, org_a, actor_id),
                default_category_id=category_id,
                created_by=actor_id,
            )
            session.add(mailbox)
            await session.flush()
            session.add(
                InboundMessage(
                    org_id=org_b,  # <-- the other org
                    mailbox_id=mailbox.id,
                    message_id=f"<{uuid4().hex}@b.test>",
                    from_address="user@b.test",
                    received_at=datetime.now(UTC),
                    disposition="created",
                )
            )


@pytest.mark.parametrize("code", ["email_intake.read", "email_intake.admin"])
async def test_permission_is_seeded_to_org_admin(admin_engine, code: str) -> None:
    async with AsyncSession(admin_engine) as session:
        granted = (
            await session.execute(
                text(
                    "SELECT 1 FROM role_permissions rp "
                    "JOIN roles r ON r.id = rp.role_id "
                    "JOIN permissions p ON p.id = rp.permission_id "
                    "WHERE r.name = 'org-admin' AND p.code = :c"
                ),
                {"c": code},
            )
        ).scalar_one_or_none()
    assert granted == 1


# ---------------------------------------------------------------------------
# Seeding helpers. Kept here and imported from later test modules (Tasks 2, 5,
# 6, 7). Constructor kwargs are corrected against the REAL models:
#
# - `_seed_category`: `TicketQueue`/`TicketCategory.created_by` is NOT NULL
#   with no Python-side default (backend/app/modules/service_desk/models.py),
#   and `key` is NOT NULL with no default either. The brief's helper omitted
#   both. `_seed_category`'s own signature carries no actor id, so a
#   throwaway creator user is seeded internally to satisfy `created_by`.
# - `_seed_credential`: `Credential` lives in `app.models.core` (re-exported
#   from `app.models`), NOT `app.models.vault` (that module only defines
#   `OrgVaultKey`). `created_by` there is nullable, but the caller-supplied
#   `actor_id` is still passed through for realism.
# ---------------------------------------------------------------------------


async def _seed_user(api_env, org_id: UUID, email: str | None = None) -> UUID:
    from app.models import User

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user = User(
            org_id=org_id,
            external_subject=f"sub-{uuid4().hex[:10]}",
            email=email or f"user-{uuid4().hex[:8]}@test.local",
        )
        session.add(user)
        await session.commit()
        return user.id


async def _seed_category(api_env, org_id: UUID) -> UUID:
    """Seed a `TicketQueue` + `TicketCategory` for `org_id` and return the
    category's id.

    Side effect callers must know about: `TicketQueue`/`TicketCategory.created_by`
    is NOT NULL with no Python-side default and this helper's signature carries
    no actor id, so it ALSO INSERTS A THROWAWAY `User` ROW into `org_id` to use
    as `created_by`. That user's email is `seed-category-owner-<hex>@seed.invalid`
    -- a domain no real sender ever uses -- specifically so it can never collide
    with a `From:` address a later test (e.g. Task 5's sender-resolution
    ambiguity check) matches against `users.email` in the same org.
    """
    from app.models import User
    from app.modules.service_desk.models import TicketCategory, TicketQueue

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        creator = User(
            org_id=org_id,
            external_subject=f"sub-{uuid4().hex[:10]}",
            email=f"seed-category-owner-{uuid4().hex}@seed.invalid",
        )
        session.add(creator)
        await session.flush()
        queue = TicketQueue(
            org_id=org_id,
            key=f"q-{uuid4().hex[:6]}",
            name=f"q-{uuid4().hex[:6]}",
            created_by=creator.id,
        )
        session.add(queue)
        await session.flush()
        category = TicketCategory(
            org_id=org_id,
            key=f"c-{uuid4().hex[:6]}",
            name=f"c-{uuid4().hex[:6]}",
            type="incident",
            default_queue_id=queue.id,
            created_by=creator.id,
        )
        session.add(category)
        await session.commit()
        return category.id


async def _seed_credential(session: AsyncSession, org_id: UUID, actor_id: UUID) -> UUID:
    from app.models import Credential

    credential = Credential(
        org_id=org_id,
        name="email-mailbox-secret",
        type="email_mailbox",
        vault_ref="",
        created_by=actor_id,
    )
    session.add(credential)
    await session.flush()
    return credential.id
