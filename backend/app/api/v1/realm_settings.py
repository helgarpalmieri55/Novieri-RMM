"""Settings -> Email: how this organization sends its account email.

Design `docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §7
("Correo de cuentas"); the surface itself is from
`docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md` §2.1.

    GET  /api/v1/settings/email        orgs.manage
    PUT  /api/v1/settings/email        orgs.manage, on every client
    POST /api/v1/settings/email/test   orgs.manage, on every client

WHAT CHANGED ON 2026-11-04. These settings used to be written into the
identity provider's realm, which held the relay password and did the sending.
The platform now has its own sign-in and sends its own invitations and resets,
so the settings are one `account_mail_settings` row per organization and the
relay's password or Microsoft client secret is a vault credential
(`app/core/mail.py`, invariant 3). The wire shape is unchanged.

THIS ROUTE IS THIN ON PURPOSE. `app/core/mail.py` owns the storage, the vault
and the sending. What is here is the part that has to be here: the
permission, the tenant session, the audit record, and the body rules that
must not be field constraints.

THE PASSWORD (invariant 3)
--------------------------
It arrives in the PUT body, goes to the vault, and is never logged or
returned. `EmailSettingsOut` has no field that can carry it -- there is no key
to forget to strip -- and `tests/test_realm_settings_api.py` scans the RAW body
of all three routes for the literal rather than for the absence of a key,
because a renamed field would satisfy the weaker assertion while still
shipping the value.

`password` is OPTIONAL on the way in, and that is not a convenience. The
console cannot send back a value it never received, so "absent" has to mean
"leave the stored one alone" -- otherwise the first operator to correct a
display name writes an empty string over a working credential and finds out
when the next invitation fails.

THE MICROSOFT SIGN-IN (`auth_type="token"`) follows the same rules for its
client secret. The platform now performs the client-credentials exchange
itself, so the token URL is PINNED to Microsoft's own endpoint
(`mail.microsoft_tenant`): a configurable URL would be a place to send the
client secret to anyone. Switching modes CLEARS the other mode's credential in
the same write (`docs/superpowers/specs/2026-09-11-mail-oauth-design.md` §3).

ONE ORGANIZATION'S SETTINGS. The realm was shared by every tenant; this row is
not. Writing it still needs `orgs.manage` on every client: one relay serves
every client of the organization (MSP clients spec §1.4).

AUDIT (invariant 4). One `email.settings_updated` per accepted save, recording
WHICH fields were supplied and none of their values (the log is append-only;
a value written into it can never be redacted, and one of these fields is a
password); one `email.test_requested` per test message; and one
`email.credential_used` for every vault checkout (`app/core/mail.py`). The save
and its record are one transaction.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import AuthContext, require_permission, require_permission_on_every_client
from app.core import rate_limit
from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.identity.policy import resolve_console_domain
from app.core.mail import (
    MailNotConfigured,
    MailRecipientRefused,
    MailSendFailed,
    MailSettingsInput,
    failure_message,
    microsoft_tenant,
    pick_test_recipient,
    read_settings,
    save_settings,
    send_account_mail,
)
from app.core.tenancy import org_session
from app.models.auth import AccountMailSettings

router = APIRouter(prefix="/settings", tags=["settings"])

MAX_HOST = 253
MAX_ADDRESS = 320
MAX_DISPLAY_NAME = 200
#: Long enough for any relay credential, short enough that the body is bounded.
MAX_PASSWORD = 512
#: Same bound and reasoning as MAX_PASSWORD, for an Azure client secret.
MAX_CLIENT_SECRET = 512
MAX_TOKEN_URL = 2048
#: Test messages one organization may send in a minute: enough to retry after
#: fixing a setting, too few to make this button a way to send mail in bulk --
#: the figure and the reason of `notification/api.py`'s test send.
TEST_SENDS_PER_MINUTE = 5

#: No control character in a field that reaches a mail header:
#: `email.policy.default` refuses a CR or LF by raising deep inside the send
#: (`app/modules/email_intake/identity.py` tells that story). None of these
#: fields is a secret, so a field failure echoing its input is acceptable.
_NO_CONTROL = r"^[^\x00-\x1f\x7f]*$"
#: The same, and no whitespace at all: a hostname or an address has none.
_NO_SPACE = r"^[^\s\x00-\x1f\x7f]*$"


# --------------------------------------------------------------------------- #
# models
# --------------------------------------------------------------------------- #


class EmailSettingsOut(BaseModel):
    """How this organization sends mail: the relay, the sender, and whether it
    is set up at all.

    NO FIELD HERE CAN CARRY A CREDENTIAL, and that is structural rather than
    careful: there is no password key to forget to strip. `authenticated` says
    only that a sign-in is configured.
    """

    configured: bool = Field(
        description=(
            "False when no relay is set, which is how a new organization starts. While it "
            "is false no invitation can be delivered, and the Users tab says so."
        )
    )
    host: str = Field(description="The relay's hostname. Empty when nothing is set.")
    port: str = Field(description="The relay's port. Empty when nothing is set.")
    from_address: str = Field(
        description="The address invitations and password-reset mails are sent from."
    )
    from_display_name: str = Field(description="The sender name a recipient sees.")
    reply_to: str = Field(description="Where replies go, if that differs from the sender.")
    encryption: str = Field(
        description="`none`, `starttls` or `ssl` -- how the connection to the relay is secured."
    )
    username: str = Field(description="The account this platform signs in to the relay as.")
    authenticated: bool = Field(
        description=(
            "True when the relay requires a sign-in and one is saved. The stored secret "
            "itself is never returned by this API."
        )
    )
    auth_type: str = Field(
        description=(
            "`password` or `token`. `token` means the relay sign-in uses a Microsoft 365 "
            "application; this platform obtains a token for each message and never stores it."
        )
    )
    # Named `microsoft_*`, not `token_*`: a response field whose name reads
    # as a secret is refused by the AI layer's scrubber (`core/secrets.py`).
    microsoft_client_id: str = Field(
        description=(
            "The application (client id) the Microsoft sign-in uses. Its secret is never "
            "returned by this API and has no field here."
        )
    )
    microsoft_sign_in_url: str = Field(
        description="Microsoft's token address the sign-in uses. Not a secret."
    )
    microsoft_scope: str = Field(
        description="What the Microsoft sign-in asks for access to. Not a secret."
    )
    suggested_test_recipient: str = Field(
        default="",
        description=(
            "Where the console proposes sending a test: the caller's own address with "
            "`+itms-test` before the @, which reaches the same inbox on Microsoft 365 and "
            "Gmail and is easy to find there. Empty when the caller's sign-in carries no "
            "usable address."
        ),
    )


class TestSendOut(BaseModel):
    """What came of sending a test message."""

    sent: bool = Field(
        description=(
            "True when the relay accepted the message for delivery. ACCEPTANCE IS NOT "
            "DELIVERY: a relay can accept a message and drop it silently, so the console "
            "names the mailbox to check rather than showing a tick."
        )
    )
    recipient: str | None = Field(
        default=None,
        description=(
            "The mailbox the test went to: the address asked for, or the saved sending "
            "address when none was."
        ),
    )


class TestSendIn(BaseModel):
    """Where to send the test. `extra="forbid"`, and the body is optional: an
    empty POST still sends to the saved sending address."""

    model_config = ConfigDict(extra="forbid")

    recipient: str | None = Field(
        default=None,
        max_length=254,
        description=(
            "The sending address, the caller's own address (a `+tag` is fine), or an "
            "address in the organization's console sign-in domain. Anything else is "
            "refused: this is not a way to send mail to arbitrary addresses."
        ),
    )


def suggested_test_recipient(email: str) -> str:
    """`name+itms-test@domain` for the caller's own address, or "" if it is not
    one plausible address. An existing tag is replaced, not stacked."""
    if email.count("@") != 1:
        return ""
    local, domain = email.strip().split("@")
    local = local.split("+", 1)[0]
    if not local or "." not in domain:
        return ""
    return f"{local}+itms-test@{domain.lower()}"


class EmailSettingsIn(BaseModel):
    """`extra="forbid"`: a body carrying `smtpServer`, `org_id` or a stray key is
    a 422, not a silent no-op."""

    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1, max_length=MAX_HOST, pattern=_NO_SPACE)
    port: int = Field(ge=1, le=65535)
    from_address: str = Field(min_length=3, max_length=MAX_ADDRESS, pattern=_NO_SPACE)
    from_display_name: str = Field(default="", max_length=MAX_DISPLAY_NAME, pattern=_NO_CONTROL)
    reply_to: str = Field(default="", max_length=MAX_ADDRESS, pattern=_NO_SPACE)
    encryption: Literal["none", "starttls", "ssl"]
    username: str = Field(default="", max_length=MAX_ADDRESS, pattern=_NO_SPACE)
    # NO `max_length` OR `pattern` HERE, AND THAT IS NOT AN OVERSIGHT. A
    # field-level constraint failure is reported by FastAPI with the offending
    # INPUT echoed into the 422 body -- which for this field would be the
    # password. The bound is enforced in `_refuse_unusable` instead, where the
    # refusal can name the rule without repeating the value.
    password: str | None = Field(
        default=None,
        description=(
            "Stored in the vault and never logged or returned by this platform. OMIT IT to "
            "keep the one already saved -- the console cannot send back a value it was never "
            "given, so an absent field must not clear a working credential."
        ),
    )
    auth_type: Literal["password", "token"] = Field(
        default="password",
        description="How the relay sign-in works. Defaults to `password`.",
    )
    token_url: str = Field(default="", max_length=MAX_TOKEN_URL, pattern=_NO_SPACE)
    token_scope: str = Field(default="", max_length=500, pattern=_NO_SPACE)
    token_client_id: str = Field(default="", max_length=200, pattern=_NO_SPACE)
    # No `max_length`, for the same reason `password` has none. Bounded in
    # `_refuse_unusable`.
    token_client_secret: str | None = Field(
        default=None,
        description=(
            "Stored in the vault and never logged or returned by this platform. OMIT IT to "
            "keep the one already saved."
        ),
    )


# --------------------------------------------------------------------------- #
# body rules and translation
# --------------------------------------------------------------------------- #


def _refuse_unusable(body: EmailSettingsIn) -> None:
    """The body checks that are NOT expressed as field constraints, because a
    model-level Pydantic failure echoes the WHOLE body, password included.

    1. A password with no username is REFUSED, not quietly dropped: no username
       means no sign-in and the save CLEARS the stored credential, so accepting
       a typed password would answer 200 to a request whose most sensitive
       field was discarded.
    2. The length bounds, which would otherwise put the secret in the 422.
    3. A Microsoft sign-in's required fields, and its token URL pinned to
       Microsoft's own endpoint (the platform presents the secret there).
    """
    if body.password is not None:
        if not body.username:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                tr(
                    "a relay password needs a sign-in username; leave both blank for a relay "
                    "that needs no sign-in",
                ),
            )
        if len(body.password) > MAX_PASSWORD:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                tr(
                    "the relay password may be at most {max_password} characters",
                    max_password=MAX_PASSWORD,
                ),
            )
    if body.auth_type == "token":
        if not body.username:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                tr("a Microsoft sign-in needs a username: the mailbox the relay sends as"),
            )
        if not body.token_client_id:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                tr(
                    "a Microsoft sign-in needs an application (client id); choose Password "
                    "instead for a relay that signs in with one",
                ),
            )
        if not body.token_url:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                tr("a Microsoft sign-in needs a token URL"),
            )
        if microsoft_tenant(body.token_url) is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                tr(
                    "a Microsoft sign-in's token URL must be Microsoft's own: "
                    "https://login.microsoftonline.com/<directory>/oauth2/v2.0/token"
                ),
            )
        if body.password is not None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                tr(
                    "a Microsoft sign-in does not use a relay password; the saved one is "
                    "removed when this is saved",
                ),
            )
    if body.token_client_secret is not None:
        if body.auth_type != "token" or not body.token_client_id:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                tr(
                    "a client secret is only used by a Microsoft sign-in, and needs an "
                    "application (client id)",
                ),
            )
        if len(body.token_client_secret) > MAX_CLIENT_SECRET:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                tr(
                    "the client secret may be at most {max_client_secret} characters",
                    max_client_secret=MAX_CLIENT_SECRET,
                ),
            )


def _values(body: EmailSettingsIn) -> MailSettingsInput:
    return MailSettingsInput(
        host=body.host,
        port=body.port,
        from_address=body.from_address,
        from_display_name=body.from_display_name,
        reply_to=body.reply_to,
        encryption=body.encryption,
        username=body.username,
        auth_type=body.auth_type,
        token_url=body.token_url,
        token_scope=body.token_scope,
        token_client_id=body.token_client_id,
    )


def _as_settings(row: AccountMailSettings | None, caller_email: str = "") -> EmailSettingsOut:
    suggestion = suggested_test_recipient(caller_email)
    if row is None:
        return EmailSettingsOut(
            configured=False,
            host="",
            port="",
            from_address="",
            from_display_name="",
            reply_to="",
            encryption="none",
            username="",
            authenticated=False,
            auth_type="password",
            microsoft_client_id="",
            microsoft_sign_in_url="",
            microsoft_scope="",
            suggested_test_recipient=suggestion,
        )
    return EmailSettingsOut(
        configured=bool(row.host),
        host=row.host,
        port=str(row.port),
        from_address=row.from_address,
        from_display_name=row.from_display_name,
        reply_to=row.reply_to,
        encryption=row.encryption,
        username=row.username,
        authenticated=bool(row.username) and row.credential_id is not None,
        auth_type="token" if row.auth_type == "token" else "password",
        microsoft_client_id=row.token_client_id,
        microsoft_sign_in_url=row.token_url,
        microsoft_scope=row.token_scope,
        suggested_test_recipient=suggestion,
    )


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #


@router.get("/email", response_model=EmailSettingsOut)
async def read_email_settings(
    request: Request,
    ctx: AuthContext = Depends(require_permission("orgs.manage")),
) -> EmailSettingsOut:
    """How this organization sends mail today.

    `configured: false` for an organization that has not set it up -- a state,
    not a 404. Never opens the vault: `authenticated` is read off the row.
    """
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        return _as_settings(await read_settings(session, ctx.org_id), ctx.claims.email)


@router.put("/email", response_model=EmailSettingsOut)
async def write_email_settings(
    body: EmailSettingsIn,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("orgs.manage")),
) -> EmailSettingsOut:
    """Save the relay settings, and answer with what is stored afterwards --
    never with what was typed (a fresh read, in its own session)."""
    _refuse_unusable(body)
    state = request.app.state
    supplied = sorted(body.model_fields_set)
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        await save_settings(
            session,
            state.vault,
            org_id=ctx.org_id,
            actor_id=ctx.user_id,
            values=_values(body),
            password=body.password,
            client_secret=body.token_client_secret,
        )
        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="email.settings_updated",
            target_type="email_settings",
            target_id="account_mail",
            # FIELD NAMES ONLY. `password` appearing here is a record that the
            # credential was rotated; its value never can be.
            metadata={"fields": supplied},
        )
        await session.flush()

    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        return _as_settings(await read_settings(session, ctx.org_id), ctx.claims.email)


@router.post("/email/test", response_model=TestSendOut)
async def send_test_message(
    request: Request,
    body: TestSendIn | None = None,
    ctx: AuthContext = Depends(require_permission_on_every_client("orgs.manage")),
) -> TestSendOut:
    """Send a test message through the saved relay to the address asked for, or
    to the saved SENDING address when the body names none.

    The address must be the sending address, the caller's own (a `+tag` is
    fine), or one in the organization's console sign-in domain
    (`mail.pick_test_recipient`), so this route cannot become a way to send the
    organization's mail to anyone.

    A FAILED SEND IS CAUGHT INSIDE THE SESSION and refused after it: the vault
    checkout that signed in to the relay has already been audited in this
    session, and that record must survive the refusal (`app/core/mail.py`).

    A success means the relay ACCEPTED the message, not that it arrived.
    """
    state = request.app.state
    if not await rate_limit.hit(
        state.redis, f"email-test:org:{ctx.org_id}", limit=TEST_SENDS_PER_MINUTE, window_s=60
    ):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            tr("too many test messages for this organization; try again in a minute"),
        )
    failure: MailSendFailed | None = None
    async with org_session(ctx.org_id, engine=state.app_engine, clients=ctx.clients) as session:
        saved = await read_settings(session, ctx.org_id)
        if saved is None or not saved.host:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                tr("no email settings are saved yet, so there is nothing to test"),
            )
        try:
            recipient = pick_test_recipient(
                body.recipient if body else None,
                sender=saved.from_address,
                caller_email=ctx.claims.email,
                org_domain=await resolve_console_domain(session, ctx.org_id, state.settings),
            )
        except MailRecipientRefused as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        try:
            await send_account_mail(
                session,
                org_id=ctx.org_id,
                to=recipient,
                subject=tr("ITMS test message"),
                text=tr(
                    "This is a test message from ITMS. It reached you, so this organization's "
                    "account email (invitations and password resets) is working."
                ),
                vault=state.vault,
                actor_id=ctx.user_id,
            )
        except MailNotConfigured as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                tr("no email settings are saved yet, so there is nothing to test"),
            ) from exc
        except MailSendFailed as exc:
            failure = exc
        else:
            await audit_service.record(
                session,
                org_id=ctx.org_id,
                actor_type="user",
                actor_id=ctx.user_id,
                action="email.test_requested",
                target_type="email_settings",
                target_id="account_mail",
                metadata={"recipient": recipient},
            )
        await session.flush()
    if failure is not None:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, failure_message(failure.reason))
    return TestSendOut(sent=True, recipient=recipient)
