"""Email-intake admin API (docs/modules/email-intake/spec.md §10).

Seven route registrations under `/api/v1/email-intake` (§10's table collapses
GET/PATCH/DELETE on `{mailbox_id}` into one row). `register(app)` mounts this
router under `/api/v1`; this module dispatches no agent job and registers no
job type -- like `service_desk`/`notification`/`portal`, it is control-plane
only (spec §1).

**RBAC (invariant #6, spec §10).** Every route depends on
`require_permission` -- reads take `email_intake.read` directly, every
mutation AND the connection test take `email_intake.admin` through
`require_permission_on_every_client`, which also requires an every-client
scope (MSP clients spec §2.2: this is global configuration). It is
high-privilege by design: it sets which mailbox the platform authenticates to
and which category inbound tickets land in. Completeness is derived from the
router by `app.core.rbac.route_permission`
(`tests/test_email_intake_api.py`), not from a hand-maintained list a later
route addition could silently miss. `org_id`/`actor_id` always come from
`AuthContext`, never from the body or the path (invariant #5).

**ROUTE ORDERING** (the Phase-11/12 lesson, carried through the ticket core
and the notification module): literal segments before `{id}` parameters --
the `mailboxes` collection first, then the by-id routes, then the suffixed
`/test` sub-action, then `messages`.

**ERROR TRANSLATION HAPPENS OUTSIDE THE `org_session` BLOCK.** Every handler
here is shaped

    try:
        async with org_session(...) as session:
            ...
    except MailboxValidationError as exc:
        raise _http_error(exc) from None

rather than catching inside the block as the notification module does.
`org_session` wraps `session.begin()`, so the transaction commits when the
block exits NORMALLY, and rolls back when ANY exception leaves it.

BE PRECISE ABOUT WHAT THAT BUYS, because the obvious claim is wrong. Moving
these `except` clauses inside the block would NOT orphan anything on its own:
`_http_error` re-RAISES, an `HTTPException` still leaves the block, and the
transaction still rolls back. What actually mints an ownerless `Credential`
is a handler that catches and then lets the block exit NORMALLY -- catch and
return a value, catch and `pass`, catch and stash the error to raise after
the `async with`. This shape's value is that it makes that mistake
STRUCTURALLY IMPOSSIBLE rather than merely unwritten: with the `except`
outside, there is no point at which a caught error can reach the end of the
block. (Verified by mutation: `except` moved inside with the `raise` kept
still passes; the same mutation that also exits the block normally commits an
ownerless credential and fails.)

The concrete stake is `create_mailbox`: it mints and flushes a `Credential`
before the mailbox INSERT that may lose the `uq_email_mailbox_org_address`
race, and an ownerless envelope-encrypted IMAP password is unrevocable sprawl
on a table whose whole purpose is bounded credential lifetime.
`create_mailbox` drops that credential itself on its own losing path; this
shape is the second belt, and it is the one that holds for any future service
function that lacks the first.

**NOT-MINE IS 404, NEVER 403.** `MailboxNotFoundError` -- raised for a
mailbox that is absent OR belongs to another org, indistinguishably, by
`mailboxes.get_mailbox` and by `poller.test_mailbox` -- becomes 404. A 403
would confirm the id exists somewhere, which is an org-membership oracle.
Every other `MailboxValidationError` becomes 400. Routing is by TYPE, never
by matching exception-message prose (mirrors `service_desk.api._http_error`).

**NO RESPONSE CARRIES A PASSWORD OR A CREDENTIAL ID** (invariant #3, spec
§10/§11.12). `MailboxOut` structurally has no field that can hold either;
`_mailbox_out` below is the only builder and passes `has_credential` --
the FACT that a credential is configured, which the admin UI needs -- and
nothing more. `MailboxTestResult.detail` is a constructed
`stage failed: reason` from `imap.py`'s fixed vocabularies, never
`str(exc)` and never a server response line.

**`last_error` AND `last_polled_at` ARE SURFACED.** A poison message stops
its own mailbox by design (Task 7's `poll_mailbox` records the failure and
moves on), so this API is the only place an operator learns that a mailbox
has stopped working. Both fields are on `MailboxOut`; both are already
sanitized by `poller._sanitize_excerpt` before they ever reach the column.

**THE TEST ROUTE IS RATE-LIMITED PER ORG** (spec §10). Same minimal Redis
INCR+EXPIRE counter `notification.api` uses for its test-send, for the same
reason one level removed: `MailboxTestResult.detail` distinguishes `connect
failed: dns` from `connect failed: connection-refused` from `tls failed:
certificate-invalid`, and `PATCH host` + `POST .../test`, repeated, is
otherwise a per-request oracle for which internal hosts exist and answer on
which ports. It is a defense-in-depth throttle on top of an already
`email_intake.admin`-gated, audited route -- not the sole control -- so the
limit is conservative-but-usable rather than the tightest possible.

**BUSINESS LOGIC IS NOT HERE.** Mailbox CRUD is `mailboxes.py`, the message
log is `messages.py`, the connection test is `poller.test_mailbox`. These
handlers resolve the principal, call one service function, and map errors.
"""

