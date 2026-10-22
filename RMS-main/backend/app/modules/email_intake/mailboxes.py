"""Mailbox CRUD and its vault-held credential (spec §2.1, §3, §9).

THE PASSWORD NEVER LANDS ON `email_mailbox`. It goes to the vault via
`_store_secret`, and the row holds only `credential_id`. It is never returned
by a service function, never placed in an audit `metadata` dict, and never
written to `last_error` (invariant #3).

CREATE IS ON CONFLICT, NOT CHECK-THEN-INSERT. `uq_email_mailbox_org_address`
is the authority on uniqueness; a pre-check is only a fast path for a good
error message. Two concurrent creates of the same address must produce one
row and one typed error, not an IntegrityError 500.

DELETE REFUSES RATHER THAN CASCADES. `inbound_message.mailbox_id` is a NOT
NULL FK to `email_mailbox`, so a plain delete of a mailbox with processed
messages would raise an FK violation. Rather than adding `ondelete="CASCADE"`
to the migration, `delete_mailbox` pre-checks for dependent `inbound_message`
rows and refuses with a typed `MailboxValidationError` naming the count: the
disposition log is the operator's only record of what mail arrived and why it
was refused, and deleting a mailbox must not silently erase it.

NO PATH LEAVES A `Credential` ROW WITHOUT AN OWNER. A mailbox is the only
thing that ever references the credential this module mints, so the credential
dies with it: `delete_mailbox` drops it after the mailbox row is gone, and the
loser of a create race drops the one it just minted before raising. An
ownerless envelope-encrypted IMAP password is not an invariant #3 breach (it
is still encrypted, still never plaintext) but it is unrevocable sprawl on a
table whose whole purpose is bounded credential lifetime.
"""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.vault import VaultService
from app.models import Credential
from app.modules.email_intake.errors import MailboxNotFoundError, MailboxValidationError
from app.modules.email_intake.models import EmailMailbox, InboundMessage
from app.modules.email_intake.schema import MailboxCreate, MailboxUpdate
from app.modules.service_desk.models import TicketCategory


async def _store_secret(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    secret: str,
    vault: VaultService,
    existing_credential_id: UUID | None = None,
    # This module stores three kinds of secret on `credentials`: the IMAP
    # password, the Microsoft refresh token and the Azure client secret.
    # `type` is how an audit tells them apart; the defaults keep existing
    # callers unchanged.
    name: str = "email-mailbox-secret",
    credential_type: str = "email_mailbox",
) -> UUID:
    """Store `secret` and return the `Credential` row id it lives on.

    Reuses `existing_credential_id` in place (a rotation) rather than minting
    a fresh row -- an orphaned Credential per rotation is credential sprawl.
    Never returns or logs the plaintext.
    """
    credential: Credential | None = None
    if existing_credential_id is not None:
        credential = (
            await session.execute(
                select(Credential).where(
                    Credential.org_id == org_id, Credential.id == existing_credential_id
                )
            )
        ).scalar_one_or_none()

    if credential is None:
        credential = Credential(
            org_id=org_id,
            name=name,
            type=credential_type,
            vault_ref="",
            created_by=actor_id,
        )
        session.add(credential)
        await session.flush()

    stored = await vault.store(
        session, org_id=org_id, credential_id=credential.id, plaintext=secret
    )
    credential.ciphertext = stored.ciphertext_b64
    credential.wrapped_dek = stored.wrapped_dek
    credential.key_version = stored.key_version
    credential.vault_ref = f"envelope:{credential.id}"
    session.add(credential)
    await session.flush()
    return credential.id


async def _delete_secret(
    session: AsyncSession,
    *,
    org_id: UUID,
    credential_id: UUID,
    vault: VaultService,
) -> None:
    """Drop the vault-held secret and the `Credential` row carrying it.

    THE CALLER MUST HAVE ALREADY DELETED AND FLUSHED whatever pointed at this
    credential. `email_mailbox.credential_id` is a NOT NULL FK with no ON
    DELETE clause, so deleting the credential while a mailbox still references
    it raises a foreign-key violation.
    """
    await vault.delete(session, org_id=org_id, credential_id=credential_id)
    credential = (
        await session.execute(
            select(Credential).where(
                Credential.org_id == org_id, Credential.id == credential_id
            )
        )
    ).scalar_one_or_none()
    if credential is not None:
        await session.delete(credential)
        await session.flush()


