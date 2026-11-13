"""The AI apps an organization knows (design §5.4, §5.5).

`find_client` is the one lookup: an app this organization has, used in the
last `CLIENT_IDLE` (§5.4: "Un registro sin uso en 30 días caduca"). An idle
one is not found, whatever its row says, and the next registration removes it
(`purge_idle`, Task 6; plan D10). `touch` records a use -- an authorization,
a token request -- AT MOST ONCE AN HOUR (`TOUCH_EVERY`, A3-37): the idle limit
is counted in days, and one app row is shared by every person of the
organization who connects it (Claude's metadata document), so writing it on
every token request would have each of them wait on the same row lock.

THE APP ROW'S PLACE IN THE LOCK ORDER (`store`'s LOCK ORDER, steps 3 and 5).
An idle app's purge deletes the app row and then, by cascade, every grant
row under it. So a transaction that writes a person's codes, families or
refresh tokens takes the app row after the person's and before any grant
row: `hold`, FOR KEY SHARE, which writes nothing. A DELETE must wait for it;
another person's `hold` and a use being written do not. The use itself,
`touch`, comes last, after the access token is signed (plan Task 7, decision
9): from its write to the commit, it is the one thing another request's use
of the same app waits for, which is why it is written at most once an hour
and never across the signature (A3-37). `find_client` is a read that holds
nothing.

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
    grant rows (`store`'s LOCK ORDER, step 3): called after the person's
    `users` row is locked and before any grant row is written or locked.

    Re-reads the row FOR KEY SHARE (`populate_existing`), so a purge that
    committed since the app was looked up leaves None, not a stale row whose
    flush would fail; None too for an idle app or another organization's.
    WRITES NOTHING: the use is the caller's `touch`, after the access token
    is signed (step 5), so a refused request records none and no signature
    is made while holding the shared row's write lock. A purge that meets
    the hold waits on the app row; if the holder's use commits first, the
    purge's own idle condition no longer holds and the app survives."""
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
    return row


def touch(row: OAuthClient, now: datetime) -> None:
    """A use of the app: written when the last one recorded is `TOUCH_EVERY`
    old, so most token requests write nothing to this row.

    The write (FOR NO KEY UPDATE on the shared row) is sent at the session's
    next flush and held to the commit, so it comes LAST (`store`'s LOCK
    ORDER, step 5; plan Task 7, decision 9): after the grant rows and after
    the access token is signed, never across the OpenBao call -- while it is
    held, another request's use of the app waits for this commit. Flush it
    before the audit chain when the transaction takes one: the chain's
    `text()` execute does not autoflush, and a use written after the chain
    would hold the chain while waiting for another request's use, which
    waits for the chain.

    In a transaction that holds the row FOR KEY SHARE (`hold`), this write is
    an upgrade that does NOT wait on an idle app's purge queued for the row:
    PostgreSQL skips the tuple lock for a transaction that already holds the
    row, so the upgrade does not queue behind the purge waiting there (the
    re-review's probe P6, which `tests/test_oauth_schema.py` repeats in
    `test_a_late_use_does_not_wait_on_a_queued_purge_and_spares_the_app`).
    The purge waits for this commit, and its re-check of the idle condition
    then fails. Where a transaction writes no grant row (a registration),
    call this directly."""
    if row.last_used_at <= now - TOUCH_EVERY:
        row.last_used_at = now
