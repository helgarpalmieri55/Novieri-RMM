"""The HTTP half of user provisioning: the token exchange and the four admin calls.

Spec `docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md` §2.

WHY A TEST FILE FOR A CLIENT THAT IS DELIBERATELY THIN. "Thin" is not "zero".
Every string in `kc_client.py` is a fact about a system this project pins and
does not build -- the token path, the admin path, the header the token goes in,
the shape of the user representation, and the fact that Keycloak returns the new
user's id in a `Location` header rather than a body. Each of them is either
right or produces a 404 at 03:00 on the operator's first attempt to add a
colleague, rather than in CI.

WHAT THIS CANNOT SHOW, stated because §6 of the spec already says it: nothing
here has run against a live Keycloak. `httpx.MockTransport` answers whatever
these tests tell it to. It proves the request this code SENDS. It proves nothing
about what a real Keycloak replies, and nothing about whether `manage-users` is
sufficient for these endpoints.

THE FIXTURE PATTERN IS BORROWED FROM `tests/test_keycloak_event_client.py` AND
THE REASON MATTERS: `_REAL_CLIENT` is captured at import, before any fixture
swaps `httpx.AsyncClient`. A factory that called `httpx.AsyncClient(...)` by
name would recurse into whichever factory is currently installed.
"""

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.modules.user_admin.kc_client import (
    ENROLMENT_ACTIONS,
    PASSKEY_REQUIRED_ACTION,
    PASSWORD_RESET_ACTIONS,
    KeycloakAdminConfig,
    KeycloakAdminError,
    create_user,
    delete_user,
    execute_actions_email,
    set_enabled,
)

# `asyncio_mode = "auto"` (pyproject.toml), so async tests need no marker and
# the sync ones below are ordinary tests.

#: Captured at import, before any test swaps the name.
_REAL_CLIENT = httpx.AsyncClient

CONFIG = KeycloakAdminConfig(
    base_url="https://keycloak.test",
    realm="it-suite",
    client_id="it-suite-provisioning",
    client_secret="not-a-real-secret",
)

NEW_ID = "3f0a2b41-9c1e-4c0e-9a2b-0d1e2f3a4b5c"


def _handler(
    seen: list[httpx.Request], *, location: str | None, status: int
) -> Any:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/protocol/openid-connect/token"):
            return httpx.Response(200, json={"access_token": "tok-123"})
        headers = {}
        if location is not None and request.method == "POST":
            headers["Location"] = location
        return httpx.Response(status, headers=headers)

    return handle


