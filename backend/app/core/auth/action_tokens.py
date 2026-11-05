"""One-time links: reset a password, accept an invitation, recover an account
(design §2.8, §7).

THE VALUE CARRIES ITS ORGANIZATION (plan decision 2): `<org id hex>.<32
random bytes, base64url>`. A link is followed before anybody is signed in, and
the only reads allowed before that are migration 0245's two functions, neither
of which finds a token. So the organization is taken from the token and the
row is found, under RLS, by the SHA-256 of the whole value. Changing the id
buys nothing: without the exact hash there is no row in that organization.

ONLY THE HASH IS STORED (Global Constraints). The value exists in the mail, in
the link shown once when there is no mail (Task 8), and in the browser's
address bar after `#` -- never in the path, so no web server logs it (plan
decision 3).

A NEW LINK REPLACES THE OLD ONES of the same purpose: `issue` marks them used.
"""

import hashlib
import secrets
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import AuthActionToken

INVITE = "invite"
RESET = "reset"
RECOVER = "recover"

INVITE_TTL = timedelta(hours=72)
RESET_TTL = timedelta(minutes=30)
RECOVER_TTL = timedelta(minutes=15)

_SEPARATOR = "."


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def org_of(token: str) -> UUID | None:
    head, separator, tail = token.partition(_SEPARATOR)
    if not separator or not tail or len(head) != 32:
        return None
    try:
        return UUID(hex=head)
    except ValueError:
        return None


def link(console_url: str, page: str, token: str) -> str:
    """`https://console/<page>#<token>`: the fragment never reaches a server."""
    return f"{console_url.rstrip('/')}/{page}#{token}"


async def revoke_open(session: AsyncSession, *, user_id: UUID, purpose: str) -> None:
    await session.execute(
        update(AuthActionToken)
        .where(
            AuthActionToken.user_id == user_id,
            AuthActionToken.purpose == purpose,
            AuthActionToken.used_at.is_(None),
        )
        .values(used_at=datetime.now(UTC))
    )


async def issue(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    purpose: str,
    ttl: timedelta,
    created_by: UUID | None,
) -> str:
    await revoke_open(session, user_id=user_id, purpose=purpose)
    token = f"{org_id.hex}{_SEPARATOR}{secrets.token_urlsafe(32)}"
    now = datetime.now(UTC)
    session.add(
        AuthActionToken(
            org_id=org_id,
            user_id=user_id,
            purpose=purpose,
            token_hash=_digest(token),
            created_at=now,
            expires_at=now + ttl,
            created_by=created_by,
        )
    )
    await session.flush()
    return token


def _live(token: str, purposes: Sequence[str]) -> Select[tuple[AuthActionToken]]:
    return select(AuthActionToken).where(
        AuthActionToken.token_hash == _digest(token),
        AuthActionToken.purpose.in_(list(purposes)),
        AuthActionToken.used_at.is_(None),
        AuthActionToken.expires_at > datetime.now(UTC),
    )


async def peek(
    session: AsyncSession, *, token: str, purposes: Sequence[str]
) -> AuthActionToken | None:
    """The live row, left unused."""
    return (await session.execute(_live(token, purposes))).scalar_one_or_none()


async def consume(
    session: AsyncSession, *, token: str, purposes: Sequence[str]
) -> AuthActionToken | None:
    """The live row, marked used under a row lock: two requests with one link
    cannot both get it. The caller's transaction decides: an exception after
    this rolls the use back, so a refused new password leaves the link usable."""
    row = (await session.execute(_live(token, purposes).with_for_update())).scalar_one_or_none()
    if row is not None:
        row.used_at = datetime.now(UTC)
        await session.flush()
    return row