async def _resolve_category(session: AsyncSession, org_id: UUID, category_id: UUID) -> UUID:
    """`default_category_id` is NOT NULL and load-bearing (spec §2.1): without
    it no inbound message could ever become a ticket, because
    `ticket.category_id` is NOT NULL and an email carries no category.

    THE CATEGORY MUST ALSO RESOLVE TO A QUEUE. `ticket_category.default_queue_id`
    is nullable, and `service_desk.tickets.create_ticket` refuses outright for a
    category that has none -- so a mailbox pointed at a queue-less category can
    never produce a ticket. Task 5 handles that at the message level by
    recording a `quarantined` row rather than stalling the mailbox (see
    `intake.process_message`'s `TicketValidationError` branch), which keeps the
    poller correct but means EVERY message to that mailbox is lost work,
    discovered only by an operator reading the quarantine list. This is the
    cheapest place to catch it: refuse the CONFIGURATION, at create and at
    update, instead of quarantining mail one message at a time forever.
    """
    row = (
        await session.execute(
            select(TicketCategory.id, TicketCategory.default_queue_id).where(
                TicketCategory.org_id == org_id, TicketCategory.id == category_id
            )
        )
    ).one_or_none()
    if row is None:
        raise MailboxValidationError(tr("default_category_id does not resolve in this org"))
    if row.default_queue_id is None:
        raise MailboxValidationError(
            tr(
                "default_category_id names a category with no default queue; every message "
                "to this mailbox would be quarantined instead of becoming a ticket"
            )
        )
    return UUID(str(row.id))


async def get_mailbox(session: AsyncSession, org_id: UUID, mailbox_id: UUID) -> EmailMailbox:
    mailbox = (
        await session.execute(
            select(EmailMailbox).where(
                EmailMailbox.org_id == org_id, EmailMailbox.id == mailbox_id
            )
        )
    ).scalar_one_or_none()
    if mailbox is None:
        raise MailboxNotFoundError(tr("mailbox {mailbox_id} not found", mailbox_id=mailbox_id))
    return mailbox


async def list_mailboxes(session: AsyncSession, org_id: UUID) -> list[EmailMailbox]:
    return list(
        (
            await session.execute(
                select(EmailMailbox)
                .where(EmailMailbox.org_id == org_id)
                .order_by(EmailMailbox.address)
            )
        )
        .scalars()
        .all()
    )


