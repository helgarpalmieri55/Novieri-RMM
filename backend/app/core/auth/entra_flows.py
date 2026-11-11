"""Sign in with Microsoft, the flows (design §4; plan 2026-11-06, Task 9;
decisions D30-D32, D41; operator decisions C3 and C4; this task's 1-8).

THREE INTENTS, ONE RETURN PAGE (`/sign-in/microsoft`, Task 12):

- `sign_in`: nobody is signed in. The account must already be linked -- by
  (`tid`, `oid`), never by address -- to an active person (falsifier 8):
  nothing is ever created here. Then the password's path WITHOUT its
  resumption (B2-16: only the password resumes an idle session, design
  §2.4): the person's own second factor (save a remembered device), or a
  contact without one, signed in (`flows.after_first_factor`).
- `link`: a signed-in person, recently confirmed at BOTH ends (design §2.5),
  links their own account from My security.
- `invite`: somebody accepting an invitation. A contact may link their
  account instead of choosing a password (D40). A technician chooses a
  password AS WELL (operator decision C3): `start` accepts the invitation
  with it, as `/auth/invite/accept` does, and the link is then added to that
  new person -- so Microsoft is never a technician's only way in, even when
  the round trip never comes back.

THE TRANSACTION (decision 1). `start` makes the `state` (the Redis key is its
SHA-256), the `nonce` and the PKCE verifier, which travels ONLY in the
`__Host-itms_entra` cookie. Redis keeps, for `ENTRA_TTL_S`: the organization,
the intent, the verifier's S256, the nonce, and the session (a link; a
technician's invitation) or the invitation's digest (a contact's) -- none of
it secret. `complete` takes the entry once (GETDEL), checks the cookie
against it, knows who acts BEFORE the key is used (decision 6), signs the
client assertion with the platform's key (C4), redeems the code, validates
the ID token with the nonce -- and only then looks at who it is. RFC 9700
§2.1.1: "the PKCE challenge or OpenID Connect nonce MUST be
transaction-specific and securely bound to the client and the user agent in
which the transaction was started".

THE RESIDUAL OF AN ANONYMOUS PAIR (decision 1, A2-52): a `/start` and a
`/complete` with any `code` cost one decryption of the key, one audit row,
one WARNING and one call to Microsoft, which refuses it. `_MICROSOFT` bounds
it per address; no ceiling per organization, which would let anyone switch
the sign-in off for everybody.

THE TENANT IS THE KIND'S (decision 2, D32): a technician only from Novieri's
tenant (`staff_tenant_id`), a contact only from one of their active client's
`microsoft_tenants`. At a sign-in a refused tenant answers as an account
linked to nobody (A2-54); at a link or an invitation, where the person is
known, it says so.

A RESET KEEPS THE LINK (decision 5, B2-24): it is a first factor, like the
password; `factors.clear_second_factors` takes second factors only.

ONE ORGANIZATION, EVERY CLIENT (`_entra_session`, `ALL_SCOPE_ALLOWLIST`), for
the steps of `/auth/microsoft/*` where no signed-in person is involved: the
sign-in, whose lookup by (`tid`, `oid`) happens before anybody's scope
exists; an invitation accepted with Microsoft, which is a link's use; and
the return's checkout of the platform's own key, which reads and writes no
person's rows. The organization comes from the deployment's slug, the
session or the invitation, never a request field naming it. A SIGNED-IN
PERSON'S LINK -- its start and its return -- runs in the scope of their kind
(invariant 5a; Task 9b, item C): a contact's own client, a technician's
every client (`flows.signed_in_session`), as a technician's invitation
finished at the return does. NOR FOR UNLINKING (the coordinator's Task 7
decision): `DELETE /me/microsoft` goes through the door that admits
contacts, and `unlink` runs in the scope it established -- a contact's own
client, a technician's every client (`account.own_session`, an allowlist of
kinds).

THE LOCK ORDER is `flows`' (Task 9b): the person's `users` row first, then
their advisory lock where it is taken, then their link and session, and the
audit chain last.

AN UNKNOWN ORGANIZATION (the Task 6 review note): a slug that names none is
answered as everybody else -- at `start`, where nobody is known yet, as an
organization where sign-in with Microsoft is not set up. No slug at all is
the installation's 503, the one every sign-in gives (`flows.deployment_org`).

THE "PASSWORD SET BY MAIL" MARK (the coordinator's Task 6 decision): no
password is presented here, so nothing records a success that would clear
it. Microsoft has no failure counter of its own (B2-17), so the first step
records none at all.
"""

