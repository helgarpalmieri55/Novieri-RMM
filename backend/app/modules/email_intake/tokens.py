"""The stored refresh token, turned into an access token at point of use.

The access token is never stored: fetched per poll, handed to one IMAP
connection, dropped. A rotated refresh token is stored BEFORE the access token
is returned, so a crash in between costs one poll rather than the mailbox
(design §4.2).

Two exception classes, because they need different people. `TokenRevoked`
means a person must act (reconnect the mailbox, or fix the Azure application,
per `cause`); `TokenTransportError` means try again next interval. Neither ever
carries provider text: only the machine `error` code is read.

`app_access_token` is the client-credentials sibling, used by notification
email (SMTP.SendAsApp). Both go through `_exchange` and the pinned host.
"""

from typing import Literal
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.audit import audit_service
from app.core.tenancy import org_session
from app.core.vault import VaultService
from app.modules.email_intake import microsoft, oauth_app
from app.modules.email_intake.mailboxes import _store_secret
from app.modules.email_intake.models import EmailMailbox
from app.modules.email_intake.oauth import REFRESH_TOKEN_TYPE, refusal_code

__all__ = ["TokenRevoked", "TokenTransportError", "access_token_for", "app_access_token"]

#: The stored sign-in is dead; only the person who owns the mailbox can renew it.
_CONSENT = frozenset({"invalid_grant", "interaction_required", "consent_required"})
#: The Azure application itself was refused; reconnecting the mailbox won't help.
_APPLICATION = frozenset({"invalid_client", "unauthorized_client", "invalid_scope"})


class TokenRevoked(Exception):
    """Permanent until a person acts. `cause` says which person."""

    def __init__(self, cause: Literal["consent", "application"]) -> None:
        super().__init__(  # i18n-exempt: the poller words it by cause (REVOKED_MESSAGES)
            f"Microsoft refused the stored sign-in ({cause})"
        )
        self.cause = cause


class TokenTransportError(Exception):
    """Transient. `reason` comes from a fixed vocabulary, never provider text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def _exchange(
    tenant_id: str, form: dict[str, str], *, permanent_http: frozenset[int]
) -> dict[str, object]:
    """POST one grant to the pinned token endpoint; the parsed JSON body.
    `permanent_http` are statuses that mean "the application was refused"
    even when the body names no code we know."""
    try:
        async with microsoft.http_client() as client:
            response = await client.post(microsoft.token_url(tenant_id), data=form)
    except httpx.HTTPError:
        # Never `str(exc)`: httpx puts the request in its messages.
        raise TokenTransportError(  # i18n-exempt: token, stored in last_error as written
            "microsoft-unreachable"
        ) from None

    if response.status_code != 200:
        code = refusal_code(response)
        if code in _CONSENT:
            raise TokenRevoked("consent")  # i18n-exempt: token, stored in last_error as written
        if code in _APPLICATION or response.status_code in permanent_http:
            raise TokenRevoked(  # i18n-exempt: token, stored in last_error as written
                "application"
            )
        raise TokenTransportError(  # i18n-exempt: token, stored in last_error as written
            f"microsoft-http-{response.status_code}"
        )

    try:
        payload = response.json()
    except ValueError:
        raise TokenTransportError(  # i18n-exempt: token, stored in last_error as written
            "microsoft-bad-response"
        ) from None
    if not isinstance(payload, dict):
        raise TokenTransportError(  # i18n-exempt: token, stored in last_error as written
            "microsoft-bad-response"
        )
    return payload


async def app_access_token(
    *, tenant_id: str, client_id: str, client_secret: str, scope: str
) -> str:
    """Client-credentials (app-only) access token, e.g. for SMTP.SendAsApp.
    No person's consent is involved, so every refusal is the application's."""
    try:
        payload = await _exchange(
            tenant_id,
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "client_credentials",
                "scope": scope,
            },
            permanent_http=frozenset({400, 401, 403}),
        )
    except TokenRevoked:
        raise TokenRevoked(  # i18n-exempt: token, stored in last_error as written
            "application"
        ) from None
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise TokenTransportError(  # i18n-exempt: token, stored in last_error as written
            "microsoft-bad-response"
        )
    return token


async def access_token_for(
    org_id: UUID,
    mailbox_id: UUID,
    *,
    engine: AsyncEngine,
    vault: VaultService,
    actor_id: UUID | None = None,
) -> str:
    """An access token for one IMAP connection. `actor_id` is the operator
    who pressed Test; `None` for a scheduled poll."""
    async with org_session(org_id, engine=engine) as session:
        mailbox = (
            await session.execute(
                select(EmailMailbox).where(
                    EmailMailbox.org_id == org_id, EmailMailbox.id == mailbox_id
                )
            )
        ).scalar_one()
        app = await oauth_app.get_app(session, org_id=org_id)
        if app is None:
            raise TokenRevoked(  # i18n-exempt: token, stored in last_error as written
                "application"
            )
        if mailbox.credential_id is None:
            raise TokenRevoked("consent")  # i18n-exempt: token, stored in last_error as written
        credential_id, created_by = mailbox.credential_id, mailbox.created_by
        tenant_id, client_id = app.tenant_id, app.client_id
        refresh = await vault.checkout(session, org_id=org_id, credential_id=credential_id)
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="system" if actor_id is None else "user",
            actor_id=actor_id,
            action="email_intake.credential_used",
            target_type="credential",
            target_id=str(credential_id),
            metadata={"mailbox_id": str(mailbox_id), "credential": REFRESH_TOKEN_TYPE},
        )
        secret = await oauth_app.app_client_secret(
            session, org_id=org_id, app=app, vault=vault, mailbox_id=mailbox_id, actor_id=actor_id
        )

    try:
        payload = await _exchange(
            tenant_id,
            {
                "client_id": client_id,
                "client_secret": secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "scope": microsoft.IMAP_SCOPE,
            },
            permanent_http=frozenset(),
        )
    finally:
        secret = ""

    rotated = payload.get("refresh_token")
    if isinstance(rotated, str) and rotated and rotated != refresh:
        # Stored before the access token leaves this function (module docstring).
        async with org_session(org_id, engine=engine) as session:
            await _store_secret(
                session,
                org_id=org_id,
                actor_id=created_by,
                secret=rotated,
                vault=vault,
                existing_credential_id=credential_id,
                name="email-mailbox-oauth-refresh",
                credential_type=REFRESH_TOKEN_TYPE,
            )
    refresh = ""

    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise TokenTransportError(  # i18n-exempt: token, stored in last_error as written
            "microsoft-bad-response"
        )
    return token
