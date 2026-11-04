"""Passkeys as a second factor: `app/core/auth/passkeys.py` (P2 design §2.2).

A software authenticator (`tests/webauthn_authenticator.py`) answers the
options the platform issues, through the same py_webauthn 3.0.1 calls the
routes will make, against the real database (`auth_passkeys`,
`auth_webauthn_challenges`, Task 1). The fixed vectors that need no database
are in `tests/test_auth_passkey_vectors.py`.

Every refusal is asserted by REASON, and each names the line it protects: a
test that only asserted "refused" would pass for the wrong reason.

Falsifier 4 of design §10 ("Una llave de acceso sin verificación del usuario se
rechaza") is `test_a_registration_without_user_verification_is_refused` and
`test_an_assertion_without_user_verification_is_refused`; the browser half is
Task 13's.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.auth.passkeys import (
    CHALLENGE_BYTES,
    PasskeyRejected,
    authentication_options,
    registration_options,
    user_handle,
    verify_authentication,
    verify_registration,
)
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import Organization, User
from app.models.auth import AuthPasskey, AuthWebauthnChallenge
from tests.webauthn_authenticator import SoftPasskey, b64url, unb64url

RP_ID = "itms.example.com"
ORIGIN = "https://itms.example.com"
RP_NAME = "ITMS"


async def _person(admin_engine: AsyncEngine, org_id: UUID | None = None) -> User:
    """A staff user (and, without `org_id`, a new organisation for them)."""
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        if org_id is None:
            slug = f"pk-{uuid4().hex[:8]}"
            org = Organization(name=slug, slug=slug)
            s.add(org)
            await s.flush()
            org_id = org.id
        tag = uuid4().hex[:8]
        user = User(
            org_id=org_id,
            external_subject=f"sub-{tag}",
            email=f"{tag}@example.com",
            display_name=f"Person {tag}",
        )
        s.add(user)
        await s.commit()
        return user


def _session(app_engine: AsyncEngine, org_id: UUID) -> Any:
    """What the sign-in routes run these under: every client, because a staff
    person's rows have no client (module docstring of passkeys.py)."""
    return org_session(org_id, engine=app_engine, clients=ClientScope.all())


async def _registered(
    app_engine: AsyncEngine, user: User, *, backup_eligible: bool = True
) -> tuple[SoftPasskey, AuthPasskey]:
    authenticator = SoftPasskey(
        rp_id=RP_ID,
        user_handle=user_handle(user.id),
        backup_eligible=backup_eligible,
        backed_up=backup_eligible,
    )
    async with _session(app_engine, user.org_id) as s:
        options = await registration_options(s, user, rp_id=RP_ID, rp_name=RP_NAME)
        passkey = await verify_registration(
            s,
            user,
            authenticator.create(options, origin=ORIGIN),
            name="Work laptop",
            expected_origin=ORIGIN,
            rp_id=RP_ID,
        )
    return authenticator, passkey


async def _reason(call: Any) -> str:
    with pytest.raises(PasskeyRejected) as refused:
        await call
    return refused.value.reason


# --- registration ---------------------------------------------------------


