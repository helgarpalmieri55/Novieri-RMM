"""Authorization codes and refresh-token families (design §5.2, §5.3, §7;
plan D6, D10).

THE CODE (§5.2): 256 random bits, stored as its SHA-256, 60 seconds, single
use, bound to the app, the exact redirect the authorization named (a loopback
port included), the PKCE challenge and the resource. The token request need
not repeat the redirect (OAuth 2.1-17 §10.2: "A client following only the
OAuth 2.1 recommendations will not send the redirect_uri in the token
request"; plan B3-23); when it does, it is that one. A FAILED EXCHANGE SPENDS
THE CODE (RFC 6749 §10.5: "Authorization codes MUST be short lived and
single-use"; plan B3-21): another app, another redirect or a wrong verifier is
refused, the code is used up and a WARNING says why -- a legitimate client
sends none of the three wrong. Used again after a success, the code is
refused and the grant its first use opened ends: RFC 6749 §4.1.2, "MUST deny
the request and SHOULD revoke (when possible) all tokens previously issued
based on that authorization code".

THE FAMILY (§5.3): one person's grant to one app. Its refresh tokens are 256
random bits stored as SHA-256 and ROTATE on every use (RFC 9700: "the
authorization server issues a new refresh token with every access token
refresh response. The previous refresh token is invalidated, but information
about the relationship is retained" [S16]; MCP: "MUST rotate refresh tokens"
[S30]). Presenting a used one ends the family, with NO GRACE WINDOW (§5.3:
"reutilizar uno ya usado revoca toda la familia"; plan B3-27): a refresh whose
answer the app never read, or two racing with one token, end it too, and the
person connects again. So does a person no longer active; the family dies
after `REFRESH_IDLE` without use. Every refusal is `invalid_grant`
(Anthropic: "Return RFC 6749-compliant error codes when a refresh token is no
longer valid: invalid_grant" [S33]).

WHAT IS AUDITED (§7, "consentimientos y revocaciones de clientes de IA"; plan
B3-22): every end of a grant the person did not cause -- a reused code, a
reused refresh token, the app's own RFC 7009 request, a person no longer
active -- as "system", the person as the target, the app in `metadata.app`.
NOT a refresh, which continues a consent already audited
(`mcp.app_authorized`, Task 8) every five to ten minutes while the app is in
use, and NOT a registration (Task 6), a stranger's claim that grants nothing
until a person approves it. Each action is a literal at its call: the audit
vocabulary's coverage test reads them.

ONE TRANSACTION PER REQUEST, SIGNED INSIDE IT (`oauth_session`): the access
token is minted before the commit, so OpenBao down rolls back and spends
nothing. A refusal that follows a write -- a spent code, a reuse that ended a
family -- is returned as a `Problem` and raised AFTER the block has
committed, so the write stays (the `flows` pattern, phase 1). The app's
`last_used_at` is touched after the signature, at most once an hour
(`clients.touch`; A3-37).

NOTHING IS DELETED HERE: spent codes and ended families go with the daily
retention sweep (`app/core/auth/retention.py`, Task 13).

LOCKED IN THE ORDER OF A PERSON'S ACCOUNT (own authentication phase 2's
rule, `guards.lock_user_row`; `store`'s LOCK ORDER; operator decision D2,
2026-11-12). `_exchange` and `_refresh` first read the code, or the refresh
token and its family, WITHOUT A LOCK, only to learn whose it is and for which
app (the `flows._consumed` pattern). Then, in this order:

1. the person's `users` row FOR NO KEY UPDATE, re-read (`lock_user_row`);
2. the app's row, `clients.hold`: FOR KEY SHARE, re-read, the idle condition
   re-checked, nothing written;
3. the code, or the token and its family, FOR UPDATE, re-read, and every
   condition checked again on what was re-read. Whether the person is active
   and staff is read from the row locked at 1, never from an unlocked one;
4. the grant rows written: the family and its token, the code's `used_at`;
5. the access token signed;
6. the app's use, `clients.touch`, flushed (plan decision 9: the use comes
   last, never across the signature);
7. the audit chain, last.

So two requests with one code or one token wait for each other on the
person's row, and the second sees it used. An account act -- a disable, a
deletion, any act that ends the person's grants -- holds the same row first:
an exchange that meets one waits for it and reads its outcome, and opens no
grant the act's revocation could miss and a later re-enable bring back
(`tests/test_oauth_token.py`, race (a)). An idle app's purge waits on the app
row at 2, never in its cascade on a grant row this transaction holds, and a
use that commits first spares the app (race (b)).

A REVOCATION TAKES NEITHER ROW (`store`'s carve-out). `revoke_presented`
reads the grant unlocked, then makes one UPDATE of its family
(`revoke_family`) and writes only the chain after it. `revoke_all_for_user`
and `revoke_app` are the same one UPDATE; their callers (Task 10) hold what
they hold, and nothing here takes a lock for them.

A CODE AN ACCOUNT ACT ENDED is one whose `expires_at` the act set to now --
never its `used_at` (Task 10; operator decision D2, part c). The exchange
answers it as any expired code: a plain `invalid_grant`, with nothing spent,
logged, audited or revoked. Only a `used_at` already set is a reuse. The row
lock alone does not stop an act that leaves the person active (signing out
everywhere, a new password, a factor reset): the exchange waits for it and
then runs. Ending the person's unspent codes in the same act does: the
exchange that waited re-reads its code at 3 and finds it expired.

BULK STATEMENTS ON O TABLES (`update`) are invisible to
`app.core.global_rows`'s listener; every one here runs under `oauth_session`
or a caller's every-client session (the lifecycle acts, Task 10), where
writing a technician's global rows is allowed.
"""

