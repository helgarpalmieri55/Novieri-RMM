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
active -- as "system", the person as the target, the app whose grant ended
in `metadata.app`. A used code presented by ANOTHER app also names that one
(`metadata.presented_by`): the likeliest trace of a stolen code. NOT a
refresh, which continues a consent already audited
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
3. the code, or the token and its family, FOR UPDATE, re-read; THEN THE
   CLOCK, the database's (`_clock_after_the_locks`), and every condition
   checked again on what was re-read against that clock -- the arrival time
   serves only the lookups before the locks. Whether the person is active
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
and `revoke_app` (Task 10) end the unspent codes' expiry
(`end_unspent_codes`), then the live families, each table's rows LOCKED IN
ID ORDER first (`_lock_in_id_order`; Task 10 review, Important 2) and then
ended by id: two of them sharing rows take those rows in one order, codes
before families, whatever plan reads them. Their callers hold what they
hold -- an account act, the person's row; Settings
(`connections.revoke_app_access`), neither row.

A CODE AN ACCOUNT ACT ENDED is one whose `expires_at` the act set to its
own clock -- never its `used_at` (Task 10; operator decision D2, part c;
`end_unspent_codes`, which every revocation of a person's or an app's
grants runs first). The exchange answers it as any expired code: a plain
`invalid_grant`, with nothing spent, logged, audited or revoked. Only a
`used_at` already set is a reuse. The row lock alone does not stop an act
that leaves the person active (signing out everywhere, a new password, a
factor reset): the exchange waits for it and then runs. Ending the person's
unspent codes in the same act does: the exchange re-reads its code at 3,
after the act's commit, and compares it with a clock read after that
re-read, so any `expires_at` the act wrote up to its commit reads as
expired -- whether the exchange waited for the act or only arrived before
it (review of Task 7, fix round 1; `tests/test_mcp_grant_lifecycle.py`, the
race with signing out everywhere).

