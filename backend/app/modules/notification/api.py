"""Notification core REST API (docs/modules/notification/spec.md §9, §8).

Routes are all `/notifications`-prefixed; `app.modules.notification.register`
mounts this router under `/api/v1`. Every route is gated server-side by
`require_permission` (invariant #6, the frontend never gates security):
`notification.read` for every read, `notification.admin` for every mutation
AND the test-send (spec §8 -- it is a high-privilege grant, admin tier not
technician tier). `org_id`/`actor_id` always come from `AuthContext`, never
the request body/path (invariant #5).

Like service_desk/budget/licenses/..., this module dispatches no agent jobs --
`register(app)` mounts this router only, no job type registration and no
gateway edit (spec §1: control-plane only, no agent capability).

ROUTE-DERIVED RBAC COMPLETENESS (requirement #2, `tests/test_notification_api.py`).
`app.core.rbac.route_permission` walks a route's OWN dependant tree to recover
the `require_permission(code)` it was built with, rather than requiring a
second, hand-maintained "route -> permission" list a later addition to this
file could silently miss. Every route below MUST depend on
`require_permission(...)` (directly, not merely transitively) for that test to
see it -- there is no other way to satisfy it.

ROUTE ORDERING (requirement #3, the Phase-11/12 lesson carried into the
ticket core and repeated here): literal path segments are declared before the
`{id}`-shaped route at the same depth, and a route with an extra literal
suffix (`/channels/{channel_id}/test`) is declared after the bare
`/channels/{channel_id}` routes it extends. Concretely, within each resource
block: the collection routes (list/create, no id) come first, then the
by-id routes (get/patch/delete), then any suffixed sub-action.

ERROR TRANSLATION (requirement -- mirrors `service_desk.api._http_error`
exactly). Every `NotificationValidationError` the service layer raises is
translated to an HTTP 4xx here, by TYPE never by message text:
`NotificationNotFoundError` (a subclass, raised for a missing/cross-org
channel, rule, allowlist entry, queue, or user) becomes a 404; every other
`NotificationValidationError` -- a duplicate name/host, a bad config, the
§6.4 internal-note-to-requester pairing -- becomes a 400. Route handlers
translate errors and nothing more; the business logic that raises them stays
in `channels.py`/`rules.py`/`delivery.py`.

NO SECRET, NO FULL URL, NO RESPONSE BODY (requirement #4, spec §4.4/§9/
invariant #3). `ChannelOut` structurally has no field that can carry a
credential -- this file adds none. `AllowlistEntryOut`/`DeliveryOut` never
carry a full webhook URL with a query string (the allowlist names a bare
host+port; a delivery's `recipient` is already a bare address/host, never
reconstructed here). `TestSendResult`/`DeliveryOut` never carry
`error_excerpt`/a raw response body -- only the bounded, already-sanitized
`error`/`last_error` category strings the service layer itself produced.

THE TEST-SEND ROUTE IS SUBJECT TO THE IDENTICAL EGRESS GUARD AS A REAL
DELIVERY (requirement #5, spec §5/§12.6). `POST /channels/{id}/test` calls
`delivery.send_test`, which dispatches through the exact same
`_send_webhook`/`app.core.egress.post_json` path a real delivery attempt
uses for a webhook channel -- there is no second, route-local HTTP call here.
It is `notification.admin`-gated (not `notification.read`) and audited
(`notification.channel_tested`), matching the audit list spec §8 names. It is
also RATE-LIMITED PER ORG (spec §5, whole-branch review finding I3) via
`_enforce_test_send_rate_limit` below -- a minimal, contained Redis counter
for this one route, not a general rate-limiting framework.

REQUEST-BODY COMPOSITION FOR CHANNEL SECRETS. `channels.create_channel`/
`update_channel` take a channel's plaintext `secret` as a keyword SEPARATE
from `data: ChannelCreate`/`ChannelUpdate` (`channels.py`) -- `ChannelCreate`/
`ChannelUpdate` themselves carry no such field, deliberately: `config` is
what those models validate against `_reject_secret_keys`, and a secret is
never part of `config`. `ChannelCreateRequest`/`ChannelUpdateRequest` below
are the API-layer request bodies that add exactly one extra optional
`secret` field on top of the schema-layer models they subclass (inheriting
every one of those models' own validators unchanged) -- composition for
request PARSING, not business logic, so this stays within a route handler's
remit.

`GET /deliveries` HAS NO SERVICE-LAYER LIST FUNCTION (spec §9's filters:
`status`, `channel_id`, `event_type`, a time range, plus pagination) -- the
same shape `service_desk.api.list_attachments_route` already uses for
`TicketAttachment` ("no service list function, query inline"). It returns
the `items`/`total` pagination envelope `app.api.v1.audit`/`app.api.v1.jobs`
already use, not a bare list -- spec §9's "pagination matching the ticket
core's convention" generalizes to the shape THIS project uses whenever a
list endpoint reports a `total` count, of which the ticket core's own
(uncounted) list endpoints are the simpler special case.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import AuthContext, require_permission, require_permission_on_every_client
from app.core.audit import audit_service
from app.core.i18n import tr, tr_marked
from app.core.tenancy import org_session
from app.models import (
    DomainEvent,
    NotificationChannel,
    NotificationDelivery,
    NotificationRule,
    QueueMember,
    WebhookAllowlistEntry,
)
from app.modules.notification.channels import (
    MICROSOFT_NEEDS_SIGN_IN,
    create_channel,
    delete_channel,
    get_channel,
    list_channels,
    update_channel,
)
from app.modules.notification.delivery import send_test
from app.modules.notification.errors import (
    NotificationNotFoundError,
    NotificationValidationError,
)
from app.modules.notification.rules import (
    add_allowlist_entry,
    add_queue_member,
    create_rule,
    delete_allowlist_entry,
    delete_rule,
    list_allowlist,
    list_queue_members,
    list_rules,
    remove_queue_member,
    update_rule,
)
from app.modules.notification.schema import (
    AllowlistEntryCreate,
    AllowlistEntryOut,
    ChannelCreate,
    ChannelOut,
    ChannelUpdate,
    DeliveryListResponse,
    DeliveryOut,
    DeliveryStatus,
    EmailSignIn,
    MicrosoftSignIn,
    PasswordSignIn,
    QueueMemberOut,
    RuleCreate,
    RuleOut,
    RuleUpdate,
    TestSendResult,
)

router = APIRouter(tags=["notification"])

# --------------------------------------------------------------------------- #
# POST /channels/{id}/test rate limit (whole-branch review finding I3, spec
# §5: "rate-limited per org"). A MINIMAL, CONTAINED throttle for this ONE
# route -- there is no general rate-limiting facility anywhere in this
# codebase (a grep for `rate.limit|ratelimit` across `app/` returns nothing)
# and this does not add one; it is a plain Redis INCR+EXPIRE counter, the
# same `redis.asyncio` client `app.worker`/`app.api.v1.jobs` already use via
# `request.app.state.redis`, scoped to one Redis key per org.
#
# WHY THIS ROUTE SPECIFICALLY NEEDS IT. `TestSendResult.error` returns the
# `EgressBlocked` reason CATEGORY verbatim, and `blocked_private` vs
# `dns_failure` distinguishes "this internal hostname exists and resolves
# into private space" from "this name does not resolve" -- exactly the
# per-host detail spec §4.4 says the category strings exist to withhold
# ("not a per-IP detail that would let an admin map the internal network").
# Withholding the IP while leaking existence, UNMETERED, gives most of that
# back: `PATCH channel.config.url` + `POST .../test`, repeated, is a
# per-request internal-DNS existence oracle.
#
# LIMIT CHOSEN: 5 requests per rolling 60-second window, per org. An admin
# iterating on a channel's URL/credentials plausibly wants a handful of tries
# in quick succession; 5/min preserves that while still bounding a
# DNS-existence-mapping campaign to an impractically slow rate (minutes per
# handful of hostnames, not per request). This is a defense-in-depth throttle
# on top of an already-`notification.admin`-gated, audited route (spec §5) --
# not the sole control -- so a conservative-but-usable limit is the right
# trade-off, not the tightest one possible.
_TEST_SEND_RATE_LIMIT = 5
_TEST_SEND_RATE_WINDOW_S = 60


async def _enforce_test_send_rate_limit(redis: aioredis.Redis, org_id: UUID) -> None:
    """`INCR` a per-org, per-window counter; set its expiry only the first
    time this window's key is created (`count == 1`) so the window is a
    genuine rolling 60s TTL rather than being pushed out on every request.
    Raises 429 once `count` exceeds the limit -- the request that trips it is
    itself counted (and rejected), not allowed through "for free"."""
    key = f"notification:test_send_rate:{org_id}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, _TEST_SEND_RATE_WINDOW_S)
    if count > _TEST_SEND_RATE_LIMIT:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            tr("test-send rate limit exceeded for this organization; try again shortly"),
        )


def _http_error(exc: NotificationValidationError) -> HTTPException:
    """Translate a `NotificationValidationError` to an HTTP 4xx -- it must
    never become a 500. Routing is by TYPE, never by matching exception-
    message prose (mirrors `service_desk.api._http_error` exactly).
    `NotificationNotFoundError` -- a missing/cross-org channel, rule,
    allowlist entry, queue, or user -- becomes a 404; every other
    `NotificationValidationError` becomes a 400."""
    if isinstance(exc, NotificationNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


# --------------------------------------------------------------------------- #
# Request bodies that add an API-layer-only `secret` field on top of the
# schema-layer `ChannelCreate`/`ChannelUpdate` models (see module docstring).
# --------------------------------------------------------------------------- #


class _SignInFields(BaseModel):
    """Email sign-in, write-only. A password channel takes `smtp_username` +
    `smtp_password`; a Microsoft 365 channel the three `microsoft_*` values.
    All go to the vault as one value; none is ever returned."""

    smtp_username: str | None = Field(default=None, min_length=1, max_length=320)
    smtp_password: str | None = Field(default=None, min_length=1)
    microsoft_tenant_id: str | None = Field(default=None, min_length=1, max_length=200)
    microsoft_client_id: str | None = Field(default=None, min_length=1, max_length=200)
    microsoft_client_secret: str | None = Field(default=None, min_length=1)

    def sign_in(self) -> EmailSignIn | None:
        microsoft_values = (
            self.microsoft_tenant_id,
            self.microsoft_client_id,
            self.microsoft_client_secret,
        )
        password_values = (self.smtp_username, self.smtp_password)
        if any(v is not None for v in microsoft_values):
            if any(v is not None for v in password_values):
                raise NotificationValidationError(
                    tr("give either a username and password or the Microsoft 365 sign-in, not both")
                )
            tenant_id, client_id, client_secret = microsoft_values
            if tenant_id is None or client_id is None or client_secret is None:
                raise NotificationValidationError(tr_marked(MICROSOFT_NEEDS_SIGN_IN))
            return MicrosoftSignIn(
                tenant_id=tenant_id, client_id=client_id, client_secret=client_secret
            )
        if any(v is not None for v in password_values):
            if self.smtp_username is None or self.smtp_password is None:
                raise NotificationValidationError(tr("give both the username and the password"))
            return PasswordSignIn(username=self.smtp_username, password=self.smtp_password)
        return None


class ChannelCreateRequest(ChannelCreate, _SignInFields):
    secret: str | None = Field(default=None, min_length=1)


class ChannelUpdateRequest(ChannelUpdate, _SignInFields):
    secret: str | None = Field(default=None, min_length=1)


# --------------------------------------------------------------------------- #
# Response-out builders -- no `org_id`, no credential-shaped field anywhere
# (invariants #3, #5).
# --------------------------------------------------------------------------- #


def _channel_out(row: NotificationChannel) -> ChannelOut:
    return ChannelOut(
        id=row.id,
        kind=row.kind,
        name=row.name,
        config=row.config,
        active=row.active,
        has_credential=row.credential_id is not None,
        created_at=row.created_at,
    )


def _rule_out(row: NotificationRule) -> RuleOut:
    return RuleOut(
        id=row.id,
        event_type=row.event_type,
        channel_id=row.channel_id,
        recipient_kind=row.recipient_kind,
        queue_id=row.queue_id,
        active=row.active,
        created_at=row.created_at,
    )


def _allowlist_out(row: WebhookAllowlistEntry) -> AllowlistEntryOut:
    return AllowlistEntryOut(
        id=row.id, host=row.host, port=row.port, note=row.note, created_at=row.created_at
    )


def _queue_member_out(row: QueueMember) -> QueueMemberOut:
    return QueueMemberOut(user_id=row.user_id, queue_id=row.queue_id)


def _delivery_out(row: NotificationDelivery) -> DeliveryOut:
    return DeliveryOut(
        id=row.id,
        event_id=row.event_id,
        channel_id=row.channel_id,
        recipient=row.recipient,
        status=row.status,
        attempts=row.attempts,
        last_error=row.last_error,
        last_status_code=row.last_status_code,
        next_attempt_at=row.next_attempt_at,
        sent_at=row.sent_at,
        created_at=row.created_at,
    )


# --------------------------------------------------------------------------- #
# GET/POST /notifications/channels, GET/PATCH/DELETE
# /notifications/channels/{channel_id}, POST
# /notifications/channels/{channel_id}/test (spec §2.2, §5, §9).
# --------------------------------------------------------------------------- #


@router.get("/notifications/channels", response_model=list[ChannelOut])
async def list_channels_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("notification.read")),
) -> list[ChannelOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await list_channels(session, org_id=ctx.org_id)
        return [_channel_out(r) for r in rows]


@router.post(
    "/notifications/channels", status_code=status.HTTP_201_CREATED, response_model=ChannelOut
)
async def create_channel_route(
    body: ChannelCreateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("notification.admin")),
) -> ChannelOut:
    state = request.app.state
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        try:
            channel = await create_channel(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                data=body,
                secret=body.secret,
                sign_in=body.sign_in(),
                vault=state.vault,
            )
        except NotificationValidationError as exc:
            raise _http_error(exc) from None
        return _channel_out(channel)


@router.get("/notifications/channels/{channel_id}", response_model=ChannelOut)
async def get_channel_route(
    channel_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("notification.read")),
) -> ChannelOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            channel = await get_channel(session, org_id=ctx.org_id, channel_id=channel_id)
        except NotificationValidationError as exc:
            raise _http_error(exc) from None
        return _channel_out(channel)


@router.patch("/notifications/channels/{channel_id}", response_model=ChannelOut)
async def update_channel_route(
    channel_id: UUID,
    body: ChannelUpdateRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("notification.admin")),
) -> ChannelOut:
    state = request.app.state
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        try:
            channel = await update_channel(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                channel_id=channel_id,
                data=body,
                secret=body.secret,
                sign_in=body.sign_in(),
                vault=state.vault,
            )
        except NotificationValidationError as exc:
            raise _http_error(exc) from None
        return _channel_out(channel)


@router.delete("/notifications/channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel_route(
    channel_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("notification.admin")),
) -> None:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await delete_channel(
                session, org_id=ctx.org_id, actor_id=ctx.user_id, channel_id=channel_id
            )
        except NotificationValidationError as exc:
            raise _http_error(exc) from None


@router.post("/notifications/channels/{channel_id}/test", response_model=TestSendResult)
async def test_channel_route(
    channel_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("notification.admin")),
) -> TestSendResult:
    """`notification.admin`-gated (spec §5: not `notification.read` -- a test
    send dials a real destination) and SUBJECT TO THE IDENTICAL EGRESS GUARD
    as a real delivery -- see `delivery.send_test`'s docstring. Audited here,
    once, as `notification.channel_tested`; `send_test` itself writes no
    delivery row and no outbox event (a test send is not a delivery
    attempt), so this is the only place that audit event is ever written."""
    state = request.app.state
    await _enforce_test_send_rate_limit(state.redis, ctx.org_id)
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        try:
            channel = await get_channel(session, org_id=ctx.org_id, channel_id=channel_id)
        except NotificationValidationError as exc:
            raise _http_error(exc) from None

        outcome = await send_test(
            session, org_id=ctx.org_id, channel=channel, vault=state.vault, actor_id=ctx.user_id
        )

        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="notification.channel_tested",
            target_type="notification_channel",
            target_id=str(channel.id),
            metadata={"delivered": outcome.delivered, "error": outcome.error},
        )
        await session.flush()
        return TestSendResult(
            delivered=outcome.delivered, status_code=outcome.status_code, error=outcome.error
        )


# --------------------------------------------------------------------------- #
# GET/POST /notifications/rules, PATCH/DELETE /notifications/rules/{rule_id}
# (spec §2.3, §6, §9).
# --------------------------------------------------------------------------- #


@router.get("/notifications/rules", response_model=list[RuleOut])
async def list_rules_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("notification.read")),
) -> list[RuleOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await list_rules(session, org_id=ctx.org_id)
        return [_rule_out(r) for r in rows]


@router.post("/notifications/rules", status_code=status.HTTP_201_CREATED, response_model=RuleOut)
async def create_rule_route(
    body: RuleCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("notification.admin")),
) -> RuleOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            rule = await create_rule(session, org_id=ctx.org_id, actor_id=ctx.user_id, data=body)
        except NotificationValidationError as exc:
            raise _http_error(exc) from None
        return _rule_out(rule)


@router.patch("/notifications/rules/{rule_id}", response_model=RuleOut)
async def update_rule_route(
    rule_id: UUID,
    body: RuleUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("notification.admin")),
) -> RuleOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            rule = await update_rule(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                rule_id=rule_id,
                active=body.active,
            )
        except NotificationValidationError as exc:
            raise _http_error(exc) from None
        return _rule_out(rule)


@router.delete("/notifications/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule_route(
    rule_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("notification.admin")),
) -> None:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await delete_rule(session, org_id=ctx.org_id, actor_id=ctx.user_id, rule_id=rule_id)
        except NotificationValidationError as exc:
            raise _http_error(exc) from None


# --------------------------------------------------------------------------- #
# GET/POST /notifications/allowlist, DELETE
# /notifications/allowlist/{entry_id} (spec §2.6, §4, §9). The literal
# collection route is declared before the `{entry_id}` route at the same
# resource (requirement #3).
# --------------------------------------------------------------------------- #


@router.get("/notifications/allowlist", response_model=list[AllowlistEntryOut])
async def list_allowlist_route(
    request: Request,
    ctx: AuthContext = Depends(require_permission("notification.read")),
) -> list[AllowlistEntryOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await list_allowlist(session, org_id=ctx.org_id)
        return [_allowlist_out(r) for r in rows]


@router.post(
    "/notifications/allowlist",
    status_code=status.HTTP_201_CREATED,
    response_model=AllowlistEntryOut,
)
async def create_allowlist_entry_route(
    body: AllowlistEntryCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("notification.admin")),
) -> AllowlistEntryOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            entry = await add_allowlist_entry(
                session, org_id=ctx.org_id, actor_id=ctx.user_id, data=body
            )
        except NotificationValidationError as exc:
            raise _http_error(exc) from None
        return _allowlist_out(entry)


@router.delete("/notifications/allowlist/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_allowlist_entry_route(
    entry_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("notification.admin")),
) -> None:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await delete_allowlist_entry(
                session, org_id=ctx.org_id, actor_id=ctx.user_id, entry_id=entry_id
            )
        except NotificationValidationError as exc:
            raise _http_error(exc) from None


# --------------------------------------------------------------------------- #
# GET /notifications/queues/{queue_id}/members, PUT/DELETE
# /notifications/queues/{queue_id}/members/{user_id} (spec §2.4, §9).
# --------------------------------------------------------------------------- #


@router.get(
    "/notifications/queues/{queue_id}/members", response_model=list[QueueMemberOut]
)
async def list_queue_members_route(
    queue_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("notification.read")),
) -> list[QueueMemberOut]:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        rows = await list_queue_members(session, org_id=ctx.org_id, queue_id=queue_id)
        return [_queue_member_out(r) for r in rows]


@router.put(
    "/notifications/queues/{queue_id}/members/{user_id}", response_model=QueueMemberOut
)
async def add_queue_member_route(
    queue_id: UUID,
    user_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("notification.admin")),
) -> QueueMemberOut:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            member = await add_queue_member(
                session, org_id=ctx.org_id, actor_id=ctx.user_id, queue_id=queue_id, user_id=user_id
            )
        except NotificationValidationError as exc:
            raise _http_error(exc) from None
        return _queue_member_out(member)


@router.delete(
    "/notifications/queues/{queue_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_queue_member_route(
    queue_id: UUID,
    user_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("notification.admin")),
) -> None:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await remove_queue_member(
                session, org_id=ctx.org_id, actor_id=ctx.user_id, queue_id=queue_id, user_id=user_id
            )
        except NotificationValidationError as exc:
            raise _http_error(exc) from None


# --------------------------------------------------------------------------- #
# GET /notifications/deliveries (spec §2.5, §7, §9). No service-layer list
# function exists for `NotificationDelivery` -- see module docstring's note
# on this route, mirroring `service_desk.api.list_attachments_route`.
# --------------------------------------------------------------------------- #


@router.get("/notifications/deliveries", response_model=DeliveryListResponse)
async def list_deliveries_route(
    request: Request,
    status_: DeliveryStatus | None = Query(default=None, alias="status"),
    channel_id: UUID | None = Query(default=None),
    event_type: str | None = Query(default=None, max_length=100),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: AuthContext = Depends(require_permission("notification.read")),
) -> DeliveryListResponse:
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        conditions: list[Any] = [NotificationDelivery.org_id == ctx.org_id]
        if status_ is not None:
            conditions.append(NotificationDelivery.status == status_)
        if channel_id is not None:
            conditions.append(NotificationDelivery.channel_id == channel_id)
        if since is not None:
            conditions.append(NotificationDelivery.created_at >= since)
        if until is not None:
            conditions.append(NotificationDelivery.created_at <= until)

        base = select(NotificationDelivery).where(*conditions)
        if event_type is not None:
            base = base.join(
                DomainEvent, DomainEvent.id == NotificationDelivery.event_id
            ).where(
                # Whole-branch review finding M5: `NotificationDelivery.
                # org_id == ctx.org_id` is already in `conditions` above, so
                # this join is already effectively org-scoped through the
                # delivery row -- this predicate is defense in depth (RLS via
                # `org_session` scopes `domain_events` too), not a fix for a
                # reachable leak, matching this module's "explicit org_id
                # predicate on every query" discipline.
                DomainEvent.org_id == ctx.org_id,
                DomainEvent.event_type == event_type,
            )

        total = (
            await session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        rows = (
            (
                await session.execute(
                    base.order_by(
                        NotificationDelivery.created_at, NotificationDelivery.id
                    )
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return DeliveryListResponse(items=[_delivery_out(r) for r in rows], total=total)