import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.auth.guards import lock_user_row
from app.core.auth.oauth import clients, pkce
from app.core.auth.oauth.access import mint_access_token, verify_access_token
from app.core.auth.oauth.errors import (
    BAD_CODE,
    BAD_REFRESH,
    BAD_VERIFIER,
    UNKNOWN_CLIENT,
    WRONG_CLIENT,
    OAuthError,
    Problem,
)
from app.core.auth.oauth.policy import CODE_TTL_S, REFRESH_IDLE
from app.core.auth.oauth.store import oauth_session
from app.core.identity import ACTIVE_STATUS, STAFF_KIND
from app.models import User
from app.models.oauth import OAuthClient, OAuthCode, OAuthRefreshFamily, OAuthRefreshToken

logger = logging.getLogger("its.auth.oauth.grants")


def hash_secret(value: str) -> bytes:
    """SHA-256: a random 256-bit value needs no salt and no pepper."""
    return sha256(value.encode("utf-8")).digest()


def new_secret() -> str:
    """256 random bits, base64url: 43 characters."""
    return secrets.token_urlsafe(32)


def names_resource(value: str, resource: str) -> bool:
    """`value` is `resource`, its scheme and host in any case and the rest
    exactly (MCP: "implementations SHOULD accept uppercase scheme and host
    components for robustness and interoperability"; RFC 3986 §6.2.2.1; plan
    B3-05)."""
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    canonical = urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, parts.fragment)
    )
    return canonical == resource


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str = field(repr=False)
    expires_in: int
    refresh_token: str = field(repr=False)


def _active(person: User | None, org_id: UUID) -> bool:
    """The person a grant names may hold one: read from THEIR LOCKED ROW
    (`lock_user_row`, step 1 of the lock order), never from an unlocked
    read, which an account act in flight would not have reached yet."""
    return (
        person is not None
        and person.org_id == org_id
        and person.status == ACTIVE_STATUS
        and person.kind == STAFF_KIND
    )


async def issue_code(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    oauth_client_id: UUID,
    redirect_uri: str,
    code_challenge: str,
    resource: str,
    now: datetime,
) -> str:
    """A one-time code for what the person just approved (Task 8)."""
    code = new_secret()
    session.add(
        OAuthCode(
            org_id=org_id,
            user_id=user_id,
            oauth_client_id=oauth_client_id,
            code_hash=hash_secret(code),
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            resource=resource,
            created_at=now,
            expires_at=now + timedelta(seconds=CODE_TTL_S),
        )
    )
    await session.flush()
    return code


async def _new_refresh_token(
    session: AsyncSession, family: OAuthRefreshFamily, now: datetime
) -> str:
    secret = new_secret()
    session.add(
        OAuthRefreshToken(
            org_id=family.org_id,
            user_id=family.user_id,
            family_id=family.id,
            token_hash=hash_secret(secret),
            created_at=now,
        )
    )
    await session.flush()
    return secret


