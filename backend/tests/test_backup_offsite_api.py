"""Off-site and recovery-key routes: RBAC on every route (invariant 6), no
secret in any response (invariant 3), the recovery key before off-site rule,
and the Object Lock refusal surfacing as a message the operator can act on.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent, BackupOffsiteTarget, BackupRecoveryKey, Credential
from tests.backup_offsite_fixtures import load_recover_tool
from tests.client_scope_support import EVERY_CLIENT, client_with_site
from tests.test_client_record_checks import _holder

SECRET = "wJalrXUtnFEMI-offsite-secret"

_TARGET = {
    "name": "Wasabi EU",
    "endpoint": "https://s3.eu-central-1.wasabisys.com",
    "region": "eu-central-1",
    "bucket": "acme-itms-offsite",
    "prefix": "itms",
    "access_key_id": "AKIAOFFSITE",
    "secret_access_key": SECRET,
}


async def _admin(api_env: Any) -> tuple[UUID, dict[str, str]]:
    org_id = await api_env.create_org("offsite")
    admin = await api_env.create_user(org_id, "org-admin")
    return org_id, await api_env.headers(org_id, admin)


def _no_secret(resp: Any) -> None:
    assert SECRET not in resp.text
    assert "AKIAOFFSITE" not in resp.text
    assert "credential_id" not in resp.text


async def test_create_and_list_a_target_without_ever_returning_the_secret(api_env: Any) -> None:
    org_id, headers = await _admin(api_env)
    created = await api_env.client.post("/api/v1/backup/offsite-targets", json=_TARGET,
                                        headers=headers)
    assert created.status_code == 201, created.text
    _no_secret(created)
    out = created.json()
    assert out["lock_mode"] == "compliance"
    assert out["immutable_days"] == 30
    assert out["state"] == "untested"

    listed = await api_env.client.get("/api/v1/backup/offsite-targets", headers=headers)
    assert listed.status_code == 200
    _no_secret(listed)

    async with AsyncSession(api_env.admin_engine) as s:
        target = (await s.execute(select(BackupOffsiteTarget).where(
            BackupOffsiteTarget.org_id == org_id))).scalar_one()
        cred = (await s.execute(select(Credential).where(
            Credential.id == target.credential_id))).scalar_one()
        events = (await s.execute(select(AuditEvent).where(
            AuditEvent.org_id == org_id))).scalars().all()
    assert cred.type == "s3_offsite"
    assert cred.ciphertext and SECRET not in cred.ciphertext
    assert {e.action for e in events} >= {"credential.created", "backup.offsite_target_created"}
    assert all(SECRET not in str(e.meta) for e in events)


async def test_the_off_site_secret_cannot_be_checked_out_by_a_person(api_env: Any) -> None:
    org_id, headers = await _admin(api_env)
    await api_env.client.post("/api/v1/backup/offsite-targets", json=_TARGET, headers=headers)
    async with AsyncSession(api_env.admin_engine) as s:
        cred_id = (await s.execute(select(Credential.id).where(
            Credential.org_id == org_id, Credential.type == "s3_offsite"))).scalar_one()
    resp = await api_env.client.post(f"/api/v1/credentials/{cred_id}/checkout",
                                     json={"reason": "curious"}, headers=headers)
    assert resp.status_code == 409
    _no_secret(resp)


async def test_offsite_routes_are_gated(api_env: Any) -> None:
    org_id = await api_env.create_org("offsite")
    tech = await api_env.create_user(org_id, "technician")
    ro = await api_env.create_user(org_id, "read-only")
    tech_h = await api_env.headers(org_id, tech)
    ro_h = await api_env.headers(org_id, ro)

    for headers in (tech_h, ro_h):
        resp = await api_env.client.post("/api/v1/backup/offsite-targets", json=_TARGET,
                                         headers=headers)
        assert resp.status_code == 403
        resp = await api_env.client.post(
            "/api/v1/backup/recovery-keys",
            json={"public_key": "x", "stored_confirmed": True},
            headers=headers,
        )
        assert resp.status_code == 403
    assert (await api_env.client.get("/api/v1/backup/offsite-targets",
                                     headers=ro_h)).status_code == 200
    assert (await api_env.client.get("/api/v1/backup/recovery-keys",
                                     headers=ro_h)).status_code == 200


async def test_the_recovery_key_wizard_end_to_end(api_env: Any) -> None:
    tool = load_recover_tool()
    _org_id, headers = await _admin(api_env)
    private_text, public_text = tool.keygen()

    refused = await api_env.client.post(
        "/api/v1/backup/recovery-keys",
        json={"public_key": public_text, "stored_confirmed": False}, headers=headers,
    )
    assert refused.status_code == 422

    created = await api_env.client.post(
        "/api/v1/backup/recovery-keys",
        json={"public_key": public_text, "stored_confirmed": True}, headers=headers,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["key"]["proven_at"] is None
    assert private_text.split(":", 1)[1] not in created.text
    key_id = body["key"]["id"]

    wrong = await api_env.client.post(f"/api/v1/backup/recovery-keys/{key_id}/prove",
                                      json={"answer": "AAAA"}, headers=headers)
    assert wrong.status_code == 422
    assert "does not match" in wrong.json()["detail"]

    answer = tool.prove(tool.load_private_key(private_text), body["challenge"])
    proven = await api_env.client.post(f"/api/v1/backup/recovery-keys/{key_id}/prove",
                                       json={"answer": answer}, headers=headers)
    assert proven.status_code == 200, proven.text
    assert proven.json()["proven_at"] is not None


async def test_enabling_off_site_without_a_recovery_key_is_a_409_that_says_why(
    api_env: Any,
) -> None:
    org_id, headers = await _admin(api_env)
    repo = await api_env.client.post("/api/v1/backup/repositories",
                                     json={"name": "primary", "client_id": str(org_id)},
                                     headers=headers)
    target = await api_env.client.post("/api/v1/backup/offsite-targets", json=_TARGET,
                                       headers=headers)
    resp = await api_env.client.put(
        f"/api/v1/backup/repositories/{repo.json()['id']}/offsite",
        json={"target_id": target.json()["id"]}, headers=headers,
    )
    assert resp.status_code == 409
    assert "recovery key" in resp.json()["detail"]


async def test_requesting_a_test_leaves_decryption_to_the_worker(api_env: Any) -> None:
    org_id, headers = await _admin(api_env)
    target = await api_env.client.post("/api/v1/backup/offsite-targets", json=_TARGET,
                                       headers=headers)
    resp = await api_env.client.post(
        f"/api/v1/backup/offsite-targets/{target.json()['id']}/test", headers=headers
    )
    assert resp.status_code == 202
    assert resp.json()["state"] == "testing"
    async with AsyncSession(api_env.admin_engine) as s:
        accessed = (await s.execute(select(AuditEvent).where(
            AuditEvent.org_id == org_id, AuditEvent.action == "credential.accessed"
        ))).scalars().all()
    assert accessed == []


async def test_changing_the_bucket_or_the_key_requires_a_new_test(api_env: Any) -> None:
    _org_id, headers = await _admin(api_env)
    target = (await api_env.client.post("/api/v1/backup/offsite-targets", json=_TARGET,
                                        headers=headers)).json()
    half = {**_TARGET, "secret_access_key": None}
    resp = await api_env.client.put(f"/api/v1/backup/offsite-targets/{target['id']}",
                                    json=half, headers=headers)
    assert resp.status_code == 422
    keep_key = {k: v for k, v in _TARGET.items()
                if k not in ("access_key_id", "secret_access_key")}
    resp = await api_env.client.put(f"/api/v1/backup/offsite-targets/{target['id']}",
                                    json={**keep_key, "bucket": "another-bucket"},
                                    headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "untested"
    _no_secret(resp)


async def _offsite_state(api_env: Any, org_id: UUID) -> Any:
    """Every target and key as stored, the credentials behind them, and the
    backup and credential audit rows: what a refused write must leave."""
    async with AsyncSession(api_env.admin_engine) as s:
        targets = (await s.execute(select(
            BackupOffsiteTarget.id, BackupOffsiteTarget.name, BackupOffsiteTarget.bucket,
            BackupOffsiteTarget.state, BackupOffsiteTarget.client_id,
        ).where(BackupOffsiteTarget.org_id == org_id).order_by(BackupOffsiteTarget.id))).all()
        keys = (await s.execute(select(
            BackupRecoveryKey.id, BackupRecoveryKey.challenge_sha256,
            BackupRecoveryKey.proven_at, BackupRecoveryKey.client_id,
        ).where(BackupRecoveryKey.org_id == org_id).order_by(BackupRecoveryKey.id))).all()
        credentials = (await s.execute(select(Credential.id).where(
            Credential.org_id == org_id).order_by(Credential.id))).scalars().all()
        audits = (await s.execute(select(AuditEvent.action).where(
            AuditEvent.org_id == org_id,
            AuditEvent.action.like("backup.%") | AuditEvent.action.like("credential.%"),
        ).order_by(AuditEvent.id))).scalars().all()
    return targets, keys, credentials, audits


async def test_every_target_and_key_write_needs_a_scope_of_every_client(
    api_env: Any,
) -> None:
    """No route writes a client on an off-site target or a recovery key, and
    none changes one, so every target and key is a global row (an O table,
    no client) and every write of one is a write of every client: refused
    with the every-client sentence to a per-client holder of the code and
    to an every-client holder narrowed by `X-Client-Id`, leaving every row
    and audit as it was; let through un-narrowed. That is why these six are
    in `GLOBAL_WRITES` and the console never sends the header on them
    (final review A7; review C Important 2).
    FALSIFIER: a target or key route writing a client (a narrowed holder of
    that client then gets through), or any of the six losing
    `require_permission_on_every_client` (`offsite_api.py`): `prove` with a
    wrong answer writes no row, so only the gate refuses it."""
    tool = load_recover_tool()
    org_id, every = await _admin(api_env)
    x, _ = await client_with_site(api_env, org_id)
    _, per_client = await _holder(
        api_env, org_id, {x: ("backup.read", "backup.offsite.manage", "backup.recovery.manage")}
    )
    narrowed = {**every, "X-Client-Id": str(x)}

    target = await api_env.client.post(
        "/api/v1/backup/offsite-targets", json=_TARGET, headers=every
    )
    assert target.status_code == 201, target.text
    target_id = target.json()["id"]
    private_text, public_text = tool.keygen()
    registered = await api_env.client.post(
        "/api/v1/backup/recovery-keys",
        json={"public_key": public_text, "stored_confirmed": True},
        headers=every,
    )
    assert registered.status_code == 201, registered.text
    key_id = registered.json()["key"]["id"]
    answer = tool.prove(tool.load_private_key(private_text), registered.json()["challenge"])
    _, other_public = tool.keygen()
    keep_key = {k: v for k, v in _TARGET.items() if k not in ("access_key_id", "secret_access_key")}
    writes: list[tuple[str, str, Any]] = [
        ("POST", "/api/v1/backup/offsite-targets", {**_TARGET, "name": "Another"}),
        ("PUT", f"/api/v1/backup/offsite-targets/{target_id}", {**keep_key, "bucket": "moved"}),
        ("POST", f"/api/v1/backup/offsite-targets/{target_id}/test", None),
        (
            "POST", "/api/v1/backup/recovery-keys",
            {"public_key": other_public, "stored_confirmed": True},
        ),
        ("POST", f"/api/v1/backup/recovery-keys/{key_id}/prove", {"answer": "AAAA"}),
        ("POST", f"/api/v1/backup/recovery-keys/{key_id}/prove", {"answer": answer}),
        ("POST", f"/api/v1/backup/recovery-keys/{key_id}/challenge", None),
    ]
    before = await _offsite_state(api_env, org_id)
    assert {row.client_id for row in before[0] + before[1]} == {None}

    for who, headers in (("per-client", per_client), ("narrowed", narrowed)):
        for method, url, body in writes:
            r = await api_env.client.request(method, url, json=body, headers=headers)
            assert (r.status_code, r.json()) == (403, {"detail": EVERY_CLIENT}), (
                who, method, url, r.text
            )
    assert await _offsite_state(api_env, org_id) == before

    expected = [201, 200, 202, 201, 422, 200, 200]
    for (method, url, body), status in zip(writes, expected, strict=True):
        r = await api_env.client.request(method, url, json=body, headers=every)
        assert r.status_code == status, (method, url, r.text)


async def test_a_bad_endpoint_and_an_over_long_lock_are_refused(api_env: Any) -> None:
    _org_id, headers = await _admin(api_env)
    for bad in ({"endpoint": "ftp://x"}, {"immutable_days": 91}, {"lock_mode": "none"},
                {"retention_gfs": {"hourly": 3}}):
        resp = await api_env.client.post("/api/v1/backup/offsite-targets",
                                         json={**_TARGET, **bad}, headers=headers)
        assert resp.status_code == 422, bad
