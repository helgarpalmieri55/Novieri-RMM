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

REGISTRATION (Task 6). `parse_registration`, `register`, `purge_idle`,
`registration_response` and `register_new` are RFC 7591, the last one the
whole registration: the switch read, the purge, then the ceiling counted and
the row written in one transaction under the organization's registration
lock (`lock_registrations`), so the ceiling holds when two registrations race
for its last place. `resolve_client` is the authorization request's lookup,
a registered app or a client metadata document, read and stored (CIMD-03
§6).

THE PURGE'S SHAPE (`guards.lock_user_row`'s lock map). `purge_idle` deletes
ONE IDLE APP PER TRANSACTION, by `DELETE ... WHERE id = :id AND last_used_at
<= now - CLIENT_IDLE` (`idle_at`, `is_idle` in SQL): the idle condition is
inside the statement, so a use that commits while the DELETE waits on the
app's hold spares the app. One app per transaction, because the purge's
cascade takes a person's families, and a revocation across a person's apps
(one UPDATE, Task 7's `grants.revoke_all_for_user`) locks those in no app's
order: a purge holding one app's cascade while waiting in another's could
meet it in a cycle. That revocation can close one within a single app too,
when the person holds two of its grants, so each app's DELETE waits at most
`PURGE_LOCK_WAIT` and then gives way, leaving the app for later
(`_purge_one`). The same step removes a document's idle row before a new
copy is stored (`resolve_client`).

A DOCUMENT'S ROW IS REFRESHED UNDER A HOLD (`store`'s LOCK ORDER, step 3):
an expired copy is rewritten only after `hold` has re-read it, and a use
found by `resolve_client` is written only to a held row. An idle copy is not
revived: it goes as the purge would take it, grants and all (plan D10), and
the document is stored as a new app.
"""

import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from uuid_extensions import uuid7

from app.core.auth.oauth import cimd, policy
from app.core.auth.oauth.errors import (
    BAD_GRANT_TYPES,
    BAD_NAME,
    BAD_REDIRECT,
    BAD_RESPONSE_TYPES,
    NO_REDIRECTS,
    NOT_JSON,
    REGISTRATION_CLOSED,
    REGISTRATIONS_FULL,
    UNAVAILABLE,
    OAuthError,
)
from app.core.auth.oauth.policy import (
    CLIENT_IDLE,
    GRANT_TYPES,
    MAX_REDIRECT_URIS,
    MAX_REGISTRATIONS,
    SCOPE,
    display_name,
    issuer,
    registration_open,
)
from app.core.auth.oauth.redirects import RedirectRejected, check_registered
from app.core.auth.oauth.store import deployment_org, oauth_session
from app.models import Organization
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
    same condition, inside its DELETE (`idle_at`)."""
    return row.last_used_at <= now - CLIENT_IDLE


def idle_at(now: datetime) -> ColumnElement[bool]:
    """`is_idle` in SQL: the condition a DELETE of an idle app carries, so the
    row is judged as it is when the statement reaches it -- after a use that
    committed while the DELETE waited -- and not as it was read."""
    return OAuthClient.last_used_at <= now - CLIENT_IDLE


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


# --------------------------------------------------------------------------- #
# Registration (RFC 7591; plan Task 6)
# --------------------------------------------------------------------------- #

#: How many idle apps one registration removes (plan D10).
PURGE_BATCH = 100
#: The longest one app's purge waits on a lock before it gives way
#: (`_purge_one`): under Postgres' default `deadlock_timeout` of 1 s.
PURGE_LOCK_WAIT = "500ms"
#: lock_not_available (the wait above ran out) and deadlock_detected.
_GAVE_WAY = frozenset({"55P03", "40P01"})
#: Registrations one network may make per hour (`policy`), read here by the
#: route so that a test lowers it in one place.
REGISTRATIONS_PER_HOUR = policy.REGISTRATIONS_PER_HOUR


@dataclass(frozen=True)
class Registration:
    client_name: str | None
    redirect_uris: tuple[str, ...]