async def test_registration_options_require_user_verification_and_no_attestation(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The options a browser receives, in the W3C JSON shape `startRegistration`
    takes: every key is one `PublicKeyCredentialCreationOptionsJSON` defines,
    bytes are unpadded base64url, and the three choices of design §2.2 are
    there -- user verification required, no attestation, EdDSA/ES256/RS256."""
    user = await _person(admin_engine)
    async with _session(app_engine, user.org_id) as s:
        options = await registration_options(s, user, rp_id=RP_ID, rp_name=RP_NAME)

    w3c_keys = {
        "rp", "user", "challenge", "pubKeyCredParams", "timeout", "excludeCredentials",
        "authenticatorSelection", "hints", "attestation", "attestationFormats", "extensions",
    }
    assert set(options) <= w3c_keys
    assert options["rp"] == {"name": RP_NAME, "id": RP_ID}
    assert options["user"] == {
        "id": b64url(user_handle(user.id)),
        "name": user.email,
        "displayName": user.display_name,
    }
    assert len(unb64url(options["challenge"])) == CHALLENGE_BYTES
    assert "=" not in options["challenge"]
    assert [p["alg"] for p in options["pubKeyCredParams"]] == [-8, -7, -257]
    assert options["authenticatorSelection"]["userVerification"] == "required"
    assert options["authenticatorSelection"]["residentKey"] == "preferred"
    assert options["attestation"] == "none"
    assert options["excludeCredentials"] == []


async def test_a_passkey_registers_with_its_key_flags_and_transports(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    user = await _person(admin_engine)
    authenticator, passkey = await _registered(app_engine, user)

    assert passkey.user_id == user.id and passkey.org_id == user.org_id
    assert passkey.credential_id == authenticator.credential_id
    assert passkey.public_key == authenticator.cose_public_key()
    assert passkey.sign_count == 0
    assert passkey.backup_eligible is True and passkey.backup_state is True
    assert passkey.transports == ["hybrid", "internal"]
    assert passkey.name == "Work laptop"


async def test_the_next_options_exclude_the_registered_passkey(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Several passkeys per person, never the same authenticator twice [S14]."""
    user = await _person(admin_engine)
    authenticator, _ = await _registered(app_engine, user)
    async with _session(app_engine, user.org_id) as s:
        options = await registration_options(s, user, rp_id=RP_ID, rp_name=RP_NAME)
    assert options["excludeCredentials"] == [
        {
            "id": b64url(authenticator.credential_id),
            "type": "public-key",
            "transports": ["hybrid", "internal"],
        }
    ]


async def test_a_registration_without_user_verification_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """FALSIFIER 4 (design §10). Protects `require_user_verification=True` in
    `verify_registration`: with it removed, py_webauthn's default accepts this
    response (`test_auth_passkey_vectors.py` shows it) and a touch alone would
    be a second factor. Nothing is stored."""
    user = await _person(admin_engine)
    authenticator = SoftPasskey(rp_id=RP_ID, user_handle=user_handle(user.id))
    async with _session(app_engine, user.org_id) as s:
        options = await registration_options(s, user, rp_id=RP_ID, rp_name=RP_NAME)
        reason = await _reason(
            verify_registration(
                s,
                user,
                authenticator.create(options, origin=ORIGIN, uv=False),
                name="Key",
                expected_origin=ORIGIN,
                rp_id=RP_ID,
            )
        )
        stored = (await s.execute(select(func.count()).select_from(AuthPasskey))).scalar_one()
    assert reason == "verification"
    assert stored == 0


@pytest.mark.parametrize(
    ("origin", "rp_id"),
    [("https://itms.example.org", RP_ID), (ORIGIN, "itms.example.org")],
    ids=["another-origin", "another-rp"],
)
async def test_a_registration_for_another_origin_or_rp_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, origin: str, rp_id: str
) -> None:
    user = await _person(admin_engine)
    authenticator = SoftPasskey(rp_id=rp_id, user_handle=user_handle(user.id))
    async with _session(app_engine, user.org_id) as s:
        options = await registration_options(s, user, rp_id=RP_ID, rp_name=RP_NAME)
        reason = await _reason(
            verify_registration(
                s,
                user,
                authenticator.create(options, origin=origin),
                name="Key",
                expected_origin=ORIGIN,
                rp_id=RP_ID,
            )
        )
    assert reason == "verification"


