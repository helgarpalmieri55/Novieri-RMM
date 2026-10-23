"""Connect with Microsoft: the delegated-consent handshake (start, complete,
abandon). Design: docs/superpowers/specs/2026-09-11-mail-oauth-design.md §4.3.

Microsoft redirects the browser to a CONSOLE route; the console then calls
`complete` as an ordinary authenticated request. RBAC is the route's
`email_intake.admin` gate and tenancy is `org_session` + RLS (another org's
row is simply not visible), so `state` does only CSRF and replay defence.

Single use is an `UPDATE ... RETURNING` that commits BEFORE the call to
Microsoft, so a state is spent even when the exchange fails, and no database
transaction is held open across someone else's network timeout. The update
also requires the caller to be the person who started the flow and the row to
be unexpired, so a colleague or an attacker cannot burn it.

Only the provider's `error` code is ever read from a refusal: its
`error_description` can quote what it refused, which here is a client secret.
"""

import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import audit_service
from app.core.client_scope import ClientScope
from app.core.config import Settings
from app.core.i18n import N_, tr, tr_marked
from app.core.tenancy import org_session
from app.core.vault import VaultService
from app.modules.email_intake import microsoft, oauth_app
from app.modules.email_intake.mailboxes import _store_secret
from app.modules.email_intake.models import EmailMailbox, EmailOAuthAuthorization

__all__ = ["REFRESH_TOKEN_TYPE", "TTL", "OAuthFlowError", "abandon", "complete", "start"]

#: Long enough for a sign-in with MFA; short enough that a leaked `state` is
#: stale before anyone could use it.
TTL = timedelta(minutes=10)

REFRESH_TOKEN_TYPE = "email_mailbox_oauth"

APPLICATION_REFUSED = N_(
    "Microsoft refused the application this organisation is registered with. "
    "Check Email Intake → Microsoft connection; reconnecting the mailbox will not fix it."
)


class OAuthFlowError(Exception):
    """Carries the HTTP status the route answers with."""

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


