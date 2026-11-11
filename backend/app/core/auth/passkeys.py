"""Passkeys (WebAuthn) as the second factor that is offered first (P2 design §2.2).

THE FOUR CEREMONY HALVES, AND NOTHING ELSE. Options out, response in, for
registration and for authentication. Which route calls them, what counts as a
failure, how failures add up (`app/core/auth/failures.py`) and what the person
is told are the callers' (Tasks 7 and 8). A refusal here is a `PasskeyRejected`
with a `reason` for the log and the tests; the caller answers every reason with
the same generic message, because W3C Web Authentication Level 3 asks that a
failed assertion be indistinguishable from "no such credential" [S14].

THE PARAMETERS, EACH WITH ITS SOURCE:

- **User verification is REQUIRED, and checked here, not only asked for.**
  `userVerification: "required"` in the options is a request to the browser; the
  `require_user_verification=True` passed to py_webauthn 3.0.1 on BOTH verify
  calls is the check. The library's default is `False`
  (`webauthn/registration/verify_registration_response.py`,
  `webauthn/authentication/verify_authentication_response.py`, both 3.0.1),
  which accepts a touch alone -- OWASP: "a touch confirming user presence alone
  is not a second factor" [S7]. Coordinator decision D22.
  `tests/test_auth_passkey_vectors.py` shows the default accepting a response
  without UV, which is why the argument is never left out.
- **No attestation** (`attestation: "none"`) and synced passkeys accepted: NIST
  admits syncable authenticators at AAL2 (SP 800-63B-4 App. B) [S1, S10].
- **Several per person** (`excludeCredentials` stops the same authenticator
  registering twice, nothing stops a second one) [S14 §13.4.6].
- **Challenges**: 32 random bytes (at least 16, [S10, S14 §13.4.3]), stored in
  `auth_webauthn_challenges`, live for `CHALLENGE_TTL`, and spent the moment a
  response naming them arrives -- BEFORE the response is verified, so a response
  that fails cannot be retried against the same challenge. One live challenge
  per person and purpose: issuing a new one retires the last.
- **Purposes do not mix.** A challenge issued to sign in (`authenticate`) never
  confirms a step-up (`step_up`, design §2.5), nor the other way round.
- **Backup eligibility is permanent** for a credential [S14: "Backup eligibility
  is a credential property and is permanent for a given public key credential
  source"]: an assertion whose BE flag differs from the one registered is
  refused.
- **The counter**: py_webauthn refuses a counter that did not move past the
  stored one unless both are zero (most synced passkeys always send zero).

THE USER HANDLE (`user.id` in the options) is `user_handle(user_id)`, a SHA-256
of the platform's user id: stable for a person, so an authenticator replaces
rather than duplicates their credential, and free of anything personal -- the
users table's ids are UUIDv7, whose first 48 bits are the account's creation
time.

THE CALLER'S TRANSACTION. Spending a challenge is an UPDATE in the caller's
transaction. A refusal raised after it (`PasskeyRejected`) is only recorded as
spent if that transaction COMMITS: the caller must catch `PasskeyRejected`,
leave the transaction block (so it commits), and only then answer with the
refusal. Letting the exception unwind through the session's context manager
rolls the spend back and the challenge stays live for another try.

THE SESSION PASSED IN is the caller's, and it must be able to write a staff
row: `auth_passkeys` and `auth_webauthn_challenges` are O tables keyed to
`users` (Task 1), whose staff rows have no client, so only an every-client
session writes them (migration 0231). The bulk statements below bypass
`app/core/global_rows.py`'s listener; under a narrowed scope RLS would make
them match nothing, which here only ever means "refused".
"""

import hashlib
import json
import secrets
import unicodedata
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, parse_client_data_json
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    CredentialDeviceType,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.models import User
from app.models.auth import AuthPasskey, AuthWebauthnChallenge

#: Bytes of entropy in a challenge ([S10]: "at least 16 bytes").
CHALLENGE_BYTES = 32
#: How long a challenge may be answered.
CHALLENGE_TTL = timedelta(minutes=5)
#: The browser's ceremony timeout, a hint (milliseconds): the challenge's life.
CEREMONY_TIMEOUT_MS = int(CHALLENGE_TTL.total_seconds() * 1000)
#: The longest name a person may give a passkey ("Work laptop").
NAME_MAX = 100
#: The longest credential id W3C accepts (WebAuthn Level 3 §7.1: "at most 1023
#: bytes"); a longer one is refused before it is stored.
CREDENTIAL_ID_MAX = 1023
#: Domain separation for `user_handle`: no other hash of a user id collides.
_USER_HANDLE_CONTEXT = b"itms.webauthn.user-handle\x00"
#: The transports W3C defines, as py_webauthn spells them; anything else a
#: browser reports is dropped rather than stored.
_TRANSPORTS = {transport.value: transport for transport in AuthenticatorTransport}