async def test_a_registration_challenge_is_spent_by_its_first_answer(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Protects `_spend`'s `used_at IS NULL` and its single UPDATE: the same
    response a second time finds no live challenge -- even though the first
    one failed (no UV), because the challenge is spent before verification."""
    user = await _person(admin_engine)
    authenticator = SoftPasskey(rp_id=RP_ID, user_handle=user_handle(user.id))
    async with _session(app_engine, user.org_id) as s:
        options = await registration_options(s, user, rp_id=RP_ID, rp_name=RP_NAME)
        unverified = authenticator.create(options, origin=ORIGIN, uv=False)
        first = await _reason(
            verify_registration(
                s, user, unverified, name="Key", expected_origin=ORIGIN, rp_id=RP_ID
            )
        )
        second = await _reason(
            verify_registration(
                s,
                user,
                authenticator.create(options, origin=ORIGIN),
                name="Key",
                expected_origin=ORIGIN,
                rp_id=RP_ID,
            )
        )
    assert (first, second) == ("verification", "challenge")


async def test_an_expired_or_never_issued_challenge_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Protects `expires_at > now` and the lookup by the response's own
    challenge: an answer to a challenge past its life, or to one the server
    never issued, is refused before any cryptography runs."""
    user = await _person(admin_engine)
    authenticator = SoftPasskey(rp_id=RP_ID, user_handle=user_handle(user.id))
    async with _session(app_engine, user.org_id) as s:
        options = await registration_options(s, user, rp_id=RP_ID, rp_name=RP_NAME)
    async with AsyncSession(admin_engine) as admin:
        await admin.execute(
            update(AuthWebauthnChallenge)
            .where(AuthWebauthnChallenge.user_id == user.id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await admin.commit()
    async with _session(app_engine, user.org_id) as s:
        expired = await _reason(
            verify_registration(
                s,
                user,
                authenticator.create(options, origin=ORIGIN),
                name="Key",
                expected_origin=ORIGIN,
                rp_id=RP_ID,
            )
        )
        forged = await _reason(
            verify_registration(
                s,
                user,
                authenticator.create(options, origin=ORIGIN, challenge=b64url(bytes(32))),
                name="Key",
                expected_origin=ORIGIN,
                rp_id=RP_ID,
            )
        )
    assert (expired, forged) == ("challenge", "challenge")


async def test_a_new_options_call_retires_the_previous_challenge(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """One live challenge per person and purpose (`_issue`)."""
    user = await _person(admin_engine)
    authenticator = SoftPasskey(rp_id=RP_ID, user_handle=user_handle(user.id))
    async with _session(app_engine, user.org_id) as s:
        first = await registration_options(s, user, rp_id=RP_ID, rp_name=RP_NAME)
        await registration_options(s, user, rp_id=RP_ID, rp_name=RP_NAME)
        reason = await _reason(
            verify_registration(
                s,
                user,
                authenticator.create(first, origin=ORIGIN),
                name="Key",
                expected_origin=ORIGIN,
                rp_id=RP_ID,
            )
        )
        live = (
            await s.execute(
                select(func.count())
                .select_from(AuthWebauthnChallenge)
                .where(AuthWebauthnChallenge.user_id == user.id)
            )
        ).scalar_one()
    assert reason == "challenge"
    assert live == 1


async def test_another_persons_registration_challenge_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Protects `owner != user.id`: a challenge issued to A does not register
    a passkey on B's account, though B's session sent it."""
    alice = await _person(admin_engine)
    bob = await _person(admin_engine, alice.org_id)
    authenticator = SoftPasskey(rp_id=RP_ID, user_handle=user_handle(bob.id))
    async with _session(app_engine, alice.org_id) as s:
        options = await registration_options(s, alice, rp_id=RP_ID, rp_name=RP_NAME)
        reason = await _reason(
            verify_registration(
                s,
                bob,
                authenticator.create(options, origin=ORIGIN),
                name="Key",
                expected_origin=ORIGIN,
                rp_id=RP_ID,
            )
        )
    assert reason == "challenge"


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
async def test_a_passkey_needs_a_name_of_at_most_100_characters(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, name: str
) -> None:
    user = await _person(admin_engine)
    authenticator = SoftPasskey(rp_id=RP_ID, user_handle=user_handle(user.id))
    async with _session(app_engine, user.org_id) as s:
        options = await registration_options(s, user, rp_id=RP_ID, rp_name=RP_NAME)
        reason = await _reason(
            verify_registration(
                s,
                user,
                authenticator.create(options, origin=ORIGIN),
                name=name,
                expected_origin=ORIGIN,
                rp_id=RP_ID,
            )
        )
    assert reason == "name"


# --- authentication -------------------------------------------------------


async def test_an_assertion_verifies_and_moves_the_counter(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    user = await _person(admin_engine)
    authenticator, _ = await _registered(app_engine, user)
    async with _session(app_engine, user.org_id) as s:
        options = await authentication_options(
            s, user.id, org_id=user.org_id, rp_id=RP_ID, purpose="authenticate"
        )
        assert options["userVerification"] == "required"
        assert options["rpId"] == RP_ID
        assert [c["id"] for c in options["allowCredentials"]] == [
            b64url(authenticator.credential_id)
        ]
        passkey = await verify_authentication(
            s,
            authenticator.get(options, origin=ORIGIN),
            expected_origin=ORIGIN,
            rp_id=RP_ID,
            purpose="authenticate",
        )
    assert passkey.user_id == user.id
    assert passkey.sign_count == 1
    assert passkey.last_used_at is not None


async def test_an_assertion_without_user_verification_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """FALSIFIER 4 (design §10). Protects `require_user_verification=True` in
    `verify_authentication`; the counter does not move."""
    user = await _person(admin_engine)
    authenticator, _ = await _registered(app_engine, user)
    async with _session(app_engine, user.org_id) as s:
        options = await authentication_options(
            s, user.id, org_id=user.org_id, rp_id=RP_ID, purpose="authenticate"
        )
        reason = await _reason(
            verify_authentication(
                s,
                authenticator.get(options, origin=ORIGIN, uv=False),
                expected_origin=ORIGIN,
                rp_id=RP_ID,
                purpose="authenticate",
            )
        )
        count = (
            await s.execute(select(AuthPasskey.sign_count).where(AuthPasskey.user_id == user.id))
        ).scalar_one()
    assert reason == "verification"
    assert count == 0


async def test_a_counter_that_did_not_move_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """A cloned authenticator replays a counter it already used."""
    user = await _person(admin_engine)
    authenticator, _ = await _registered(app_engine, user)
    async with _session(app_engine, user.org_id) as s:
        first = await authentication_options(
            s, user.id, org_id=user.org_id, rp_id=RP_ID, purpose="authenticate"
        )
        await verify_authentication(
            s,
            authenticator.get(first, origin=ORIGIN),
            expected_origin=ORIGIN,
            rp_id=RP_ID,
            purpose="authenticate",
        )
        second = await authentication_options(
            s, user.id, org_id=user.org_id, rp_id=RP_ID, purpose="authenticate"
        )
        reason = await _reason(
            verify_authentication(
                s,
                authenticator.get(second, origin=ORIGIN, advance=0),
                expected_origin=ORIGIN,
                rp_id=RP_ID,
                purpose="authenticate",
            )
        )
    assert reason == "verification"


async def test_a_sign_in_challenge_does_not_confirm_a_step_up(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Protects `purpose == purpose` in `_spend` (design §2.5): an assertion
    made for signing in is not a recent confirmation of anything else."""
    user = await _person(admin_engine)
    authenticator, _ = await _registered(app_engine, user)
    async with _session(app_engine, user.org_id) as s:
        options = await authentication_options(
            s, user.id, org_id=user.org_id, rp_id=RP_ID, purpose="authenticate"
        )
        reason = await _reason(
            verify_authentication(
                s,
                authenticator.get(options, origin=ORIGIN),
                expected_origin=ORIGIN,
                rp_id=RP_ID,
                purpose="step_up",
            )
        )
    assert reason == "challenge"


async def test_another_persons_passkey_does_not_answer_their_challenge(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Protects `AuthPasskey.user_id == owner`: Bob's genuine passkey signing
    Alice's challenge is not Alice confirming anything."""
    alice = await _person(admin_engine)
    bob = await _person(admin_engine, alice.org_id)
    await _registered(app_engine, alice)
    bobs, _ = await _registered(app_engine, bob)
    async with _session(app_engine, alice.org_id) as s:
        options = await authentication_options(
            s, alice.id, org_id=alice.org_id, rp_id=RP_ID, purpose="step_up"
        )
        reason = await _reason(
            verify_authentication(
                s,
                bobs.get(options, origin=ORIGIN),
                expected_origin=ORIGIN,
                rp_id=RP_ID,
                purpose="step_up",
            )
        )
    assert reason == "credential"


async def test_a_user_handle_of_someone_else_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Protects the `handle != user_handle(passkey.user_id)` comparison."""
    user = await _person(admin_engine)
    authenticator, _ = await _registered(app_engine, user)
    authenticator.user_handle = user_handle(uuid4())
    async with _session(app_engine, user.org_id) as s:
        options = await authentication_options(
            s, user.id, org_id=user.org_id, rp_id=RP_ID, purpose="authenticate"
        )
        reason = await _reason(
            verify_authentication(
                s,
                authenticator.get(options, origin=ORIGIN),
                expected_origin=ORIGIN,
                rp_id=RP_ID,
                purpose="authenticate",
            )
        )
    assert reason == "credential"


async def test_backup_eligibility_cannot_change_after_registration(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Protects the BE comparison: "Backup eligibility is a credential property
    and is permanent" [S14]. A credential registered single-device that now
    claims to be syncable is not the credential that was registered."""
    user = await _person(admin_engine)
    authenticator, _ = await _registered(app_engine, user, backup_eligible=False)
    authenticator.backup_eligible = True
    async with _session(app_engine, user.org_id) as s:
        options = await authentication_options(
            s, user.id, org_id=user.org_id, rp_id=RP_ID, purpose="authenticate"
        )
        reason = await _reason(
            verify_authentication(
                s,
                authenticator.get(options, origin=ORIGIN),
                expected_origin=ORIGIN,
                rp_id=RP_ID,
                purpose="authenticate",
            )
        )
    assert reason == "verification"


async def test_a_discoverable_sign_in_finds_the_person_from_the_passkey(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """No person yet (phase 2's contacts): an empty allow list, an anonymous
    challenge, and the passkey that answers says who it is."""
    user = await _person(admin_engine)
    authenticator, _ = await _registered(app_engine, user)
    async with _session(app_engine, user.org_id) as s:
        options = await authentication_options(
            s, None, org_id=user.org_id, rp_id=RP_ID, purpose="authenticate"
        )
        assert options["allowCredentials"] == []
        passkey = await verify_authentication(
            s,
            authenticator.get(options, origin=ORIGIN),
            expected_origin=ORIGIN,
            rp_id=RP_ID,
            purpose="authenticate",
        )
    assert passkey.user_id == user.id


async def test_an_unknown_credential_is_refused(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    user = await _person(admin_engine)
    await _registered(app_engine, user)
    stranger = SoftPasskey(rp_id=RP_ID, user_handle=user_handle(user.id))
    async with _session(app_engine, user.org_id) as s:
        options = await authentication_options(
            s, None, org_id=user.org_id, rp_id=RP_ID, purpose="authenticate"
        )
        reason = await _reason(
            verify_authentication(
                s,
                stranger.get(options, origin=ORIGIN),
                expected_origin=ORIGIN,
                rp_id=RP_ID,
                purpose="authenticate",
            )
        )
    assert reason == "credential"


async def test_another_organisations_passkey_does_not_exist_here(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Invariant 5: the passkey lookup runs under the session's organisation,
    and RLS hides the other one's row -- the same refusal as a stranger's."""
    elsewhere = await _person(admin_engine)
    theirs, _ = await _registered(app_engine, elsewhere)
    here = await _person(admin_engine)
    async with _session(app_engine, here.org_id) as s:
        options = await authentication_options(
            s, None, org_id=here.org_id, rp_id=RP_ID, purpose="authenticate"
        )
        reason = await _reason(
            verify_authentication(
                s,
                theirs.get(options, origin=ORIGIN),
                expected_origin=ORIGIN,
                rp_id=RP_ID,
                purpose="authenticate",
            )
        )
    assert reason == "credential"


async def test_a_malformed_response_is_a_refusal_not_an_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Hostile input is a `PasskeyRejected`, never a 500."""
    user = await _person(admin_engine)
    async with _session(app_engine, user.org_id) as s:
        for garbage in ({}, {"response": {"clientDataJSON": "%%%"}}, {"response": "x"}):
            reason = await _reason(
                verify_authentication(
                    s, garbage, expected_origin=ORIGIN, rp_id=RP_ID, purpose="authenticate"
                )
            )
            assert reason == "challenge", garbage


async def test_both_verify_calls_ask_the_library_to_require_user_verification(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D22, pinned at the call: py_webauthn's own check runs on both halves.
    `verify_registration` and `verify_authentication` ALSO refuse an
    unverified result themselves, so the two falsifier-4 tests above stay red
    only while at least one of the two checks is there; this one is red the
    moment the keyword argument is dropped from either call."""
    from app.core.auth import passkeys

    seen: list[object] = []

    def spy(original: Any) -> Any:
        def wrapper(**kwargs: Any) -> Any:
            seen.append(kwargs.get("require_user_verification"))
            return original(**kwargs)

        return wrapper

    monkeypatch.setattr(
        passkeys, "verify_registration_response", spy(passkeys.verify_registration_response)
    )
    monkeypatch.setattr(
        passkeys, "verify_authentication_response", spy(passkeys.verify_authentication_response)
    )
    user = await _person(admin_engine)
    authenticator, _ = await _registered(app_engine, user)
    async with _session(app_engine, user.org_id) as s:
        options = await authentication_options(
            s, user.id, org_id=user.org_id, rp_id=RP_ID, purpose="authenticate"
        )
        await verify_authentication(
            s,
            authenticator.get(options, origin=ORIGIN),
            expected_origin=ORIGIN,
            rp_id=RP_ID,
            purpose="authenticate",
        )
    assert seen == [True, True]
