"""`PATCH /me` locale (spec 2026-10-09 §3.5, §4), and the identity-provider
write it no longer makes (P2 phase 1 Task 8; design §1.3 retires "el espejo del
idioma en Keycloak (`me.py`)").

TWO HALVES.

1. The route: the format check (unchanged), and the language staying in the
   platform. `PATCH /me` calls no identity provider whatever is configured,
   and its answer carries no `identity_locale_synced`. The sign-in pages and
   the account mails are the platform's own now and read `users.locale`.
2. `kc_client.set_locale_attribute`, which keeps no caller until phase 4
   retires `kc_client.py` with Keycloak: its read-modify-write is still held
   here, driven directly, so the function cannot rot while it exists.

WHAT THIS CANNOT SHOW: `httpx.MockTransport` proves the requests the function
sends, not what a real Keycloak does with them.
"""

import copy
import json
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
    headers = await api_env.headers(org_id, sub)
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


async def _write(env: Any, locale: str | None) -> None:
    """The client function on its own, against the fake, for this user."""
    await set_locale_attribute(CONFIG, user_id=env.sub, locale=locale)


# --------------------------------------------------------------------------- #
# the route
# --------------------------------------------------------------------------- #


async def test_es_is_stored_and_no_identity_provider_is_told(env: Any) -> None:
    response = await _patch(env, {"locale": "es"})
    assert response.status_code == 200
    body = response.json()
    assert body["locale"] == "es"
    assert "identity_locale_synced" not in body
    assert (await _row(env.api.admin_engine, env.sub)).locale == "es"
    assert env.kc.seen == []


async def test_a_regional_value_stored_earlier_stays_valid(env: Any) -> None:
    response = await _patch(env, {"locale": "en-GB"})
    assert response.status_code == 200
    assert response.json()["locale"] == "en-GB"
    assert env.kc.seen == []


@pytest.mark.parametrize("bad", ["<script>", "e", "en_GB", "english-", "es es", "1234"])
async def test_a_malformed_tag_is_refused_and_writes_nothing(env: Any, bad: str) -> None:
    response = await _patch(env, {"locale": bad})
    assert response.status_code == 422
    assert (await _row(env.api.admin_engine, env.sub)).locale is None
    assert env.kc.seen == []


async def test_no_identity_shaped_field_reaches_anything(env: Any) -> None:
    other = "00000000-0000-0000-0000-00000000dead"
    for field in ("user_id", "external_subject", "sub", "keycloak_id"):
        response = await _patch(env, {"locale": "es", field: other})
        assert response.status_code == 422
    assert env.kc.seen == []


async def test_a_changed_or_cleared_language_is_kept_here_only(env: Any) -> None:
    """The cases the mirror used to treat differently -- a change, a clear,
    null over null, the same value again -- all just save."""
    await _store(env, "es")
    assert (await _patch(env, {"locale": "en"})).status_code == 200
    assert (await _patch(env, {"locale": None})).status_code == 200
    assert (await _patch(env, {"locale": None, "phone": "0101"})).status_code == 200
    assert (await _patch(env, {"locale": "es"})).status_code == 200
    assert (await _patch(env, {"locale": "es"})).status_code == 200
    assert (await _row(env.api.admin_engine, env.sub)).locale == "es"
    assert env.kc.seen == []


async def test_a_broken_provisioning_setting_cannot_touch_the_save(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Was: a misconfigured secret file or an unconfigured Keycloak is skipped.
    Now nothing reads the setting at all on this route."""

    def broken(cls: Any, settings: Any) -> Any:
        raise FileNotFoundError("secret path missing")

    monkeypatch.setattr(KeycloakAdminConfig, "from_settings", classmethod(broken))
    response = await _patch(env, {"locale": "es"})
    assert response.status_code == 200
    assert (await _row(env.api.admin_engine, env.sub)).locale == "es"


async def test_get_me_does_not_claim_a_sync_state(env: Any) -> None:
    response = await env.api.client.get("/api/v1/me", headers=env.headers)
    assert "identity_locale_synced" not in response.json()


async def test_a_failed_client_map_read_saves_nothing_and_the_retry_saves(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`PATCH /me` answers with the client map (phase 3 design §1.3), read
    before the commit: a failed read saves nothing, so a retry is a first try.
    FALSIFIER: `update_own_profile` reading the map after its `org_session`."""
    from app.core.identity import own_profile

    real = own_profile._own_clients
    calls: list[int] = []

    async def unreachable_once(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("the client map could not be read")
        return await real(*args, **kwargs)

    monkeypatch.setattr(own_profile, "_own_clients", unreachable_once)

    try:
        failed = await _patch(env, {"locale": "es"})
    except ConnectionError:
        pass  # the test transport re-raises what the 500 was made of
    else:
        assert failed.status_code == 500, failed.text
    assert (await _row(env.api.admin_engine, env.sub)).locale is None

    retried = await _patch(env, {"locale": "es"})
    assert retried.status_code == 200, retried.text
    assert retried.json()["locale"] == "es"
    assert env.kc.seen == []


# --------------------------------------------------------------------------- #
# kc_client.set_locale_attribute on its own (no caller until phase 4)
# --------------------------------------------------------------------------- #


async def test_the_attribute_update_is_a_get_put_get_with_only_the_profile_fields(
    env: Any,
) -> None:
    before = copy.deepcopy(env.kc.user)
    await _write(env, "es")

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
    await _write(env, "es")
    assert env.kc.user["enabled"] is False


async def test_a_null_locale_removes_the_attribute_and_keeps_the_rest(env: Any) -> None:
    env.kc.user["attributes"]["locale"] = ["es"]
    await _write(env, None)
    sent = env.kc.bodies[0]
    assert set(sent) == PUT_KEYS
    assert sent["attributes"] == {"org": ["acme"]}


async def test_the_target_is_the_subject_given(env: Any) -> None:
    await _write(env, "es")
    assert {r.url.path.rpartition("/")[2] for r in env.kc.seen} == {env.sub}


async def test_a_write_keycloak_accepted_but_did_not_keep_raises(env: Any) -> None:
    """A realm without internationalization answers the PUT 204 and drops
    `locale`; the verifying GET notices."""
    env.kc.drop_locale = True
    with pytest.raises(KeycloakAdminError):
        await _write(env, "es")
    assert env.kc.methods == ["GET", "PUT", "GET"]


async def test_clearing_is_verified_too(env: Any) -> None:
    env.kc.user["attributes"]["locale"] = ["es"]
    original = env.kc.handler

    def handler(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.method == "PUT" and response.status_code < 300:
            env.kc.user["attributes"]["locale"] = ["es"]
        return response

    env.kc.handler = handler  # type: ignore[method-assign]
    with pytest.raises(KeycloakAdminError):
        await _write(env, None)


async def test_a_user_with_no_attributes_map_gets_one(env: Any) -> None:
    del env.kc.user["attributes"]
    await _write(env, "es")
    assert env.kc.bodies[0]["attributes"] == {"locale": ["es"]}


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
async def test_a_keycloak_failure_raises_and_carries_no_secret(
    env: Any, knob: tuple[str, Any]
) -> None:
    setattr(env.kc, knob[0], knob[1])
    with pytest.raises((httpx.HTTPError, KeycloakAdminError)) as caught:
        await _write(env, "es")
    for forbidden in (TOKEN, "not-a-real-secret"):
        assert forbidden not in str(caught.value)


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
