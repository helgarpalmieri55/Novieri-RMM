"""The `windows_domain` credential type: a pushable credential for WinRM,
dedicated exactly as `push_install` is (design §2.5, spec §6.2)."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.agent_push.credential_scope import (
    PUSHABLE_CREDENTIAL_TYPES,
    WINDOWS_DOMAIN_CREDENTIAL_TYPE,
    refuse_push_install_credential,
    resolve_push_credential,
)
from app.modules.agent_push.errors import PushValidationError
from tests.agent_push_fixtures import seed_credential, seed_org


def test_windows_domain_is_pushable() -> None:
    assert WINDOWS_DOMAIN_CREDENTIAL_TYPE == "windows_domain"
    assert PUSHABLE_CREDENTIAL_TYPES == {"push_install", "windows_domain"}


async def test_resolve_accepts_windows_domain_and_still_refuses_others(admin_engine) -> None:
    org = await seed_org(admin_engine, "wd")
    # The REAL helper (tests/agent_push_fixtures.py): seed_credential(env_or_engine,
    # org: PushOrg, *, credential_type=..., name=...) and it returns a UUID.
    cred_id = await seed_credential(admin_engine, org, credential_type="windows_domain")
    ssh_id = await seed_credential(admin_engine, org, credential_type="ssh_key")
    async with AsyncSession(admin_engine) as session:
        resolved = await resolve_push_credential(session, org.org_id, cred_id)
        assert resolved.type == "windows_domain"
        with pytest.raises(PushValidationError):
            await resolve_push_credential(session, org.org_id, ssh_id)


async def test_a_windows_domain_credential_is_refused_to_remote_control(admin_engine) -> None:
    """The reverse direction of the dedication (spec §6.2): a `windows_domain`
    credential may no more be presented by remote control or provisioning than
    a `push_install` one may. `refuse_push_install_credential` is what
    `app/modules/remote/service.py` calls before presenting a stored secret."""
    org = await seed_org(admin_engine, "wd-rev")
    wd = await seed_credential(admin_engine, org, credential_type="windows_domain")
    pi = await seed_credential(admin_engine, org, credential_type="push_install")
    other = await seed_credential(admin_engine, org, credential_type="ssh_key")
    async with AsyncSession(admin_engine) as session:
        for cred in (wd, pi):
            with pytest.raises(PushValidationError, match="dedicated to agent push install"):
                await refuse_push_install_credential(
                    session, org_id=org.org_id, credential_id=cred
                )
        # Any other type is not this module's to refuse.
        await refuse_push_install_credential(session, org_id=org.org_id, credential_id=other)


# --------------------------------------------------------------------------- #
# Review I1: the realm comes from the CREDENTIAL, recorded at save time
# --------------------------------------------------------------------------- #

import json  # noqa: E402

from sqlalchemy import select  # noqa: E402

from app.models import Credential  # noqa: E402
from app.modules.agent_push.models import PushCredentialRealm  # noqa: E402


def _wd(username: str, **extra: str) -> str:
    return json.dumps({"username": username, "secret": "hunter2", **extra})


async def _admin_headers(api_env, tag: str):
    org_id = await api_env.create_org(tag)
    admin = await api_env.create_user(org_id, "org-admin")
    return org_id, await api_env.headers(org_id, admin)


async def _realms(api_env, org_id) -> dict[str, str]:
    async with AsyncSession(api_env.admin_engine) as session:
        rows = (
            await session.execute(
                select(PushCredentialRealm).where(PushCredentialRealm.org_id == org_id)
            )
        ).scalars().all()
        return {str(r.credential_id): r.realm for r in rows}


async def test_a_upn_account_is_saved_with_its_realm(api_env) -> None:
    org_id, headers = await _admin_headers(api_env, "wd-upn")
    response = await api_env.client.post(
        "/api/v1/credentials",
        json={"name": "push-win", "type": "windows_domain",
              "secret": _wd("svc-push@Corp.Example.com", kdc="", ca_bundle="")},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    assert await _realms(api_env, org_id) == {response.json()["id"]: "CORP.EXAMPLE.COM"}


@pytest.mark.parametrize(
    "username",
    ["CORP\\svc-push", "svc-push", "svc-push@corp", "svc-push@10.0.0.1", "svc@corp..example.com"],
)
async def test_an_account_that_names_no_realm_is_refused_at_save(api_env, username) -> None:
    """`DOMAIN\\user` carries a NetBIOS name, not a Kerberos realm. Taking the
    realm from each TARGET's DNS name instead let whatever wrote that name choose
    which domain controller the account's pre-authentication went to."""
    org_id, headers = await _admin_headers(api_env, "wd-bad")
    response = await api_env.client.post(
        "/api/v1/credentials",
        json={"name": "push-win", "type": "windows_domain", "secret": _wd(username)},
        headers=headers,
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "user@" in detail and "windows_domain" not in detail
    async with AsyncSession(api_env.admin_engine) as session:
        assert (
            await session.execute(select(Credential).where(Credential.org_id == org_id))
        ).first() is None


async def test_a_windows_domain_secret_that_is_not_the_json_blob_is_refused(api_env) -> None:
    _org_id, headers = await _admin_headers(api_env, "wd-json")
    response = await api_env.client.post(
        "/api/v1/credentials",
        json={"name": "push-win", "type": "windows_domain", "secret": "hunter2"},
        headers=headers,
    )
    assert response.status_code == 422
    assert "hunter2" not in response.text


async def test_rotating_a_windows_domain_account_re_records_its_realm(api_env) -> None:
    org_id, headers = await _admin_headers(api_env, "wd-rot")
    cred_id = (
        await api_env.client.post(
            "/api/v1/credentials",
            json={
                "name": "push-win", "type": "windows_domain", "secret": _wd("a@corp.example.com"),
            },
            headers=headers,
        )
    ).json()["id"]
    refused = await api_env.client.put(
        f"/api/v1/credentials/{cred_id}",
        json={"name": "push-win", "secret": _wd("CORP\\a")},
        headers=headers,
    )
    assert refused.status_code == 422
    assert await _realms(api_env, org_id) == {cred_id: "CORP.EXAMPLE.COM"}
    moved = await api_env.client.put(
        f"/api/v1/credentials/{cred_id}",
        json={"name": "push-win", "secret": _wd("a@other.example.org")},
        headers=headers,
    )
    assert moved.status_code == 200, moved.text
    assert await _realms(api_env, org_id) == {cred_id: "OTHER.EXAMPLE.ORG"}


async def test_other_credential_types_are_not_parsed(api_env) -> None:
    org_id, headers = await _admin_headers(api_env, "wd-other")
    response = await api_env.client.post(
        "/api/v1/credentials",
        json={"name": "rc", "type": "windows_admin", "secret": "CORP\\admin plain"},
        headers=headers,
    )
    assert response.status_code == 201
    assert await _realms(api_env, org_id) == {}
