"""Settings -> Email: the realm's SMTP configuration, edited from ITMS.

Spec `docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md` §2.1.

    GET  /api/v1/settings/email        orgs.manage
    PUT  /api/v1/settings/email        orgs.manage
    POST /api/v1/settings/email/test   orgs.manage

WHY THIS SURFACE EXISTS. Until now, "configure email" meant opening the
identity provider's admin console -- a second product with its own password,
its own vocabulary and its own idea of where things live. The operator lost an
afternoon to it. After this it is a tab in their own Settings, and the identity
provider is an appliance component they never have to have heard of.

THIS ROUTE IS THIN ON PURPOSE. `app/modules/user_admin/realm_config.py` owns the
conversation with the realm -- the merge that keeps the login theme, the
password that travels once and is never read back. What is here is the part that
has to be here: the permission, the tenant session, the audit record, and the
translation between ITMS's field names and the vendor's.

THE PASSWORD (invariant 3, and the ONE stated exception to "no password enters
the ITMS process")
-------------------------------------------------------------------------------
It arrives in the PUT body, is handed to the realm, and is then gone: not
stored, not vaulted, not logged, not returned. `EmailSettingsOut` has no field
that can carry it -- there is no key to forget to strip -- and
`tests/test_realm_settings_api.py` scans the RAW body of all three routes for
the literal rather than for the absence of a key, because a renamed field would
satisfy the weaker assertion while still shipping the value.

`password` is OPTIONAL on the way in, and that is not a convenience. The console
cannot send back a value it never received, so "absent" has to mean "leave the
stored one alone" -- otherwise the first operator to correct a display name
writes an empty string over a working credential and finds out when the next
invitation fails.

THE MICROSOFT SIGN-IN (`auth_type="token"`) follows the same rules. The relay
signs in with a Microsoft application's client id and secret, and the realm
itself exchanges them for a token (Keycloak 26.2 XOAUTH2), so this platform
never holds a token. The client secret travels once, inbound, exactly like the
password; `EmailSettingsOut` has no field for it. Switching modes CLEARS the
other mode's credential in the same write. Design:
`docs/superpowers/specs/2026-09-11-mail-oauth-design.md` §3.

WHAT THIS IS NOT. It is not credential CUSTODY. The vault governs credentials
this platform stores and later uses (invariant 3); a write-through field ITMS
never retains is configuration. The spec says this explicitly rather than
leaving the question to be re-argued here.

THE REALM IS SHARED BY EVERY TENANT, AND THAT IS STATED RATHER THAN HIDDEN.
There is one realm on this deployment, so these settings are platform-wide:
an `orgs.manage` holder in one organization changes the relay that every
organization's invitations go through. `orgs.manage` is what the spec chose,
and on the deployment this was written for (one tenant, the operator's own
appliance) it is the right gate. On a multi-tenant deployment it is not, and
the honest fix is a platform-level permission rather than a comment -- named
here so the next reader inherits the question rather than the assumption.

AUDIT (invariant 4). One `email.settings_updated` per accepted save and one
`email.test_requested` per test message, written inside the same tenant
transaction as the work. It records WHICH fields were supplied and none of their
values: the log is append-only and tamper-evident by design, so a value written
into it can never be redacted, and one of these fields is a password.

The realm write happens INSIDE that transaction and BEFORE the audit append, so
a refused write raises out of `org_session` and takes the audit row with it -- a
log saying a change happened when it did not is worse than no log. The one case
that cannot be made atomic is a commit failure AFTER the realm accepted the
write: the identity provider is not in this database's transaction and cannot
be. That window is named here because it is real, not because it is fixable.
"""

from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import AuthContext, require_permission
from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.identity.policy import resolve_console_domain
from app.core.tenancy import org_session
from app.modules.user_admin.api import keycloak_config
from app.modules.user_admin.kc_client import KeycloakAdminConfig, KeycloakAdminError
from app.modules.user_admin.realm_config import get_smtp, send_test_email, set_smtp

router = APIRouter(prefix="/settings", tags=["settings"])

MAX_HOST = 253
MAX_ADDRESS = 320
MAX_DISPLAY_NAME = 200
#: Long enough for any relay credential, short enough that the body is bounded.
MAX_PASSWORD = 512
#: Same bound and reasoning as MAX_PASSWORD, for an Azure client secret.
MAX_CLIENT_SECRET = 512
MAX_TOKEN_URL = 2048