@pytest.fixture()
def patched(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Swap the transport under `httpx.AsyncClient` without changing the
    production call sites -- the client builds its own client so its timeout is
    not something a caller can forget to pass."""
    state = SimpleNamespace(
        seen=[],
        kwargs={},
        location=f"https://keycloak.test/admin/realms/it-suite/users/{NEW_ID}",
        status=204,
    )

    def factory(**kw: Any) -> httpx.AsyncClient:
        state.kwargs.update(kw)
        return _REAL_CLIENT(
            transport=httpx.MockTransport(
                _handler(state.seen, location=state.location, status=state.status)
            ),
            **kw,
        )

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return state


# --------------------------------------------------------------------------- #
# from_settings -- the same three-state contract the events client has
# --------------------------------------------------------------------------- #


def test_it_is_none_when_the_deployment_has_not_configured_provisioning() -> None:
    """`None` IS A SUPPORTED ANSWER. An operator who has not set up the
    provisioning client has not broken anything -- the lifecycle routes refuse
    with a 503 that says so, which is visible, rather than crashing."""
    settings = SimpleNamespace(keycloak_admin_url=None, keycloak_admin_secret_path=None)

    assert KeycloakAdminConfig.from_settings(settings) is None


def test_configured_with_a_missing_secret_raises_rather_than_degrading(
    tmp_path: Path,
) -> None:
    """The one case that must NOT read as "not configured": the operator set the
    URL and the path and believes provisioning is running. Silently degrading
    would present as "Keycloak is not configured" on a deployment where it is."""
    settings = SimpleNamespace(
        keycloak_admin_url="https://keycloak.test",
        keycloak_admin_secret_path=tmp_path / "nope",
    )

    with pytest.raises(FileNotFoundError):
        KeycloakAdminConfig.from_settings(settings)


def test_an_empty_secret_file_raises(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("   \n")
    settings = SimpleNamespace(
        keycloak_admin_url="https://keycloak.test",
        keycloak_admin_secret_path=secret,
    )

    with pytest.raises(ValueError):
        KeycloakAdminConfig.from_settings(settings)


def test_it_reads_the_secret_from_the_file_and_uses_the_provisioning_client(
    tmp_path: Path,
) -> None:
    """A SEPARATE client id from `it-suite-events`, and the default is the
    provisioning one. One client holding both `view-events` and `manage-users`
    means the smaller job carries the bigger blast radius for ever (spec §2.1)."""
    secret = tmp_path / "secret"
    secret.write_text("s3cr3t\n")
    settings = SimpleNamespace(
        keycloak_admin_url="https://keycloak.test/",
        keycloak_admin_secret_path=secret,
        keycloak_realm="it-suite",
        keycloak_admin_client_id="it-suite-provisioning",
    )

    config = KeycloakAdminConfig.from_settings(settings)

    assert config is not None
    assert config.client_secret == "s3cr3t"
    assert config.client_id == "it-suite-provisioning"
    assert config.base_url == "https://keycloak.test"  # trailing slash stripped


def test_the_provisioning_client_id_is_not_the_events_client_id(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("s3cr3t")
    settings = SimpleNamespace(
        keycloak_admin_url="https://keycloak.test",
        keycloak_admin_secret_path=secret,
        keycloak_realm="it-suite",
        keycloak_admin_client_id="it-suite-provisioning",
        keycloak_events_client_id="it-suite-events",
    )

    config = KeycloakAdminConfig.from_settings(settings)

    assert config is not None
    assert config.client_id != settings.keycloak_events_client_id


# --------------------------------------------------------------------------- #
# create
# --------------------------------------------------------------------------- #


async def test_create_posts_the_user_and_reads_the_id_out_of_location(
    patched: Any,
) -> None:
    """Keycloak's create returns NO BODY. The new user's id -- which IS the
    `sub` claim the token will carry -- comes back in `Location`. Reading it is
    what lets the ITMS row be written with the right `external_subject` on the
    first try (spec §2.2 rule 3)."""
    patched.status = 201

    user_id = await create_user(CONFIG, email="ada@toeshee.com", display_name="Ada Lovelace")

    assert user_id == NEW_ID
    token_request, create_request = patched.seen
    assert token_request.url.path == "/realms/it-suite/protocol/openid-connect/token"
    assert create_request.method == "POST"
    assert create_request.url.path == "/admin/realms/it-suite/users"


async def test_create_asks_for_a_passkey_at_first_sign_in(patched: Any) -> None:
    """Spec §4: ITMS-created users get `webauthn-register-passwordless` so the
    first thing they do is enrol a passkey."""
    patched.status = 201

    await create_user(CONFIG, email="ada@toeshee.com", display_name="Ada Lovelace")

    body = json.loads(patched.seen[1].content)
    assert body["email"] == "ada@toeshee.com"
    assert body["username"] == "ada@toeshee.com"
    assert body["enabled"] is True
    assert PASSKEY_REQUIRED_ACTION in body["requiredActions"]


async def test_create_never_sends_a_password(patched: Any) -> None:
    """RULE 1, at the wire. No password ever transits ITMS -- so the user
    representation carries no `credentials` array and nothing password-shaped
    anywhere in it."""
    patched.status = 201

    await create_user(CONFIG, email="ada@toeshee.com", display_name="Ada Lovelace")

    body = json.loads(patched.seen[1].content)
    assert "credentials" not in body
    assert not any("password" in key.lower() for key in body)
    # The ONLY occurrence of the substring in the whole payload is the passkey
    # required action's name, which is not a password and is the point.
    raw = patched.seen[1].content.decode().lower()
    assert "password" not in raw.replace(PASSKEY_REQUIRED_ACTION, "")


async def test_a_create_that_returns_no_location_raises(patched: Any) -> None:
    """Without the id there is no `external_subject` to write, and guessing one
    strands a row holding roles no identity can ever claim. Better to fail here,
    where the account exists with no roles -- the state of any federated user
    before first login, which `deps.py` already self-heals."""
    patched.status = 201
    patched.location = None

    with pytest.raises(KeycloakAdminError):
        await create_user(CONFIG, email="ada@toeshee.com", display_name="Ada")


async def test_a_conflicting_email_raises_rather_than_reporting_success(
    patched: Any,
) -> None:
    """THE 409 THAT MUST NOT BE QUIET. Keycloak answers 409 when the username or
    email is taken. Swallowing it would write an ITMS row pointing at somebody
    else's identity."""
    patched.status = 409

    with pytest.raises(httpx.HTTPStatusError):
        await create_user(CONFIG, email="ada@toeshee.com", display_name="Ada")


# --------------------------------------------------------------------------- #
# enable / disable
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("enabled", [True, False])
async def test_set_enabled_puts_the_flag_on_the_user(patched: Any, enabled: bool) -> None:
    await set_enabled(CONFIG, user_id=NEW_ID, enabled=enabled)

    _token, update = patched.seen
    assert update.method == "PUT"
    assert update.url.path == f"/admin/realms/it-suite/users/{NEW_ID}"
    assert json.loads(update.content) == {"enabled": enabled}


async def test_a_refused_disable_raises(patched: Any) -> None:
    """A disable that reported success without happening leaves an account the
    console believes is off and Keycloak believes is on."""
    patched.status = 403

    with pytest.raises(httpx.HTTPStatusError):
        await set_enabled(CONFIG, user_id=NEW_ID, enabled=False)


# --------------------------------------------------------------------------- #
# execute-actions-email -- the whole of "reset password" and "enrol"
# --------------------------------------------------------------------------- #


async def test_execute_actions_email_puts_the_action_list(patched: Any) -> None:
    await execute_actions_email(CONFIG, user_id=NEW_ID, actions=PASSWORD_RESET_ACTIONS)

    _token, mail = patched.seen
    assert mail.method == "PUT"
    assert mail.url.path == f"/admin/realms/it-suite/users/{NEW_ID}/execute-actions-email"
    assert json.loads(mail.content) == ["UPDATE_PASSWORD"]


def test_a_password_reset_is_an_update_password_action_and_nothing_else() -> None:
    """RULE 1 again, as a constant rather than a call. "Reset password" in this
    platform means "ask Keycloak to email the user a link". There is no branch
    anywhere that generates one."""
    assert PASSWORD_RESET_ACTIONS == ("UPDATE_PASSWORD",)


def test_enrolment_delivers_both_a_credential_and_a_passkey_prompt() -> None:
    assert "UPDATE_PASSWORD" in ENROLMENT_ACTIONS
    assert PASSKEY_REQUIRED_ACTION in ENROLMENT_ACTIONS


async def test_a_realm_with_no_smtp_raises_so_the_caller_can_report_it(
    patched: Any,
) -> None:
    """SPEC §2.3, THE PATH THIS DEPLOYMENT IS ACTUALLY ON.
    `deploy/keycloak/realm-it-suite.json` has no `smtpServer`, so this call
    fails with a 500. Swallowing it would report "user created, check your
    email" for an email that was never sent, and Keycloak 26 exposes no admin
    API that returns the link for an operator to pass on by hand -- the account
    would be unreachable for ever with every status green."""
    patched.status = 500

    with pytest.raises(httpx.HTTPStatusError):
        await execute_actions_email(CONFIG, user_id=NEW_ID, actions=ENROLMENT_ACTIONS)


# --------------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------------- #


async def test_delete_removes_the_keycloak_identity(patched: Any) -> None:
    await delete_user(CONFIG, user_id=NEW_ID)

    _token, delete = patched.seen
    assert delete.method == "DELETE"
    assert delete.url.path == f"/admin/realms/it-suite/users/{NEW_ID}"


async def test_a_failed_delete_raises_so_the_itms_row_is_not_marked_deleted(
    patched: Any,
) -> None:
    """The ITMS row is marked `deleted` only if the identity really went away.
    A swallowed failure would show a deleted user in the console who can still
    sign in."""
    patched.status = 500

    with pytest.raises(httpx.HTTPStatusError):
        await delete_user(CONFIG, user_id=NEW_ID)


# --------------------------------------------------------------------------- #
# properties that hold across every call
# --------------------------------------------------------------------------- #


async def test_the_token_is_a_bearer_header_and_the_secret_is_never_in_a_url(
    patched: Any,
) -> None:
    """A credential in a URL is a credential in every proxy log and every
    Keycloak access log between here and there."""
    patched.status = 201
    await create_user(CONFIG, email="a@toeshee.com", display_name="A")
    await set_enabled(CONFIG, user_id=NEW_ID, enabled=False)
    await execute_actions_email(CONFIG, user_id=NEW_ID, actions=ENROLMENT_ACTIONS)
    await delete_user(CONFIG, user_id=NEW_ID)

    admin_requests = [
        r for r in patched.seen if not r.url.path.endswith("/protocol/openid-connect/token")
    ]
    assert len(admin_requests) == 4
    for request in admin_requests:
        assert request.headers["Authorization"] == "Bearer tok-123"
        assert CONFIG.client_secret not in str(request.url)
        assert "tok-123" not in str(request.url)


async def test_every_call_carries_a_timeout(patched: Any) -> None:
    """A Keycloak that is wedged must not hold a request worker open. Asserted
    on the client's configuration rather than by hanging a fake server, because
    a test that waits out a timeout costs that timeout on every run."""
    from app.modules.user_admin.kc_client import _TIMEOUT

    await delete_user(CONFIG, user_id=NEW_ID)

    assert _TIMEOUT > 0
    assert patched.kwargs["timeout"] == _TIMEOUT


def test_the_module_exposes_exactly_the_six_operations_and_no_impersonation() -> None:
    """`manage-users` also carries IMPERSONATION, and the credential cannot be
    split (spec §2.1). What CAN be bounded is the code: this module has six
    operations and impersonation is not one of them. The assertion is on the
    module's public surface so adding a seventh is a deliberate, reviewed act."""
    from app.modules.user_admin import kc_client

    exported = {n for n in kc_client.__all__ if n[0].islower()}
    assert exported == {
        "create_user",
        "delete_user",
        "execute_actions_email",
        "revoke_consent",
        "set_enabled",
        "set_locale_attribute",
    }
    # The module DOCSTRING names impersonation deliberately, to record that it
    # is never called; the assertion is that no CODE does. Stripping the
    # docstring is what makes the two statements compatible instead of one
    # silently weakening the other.
    source = Path(kc_client.__file__).read_text()
    docstring = ast.get_docstring(ast.parse(source)) or ""
    assert docstring
    assert "impersonation" not in source.replace(docstring, "").lower()
