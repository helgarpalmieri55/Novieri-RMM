"""The org's Azure application for Microsoft 365 sign-in on email intake.

Unlike Settings → Email, this client secret IS stored by this platform (the
token exchange is made by this process on every poll), so invariant #3 applies
in full: vault only, never a response, never audit metadata.
Design: docs/superpowers/specs/2026-09-11-mail-oauth-design.md §4.4.
"""

import json
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent, Credential
from tests.test_email_intake_api import _reader_headers

SECRET = "azure-client-secret-value"
PATH = "/api/v1/email-intake/oauth-app"

BODY: dict[str, Any] = {
    "tenant_id": "contoso.onmicrosoft.com",
    "client_id": "11111111-2222-3333-4444-555555555555",
    "client_secret": SECRET,
}


@pytest.fixture()
def console_url(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(api_env.app.state.settings, "public_console_url", "https://itms.example")
    return api_env


async def _admin(api_env: Any) -> tuple[UUID, dict[str, str]]:
    org_id = await api_env.create_org()
    sub = await api_env.create_user(org_id, "org-admin")
    return org_id, await api_env.headers(org_id, sub)


async def test_saving_the_application_never_echoes_the_secret(console_url: Any) -> None:
    api_env = console_url
    _, headers = await _admin(api_env)

    put = await api_env.client.put(PATH, headers=headers, json=BODY)
    get = await api_env.client.get(PATH, headers=headers)

    assert put.status_code == 200, put.text
    for response in (put, get):
        assert SECRET not in response.text
    body = get.json()
    assert body == {
        "tenant_id": BODY["tenant_id"],
        "client_id": BODY["client_id"],
        "has_secret": True,
        "redirect_uri": "https://itms.example/settings/email-intake/oauth/callback",
    }


async def test_an_unconfigured_org_reads_as_empty(console_url: Any) -> None:
    api_env = console_url
    _, headers = await _admin(api_env)

    body = (await api_env.client.get(PATH, headers=headers)).json()

    assert body["has_secret"] is False
    assert body["tenant_id"] == "" and body["client_id"] == ""


async def test_without_a_console_address_the_redirect_uri_is_null_not_a_guess(
    api_env: Any,
) -> None:
    _, headers = await _admin(api_env)

    response = await api_env.client.get(PATH, headers=headers)

    assert response.status_code == 200
    assert response.json()["redirect_uri"] is None


async def test_the_secret_is_vaulted_not_plaintext(console_url: Any) -> None:
    api_env = console_url
    org_id, headers = await _admin(api_env)

    await api_env.client.put(PATH, headers=headers, json=BODY)

    async with AsyncSession(api_env.admin_engine) as session:
        creds = list(
            (await session.execute(select(Credential).where(Credential.org_id == org_id)))
            .scalars()
            .all()
        )
        dump = json.dumps(
            [list(r) for r in (await session.execute(text("SELECT * FROM email_oauth_app")))],
            default=str,
        )
    assert [c.type for c in creds] == ["email_oauth_client"]
    assert SECRET not in (creds[0].ciphertext or "")
    assert SECRET not in dump


async def test_an_omitted_secret_keeps_the_stored_one_on_the_same_credential(
    console_url: Any,
) -> None:
    api_env = console_url
    org_id, headers = await _admin(api_env)
    await api_env.client.put(PATH, headers=headers, json=BODY)

    response = await api_env.client.put(
        PATH, headers=headers, json={"tenant_id": BODY["tenant_id"], "client_id": "other-id"}
    )

    assert response.status_code == 200
    assert response.json()["has_secret"] is True
    assert response.json()["client_id"] == "other-id"
    async with AsyncSession(api_env.admin_engine) as session:
        count = len(
            (await session.execute(select(Credential).where(Credential.org_id == org_id)))
            .scalars()
            .all()
        )
    assert count == 1


async def test_the_first_save_requires_a_secret(console_url: Any) -> None:
    api_env = console_url
    _, headers = await _admin(api_env)

    response = await api_env.client.put(
        PATH, headers=headers, json={"tenant_id": "contoso.onmicrosoft.com", "client_id": "c"}
    )

    assert response.status_code == 400
    assert "client secret" in response.json()["detail"]


@pytest.mark.parametrize("tenant", ["evil.example/x", "a?b", "../x"])
async def test_a_tenant_id_that_could_move_the_host_is_refused(
    console_url: Any, tenant: str
) -> None:
    api_env = console_url
    _, headers = await _admin(api_env)

    response = await api_env.client.put(PATH, headers=headers, json={**BODY, "tenant_id": tenant})

    assert response.status_code in (400, 422)
    assert SECRET not in response.text


async def test_an_over_long_secret_is_refused_without_repeating_it(console_url: Any) -> None:
    api_env = console_url
    _, headers = await _admin(api_env)
    over_long = "z" * 600

    response = await api_env.client.put(
        PATH, headers=headers, json={**BODY, "client_secret": over_long}
    )

    assert response.status_code == 422
    assert over_long not in response.text


async def test_reading_needs_read_and_writing_needs_admin(console_url: Any) -> None:
    api_env = console_url
    org_id = await api_env.create_org()
    reader = await _reader_headers(api_env, org_id, "email_intake.read")

    assert (await api_env.client.get(PATH, headers=reader)).status_code == 200
    assert (await api_env.client.put(PATH, headers=reader, json=BODY)).status_code == 403


async def test_the_audit_row_names_fields_and_no_values(console_url: Any) -> None:
    api_env = console_url
    org_id, headers = await _admin(api_env)

    await api_env.client.put(PATH, headers=headers, json=BODY)

    async with AsyncSession(api_env.admin_engine) as session:
        events = list(
            (await session.execute(select(AuditEvent).where(AuditEvent.org_id == org_id)))
            .scalars()
            .all()
        )
    (event,) = [e for e in events if e.action == "email_intake.oauth_app_configured"]
    assert event.meta == {"fields": ["client_id", "client_secret", "tenant_id"]}
    assert all(SECRET not in json.dumps(e.meta) for e in events)
