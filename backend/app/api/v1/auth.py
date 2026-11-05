"""The platform's own sign-in: `/api/v1/auth/*` and `/api/v1/me/*` (P2 phase 1
design §2, §6.4).

    POST   /auth/login                      the password       -> factor | enrol | signed in
    POST   /auth/factor/{totp,recovery}     the second factor  -> signed in
    POST   /auth/factor/passkey/options     the WebAuthn request options
    POST   /auth/factor/passkey             the assertion      -> signed in
    POST   /auth/enroll/totp/start          the otpauth:// URI, once
    POST   /auth/enroll/totp/confirm        a code from it
    POST   /auth/enroll/passkey/options     the WebAuthn creation options
    POST   /auth/enroll/passkey             the attestation
    POST   /auth/enroll/finish              the ten recovery codes, once  -> signed in
    POST   /auth/step-up/{totp,recovery}    the recent confirmation (design §2.5)
    POST   /auth/step-up/passkey/options
    POST   /auth/step-up/passkey
    POST   /auth/logout, /auth/logout-everywhere
    POST   /auth/password/forgot            always 202
    POST   /auth/password/reset             a one-time link
    POST   /auth/invite/details             who a one-time invitation is for
    POST   /auth/invite/accept              its password -> the first factor
    POST   /auth/recover/details            who the server's recovery link is for
    POST   /auth/recover/accept             the factors go -> a new first factor
    GET    /me/security                     the caller's factors, devices, sessions
    PUT    /me/password
    DELETE /me/passkeys/{id}, /me/totp, /me/trusted-devices/{id}, /me/sessions/{id}
    POST   /me/recovery-codes

UNGATED BY NECESSITY, every one (`tests/test_route_gating.py::UNGATED_BY_DESIGN`):
the `/auth` routes answer before there is a person to hold a permission, and
the `/me` ones act on the caller's own account, where a code would be a code
every role must hold. What stands in for the gate is the session each step
needs (`app.core.auth.guards`), Task 6's CSRF rule over all of `/api/v1/auth/`
(cookie or not: login CSRF), per-IP limits, and per-account, per-factor
counters (Task 4).

NOT MCP TOOLS (`app/mcp/exclusions.py`): each takes or returns a credential,
or needs the console session the MCP channel never has.

THE HANDLERS ARE THIN: the rules live in `app.core.auth.flows` and
`app.core.auth.account`, which hold the every-client scope a route file may
not (`ALL_SCOPE_ALLOWLIST`).
"""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import AuthContext, bound_auth_context, require_step_up
from app.core.auth import account, flows
from app.core.auth.guards import session_of, sign_in_again
from app.core.auth.refusals import AuthRefusal
from app.core.auth.sessions import (
    SessionPrincipal,
    clear_session_cookie,
    set_device_cookie,
    set_session_cookie,
)
from app.core.i18n import locale_var, tr
from app.core.rate_limit import hit, ip_key

router = APIRouter(prefix="/auth", tags=["auth"])
me_router = APIRouter(prefix="/me", tags=["me"])


def _limit(bucket: str, limit: int, window_s: int) -> Callable[[Request], Awaitable[None]]:
    """At most `limit` calls per `window_s` from one address (Task 3's limiter).
    It fails open when Redis does, because each sits on a control that does
    not need Redis: the per-account counters in the database for the password
    and the factors (design §2.3), the per-address mail cap -- which fails
    CLOSED (`flows.send_reset_link`) -- behind `_FORGOT`, and a 256-bit secret
    behind `_LINK`."""

    async def within(request: Request) -> None:
        if not await hit(
            request.app.state.redis, ip_key(request, bucket), limit=limit, window_s=window_s
        ):
            refusal = AuthRefusal(
                status.HTTP_429_TOO_MANY_REQUESTS,
                code="rate_limited",
                detail=tr("too many attempts from this network; try again in a few minutes"),
                extra={"wait_s": window_s},
            )
            refusal.headers = {"Retry-After": str(window_s)}
            raise refusal

    return within


_LOGIN = _limit("auth-login", 30, 600)
_FACTOR = _limit("auth-factor", 30, 600)
_FORGOT = _limit("auth-forgot", 5, 3600)
_LINK = _limit("auth-link", 20, 600)