Purpose = Literal["register", "authenticate", "step_up"]
AssertionPurpose = Literal["authenticate", "step_up"]
Reason = Literal["challenge", "credential", "verification", "name"]

#: Every exception a malformed or hostile response can raise on its way
#: through py_webauthn: its own, and the decoding errors of `base64url_to_bytes`
#: (`binascii.Error` is a `ValueError`) and of the JSON walk.
_MALFORMED = (WebAuthnException, ValueError, TypeError, KeyError, AttributeError)


class PasskeyRejected(Exception):
    """A ceremony response the platform does not accept. `reason` is for the
    log and the tests only (module docstring); it is not shown to anyone."""

    def __init__(self, *, reason: Reason) -> None:
        super().__init__(reason)
        self.reason: Reason = reason


def _now() -> datetime:
    return datetime.now(UTC)


def user_handle(user_id: UUID) -> bytes:
    """The WebAuthn user handle of a person (module docstring): 32 bytes."""
    return hashlib.sha256(_USER_HANDLE_CONTEXT + user_id.bytes).digest()


def _transports(values: object) -> list[str]:
    """The known transports in a browser's list, in its order, without repeats."""
    if not isinstance(values, list):
        return []
    known: list[str] = []
    for value in values:
        if isinstance(value, str) and value in _TRANSPORTS and value not in known:
            known.append(value)
    return known


def _descriptor(
    credential_id: bytes, transports: list[str] | None
) -> PublicKeyCredentialDescriptor:
    return PublicKeyCredentialDescriptor(
        id=credential_id,
        transports=[_TRANSPORTS[t] for t in transports or [] if t in _TRANSPORTS] or None,
    )


async def _issue(
    session: AsyncSession, *, org_id: UUID, user_id: UUID | None, purpose: Purpose
) -> bytes:
    """A fresh challenge for `purpose`, stored. A person's earlier challenge for
    the same purpose is retired, and every spent or expired one of theirs is
    swept; an anonymous request (no person yet) sweeps only spent and expired
    anonymous ones, so two people signing in at once do not retire each other's."""
    now = _now()
    stale = or_(AuthWebauthnChallenge.used_at.is_not(None), AuthWebauthnChallenge.expires_at <= now)
    owner = (
        AuthWebauthnChallenge.user_id.is_(None)
        if user_id is None
        else AuthWebauthnChallenge.user_id == user_id
    )
    retire = stale if user_id is None else or_(stale, AuthWebauthnChallenge.purpose == purpose)
    await session.execute(
        delete(AuthWebauthnChallenge).where(
            AuthWebauthnChallenge.org_id == org_id, owner, retire
        )
    )
    challenge = secrets.token_bytes(CHALLENGE_BYTES)
    session.add(
        AuthWebauthnChallenge(
            org_id=org_id,
            user_id=user_id,
            purpose=purpose,
            challenge=challenge,
            expires_at=now + CHALLENGE_TTL,
        )
    )
    await session.flush()
    return challenge


async def _spend(
    session: AsyncSession, credential: dict[str, Any], *, purpose: Purpose
) -> tuple[bytes, UUID | None]:
    """Spend the challenge `credential` answers: `(challenge, its person)`.

    The challenge is read from the response's own `clientDataJSON` and marked
    used in ONE statement whose WHERE requires it unused and unexpired, so of
    two responses racing for it exactly one gets a row. Spent before the
    response is verified: a response that then fails has used it up."""
    try:
        client_data = parse_client_data_json(
            base64url_to_bytes(credential["response"]["clientDataJSON"])
        )
    except _MALFORMED as exc:
        raise PasskeyRejected(reason="challenge") from exc
    now = _now()
    row = (
        await session.execute(
            update(AuthWebauthnChallenge)
            .where(
                AuthWebauthnChallenge.challenge == client_data.challenge,
                AuthWebauthnChallenge.purpose == purpose,
                AuthWebauthnChallenge.used_at.is_(None),
                AuthWebauthnChallenge.expires_at > now,
            )
            .values(used_at=now)
            .returning(AuthWebauthnChallenge.challenge, AuthWebauthnChallenge.user_id)
        )
    ).one_or_none()
    if row is None:
        raise PasskeyRejected(reason="challenge")
    # Spent already (above); a ceremony run from inside another origin's frame
    # is refused (WebAuthn Level 3 §7.1/§7.2 step "crossOrigin").
    if client_data.cross_origin:
        raise PasskeyRejected(reason="verification")
    return row.challenge, row.user_id