async def open_family(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    oauth_client_id: UUID,
    resource: str,
    now: datetime,
) -> tuple[OAuthRefreshFamily, str]:
    """A new grant and its first refresh token."""
    family = OAuthRefreshFamily(
        org_id=org_id,
        user_id=user_id,
        oauth_client_id=oauth_client_id,
        resource=resource,
        created_at=now,
        last_used_at=now,
        expires_at=now + REFRESH_IDLE,
    )
    session.add(family)
    await session.flush()
    return family, await _new_refresh_token(session, family, now)


async def revoke_family(session: AsyncSession, family_id: UUID, reason: str, now: datetime) -> bool:
    """End one grant; False when it had already ended."""
    ended = await session.execute(
        update(OAuthRefreshFamily)
        .where(OAuthRefreshFamily.id == family_id, OAuthRefreshFamily.revoked_at.is_(None))
        .values(revoked_at=now, revoked_reason=reason)
        .returning(OAuthRefreshFamily.id)
    )
    return ended.first() is not None


async def revoke_all_for_user(session: AsyncSession, user_id: UUID, reason: str) -> int:
    """End every live grant of a person (the lifecycle acts, Task 10); how many."""
    ended = await session.execute(
        update(OAuthRefreshFamily)
        .where(OAuthRefreshFamily.user_id == user_id, OAuthRefreshFamily.revoked_at.is_(None))
        .values(revoked_at=_now(), revoked_reason=reason)
        .returning(OAuthRefreshFamily.id)
    )
    return len(ended.all())


async def revoke_app(
    session: AsyncSession,
    *,
    oauth_client_id: UUID,
    user_id: UUID | None,
    reason: str,
    now: datetime,
) -> int:
    """End an app's live grants: one person's, or (`user_id` None) everyone's
    in the session's organization (Task 10); how many."""
    query = update(OAuthRefreshFamily).where(
        OAuthRefreshFamily.oauth_client_id == oauth_client_id,
        OAuthRefreshFamily.revoked_at.is_(None),
    )
    if user_id is not None:
        query = query.where(OAuthRefreshFamily.user_id == user_id)
    ended = await session.execute(
        query.values(revoked_at=now, revoked_reason=reason).returning(OAuthRefreshFamily.id)
    )
    return len(ended.all())


async def family_is_live(
    session: AsyncSession, *, family_id: UUID, user_id: UUID, client_id: str, now: datetime
) -> bool:
    """The grant an access token names still stands, for that person and that
    app (the MCP door, Task 9; plan D6)."""
    found = (
        await session.execute(
            select(OAuthRefreshFamily.id)
            .join(OAuthClient, OAuthClient.id == OAuthRefreshFamily.oauth_client_id)
            .where(
                OAuthRefreshFamily.id == family_id,
                OAuthRefreshFamily.user_id == user_id,
                OAuthClient.client_id == client_id,
                OAuthRefreshFamily.revoked_at.is_(None),
                OAuthRefreshFamily.expires_at > now,
            )
        )
    ).scalar_one_or_none()
    return found is not None


# --------------------------------------------------------------------------- #
# The token endpoint's two grants and RFC 7009
# --------------------------------------------------------------------------- #


def _refusal(
    row: OAuthCode, client: OAuthClient, redirect_uri: str | None, code_verifier: str
) -> tuple[str, Problem] | None:
    """Why the exchange of a live, unused code fails, if it does (B3-21)."""
    if row.oauth_client_id != client.id:
        return "client", BAD_CODE
    if redirect_uri is not None and redirect_uri != row.redirect_uri:
        return "redirect", BAD_CODE
    if not pkce.verify(code_verifier, row.code_challenge):
        return "verifier", BAD_VERIFIER
    return None