async def create_mailbox(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    data: MailboxCreate,
    *,
    vault: VaultService,
) -> EmailMailbox:
    await _resolve_category(session, org_id, data.default_category_id)
    credential_id: UUID | None = None
    active = data.active
    if data.auth_kind == "microsoft_oauth":
        if data.password is not None:
            raise MailboxValidationError(
                tr("a mailbox that signs in with Microsoft has no password; leave it out")
            )
        # Nothing to sign in with until someone connects it
        # (`ck_email_mailbox_credential`).
        active = False
    else:
        if data.password is None:
            raise MailboxValidationError(tr("a password is required for this mailbox"))
        credential_id = await _store_secret(
            session, org_id=org_id, actor_id=actor_id, secret=data.password, vault=vault
        )

    # ON CONFLICT DO NOTHING + RETURNING id. `inserted_id is None` means a
    # concurrent create won the race; it raises the SAME typed error a caller
    # who lost a pre-check would see, and never returns the winner's row --
    # returning it would tell this caller their configuration was applied
    # when someone else's was.
    result = await session.execute(
        pg_insert(EmailMailbox)
        .values(
            org_id=org_id,
            address=data.address,
            host=data.host,
            port=data.port,
            use_ssl=data.use_ssl,
            folder=data.folder,
            username=data.username,
            credential_id=credential_id,
            auth_kind=data.auth_kind,
            default_category_id=data.default_category_id,
            reopen_window_days=data.reopen_window_days,
            active=active,
            created_by=actor_id,
        )
        .on_conflict_do_nothing(constraint="uq_email_mailbox_org_address")
        .returning(EmailMailbox.id)
    )
    inserted_id = result.scalar_one_or_none()
    if inserted_id is None:
        # `_store_secret` minted (and flushed) a `Credential` before the insert
        # that just lost. Drop it HERE rather than relying on the caller's
        # rollback: a Task 9 route that catches `MailboxValidationError`
        # *inside* its `org_session` block and returns 409 would otherwise
        # commit an ownerless credential. Nothing references it yet -- the
        # mailbox insert did nothing -- so the delete is unconstrained.
        if credential_id is not None:
            await _delete_secret(
                session, org_id=org_id, credential_id=credential_id, vault=vault
            )
        raise MailboxValidationError(
            tr(
                "a mailbox for {address} already exists in this org",
                address=repr(data.address),
            )
        )

    mailbox = await get_mailbox(session, org_id, inserted_id)

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="email_intake.mailbox_created",
        target_type="email_mailbox",
        target_id=str(mailbox.id),
        # Address/host/port only. NEVER the password, and never the
        # credential id -- an audit reader has no use for a vault handle.
        metadata={
            "address": data.address,
            "host": data.host,
            "port": data.port,
            "auth_kind": data.auth_kind,
        },
    )
    return mailbox


async def update_mailbox(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    mailbox_id: UUID,
    data: MailboxUpdate,
    *,
    vault: VaultService,
) -> EmailMailbox:
    """Renaming to an address another mailbox in this org already holds is a
    typed `MailboxValidationError`, never a raw `IntegrityError` (a 500).

    The write here is an UPDATE of an already-loaded row, not an INSERT, so
    there is no `ON CONFLICT DO NOTHING` to reach for as `create_mailbox`
    does. Instead the flush runs inside a SAVEPOINT (`session.begin_nested`)
    and an `IntegrityError` from it is converted to the SAME typed error a
    colliding create raises. The savepoint is opened BEFORE `mailbox` is
    mutated at all -- `begin_nested()` autoflushes pending dirty state the
    instant it opens, so mutating first would issue the UPDATE *outside* the
    savepoint and defeat it silently (the same ordering trap documented in
    `notification/channels.py::update_channel`).
    """
    mailbox = await get_mailbox(session, org_id, mailbox_id)

    if data.default_category_id is not None:
        await _resolve_category(session, org_id, data.default_category_id)

    renaming = data.address is not None and data.address != mailbox.address
    target_kind = data.auth_kind or mailbox.auth_kind
    switching = target_kind != mailbox.auth_kind
    if target_kind == "microsoft_oauth" and data.password is not None:
        raise MailboxValidationError(
            tr("a mailbox that signs in with Microsoft has no password; leave it out")
        )
    if switching and target_kind == "password" and data.password is None:
        raise MailboxValidationError(tr("switching this mailbox to a password needs the password"))

    changed: list[str] = []
    retired: UUID | None = None
    if switching:
        # The old credential is the wrong KIND of secret for the new sign-in:
        # it is destroyed, never reused (a refresh token is not a password).
        retired = mailbox.credential_id
        mailbox.credential_id = None
        mailbox.auth_kind = target_kind
        mailbox.credential_state = "ok"
        mailbox.active = False
        changed.append("auth_kind")
        session.add(mailbox)
        await session.flush()
        if retired is not None:
            await _delete_secret(session, org_id=org_id, credential_id=retired, vault=vault)

    try:
        async with session.begin_nested():
            if data.password is not None:
                # ASSIGN the returned id. `_store_secret` mints a fresh
                # `Credential` if the existing one is not visible to this
                # session; ignoring its return value would leave the mailbox
                # pointing at the old row and the rotated password
                # unreachable -- a silent failure rather than a loud one.
                mailbox.credential_id = await _store_secret(
                    session,
                    org_id=org_id,
                    actor_id=actor_id,
                    secret=data.password,
                    vault=vault,
                    existing_credential_id=mailbox.credential_id,
                )

            for field in (
                "address",
                "host",
                "port",
                "use_ssl",
                "folder",
                "username",
                "default_category_id",
                "reopen_window_days",
                "active",
            ):
                value = getattr(data, field)
                if value is not None and value != getattr(mailbox, field):
                    setattr(mailbox, field, value)
                    changed.append(field)
            if data.password is not None:
                changed.append("password")
            if mailbox.active and mailbox.credential_id is None:
                raise MailboxValidationError(
                    tr("connect this mailbox to Microsoft before switching it on")
                )

            session.add(mailbox)
            await session.flush()
    except IntegrityError:
        if renaming:
            raise MailboxValidationError(
                tr(
                    "a mailbox for {address} already exists in this org",
                    address=repr(data.address),
                )
            ) from None
        raise

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="email_intake.mailbox_updated",
        target_type="email_mailbox",
        target_id=str(mailbox.id),
        # The NAMES of the changed fields, never their values -- "password"
        # appearing here records that a rotation happened, which is the whole
        # point, without recording what it rotated to.
        metadata={"changed": sorted(changed)},
    )
    return mailbox


async def delete_mailbox(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    mailbox_id: UUID,
    *,
    vault: VaultService,
) -> None:
    mailbox = await get_mailbox(session, org_id, mailbox_id)

    # `inbound_message.mailbox_id` is a NOT NULL FK with no ON DELETE clause;
    # refuse up front with a good message rather than let a plain delete
    # surface as a raw IntegrityError. The disposition log is the operator's
    # only record of what mail arrived and why it was refused, so deleting a
    # mailbox must never silently erase it.
    message_count = (
        await session.execute(
            select(func.count())
            .select_from(InboundMessage)
            .where(InboundMessage.org_id == org_id, InboundMessage.mailbox_id == mailbox_id)
        )
    ).scalar_one()
    if message_count > 0:
        raise MailboxValidationError(
            tr(
                "mailbox {mailbox_id} has {message_count} processed message(s) and cannot be "
                "deleted; deactivate it instead",
                mailbox_id=mailbox_id,
                message_count=message_count,
            )
        )

    target_id = str(mailbox.id)
    address = mailbox.address
    credential_id = mailbox.credential_id
    await session.delete(mailbox)
    await session.flush()
    # ORDER IS LOAD-BEARING: `email_mailbox.credential_id` is a NOT NULL FK
    # with no ON DELETE clause, so the mailbox row must be gone (and flushed)
    # before its credential can be. In the same transaction, so a delete that
    # fails leaves both.
    if credential_id is not None:
        await _delete_secret(session, org_id=org_id, credential_id=credential_id, vault=vault)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="email_intake.mailbox_deleted",
        target_type="email_mailbox",
        target_id=target_id,
        metadata={"address": address},
    )


async def disconnect_mailbox(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    mailbox_id: UUID,
    *,
    vault: VaultService,
) -> EmailMailbox:
    """Destroy a Microsoft mailbox's stored sign-in and switch it off.

    There is no fallback to a password: a mailbox that seemed to keep working
    after a disconnect would be worse than one that visibly stopped.
    """
    mailbox = await get_mailbox(session, org_id, mailbox_id)
    if mailbox.auth_kind != "microsoft_oauth":
        raise MailboxValidationError(
            tr("only a mailbox that signs in with Microsoft can be disconnected")
        )
    retired = mailbox.credential_id
    mailbox.credential_id = None
    mailbox.active = False
    mailbox.credential_state = "ok"
    mailbox.last_error = None
    session.add(mailbox)
    await session.flush()
    if retired is not None:
        await _delete_secret(session, org_id=org_id, credential_id=retired, vault=vault)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="email_intake.oauth_disconnected",
        target_type="email_mailbox",
        target_id=str(mailbox.id),
        metadata={"address": mailbox.address},
    )
    return mailbox