async def registration_options(
    session: AsyncSession, user: User, *, rp_id: str, rp_name: str, discoverable: bool = False
) -> dict[str, Any]:
    """`PublicKeyCredentialCreationOptionsJSON` for `user`, as
    `@simplewebauthn/browser`'s `startRegistration({ optionsJSON })` takes it:
    user verification required, no attestation, the person's existing passkeys
    excluded, and a discoverable credential preferred -- or REQUIRED with
    `discoverable` (a contact's, which signs in without an address: W3C
    WebAuthn Level 3 §5.4.6, "The client MUST return an error if a
    client-side discoverable credential cannot be created"; plan 2026-11-06,
    Task 6, decision 3)."""
    existing = (
        await session.execute(
            select(AuthPasskey.credential_id, AuthPasskey.transports).where(
                AuthPasskey.user_id == user.id
            )
        )
    ).all()
    challenge = await _issue(session, org_id=user.org_id, user_id=user.id, purpose="register")
    options = generate_registration_options(
        rp_id=rp_id,
        rp_name=rp_name,
        user_name=user.email,
        user_id=user_handle(user.id),
        user_display_name=user.display_name or user.email,
        challenge=challenge,
        timeout=CEREMONY_TIMEOUT_MS,
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=(
                ResidentKeyRequirement.REQUIRED
                if discoverable
                else ResidentKeyRequirement.PREFERRED
            ),
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[_descriptor(cid, transports) for cid, transports in existing],
    )
    return cast(dict[str, Any], json.loads(options_to_json(options)))


async def verify_registration(
    session: AsyncSession,
    user: User,
    credential: dict[str, Any],
    *,
    name: str,
    expected_origin: str,
    rp_id: str,
) -> AuthPasskey:
    """Verify `navigator.credentials.create()`'s answer and store the passkey.

    The challenge is spent in the caller's transaction: after a refusal the
    caller must COMMIT (module docstring) or the challenge stays live.

    Refused (`PasskeyRejected`): a name that is empty, longer than `NAME_MAX`
    or holding a control character; a challenge that is not this person's live
    registration challenge; anything py_webauthn refuses -- another origin or RP ID, no user
    presence, NO USER VERIFICATION; a credential id already registered."""
    label = name.strip()
    if (
        not label
        or len(label) > NAME_MAX
        or any(unicodedata.category(ch) == "Cc" for ch in label)
    ):
        raise PasskeyRejected(reason="name")
    expected, owner = await _spend(session, credential, purpose="register")
    if owner != user.id:
        raise PasskeyRejected(reason="challenge")
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=expected,
            expected_rp_id=rp_id,
            expected_origin=expected_origin,
            require_user_verification=True,
        )
    except _MALFORMED as exc:
        raise PasskeyRejected(reason="verification") from exc
    # The library refused already; this is the platform's own statement of
    # the rule, so the property does not rest on one keyword argument alone.
    if not verified.user_verified:
        raise PasskeyRejected(reason="verification")
    if len(verified.credential_id) > CREDENTIAL_ID_MAX:
        raise PasskeyRejected(reason="credential")
    taken = (
        await session.execute(
            select(AuthPasskey.id).where(AuthPasskey.credential_id == verified.credential_id)
        )
    ).scalar_one_or_none()
    if taken is not None:
        raise PasskeyRejected(reason="credential")
    passkey = AuthPasskey(
        org_id=user.org_id,
        user_id=user.id,
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        transports=_transports(credential.get("response", {}).get("transports")),
        backup_eligible=verified.credential_device_type == CredentialDeviceType.MULTI_DEVICE,
        backup_state=verified.credential_backed_up,
        name=label,
        created_at=_now(),
    )
    # `uq_auth_passkeys_credential_id` is platform-wide while the pre-check above
    # sees only this organisation's rows (RLS): an id registered in another
    # organisation is refused by the constraint, in a savepoint so the spend
    # above survives, and with the same reason as a duplicate here -- it does
    # not tell this organisation that the id exists elsewhere.
    try:
        async with session.begin_nested():
            session.add(passkey)
            await session.flush()
    except IntegrityError as exc:
        raise PasskeyRejected(reason="credential") from exc
    return passkey