# --------------------------------------------------------------------------- #
# models -- every request body `extra="forbid"`
#
# A FIELD THAT CARRIES A SECRET -- a password, a TOTP code, a recovery code, a
# one-time link's token -- IS A PLAIN `str` (plan decision 16). A Pydantic
# constraint that fails puts the field's INPUT in the 422 body, and the input
# would be the secret (Task 3's
# `test_an_over_long_password_is_refused_without_repeating_it`: "The bound
# lives in the route, not on the field"). The flows bound them instead:
# `flows.login` and `account.change_password` hold a password to verify to
# `staff_password.SUBMITTED_MAX` (`invalid_credentials`); a new password is
# `check_new_password`'s (`password_rejected`, `too_short`/`too_long`); a code
# goes to Task 4's `verify_totp`/`confirm_secret` (spaces dropped, six digits)
# or `recovery.normalise`, and any other shape is `factor_failed`; a link's
# token goes to `action_tokens.org_of`, and any other shape is `invalid_link`.
# --------------------------------------------------------------------------- #


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=320)
    password: str


class CodeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    remember_device: bool = False


class RecoveryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    remember_device: bool = False


class PasskeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credential: dict[str, Any]
    remember_device: bool = False


class StepUpCodeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str


class StepUpRecoveryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str


class StepUpPasskeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credential: dict[str, Any]


class EnrollCodeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str


class EnrollPasskeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credential: dict[str, Any]
    name: str = Field(min_length=1, max_length=100)


class ForgotIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=320)


class ResetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # The one-time token is a secret like the password: no Pydantic bound.
    # `action_tokens.org_of` refuses anything not shaped like a link it issued,
    # and a dead link of any kind is the one 410 `invalid_link`.
    token: str
    password: str


class PasswordChangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current: str
    new: str


class LinkTokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # A one-time link's token: a secret, so no Pydantic bound, as `ResetIn`.
    # `action_tokens.org_of` refuses any other shape, and that is the 410.
    token: str


class InviteAcceptIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    #: No Pydantic bound on either (Task 7's decision 16): a field failure
    #: repeats its input in the 422, and both are secrets.
    #: `action_tokens.org_of` bounds the token, `staff_password.require_acceptable`
    #: the password.
    token: str
    password: str


class InviteOut(BaseModel):
    """Who an invitation is for, for its page."""

    email: str
    display_name: str


class RecoveryOut(BaseModel):
    """Who a recovery link is for, for its page."""

    email: str


class SignInOut(BaseModel):
    """Where the sign-in stands. The session itself travels in the cookie."""

    state: Literal["factor_required", "enroll_required", "signed_in"]
    methods: list[str] | None = Field(
        default=None,
        description="With `factor_required`: the factors this account has, passkey first.",
    )
    remember_device_days: int | None = Field(
        default=None,
        description=(
            "With `factor_required`: the days 'remember this device' would last; null when "
            "this organization does not remember devices."
        ),
    )


class TotpStartOut(BaseModel):
    """The authenticator app's setup URI. Shown once; never stored in clear."""

    otpauth_uri: str


class RecoveryCodesOut(BaseModel):
    """Ten single-use codes. Shown once; only their hashes are kept."""

    recovery_codes: list[str]


class PasskeyOut(BaseModel):
    """One of the caller's passkeys. Never its key material."""

    id: UUID
    name: str
    created_at: datetime
    last_used_at: datetime | None
    backup_state: bool = Field(description="True for a passkey a password manager syncs.")


class TrustedDeviceOut(BaseModel):
    """A browser that signs in without the second factor until it expires."""

    id: UUID
    label: str
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    current: bool = Field(description="True for the browser making this request.")


class SessionOut(BaseModel):
    """One of the caller's open console sessions."""

    id: UUID
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    user_agent: str
    ip: str
    current: bool = Field(description="True for the session making this request.")


class MySecurityOut(BaseModel):
    """The caller's own second factors, remembered browsers and sessions."""

    has_totp: bool
    passkeys: list[PasskeyOut]
    recovery_codes_left: int
    trusted_devices: list[TrustedDeviceOut]
    sessions: list[SessionOut]


