"""Pydantic request/response models for the Notification module
(docs/modules/notification/spec.md §2, §6.4, §9).

Every `*Update` model has all-optional fields (`X | None = None`) so the
service layer can apply `model_dump(exclude_unset=True)` (spec §9) -- an
omitted field is left untouched.

SECRET-KEY VALIDATION IS SHARED, NOT DUPLICATED. `config` is returned by the
API (spec §2.2), so a secret placed there would be a credential disclosure
(invariant #3). Both `ChannelCreate.config` and `ChannelUpdate.config` run
their check through the single module-level `_reject_secret_keys` function
below -- there is exactly one copy of the key list and the rejection logic,
so it cannot drift between create and update and silently stop rejecting
secrets on one of the two paths.

KIND/CONFIG COHERENCE VALIDATION IS ALSO SHARED (whole-branch review finding
C1), the same discipline. `validate_channel_config(kind, config)` is the one
copy of "does this config match what this kind of channel needs" --
`ChannelCreate`'s own `model_validator` calls it with `self.kind`;
`ChannelUpdate` has no `kind` field to call it with at the Pydantic layer (see
`validate_channel_config`'s own docstring), so `channels.update_channel` calls
it directly, service-side, against the channel row's EXISTING `kind` once
loaded. Before this fix `ChannelUpdate(config={})` was accepted outright.

`ChannelOut` carries no field capable of holding a credential -- not even
`credential_id` -- which is a structural guarantee (the type simply has no
such field) rather than a runtime filter that a future edit could bypass.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.events import EVENT_TYPES, staff_only_rejection
from app.core.i18n import tr
from app.modules.email_intake import microsoft

RecipientKind = Literal["queue_members", "assignee", "requester", "channel_default"]
ChannelKind = Literal["webhook", "email"]
DeliveryStatus = Literal["pending", "sent", "failed", "dead"]
#: How an email channel signs in to its server. Absent means "password".
EmailAuth = Literal["password", "microsoft"]
EMAIL_AUTHS: frozenset[str] = frozenset({"password", "microsoft"})


def email_auth(config: dict[str, Any]) -> str:
    return str(config.get("auth", "password"))

# Keys that must never appear in `config` -- it is returned by the API, so a
# secret placed here would be a credential disclosure (invariant #3, spec
# §2.2). Secrets belong in the vault, referenced by `credential_id`.
_SECRET_KEYS = {"password", "secret", "token", "api_key", "apikey", "authorization"}


def _reject_secret_keys(value: dict[str, Any]) -> dict[str, Any]:
    """The single copy of the secret-key check. Both `ChannelCreate` and
    `ChannelUpdate` call this -- see module docstring for why it must not be
    copied a second time."""
    found = {k for k in value if k.lower() in _SECRET_KEYS}
    if found:
        raise ValueError(
            tr(
                "config must not contain secret keys {keys}; "
                "secrets belong in the vault (spec §2.2)",
                keys=sorted(found),
            )
        )
    return value


def validate_channel_config(kind: str, config: dict[str, Any]) -> None:
    """THE SINGLE COPY of kind/config coherence validation (whole-branch
    review finding C1). Raises `ValueError` (never returns a value) so it can
    be called both from `ChannelCreate`'s own Pydantic `model_validator`
    (where a raised `ValueError` becomes part of the model's normal
    `ValidationError`) AND from `channels.update_channel` as a plain function
    call against a config replacing an EXISTING channel's config.

    `ChannelUpdate` cannot run this itself: it carries no `kind` field --
    `kind` is immutable after creation (`channels.py`'s own docstring), so a
    `PATCH` body never names it. Validating a channel's UPDATED config
    therefore requires the row's EXISTING `kind`, which only exists once
    `channels.update_channel` has loaded it via `get_channel` -- this is the
    "validate service-side, against the loaded row's kind" fix the review
    names. Before this fix, `ChannelUpdate(config={})` was accepted outright:
    no validator ran at all on the update path, so a channel could be PATCHed
    into a structurally broken config `_send_email`/`_send_webhook` cannot
    parse, with nothing upstream of the worker ever catching it (see
    `delivery.py`'s `attempt_delivery`, which now also degrades that failure
    to a terminal outcome on the one affected delivery rather than letting it
    escape -- the second, independent half of this fix).

    Beyond bare key PRESENCE (the original check), `port` must be a real
    `int` in the legal TCP range and `host`/`from_address` must be non-empty
    strings -- `ChannelCreate(kind="email", config={"host": "h", "port":
    "not-a-number", "from_address": "x@y.z"})` used to be ACCEPTED (only
    presence was checked), and `_send_email`'s `int(config["port"])` raised an
    uncaught `ValueError` at send time instead of at write time."""
    if kind == "webhook":
        if not config.get("url"):
            raise ValueError(tr("webhook config requires 'url'"))
        return

    missing = [k for k in ("host", "port", "from_address") if k not in config]
    if missing:
        raise ValueError(tr("email config requires {missing}", missing=missing))

    port = config["port"]
    # `bool` is a subclass of `int` in Python; reject it explicitly so
    # `port: true` doesn't slip through as `port=1`.
    if isinstance(port, bool) or not isinstance(port, int):
        raise ValueError(tr("email config 'port' must be an integer, not a string or other type"))
    if not (1 <= port <= 65535):
        raise ValueError(tr("email config 'port' must be between 1 and 65535"))

    host = config["host"]
    if not isinstance(host, str) or not host:
        raise ValueError(tr("email config 'host' must be a non-empty string"))

    from_address = config["from_address"]
    if not isinstance(from_address, str) or not from_address:
        raise ValueError(tr("email config 'from_address' must be a non-empty string"))

    auth = config.get("auth", "password")
    if auth not in EMAIL_AUTHS:
        raise ValueError(tr("email config 'auth' must be 'password' or 'microsoft'"))
    # Microsoft 365 sign-in always sends through Microsoft's own server: the
    # token it uses can send as the mailbox, so it is never handed elsewhere.
    if auth == "microsoft" and (
        host.strip().lower() != microsoft.SMTP_HOST
        or port != microsoft.SMTP_PORT
        or config.get("use_tls", True) is not True
    ):
        raise ValueError(
            tr(
                "Microsoft 365 sign-in always sends through {smtp_host} on port {smtp_port} "
                "with TLS",
                smtp_host=microsoft.SMTP_HOST,
                smtp_port=microsoft.SMTP_PORT,
            )
        )


class ChannelCreate(BaseModel):
    kind: ChannelKind
    name: str = Field(min_length=1, max_length=100)
    config: dict[str, Any]
    active: bool = True

    @field_validator("config")
    @classmethod
    def _no_secrets_in_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _reject_secret_keys(value)

    @model_validator(mode="after")
    def _config_matches_kind(self) -> "ChannelCreate":
        validate_channel_config(self.kind, self.config)
        return self


class ChannelUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    config: dict[str, Any] | None = None
    active: bool | None = None

    @field_validator("config")
    @classmethod
    def _no_secrets_in_config(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return value
        return _reject_secret_keys(value)


class PasswordSignIn(BaseModel):
    """Username and password for an SMTP server. Vaulted as one JSON value."""

    username: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1)


class MicrosoftSignIn(BaseModel):
    """The Azure app registration an email channel sends with
    (client credentials, SMTP.SendAsApp). All three are vaulted together."""

    tenant_id: str = Field(min_length=1, max_length=200)
    client_id: str = Field(min_length=1, max_length=200)
    client_secret: str = Field(min_length=1)


EmailSignIn = PasswordSignIn | MicrosoftSignIn


class ChannelOut(BaseModel):
    """Carries no field capable of holding a credential -- a structural
    guarantee rather than a runtime filter (spec §9: no response ever
    contains a credential)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    kind: ChannelKind
    name: str
    config: dict[str, Any]
    active: bool
    has_credential: bool
    created_at: datetime


class RuleCreate(BaseModel):
    event_type: str = Field(max_length=100)
    channel_id: UUID
    recipient_kind: RecipientKind
    queue_id: UUID | None = None
    active: bool = True

    @field_validator("event_type")
    @classmethod
    def _known_event_type(cls, value: str) -> str:
        if value not in EVENT_TYPES:
            raise ValueError(tr("unknown event_type {value}", value=repr(value)))
        return value

    @model_validator(mode="after")
    def _no_staff_only_content_to_requester(self) -> "RuleCreate":
        # Spec §6.4. Rejected here, at write time, because staff-only events
        # have their own event types. A render-time filter would be one
        # template edit away from leaking staff discussion -- or a missed
        # contractual deadline -- to the external customer. Defence in depth:
        # the DB also carries ck_rule_no_internal_to_requester and
        # ck_rule_no_sla_breach_to_requester, but a raw IntegrityError is not
        # an acceptable user-facing error -- this validator is what actually
        # surfaces to the caller.
        #
        # The forbidden set lives in `app.core.events.STAFF_ONLY_EVENT_TYPES`,
        # not as a literal here, so this layer cannot drift from the service
        # layer's copy of the same rule.
        reason = staff_only_rejection(self.event_type, self.recipient_kind)
        if reason is not None:
            raise ValueError(reason)
        return self


class RuleUpdate(BaseModel):
    """Only `active` is editable after create (Task 10). `event_type`,
    `channel_id`, `recipient_kind`, and `queue_id` together define WHAT the
    rule routes and are immutable -- changing any of them is indistinguishable
    from creating a different rule, and letting `recipient_kind`/`event_type`
    drift post-create would reopen the §6.4 internal-note guard's write-time
    check against a row that already exists rather than one being inserted."""

    active: bool | None = None


class RuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    event_type: str
    channel_id: UUID
    recipient_kind: RecipientKind
    queue_id: UUID | None
    active: bool
    created_at: datetime


class AllowlistEntryCreate(BaseModel):
    host: str = Field(min_length=1, max_length=255)
    # Migration 0026: `port` is NOT NULL, server_default 443, and part of the
    # unique key (org_id, host, port) -- an admin allowlists a host AND a
    # port together, not a host and every port on it. Bounded to the legal
    # TCP range; there is no NULL-means-"any port" escape hatch.
    port: int = Field(default=443, ge=1, le=65535)
    note: str = Field(default="", max_length=200)

    @field_validator("host")
    @classmethod
    def _host_not_cidr(cls, value: str) -> str:
        # Spec §2.6: an entry names a HOST. Allowlisting a range is how an
        # allowlist quietly becomes a blanket permit.
        if "/" in value:
            raise ValueError(tr("allowlist entries name a host, not a CIDR range"))
        # Whole-branch review finding M10: normalise the SAME way
        # `app.core.egress.post_json` normalises the request's own `host`
        # before comparing against `allowlisted_hosts` (lowercase, strip a
        # trailing root dot -- DNS treats `gw.example.com.` and
        # `gw.example.com` as the exact same name, I1's fix). Before this
        # fix, an entry stored verbatim (`Mattermost.LAN`) could never match
        # the lowercase form `post_json` actually compares against -- fails
        # CLOSED (a usability wart, not a hole), but both sides of the
        # comparison must agree for the allowlist to do its job at all.
        return value.rstrip(".").lower()


class AllowlistEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    host: str
    port: int
    note: str
    created_at: datetime


class QueueMemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: UUID
    queue_id: UUID


class DeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    event_id: UUID
    channel_id: UUID
    recipient: str
    status: DeliveryStatus
    attempts: int
    last_error: str | None
    last_status_code: int | None
    next_attempt_at: datetime | None
    sent_at: datetime | None
    created_at: datetime


class DeliveryListResponse(BaseModel):
    """Pagination envelope matching `app.api.v1.audit`/`app.api.v1.jobs`'s
    convention (`items` + `total`) -- spec §9's "pagination matching the
    ticket core's convention" (this project's broader paginated-list
    convention, of which the ticket core's list endpoints are the
    non-paginated special case; audit/jobs are the shape an endpoint with a
    `total` count actually follows)."""

    items: list[DeliveryOut]
    total: int


class TestSendResult(BaseModel):
    delivered: bool
    status_code: int | None = None
    error: str | None = None
