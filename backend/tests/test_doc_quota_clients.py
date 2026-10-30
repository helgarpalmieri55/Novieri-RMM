"""The document quota is per client (operator decision 2026-10-29, final
review D Important 2).

The quota's `SUM` was "whatever this session can see" under the client
policy: an every-client admin was measured against the organisation's total
and a technician narrowed to X against X's plus the documents for every
client, so the same upload got two answers -- and each narrowed scope could
fill its own 5 GiB of a bucket shared with backups. Now each client has its
own quota, counted explicitly for the document's owner (its `client_id`,
the documents for every client being a bucket of their own), whoever asks.
"""

from typing import Any
from uuid import UUID

import pytest
from moto import mock_aws

from app.core.client_scope import ClientScope
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.modules.documentation import documents as svc
from tests.client_scope_support import client_with_site, everything_on
from tests.conftest import ApiEnv

API = "/api/v1/docs"
CAP = 32


@pytest.fixture()
def s3(api_env: ApiEnv) -> Any:
    mock = mock_aws()
    mock.start()
    api_env.app.state.object_store = ObjectStore(api_env.settings)
    yield api_env.app.state.object_store
    mock.stop()
    api_env.app.state.object_store = None


async def _document(env: ApiEnv, headers: dict[str, str], client: UUID | None) -> str:
    body: dict[str, Any] = {"title": "contract"}
    if client is not None:
        body["client_id"] = str(client)
    r = await env.client.post(f"{API}/documents", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _upload(env: ApiEnv, headers: dict[str, str], document: str, size: int) -> Any:
    return await env.client.post(
        f"{API}/documents/{document}/versions?filename=f.txt",
        content=b"x" * size,
        headers=headers,
    )


async def _world(env: ApiEnv) -> dict[str, Any]:
    """X with 16 bytes, Y with 24, the documents for every client with 8."""
    org = await env.create_org("doc-quota")
    x, _ = await client_with_site(env, org)
    y, _ = await client_with_site(env, org)
    admin = await env.headers(org, await env.create_user(org, "org-admin"))
    docs = {name: await _document(env, admin, client)
            for name, client in (("x", x), ("y", y), ("every", None))}
    for name, size in (("x", 16), ("y", 24), ("every", 8)):
        r = await _upload(env, admin, docs[name], size)
        assert r.status_code == 201, r.text
    return {"org": org, "x": x, "y": y, "admin": admin, "docs": docs,
            "x_tech": await everything_on(env, org, x)}


async def test_a_client_s_usage_is_its_own_whoever_asks(api_env: ApiEnv, s3: Any) -> None:
    w = await _world(api_env)
    for scope in (ClientScope.all(), ClientScope.only([w["x"]])):
        async with org_session(w["org"], clients=scope, engine=api_env.app_engine) as s:
            assert await svc.client_usage_bytes(s, w["org"], client_id=w["x"]) == 16, scope
            assert await svc.client_usage_bytes(s, w["org"], client_id=None) == 8, scope
    async with org_session(w["org"], clients=ClientScope.all(),
                           engine=api_env.app_engine) as s:
        assert await svc.client_usage_bytes(s, w["org"], client_id=w["y"]) == 24


async def test_the_507_names_the_owner_s_usage_the_same_for_every_caller(
    api_env: ApiEnv, s3: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.modules.documentation.documents.CLIENT_DOCUMENT_QUOTA_BYTES", CAP)
    w = await _world(api_env)
    assert (await _upload(api_env, w["admin"], w["docs"]["x"], 16)).status_code == 201
    expected = (
        f"this client's document storage quota is reached ({CAP} of {CAP} bytes used). "
        "Purge or archive documents to free space."
    )
    for headers in (w["admin"], w["x_tech"]):
        r = await _upload(api_env, headers, w["docs"]["x"], 1)
        assert (r.status_code, r.json()) == (507, {"detail": expected}), r.text
    r = await _upload(api_env, {**w["x_tech"], "Accept-Language": "es"}, w["docs"]["x"], 1)
    assert r.json() == {"detail": (
        f"se alcanzó la cuota de almacenamiento de documentos de este cliente ({CAP} de "
        f"{CAP} bytes usados). Purga o archiva documentos para liberar espacio."
    )}


async def test_x_at_the_cap_does_not_block_y_or_the_documents_for_every_client(
    api_env: ApiEnv, s3: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.modules.documentation.documents.CLIENT_DOCUMENT_QUOTA_BYTES", CAP)
    w = await _world(api_env)
    assert (await _upload(api_env, w["admin"], w["docs"]["x"], 16)).status_code == 201
    # Y: 24 + 8 lands exactly on the cap; the documents for every client
    # likewise, 8 + 24.
    assert (await _upload(api_env, w["admin"], w["docs"]["y"], 8)).status_code == 201
    assert (await _upload(api_env, w["admin"], w["docs"]["every"], 24)).status_code == 201
    full = await _upload(api_env, w["admin"], w["docs"]["every"], 1)
    assert (full.status_code, full.json()) == (507, {"detail": (
        f"the documents for every client have reached their storage quota ({CAP} of {CAP} "
        "bytes used). Purge or archive documents to free space."
    )}), full.text