def parse_registration(body: object) -> Registration:
    """A registration request this server can honour (plan D4, D9), or the
    RFC 7591 §3.2.2 error. What it does not understand is ignored (§2)."""
    if not isinstance(body, dict):
        raise OAuthError(NOT_JSON)
    uris = body.get("redirect_uris")
    if not isinstance(uris, list) or not 1 <= len(uris) <= MAX_REDIRECT_URIS:
        raise OAuthError(NO_REDIRECTS)
    try:
        checked = tuple(dict.fromkeys(check_registered(uri) for uri in uris))
    except RedirectRejected as exc:
        raise OAuthError(BAD_REDIRECT) from exc
    grants = body.get("grant_types", list(GRANT_TYPES))
    if (
        not isinstance(grants, list)
        or "authorization_code" not in grants
        or not set(grants) <= set(GRANT_TYPES)
    ):
        raise OAuthError(BAD_GRANT_TYPES)
    responses = body.get("response_types", ["code"])
    if not isinstance(responses, list) or set(responses) != {"code"}:
        raise OAuthError(BAD_RESPONSE_TYPES)
    name = body.get("client_name")
    if name is not None and not isinstance(name, str):
        raise OAuthError(BAD_NAME)
    return Registration(client_name=display_name(name) if name else None, redirect_uris=checked)


async def mcp_switched_on(session: AsyncSession, org_id: UUID) -> bool:
    """The organization's MCP switch, as `/mcp` reads it (`is True`: a
    malformed value is off)."""
    settings = (
        await session.execute(select(Organization.settings).where(Organization.id == org_id))
    ).scalar_one_or_none()
    return (settings or {}).get("mcp_enabled") is True


async def registration_offered(app: Any) -> bool:
    """Dynamic registration is open: the operator's opt-in, a deployment
    organization, and MCP switched on there (plan Task 6, decision 1)."""
    if not registration_open(app.state.settings):
        return False
    org_id = await deployment_org(app)
    if org_id is None:
        return False
    async with oauth_session(org_id, app.state.app_engine) as session:
        return await mcp_switched_on(session, org_id)


async def purge_idle(engine: AsyncEngine, *, org_id: UUID, now: datetime) -> int:
    """Remove up to `PURGE_BATCH` apps of `org_id` unused for `CLIENT_IDLE`,
    registered or known by their document, with their grants and codes (ON
    DELETE CASCADE; plan D10). How many went.

    ONE APP PER TRANSACTION, THE IDLE CONDITION INSIDE ITS DELETE
    (`guards.lock_user_row`'s lock map; module docstring). The candidates are
    read first, holding nothing; each is then deleted in a transaction of
    its own by `DELETE ... WHERE id = :id AND last_used_at <= now -
    CLIENT_IDLE` (`idle_at`). So a token request holding the app (`hold`)
    keeps that DELETE waiting, and if its use (`touch`) commits first the
    DELETE's condition fails on the row as it now is: the app survives.
    While a DELETE waits it holds no other app's cascade, so a revocation
    across a person's apps cannot wait on this purge while this purge waits
    on it. And it waits at most `PURGE_LOCK_WAIT` (`_purge_one`), for the
    cycle one app can still close with a revocation of two of its grants.
    Nothing follows the cascade, not even the chain: a registration and its
    purge are not audited (B3-22)."""
    async with oauth_session(org_id, engine) as session:
        candidates = list(
            (
                await session.execute(
                    select(OAuthClient.id)
                    .where(OAuthClient.org_id == org_id, idle_at(now))
                    .order_by(OAuthClient.last_used_at, OAuthClient.id)
                    .limit(PURGE_BATCH)
                )
            ).scalars()
        )
    removed = 0
    for app_id in candidates:
        removed += await _purge_one(engine, org_id=org_id, which=OAuthClient.id == app_id, now=now)
    return removed


async def _purge_one(
    engine: AsyncEngine, *, org_id: UUID, which: ColumnElement[bool], now: datetime
) -> int:
    """The one app `which` names, if idle by `now`, gone with its grants, in
    a transaction of its own: 1, or 0 when it was not idle or gave way.

    IT GIVES WAY RATHER THAN WAIT LONG. Its DELETE waits on a token request
    that holds the app (`hold`) -- whose use, if it commits first, spares the
    app -- and in its cascade on a revocation that took one of the app's
    families first (`store`'s carve-out). With one person's two families on
    the app, that revocation's one UPDATE may take the second and then reach
    the first, which the cascade already holds: a cycle. So the DELETE waits
    at most `PURGE_LOCK_WAIT`, below Postgres' default `deadlock_timeout`
    (1 s). In that cycle its wait began first, so it times out before either
    side's deadlock check runs; the person's act completes, and the app is
    left to the next purge or the daily sweep. A 40P01 the purge meets
    anyway (a lower `deadlock_timeout`) is given way to the same."""
    try:
        async with oauth_session(org_id, engine) as session:
            await session.execute(
                text("SELECT set_config('lock_timeout', :wait, true)"), {"wait": PURGE_LOCK_WAIT}
            )
            gone = await session.execute(
                delete(OAuthClient)
                .where(OAuthClient.org_id == org_id, which, idle_at(now))
                .returning(OAuthClient.id)
            )
            return len(gone.all())
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) not in _GAVE_WAY:
            raise
        return 0