def _provisioning(config: KeycloakAdminConfig | None) -> KeycloakAdminConfig:
    """`None` is a supported deployment state, not a crash.

    An operator who has not configured the provisioning client is told which
    setting is missing, with a 503 -- exactly as the user-lifecycle routes
    answer, because it is the same credential and the same absence.
    """
    if config is None:
        # The setting names -- `ITS_KEYCLOAK_ADMIN_URL` and
        # `ITS_KEYCLOAK_ADMIN_SECRET_PATH` -- are deliberately NOT in the
        # message: they carry the vendor's name inside the identifier, and a
        # console user reading them learns there is a second system they are
        # expected to understand (§1.3's buyer test). They stay named here,
        # accurately, where whoever configures the deployment is reading.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            tr(
                "email settings cannot be changed on this installation yet: its identity "
                "provisioning is not configured. That is a server setting, not something this "
                "screen can fix.",
            ),
        )
    return config


def _upstream(exc: httpx.HTTPStatusError) -> HTTPException:
    """The identity provider's STATUS, never its body.

    Its error bodies can echo the settings they were given, and one of those is
    the password. The status is what the console needs to tell "the relay
    refused" from "this platform may not write the realm"; the body would be a
    credential in a response, delivered by an error path.
    """
    return HTTPException(
        status.HTTP_502_BAD_GATEWAY,
        tr(
            "The identity provider refused that (HTTP {status_code}).",
            status_code=exc.response.status_code,
        ),
    )


# --------------------------------------------------------------------------- #
# models
# --------------------------------------------------------------------------- #


class EmailSettingsOut(BaseModel):
    """How this platform sends mail: the relay, the sender, and whether it is
    set up at all.

    NO FIELD HERE CAN CARRY A CREDENTIAL, and that is structural rather than
    careful: there is no password key to forget to strip. `authenticated` says
    only that a sign-in is configured.
    """

    configured: bool = Field(
        description=(
            "False when no relay is set, which is how this platform ships. While "
            "it is false no invitation can be delivered, and the Users tab says so."
        )
    )
    host: str = Field(description="The relay's hostname. Empty when nothing is set.")
    port: str = Field(
        description=(
            "The relay's port, as the identity provider stores it. Empty when "
            "nothing is set."
        )
    )
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
            "True when the relay requires a sign-in and one is configured. The "
            "stored secret itself is never returned by this API."
        )
    )
    auth_type: str = Field(
        description=(
            "`password` or `token`. `token` means the relay sign-in uses a "
            "Microsoft application (OAuth); the token is obtained by the sign-in "
            "service itself and this platform never holds it."
        )
    )
    # Named `microsoft_*`, not `token_*`: a response field whose name reads
    # as a secret is refused by the AI layer's scrubber (`core/secrets.py`).
    microsoft_client_id: str = Field(
        description=(
            "The application (client id) the Microsoft sign-in uses. Its secret "
            "is never returned by this API and has no field here."
        )
    )
    microsoft_sign_in_url: str = Field(
        description="Where the Microsoft sign-in fetches its access. Not a secret."
    )
    microsoft_scope: str = Field(
        description="What the Microsoft sign-in asks for access to. Not a secret."
    )
    suggested_test_recipient: str = Field(
        default="",
        description=(
            "Where the console proposes sending a test: the caller's own address "
            "with `+itms-test` before the @. The identity provider will not send "
            "a test to a sign-in account's own address, and a tagged one reaches "
            "the same inbox on Microsoft 365 and Gmail. Empty when the caller's "
            "sign-in carries no usable address."
        ),
    )


