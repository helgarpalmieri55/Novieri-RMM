
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent


async def _create(api_env, headers, name="dc-admin", secret="P@ssw0rd!"):
    return await api_env.client.post(
        "/api/v1/credentials",
        json={"name": name, "type": "windows_admin", "secret": secret},
        headers=headers,
    )


async def test_create_returns_metadata_only(api_env) -> None:
    org_id = await api_env.create_org("cred")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    response = await _create(api_env, headers)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "dc-admin"
    assert body["has_secret"] is True
    # the secret and its ciphertext are NEVER in a response
    for forbidden in ("secret", "ciphertext", "wrapped_dek", "key_version"):
        assert forbidden not in body


async def test_checkout_returns_secret_and_audits(api_env) -> None:
    org_id = await api_env.create_org("cred")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    cred_id = (await _create(api_env, headers)).json()["id"]

    checkout = await api_env.client.post(
        f"/api/v1/credentials/{cred_id}/checkout",
        json={"reason": "helpdesk ticket 4821"},
        headers=headers,
    )
    assert checkout.status_code == 200
    assert checkout.json()["secret"] == "P@ssw0rd!"

    # a second checkout still works (read is idempotent)
    again = await api_env.client.post(
        f"/api/v1/credentials/{cred_id}/checkout",
        json={"reason": "again"},
        headers=headers,
    )
    assert again.json()["secret"] == "P@ssw0rd!"

    async with AsyncSession(api_env.admin_engine) as session:
        events = (
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.org_id == org_id)
                    .order_by(AuditEvent.id)
                )
            )
            .scalars()
            .all()
        )
    actions = [e.action for e in events]
    assert "credential.created" in actions
    assert actions.count("credential.checked_out") == 2
    checkout_event = next(e for e in events if e.action == "credential.checked_out")
    assert checkout_event.meta["reason"] == "helpdesk ticket 4821"
    # the secret never appears in an audit event either
    assert "P@ssw0rd!" not in str(checkout_event.meta)


async def test_rotate_changes_secret(api_env) -> None:
    org_id = await api_env.create_org("cred")
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    cred_id = (await _create(api_env, headers)).json()["id"]

    rotate = await api_env.client.put(
        f"/api/v1/credentials/{cred_id}",
        json={"name": "dc-admin", "secret": "NewP@ss2"},
        headers=headers,
    )
    assert rotate.status_code == 200
    checkout = await api_env.client.post(
        f"/api/v1/credentials/{cred_id}/checkout",
        json={"reason": "verify rotation"},
        headers=headers,
    )
    assert checkout.json()["secret"] == "NewP@ss2"

    async with AsyncSession(api_env.admin_engine) as session:
        actions = [
            e.action
            for e in (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.org_id == org_id)
                )
            ).scalars()
        ]
    assert "credential.rotated" in actions


async def test_rbac_read_cannot_manage(api_env) -> None:
    org_id = await api_env.create_org("cred")
    admin = await api_env.create_user(org_id, "org-admin")
    admin_headers = await api_env.headers(org_id, admin)
    cred_id = (await _create(api_env, admin_headers)).json()["id"]

    # auditor has credentials.read (metadata) but NOT manage or checkout
    auditor = await api_env.create_user(org_id, "auditor")
    reader_headers = await api_env.headers(org_id, auditor)
    assert (await _create(api_env, reader_headers)).status_code == 403
    assert (
        await api_env.client.put(
            f"/api/v1/credentials/{cred_id}",
            json={"name": "x", "secret": "y"},
            headers=reader_headers,
        )
    ).status_code == 403
    assert (
        await api_env.client.delete(f"/api/v1/credentials/{cred_id}", headers=reader_headers)
    ).status_code == 403
    # auditor can read metadata but CANNOT decrypt the secret (least privilege)
    assert (
        await api_env.client.get(f"/api/v1/credentials/{cred_id}", headers=reader_headers)
    ).status_code == 200
    denied = await api_env.client.post(
        f"/api/v1/credentials/{cred_id}/checkout",
        json={"reason": "auditor review"},
        headers=reader_headers,
    )
    assert denied.status_code == 403
    # an operator (org-admin, has credentials.checkout) can
    allowed = await api_env.client.post(
        f"/api/v1/credentials/{cred_id}/checkout",
        json={"reason": "operator use"},
        headers=admin_headers,
    )
    assert allowed.status_code == 200


async def test_another_orgs_credential_answers_404_and_delete_still_works(api_env) -> None:
    org_a = await api_env.create_org("cred")
    org_b = await api_env.create_org("cred-b")
    admin_a = await api_env.create_user(org_a, "org-admin")
    headers_a = await api_env.headers(org_a, admin_a)
    admin_b = await api_env.create_user(org_b, "org-admin")
    headers_b = await api_env.headers(org_b, admin_b)

    cred_b = (await _create(api_env, headers_b)).json()["id"]
    # org A cannot see or checkout org B's credential
    assert (
        await api_env.client.get(f"/api/v1/credentials/{cred_b}", headers=headers_a)
    ).status_code == 404

    cred_a = (await _create(api_env, headers_a)).json()["id"]
    deleted = await api_env.client.delete(f"/api/v1/credentials/{cred_a}", headers=headers_a)
    assert deleted.status_code == 200
    assert (
        await api_env.client.get(f"/api/v1/credentials/{cred_a}", headers=headers_a)
    ).status_code == 404

    verify = await api_env.client.get("/api/v1/audit/verify", headers=headers_a)
    assert verify.json()["ok"] is True