async def lock_registrations(session: AsyncSession, org_id: UUID) -> None:
    """The organization's registration lock, held to the end of the caller's
    transaction: a registration counts the ceiling and writes its row under
    it, so two racing for the last place take it once (plan Task 6, decision
    2: the limits limit). Taken first in a transaction that then only reads
    and inserts a new row, and by nothing else: it waits on no lock while
    another waits on it."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"oauth_registrations:{org_id}"},
    )


async def register(
    session: AsyncSession, *, org_id: UUID, registration: Registration, now: datetime
) -> OAuthClient:
    row = OAuthClient(
        org_id=org_id,
        # 24 random bytes, 32 base64url characters: no two apps share one
        # (RFC 7591 §3.2.1: "SHOULD NOT be currently valid for any other
        # registered client"), and none is an https URL, so a registered id
        # never reads as a client metadata document.
        client_id="itms_" + secrets.token_urlsafe(24),
        kind="registered",
        client_name=registration.client_name,
        redirect_uris=list(registration.redirect_uris),
        created_at=now,
        last_used_at=now,
        metadata_expires_at=None,
    )
    session.add(row)
    await session.flush()
    return row


def registration_response(row: OAuthClient) -> dict[str, Any]:
    """RFC 7591 §3.2.1: the client id and every registered value, including
    the ones this server replaced (plan D4)."""
    answer: dict[str, Any] = {
        "client_id": row.client_id,
        "client_id_issued_at": int(row.created_at.timestamp()),
        "redirect_uris": list(row.redirect_uris),
        "grant_types": list(GRANT_TYPES),
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": SCOPE,
    }
    if row.client_name:
        answer["client_name"] = row.client_name
    return answer


async def register_new(
    app: Any, *, org_id: UUID, registration: Registration, now: datetime
) -> dict[str, Any]:
    """One registration (plan Task 6, decisions 2 and 3); RFC 7591's answer.

    The switch is read first, so a closed one removes nothing. Then up to
    `PURGE_BATCH` idle apps go (`purge_idle`), each in its own transaction --
    the lock map's shape, which is why not inside the registration's. Then,
    in one transaction under `lock_registrations`, the switch is read again,
    the `MAX_REGISTRATIONS` ceiling counted and the row written. The ceiling
    counts live registrations only, so the purge running first and apart
    changes no count: it keeps the table from holding what nobody can use."""
    engine = app.state.app_engine
    async with oauth_session(org_id, engine) as session:
        if not await mcp_switched_on(session, org_id):
            raise OAuthError(REGISTRATION_CLOSED)
    await purge_idle(engine, org_id=org_id, now=now)
    async with oauth_session(org_id, engine) as session:
        await lock_registrations(session, org_id)
        if not await mcp_switched_on(session, org_id):
            raise OAuthError(REGISTRATION_CLOSED)
        live = (
            await session.execute(
                select(func.count())
                .select_from(OAuthClient)
                .where(
                    OAuthClient.org_id == org_id,
                    OAuthClient.kind == "registered",
                    OAuthClient.last_used_at > now - CLIENT_IDLE,
                )
            )
        ).scalar_one()
        if live >= MAX_REGISTRATIONS:
            raise OAuthError(REGISTRATIONS_FULL)
        row = await register(session, org_id=org_id, registration=registration, now=now)
        return registration_response(row)


# --------------------------------------------------------------------------- #
# The authorization request's client: registered, or a metadata document
# --------------------------------------------------------------------------- #

Fetch = Callable[[str], Awaitable[cimd.ClientDocument]]


def _fresh(row: OAuthClient, now: datetime) -> bool:
    """Usable as stored: a registration, or a document whose copy has not
    expired (CIMD-03 §6.2)."""
    return row.kind == "registered" or (
        row.metadata_expires_at is not None and row.metadata_expires_at > now
    )


async def resolve_client(
    app: Any,
    *,
    org_id: UUID,
    client_id: str,
    now: datetime,
    fetch: Fetch | None = None,
    record_use: bool = True,
) -> ClientView | None:
    """The app an authorization request names, or None. A registered app is
    looked up; a client metadata document is read whenever MCP is on (C2: the
    caller, the consent, has checked the switch) -- outside any transaction,
    so the network is not held under a database connection -- when the
    stored copy has expired, and stored for `document.cache_for` (CIMD-03
    §6.2). A transient failure keeps a stored copy (§6.1); a refusal
    (`cimd.DocumentRefused`) is raised and never stored, and does not delete
    the stored copy; a document on this server's own origin is never read
    (§10.1).

    A use is written only to a row `hold` has re-read (`store`'s LOCK ORDER,
    step 3): a purge that took the row meanwhile leaves None, not a stale-row
    500. A document's copy is written by `_store_document`, after a hold too.

    `fetch` defaults to `cimd.fetch_document`, looked up at call time so a
    test can replace it on the module. `record_use=False` WRITES NOTHING, and
    so reads no document: what a person who may not connect AI apps here
    asks about neither keeps an idle app alive nor downloads anything
    (re-review f3-2, M-3; A3-31). A stored copy still answers, expired or
    not; without one the document is `unavailable`, as if it could not be
    read now -- what the consent's own `fetch` for that person answers
    (Task 8)."""
    engine = app.state.app_engine
    async with oauth_session(org_id, engine) as session:
        row = await find_client(session, org_id=org_id, client_id=client_id, now=now)
        if row is not None and record_use:
            row = await hold(session, org_id=org_id, app_id=row.id, now=now)
        if row is not None and _fresh(row, now):
            if record_use:
                touch(row, now)
            return view(row)
        stored = view(row) if row is not None and row.kind == "metadata_document" else None
    if not cimd.is_document_client_id(client_id):
        return None
    iss = issuer(app.state.settings)
    if iss is None:
        raise OAuthError(UNAVAILABLE)
    if cimd.on_origin(client_id, iss):
        raise cimd.DocumentRefused("own_origin")  # i18n-exempt: a reason code
    if not record_use:
        if stored is not None:
            return stored
        raise cimd.DocumentRefused("unavailable")  # i18n-exempt: a reason code
    try:
        document = await (fetch or cimd.fetch_document)(client_id)
    except cimd.DocumentRefused as refused:
        if refused.transient and stored is not None:
            return stored
        raise
    # A row of this client idle by `now` goes first, as the purge takes one
    # -- its grants with it (plan D10) -- so reading the document again never
    # revives an idle app, nor the grants under it.
    await _purge_one(engine, org_id=org_id, which=OAuthClient.client_id == client_id, now=now)
    async with oauth_session(org_id, engine) as session:
        return await _store_document(session, org_id=org_id, document=document, now=now)


async def _store_document(
    session: AsyncSession, *, org_id: UUID, document: cimd.ClientDocument, now: datetime
) -> ClientView:
    """`document`'s copy, written in the caller's transaction:

    1. a row is inserted if none is there (ON CONFLICT DO NOTHING: another
       consent may be storing the same document);
    2. otherwise the row there is held (`hold`) and only then refreshed. A
       purge that took it between 1 and 2 leaves None, and 1 runs again. So
       does an idle row the caller's purge gave way on: `hold` refuses it,
       and the document is "unavailable" just now, never stored over it.
    """
    expires = now + document.cache_for
    for _ in range(2):
        new_id = (
            await session.execute(
                pg_insert(OAuthClient)
                .values(
                    id=uuid7(),
                    org_id=org_id,
                    client_id=document.client_id,
                    kind="metadata_document",
                    client_name=document.client_name,
                    redirect_uris=list(document.redirect_uris),
                    created_at=now,
                    last_used_at=now,
                    metadata_expires_at=expires,
                )
                .on_conflict_do_nothing(constraint="uq_oauth_clients_org_client")
                .returning(OAuthClient.id)
            )
        ).scalar_one_or_none()
        if new_id is not None:
            return ClientView(
                id=new_id,
                client_id=document.client_id,
                kind="metadata_document",
                client_name=document.client_name,
                redirect_uris=document.redirect_uris,
            )
        there = (
            await session.execute(
                select(OAuthClient.id).where(
                    OAuthClient.org_id == org_id, OAuthClient.client_id == document.client_id
                )
            )
        ).scalar_one_or_none()
        row = None if there is None else await hold(session, org_id=org_id, app_id=there, now=now)
        if row is not None:
            row.client_name = document.client_name
            row.redirect_uris = list(document.redirect_uris)
            row.metadata_expires_at = expires
            touch(row, now)
            await session.flush()
            return view(row)
    # Twice a row there and none to hold: idle (the purge gave way on it) or
    # taken between the conflict and the hold. Nothing to keep just now.
    raise cimd.DocumentRefused("unavailable")  # i18n-exempt: a reason code