BULK STATEMENTS ON O TABLES (`update`) are invisible to
`app.core.global_rows`'s listener; every one here runs under `oauth_session`
or a caller's every-client session (the lifecycle acts, Task 10), where
writing a technician's global rows is allowed.
"""

import logging
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from sqlalchemy import func, select, update
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


async def _clock_after_the_locks(session: AsyncSession) -> datetime:
    """The time every check and write of `_exchange` and `_refresh` uses,
    READ AFTER THEIR LOCKS (the review of Task 7, fix round 1) and FROM THE
    DATABASE.

    After: a clock read on arrival is older than whatever an account act
    committed while this request waited for the person's row, or was on its
    way to it -- a code the act ended at its own clock (`expires_at =
    now()`, D2 part c) still looked live, and the exchange opened a grant
    after the act had ended the person's grants. Read here, it is later than
    the commit of any act this transaction waited for or now sees, so every
    `expires_at` such an act wrote, up to its commit, is in the past.

    From the database: the acts write that clock (`now()`,
    `clock_timestamp()`), and comparing it with the API process's would let
    a skew between the two hosts reopen the window by its size."""
    clock: datetime = (await session.execute(select(func.clock_timestamp()))).scalar_one()
    return clock


async def issue_code(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    oauth_client_id: UUID,
    redirect_uri: str,
    code_challenge: str,
    resource: str,
) -> str:
    """A one-time code for what the person just approved (Task 8).

    ITS TIMES ARE THE DATABASE'S (Task 10 review, Important 1): `created_at`
    is the column's own default and `expires_at` that same `now()` plus
    `CODE_TTL_S`, never the API host's clock. Everything that reads them
    reads the database's: the exchange after its locks
    (`_clock_after_the_locks`), and an act ending the person's codes
    (`end_unspent_codes`). From the API's clock, a host running Δ ahead put
    `created_at` Δ in the database's future; the act's floor (`created_at`
    + 1 µs, for `ck_oauth_codes_expiry`) then left the code live for Δ after
    the act, and an exchange inside it opened a grant
    (`tests/test_mcp_grant_lifecycle.py`, the clock ahead)."""
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
            expires_at=func.now() + timedelta(seconds=CODE_TTL_S),
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


#: Where an ended code's expiry goes: the database's clock, the one the
#: exchange compares it with after its locks (`_clock_after_the_locks`) --
#: but never at or before the code's `created_at`, which
#: `ck_oauth_codes_expiry` (`expires_at > created_at`) forbids. `created_at`
#: is the database's own `now()` at the approval (`issue_code`), which
#: committed before this statement could see the code, so the floor never
#: lifts the expiry past this clock: it only keeps the CHECK true.
_ENDED_AT = func.greatest(func.clock_timestamp(), OAuthCode.created_at + timedelta(microseconds=1))


async def _lock_in_id_order(
    session: AsyncSession, id_column: Any, conditions: Sequence[Any]
) -> list[UUID]:
    """Lock the rows `conditions` select, IN ID ORDER, and say which they are
    (Task 10 review, Important 2).

    `SELECT id … ORDER BY id FOR NO KEY UPDATE`: the row-locking step sits
    above the sort, so the rows are locked in id order whatever plan reads
    them -- an index whose ties hold a HOT chain's root slot, a sequential
    scan that meets the live version at its own slot, another predicate's
    index. Two revocations that share rows (an act on a person and a revoke
    of one of their apps) therefore take them in the same order and cannot
    close a cycle; a plain UPDATE takes them in its scan's order, which two
    plans need not share (`tests/test_mcp_grant_lifecycle.py`, the
    cross-order). NO KEY UPDATE, the lock the UPDATE would take: a refresh
    token or a code that refers to a grant takes KEY SHARE on it, which this
    does not block. A row that a writer this waited for has ended meanwhile
    is re-checked, found ended, and not returned."""
    locked = await session.execute(
        select(id_column).where(*conditions).order_by(id_column).with_for_update(key_share=True)
    )
    return list(locked.scalars())


async def end_unspent_codes(
    session: AsyncSession, *, user_id: UUID | None = None, oauth_client_id: UUID | None = None
) -> int:
    """End the unspent, still-live codes of a person, of an app, or of one
    person's for one app (operator decision D2, part c); how many.

    BY THEIR EXPIRY, NEVER BY `used_at`: a set `used_at` is what the
    exchange reads as a reuse (`mcp.code_reused`, an audit row, the grant it
    opened revoked). Ended this way the code is what any expired code is: a
    plain `invalid_grant`. An exchange waiting on the caller's locks re-reads
    the code after its commit and compares it with a clock read after that
    (`_clock_after_the_locks`), so whatever this writes up to the commit
    reads as expired. Spent and already expired codes are left as they are.

    LOCKED IN ID ORDER FIRST (`_lock_in_id_order`), then ended by id, the
    live condition checked again under the lock.

    The caller says whose: a person, an app, or both. With neither it would
    end every code the session can see, so it refuses."""
    if user_id is None and oauth_client_id is None:
        raise ValueError(  # i18n-exempt: programmer error, unhandled 500
            "name the person or the app whose codes end"
        )
    live = [OAuthCode.used_at.is_(None), OAuthCode.expires_at > func.clock_timestamp()]
    whose: list[Any] = []
    if user_id is not None:
        whose.append(OAuthCode.user_id == user_id)
    if oauth_client_id is not None:
        whose.append(OAuthCode.oauth_client_id == oauth_client_id)
    ids = await _lock_in_id_order(session, OAuthCode.id, [*whose, *live])
    if not ids:
        return 0
    ended = await session.execute(
        update(OAuthCode)
        .where(OAuthCode.id.in_(ids), *live)
        .values(expires_at=_ENDED_AT)
        .returning(OAuthCode.id)
    )
    return len(ended.all())


async def _end_live_families(
    session: AsyncSession, whose: Sequence[Any], *, reason: str, revoked_at: datetime
) -> int:
    """End the live families `whose` selects, locked in id order first
    (`_lock_in_id_order`); how many. LIVE: neither revoked nor past their idle
    limit, by the database's clock -- an expired grant is dead already and
    is neither marked nor counted, and an idle app's grants have all but
    always expired with it, which keeps this clear of a purge's cascade
    (`clients.purge_idle`). Checked again under the lock: a grant a refresh
    or another revocation ended while this waited is not counted."""
    live = [
        OAuthRefreshFamily.revoked_at.is_(None),
        OAuthRefreshFamily.expires_at > func.clock_timestamp(),
    ]
    ids = await _lock_in_id_order(session, OAuthRefreshFamily.id, [*whose, *live])
    if not ids:
        return 0
    ended = await session.execute(
        update(OAuthRefreshFamily)
        .where(OAuthRefreshFamily.id.in_(ids), *live)
        .values(revoked_at=revoked_at, revoked_reason=reason)
        .returning(OAuthRefreshFamily.id)
    )
    return len(ended.all())


async def revoke_all_for_user(session: AsyncSession, user_id: UUID, reason: str) -> int:
    """End every live grant of a person (the lifecycle acts, Task 10); how many.

    IN THIS ORDER, and nothing else (`store`'s carve-out): the person's
    unspent codes end first (`end_unspent_codes`; operator decision D2, part
    c), then their live families -- each table's rows locked in id order,
    then ended by id. The caller holds the person's row (every act does,
    first), so an exchange of one of those codes waits for it and then finds
    the code expired: an act that leaves the person active opens no way back
    for a code approved a moment before it."""
    await end_unspent_codes(session, user_id=user_id)
    return await _end_live_families(
        session, [OAuthRefreshFamily.user_id == user_id], reason=reason, revoked_at=_now()
    )


async def revoke_app(
    session: AsyncSession,
    *,
    oauth_client_id: UUID,
    user_id: UUID | None,
    reason: str,
    now: datetime,
) -> int:
    """End an app's live grants: one person's, or (`user_id` None) everyone's
    in the session's organization (Task 10); how many.

    The same steps as `revoke_all_for_user`, for the app: its unspent codes
    end first, then its live families, in SEPARATE statements, each table's
    rows locked in id order. The caller holds no person's row and no app row
    (`store`'s carve-out), so an exchange of the app's code may be in flight.
    Under READ COMMITTED each statement reads what committed before it
    began. An exchange that holds its code when the codes' lock reaches it
    is waited for, and the family it opened has committed before the
    families' lock begins, which then reads it and ends it; one that has not
    locked its code yet waits on that lock, and then finds the code
    expired."""
    await end_unspent_codes(session, user_id=user_id, oauth_client_id=oauth_client_id)
    whose = [OAuthRefreshFamily.oauth_client_id == oauth_client_id]
    if user_id is not None:
        whose.append(OAuthRefreshFamily.user_id == user_id)
    return await _end_live_families(session, whose, reason=reason, revoked_at=now)


async def family_is_live(
    session: AsyncSession, *, family_id: UUID, user_id: UUID, client_id: str
) -> bool:
    """The grant an access token names still stands, for that person and that
    app (the MCP door, Task 9; plan D6). A READ: no lock, no write.

    ITS IDLE LIMIT ON THE DATABASE'S CLOCK (Task 10 review, Minor 3), the
    clock a refresh writes `expires_at` from (`_clock_after_the_locks`) and
    an account act reads: compared with the API host's, a host running
    behind admitted a grant past its limit for as long as it ran behind."""
    found = (
        await session.execute(
            select(OAuthRefreshFamily.id)
            .join(OAuthClient, OAuthClient.id == OAuthRefreshFamily.oauth_client_id)
            .where(
                OAuthRefreshFamily.id == family_id,
                OAuthRefreshFamily.user_id == user_id,
                OAuthClient.client_id == client_id,
                OAuthRefreshFamily.revoked_at.is_(None),
                OAuthRefreshFamily.expires_at > func.clock_timestamp(),
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
    arrived: datetime,
) -> IssuedTokens | Problem:
    client = await clients.find_client(session, org_id=org_id, client_id=client_id, now=arrived)
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
    app = await clients.hold(session, org_id=org_id, app_id=seen.oauth_client_id, now=arrived)
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
    now = await _clock_after_the_locks(session)
    if row is None or row.user_id != seen.user_id or row.oauth_client_id != app.id:
        return BAD_CODE
    if row.used_at is not None:
        if row.family_id is not None and await revoke_family(
            session, row.family_id, "code_reused", now
        ):
            reused: dict[str, str] = {"app": app.client_id, "family": str(row.family_id)}
            if client.id != app.id:
                # Another app holds the code: the likeliest trace of a theft.
                reused["presented_by"] = client.client_id
            await audit_service.record(  # 7. the chain, last
                session,
                org_id=org_id,
                actor_type="system",
                actor_id=None,
                action="mcp.code_reused",
                target_type="user",
                target_id=str(row.user_id),
                metadata=reused,
            )
        return BAD_CODE
    if row.expires_at <= now:
        # Expired, or ended by an account act (`expires_at` set to the act's
        # clock; D2, part c) at any moment up to its commit, which this
        # clock follows: a plain refusal that spends, logs, audits and
        # revokes nothing.
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
            arrived=_now(),
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
    arrived: datetime,
) -> IssuedTokens | Problem:
    client = await clients.find_client(session, org_id=org_id, client_id=client_id, now=arrived)
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
    app = await clients.hold(session, org_id=org_id, app_id=client.id, now=arrived)  # 2. the app
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
    now = await _clock_after_the_locks(session)
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
            arrived=_now(),
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