async def _oauth_mailbox(session: AsyncSession, org_id: UUID, mailbox_id: UUID) -> EmailMailbox:
    row = (
        await session.execute(
            select(EmailMailbox).where(
                EmailMailbox.org_id == org_id, EmailMailbox.id == mailbox_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise OAuthFlowError(
            tr("mailbox {mailbox_id} not found", mailbox_id=mailbox_id), status=404
        )
    if row.auth_kind != "microsoft_oauth":
        raise OAuthFlowError(
            tr("this mailbox signs in with a password; switch it to Microsoft first"), status=409
        )
    return row


async def start(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    mailbox_id: UUID,
    settings: Settings,
    vault: VaultService,
) -> str:
    """Mint a single-use authorization and return the URL to send the browser to."""
    mailbox = await _oauth_mailbox(session, org_id, mailbox_id)
    app = await oauth_app.get_app(session, org_id=org_id)
    if app is None:
        raise OAuthFlowError(
            tr("set up the Microsoft connection for this organisation first"), status=409
        )
    redirect = microsoft.redirect_uri(settings)

    verifier = microsoft.make_verifier()
    state = secrets.token_urlsafe(32)
    row = EmailOAuthAuthorization(
        org_id=org_id,
        mailbox_id=mailbox.id,
        user_id=user_id,
        state=state,
        sealed_verifier="",
        expires_at=datetime.now(UTC) + TTL,
    )
    session.add(row)
    # Flushed first: the seal is bound to this row's id.
    await session.flush()
    row.sealed_verifier = await vault.seal_bytes(
        session, org_id=org_id, key_id=row.id, plaintext=verifier.encode()
    )
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=user_id,
        action="email_intake.oauth_started",
        target_type="email_mailbox",
        target_id=str(mailbox.id),
        metadata={"address": mailbox.address},
    )
    return microsoft.authorize_url(
        tenant_id=app.tenant_id,
        client_id=app.client_id,
        redirect_uri=redirect,
        state=state,
        code_challenge=microsoft.challenge_of(verifier),
    )


async def _consume(
    session: AsyncSession, *, org_id: UUID, user_id: UUID, state: str
) -> EmailOAuthAuthorization:
    """Spend the authorization atomically, or say why not."""
    now = datetime.now(UTC)
    row = (
        await session.execute(
            update(EmailOAuthAuthorization)
            .where(
                EmailOAuthAuthorization.org_id == org_id,
                EmailOAuthAuthorization.state == state,
                EmailOAuthAuthorization.consumed_at.is_(None),
                EmailOAuthAuthorization.user_id == user_id,
                EmailOAuthAuthorization.expires_at > now,
            )
            .values(consumed_at=now)
            .returning(EmailOAuthAuthorization)
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    # Classify the refusal. Another org's row is invisible under RLS and so
    # reads exactly like an unknown or used one: no cross-tenant oracle.
    found = (
        await session.execute(
            select(EmailOAuthAuthorization).where(
                EmailOAuthAuthorization.org_id == org_id,
                EmailOAuthAuthorization.state == state,
                EmailOAuthAuthorization.consumed_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if found is None:
        raise OAuthFlowError(
            tr("that sign-in has already been used, or does not exist"), status=409
        )
    if found.user_id != user_id:
        raise OAuthFlowError(tr("that sign-in was started by somebody else"), status=403)
    raise OAuthFlowError(tr("that sign-in took too long; start it again"), status=410)


def refusal_code(response: httpx.Response) -> str:
    """The provider's machine `error` code, and nothing else from the body."""
    try:
        body = response.json()
    except ValueError:
        return ""
    code = body.get("error") if isinstance(body, dict) else None
    return code if isinstance(code, str) and code.isascii() and len(code) <= 64 else ""


async def complete(
    *,
    org_id: UUID,
    user_id: UUID,
    state: str,
    code: str,
    settings: Settings,
    engine: AsyncEngine,
    vault: VaultService,
) -> UUID:
    """Redeem the authorization code and store the refresh token; returns the
    mailbox id.

    Three steps, deliberately: spend the state and decrypt what the exchange
    needs (one transaction, committed); call Microsoft (no transaction); store
    the refresh token (one transaction).
    """
    redirect = microsoft.redirect_uri(settings)
    async with org_session(org_id, engine=engine, clients=ClientScope.all()) as session:
        row = await _consume(session, org_id=org_id, user_id=user_id, state=state)
        mailbox = await _oauth_mailbox(session, org_id, row.mailbox_id)
        app = await oauth_app.get_app(session, org_id=org_id)
        if app is None:
            raise OAuthFlowError(tr("the Microsoft connection has been removed"), status=409)
        mailbox_id, address = mailbox.id, mailbox.address
        tenant_id, client_id = app.tenant_id, app.client_id
        verifier = (
            await vault.open_bytes(
                session, org_id=org_id, key_id=row.id, sealed=row.sealed_verifier
            )
        ).decode()
        secret = await oauth_app.app_client_secret(
            session, org_id=org_id, app=app, vault=vault, mailbox_id=mailbox_id, actor_id=user_id
        )

    try:
        async with microsoft.http_client() as client:
            response = await client.post(
                microsoft.token_url(tenant_id),
                data={
                    "client_id": client_id,
                    "client_secret": secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect,
                    "code_verifier": verifier,
                    "scope": microsoft.IMAP_SCOPE,
                },
            )
    except httpx.HTTPError as exc:
        # The class name only: httpx puts the request in its messages.
        raise OAuthFlowError(
            tr(
                "could not reach Microsoft ({error_class}); try again",
                error_class=type(exc).__name__,
            ),
            status=502,
        ) from None
    finally:
        secret = verifier = ""

    if response.status_code != 200:
        refused = refusal_code(response)
        if refused in ("invalid_client", "unauthorized_client", "invalid_scope"):
            raise OAuthFlowError(tr_marked(APPLICATION_REFUSED), status=400)
        if response.status_code >= 500:
            raise OAuthFlowError(
                tr(
                    "Microsoft could not complete the sign-in (HTTP {http_status}); try again",
                    http_status=response.status_code,
                ),
                status=502,
            )
        raise OAuthFlowError(tr("Microsoft refused that sign-in; start it again"), status=400)

    try:
        payload = response.json()
    except ValueError:
        payload = {}
    refresh = payload.get("refresh_token") if isinstance(payload, dict) else None
    if not isinstance(refresh, str) or not refresh:
        raise OAuthFlowError(
            tr(
                "Microsoft returned no refresh token, so this mailbox could only be read for "
                "an hour. Check that the application is allowed offline_access."
            ),
            status=400,
        )

    async with org_session(org_id, engine=engine, clients=ClientScope.all()) as session:
        mailbox = await _oauth_mailbox(session, org_id, mailbox_id)
        mailbox.credential_id = await _store_secret(
            session,
            org_id=org_id,
            actor_id=user_id,
            secret=refresh,
            vault=vault,
            existing_credential_id=mailbox.credential_id,
            name="email-mailbox-oauth-refresh",
            credential_type=REFRESH_TOKEN_TYPE,
        )
        mailbox.credential_state = "ok"
        mailbox.last_error = None
        session.add(mailbox)
        await session.flush()
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="user",
            actor_id=user_id,
            action="email_intake.oauth_connected",
            target_type="email_mailbox",
            target_id=str(mailbox_id),
            metadata={"address": address},
        )
    return mailbox_id


async def abandon(session: AsyncSession, *, org_id: UUID, user_id: UUID, state: str) -> None:
    """The person cancelled at Microsoft: spend the state, leave the mailbox as it was."""
    row = await _consume(session, org_id=org_id, user_id=user_id, state=state)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=user_id,
        action="email_intake.oauth_abandoned",
        target_type="email_mailbox",
        target_id=str(row.mailbox_id),
        metadata={},
    )
