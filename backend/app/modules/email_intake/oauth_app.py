"""The org's Azure application registration for Microsoft 365 sign-in.

The client secret is vaulted here rather than written through (as Settings →
Email does to the realm) because this process makes the token exchange, on
every poll. That makes it a stored credential under invariant #3: a
`credentials` row of type `email_oauth_client`, rotated in place.
"""

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.vault import VaultService
from app.modules.email_intake import microsoft
from app.modules.email_intake.errors import MailboxValidationError
from app.modules.email_intake.mailboxes import _store_secret
from app.modules.email_intake.models import EmailMailbox, EmailOAuthApp
from app.modules.email_intake.schema import OAuthAppUpdate

__all__ = ["CLIENT_SECRET_TYPE", "app_client_secret", "get_app", "set_app"]

CLIENT_SECRET_TYPE = "email_oauth_client"


async def get_app(session: AsyncSession, *, org_id: UUID) -> EmailOAuthApp | None:
    return (
        await session.execute(select(EmailOAuthApp).where(EmailOAuthApp.org_id == org_id))
    ).scalar_one_or_none()


async def set_app(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    body: OAuthAppUpdate,
    vault: VaultService,
) -> EmailOAuthApp:
    """Create or update the registration. An absent `client_secret` keeps the
    stored one; on the first save there is nothing to keep, so it is required."""
    tenant_id = body.tenant_id.strip()
    client_id = body.client_id.strip()
    try:
        microsoft.token_url(tenant_id)
    except ValueError as exc:
        raise MailboxValidationError(str(exc)) from None
    if not client_id:
        raise MailboxValidationError(tr("the application (client) ID is empty"))

    existing = await get_app(session, org_id=org_id)
    if existing is None and body.client_secret is None:
        raise MailboxValidationError(
            tr("a client secret is required the first time this application is saved")
        )

    if body.client_secret is not None:
        credential_id = await _store_secret(
            session,
            org_id=org_id,
            actor_id=actor_id,
            secret=body.client_secret,
            vault=vault,
            existing_credential_id=existing.credential_id if existing else None,
            name="email-oauth-client-secret",
            credential_type=CLIENT_SECRET_TYPE,
        )
    else:
        assert existing is not None  # guarded above
        credential_id = existing.credential_id

    if existing is None:
        existing = EmailOAuthApp(
            org_id=org_id,
            tenant_id=tenant_id,
            client_id=client_id,
            credential_id=credential_id,
            created_by=actor_id,
        )
    else:
        existing.tenant_id = tenant_id
        existing.client_id = client_id
        existing.credential_id = credential_id
    session.add(existing)
    await session.flush()

    # A mailbox stopped because Microsoft refused the APPLICATION gets another
    # try now that the application changed; one whose own sign-in is dead
    # fails again at its next poll and goes back to needs_reconsent.
    await session.execute(
        update(EmailMailbox)
        .where(
            EmailMailbox.org_id == org_id,
            EmailMailbox.auth_kind == "microsoft_oauth",
            EmailMailbox.credential_state == "needs_reconsent",
            EmailMailbox.credential_id.is_not(None),
        )
        .values(credential_state="ok")
    )

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="email_intake.oauth_app_configured",
        target_type="email_oauth_app",
        target_id=str(org_id),
        # Field names only: `client_secret` here records a rotation, never a value.
        metadata={"fields": sorted(body.model_fields_set)},
    )
    return existing


async def app_client_secret(
    session: AsyncSession,
    *,
    org_id: UUID,
    app: EmailOAuthApp,
    vault: VaultService,
    mailbox_id: UUID,
    actor_id: UUID | None,
) -> str:
    """Decrypt at point of use, audited in the same transaction (invariant #4).
    The caller holds the result for one token exchange and drops it."""
    secret = await vault.checkout(session, org_id=org_id, credential_id=app.credential_id)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="system" if actor_id is None else "user",
        actor_id=actor_id,
        action="email_intake.credential_used",
        target_type="credential",
        target_id=str(app.credential_id),
        metadata={"mailbox_id": str(mailbox_id), "credential": CLIENT_SECRET_TYPE},
    )
    return secret
