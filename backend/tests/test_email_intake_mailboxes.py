"""Mailbox CRUD and its vault-held credential (spec §2.1, §3, §9)."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.core.vault import get_vault
from app.models import AuditEvent, Credential
from app.modules.email_intake.errors import MailboxNotFoundError, MailboxValidationError
from app.modules.email_intake.mailboxes import (
    create_mailbox,
    delete_mailbox,
    get_mailbox,
    list_mailboxes,
    update_mailbox,
)
from app.modules.email_intake.models import EmailMailbox, InboundMessage
from app.modules.email_intake.schema import MailboxCreate, MailboxUpdate
from tests.test_email_intake_migration import _seed_category, _seed_user

SECRET = "correct-horse-battery-staple"


def _payload(category_id, **overrides) -> MailboxCreate:
    data = {
        "address": "helpdesk@acme.test",
        "host": "imap.acme.test",
        "port": 993,
        "username": "helpdesk@acme.test",
        "password": SECRET,
        "default_category_id": category_id,
    }
    data.update(overrides)
    return MailboxCreate(**data)


async def _audit(api_env, org_id) -> list[AuditEvent]:
    async with AsyncSession(api_env.admin_engine) as session:
        return list(
            (await session.execute(select(AuditEvent).where(AuditEvent.org_id == org_id)))
            .scalars()
            .all()
        )


async def _credentials(api_env, org_id) -> list[Credential]:
    async with AsyncSession(api_env.admin_engine) as session:
        return list(
            (await session.execute(select(Credential).where(Credential.org_id == org_id)))
            .scalars()
            .all()
        )


async def test_create_stores_the_password_in_the_vault_and_never_on_the_row(api_env) -> None:
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    category_id = await _seed_category(api_env, org_id)
    vault = get_vault(api_env.settings)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        mailbox = await create_mailbox(
            session, org_id, actor_id, _payload(category_id), vault=vault
        )
        mailbox_id = mailbox.id

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        row = await get_mailbox(session, org_id, mailbox_id)
        # Invariant #3: the plaintext appears in NO column of this row.
        assert SECRET not in " ".join(
            str(getattr(row, c.name)) for c in EmailMailbox.__table__.columns
        )
        # ...and the vault can hand it back at point of use.
        secret = await vault.checkout(session, org_id=org_id, credential_id=row.credential_id)
        assert secret == SECRET


async def test_rotating_the_password_reuses_the_same_credential_row(api_env) -> None:
    """A fresh Credential row per rotation is credential sprawl; the
    notification module made the same call (channels.py's docstring)."""
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    category_id = await _seed_category(api_env, org_id)
    vault = get_vault(api_env.settings)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        mailbox = await create_mailbox(
            session, org_id, actor_id, _payload(category_id), vault=vault
        )
        mailbox_id, credential_id = mailbox.id, mailbox.credential_id

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await update_mailbox(
            session, org_id, actor_id, mailbox_id, MailboxUpdate(password="new-secret"), vault=vault
        )

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        row = await get_mailbox(session, org_id, mailbox_id)
        assert row.credential_id == credential_id
        secret = await vault.checkout(session, org_id=org_id, credential_id=credential_id)
        assert secret == "new-secret"


async def test_no_audit_metadata_contains_the_password(api_env) -> None:
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    category_id = await _seed_category(api_env, org_id)
    vault = get_vault(api_env.settings)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        mailbox = await create_mailbox(
            session, org_id, actor_id, _payload(category_id), vault=vault
        )
        mailbox_id = mailbox.id
        await update_mailbox(
            session, org_id, actor_id, mailbox_id, MailboxUpdate(password="another"), vault=vault
        )

    events = await _audit(api_env, org_id)
    # Invariant #4, asserted on the ACTIONS, not on `assert events`: with only
    # a truthiness check, deleting `update_mailbox`'s audit call entirely
    # still leaves the create event behind and the test still passes.
    assert sorted((e.action, e.target_id) for e in events) == [
        ("email_intake.mailbox_created", str(mailbox_id)),
        ("email_intake.mailbox_updated", str(mailbox_id)),
    ]
    # NOTE: `AuditEvent`'s Python attribute for its "metadata" column is
    # `meta` -- `metadata` is reserved by SQLAlchemy's declarative base
    # (`app/models/core.py`: `meta: Mapped[...] = mapped_column("metadata", ...)`).
    blob = " ".join(str(e.meta) for e in events)
    assert SECRET not in blob and "another" not in blob


async def test_a_duplicate_address_is_a_typed_error_not_an_integrity_error(api_env) -> None:
    """Two concurrent creates race on uq_email_mailbox_org_address. The loser
    must see the SAME typed error as a caller who lost the pre-check --
    check-then-insert with no ON CONFLICT is the bug class this repo has
    already run two sweeps to eliminate."""
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    category_id = await _seed_category(api_env, org_id)
    vault = get_vault(api_env.settings)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await create_mailbox(session, org_id, actor_id, _payload(category_id), vault=vault)

    with pytest.raises(MailboxValidationError):
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            await create_mailbox(session, org_id, actor_id, _payload(category_id), vault=vault)


async def test_concurrent_creates_of_the_same_address_yield_one_row(api_env) -> None:
    """The real race, two sessions, not a simulated one.

    THE ORG'S VAULT KEY IS PRE-WARMED ON PURPOSE, AND THE TWO ATTEMPTS MEET
    AT A BARRIER. Without both, this test cannot fail for the reason its name
    claims. `VaultService._org_dek` creates the org's `org_vault_keys` row
    with its OWN `ON CONFLICT DO NOTHING` on the first secret ever stored for
    that org; two racing creates therefore both block *there*, the loser
    released only once the winner has committed -- so the loser's mailbox
    insert runs after the winner's row is already visible and the mailbox
    insert never actually races. A replacement check-then-insert
    implementation passes such a test unchanged. Storing an unrelated secret
    first makes `_org_dek` a plain read for both attempts; the barrier (with
    the connection and transaction already established before it) removes the
    remaining connection-latency serialization. Only then do the two attempts
    genuinely collide on `uq_email_mailbox_org_address`.
    """
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    category_id = await _seed_category(api_env, org_id)
    vault = get_vault(api_env.settings)

    # Pre-warm the org DEK (see docstring). `store` writes only
    # `org_vault_keys`; the credential id is AAD, so no `Credential` row is
    # created and this leaves nothing else behind for the racers to see.
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await vault.store(session, org_id=org_id, credential_id=uuid4(), plaintext="warm-the-dek")

    barrier = asyncio.Barrier(2)

    async def attempt() -> bool:
        try:
            async with org_session(
                org_id, engine=api_env.app_engine, clients=ClientScope.all()
            ) as session:
                # Take the connection and open the transaction BEFORE the
                # rendezvous so neither attempt pays connection latency after
                # the barrier releases. `wait_for` so a failure on one side
                # fails the test rather than hanging the other forever.
                await session.execute(select(EmailMailbox.id))
                await asyncio.wait_for(barrier.wait(), timeout=30)
                await create_mailbox(session, org_id, actor_id, _payload(category_id), vault=vault)
            return True
        except MailboxValidationError:
            return False

    results = await asyncio.gather(attempt(), attempt(), return_exceptions=True)
    assert all(not isinstance(r, BaseException) for r in results), results
    # Exactly one winner and one loser -- not two winners, not two losers.
    assert sorted(results) == [False, True], results

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        rows = (await session.execute(select(EmailMailbox))).scalars().all()
    assert len(rows) == 1


async def test_losing_a_create_race_leaves_no_orphan_credential(api_env) -> None:
    """The loser drops the `Credential` it minted BEFORE raising.

    `_store_secret` flushes a credential row *before* the ON CONFLICT insert
    that may lose. A Task 9 route that catches `MailboxValidationError`
    INSIDE its `org_session` block to return 409 -- the natural way to write
    it -- would then commit an envelope-encrypted IMAP password no mailbox
    references and nothing can revoke. This test catches the error inside the
    block precisely so the transaction commits rather than rolling back.
    """
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    category_id = await _seed_category(api_env, org_id)
    vault = get_vault(api_env.settings)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await create_mailbox(session, org_id, actor_id, _payload(category_id), vault=vault)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        with pytest.raises(MailboxValidationError):
            await create_mailbox(session, org_id, actor_id, _payload(category_id), vault=vault)

    credentials = await _credentials(api_env, org_id)
    assert len(credentials) == 1, "the losing create committed an ownerless Credential"


async def test_updating_to_an_address_already_used_in_the_org_is_a_typed_error(api_env) -> None:
    """The same escaped-`IntegrityError` class the create path guards against,
    one function below it. Task 9 must be able to return 409, not 500."""
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    category_id = await _seed_category(api_env, org_id)
    vault = get_vault(api_env.settings)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await create_mailbox(
            session, org_id, actor_id, _payload(category_id, address="a@acme.test"), vault=vault
        )
        second = await create_mailbox(
            session, org_id, actor_id, _payload(category_id, address="b@acme.test"), vault=vault
        )
        second_id = second.id

    with pytest.raises(MailboxValidationError):
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            await update_mailbox(
                session,
                org_id,
                actor_id,
                second_id,
                MailboxUpdate(address="a@acme.test"),
                vault=vault,
            )

    # The rejected rename applied nothing.
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        assert (await get_mailbox(session, org_id, second_id)).address == "b@acme.test"


async def test_a_category_from_another_org_is_rejected(api_env) -> None:
    """`default_category_id` is NOT NULL and load-bearing (spec §2.1);
    `_resolve_category` is the only thing enforcing it is in-org. Deleting it
    would leave the FK to fire as a raw `IntegrityError` instead."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    actor_id = await _seed_user(api_env, org_a)
    foreign_category = await _seed_category(api_env, org_b)
    vault = get_vault(api_env.settings)

    with pytest.raises(MailboxValidationError, match="default_category_id"):
        async with org_session(
            org_a, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            await create_mailbox(
                session, org_a, actor_id, _payload(foreign_category), vault=vault
            )


async def test_list_mailboxes_returns_only_this_orgs_rows_ordered(api_env) -> None:
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    actor_a = await _seed_user(api_env, org_a)
    actor_b = await _seed_user(api_env, org_b)
    category_a = await _seed_category(api_env, org_a)
    category_b = await _seed_category(api_env, org_b)
    vault = get_vault(api_env.settings)

    async with org_session(org_a, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        for address in ("zulu@a.test", "alpha@a.test", "mike@a.test"):
            await create_mailbox(
                session, org_a, actor_a, _payload(category_a, address=address), vault=vault
            )
    async with org_session(org_b, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await create_mailbox(
            session, org_b, actor_b, _payload(category_b, address="alpha@b.test"), vault=vault
        )

    async with org_session(org_a, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        rows = await list_mailboxes(session, org_a)
    assert [r.address for r in rows] == ["alpha@a.test", "mike@a.test", "zulu@a.test"]

    # ...and on a session where RLS is NOT the thing filtering (the admin
    # engine bypasses it), so the application-layer `org_id` predicate is the
    # only defence in play. Fails if that predicate is removed.
    async with AsyncSession(api_env.admin_engine) as session:
        rows = await list_mailboxes(session, org_a)
    assert [r.address for r in rows] == ["alpha@a.test", "mike@a.test", "zulu@a.test"]


async def test_get_mailbox_from_another_org_is_not_found(api_env) -> None:
    """The RLS half: the org_session connection cannot see the row at all."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    actor_id = await _seed_user(api_env, org_a)
    category_id = await _seed_category(api_env, org_a)
    vault = get_vault(api_env.settings)

    async with org_session(org_a, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        mailbox = await create_mailbox(session, org_a, actor_id, _payload(category_id), vault=vault)
        mailbox_id = mailbox.id

    with pytest.raises(MailboxNotFoundError):
        async with org_session(
            org_b, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            await get_mailbox(session, org_b, mailbox_id)


async def test_get_mailbox_rejects_another_orgs_row_without_relying_on_rls(api_env) -> None:
    """The APPLICATION half of invariant #5. The test above passes with
    `get_mailbox`'s `org_id` predicate deleted -- RLS on the org_session
    connection rejects first, so it cannot distinguish the two layers. Here
    the lookup runs on the admin (RLS-bypassing) engine, where the service's
    own predicate is the only thing standing between org_b and org_a's
    mailbox. Delete that predicate and this test fails."""
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    actor_id = await _seed_user(api_env, org_a)
    category_id = await _seed_category(api_env, org_a)
    vault = get_vault(api_env.settings)

    async with org_session(org_a, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        mailbox = await create_mailbox(session, org_a, actor_id, _payload(category_id), vault=vault)
        mailbox_id = mailbox.id

    async with AsyncSession(api_env.admin_engine) as session:
        # Sanity: this session CAN see the row, so a pass below is the
        # application predicate at work and not an empty database.
        assert await session.get(EmailMailbox, mailbox_id) is not None
        with pytest.raises(MailboxNotFoundError):
            await get_mailbox(session, org_b, mailbox_id)


async def test_update_and_delete_from_another_org_are_not_found(api_env) -> None:
    org_a = await api_env.create_org("a")
    org_b = await api_env.create_org("b")
    actor_a = await _seed_user(api_env, org_a)
    actor_b = await _seed_user(api_env, org_b)
    category_id = await _seed_category(api_env, org_a)
    vault = get_vault(api_env.settings)

    async with org_session(org_a, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        mailbox = await create_mailbox(session, org_a, actor_a, _payload(category_id), vault=vault)
        mailbox_id = mailbox.id

    with pytest.raises(MailboxNotFoundError):
        async with org_session(
            org_b, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            await update_mailbox(
                session, org_b, actor_b, mailbox_id, MailboxUpdate(active=False), vault=vault
            )

    with pytest.raises(MailboxNotFoundError):
        async with org_session(
            org_b, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            await delete_mailbox(session, org_b, actor_b, mailbox_id, vault=vault)

    # Neither foreign call touched org_a's row.
    async with org_session(org_a, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        assert (await get_mailbox(session, org_a, mailbox_id)).active is True


async def test_delete_removes_the_mailbox_and_audits(api_env) -> None:
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    category_id = await _seed_category(api_env, org_id)
    vault = get_vault(api_env.settings)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        mailbox = await create_mailbox(
            session, org_id, actor_id, _payload(category_id), vault=vault
        )
        mailbox_id = mailbox.id

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await delete_mailbox(session, org_id, actor_id, mailbox_id, vault=vault)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        assert (await session.execute(select(EmailMailbox))).scalars().all() == []

    # Invariant #4 for the delete path, asserted on the action and the target
    # -- not merely implied by the test's name.
    events = await _audit(api_env, org_id)
    assert sorted((e.action, e.target_id) for e in events) == [
        ("email_intake.mailbox_created", str(mailbox_id)),
        ("email_intake.mailbox_deleted", str(mailbox_id)),
    ]


async def test_delete_removes_the_mailboxs_credential_too(api_env) -> None:
    """An ownerless `Credential` is an envelope-encrypted IMAP password with
    no path to revocation, on the table whose whole purpose is bounded
    credential lifetime. It dies in the same transaction as its mailbox."""
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    category_id = await _seed_category(api_env, org_id)
    vault = get_vault(api_env.settings)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        mailbox = await create_mailbox(
            session, org_id, actor_id, _payload(category_id), vault=vault
        )
        mailbox_id = mailbox.id
    assert len(await _credentials(api_env, org_id)) == 1

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await delete_mailbox(session, org_id, actor_id, mailbox_id, vault=vault)

    assert await _credentials(api_env, org_id) == []


async def test_delete_refuses_when_the_mailbox_has_processed_messages(api_env) -> None:
    """Ambiguity resolution: refuse rather than cascade (see mailboxes.py's
    module docstring). `inbound_message` is the operator's only record of
    what arrived; a delete must not silently erase it."""
    org_id = await api_env.create_org()
    actor_id = await _seed_user(api_env, org_id)
    category_id = await _seed_category(api_env, org_id)
    vault = get_vault(api_env.settings)

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        mailbox = await create_mailbox(
            session, org_id, actor_id, _payload(category_id), vault=vault
        )
        mailbox_id = mailbox.id

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        session.add(
            InboundMessage(
                org_id=org_id,
                mailbox_id=mailbox_id,
                message_id=f"<{uuid4().hex}@acme.test>",
                from_address="someone@acme.test",
                received_at=datetime.now(UTC),
                disposition="quarantined",
            )
        )

    with pytest.raises(MailboxValidationError):
        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            await delete_mailbox(session, org_id, actor_id, mailbox_id, vault=vault)

    # The mailbox and its message both still exist -- the delete refused
    # rather than partially applying.
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        assert (await session.execute(select(EmailMailbox))).scalars().all() != []
        assert (await session.execute(select(InboundMessage))).scalars().all() != []
    # ...and so does the credential it would have dropped.
    assert len(await _credentials(api_env, org_id)) == 1