async def authentication_options(
    session: AsyncSession,
    user_id: UUID | None,
    *,
    org_id: UUID,
    rp_id: str,
    purpose: AssertionPurpose,
) -> dict[str, Any]:
    """`PublicKeyCredentialRequestOptionsJSON` for `startAuthentication`.

    With a person: their passkeys in `allowCredentials`. Without one (a
    discoverable credential, phase 2's contacts): an empty list, and the
    challenge belongs to nobody until a passkey answers it. A step-up always
    has a person -- it confirms the one already signed in."""
    if purpose == "step_up" and user_id is None:
        raise ValueError("a step-up confirms a person")  # i18n-exempt: a caller's defect
    allowed: list[PublicKeyCredentialDescriptor] = []
    if user_id is not None:
        rows = (
            await session.execute(
                select(AuthPasskey.credential_id, AuthPasskey.transports).where(
                    AuthPasskey.user_id == user_id
                )
            )
        ).all()
        allowed = [_descriptor(cid, transports) for cid, transports in rows]
    challenge = await _issue(session, org_id=org_id, user_id=user_id, purpose=purpose)
    return _request_options(rp_id=rp_id, challenge=challenge, allowed=allowed)


def unanswerable_options(*, rp_id: str) -> dict[str, Any]:
    """Request options shaped as `authentication_options(None, ...)` shapes
    them, whose challenge is stored nowhere: no response can answer it
    (`_spend` finds no row, `PasskeyRejected("challenge")`). What a sign-in
    without an address is offered when the deployment's slug names no
    organization to keep a challenge in (plan 2026-11-06, Task 6): the same
    answer as anywhere else, and the response to it is refused at the
    challenge, as a replayed one is."""
    return _request_options(rp_id=rp_id, challenge=secrets.token_bytes(CHALLENGE_BYTES), allowed=[])


def _request_options(
    *, rp_id: str, challenge: bytes, allowed: list[PublicKeyCredentialDescriptor]
) -> dict[str, Any]:
    options = generate_authentication_options(
        rp_id=rp_id,
        challenge=challenge,
        timeout=CEREMONY_TIMEOUT_MS,
        allow_credentials=allowed,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    return cast(dict[str, Any], json.loads(options_to_json(options)))


async def verify_authentication(
    session: AsyncSession,
    credential: dict[str, Any],
    *,
    expected_origin: str,
    rp_id: str,
    purpose: AssertionPurpose,
    owner: UUID | None = None,
) -> AuthPasskey:
    """Verify `navigator.credentials.get()`'s answer; the passkey that signed.

    The challenge says whose ceremony this is; the passkey must be that
    person's, and the response's user handle (when sent) must be theirs too.
    The challenge is spent in the caller's transaction: after a refusal the
    caller must COMMIT (module docstring) or the challenge stays live.

    `owner`, when given, is the person the CHALLENGE must belong to: the
    second factor and the recent confirmation pass theirs, so an anonymous
    challenge (`/auth/passkey/options`, phase 2) cannot answer them (plan
    2026-11-06, Task 6, decision 4). A sign-in without an address passes
    none: its challenge is nobody's.

    Refused (`PasskeyRejected`): an unknown, spent, expired or other-purpose
    challenge, or one that is not `owner`'s; a passkey that is not the
    challenge's person's; anything py_webauthn refuses -- origin, RP ID,
    signature, presence, NO USER VERIFICATION, a counter that did not move; a
    backup-eligibility flag that changed. On success the counter, the backup
    state and `last_used_at` are updated."""
    expected, challenge_owner = await _spend(session, credential, purpose=purpose)
    if owner is not None and challenge_owner != owner:
        raise PasskeyRejected(reason="challenge")
    try:
        raw_id = base64url_to_bytes(credential["rawId"])
        sent_handle = credential["response"].get("userHandle")
        handle = None if sent_handle is None else base64url_to_bytes(sent_handle)
    except _MALFORMED as exc:
        raise PasskeyRejected(reason="credential") from exc
    query = select(AuthPasskey).where(AuthPasskey.credential_id == raw_id)
    if challenge_owner is not None:
        query = query.where(AuthPasskey.user_id == challenge_owner)
    passkey = (await session.execute(query)).scalar_one_or_none()
    if challenge_owner is None and handle is None:
        # A discoverable assertion names its person only through `userHandle`
        # (WebAuthn Level 3 §7.2).
        raise PasskeyRejected(reason="credential")
    if passkey is None or (handle is not None and handle != user_handle(passkey.user_id)):
        raise PasskeyRejected(reason="credential")
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=expected,
            expected_rp_id=rp_id,
            expected_origin=expected_origin,
            credential_public_key=passkey.public_key,
            credential_current_sign_count=passkey.sign_count,
            require_user_verification=True,
        )
    except _MALFORMED as exc:
        raise PasskeyRejected(reason="verification") from exc
    if not verified.user_verified:
        raise PasskeyRejected(reason="verification")
    eligible = verified.credential_device_type == CredentialDeviceType.MULTI_DEVICE
    if eligible != passkey.backup_eligible:
        raise PasskeyRejected(reason="verification")
    passkey.sign_count = verified.new_sign_count
    passkey.backup_state = verified.credential_backed_up
    passkey.last_used_at = _now()
    await session.flush()
    return passkey