from datetime import datetime
from uuid import UUID

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api.deps import AuthContext, require_permission, require_permission_on_every_client
from app.core.i18n import tr
from app.core.tenancy import org_session
from app.modules.email_intake import microsoft, oauth, oauth_app
from app.modules.email_intake.errors import MailboxNotFoundError, MailboxValidationError
from app.modules.email_intake.mailboxes import (
    create_mailbox,
    delete_mailbox,
    disconnect_mailbox,
    get_mailbox,
    list_mailboxes,
    update_mailbox,
)
from app.modules.email_intake.messages import DEFAULT_LIMIT, MAX_LIMIT, list_messages
from app.modules.email_intake.models import EmailMailbox
from app.modules.email_intake.poller import test_mailbox
from app.modules.email_intake.schema import (
    Disposition,
    MailboxCreate,
    MailboxOut,
    MailboxTestResult,
    MailboxUpdate,
    MessageLogOut,
    OAuthAbandonIn,
    OAuthAppOut,
    OAuthAppUpdate,
    OAuthCompleteIn,
    OAuthStartOut,
)

#: `email_intake.admin` with a scope of every client: mailboxes, the OAuth app
#: and the credentials they write (`oauth.complete`, the refresh-token
#: rotation) are Novieri-global configuration carrying no client (MSP clients
#: spec §1.4, §2.2), so a technician whose grants are per client, or whose
#: request X-Client-Id narrowed, is refused before anything is read or
#: written. Reads (`email_intake.read`) stay open to any scope.
_ADMIN = require_permission_on_every_client("email_intake.admin")


router = APIRouter(tags=["email-intake"])

# 5 requests per rolling 60-second window, per org -- the same numbers
# `notification.api._enforce_test_send_rate_limit` chose, deliberately: it is
# the same class of route (an admin-triggered outbound connection whose
# failure classification is informative about the network) and one number is
# easier to reason about than two. An admin iterating on a mailbox's
# host/credentials plausibly wants a handful of tries in quick succession;
# 5/min preserves that while bounding a host-existence-mapping campaign to
# minutes per handful of hostnames.
_TEST_RATE_LIMIT = 5
_TEST_RATE_WINDOW_S = 60


async def _enforce_test_rate_limit(redis: aioredis.Redis, org_id: UUID) -> None:
    """`INCR` a per-org, per-window counter; set the expiry only when this
    window's key is created (`count == 1`) so the window is a genuine rolling
    60s TTL rather than being pushed out by every request. The request that
    trips the limit is itself counted and rejected, not let through.

    **IT FAILS CLOSED.** If Redis cannot be reached the counter does not
    exist, and a limiter that cannot count is not a limiter: letting the
    request through would silently convert a Redis outage into an unmetered
    outbound-connection probe with an informative failure vocabulary, which
    is the one property this throttle exists to deny. The trade is explicit
    and it is the cheap direction -- `/test` is a diagnostic convenience, not
    a delivery path, so refusing it during an outage costs an admin a manual
    retry, while failing open costs the property. Nothing else in this
    module's request path depends on Redis, so this cannot cascade.

    503, not 500: the request was well-formed and will work again shortly.
    """
    key = f"email_intake:mailbox_test_rate:{org_id}"
    try:
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, _TEST_RATE_WINDOW_S)
    except aioredis.RedisError:
        # Never `str(exc)`: a Redis URL can carry credentials.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            tr("mailbox-test rate limiting is unavailable; try again shortly"),
        ) from None
    if count > _TEST_RATE_LIMIT:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            tr("mailbox-test rate limit exceeded for this organization; try again shortly"),
        )


