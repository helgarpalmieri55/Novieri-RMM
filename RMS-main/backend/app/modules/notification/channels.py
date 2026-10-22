"""Channel CRUD and vault-held credentials
(docs/modules/notification/spec.md §2.2, §5, §8).

Mirrors `app.modules.service_desk.refdata`'s shape closely: an explicit
`org_id` predicate on every query (invariant #5), audit-then-flush on every
mutation (invariant #4), and a SINGLE `NotificationNotFoundError` raised
identically whether a channel id does not exist OR belongs to another org --
the two cases must be indistinguishable to the caller. `refdata.py`'s module
docstring records the exact bug class this guards against: the ticket core
shipped a distinguishable "exists but not yours" for `default_queue_id` and
had to fix it in review because it let one org use another org's real ids as
a cross-tenant existence oracle. `get_channel`/`list_channels` here are the
load-bearing functions for that guarantee.

SECRETS (invariant #3). `VaultService.store(...)` (`app/core/vault.py`)
encrypts a plaintext secret under the caller's org data-encryption key and
returns only ciphertext/wrapped-key material (`StoredSecret`) -- it does
NOT mint a credential id itself. The only existing caller,
`app/api/v1/credentials.py`'s `create_credential`/`update_credential`,
establishes the actual shape: create a `Credential` METADATA row first
(flush it to get a real id), call `vault.store(..., credential_id=<that
id>, ...)`, then copy `ciphertext_b64`/`wrapped_dek`/`key_version` onto that
row. `_store_secret` below follows exactly that shape and returns the
credential's id for `NotificationChannel.credential_id` to reference.
Rotation (`update_channel` with a channel that already has a credential)
reuses that SAME credential id rather than minting a new row -- an orphaned
`Credential` row per rotation would be a small but real credential-sprawl
leak, and nothing else ever points a second channel at one channel's secret.

The plaintext itself is never assigned to `NotificationChannel.config`
(the Pydantic layer already refuses to accept one there, schema.py's
`_reject_secret_keys`), never placed in an audit `metadata` dict, and never
logged -- only the *fact* that a credential was stored, via `credential_id`,
appears anywhere this module writes.
"""

import json
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_extensions import uuid7

from app.core.audit import audit_service
from app.core.i18n import N_, tr, tr_marked
from app.core.vault import VaultService
from app.models import Credential, NotificationChannel
from app.modules.email_intake import microsoft
from app.modules.notification.errors import (
    NotificationNotFoundError,
    NotificationValidationError,
)
from app.modules.notification.schema import (
    ChannelCreate,
    ChannelUpdate,
    EmailSignIn,
    MicrosoftSignIn,
    email_auth,
    validate_channel_config,
)

# `kind` is deliberately absent: it is immutable after creation (changing a
# webhook channel into an email channel mid-life makes no sense of its own
# `config` shape, spec §2.2's kind-specific schema).
_CHANNEL_EDITABLE_FIELDS = ("name", "config", "active")
# All three are NOT NULL columns on `notification_channels` (models.py) -- an
# explicit `null` for any of them would otherwise reach Postgres as a NOT
# NULL violation and surface as a 500; reject it up front as a typed 400
# instead (the `service_desk.refdata` pattern).
_CHANNEL_NON_NULLABLE_FIELDS = frozenset({"name", "config", "active"})


def _reject_null_for_non_nullable(fields: dict[str, Any]) -> None:
    for field in _CHANNEL_NON_NULLABLE_FIELDS:
        if field in fields and fields[field] is None:
            raise NotificationValidationError(tr("{field} cannot be null", field=field))


async def _reject_duplicate_name(
    session: AsyncSession, org_id: UUID, name: str, *, exclude_channel_id: UUID | None = None
) -> None:
    """Pre-check for `uq_channel_org_name` (spec §2.2) -- rejected here as a
    typed 400 rather than surfacing the constraint violation as a raw
    `IntegrityError` (a 500).

    This is a FAST PATH, not the sole guard -- see `create_channel` and
    `update_channel`, both of which follow this pre-check with a
    concurrency-safe write of their own (`service_desk.refdata.create_queue`'s
    pattern) rather than relying on this SELECT alone."""
    stmt = select(NotificationChannel.id).where(
        NotificationChannel.org_id == org_id, NotificationChannel.name == name
    )
    if exclude_channel_id is not None:
        stmt = stmt.where(NotificationChannel.id != exclude_channel_id)
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        raise NotificationValidationError(tr("channel name already exists: {name}", name=name))


