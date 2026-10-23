"""Request/response models for email intake (spec §10).

NO RESPONSE MODEL HERE CARRIES A PASSWORD OR A CREDENTIAL ID (invariant #3,
spec §10). `MailboxOut` exposes `has_credential: bool` -- the FACT that a
credential is configured, which an admin UI needs, and nothing more.

`has_credential` is false only for a Microsoft mailbox nobody has connected
yet (spec §2.1 amendment A5); a password mailbox always has one. It lets the
admin UI say "Not connected" without being handed an id it must never see.
What is worth asserting is the field set of the response -- see
`test_no_response_contains_the_password_or_a_credential_id`.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

_MAX_ADDRESS = 320
_MAX_HOST = 255

Disposition = Literal[
    "created",
    "commented",
    "reopened",
    "quarantined",
    "refused_loop",
    "refused_rate",
    "refused_parse",
]

#: How a mailbox signs in to its IMAP server.
AuthKind = Literal["password", "microsoft_oauth"]


class MailboxCreate(BaseModel):
    address: str = Field(max_length=_MAX_ADDRESS)
    host: str = Field(max_length=_MAX_HOST)
    port: int = Field(ge=1, le=65535)
    use_ssl: bool = True
    folder: str = Field(default="INBOX", max_length=100)
    username: str = Field(max_length=_MAX_ADDRESS)
    auth_kind: AuthKind = "password"
    # Required for `password`, refused for `microsoft_oauth` -- checked in
    # `mailboxes.create_mailbox`, not here, so a refusal never echoes it.
    password: str | None = Field(default=None, min_length=1, max_length=1024)
    default_category_id: UUID
    reopen_window_days: int = Field(default=14, ge=1, le=3650)
    # A Microsoft mailbox is always created switched off: it has nothing to
    # sign in with until someone connects it.
    active: bool = True


class MailboxUpdate(BaseModel):
    address: str | None = Field(default=None, max_length=_MAX_ADDRESS)
    host: str | None = Field(default=None, max_length=_MAX_HOST)
    port: int | None = Field(default=None, ge=1, le=65535)
    use_ssl: bool | None = None
    folder: str | None = Field(default=None, max_length=100)
    username: str | None = Field(default=None, max_length=_MAX_ADDRESS)
    # Switching to `microsoft_oauth` drops the saved password and switches the
    # mailbox off until it is connected; switching back needs `password`.
    auth_kind: AuthKind | None = None
    # None means "leave the stored password alone" -- it does NOT mean "clear
    # it". A password is refused on a Microsoft mailbox.
    password: str | None = Field(default=None, min_length=1, max_length=1024)
    default_category_id: UUID | None = None
    reopen_window_days: int | None = Field(default=None, ge=1, le=3650)
    active: bool | None = None


class MailboxOut(BaseModel):
    # `from_attributes` so this CAN be built from an `EmailMailbox` row.
    # `api._mailbox_out` deliberately does not: `has_credential` is not a
    # column, Pydantic 2.13's `model_validate` has no `update=` parameter to
    # inject it with, and enumerating the fields by hand is what makes it
    # structurally obvious that `credential_id` is not among them
    # (invariant #3).
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    address: str
    host: str
    port: int
    use_ssl: bool
    folder: str
    username: str
    has_credential: bool
    #: `password` or `microsoft_oauth`.
    auth_kind: str
    #: `ok` or `needs_reconsent`. The latter is a state with an action
    #: (Reconnect), not an error, and the console renders it as one.
    credential_state: str
    default_category_id: UUID
    reopen_window_days: int
    active: bool
    last_polled_at: datetime | None
    last_error: str | None
    created_at: datetime


class InboundMessageOut(BaseModel):
    id: UUID
    mailbox_id: UUID
    message_id: str
    from_address: str
    subject: str
    # THE SENDER'S OWN `Date:` HEADER, and therefore attacker-chosen. Shown
    # because it is genuinely useful and because a wild value is itself
    # diagnostic -- but it decides NOTHING: `messages.list_messages` filters
    # and orders on `created_at`, which is when this platform recorded the
    # row. See that module's docstring for what using `received_at` for
    # either one allows a sender to do.
    received_at: datetime
    created_at: datetime
    # `str`, not the `Disposition` literal, DELIBERATELY: the check
    # constraint is the authority for the vocabulary, and validating it again
    # here would let one off-vocabulary row 500 the whole page -- destroying
    # the operator's only diagnosis surface exactly when the data is wrong.
    # `Disposition` still validates the QUERY FILTER, which is untrusted
    # input; this is trusted output. See `messages._message_out`.
    disposition: str
    reason: str
    ticket_id: UUID | None
    # Deliberately exposed to `email_intake.read` holders (spec §10): this is
    # how an operator diagnoses a quarantined message. It is the PARSED text,
    # never the raw RFC822 message, and on the LIST it is an excerpt --
    # `messages.BODY_EXCERPT_CHARS` explains why and `body_truncated` says
    # when something was cut.
    body_text: str | None
    body_truncated: bool = False


class MessageLogOut(BaseModel):
    messages: list[InboundMessageOut]
    # The extent of the LIST -- every filter, `disposition` included -- so a
    # caller can page. Without it `limit`/`offset` are unusable: there is no
    # way to know whether another page exists.
    total: int
    # Quarantined mail is mail a human expected to become a ticket and which
    # did not. It is invisible unless surfaced (spec §10). Counted over the
    # whole SCOPE and never over the returned page, and -- unlike `total` --
    # NOT narrowed by `disposition`; see `messages.list_messages` for why the
    # two counts deliberately answer different questions.
    quarantined_count: int


# The `/mailboxes/{id}/test` result (spec §10). `detail` is a CONSTRUCTED
# classification -- `imap.py`'s `stage` + `reason`, both drawn from fixed
# vocabularies written in that file -- never `str(exc)` and never a server
# response line, either of which can embed the credential that was refused
# (invariant #3, spec §11.12). The bound is belt-and-braces on top of that.
MAX_TEST_DETAIL = 200


class MailboxTestResult(BaseModel):
    ok: bool
    detail: str = Field(max_length=MAX_TEST_DETAIL)


# --------------------------------------------------------------------------- #
# Microsoft 365 sign-in (docs/superpowers/specs/2026-09-11-mail-oauth-design.md)
# --------------------------------------------------------------------------- #


class OAuthAppUpdate(BaseModel):
    """The org's Azure application. `extra="forbid"`: a stray key is a 422."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=200)
    client_id: str = Field(min_length=1, max_length=200)
    # No `max_length`: a field failure echoes its input into the 422, and this
    # input is the secret. The route bounds it without repeating it.
    client_secret: str | None = Field(default=None)


class OAuthAppOut(BaseModel):
    """No field here can carry the client secret."""

    tenant_id: str
    client_id: str
    has_secret: bool
    redirect_uri: str | None = Field(
        description=(
            "Register this exact address as a redirect URI on the Azure "
            "application. Null when the server's console address is not set, "
            "in which case Connect with Microsoft is unavailable."
        )
    )


class OAuthStartOut(BaseModel):
    authorize_url: str = Field(
        description="Send the browser here. Carries `state` and the PKCE challenge."
    )


class OAuthCompleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1, max_length=4096)


class OAuthAbandonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str = Field(min_length=1, max_length=64)