def _http_error(exc: MailboxValidationError) -> HTTPException:
    """By TYPE, never by message text. `MailboxNotFoundError` (a subclass) is
    404 -- and it is the SAME error for "absent" and "another org's", so this
    adds no oracle; every other `MailboxValidationError` is 400."""
    if isinstance(exc, MailboxNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


def _mailbox_out(row: EmailMailbox) -> MailboxOut:
    """The ONLY `MailboxOut` builder. Written out field by field rather than
    `MailboxOut.model_validate(row, ...)`: `has_credential` is derived, not a
    column, and Pydantic 2.13's `model_validate` has no `update=` parameter
    to inject it with (schema.py's docstring assumes one exists -- it does
    not). Enumerating the fields also makes it structurally obvious that
    `credential_id` is not among them (invariant #3)."""
    return MailboxOut(
        id=row.id,
        address=row.address,
        host=row.host,
        port=row.port,
        use_ssl=row.use_ssl,
        folder=row.folder,
        username=row.username,
        has_credential=row.credential_id is not None,
        auth_kind=row.auth_kind,
        credential_state=row.credential_state,
        default_category_id=row.default_category_id,
        reopen_window_days=row.reopen_window_days,
        active=row.active,
        last_polled_at=row.last_polled_at,
        last_error=row.last_error,
        created_at=row.created_at,
    )


# --------------------------------------------------------------------------- #
# GET/POST /email-intake/mailboxes (spec §2.1, §10)
# --------------------------------------------------------------------------- #


@router.get("/email-intake/mailboxes", response_model=list[MailboxOut])
async def list_mailboxes_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("email_intake.read")),
) -> list[MailboxOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await list_mailboxes(session, ctx.org_id)
        return [_mailbox_out(row) for row in rows]


@router.post(
    "/email-intake/mailboxes", status_code=status.HTTP_201_CREATED, response_model=MailboxOut
)
async def create_mailbox_route(
    body: MailboxCreate,
    request: Request,
    ctx: AuthContext = Depends(_ADMIN),
) -> MailboxOut:
    state = request.app.state
    try:
        # The error must escape the `org_session` block so the transaction
        # rolls back -- see the module docstring.
        async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
            mailbox = await create_mailbox(
                session, ctx.org_id, ctx.user_id, body, vault=state.vault
            )
            out = _mailbox_out(mailbox)
    except MailboxValidationError as exc:
        raise _http_error(exc) from None
    return out


# --------------------------------------------------------------------------- #
# GET/PATCH/DELETE /email-intake/mailboxes/{mailbox_id} (spec §10)
# --------------------------------------------------------------------------- #


@router.get("/email-intake/mailboxes/{mailbox_id}", response_model=MailboxOut)
async def get_mailbox_route(
    mailbox_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("email_intake.read")),
) -> MailboxOut:
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            out = _mailbox_out(await get_mailbox(session, ctx.org_id, mailbox_id))
    except MailboxValidationError as exc:
        raise _http_error(exc) from None
    return out


@router.patch("/email-intake/mailboxes/{mailbox_id}", response_model=MailboxOut)
async def update_mailbox_route(
    mailbox_id: UUID,
    body: MailboxUpdate,
    request: Request,
    ctx: AuthContext = Depends(_ADMIN),
) -> MailboxOut:
    state = request.app.state
    try:
        async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
            mailbox = await update_mailbox(
                session, ctx.org_id, ctx.user_id, mailbox_id, body, vault=state.vault
            )
            out = _mailbox_out(mailbox)
    except MailboxValidationError as exc:
        raise _http_error(exc) from None
    return out


@router.delete(
    "/email-intake/mailboxes/{mailbox_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_mailbox_route(
    mailbox_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(_ADMIN),
) -> None:
    """`vault=` is not optional. `delete_mailbox` drops the vault-held secret
    and its `Credential` row in the same transaction as the mailbox; omitting
    it would leave an ownerless envelope-encrypted IMAP password behind."""
    state = request.app.state
    try:
        async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
            await delete_mailbox(
                session, ctx.org_id, ctx.user_id, mailbox_id, vault=state.vault
            )
    except MailboxValidationError as exc:
        raise _http_error(exc) from None


# --------------------------------------------------------------------------- #
# POST /email-intake/mailboxes/{mailbox_id}/test (spec §10)
# --------------------------------------------------------------------------- #