class TestSendOut(BaseModel):
    """What came of asking the identity provider to send a test message."""

    sent: bool = Field(
        description=(
            "True when the identity provider accepted the message for delivery. "
            "ACCEPTANCE IS NOT DELIVERY: a relay can accept a message and drop it "
            "silently, so the console names the mailbox to check rather than "
            "showing a tick."
        )
    )
    recipient: str | None = Field(
        default=None,
        description=(
            "The mailbox the test went to: the address asked for, or the saved "
            "sending address when none was."
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
            "The sending address, the caller's own address (a `+tag` is fine), "
            "or an address in the organization's console sign-in domain. "
            "Anything else is refused: this is not a way to send mail to "
            "arbitrary addresses."
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
    """`extra="forbid"`: a body carrying `smtpServer`, `org_id` or a stray key
    is a 422, not a silent no-op -- and nothing but these fields can reach the
    realm."""

    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1, max_length=MAX_HOST)
    port: int = Field(ge=1, le=65535)
    from_address: str = Field(min_length=3, max_length=MAX_ADDRESS)
    from_display_name: str = Field(default="", max_length=MAX_DISPLAY_NAME)
    reply_to: str = Field(default="", max_length=MAX_ADDRESS)
    encryption: Literal["none", "starttls", "ssl"]
    username: str = Field(default="", max_length=MAX_ADDRESS)
    # NO `max_length` HERE, AND THAT IS NOT AN OVERSIGHT. A field-level
    # constraint failure is reported by FastAPI with the offending INPUT
    # echoed into the 422 body -- which for this field would be the password.
    # The bound is enforced in the route instead, where the refusal can name
    # the rule without repeating the value. `_refuse_unusable` is the check.
    password: str | None = Field(
        default=None,
        description=(
            "Sent straight to the identity provider and never stored, logged or "
            "returned by this platform. OMIT IT to keep the one already saved -- "
            "the console cannot send back a value it was never given, so an "
            "absent field must not clear a working credential."
        ),
    )
    auth_type: Literal["password", "token"] = Field(
        default="password",
        description=(
            "How the relay sign-in works. Defaults to `password`, which is what "
            "every existing deployment sends."
        ),
    )
    token_url: str = Field(default="", max_length=MAX_TOKEN_URL)
    token_scope: str = Field(default="", max_length=500)
    token_client_id: str = Field(default="", max_length=200)
    # No `max_length`, for the same reason `password` has none: a field failure
    # echoes its input into the 422. Bounded in `_refuse_unusable`.
    token_client_secret: str | None = Field(
        default=None,
        description=(
            "Sent straight to the sign-in service and never stored, logged or "
            "returned by this platform. OMIT IT to keep the one already saved."
        ),
    )


# --------------------------------------------------------------------------- #
# translation -- ITMS field names <-> the realm's own
# --------------------------------------------------------------------------- #


def _refuse_unusable(body: EmailSettingsIn) -> None:
    """The body checks that are NOT expressed as field constraints.

    Both are here rather than on the model because a Pydantic failure is
    reported with the offending input echoed back in the 422 body, and for a
    MODEL-level rule that input is the whole body -- password included. Field
    constraints echo only their own field, which is why every other rule on
    this body is a field constraint and these two are not.

    1. A password with no username is REFUSED, not quietly dropped. No username
       means no relay sign-in and the save CLEARS the stored credential, so
       accepting a typed password would answer 200 to a request whose most
       sensitive field was discarded -- and the operator would believe they had
       just rotated it.
    2. The length bound, which would otherwise put the password in the 422.
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
    # The same two rules for the Microsoft sign-in, plus its required fields. A
    # value the save would discard is refused rather than answered with 200.
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


def _as_settings(values: dict[str, str], caller_email: str = "") -> EmailSettingsOut:
    host = values.get("host", "")
    return EmailSettingsOut(
        suggested_test_recipient=suggested_test_recipient(caller_email),
        configured=bool(host),
        host=host,
        port=values.get("port", ""),
        from_address=values.get("from", ""),
        from_display_name=values.get("fromDisplayName", ""),
        reply_to=values.get("replyTo", ""),
        encryption=(
            "ssl"
            if values.get("ssl") == "true"
            else "starttls"
            if values.get("starttls") == "true"
            else "none"
        ),
        username=values.get("user", ""),
        authenticated=values.get("auth") == "true",
        auth_type="token" if values.get("authType") == "token" else "password",
        microsoft_client_id=values.get("authTokenClientId", ""),
        microsoft_sign_in_url=values.get("authTokenUrl", ""),
        microsoft_scope=values.get("authTokenScope", ""),
    )


def _as_realm_block(body: EmailSettingsIn) -> dict[str, str]:
    """The realm's own keys, and only the ones this form owns.

    The TLS mode is one of three rather than two independent flags: a realm
    carrying `ssl` and `starttls` together leaves the relay to decide what was
    meant. Only the selected one is written, so switching modes REMOVES the
    other -- which is why `set_smtp` replaces the block instead of layering
    over it.

    Authentication follows the username: a relay sign-in with no account name
    is not a state worth being able to express, and a separate toggle would let
    the console save "authenticate, as nobody".
    """
    block: dict[str, str] = {
        "host": body.host,
        "port": str(body.port),
        "from": body.from_address,
    }
    if body.from_display_name:
        block["fromDisplayName"] = body.from_display_name
    if body.reply_to:
        block["replyTo"] = body.reply_to
    if body.encryption == "ssl":
        block["ssl"] = "true"
    elif body.encryption == "starttls":
        block["starttls"] = "true"
    # Every credential this form does not use is written EMPTY, never omitted:
    # `set_smtp` carries the stored value for an absent secret, which would
    # leave behind a credential nothing uses -- and nobody rotates.
    cleared_token = {
        "authTokenUrl": "",
        "authTokenScope": "",
        "authTokenClientId": "",
        "authTokenClientSecret": "",
    }
    if not body.username:
        # NO USERNAME MEANS NO SIGN-IN, so every stored credential is cleared.
        block["password"] = ""
        block["authType"] = "password"
        block.update(cleared_token)
        return block

    block["auth"] = "true"
    block["user"] = body.username
    if body.auth_type == "token":
        block["authType"] = "token"
        block["authTokenUrl"] = body.token_url
        block["authTokenScope"] = body.token_scope
        block["authTokenClientId"] = body.token_client_id
        if body.token_client_secret is not None:
            block["authTokenClientSecret"] = body.token_client_secret
        # One credential per relay: a realm holding a password AND a token
        # client leaves the relay to decide which was meant.
        block["password"] = ""
        return block

    block["authType"] = "password"
    block.update(cleared_token)
    if body.password is not None:
        block["password"] = body.password
    return block


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #


@router.get("/email", response_model=EmailSettingsOut)
async def read_email_settings(
    # ORDER MATTERS AND IT IS THE SECURITY HALF OF THIS SIGNATURE. FastAPI
    # resolves dependencies in declaration order and `keycloak_config` reads the
    # provisioning secret off disk, so the gate goes first on every route here.
    ctx: AuthContext = Depends(require_permission("orgs.manage")),
    config: KeycloakAdminConfig | None = Depends(keycloak_config),
) -> EmailSettingsOut:
    """How this platform sends mail today.

    `configured: false` for a deployment that has not set it up -- a state, not
    a 404. The console renders an empty form, and the Users tab shows the banner
    that stops an operator discovering the gap from a failed invitation.
    """
    try:
        values = await get_smtp(_provisioning(config))
    except httpx.HTTPStatusError as exc:
        raise _upstream(exc) from exc
    return _as_settings(values, ctx.claims.email)


@router.put("/email", response_model=EmailSettingsOut)
async def write_email_settings(
    body: EmailSettingsIn,
    request: Request,
    ctx: AuthContext = Depends(require_permission("orgs.manage")),
    config: KeycloakAdminConfig | None = Depends(keycloak_config),
) -> EmailSettingsOut:
    """Save the relay settings, and answer with what the identity provider
    holds afterwards -- never with what was typed.

    The answer is a fresh read for the same reason every form on this page
    re-reads: a value the identity provider normalised, or did not accept, must
    show up on screen rather than being echoed back and believed.
    """
    _refuse_unusable(body)
    provisioning = _provisioning(config)
    supplied = sorted(body.model_fields_set)
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            await set_smtp(provisioning, _as_realm_block(body))
        except httpx.HTTPStatusError as exc:
            # Raised INSIDE the transaction, so nothing below has run and no
            # audit row survives: the record and the change stand or fall
            # together.
            raise _upstream(exc) from exc
        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="email.settings_updated",
            target_type="email_settings",
            target_id="realm",
            # FIELD NAMES ONLY. `password` appearing here is a record that the
            # credential was rotated, which is exactly what an audit log is
            # for; its value is not, and never can be, because this log cannot
            # be redacted afterwards.
            metadata={"fields": supplied},
        )
        await session.flush()

    try:
        values = await get_smtp(provisioning)
    except httpx.HTTPStatusError as exc:
        raise _upstream(exc) from exc
    return _as_settings(values, ctx.claims.email)


@router.post("/email/test", response_model=TestSendOut)
async def send_test_message(
    request: Request,
    body: TestSendIn | None = None,
    ctx: AuthContext = Depends(require_permission("orgs.manage")),
    config: KeycloakAdminConfig | None = Depends(keycloak_config),
) -> TestSendOut:
    """Send a test message to the address asked for, or to the saved SENDING
    address when the body names none.

    The address must be the sending address, the caller's own (a `+tag` is
    fine), or one in the organization's console sign-in domain --
    `realm_config.send_test_email` enforces it, so this route cannot become a
    way to send the organization's mail to anyone. The
    caller's own sign-in address is refused by the identity provider (409), so
    the console proposes a tagged form of it.

    A success here means the relay ACCEPTED the message. It does not mean the
    message arrived, and the console says so -- a relay that accepts mail from
    an unknown sender and then drops it is the common case, and the one this
    platform's own runbook warns about.
    """
    provisioning = _provisioning(config)
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        try:
            recipient = await send_test_email(
                provisioning,
                body.recipient if body else None,
                caller_email=ctx.claims.email,
                org_domain=await resolve_console_domain(
                    session, ctx.org_id, request.app.state.settings
                ),
            )
        except KeycloakAdminError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            raise _upstream(exc) from exc
        await audit_service.record(
            session,
            org_id=ctx.org_id,
            actor_type="user",
            actor_id=ctx.user_id,
            action="email.test_requested",
            target_type="email_settings",
            target_id="realm",
            metadata={"recipient": recipient},
        )
        await session.flush()
    return TestSendOut(sent=True, recipient=recipient)