def _answer(response: Response, step: flows.SignIn) -> SignInOut:
    """The cookies through Task 6's helpers (`__Host-`, `Secure`, `HttpOnly`,
    `SameSite=Strict`, expiring with the session or the device)."""
    if step.session_token and step.expires_at:
        set_session_cookie(response, step.session_token, expires_at=step.expires_at)
    if step.device_token and step.device_expires_at:
        set_device_cookie(response, step.device_token, expires_at=step.device_expires_at)
    if step.state == "factor_required":
        # Set explicitly, null included: `response_model_exclude_unset` keeps
        # them, and leaves them out of every other state (decision 15).
        return SignInOut(
            state=step.state,
            methods=list(step.methods),
            remember_device_days=step.remember_device_days,
        )
    return SignInOut(state=step.state)


def _no_content() -> Response:
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# /auth -- the password and the second factor
# --------------------------------------------------------------------------- #


@router.post(
    "/login",
    response_model=SignInOut,
    response_model_exclude_unset=True,
    dependencies=[Depends(_LOGIN)],
)
async def login(body: LoginIn, request: Request, response: Response) -> SignInOut:
    """The address and the password. One answer for every refusal (design §2.3)."""
    step = await flows.login(request, email=body.email, password=body.password)
    return _answer(response, step)


@router.post(
    "/factor/totp",
    response_model=SignInOut,
    response_model_exclude_unset=True,
    dependencies=[Depends(_FACTOR)],
)
async def factor_totp(body: CodeIn, request: Request, response: Response) -> SignInOut:
    """A code from the authenticator app, after the password."""
    step = await flows.factor_totp(
        request, await session_of(request), code=body.code, remember=body.remember_device
    )
    return _answer(response, step)


@router.post(
    "/factor/recovery",
    response_model=SignInOut,
    response_model_exclude_unset=True,
    dependencies=[Depends(_FACTOR)],
)
async def factor_recovery(body: RecoveryIn, request: Request, response: Response) -> SignInOut:
    """One of the ten recovery codes, after the password. Each works once."""
    step = await flows.factor_recovery(
        request, await session_of(request), code=body.code, remember=body.remember_device
    )
    return _answer(response, step)


@router.post("/factor/passkey/options", dependencies=[Depends(_FACTOR)])
async def factor_passkey_options(request: Request) -> dict[str, Any]:
    """The WebAuthn request options for this person's passkeys."""
    return await flows.factor_passkey_options(request, await session_of(request))


@router.post(
    "/factor/passkey",
    response_model=SignInOut,
    response_model_exclude_unset=True,
    dependencies=[Depends(_FACTOR)],
)
async def factor_passkey(body: PasskeyIn, request: Request, response: Response) -> SignInOut:
    """The browser's assertion, after the password."""
    step = await flows.factor_passkey(
        request,
        await session_of(request),
        credential=body.credential,
        remember=body.remember_device,
    )
    return _answer(response, step)


# --------------------------------------------------------------------------- #
# /auth/enroll -- the first factor, or one more
# --------------------------------------------------------------------------- #


@router.post("/enroll/totp/start", response_model=TotpStartOut, dependencies=[Depends(_FACTOR)])
async def enroll_totp_start(request: Request) -> TotpStartOut:
    """A new authenticator-app secret, as the URI its QR code encodes."""
    uri = await flows.enroll_totp_start(request, await session_of(request))
    return TotpStartOut(otpauth_uri=uri)


@router.post(
    "/enroll/totp/confirm",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(_FACTOR)],
)
async def enroll_totp_confirm(body: EnrollCodeIn, request: Request) -> Response:
    """A code from the new secret: only now is it a factor."""
    await flows.enroll_totp_confirm(request, await session_of(request), code=body.code)
    return _no_content()


@router.post("/enroll/passkey/options", dependencies=[Depends(_FACTOR)])
async def enroll_passkey_options(request: Request) -> dict[str, Any]:
    """The WebAuthn creation options: user verification required, no attestation."""
    return await flows.enroll_passkey_options(request, await session_of(request))