@router.post("/email-intake/mailboxes/{mailbox_id}/test", response_model=MailboxTestResult)
async def test_mailbox_route(
    mailbox_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(_ADMIN),
) -> MailboxTestResult:
    """Connect and list the folder. `email_intake.admin`-gated, rate-limited
    per org, audited (`email_intake.mailbox_tested`, written by
    `poller.test_mailbox` alongside the `email_intake.credential_used` event
    for the checkout it performs), and returning neither the password nor a
    raw server error.

    A failed connection is `200 {"ok": false, ...}`, not a 4xx/5xx: the
    REQUEST succeeded and its answer is "this configuration does not work".
    `test_mailbox` owns its own sessions -- it must not hold a database
    transaction open across an IMAP conversation -- so there is no
    `org_session` here.

    **THE WORST CASE, EXPLICITLY, BECAUSE THIS ROUTE OPENS A SOCKET TO AN
    ADMIN-SUPPLIED HOST FROM THE API PROCESS.** This request returns within
    `poller.TEST_REQUEST_DEADLINE_S` (20s) plus two short database round
    trips. The worker thread it borrows is held for at most
    `poller.TEST_CONVERSATION_DEADLINE_S` (15s), after which the transport
    closes its socket underneath it, plus the platform resolver's own bound
    on an unresolvable name (`getaddrinfo` is not interruptible and runs
    before any socket exists). It is NOT bounded by `imap.IMAP_TIMEOUT_S`,
    which is a per-socket-operation timeout that restarts on every byte and
    bounds no exchange at all -- a server that trickles never trips it. That
    distinction is the whole reason the two deadlines exist: without them, a
    host an admin controls could hold one of this process's default-executor
    threads indefinitely, five times a minute, starving everything else that
    shares it (`egress.py`'s DNS resolution included).
    """
    state = request.app.state
    await _enforce_test_rate_limit(state.redis, ctx.org_id)
    try:
        return await test_mailbox(
            ctx.org_id,
            mailbox_id,
            actor_id=ctx.user_id,
            engine=state.app_engine,
            vault=state.vault,
            clients=ctx.clients,
        )
    except MailboxValidationError as exc:
        raise _http_error(exc) from None


# --------------------------------------------------------------------------- #
# GET/PUT /email-intake/oauth-app -- the org's Azure application
# (docs/superpowers/specs/2026-09-11-mail-oauth-design.md §4.4)
# --------------------------------------------------------------------------- #

#: Bounded in the route, not on the model, so a refusal never echoes it.
MAX_CLIENT_SECRET = 512


def _redirect_uri_or_none(request: Request) -> str | None:
    try:
        return microsoft.redirect_uri(request.app.state.settings)
    except microsoft.MicrosoftNotConfigured:
        return None


def _oauth_app_out(app: object | None, request: Request) -> OAuthAppOut:
    return OAuthAppOut(
        tenant_id=getattr(app, "tenant_id", ""),
        client_id=getattr(app, "client_id", ""),
        has_secret=app is not None,
        redirect_uri=_redirect_uri_or_none(request),
    )