async def _store_secret(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    secret: str,
    vault: VaultService,
    existing_credential_id: UUID | None = None,
) -> UUID:
    """Store `secret` in the vault and return the id of the `Credential` row
    it now lives on. Reuses `existing_credential_id` in place (a rotation)
    when the caller supplies one that resolves org-scoped; otherwise creates
    a fresh `Credential` metadata row. Never returns or logs the plaintext --
    only `credential.ciphertext`/`wrapped_dek`/`key_version` (all opaque,
    vault-managed) are written."""
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
            name="notification-channel-secret",
            type="notification_channel",
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


def _sign_in_secret(kind: str, config: dict[str, Any], sign_in: EmailSignIn | None) -> str | None:
    """The vault value for an email channel's sign-in, checked against the
    channel's `auth`. One JSON value per channel; `delivery.py` reads it."""
    if sign_in is None:
        return None
    if kind != "email":
        raise NotificationValidationError(tr("sign-in details apply to email channels only"))
    auth = email_auth(config)
    if isinstance(sign_in, MicrosoftSignIn):
        if auth != "microsoft":
            raise NotificationValidationError(
                tr("Microsoft 365 details were given for a channel that signs in with a password")
            )
        tenant_id = sign_in.tenant_id.strip()
        try:
            microsoft.token_url(tenant_id)
        except ValueError as exc:
            raise NotificationValidationError(str(exc)) from None
        return json.dumps(
            {
                "tenant_id": tenant_id,
                "client_id": sign_in.client_id.strip(),
                "client_secret": sign_in.client_secret,
            }
        )
    if auth != "password":
        raise NotificationValidationError(
            tr("a username and password were given for a channel that signs in with Microsoft 365")
        )
    return json.dumps({"username": sign_in.username.strip(), "password": sign_in.password})


_EMAIL_SECRET_KEYS = {
    "password": {"username", "password"},
    "microsoft": {"tenant_id", "client_id", "client_secret"},
}


def _check_email_secret(config: dict[str, Any], secret: str) -> None:
    """An email channel's vault value is the JSON `delivery.py` reads; a bare
    password stored here used to fail at every send instead of at save."""
    try:
        value = json.loads(secret)
    except ValueError:
        value = None
    if not isinstance(value, dict) or set(value) != _EMAIL_SECRET_KEYS[email_auth(config)]:
        raise NotificationValidationError(
            tr(
                "an email channel takes a username and password, or the Microsoft 365 "
                "sign-in details, not a single secret"
            )
        )


MICROSOFT_NEEDS_SIGN_IN = N_(
    "Microsoft 365 sign-in needs the directory (tenant) ID, application (client) ID "
    "and client secret"
)


async def _drop_credential(
    session: AsyncSession, *, org_id: UUID, credential_id: UUID, vault: VaultService
) -> None:
    """Destroy a credential nothing points at any more (the caller flushed that)."""
    await vault.delete(session, org_id=org_id, credential_id=credential_id)
    credential = (
        await session.execute(
            select(Credential).where(Credential.org_id == org_id, Credential.id == credential_id)
        )
    ).scalar_one_or_none()
    if credential is not None:
        await session.delete(credential)
        await session.flush()