import hashlib
import hmac
import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

import httpx
import jwt
from cryptography.exceptions import InvalidTag
from fastapi import Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import audit_service
from app.core.auth import account, action_tokens, entra, entra_settings, flows, notices
from app.core.auth.entra_settings import EntraConfig
from app.core.auth.factors import lock_person
from app.core.auth.guards import (
    lock_user_row,
    locked_user,
    require_fresh,
    require_live,
    session_of,
    sign_in_again,
    signed_user,
)
from app.core.auth.methods import has_password, has_sign_in_method
from app.core.auth.refusals import AuthRefusal
from app.core.auth.sessions import ENTRA_COOKIE, SessionPrincipal, cookie_token, resolve
from app.core.client_scope import ClientScope
from app.core.i18n import tr
from app.core.identity import ACTIVE_STATUS, CONTACT_KIND, STAFF_KIND
from app.core.tenancy import org_session
from app.core.vault import VaultService
from app.models import Client, User
from app.models.auth import ENTRA, AuthExternalIdentity

ENTRA_TTL_S = 600
_KEY = "auth:entra:"
#: The longest code accepted before it is sent anywhere.
_CODE_MAX = 4096

Intent = Literal["sign_in", "link", "invite"]


@dataclass(frozen=True)
class Started:
    """What `start` hands the route: Microsoft's URL, the transaction cookie's
    value, and -- a technician's invitation only (decision 4) -- the pending
    session their password just opened, whose cookie the route sets too."""

    authorize_url: str
    verifier: str
    signed: flows.SignIn | None = None


