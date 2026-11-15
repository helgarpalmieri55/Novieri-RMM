"""`PATCH /me` locale (spec 2026-10-09 §3.5, §4), and the identity-provider
write it no longer makes (P2 phase 1 Task 8; design §1.3 retires "el espejo del
idioma en Keycloak (`me.py`)").

The format check (unchanged), and the language staying in the platform.
`PATCH /me` calls no identity provider whatever is configured, and its answer
carries no `identity_locale_synced`. The sign-in pages and the account mails
are the platform's own and read `users.locale`. Every `httpx.AsyncClient` the
route could build sends to `test_user_admin_api.OutboundRecorder`, so a call
would be seen; `env.outbound.seen == []` holds the rule. (Until own
authentication phase 4 this file also drove `kc_client.set_locale_attribute`
on its own; that function went with Keycloak.)
"""

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import User
from tests.test_user_admin_api import install_outbound


@pytest.fixture()
async def env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    org_id = await api_env.create_org("loc")
    sub = await api_env.create_user(org_id, "technician")
    outbound = install_outbound(monkeypatch)
    headers = await api_env.headers(org_id, sub)
    return SimpleNamespace(
        api=api_env, org_id=org_id, sub=sub, outbound=outbound, headers=headers
    )


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
# the route
# --------------------------------------------------------------------------- #


async def test_es_is_stored_and_no_identity_provider_is_told(env: Any) -> None:
    response = await _patch(env, {"locale": "es"})
    assert response.status_code == 200
    body = response.json()
    assert body["locale"] == "es"
    assert "identity_locale_synced" not in body
    assert (await _row(env.api.admin_engine, env.sub)).locale == "es"
    assert env.outbound.seen == []


async def test_a_regional_value_stored_earlier_stays_valid(env: Any) -> None:
    response = await _patch(env, {"locale": "en-GB"})
    assert response.status_code == 200
    assert response.json()["locale"] == "en-GB"
    assert env.outbound.seen == []


@pytest.mark.parametrize("bad", ["<script>", "e", "en_GB", "english-", "es es", "1234"])
async def test_a_malformed_tag_is_refused_and_writes_nothing(env: Any, bad: str) -> None:
    response = await _patch(env, {"locale": bad})
    assert response.status_code == 422
    assert (await _row(env.api.admin_engine, env.sub)).locale is None
    assert env.outbound.seen == []


async def test_no_identity_shaped_field_reaches_anything(env: Any) -> None:
    other = "00000000-0000-0000-0000-00000000dead"
    for field in ("user_id", "external_subject", "sub", "keycloak_id"):
        response = await _patch(env, {"locale": "es", field: other})
        assert response.status_code == 422
    assert env.outbound.seen == []


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
    assert env.outbound.seen == []


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
    assert env.outbound.seen == []