async def create_channel(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    data: ChannelCreate,
    secret: str | None,
    vault: VaultService,
    sign_in: EmailSignIn | None = None,
) -> NotificationChannel:
    """Create a channel. `secret`, when supplied, is stored in the vault
    BEFORE the channel row exists (`_store_secret` mints its own `Credential`
    row) and only its `credential_id` is attached below -- the plaintext
    never touches `NotificationChannel` at all.

    A duplicate `name` within the org is rejected up front as a typed
    `NotificationValidationError`; the same `name` in a different org is
    unaffected (`uq_channel_org_name` is per-org).

    The pre-check above is a fast path, not the sole guard: two concurrent
    `create_channel` calls for the same `(org_id, name)` can both miss it
    before either has written. A plain `session.add` here would let the
    loser surface a raw `IntegrityError` as a 500, so the actual insert
    instead goes through `ON CONFLICT DO NOTHING` against
    `uq_channel_org_name` (`service_desk.refdata.create_queue`'s pattern)
    -- but this is a single-caller CREATE, not a get-or-create: a race loser
    must get the identical typed `NotificationValidationError` the pre-check
    raises, never someone else's channel."""
    await _reject_duplicate_name(session, org_id, data.name)

    if sign_in is not None:
        secret = _sign_in_secret(data.kind, data.config, sign_in)
    if data.kind == "email":
        if secret is not None:
            _check_email_secret(data.config, secret)
        elif email_auth(data.config) == "microsoft":
            raise NotificationValidationError(tr_marked(MICROSOFT_NEEDS_SIGN_IN))

    credential_id: UUID | None = None
    if secret is not None:
        credential_id = await _store_secret(
            session, org_id=org_id, actor_id=actor_id, secret=secret, vault=vault
        )

    channel_id = uuid7()
    inserted_id = (
        await session.execute(
            pg_insert(NotificationChannel)
            .values(
                id=channel_id,
                org_id=org_id,
                kind=data.kind,
                name=data.name,
                config=data.config,
                credential_id=credential_id,
                active=data.active,
                created_by=actor_id,
            )
            .on_conflict_do_nothing(index_elements=["org_id", "name"])
            .returning(NotificationChannel.id)
        )
    ).scalar_one_or_none()
    if inserted_id is None:
        raise NotificationValidationError(
            tr(
                "channel name already exists: {name}",
                name=data.name,
            )
        )

    channel = (
        await session.execute(
            select(NotificationChannel).where(
                NotificationChannel.org_id == org_id, NotificationChannel.id == inserted_id
            )
        )
    ).scalar_one()
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="notification.channel_created",
        target_type="notification_channel",
        target_id=str(channel.id),
        metadata={
            "kind": data.kind,
            "name": data.name,
            "has_credential": credential_id is not None,
        },
    )
    await session.flush()
    return channel