@router.post(
    "/enroll/passkey", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(_FACTOR)]
)
async def enroll_passkey(body: EnrollPasskeyIn, request: Request) -> Response:
    """The browser's attestation, named by the person."""
    await flows.enroll_passkey(
        request, await session_of(request), credential=body.credential, name=body.name
    )
    return _no_content()


@router.post("/enroll/finish", response_model=RecoveryCodesOut, dependencies=[Depends(_FACTOR)])
async def enroll_finish(request: Request, response: Response) -> RecoveryCodesOut:
    """The ten recovery codes, once, and the console session."""
    codes, step = await flows.enroll_finish(request, await session_of(request))
    _answer(response, step)
    return RecoveryCodesOut(recovery_codes=codes)


# --------------------------------------------------------------------------- #
# /auth/step-up -- the recent confirmation
# --------------------------------------------------------------------------- #


@router.post(
    "/step-up/totp", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(_FACTOR)]
)
async def step_up_totp(body: StepUpCodeIn, request: Request) -> Response:
    """Confirm the second factor again, for ten minutes."""
    await flows.step_up_totp(request, await session_of(request), code=body.code)
    return _no_content()


@router.post(
    "/step-up/recovery", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(_FACTOR)]
)
async def step_up_recovery(body: StepUpRecoveryIn, request: Request) -> Response:
    """Confirm with a recovery code, for ten minutes. The code is spent."""
    await flows.step_up_recovery(request, await session_of(request), code=body.code)
    return _no_content()


@router.post("/step-up/passkey/options", dependencies=[Depends(_FACTOR)])
async def step_up_passkey_options(request: Request) -> dict[str, Any]:
    """The WebAuthn request options of a confirmation."""
    return await flows.step_up_passkey_options(request, await session_of(request))


@router.post(
    "/step-up/passkey", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(_FACTOR)]
)
async def step_up_passkey(body: StepUpPasskeyIn, request: Request) -> Response:
    """Confirm with a passkey, for ten minutes."""
    await flows.step_up_passkey(request, await session_of(request), credential=body.credential)
    return _no_content()


# --------------------------------------------------------------------------- #
# /auth -- signing out, forgot and reset
# --------------------------------------------------------------------------- #


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request) -> Response:
    """End this session -- idle or not -- and clear its cookie. Answered alike
    with no session."""
    await flows.logout(request)
    response = _no_content()
    clear_session_cookie(response)
    return response


@router.post("/logout-everywhere", status_code=status.HTTP_204_NO_CONTENT)
async def logout_everywhere(request: Request) -> Response:
    """End every session of the caller and forget every remembered browser."""
    await flows.logout_everywhere(request, await session_of(request))
    response = _no_content()
    clear_session_cookie(response)
    return response


@router.post(
    "/password/forgot", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(_FORGOT)]
)
async def forgot_password(
    body: ForgotIn, request: Request, background: BackgroundTasks
) -> Response:
    """Always 202, at once: the link, when there is an account, goes after the
    answer (plan decision 8)."""
    background.add_task(
        flows.send_reset_link, request.app, email=body.email, locale=locale_var.get()
    )
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post(
    "/password/reset", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(_LINK)]
)
async def reset_password(body: ResetIn, request: Request) -> Response:
    """A new password through a one-time link. Signs nobody in."""
    await flows.reset_password(request, token=body.token, password=body.password)
    return _no_content()


@router.post("/invite/details", response_model=InviteOut, dependencies=[Depends(_LINK)])
async def invite_details(body: LinkTokenIn, request: Request) -> InviteOut:
    """The address and the name an invitation is for. Leaves the link unused."""
    email, name = await flows.invite_details(request, token=body.token)
    return InviteOut(email=email, display_name=name)


@router.post(
    "/invite/accept",
    response_model=SignInOut,
    response_model_exclude_unset=True,
    dependencies=[Depends(_LINK)],
)
async def accept_invite(body: InviteAcceptIn, request: Request, response: Response) -> SignInOut:
    """Choose a password through an invitation; the first factor comes next."""
    step = await flows.accept_invite(request, token=body.token, password=body.password)
    return _answer(response, step)


