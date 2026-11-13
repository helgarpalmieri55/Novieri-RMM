"""The AI apps an organization knows (design §5.4, §5.5).

`find_client` is the one lookup: an app this organization has, used in the
last `CLIENT_IDLE` (§5.4: "Un registro sin uso en 30 días caduca"). An idle
one is not found, whatever its row says, and the next registration removes it
(`purge_idle`, Task 6; plan D10). `touch` records a use -- an authorization,
a token request -- AT MOST ONCE AN HOUR (`TOUCH_EVERY`, A3-37): the idle limit
is counted in days, and one app row is shared by every person of the
organization who connects it (Claude's metadata document), so writing it on
every token request would have each of them wait on the same row lock.

THE APP ROW'S PLACE IN THE LOCK ORDER (`store`'s LOCK ORDER, step 3). An
idle app's purge deletes the app row and then, by cascade, every grant row
under it. So a transaction that writes a person's codes, families or refresh
tokens takes the app row after the person's and before any grant row:
`hold`. Holding it is FOR KEY SHARE, the one lock a DELETE must wait for and
that no other user of the app waits for -- another person's token request
holds it too, and a use being written (FOR NO KEY UPDATE) passes it -- so
A3-37 still holds. `find_client` is a read that holds nothing.

THE IDLE LIMIT RUNS FROM THE LAST USE RECORDED, which `TOUCH_EVERY` lets lag
the last real use by up to an hour. An app, and by cascade its grants, can so
be judged idle up to an hour before thirty days without use. Kept at
`CLIENT_IDLE` exactly, not `CLIENT_IDLE + TOUCH_EVERY`: the plan's own test
pins that threshold (in `tests/test_oauth_schema.py`,
`test_a_registration_unused_for_30_days_is_not_found`: thirty days and a
minute is not found), the purge must judge by the same rule as the lookup
(`is_idle`), and the error falls on the side of access ending early, never
late.

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


def is_idle(row: OAuthClient, now: datetime) -> bool:
    """Unused for `CLIENT_IDLE`, by the last use recorded (module docstring:
    up to `TOUCH_EVERY` behind the last real one). The purge deletes by this
    same condition, inside its DELETE."""
    return row.last_used_at <= now - CLIENT_IDLE


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
    if row is None or is_idle(row, now):
        return None
    return row


async def hold(
    session: AsyncSession, *, org_id: UUID, app_id: UUID, now: datetime
) -> OAuthClient | None:
    """The app row, held for a transaction that goes on to write a person's
    grant rows (module docstring): called after the person's `users` row is
    locked and before any grant row is written or locked.

    Re-reads the row FOR KEY SHARE (`populate_existing`), so a purge that
    committed since the app was looked up leaves None, not a stale row whose
    flush would fail; None too for an idle app or another organization's.
    Then records the use (`touch`) and FLUSHES it, so a use that is due is
    written here, at this place in the order, not at the commit. A purge
    that meets the hold waits on the app row; if the use committed first,
    the purge's own idle condition no longer holds and the app survives."""
    row = (
        await session.execute(
            select(OAuthClient)
            .where(OAuthClient.id == app_id, OAuthClient.org_id == org_id)
            .with_for_update(read=True, key_share=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None or is_idle(row, now):
        return None
    touch(row, now)
    await session.flush()
    return row


def touch(row: OAuthClient, now: datetime) -> None:
    """A use of the app: written when the last one recorded is `TOUCH_EVERY`
    old, so most token requests write nothing to this row.

    The write is sent at the session's next flush. Where a transaction writes
    no grant row (a registration, a metadata document's refresh), call this
    directly. Where it writes a person's grant rows, call `hold`, which takes
    the app row in its place in the lock order and flushes this there. A use
    flushed after a grant row would wait for the app row while holding the
    grant row, and a purge holding the app row waits for that grant row in
    its cascade: a deadlock (40P01)."""
    if row.last_used_at <= now - TOUCH_EVERY:
        row.last_used_at = now