async def list_channels(session: AsyncSession, *, org_id: UUID) -> list[NotificationChannel]:
    """Plain org-scoped list, explicitly ordered (spec §9's convention: any
    list-equality assertion in a test needs an ORDER BY). No audit -- this
    project's convention is no-audit-on-view."""
    stmt = (
        select(NotificationChannel)
        .where(NotificationChannel.org_id == org_id)
        .order_by(NotificationChannel.created_at, NotificationChannel.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_channel(
    session: AsyncSession, *, org_id: UUID, channel_id: UUID
) -> NotificationChannel:
    """Org-scoped load. A channel id from another org is indistinguishable
    from a nonexistent one -- both raise `NotificationNotFoundError` (spec
    §8, §12.10, invariant #5). No audit -- this is a read."""
    channel = (
        await session.execute(
            select(NotificationChannel).where(
                NotificationChannel.org_id == org_id, NotificationChannel.id == channel_id
            )
        )
    ).scalar_one_or_none()
    if channel is None:
        raise NotificationNotFoundError(
            tr(
                "notification channel {channel_id} not found",
                channel_id=channel_id,
            )
        )
    return channel


async def update_channel(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    channel_id: UUID,
    data: ChannelUpdate,
    secret: str | None,
    vault: VaultService,
    sign_in: EmailSignIn | None = None,
) -> NotificationChannel:
    """Apply the SUPPLIED fields to a channel, org-scoped-loaded. `kind` is
    never touched here even if somehow present (it is absent from
    `_CHANNEL_EDITABLE_FIELDS` and `ChannelUpdate` has no such field at all).

    A supplied `secret` rotates the vault credential IN PLACE -- the same
    `credential_id` the channel already carries, if any, so a rotation never
    orphans a stale `Credential` row. A channel with no existing credential
    gets a freshly minted one, exactly like `create_channel`.

    Renaming to a name another channel in this org already holds is rejected
    up front by `_reject_duplicate_name`, same as `create_channel` -- and
    that pre-check is a fast path here too, not the sole guard: two
    concurrent renames to the same target name can both miss it before
    either has written. Unlike `create_channel`, the actual write here is an
    UPDATE of an already-loaded row, not an INSERT, so there is no `ON
    CONFLICT DO NOTHING` clause to reach for; instead the flush that applies
    the rename runs inside a SAVEPOINT (`session.begin_nested`), and an
    `IntegrityError` against `uq_channel_org_name` from that flush is caught,
    the savepoint rolled back, and the identical typed
    `NotificationValidationError` the pre-check raises is raised in its
    place -- never a raw `IntegrityError`, and never someone else's row (a
    caught race here always raises; it never returns a channel).

    The SAVEPOINT is opened BEFORE `channel` is mutated at all, deliberately
    -- `Session.begin_nested()` autoflushes any already-pending dirty state
    the instant it opens the new subtransaction, so if the rename (or any
    other field write, or the secret rotation) happened first, that
    autoflush would issue the UPDATE OUTSIDE the savepoint's protection,
    defeating it silently (verified empirically against this exact
    ordering bug while building this fix). Opening the SAVEPOINT first,
    with `channel` still clean, means the mutations below and the flush
    that applies them all happen inside the one subtransaction an
    `IntegrityError` here rolls back."""
    fields = data.model_dump(exclude_unset=True)
    _reject_null_for_non_nullable(fields)

    channel = await get_channel(session, org_id=org_id, channel_id=channel_id)

    if "config" in fields:
        # C1 fix: `ChannelUpdate` carries no `kind` field (schema.py's
        # docstring), so this is the one place a replacement `config` can be
        # validated against what the channel actually IS -- the row's own
        # `kind`, just loaded above. Before this fix, `ChannelUpdate(config=
        # {})` reached this function's `setattr` below completely
        # unvalidated; a poisoned config would then only surface as an
        # uncaught exception inside the WORKER, on whichever delivery
        # happened to use it next (see `delivery.py::attempt_delivery`'s own
        # fix for why that no longer wedges the whole sweep either way).
        try:
            validate_channel_config(channel.kind, fields["config"])
        except ValueError as exc:
            raise NotificationValidationError(str(exc)) from exc

    # An email channel's stored sign-in belongs to its `auth`: switching to
    # Microsoft needs the new values, and switching away drops the old ones.
    drop_credential: UUID | None = None
    if channel.kind == "email":
        new_config = fields.get("config") or channel.config
        if sign_in is not None:
            secret = _sign_in_secret(channel.kind, new_config, sign_in)
        if secret is not None:
            _check_email_secret(new_config, secret)
        new_auth = email_auth(new_config)
        if new_auth != email_auth(channel.config) and secret is None:
            if new_auth == "microsoft":
                raise NotificationValidationError(tr_marked(MICROSOFT_NEEDS_SIGN_IN))
            drop_credential = channel.credential_id

    renaming = "name" in fields and fields["name"] != channel.name
    if renaming:
        await _reject_duplicate_name(
            session, org_id, fields["name"], exclude_channel_id=channel.id
        )

    changed_fields: list[str] = []
    try:
        async with session.begin_nested():
            for field in _CHANNEL_EDITABLE_FIELDS:
                if field not in fields:
                    continue
                if getattr(channel, field) != fields[field]:
                    changed_fields.append(field)
                setattr(channel, field, fields[field])

            if secret is not None:
                channel.credential_id = await _store_secret(
                    session,
                    org_id=org_id,
                    actor_id=actor_id,
                    secret=secret,
                    vault=vault,
                    existing_credential_id=channel.credential_id,
                )
                changed_fields.append("credential_id")
            elif drop_credential is not None:
                channel.credential_id = None
                changed_fields.append("credential_id")

            session.add(channel)
            await session.flush()
    except IntegrityError:
        if renaming:
            raise NotificationValidationError(
                tr("channel name already exists: {name}", name=fields["name"])
            ) from None
        raise

    if drop_credential is not None:
        await _drop_credential(
            session, org_id=org_id, credential_id=drop_credential, vault=vault
        )

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="notification.channel_updated",
        target_type="notification_channel",
        target_id=str(channel.id),
        metadata={"changed_fields": sorted(set(changed_fields))},
    )
    await session.flush()
    return channel


async def delete_channel(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, channel_id: UUID
) -> None:
    """Org-scoped delete. Audits BEFORE the row is gone, so `target_id`
    still resolves to a real (now-historical) channel name in the immutable
    log."""
    channel = await get_channel(session, org_id=org_id, channel_id=channel_id)

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="notification.channel_deleted",
        target_type="notification_channel",
        target_id=str(channel.id),
        metadata={"name": channel.name},
    )
    await session.delete(channel)
    await session.flush()