@router.post("/recover/details", response_model=RecoveryOut, dependencies=[Depends(_LINK)])
async def recovery_details(body: LinkTokenIn, request: Request) -> RecoveryOut:
    """The address a recovery link is for. Leaves the link unused."""
    return RecoveryOut(email=await flows.recovery_details(request, token=body.token))


@router.post(
    "/recover/accept",
    response_model=SignInOut,
    response_model_exclude_unset=True,
    dependencies=[Depends(_LINK)],
)
async def accept_recovery(body: LinkTokenIn, request: Request, response: Response) -> SignInOut:
    """Use the server's recovery link: the second factors, remembered browsers
    and sessions go, and a new first factor is set up next."""
    step = await flows.accept_recovery(request, token=body.token)
    return _answer(response, step)


# --------------------------------------------------------------------------- #
# /me -- my security
# --------------------------------------------------------------------------- #


async def _console_ctx(ctx: AuthContext = Depends(bound_auth_context)) -> AuthContext:
    """The door admitted somebody; these routes need it to have been the
    console session (`ctx.principal`, Task 6), not a token."""
    if ctx.principal is None:
        raise sign_in_again()
    return ctx


def _me(ctx: AuthContext) -> SessionPrincipal:
    assert ctx.principal is not None  # `_console_ctx` refused it otherwise
    return ctx.principal


@me_router.get("/security", response_model=MySecurityOut)
async def my_security(request: Request, ctx: AuthContext = Depends(_console_ctx)) -> MySecurityOut:
    """The caller's second factors, remembered browsers and open sessions."""
    mine = await account.my_security(request, _me(ctx))
    return MySecurityOut(
        has_totp=mine.has_totp,
        passkeys=[PasskeyOut(**vars(p)) for p in mine.passkeys],
        recovery_codes_left=mine.recovery_codes_left,
        trusted_devices=[TrustedDeviceOut(**vars(d)) for d in mine.trusted_devices],
        sessions=[SessionOut(**vars(s)) for s in mine.sessions],
    )


@me_router.put("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: PasswordChangeIn, request: Request, ctx: AuthContext = Depends(_console_ctx)
) -> Response:
    """The current password and a new one, after a recent confirmation. The
    caller's session stays under a new cookie; every other session ends."""
    require_step_up(ctx)
    token, expires_at = await account.change_password(
        request, _me(ctx), current=body.current, new=body.new
    )
    response = _no_content()
    # The new identifier, expiring with the session as the change capped it.
    set_session_cookie(response, token, expires_at=expires_at)
    return response


@me_router.delete("/passkeys/{passkey_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_passkey(
    passkey_id: UUID, request: Request, ctx: AuthContext = Depends(_console_ctx)
) -> Response:
    """Remove one of the caller's passkeys; never the last second factor."""
    require_step_up(ctx)
    await account.remove_passkey(request, _me(ctx), passkey_id)
    return _no_content()


@me_router.delete("/totp", status_code=status.HTTP_204_NO_CONTENT)
async def remove_totp(request: Request, ctx: AuthContext = Depends(_console_ctx)) -> Response:
    """Remove the caller's authenticator app; never the last second factor."""
    require_step_up(ctx)
    await account.remove_totp(request, _me(ctx))
    return _no_content()


@me_router.post("/recovery-codes", response_model=RecoveryCodesOut)
async def new_recovery_codes(
    request: Request, ctx: AuthContext = Depends(_console_ctx)
) -> RecoveryCodesOut:
    """Ten new recovery codes, once; the old ones stop working."""
    require_step_up(ctx)
    return RecoveryCodesOut(recovery_codes=await account.new_recovery_codes(request, _me(ctx)))


@me_router.delete("/trusted-devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def forget_device(
    device_id: UUID, request: Request, ctx: AuthContext = Depends(_console_ctx)
) -> Response:
    """Stop remembering one browser."""
    await account.forget_device(request, _me(ctx), device_id)
    return _no_content()


@me_router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def end_my_session(
    session_id: UUID, request: Request, ctx: AuthContext = Depends(_console_ctx)
) -> Response:
    """End one of the caller's own sessions."""
    await account.end_my_session(request, _me(ctx), session_id)
    return _no_content()