@asynccontextmanager
async def _entra_session(org_id: UUID, engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """See the module docstring: one organization, every client."""
    async with org_session(org_id, engine=engine, clients=ClientScope.all()) as session:
        yield session


def _engine(request: Request) -> AsyncEngine:
    engine: AsyncEngine = request.app.state.app_engine
    return engine


def _vault(request: Request) -> VaultService:
    vault: VaultService = request.app.state.vault
    return vault


def _key(state: str) -> str:
    return _KEY + hashlib.sha256(state.encode("utf-8")).hexdigest()


def _refusal(code: str, status_code: int, detail: str) -> AuthRefusal:
    return AuthRefusal(status_code, code=code, detail=detail)


def _unavailable() -> AuthRefusal:
    return _refusal(
        "microsoft_unavailable",
        status.HTTP_409_CONFLICT,
        tr("sign-in with Microsoft is not set up here"),
    )


def _not_configured() -> AuthRefusal:
    return _refusal(
        "microsoft_unavailable",
        status.HTTP_503_SERVICE_UNAVAILABLE,
        tr("signing in is not set up on this installation: its console address is not configured"),
    )


def _failed() -> AuthRefusal:
    return _refusal(
        "microsoft_failed",
        status.HTTP_400_BAD_REQUEST,
        tr("the sign-in with Microsoft did not finish; start it again"),
    )


def _not_linked() -> AuthRefusal:
    return _refusal(
        "microsoft_not_linked",
        status.HTTP_401_UNAUTHORIZED,
        tr(
            "this Microsoft account is not linked to anybody here; sign in another way and link "
            "it from your profile, or use your invitation"
        ),
    )


def _tenant_refused() -> AuthRefusal:
    return _refusal(
        "microsoft_tenant_refused",
        status.HTTP_403_FORBIDDEN,
        tr("this Microsoft account's organization may not sign in here"),
    )


def _already_linked(detail: str) -> AuthRefusal:
    return _refusal("microsoft_already_linked", status.HTTP_409_CONFLICT, detail)


def _keys_down() -> AuthRefusal:
    return _refusal(
        "sign_in_unavailable",
        status.HTTP_503_SERVICE_UNAVAILABLE,
        tr("signing in is not available right now; try again in a moment"),
    )


def _invalid_link() -> AuthRefusal:
    return _refusal(
        "invalid_link",
        status.HTTP_410_GONE,
        tr(
            "this link is not valid: it has expired, was already used or was replaced by a "
            "newer one"
        ),
    )


def _password_required() -> AuthRefusal:
    return _refusal(
        "password_required",
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        tr("choose a password too: a technician never signs in with Microsoft alone"),
    )


def _password_first() -> AuthRefusal:
    """C3 at a link (f2-2 M1): a technician with no password row would enter
    with Microsoft alone, and could not unlink it (decision 5)."""
    return _refusal(
        "password_required",
        status.HTTP_409_CONFLICT,
        tr("a technician needs a password of their own before linking Microsoft"),
    )


def _password_not_used() -> AuthRefusal:
    return _refusal(
        "password_not_used",
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        tr(
            "a contact who accepts with Microsoft sends no password; to choose one, accept "
            "with a password instead"
        ),
    )


def _no_password_here() -> AuthRefusal:
    """D4 (Task 9b): the same code as `_password_not_used`, for a sign-in or
    a link."""
    return _refusal(
        "password_not_used",
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        tr("a sign-in or a link with Microsoft sends no password"),
    )


async def _tenant_allowed(
    session: AsyncSession, user: User, config: EntraConfig, tenant: UUID
) -> bool:
    """Decision 2 (D32)."""
    if user.kind == STAFF_KIND:
        return config.staff_tenant_id is not None and tenant == config.staff_tenant_id
    if user.kind != CONTACT_KIND or user.client_id is None:
        return False
    row = (
        await session.execute(
            select(Client.status, Client.microsoft_tenants).where(Client.id == user.client_id)
        )
    ).one_or_none()
    return row is not None and row.status == "active" and tenant in row.microsoft_tenants


async def start(
    request: Request, *, intent: Intent, token: str | None, password: str | None
) -> Started:
    """Decision 1: Microsoft's sign-in URL and the cookie's value -- for a
    technician's invitation, after accepting it with `password` (decision 4)."""
    try:
        redirect = entra.redirect_uri(request.app.state.settings)
    except entra.EntraNotConfigured as exc:
        raise _not_configured() from exc
    if intent != "invite" and password is not None:
        # D4 (Task 9b): only a technician's invitation carries a password
        # here (C3). One sent with a sign-in or a link is refused, as a
        # contact's invitation refuses one, never silently dropped.
        raise _no_password_here()
    session_id: UUID | None = None
    invite_digest: bytes | None = None
    signed: flows.SignIn | None = None
    if intent == "sign_in":
        # No slug: the 503 every sign-in gives (f2-2 M7). A slug that names
        # no organization: answered as one where Microsoft is not set up
        # (module docstring, "AN UNKNOWN ORGANIZATION").
        found_org = await flows.deployment_org(request)
        if found_org is None:
            raise _unavailable()
        org_id = found_org
        async with _entra_session(org_id, _engine(request)) as session:
            config = await entra_settings.read(session, org_id)
        if not config.usable:
            raise _unavailable()
    elif intent == "link":
        principal = await session_of(request)
        org_id, session_id = principal.org_id, principal.session_id
        # The signed-in person's own scope (item C): a contact's client.
        async with flows.principal_session(request, principal) as session:
            user = await signed_user(session, principal)
            config = await entra_settings.read(session, org_id)
            # A2-58: a contact whose client lists no tenant does not set out.
            offered = await flows.microsoft_offered(session, user)
            # C3 (f2-2 M1): never a technician's only way in.
            bare = user.kind == STAFF_KIND and not await has_password(session, user.id)
        if not offered:
            raise _unavailable()
        if bare:
            raise _password_first()
        # The recent confirmation LAST (final review of phase 2, item 7):
        # asked first, somebody Microsoft is not offered to -- or a
        # technician who must choose a password first -- typed their
        # password or code and only then read why the link cannot be made.
        # Neither answer is secret from the signed-in person, so asking them
        # first protects nothing; a link that can be made still asks, here
        # and at the return.
        require_fresh(principal)
    else:
        invited_org = action_tokens.org_of(token or "")
        if invited_org is None:
            raise _invalid_link()
        org_id = invited_org
        digest = action_tokens.digest(token or "")
        async with _entra_session(org_id, _engine(request)) as session:
            user = await flows.invitee(session, digest=digest, consume=False)
            config = await entra_settings.read(session, org_id)
            offered = await flows.microsoft_offered(session, user)
            kind = user.kind
        if not offered:
            raise _unavailable()
        if kind == STAFF_KIND:
            if password is None:
                raise _password_required()
            # C3: the invitation accepted with the password NOW, in its own
            # transaction (a refused password is its 422, the link usable).
            signed = await flows.accept_invite(request, token=token or "", password=password)
            session_id = signed.session_id
        elif password is not None:
            raise _password_not_used()
        else:
            invite_digest = digest
    assert config.app_client_id is not None  # `usable` / `microsoft_offered` hold one
    verifier = secrets.token_urlsafe(32)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    pending = {
        "org_id": str(org_id),
        "intent": intent,
        "challenge": entra.challenge_of(verifier),
        "nonce": nonce,
        "session_id": None if session_id is None else str(session_id),
        "invite": None if invite_digest is None else invite_digest.hex(),
    }
    await request.app.state.redis.set(_key(state), json.dumps(pending), ex=ENTRA_TTL_S)
    url = entra.authorize_url(
        client_id=config.app_client_id,
        redirect_uri=redirect,
        state=state,
        nonce=nonce,
        code_challenge=entra.challenge_of(verifier),
    )
    return Started(authorize_url=url, verifier=verifier, signed=signed)


async def _caller(request: Request, session_id: UUID, intent: Intent) -> SessionPrincipal:
    """The session that started this transaction, still live in this browser.
    A link: complete and recently confirmed -- the §2.5 window holds at the
    return too (decision 3, A2-53). A technician's invitation: the session
    their password opened at `start` (decision 4), in whatever state."""
    principal = await resolve(request)
    if principal is None or principal.session_id != session_id:
        raise sign_in_again()
    if intent == "link":
        require_fresh(principal)
    return principal


async def complete(request: Request, *, code: str, state: str) -> flows.SignIn | None:
    """Decision 1, then the intent: a sign-in (`SignIn`), a link (None), a
    contact's invitation (`SignIn`), a technician's (`enroll_required`, or
    None when their pending session finished its enrolment meanwhile)."""
    try:
        redirect = entra.redirect_uri(request.app.state.settings)
    except entra.EntraNotConfigured as exc:
        # A2-55: configuration changed since `/start`, a 503 and not a 500.
        raise _not_configured() from exc
    verifier = cookie_token(request, ENTRA_COOKIE)
    raw = await request.app.state.redis.getdel(_key(state)) if state else None
    if verifier is None or raw is None or not code or len(code) > _CODE_MAX:
        raise _failed()
    try:
        pending = json.loads(raw)
        org_id = UUID(pending["org_id"])
        intent: Intent = pending["intent"]
        nonce = str(pending["nonce"])
        challenge = str(pending["challenge"])
        session_id = None if pending["session_id"] is None else UUID(pending["session_id"])
        invite = None if pending["invite"] is None else bytes.fromhex(pending["invite"])
    except (ValueError, KeyError, TypeError) as exc:
        raise _failed() from exc
    if not hmac.compare_digest(entra.challenge_of(verifier), challenge):
        raise _failed()
    principal = None if session_id is None else await _caller(request, session_id, intent)
    async with _entra_session(org_id, _engine(request)) as session:
        config = await entra_settings.read(session, org_id)
        if not config.usable or config.app_client_id is None:
            raise _unavailable()
        # Decision 6 (A2-51): whoever is already known uses the key.
        actor: UUID | None = None if principal is None else principal.user_id
        if invite is not None:
            actor = (await flows.invitee(session, digest=invite, consume=False)).id
        try:
            private_pem = await entra_settings.signing_key(
                session, _vault(request), org_id=org_id, config=config, actor_id=actor
            )
        except (LookupError, ValueError, InvalidTag, httpx.HTTPError) as exc:
            raise _keys_down() from exc
    assert config.certificate_thumbprint is not None  # ck_auth_entra_settings_certificate
    try:
        assertion = entra.client_assertion(
            client_id=config.app_client_id,
            private_pem=private_pem,
            thumbprint=config.certificate_thumbprint,
        )
    except (ValueError, jwt.PyJWTError) as exc:
        # A key that opens and cannot sign is a key that cannot be read (Task
        # 5's contract): PyJWT refuses an unparsable one with `InvalidKeyError`.
        raise _keys_down() from exc
    finally:
        del private_pem
    try:
        async with entra.http_client() as client:
            id_token = await entra.exchange(
                client=client,
                client_id=config.app_client_id,
                client_assertion=assertion,
                code=code,
                redirect_uri=redirect,
                code_verifier=verifier,
            )
        identity = await entra.validate_id_token(
            id_token, client_id=config.app_client_id, nonce=nonce, keys=entra.keys_of(request.app)
        )
    except entra.EntraUnavailable as exc:
        raise _keys_down() from exc
    except entra.EntraRefused as exc:
        raise _failed() from exc
    if intent == "sign_in":
        return await _sign_in(request, org_id, identity, config)
    if intent == "link":
        assert principal is not None
        await _link(request, org_id, identity, config, principal)
        return None
    if principal is not None:
        return await _technician_invited(request, org_id, identity, config, principal)
    assert invite is not None
    return await _contact_invited(request, org_id, identity, config, invite)


async def _identity_of(
    session: AsyncSession, org_id: UUID, identity: entra.EntraIdentity
) -> AuthExternalIdentity | None:
    return (
        await session.execute(
            select(AuthExternalIdentity)
            .where(
                AuthExternalIdentity.org_id == org_id,
                AuthExternalIdentity.provider == ENTRA,
                AuthExternalIdentity.tenant_id == identity.tenant_id,
                AuthExternalIdentity.subject == identity.object_id,
            )
            # Read again under the person's lock (`_sign_in`): the row as it
            # is now, not as the first read left it in the session.
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _sign_in(
    request: Request, org_id: UUID, identity: entra.EntraIdentity, config: EntraConfig
) -> flows.SignIn:
    """Decision 2: the row says who; nothing is created; never the address;
    every refusal the same 401 (A2-54); no resumption (B2-16).

    THE PERSON FIRST (Task 9b, `flows`' lock order). The link is read
    unlocked, only to learn whose it is; that person's row is locked and
    re-read; then the link is read again -- a reset of the person's access
    that deleted it while this waited leaves nothing to sign in with, and
    one moved to somebody else is not this person's -- and only then is its
    `last_used_at` written."""
    async with _entra_session(org_id, _engine(request)) as session:
        seen = await _identity_of(session, org_id, identity)
        user = await lock_user_row(session, seen.user_id) if seen is not None else None
        link = await _identity_of(session, org_id, identity) if user is not None else None
        if (
            link is None
            or user is None
            or link.user_id != user.id
            or user.status != ACTIVE_STATUS
            or not await _tenant_allowed(session, user, config, identity.tenant_id)
        ):
            raise _not_linked()
        # The last time Microsoft vouched for the person here: the first step,
        # whether or not the second follows (A2-59).
        link.last_used_at = datetime.now(UTC)
        return await flows.after_first_factor(session, request, user, method="microsoft")


async def _refuse_a_second_link(
    session: AsyncSession, user: User, identity: entra.EntraIdentity
) -> None:
    """One account per person (decision 3), said precisely (D3, Task 9b):
    the person already has THIS account linked -- from another tab, while
    this round trip was away -- or another one. Read under the person's row
    lock, which every writer of their link takes first, so a link made
    meanwhile is seen here and never reaches `_insert_link`'s "somebody
    here", which is for another person's."""
    mine = (
        await session.execute(
            select(AuthExternalIdentity.tenant_id, AuthExternalIdentity.subject).where(
                AuthExternalIdentity.user_id == user.id, AuthExternalIdentity.provider == ENTRA
            )
        )
    ).one_or_none()
    if mine is None:
        return
    if (mine.tenant_id, mine.subject) == (identity.tenant_id, identity.object_id):
        raise _already_linked(tr("this Microsoft account is already linked to your account"))
    raise _already_linked(tr("you already have a Microsoft account linked; remove it first"))


async def _insert_link(
    session: AsyncSession, org_id: UUID, user: User, identity: entra.EntraIdentity
) -> None:
    """The link, or `microsoft_already_linked`: the unique key is
    organization-wide, so a savepoint catches another person's (Task 1,
    decision 1). The row only. ITS PARTNER IS `_record_link`, split from it
    only because the audit chain's lock goes last (Task 9b): `_add_link`
    calls both; `_contact_invited` writes its role and session between them,
    handing `_record_link` to `flows.contact_admitted`, which records it."""
    try:
        async with session.begin_nested():
            session.add(
                AuthExternalIdentity(
                    org_id=org_id,
                    user_id=user.id,
                    provider=ENTRA,
                    tenant_id=identity.tenant_id,
                    subject=identity.object_id,
                )
            )
            await session.flush()
    except IntegrityError as exc:
        detail = tr("this Microsoft account is already linked to somebody here")
        raise _already_linked(detail) from exc


async def _record_link(
    session: AsyncSession, org_id: UUID, user: User, identity: entra.EntraIdentity, *, source: str
) -> None:
    """Audited `auth.identity_linked`; `source`, never `via`, the key the MCP
    channel writes (A2-56). ITS PARTNER IS `_insert_link` (above): the row it
    records, written first because the audit chain's lock goes last."""
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=user.id,
        action="auth.identity_linked",
        target_type="user",
        target_id=str(user.id),
        metadata={"provider": ENTRA, "tenant_id": str(identity.tenant_id), "source": source},
    )


async def _add_link(
    session: AsyncSession, org_id: UUID, user: User, identity: entra.EntraIdentity, *, source: str
) -> None:
    """The link and its record, in the lock order (Task 9b fix round 1):
    `_insert_link`, then `_record_link`, so the audit cannot be left out
    (invariant 4). For a transaction with no other row after the link."""
    await _insert_link(session, org_id, user, identity)
    await _record_link(session, org_id, user, identity, source=source)


async def _link(
    request: Request,
    org_id: UUID,
    identity: entra.EntraIdentity,
    config: EntraConfig,
    principal: SessionPrincipal,
) -> None:
    """Decision 3: the session that started, complete and recently confirmed
    (`_caller`); one account per person. In the person's own scope (item C):
    a contact's link is written under their client alone."""
    async with flows.principal_session(request, principal) as session:
        # The person first (Task 9b), then their advisory lock.
        user = await locked_user(session, principal)
        await lock_person(session, user.id)
        # D1 (Task 9b): the session that started may have ended while
        # Microsoft answered, after `_caller` read it -- "sign out everywhere",
        # or an act that ends it under this same lock. Checked again here,
        # under the lock: an ended session links nothing.
        await require_live(session, principal)
        # C3 again, under the lock: the password may have gone since `/start`.
        if user.kind == STAFF_KIND and not await has_password(session, user.id):
            raise _password_first()
        if not await _tenant_allowed(session, user, config, identity.tenant_id):
            raise _tenant_refused()
        await _refuse_a_second_link(session, user, identity)
        await _add_link(session, org_id, user, identity, source="profile")
        await notices.tell(
            session,
            user,
            "factor_added",
            vault=_vault(request),
            actor_id=user.id,
            factor="microsoft",
        )


async def _technician_invited(
    request: Request,
    org_id: UUID,
    identity: entra.EntraIdentity,
    config: EntraConfig,
    principal: SessionPrincipal,
) -> flows.SignIn | None:
    """Decision 4 (C3): the invitation was accepted with a password at
    `start`; the account is linked to that same new person now. A refused
    tenant leaves them where the password put them. The session is the
    signed-in person's own (`flows.principal_session`): a technician's,
    every client."""
    async with flows.principal_session(request, principal) as session:
        user = await locked_user(session, principal)
        if user.kind != STAFF_KIND:
            raise _failed()
        if not await _tenant_allowed(session, user, config, identity.tenant_id):
            raise _tenant_refused()
        # D3 (Task 9b): linked meanwhile from another tab is said as such.
        await _refuse_a_second_link(session, user, identity)
        await _add_link(session, org_id, user, identity, source="invite")
    if principal.factor_done:
        return None
    return flows.SignIn(state="enroll_required")


async def _contact_invited(
    request: Request,
    org_id: UUID,
    identity: entra.EntraIdentity,
    config: EntraConfig,
    digest: bytes,
) -> flows.SignIn:
    """Decision 4: a contact's link consumed and the account linked in one
    transaction; a refusal rolls both back and the link stays usable."""
    async with _entra_session(org_id, _engine(request)) as session:
        # The person, then the link (`flows._consumed`), then their rows,
        # and the audit chain last (Task 9b).
        user = await flows.invitee(session, digest=digest, consume=True)
        if user.kind != CONTACT_KIND:
            raise _failed()
        if not await _tenant_allowed(session, user, config, identity.tenant_id):
            raise _tenant_refused()
        # The link's row now, its record with the acceptance's: the role and
        # the session come between them (the audit chain last).
        await _insert_link(session, org_id, user, identity)
        return await flows.contact_admitted(
            session,
            request,
            user,
            method="microsoft",
            recorded_first=lambda: _record_link(
                session, org_id, user, identity, source="invite"
            ),
        )


async def unlink(request: Request, principal: SessionPrincipal, *, clients: ClientScope) -> None:
    """Decision 5: never the last way in. With a recent confirmation (the
    route's `require_step_up`, and here), under the person's lock.

    IN THE SCOPE THE DOOR ESTABLISHED (invariant 5a; the coordinator's Task
    7 decision): `clients` is `AuthContext.clients` of `DELETE /me/microsoft`,
    and the session is My security's own (`account.own_session`) -- a
    contact's own client, a technician's every client, anything else
    refused -- never `_entra_session`'s every client."""
    require_fresh(principal)
    async with account.own_session(principal, request, clients) as session:
        user = await signed_user(session, principal)
        await lock_person(session, user.id)
        link = (
            await session.execute(
                select(AuthExternalIdentity).where(
                    AuthExternalIdentity.user_id == user.id, AuthExternalIdentity.provider == ENTRA
                )
            )
        ).scalar_one_or_none()
        if link is None:
            raise AuthRefusal(
                status.HTTP_404_NOT_FOUND,
                code="not_found",
                detail=tr("no Microsoft account is linked"),
            )
        tenant = link.tenant_id
        await session.delete(link)
        await session.flush()
        keeps_a_way_in = (
            await has_password(session, user.id)
            if user.kind == STAFF_KIND
            else await has_sign_in_method(session, user.id)
        )
        if not keeps_a_way_in:
            # Raised inside the block, after the delete: it rolls back.
            raise AuthRefusal(
                status.HTTP_409_CONFLICT,
                code="last_method",
                detail=tr("this is your only way to sign in; add another one before removing it"),
            )
        await audit_service.record(
            session,
            org_id=user.org_id,
            actor_type="user",
            actor_id=user.id,
            action="auth.identity_unlinked",
            target_type="user",
            target_id=str(user.id),
            metadata={"provider": ENTRA, "tenant_id": str(tenant)},
        )
        await notices.tell(
            session,
            user,
            "factor_removed",
            vault=_vault(request),
            actor_id=user.id,
            factor="microsoft",
        )