async def _exchange(
    session: AsyncSession,
    settings: Any,
    *,
    org_id: UUID,
    client_id: str,
    code: str,
    redirect_uri: str | None,
    code_verifier: str,
    now: datetime,
) -> IssuedTokens | Problem:
    client = await clients.find_client(session, org_id=org_id, client_id=client_id, now=now)
    if client is None:
        return UNKNOWN_CLIENT  # no app: no code is read (Task 7, decision 2)
    digest = hash_secret(code)
    # Whose code, for which app: read unlocked, only to know what to lock.
    seen = (
        await session.execute(
            select(OAuthCode.user_id, OAuthCode.oauth_client_id).where(
                OAuthCode.code_hash == digest
            )
        )
    ).one_or_none()
    if seen is None:
        return BAD_CODE
    person = await lock_user_row(session, seen.user_id)  # 1. the person
    # 2. The code's app: the one whose purge would take the code by cascade.
    app = await clients.hold(session, org_id=org_id, app_id=seen.oauth_client_id, now=now)
    if app is None:
        return BAD_CODE  # its app went idle or was purged: the code with it
    row = (  # 3. the code, re-read under its lock
        await session.execute(
            select(OAuthCode)
            .where(OAuthCode.code_hash == digest)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None or row.user_id != seen.user_id or row.oauth_client_id != app.id:
        return BAD_CODE
    if row.used_at is not None:
        if row.family_id is not None and await revoke_family(
            session, row.family_id, "code_reused", now
        ):
            await audit_service.record(  # 7. the chain, last
                session,
                org_id=org_id,
                actor_type="system",
                actor_id=None,
                action="mcp.code_reused",
                target_type="user",
                target_id=str(row.user_id),
                metadata={"app": app.client_id, "family": str(row.family_id)},
            )
        return BAD_CODE
    if row.expires_at <= now:
        # Expired, or ended by an account act (`expires_at = now()`; D2, part
        # c): a plain refusal that spends, logs, audits and revokes nothing.
        return BAD_CODE
    refused = _refusal(row, client, redirect_uri, code_verifier)
    row.used_at = now  # 4. spent, whatever follows (B3-21)
    if refused is not None:
        reason, problem = refused
        logger.warning(
            "authorization code refused and spent",
            extra={"reason": reason, "app": client.client_id},
        )
        return problem
    if not _active(person, org_id):
        return BAD_CODE
    family, refresh_token = await open_family(
        session,
        org_id=org_id,
        user_id=row.user_id,
        oauth_client_id=app.id,
        resource=row.resource,
        now=now,
    )
    row.family_id = family.id
    await session.flush()
    access_token, expires_in = await mint_access_token(  # 5. the signature
        settings=settings,
        user_id=row.user_id,
        org_id=org_id,
        client_id=app.client_id,
        family_id=family.id,
    )
    clients.touch(app, now)  # 6. the use, after the signature (A3-37)
    await session.flush()
    return IssuedTokens(access_token, expires_in, refresh_token)


async def exchange_code(
    app: Any,
    *,
    org_id: UUID,
    client_id: str,
    code: str,
    redirect_uri: str | None,
    code_verifier: str,
) -> IssuedTokens:
    async with oauth_session(org_id, app.state.app_engine) as session:
        outcome = await _exchange(
            session,
            app.state.settings,
            org_id=org_id,
            client_id=client_id,
            code=code,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
            now=_now(),
        )
    if isinstance(outcome, Problem):
        raise OAuthError(outcome)
    return outcome


async def _refresh(
    session: AsyncSession,
    settings: Any,
    *,
    org_id: UUID,
    client_id: str,
    refresh_token: str,
    now: datetime,
) -> IssuedTokens | Problem:
    client = await clients.find_client(session, org_id=org_id, client_id=client_id, now=now)
    if client is None:
        return UNKNOWN_CLIENT
    digest = hash_secret(refresh_token)
    # Whose token, for which app: read unlocked, only to know what to lock.
    seen = (
        await session.execute(
            select(OAuthRefreshToken.user_id, OAuthRefreshFamily.oauth_client_id)
            .join(OAuthRefreshFamily, OAuthRefreshFamily.id == OAuthRefreshToken.family_id)
            .where(OAuthRefreshToken.token_hash == digest)
        )
    ).one_or_none()
    if seen is None:
        return BAD_REFRESH
    if seen.oauth_client_id != client.id:
        # Another app's: not a reuse (Task 7, decision 1); refused before
        # anything is locked, and a family's app never changes.
        return BAD_REFRESH
    person = await lock_user_row(session, seen.user_id)  # 1. the person
    app = await clients.hold(session, org_id=org_id, app_id=client.id, now=now)  # 2. the app
    if app is None:
        return BAD_REFRESH  # it went idle or was purged since: its grants with it
    found = (  # 3. the token and its family, re-read under their locks
        await session.execute(
            select(OAuthRefreshToken, OAuthRefreshFamily)
            .join(OAuthRefreshFamily, OAuthRefreshFamily.id == OAuthRefreshToken.family_id)
            .where(OAuthRefreshToken.token_hash == digest)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if found is None:
        return BAD_REFRESH
    token, family = found
    if family.user_id != seen.user_id or family.oauth_client_id != app.id:
        return BAD_REFRESH
    if family.revoked_at is not None or family.expires_at <= now:
        return BAD_REFRESH
    if token.used_at is not None:
        if await revoke_family(session, family.id, "refresh_reused", now):
            await audit_service.record(  # 7. the chain, last
                session,
                org_id=org_id,
                actor_type="system",
                actor_id=None,
                action="mcp.refresh_reused",
                target_type="user",
                target_id=str(family.user_id),
                metadata={"app": app.client_id, "family": str(family.id)},
            )
        return BAD_REFRESH
    if not _active(person, org_id):
        if await revoke_family(session, family.id, "person_inactive", now):
            await audit_service.record(  # 7. the chain, last
                session,
                org_id=org_id,
                actor_type="system",
                actor_id=None,
                action="mcp.grant_ended",
                target_type="user",
                target_id=str(family.user_id),
                metadata={
                    "app": app.client_id,
                    "family": str(family.id),
                    "reason": "person_inactive",
                },
            )
        return BAD_REFRESH
    token.used_at = now  # 4. the grant rows
    family.last_used_at = now
    family.expires_at = now + REFRESH_IDLE
    rotated = await _new_refresh_token(session, family, now)
    access_token, expires_in = await mint_access_token(  # 5. the signature
        settings=settings,
        user_id=family.user_id,
        org_id=org_id,
        client_id=app.client_id,
        family_id=family.id,
    )
    clients.touch(app, now)  # 6. the use, after the signature (A3-37)
    await session.flush()
    return IssuedTokens(access_token, expires_in, rotated)


async def refresh_grant(
    app: Any, *, org_id: UUID, client_id: str, refresh_token: str
) -> IssuedTokens:
    async with oauth_session(org_id, app.state.app_engine) as session:
        outcome = await _refresh(
            session,
            app.state.settings,
            org_id=org_id,
            client_id=client_id,
            refresh_token=refresh_token,
            now=_now(),
        )
    if isinstance(outcome, Problem):
        raise OAuthError(outcome)
    return outcome


async def revoke_presented(app: Any, *, org_id: UUID, client_id: str, token: str) -> None:
    """RFC 7009: a refresh token or an access token of this client ends its
    grant; a token nobody issued is not an error (§2.2).

    A REVOCATION, under `store`'s carve-out: the grant is read unlocked, and
    its family ended by one UPDATE (`revoke_family`), the chain after it and
    nothing else -- neither the person's row nor the app's is taken."""
    now = _now()
    grant = (
        await verify_access_token(token, settings=app.state.settings)
        if token.count(".") == 2
        else None
    )
    refused: Problem | None = None
    async with oauth_session(org_id, app.state.app_engine) as session:
        client = await clients.find_client(session, org_id=org_id, client_id=client_id, now=now)
        if client is None:
            refused = UNKNOWN_CLIENT
        else:
            family = (
                await session.execute(
                    select(OAuthRefreshFamily)
                    .join(OAuthRefreshToken, OAuthRefreshToken.family_id == OAuthRefreshFamily.id)
                    .where(OAuthRefreshToken.token_hash == hash_secret(token))
                )
            ).scalar_one_or_none()
            if family is None and grant is not None and grant.org_id == org_id:
                family = await session.get(OAuthRefreshFamily, grant.family_id)
            if family is not None:
                if family.oauth_client_id != client.id:
                    refused = WRONG_CLIENT
                elif await revoke_family(session, family.id, "revoked_by_app", now):
                    await audit_service.record(
                        session,
                        org_id=org_id,
                        actor_type="system",
                        actor_id=None,
                        action="mcp.grant_revoked",
                        target_type="user",
                        target_id=str(family.user_id),
                        metadata={"app": client.client_id, "family": str(family.id)},
                    )
    if refused is not None:
        raise OAuthError(refused)
