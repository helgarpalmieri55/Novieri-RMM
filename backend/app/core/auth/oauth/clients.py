"""The AI apps an organization knows (design §5.4, §5.5).

`find_client` is the one lookup: an app this organization has, used in the
last `CLIENT_IDLE` (§5.4: "Un registro sin uso en 30 días caduca"). An idle
one is not found, whatever its row says, and the next registration removes it
(`purge_idle`, Task 6; plan D10). `touch` records a use -- an authorization,
a token request -- AT MOST ONCE AN HOUR (`TOUCH_EVERY`, A3-37): the idle limit
is counted in days, and one app row is shared by every person of the
organization who connects it (Claude's metadata document), so writing it on
every token request would have each of them wait on the same row lock.

`ClientView` is what leaves a session: a plain copy, so nothing holds an ORM
object across transactions.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.oauth.policy import CLIENT_IDLE
from app.models.oauth import OAuthClient

#: How stale `last_used_at` may be before a use writes it again (A3-37).
TOUCH_EVERY = timedelta(hours=1)


@dataclass(frozen=True)
class ClientView:
    id: UUID
    client_id: str
    kind: str
    client_name: str | None
    redirect_uris: tuple[str, ...]


def view(row: OAuthClient) -> ClientView:
    return ClientView(
        id=row.id,
        client_id=row.client_id,
        kind=row.kind,
        client_name=row.client_name,
        redirect_uris=tuple(row.redirect_uris),
    )


async def find_client(
    session: AsyncSession, *, org_id: UUID, client_id: str, now: datetime
) -> OAuthClient | None:
    row = (
        await session.execute(
            select(OAuthClient).where(
                OAuthClient.org_id == org_id, OAuthClient.client_id == client_id
            )
        )
    ).scalar_one_or_none()
    if row is None or row.last_used_at <= now - CLIENT_IDLE:
        return None
    return row


def touch(row: OAuthClient, now: datetime) -> None:
    """A use of the app: written when the last one recorded is `TOUCH_EVERY`
    old, so most token requests write nothing to this row.

    The write is an UPDATE of a row everyone who connects the app shares,
    sent at the session's next flush. A transaction that also writes a
    person's rows calls this AFTER it holds that person's `users` row
    (`store`'s LOCK ORDER): flushed earlier, the app row would be held while
    the transaction waits on the person's, and one that holds the person's
    and then touches the same app would close the cycle."""
    if row.last_used_at <= now - TOUCH_EVERY:
        row.last_used_at = now