@router.get("/email-intake/oauth-app", response_model=OAuthAppOut)
async def read_oauth_app_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("email_intake.read")),
) -> OAuthAppOut:
    """The org's Azure application for Microsoft 365 sign-in, and the return
    address to register on it. Never the client secret: `has_secret` only
    says whether one is stored."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        app = await oauth_app.get_app(session, org_id=ctx.org_id)
        return _oauth_app_out(app, request)


@router.put("/email-intake/oauth-app", response_model=OAuthAppOut)
async def write_oauth_app_route(
    body: OAuthAppUpdate,
    request: Request,
    ctx: AuthContext = Depends(_ADMIN),
) -> OAuthAppOut:
    if body.client_secret is not None and len(body.client_secret) > MAX_CLIENT_SECRET:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            tr(
                "the client secret may be at most {max_chars} characters",
                max_chars=MAX_CLIENT_SECRET,
            ),
        )
    state = request.app.state
    try:
        async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
            app = await oauth_app.set_app(
                session, org_id=ctx.org_id, actor_id=ctx.user_id, body=body, vault=state.vault
            )
            out = _oauth_app_out(app, request)
    except MailboxValidationError as exc:
        raise _http_error(exc) from None
    return out


# --------------------------------------------------------------------------- #
# Connect with Microsoft (design §4.3-§4.5)
# --------------------------------------------------------------------------- #


def _not_configured(exc: microsoft.MicrosoftNotConfigured) -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))


@router.post(
    "/email-intake/mailboxes/{mailbox_id}/oauth/start", response_model=OAuthStartOut
)
async def start_oauth_route(
    mailbox_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(_ADMIN),
) -> OAuthStartOut:
    """Mint a single-use authorization and return where to send the browser.
    A POST: it writes a row and an audit event."""
    state = request.app.state
    try:
        async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
            url = await oauth.start(
                session,
                org_id=ctx.org_id,
                user_id=ctx.user_id,
                mailbox_id=mailbox_id,
                settings=state.settings,
                vault=state.vault,
            )
    except microsoft.MicrosoftNotConfigured as exc:
        raise _not_configured(exc) from None
    except oauth.OAuthFlowError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    return OAuthStartOut(authorize_url=url)


@router.post("/email-intake/mailboxes/{mailbox_id}/oauth/disconnect", response_model=MailboxOut)
async def disconnect_oauth_route(
    mailbox_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(_ADMIN),
) -> MailboxOut:
    state = request.app.state
    try:
        async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
            mailbox = await disconnect_mailbox(
                session, ctx.org_id, ctx.user_id, mailbox_id, vault=state.vault
            )
            out = _mailbox_out(mailbox)
    except MailboxValidationError as exc:
        raise _http_error(exc) from None
    return out


@router.post("/email-intake/oauth/complete", response_model=MailboxOut)
async def complete_oauth_route(
    body: OAuthCompleteIn,
    request: Request,
    ctx: AuthContext = Depends(_ADMIN),
) -> MailboxOut:
    """Redeem the code the console received from Microsoft. The caller's own
    token authorises this; `state` is a third check, not the tenant binding."""
    state = request.app.state
    try:
        mailbox_id = await oauth.complete(
            org_id=ctx.org_id,
            user_id=ctx.user_id,
            state=body.state,
            code=body.code,
            settings=state.settings,
            engine=state.app_engine,
            vault=state.vault,
            clients=ctx.clients,
        )
    except microsoft.MicrosoftNotConfigured as exc:
        raise _not_configured(exc) from None
    except oauth.OAuthFlowError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        return _mailbox_out(await get_mailbox(session, ctx.org_id, mailbox_id))


@router.post("/email-intake/oauth/abandon", status_code=status.HTTP_204_NO_CONTENT)
async def abandon_oauth_route(
    body: OAuthAbandonIn,
    request: Request,
    ctx: AuthContext = Depends(_ADMIN),
) -> None:
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            await oauth.abandon(session, org_id=ctx.org_id, user_id=ctx.user_id, state=body.state)
    except oauth.OAuthFlowError as exc:
        raise HTTPException(exc.status, str(exc)) from None


# --------------------------------------------------------------------------- #
# GET /email-intake/messages (spec §10)
# --------------------------------------------------------------------------- #


@router.get("/email-intake/messages", response_model=MessageLogOut)
async def list_messages_route(
    request: Request,
    mailbox_id: UUID | None = Query(default=None),
    disposition: Disposition | None = Query(default=None),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    ctx: AuthContext = Depends(require_permission_on_every_client("email_intake.read")),
) -> MessageLogOut:
    """The disposition log, filtered on `disposition`, `mailbox_id` and a
    time range (spec §10), with `total` for paging and the quarantined count
    over the whole scope.

    EVERY CLIENT'S MAIL (MSP clients spec, invariant 5a): the log holds the
    sender and the parsed body of every message any client's people sent.
    Each message is filed under a client -- its contact's, else the one
    holding the sender's domain, else Unassigned, a quarantined one included
    (plan decision D1); a reply that lands on a ticket takes the ticket's
    (`intake.py`). Rows from before phase 2 Task 4 have none. Reading the log needs
    `email_intake.read` on every client, not on one
    (`tests/test_global_writes.py::GLOBAL_READS`).

    `body_text` is in the response and that is deliberate (spec §10): it is
    the PARSED text, never the raw RFC822 message, and it is how an operator
    diagnoses a quarantined message. `email_intake.read` is the gate. It is
    served as an EXCERPT (`messages.BODY_EXCERPT_CHARS`, 4 KiB), which is
    what bounds this response: a stored body is capped at 1 MiB by the
    parser, that size is chosen by whoever sent the mail, and 200 of them
    would be a ~200 MB response any reader could ask for. A full page of
    excerpts is under 1 MB of body text.

    `since`/`until` and the ordering are on `created_at` -- when the platform
    recorded the row -- NOT on the sender-supplied `received_at`. See
    `messages.py`'s docstring: the latter lets a sender hide a quarantined
    message from the operator's window, and from that window's quarantined
    count, or pin it to row 1 forever.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        return await list_messages(
            session,
            ctx.org_id,
            mailbox_id=mailbox_id,
            disposition=disposition,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )
