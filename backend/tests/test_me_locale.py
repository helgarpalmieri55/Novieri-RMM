"""`PATCH /me` locale: validation, and the mirror to Keycloak's `locale` attribute.

Spec `docs/superpowers/specs/2026-10-09-spanish-language-design.md` §3.5 and §4.

THREE CLAIMS, AND THE SECOND IS THE ONE THAT CAN DO DAMAGE.

1. The format check. `en-GB` (stored before the console wrote only `en`/`es`)
   stays valid; markup and one-letter tags do not.
2. The Keycloak write is a READ-MODIFY-WRITE of the full user representation.
   Under Keycloak's declarative user profile a PUT that omits `email`,
   `firstName` or `lastName` can clear them, and a PUT of `attributes` replaces
   the whole map -- so a partial body would erase `org`, which is the claim
   every tenancy decision in this platform reads. The tests below assert that
   what goes back is what came out, plus exactly one changed key.
3. Keycloak is never allowed to fail the save. The console value is committed
   first; a Keycloak error becomes `identity_locale_synced: false`.

WHAT THIS CANNOT SHOW: `httpx.MockTransport` proves the requests this code
sends, not what a real Keycloak does with them. Whether `locale` is accepted
depends on the realm having internationalization enabled (Task 20).
"""

import copy
import json
import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import User
from app.modules.user_admin.kc_client import (
    KeycloakAdminConfig,
    KeycloakAdminError,
    set_locale_attribute,
)

_REAL_CLIENT = httpx.AsyncClient

CONFIG = KeycloakAdminConfig(
    base_url="https://keycloak.test",
    realm="it-suite",
    client_id="it-suite-provisioning",
    client_secret="not-a-real-secret",
)

TOKEN = "tok-never-log-me"


def _representation(sub: str) -> dict[str, Any]:
    return {
        "id": sub,
        "username": "ada@example.test",
        "email": "ada@example.test",
        "emailVerified": True,
        "firstName": "Ada",
        "lastName": "Lovelace",
        "enabled": True,
        "attributes": {"org": ["acme"]},
        "requiredActions": [],
        # Fields a real GET returns that a language save must never send back:
        # a stale copy of any of them would undo a concurrent admin change.
        "notBefore": 0,
        "totp": False,
        "access": {"manage": True},
        "createdTimestamp": 1700000000000,
        "disableableCredentialTypes": [],
        "federationLink": None,
        "credentials": [],
        "userProfileMetadata": {"attributes": []},
    }

#: The only keys the PUT may carry (the profile fields Keycloak would otherwise
#: treat as cleared, and the merged attributes).
PUT_KEYS = {"username", "email", "firstName", "lastName", "attributes"}


class FakeKeycloak:
    def __init__(self, sub: str) -> None:
        self.sub = sub
        self.user = _representation(sub)
        self.seen: list[httpx.Request] = []
        self.bodies: list[Any] = []
        self.get_status = 200
        self.put_status = 204
        self.token_status = 200
        self.raise_connect = False
        #: Accept the PUT (204) but silently drop `locale`, as a realm without
        #: internationalization would.
        self.drop_locale = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.raise_connect:
            raise httpx.ConnectError("boom", request=request)
        path = request.url.path
        if path.endswith("/protocol/openid-connect/token"):
            if self.token_status != 200:
                return httpx.Response(self.token_status)
            return httpx.Response(200, json={"access_token": TOKEN})
        self.seen.append(request)
        assert path.endswith(f"/users/{self.sub}"), f"unexpected path {path}"
        if request.method == "GET":
            return httpx.Response(self.get_status, json=self.user)
        if request.method == "PUT":
            self.bodies.append(json.loads(request.content))
            if self.put_status < 300:
                # Keycloak replaces the keys it is sent and leaves every other
                # field as it was.
                body = json.loads(request.content)
                if self.drop_locale:
                    body["attributes"] = {
                        k: v for k, v in body["attributes"].items() if k != "locale"
                    }
                self.user = {**self.user, **body}
            return httpx.Response(self.put_status)
        raise AssertionError(f"unexpected call {request.method} {path}")

    @property
    def methods(self) -> list[str]:
        return [r.method for r in self.seen]


def _install(monkeypatch: pytest.MonkeyPatch, kc: FakeKeycloak) -> None:
    def factory(**kw: Any) -> httpx.AsyncClient:
        return _REAL_CLIENT(transport=httpx.MockTransport(kc.handler), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


@pytest.fixture()
async def env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    org_id = await api_env.create_org("loc")
    sub = await api_env.create_user(org_id, "technician")
    kc = FakeKeycloak(sub)
    _install(monkeypatch, kc)
    monkeypatch.setattr(
        KeycloakAdminConfig, "from_settings", classmethod(lambda cls, settings: CONFIG)
    )
    slug = await api_env.org_slug(org_id)
    headers = {"Authorization": f"Bearer {api_env.idp.token(sub, slug, email=f'{sub}@test.local')}"}
    return SimpleNamespace(api=api_env, org_id=org_id, sub=sub, kc=kc, headers=headers)


async def _row(engine: AsyncEngine, sub: str) -> User:
    async with AsyncSession(engine) as session:
        return (
            await session.execute(select(User).where(User.external_subject == sub))
        ).scalar_one()


async def _store(env: Any, locale: str | None) -> None:
    """Put `users.locale` in a known state without going through the route."""
    async with AsyncSession(env.api.admin_engine) as session:
        user = (
            await session.execute(select(User).where(User.external_subject == env.sub))
        ).scalar_one()
        user.locale = locale
        await session.commit()


async def _patch(env: Any, body: dict[str, Any]) -> httpx.Response:
    return await env.api.client.patch("/api/v1/me", headers=env.headers, json=body)


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #


async def test_es_is_stored_and_mirrored(env: Any) -> None:
    response = await _patch(env, {"locale": "es"})
    assert response.status_code == 200
    body = response.json()
    assert body["locale"] == "es"
    assert body["identity_locale_synced"] is True
    assert (await _row(env.api.admin_engine, env.sub)).locale == "es"


async def test_a_regional_value_stored_earlier_stays_valid(env: Any) -> None:
    response = await _patch(env, {"locale": "en-GB"})
    assert response.status_code == 200
    assert response.json()["locale"] == "en-GB"
    # Mirrored exactly as stored; Keycloak falls back to its default for it.
    assert env.kc.bodies[0]["attributes"]["locale"] == ["en-GB"]


@pytest.mark.parametrize("bad", ["<script>", "e", "en_GB", "english-", "es es", "1234"])
async def test_a_malformed_tag_is_refused_and_writes_nothing(env: Any, bad: str) -> None:
    response = await _patch(env, {"locale": bad})
    assert response.status_code == 422
    assert (await _row(env.api.admin_engine, env.sub)).locale is None
    assert env.kc.seen == []


# --------------------------------------------------------------------------- #
# the Keycloak write is read-modify-write
# --------------------------------------------------------------------------- #


async def test_the_attribute_update_is_a_get_put_get_with_only_the_profile_fields(
    env: Any,
) -> None:
    before = copy.deepcopy(env.kc.user)
    response = await _patch(env, {"locale": "es"})
    assert response.status_code == 200

    # GET, PUT, then a GET that checks Keycloak kept it.
    assert env.kc.methods == ["GET", "PUT", "GET"]
    sent = env.kc.bodies[0]
    # EXACTLY these keys: `enabled`, `requiredActions`, `emailVerified`,
    # `notBefore`, `totp`, `access`, `credentials`... stay out, so a language
    # save cannot re-enable a user an admin disabled in the meantime.
    assert set(sent) == PUT_KEYS
    for kept in ("email", "firstName", "lastName", "username"):
        assert sent[kept] == before[kept]
    assert sent["attributes"] == {"org": ["acme"], "locale": ["es"]}
    # And the fields it did not send are untouched on the fake's side too.
    assert env.kc.user["enabled"] is True
    assert env.kc.user["requiredActions"] == []


async def test_a_concurrent_disable_is_not_undone(env: Any) -> None:
    """The GET says enabled; an admin disables the user before the PUT lands.
    Because the PUT carries no `enabled`, the disable survives."""
    original = env.kc.handler

    def handler(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.method == "GET" and len(env.kc.seen) == 1:
            env.kc.user["enabled"] = False  # the admin acts after our read
        return response

    env.kc.handler = handler  # type: ignore[method-assign]
    response = await _patch(env, {"locale": "es"})
    assert response.status_code == 200
    assert env.kc.user["enabled"] is False


async def test_a_null_locale_removes_the_attribute_and_keeps_the_rest(env: Any) -> None:
    env.kc.user["attributes"]["locale"] = ["es"]
    await _store(env, "es")

    response = await _patch(env, {"locale": None})

    assert response.status_code == 200
    assert response.json()["identity_locale_synced"] is True
    sent = env.kc.bodies[0]
    assert set(sent) == PUT_KEYS
    assert sent["attributes"] == {"org": ["acme"]}


async def test_the_target_is_the_callers_own_subject_and_no_field_redirects_it(
    env: Any,
) -> None:
    other = "00000000-0000-0000-0000-00000000dead"
    # The request model is closed: an identity-shaped field is a 422, and
    # nothing at all reaches Keycloak.
    for field in ("user_id", "external_subject", "sub", "keycloak_id"):
        response = await _patch(env, {"locale": "es", field: other})
        assert response.status_code == 422
    assert env.kc.seen == []
    # And the one legitimate call addresses `external_subject` of the caller
    # (the fake asserts the path ends with this user's id).
    assert (await _patch(env, {"locale": "es"})).status_code == 200
    assert {r.url.path.rpartition("/")[2] for r in env.kc.seen} == {env.sub}


async def test_a_write_keycloak_accepted_but_did_not_keep_is_reported_as_not_synced(
    env: Any,
) -> None:
    """A realm without internationalization answers the PUT 204 and drops
    `locale`. The flag must not say `true` for that."""
    env.kc.drop_locale = True
    response = await _patch(env, {"locale": "es"})
    assert response.status_code == 200
    assert response.json()["identity_locale_synced"] is False
    assert (await _row(env.api.admin_engine, env.sub)).locale == "es"
    assert env.kc.methods == ["GET", "PUT", "GET"]


async def test_clearing_is_verified_too(env: Any) -> None:
    env.kc.user["attributes"]["locale"] = ["es"]
    await _store(env, "es")

    # Keycloak that keeps the old attribute despite the PUT.
    original = env.kc.handler

    def handler(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.method == "PUT" and response.status_code < 300:
            env.kc.user["attributes"]["locale"] = ["es"]
        return response

    env.kc.handler = handler  # type: ignore[method-assign]
    response = await _patch(env, {"locale": None})
    assert response.json()["identity_locale_synced"] is False


async def test_the_same_language_as_stored_is_not_mirrored(env: Any) -> None:
    await _store(env, "es")
    response = await _patch(env, {"locale": "es"})
    assert response.status_code == 200
    assert response.json()["identity_locale_synced"] is None
    assert env.kc.seen == []


async def test_null_over_null_is_not_mirrored_and_cannot_delete_an_admin_set_value(
    env: Any,
) -> None:
    # The console has nothing stored; an admin set a Keycloak locale by hand.
    env.kc.user["attributes"]["locale"] = ["fr"]
    response = await _patch(env, {"locale": None, "phone": "0101"})
    assert response.status_code == 200
    assert response.json()["identity_locale_synced"] is None
    assert env.kc.seen == []
    assert env.kc.user["attributes"]["locale"] == ["fr"]


async def test_a_changed_language_is_mirrored(env: Any) -> None:
    await _store(env, "es")
    response = await _patch(env, {"locale": "en"})
    assert response.json()["identity_locale_synced"] is True
    assert env.kc.bodies[0]["attributes"]["locale"] == ["en"]


async def test_a_wedged_keycloak_is_cut_off_and_the_save_stands(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from app.api.v1 import me as me_module

    assert me_module._MIRROR_TIMEOUT_SECONDS == 5

    async def never(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(me_module, "set_locale_attribute", never)
    monkeypatch.setattr(me_module, "_MIRROR_TIMEOUT_SECONDS", 0.05)
    response = await _patch(env, {"locale": "es"})
    assert response.status_code == 200
    assert response.json()["identity_locale_synced"] is False
    assert (await _row(env.api.admin_engine, env.sub)).locale == "es"


async def test_a_user_with_no_attributes_map_gets_one(env: Any) -> None:
    del env.kc.user["attributes"]
    assert (await _patch(env, {"locale": "es"})).status_code == 200
    assert env.kc.bodies[0]["attributes"] == {"locale": ["es"]}


# --------------------------------------------------------------------------- #
# Keycloak never fails the save
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "knob",
    [
        ("put_status", 500),
        ("put_status", 403),
        ("get_status", 404),
        ("token_status", 401),
        ("raise_connect", True),
    ],
)
async def test_a_keycloak_failure_still_saves_and_says_so(
    env: Any, caplog: pytest.LogCaptureFixture, knob: tuple[str, Any]
) -> None:
    setattr(env.kc, knob[0], knob[1])
    caplog.set_level(logging.DEBUG)

    response = await _patch(env, {"locale": "es"})

    assert response.status_code == 200
    body = response.json()
    assert body["locale"] == "es"
    assert body["identity_locale_synced"] is False
    assert (await _row(env.api.admin_engine, env.sub)).locale == "es"
    # No secret and no user representation in any log line.
    text = "\n".join(
        f"{r.getMessage()} {r.exc_text or ''} {getattr(r, 'extra', '')}" for r in caplog.records
    )
    for forbidden in (TOKEN, "ada@example.test", "Lovelace", "not-a-real-secret"):
        assert forbidden not in text


async def test_a_misconfigured_secret_file_does_not_fail_the_save(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(cls: Any, settings: Any) -> Any:
        raise FileNotFoundError("secret path missing")

    monkeypatch.setattr(KeycloakAdminConfig, "from_settings", classmethod(broken))
    response = await _patch(env, {"locale": "es"})
    assert response.status_code == 200
    assert response.json()["identity_locale_synced"] is False
    assert (await _row(env.api.admin_engine, env.sub)).locale == "es"


async def test_keycloak_is_not_called_when_locale_was_not_supplied(env: Any) -> None:
    response = await _patch(env, {"phone": "0101"})
    assert response.status_code == 200
    assert response.json()["identity_locale_synced"] is None
    assert env.kc.seen == []


async def test_an_unconfigured_keycloak_is_skipped_not_failed(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        KeycloakAdminConfig, "from_settings", classmethod(lambda cls, settings: None)
    )
    response = await _patch(env, {"locale": "es"})
    assert response.status_code == 200
    assert response.json()["identity_locale_synced"] is None
    assert env.kc.seen == []
    assert (await _row(env.api.admin_engine, env.sub)).locale == "es"


async def test_get_me_does_not_claim_a_sync_state(env: Any) -> None:
    response = await env.api.client.get("/api/v1/me", headers=env.headers)
    assert response.json()["identity_locale_synced"] is None


# --------------------------------------------------------------------------- #
# the client function on its own
# --------------------------------------------------------------------------- #


async def test_set_locale_attribute_raises_on_a_non_object_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kc = FakeKeycloak("abc")
    kc.user = ["not", "an", "object"]  # type: ignore[assignment]
    _install(monkeypatch, kc)
    # The fake asserts the path; give it the matching id.
    with pytest.raises(KeycloakAdminError):
        await set_locale_attribute(CONFIG, user_id="abc", locale="es")
    assert kc.bodies == []


async def test_an_overlapping_save_leaves_keycloak_on_the_latest_language(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two quick switches each commit, then mirror; the earlier mirror can land
    after the later one. Simulated here: while this request's mirror runs,
    another save commits "en". The route reads the saved value again after its
    own write and puts Keycloak on that, so Keycloak never ends on a stale one."""
    import app.api.v1.me as me_route

    real = me_route.set_locale_attribute
    calls: list[str | None] = []

    async def racing(config: Any, *, user_id: str, locale: str | None) -> None:
        calls.append(locale)
        await real(config, user_id=user_id, locale=locale)
        if len(calls) == 1:
            await _store(env, "en")  # a later save committed meanwhile

    monkeypatch.setattr(me_route, "set_locale_attribute", racing)
    await _store(env, None)
    response = await _patch(env, {"locale": "es"})
    assert response.status_code == 200
    assert calls == ["es", "en"]
    assert env.kc.user["attributes"]["locale"] == ["en"]
    assert env.kc.user["attributes"]["org"] == ["acme"]
    assert response.json()["identity_locale_synced"] is True


async def test_no_second_write_when_the_saved_value_did_not_move(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.api.v1.me as me_route

    real = me_route.set_locale_attribute
    calls: list[str | None] = []

    async def counting(config: Any, *, user_id: str, locale: str | None) -> None:
        calls.append(locale)
        await real(config, user_id=user_id, locale=locale)

    monkeypatch.setattr(me_route, "set_locale_attribute", counting)
    await _store(env, None)
    await _patch(env, {"locale": "es"})
    assert calls == ["es"]
