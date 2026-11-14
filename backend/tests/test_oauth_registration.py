"""The OAuth server before there is a person: its metadata (RFC 8414), open
registration (RFC 7591) and client metadata documents (autenticación propia
design §5.1, §5.4, §5.5; plan 2026-11-06, Task 6).

- CIMD is offered whenever there is an issuer, and read whenever asked
  (operator decision C2); dynamic registration only with
  `ITS_MCP_REGISTRATION=open` and MCP on (B3-02);
- registration is limited: 20 an hour per network, an IPv6 network being its
  /64, and 200 live registrations (B3-02);
- a stored document survives a transient failure (CIMD-03 §6.1), never a
  refusal, and none is read on the platform's own origin (§10.1).

And what the coordinator's integration notes for Task 6 add:

- THE PURGE'S SHAPE (`guards.lock_user_row`'s lock map): one idle app per
  transaction, the idle condition inside its DELETE. A use that commits
  first spares the app, with no 40P01; and a revocation across a person's
  apps cannot close a cycle with a purge of two of them;
- the ceiling holds when two registrations race for its last place;
- `record_use=False` writes nothing and reads no document; a document's
  refresh of its row comes after `clients.hold`, and an idle row is replaced
  (with its grants gone) rather than revived.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.guards import lock_user_row
from app.core.auth.oauth import cimd, clients
from app.core.auth.oauth.clients import Purged
from app.core.auth.oauth.errors import REGISTRATIONS_FULL, OAuthError
from app.core.auth.oauth.policy import CLIENT_IDLE
from app.core.auth.oauth.store import deployment_org, oauth_session
from app.models import Organization
from app.models.oauth import OAuthClient, OAuthCode, OAuthRefreshFamily, OAuthRefreshToken
from tests.oauth_support import (
    BASE,
    CLAUDE_CALLBACK,
    LOOPBACK_CALLBACK,
    RESOURCE,
    forget_registration_limit,
    oauth_http,
    oauth_org,
    register,
    seed_client,
)
from tests.race_helpers import NO_WAITING, backend_pid, normalised, until_blocked_by
from tests.session_support import user_id_of
from tests.test_oauth_schema import _grant

DOCUMENT_URL = "https://claude.example/oauth/client-metadata.json"


@pytest.fixture()
async def env(api_env):
    await oauth_org(api_env)
    api_env.settings.mcp_registration = "open"
    await forget_registration_limit(api_env)
    return api_env


async def _metadata(env) -> Any:
    async with oauth_http(env) as client:
        return await client.get("/.well-known/oauth-authorization-server")


async def _org(env) -> Any:
    return await deployment_org(env.app)


async def _switch(env, org_id, on: bool) -> None:
    async with AsyncSession(env.admin_engine) as session:
        await session.execute(
            update(Organization)
            .where(Organization.id == org_id)
            .values(settings={"mcp_enabled": on})
        )
        await session.commit()


def _from(env, address: str) -> httpx.AsyncClient:
    """A client at `address`: with no trusted proxy, the peer is the address."""
    transport = httpx.ASGITransport(app=env.app, client=(address, 51000))
    return httpx.AsyncClient(transport=transport, base_url=BASE)


# --------------------------------------------------------------------------- #
# RFC 8414
# --------------------------------------------------------------------------- #


async def test_the_metadata_names_the_platform_as_its_own_issuer(env) -> None:
    resp = await _metadata(env)
    assert resp.status_code == 200
    assert resp.json() == {
        "issuer": BASE,
        "authorization_endpoint": f"{BASE}/oauth/authorize",
        "token_endpoint": f"{BASE}/oauth/token",
        "revocation_endpoint": f"{BASE}/oauth/revoke",
        "registration_endpoint": f"{BASE}/oauth/register",
        "jwks_uri": f"{BASE}/.well-known/jwks.json",
        "scopes_supported": ["mcp", "offline_access"],
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "authorization_response_iss_parameter_supported": True,
        "client_id_metadata_document_supported": True,
    }


async def test_closed_registration_still_offers_client_metadata_documents(env) -> None:
    """C2: CIMD needs no opt-in; dynamic registration does."""
    env.settings.mcp_registration = ""
    body = (await _metadata(env)).json()
    assert "registration_endpoint" not in body
    assert body["client_id_metadata_document_supported"] is True
    assert body["token_endpoint_auth_methods_supported"] == ["none"]


async def test_with_mcp_off_nobody_may_register(env) -> None:
    """B3-02: "MCP sigue apagado hasta que un org-admin lo enciende"."""
    await _switch(env, await _org(env), False)
    body = (await _metadata(env)).json()
    async with oauth_http(env) as client:
        resp = await client.post("/oauth/register", json={"redirect_uris": [CLAUDE_CALLBACK]})
    assert "registration_endpoint" not in body
    assert (resp.status_code, resp.json()["error"]) == (403, "access_denied")


@pytest.mark.parametrize(
    "url", [None, "http://itms.example.com", "https://itms.example.com/console"]
)
async def test_no_https_console_is_no_issuer(env, url: str | None) -> None:
    env.settings.public_console_url = url
    async with oauth_http(env) as client:
        meta = await client.get("/.well-known/oauth-authorization-server")
        reg = await client.post("/oauth/register", json={"redirect_uris": [CLAUDE_CALLBACK]})
    for resp in (meta, reg):
        assert resp.status_code == 503
        assert resp.json()["error"] == "temporarily_unavailable"


async def test_the_protected_resource_names_the_platform(env) -> None:
    async with oauth_http(env) as client:
        resp = await client.get("/.well-known/oauth-protected-resource/mcp")
    assert resp.json()["resource"] == RESOURCE
    assert resp.json()["authorization_servers"] == [BASE]


# --------------------------------------------------------------------------- #
# RFC 7591
# --------------------------------------------------------------------------- #


async def test_a_registration_is_a_public_client_of_this_organization(env) -> None:
    answer = await register(
        env, token_endpoint_auth_method="client_secret_basic", scope="openid mcp"
    )
    assert answer["client_id"].startswith("itms_")
    assert len(answer["client_id"]) == len("itms_") + 32
    # RFC 7591 §3.2.1: "MAY reject or replace" -- replaced, and said (plan D4, B3-08).
    assert answer["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in answer
    assert answer["redirect_uris"] == [CLAUDE_CALLBACK]
    assert answer["grant_types"] == ["authorization_code", "refresh_token"]
    assert answer["response_types"] == ["code"]
    assert answer["scope"] == "mcp"
    assert answer["client_name"] == "Claude"
    assert isinstance(answer["client_id_issued_at"], int)
    async with oauth_session(await _org(env), env.app_engine) as session:
        row = (
            await session.execute(
                select(OAuthClient).where(OAuthClient.client_id == answer["client_id"])
            )
        ).scalar_one()
    assert (row.kind, row.metadata_expires_at) == ("registered", None)


async def test_the_answer_is_201_json_and_never_cached(env) -> None:
    async with oauth_http(env) as client:
        resp = await client.post("/oauth/register", json={"redirect_uris": [LOOPBACK_CALLBACK]})
    assert resp.status_code == 201
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["pragma"] == "no-cache"
    assert "client_name" not in resp.json()


@pytest.mark.parametrize(
    ("metadata", "error"),
    [
        ({"redirect_uris": []}, "invalid_redirect_uri"),
        ({"redirect_uris": "https://claude.ai/cb"}, "invalid_redirect_uri"),
        ({"redirect_uris": [f"https://claude.ai/{n}" for n in range(11)]}, "invalid_redirect_uri"),
        ({"redirect_uris": ["http://evil.example/cb"]}, "invalid_redirect_uri"),
        ({"redirect_uris": ["https://claude.ai/cb#frag"]}, "invalid_redirect_uri"),
        ({"redirect_uris": ["https://сlaude.ai/cb"]}, "invalid_redirect_uri"),
        ({"redirect_uris": ["https://claude.ai/cb?code=x"]}, "invalid_redirect_uri"),
        # No browser connects to port 0 (Task 8 review, fix round 2).
        ({"redirect_uris": ["http://127.0.0.1:0/callback"]}, "invalid_redirect_uri"),
        (
            {"redirect_uris": [CLAUDE_CALLBACK], "grant_types": ["client_credentials"]},
            "invalid_client_metadata",
        ),
        (
            {"redirect_uris": [CLAUDE_CALLBACK], "grant_types": ["refresh_token"]},
            "invalid_client_metadata",
        ),
        (
            {"redirect_uris": [CLAUDE_CALLBACK], "grant_types": ["authorization_code", "password"]},
            "invalid_client_metadata",
        ),
        (
            {"redirect_uris": [CLAUDE_CALLBACK], "response_types": ["token"]},
            "invalid_client_metadata",
        ),
        ({"redirect_uris": [CLAUDE_CALLBACK], "client_name": 7}, "invalid_client_metadata"),
        # Fix round 1: a list member that is not text is refused, never a
        # 500 (an unhashable one used to reach `set()` unchecked).
        (
            {"redirect_uris": [CLAUDE_CALLBACK], "grant_types": ["authorization_code", {}]},
            "invalid_client_metadata",
        ),
        (
            {"redirect_uris": [CLAUDE_CALLBACK], "grant_types": ["authorization_code", 7]},
            "invalid_client_metadata",
        ),
        ({"redirect_uris": [CLAUDE_CALLBACK], "response_types": [[]]}, "invalid_client_metadata"),
        ({"redirect_uris": [CLAUDE_CALLBACK], "response_types": [{}]}, "invalid_client_metadata"),
        (
            {"redirect_uris": [CLAUDE_CALLBACK], "response_types": ["code", None]},
            "invalid_client_metadata",
        ),
        # RFC 7591 §3.2.2: a redirect URI that is not one is
        # `invalid_redirect_uri`, whatever its type.
        ({"redirect_uris": [{}]}, "invalid_redirect_uri"),
        ({"redirect_uris": [[CLAUDE_CALLBACK]]}, "invalid_redirect_uri"),
        ({"redirect_uris": [CLAUDE_CALLBACK, 7]}, "invalid_redirect_uri"),
    ],
)
async def test_a_registration_this_server_cannot_honour_is_refused(
    env, metadata: dict[str, Any], error: str
) -> None:
    async with oauth_http(env) as client:
        resp = await client.post("/oauth/register", json=metadata)
    assert resp.status_code == 400
    assert resp.json()["error"] == error
    async with oauth_session(await _org(env), env.app_engine) as session:
        assert (await session.execute(select(OAuthClient))).scalars().all() == []


async def test_a_body_that_is_not_one_json_object_is_refused(env) -> None:
    async with oauth_http(env) as client:
        listed = await client.post("/oauth/register", json=[{"redirect_uris": [CLAUDE_CALLBACK]}])
        broken = await client.post(
            "/oauth/register", content=b"{", headers={"Content-Type": "application/json"}
        )
        form = await client.post("/oauth/register", data={"redirect_uris": CLAUDE_CALLBACK})
    for resp in (listed, broken, form):
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_client_metadata"


async def test_a_body_nested_past_the_parsers_depth_is_refused(env) -> None:
    """Fix round 1: 16 KiB of `[` is under the size limit, and `json.loads`
    answers it with RecursionError, which is not a ValueError -- it was a
    500 on the public, anonymous endpoint."""
    async with oauth_http(env) as client:
        resp = await client.post(
            "/oauth/register", content=b"[" * 16000, headers={"Content-Type": "application/json"}
        )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_client_metadata"


@pytest.mark.parametrize("scope", [[{}], 7, {"mcp": True}, None, "openid profile"])
async def test_a_requested_scope_of_any_shape_is_replaced(env, scope: object) -> None:
    """Plan D7: the scope granted is `mcp` whatever was asked, so the asked
    value is never read -- nor able to crash anything (fix round 1)."""
    answer = await register(env, scope=scope)
    assert answer["scope"] == "mcp"


async def test_a_body_over_16_kib_is_an_rfc_error(env) -> None:
    """Not the API's translated 413 body: RFC 6749 §5.2's, in English."""
    async with oauth_http(env) as client:
        resp = await client.post(
            "/oauth/register",
            content=b'{"client_name": "' + b"x" * 17000 + b'"}',
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 413
    assert resp.json() == {
        "error": "invalid_request",
        "error_description": "the request body is too large",
    }


async def test_unknown_metadata_is_ignored_and_a_name_is_cleaned(env) -> None:
    answer = await register(
        env, client_name="  Claude\u0000\n Code ", logo_uri="https://x.example/l.png"
    )
    assert answer["client_name"] == "Claude Code"
    assert "logo_uri" not in answer


async def test_registration_is_closed_without_the_opt_in(env) -> None:
    env.settings.mcp_registration = ""
    async with oauth_http(env) as client:
        resp = await client.post("/oauth/register", json={"redirect_uris": [CLAUDE_CALLBACK]})
    assert resp.status_code == 403
    assert resp.json()["error"] == "access_denied"


async def test_registration_needs_the_deployments_organization(env) -> None:
    env.settings.default_org_slug = None
    async with oauth_http(env) as client:
        resp = await client.post("/oauth/register", json={"redirect_uris": [CLAUDE_CALLBACK]})
    assert resp.status_code == 503


async def test_one_network_may_register_only_so_often(env, monkeypatch) -> None:
    monkeypatch.setattr(clients, "REGISTRATIONS_PER_HOUR", 2)
    async with oauth_http(env) as client:
        answers = [
            await client.post("/oauth/register", json={"redirect_uris": [CLAUDE_CALLBACK]})
            for _ in range(3)
        ]
    assert [a.status_code for a in answers] == [201, 201, 429]
    assert answers[2].json()["error"] == "temporarily_unavailable"
    assert answers[2].headers["retry-after"] == "3600"


async def test_the_limit_is_counted_before_the_database_is_read(env, monkeypatch) -> None:
    """Re-review f3-2, M-2: a network over its limit costs Redis, not a lookup."""
    from app.api import oauth_registration

    async def never(app: Any) -> Any:
        raise AssertionError("the deployment's organization was read over the limit")

    monkeypatch.setattr(clients, "REGISTRATIONS_PER_HOUR", 1)
    async with oauth_http(env) as client:
        first = await client.post("/oauth/register", json={"redirect_uris": [CLAUDE_CALLBACK]})
        monkeypatch.setattr(oauth_registration, "deployment_org", never)
        second = await client.post("/oauth/register", json={"redirect_uris": [CLAUDE_CALLBACK]})
    assert first.status_code == 201, first.text
    assert second.status_code == 429


async def test_the_metadata_reads_the_database_once_per_window(env, monkeypatch) -> None:
    """Fix round 1: the metadata is public and unlimited, so one process
    reads the deployment's organization and its MCP switch at most once per
    `OFFERED_FOR_S`. Turning MCP off shows when the window ends; turning the
    opt-in off shows at once (a setting, read every time)."""
    reads = 0
    real = clients.mcp_switched_on

    async def counted(session: AsyncSession, org_id: Any) -> bool:
        nonlocal reads
        reads += 1
        return await real(session, org_id)

    clock = [1000.0]
    monkeypatch.setattr(clients, "mcp_switched_on", counted)
    monkeypatch.setattr(clients, "_monotonic", lambda: clock[0])
    offered = [("registration_endpoint" in (await _metadata(env)).json()) for _ in range(3)]
    assert (offered, reads) == ([True, True, True], 1)

    await _switch(env, await _org(env), False)
    clock[0] += clients.OFFERED_FOR_S - 1
    assert "registration_endpoint" in (await _metadata(env)).json()
    clock[0] += 1
    assert "registration_endpoint" not in (await _metadata(env)).json()
    assert reads == 2

    env.settings.mcp_registration = ""
    await _switch(env, await _org(env), True)
    assert "registration_endpoint" not in (await _metadata(env)).json()
    assert reads == 2


async def test_a_failed_read_is_not_kept(env, monkeypatch) -> None:
    from sqlalchemy.exc import OperationalError

    calls = 0
    real = clients.mcp_switched_on

    async def once_down(session: AsyncSession, org_id: Any) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OperationalError("SELECT settings", {}, OSError("down"))
        return await real(session, org_id)

    monkeypatch.setattr(clients, "mcp_switched_on", once_down)
    assert "registration_endpoint" not in (await _metadata(env)).json()
    assert "registration_endpoint" in (await _metadata(env)).json()
    assert calls == 2


async def test_a_database_that_does_not_answer_leaves_registration_out_of_the_metadata(
    env, monkeypatch
) -> None:
    from sqlalchemy.exc import OperationalError

    async def down(app: Any) -> bool:
        raise OperationalError("SELECT its_auth_org(:slug)", {}, OSError("down"))

    monkeypatch.setattr(clients, "registration_offered", down)
    resp = await _metadata(env)
    assert resp.status_code == 200
    assert "registration_endpoint" not in resp.json()
    assert resp.json()["client_id_metadata_document_supported"] is True


async def test_an_ipv6_network_is_one_bucket_by_its_64(env, monkeypatch) -> None:
    """B3-02: a host owns its /64; counted by address it would never run out."""
    monkeypatch.setattr(clients, "REGISTRATIONS_PER_HOUR", 2)
    body = {"redirect_uris": [CLAUDE_CALLBACK]}
    codes = []
    for address in ("2001:db8:1:2::1", "2001:db8:1:2::2", "2001:db8:1:2:ffff::3"):
        async with _from(env, address) as client:
            codes.append((await client.post("/oauth/register", json=body)).status_code)
    async with _from(env, "2001:db8:1:3::1") as client:
        elsewhere = (await client.post("/oauth/register", json=body)).status_code
    assert codes == [201, 201, 429]
    assert elsewhere == 201


async def test_live_registrations_have_a_ceiling(env, monkeypatch) -> None:
    """B3-02: the realm's 200; an idle registration does not count, and a
    metadata document never does."""
    monkeypatch.setattr(clients, "MAX_REGISTRATIONS", 2)
    org_id, now = await _org(env), datetime.now(UTC)
    await seed_client(env, org_id)
    await seed_client(env, org_id, last_used_at=now - timedelta(days=31))
    await seed_client(env, org_id, client_id=DOCUMENT_URL, kind="metadata_document")
    async with oauth_http(env) as client:
        second = await client.post("/oauth/register", json={"redirect_uris": [CLAUDE_CALLBACK]})
        third = await client.post("/oauth/register", json={"redirect_uris": [CLAUDE_CALLBACK]})
    assert second.status_code == 201, second.text
    assert (third.status_code, third.json()["error"]) == (503, "temporarily_unavailable")
    async with oauth_session(org_id, env.app_engine) as session:
        live = (
            await session.execute(
                select(func.count())
                .select_from(OAuthClient)
                .where(OAuthClient.kind == "registered")
            )
        ).scalar_one()
    assert live == 2  # the idle one went with the purge; the third was never written


async def test_without_redis_registration_fails_closed(env, monkeypatch) -> None:
    from redis.exceptions import ConnectionError as RedisDown

    class Down:
        async def incr(self, _key: str) -> int:
            raise RedisDown("down")

    monkeypatch.setattr(env.app.state, "redis", Down())
    async with oauth_http(env) as client:
        resp = await client.post("/oauth/register", json={"redirect_uris": [CLAUDE_CALLBACK]})
    assert resp.status_code == 429


async def test_a_registration_removes_idle_ones_and_their_grants_with_them(env) -> None:
    org_id = await _org(env)
    sub = await env.create_user(org_id, "org-admin")
    idle = await seed_client(env, org_id, last_used_at=datetime.now(UTC) - timedelta(days=31))
    alive = await seed_client(env, org_id)
    await _grant(env, org_id, await user_id_of(env, sub), idle)
    await register(env)
    async with oauth_session(org_id, env.app_engine) as session:
        ids = set((await session.execute(select(OAuthClient.id))).scalars())
        left = [
            len((await session.execute(select(model))).scalars().all())
            for model in (OAuthRefreshFamily, OAuthRefreshToken, OAuthCode)
        ]
    assert idle.id not in ids
    assert alive.id in ids
    assert len(ids) == 2
    # ON DELETE CASCADE: the idle app's family, its refresh token and its code.
    assert left == [0, 0, 0]


# --------------------------------------------------------------------------- #
# Client metadata documents
# --------------------------------------------------------------------------- #


def _document(cache_for: timedelta = timedelta(hours=1)) -> cimd.ClientDocument:
    return cimd.ClientDocument(
        client_id=DOCUMENT_URL,
        client_name="Claude Code",
        redirect_uris=("http://localhost/callback",),
        cache_for=cache_for,
    )


class FakeFetch:
    def __init__(self, document: cimd.ClientDocument | None = None, refused: str | None = None):
        self.document = document or _document()
        self.refused = refused
        self.calls = 0

    async def __call__(self, client_id: str) -> cimd.ClientDocument:
        self.calls += 1
        if self.refused is not None:
            raise cimd.DocumentRefused(self.refused)  # type: ignore[arg-type]
        assert client_id == DOCUMENT_URL
        return self.document


async def _resolve(env, fetch: FakeFetch, now: datetime, client_id: str = DOCUMENT_URL) -> Any:
    return await clients.resolve_client(
        env.app, org_id=await _org(env), client_id=client_id, now=now, fetch=fetch
    )


async def test_a_document_client_is_read_once_and_cached(env) -> None:
    now = datetime.now(UTC)
    fetch = FakeFetch()
    first = await _resolve(env, fetch, now)
    again = await _resolve(env, fetch, now + timedelta(minutes=59))
    assert fetch.calls == 1
    assert first is not None and again is not None
    assert first.id == again.id
    assert (first.kind, first.client_name, first.redirect_uris) == (
        "metadata_document",
        "Claude Code",
        ("http://localhost/callback",),
    )
    await _resolve(env, fetch, now + timedelta(minutes=61))
    assert fetch.calls == 2


async def test_a_document_is_read_with_registration_closed(env) -> None:
    """C2: a client metadata document needs no opt-in."""
    env.settings.mcp_registration = ""
    fetch = FakeFetch()
    found = await _resolve(env, fetch, datetime.now(UTC))
    assert found is not None and found.kind == "metadata_document"
    assert fetch.calls == 1


async def test_a_refused_document_is_never_kept(env) -> None:
    with pytest.raises(cimd.DocumentRefused):
        await _resolve(env, FakeFetch(refused="secret"), datetime.now(UTC))
    async with oauth_session(await _org(env), env.app_engine) as session:
        assert (await session.execute(select(OAuthClient))).scalars().all() == []


async def test_a_stored_document_survives_a_passing_failure_and_a_refusal_is_raised(
    env,
) -> None:
    """CIMD-03 §6.1: "SHOULD NOT discard previously cached valid client metadata
    on transient failures". A refusal is raised, never stored, and does not
    delete the old copy (Task 6, decision 4)."""
    now = datetime.now(UTC)
    stored = await _resolve(env, FakeFetch(), now)
    later = now + timedelta(hours=2)
    kept = await _resolve(env, FakeFetch(refused="unavailable"), later)
    assert kept is not None and kept.id == stored.id
    with pytest.raises(cimd.DocumentRefused):
        await _resolve(env, FakeFetch(refused="mismatch"), later)
    again = await _resolve(env, FakeFetch(refused="unavailable"), later)
    assert again is not None and again.id == stored.id


async def test_a_document_on_the_platforms_own_origin_is_never_read(env) -> None:
    fetch = FakeFetch()
    with pytest.raises(cimd.DocumentRefused) as refused:
        await _resolve(env, fetch, datetime.now(UTC), client_id=f"{BASE}/mcp")
    assert refused.value.reason == "own_origin"
    assert fetch.calls == 0


async def test_a_registered_client_is_found_and_an_unknown_one_is_not(env) -> None:
    org_id, now = await _org(env), datetime.now(UTC)
    app = await seed_client(env, org_id)
    fetch = FakeFetch()
    found = await _resolve(env, fetch, now, client_id=app.client_id)
    unknown = await _resolve(env, fetch, now, client_id="itms_nobody")
    assert found is not None and found.id == app.id
    assert unknown is None
    assert fetch.calls == 0


# --------------------------------------------------------------------------- #
# The purge's shape and its races (coordinator's integration notes, Task 6)
# --------------------------------------------------------------------------- #


async def _families(env, org_id) -> list[Any]:
    async with oauth_session(org_id, env.app_engine) as session:
        return list((await session.execute(select(OAuthRefreshFamily.id))).scalars().all())


async def _deadlock_ms(env) -> int:
    async with env.app_engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text("SELECT setting FROM pg_settings WHERE name = 'deadlock_timeout'")
                )
            ).scalar_one()
        )


@pytest.mark.parametrize(
    ("deadlock_ms", "wait_ms"),
    [(1000, 500), (300, 150), (5000, 500), (3, 1), (2, 1), (1, 1)],
)
def test_a_purge_waits_half_the_deadlock_timeout(deadlock_ms: int, wait_ms: int) -> None:
    """Fix round 1: the give-way holds only while the purge's wait is under
    the server's `deadlock_timeout`, so it is derived from it, not assumed:
    half, never 0 (which would be no limit at all), never over
    `PURGE_LOCK_WAIT_MS`."""
    assert clients.purge_lock_wait_ms(deadlock_ms) == wait_ms


async def test_the_purges_wait_is_set_from_the_servers_deadlock_timeout(env) -> None:
    org_id = await _org(env)
    expected = clients.purge_lock_wait_ms(await _deadlock_ms(env))
    async with oauth_session(org_id, env.app_engine) as session:
        assert await clients.bound_lock_wait(session) == expected
        shown = (await session.execute(text("SHOW lock_timeout"))).scalar_one()
    assert shown == f"{expected}ms"
    async with oauth_session(org_id, env.app_engine) as session:
        # SET LOCAL: the next transaction on the connection waits as before.
        assert (await session.execute(text("SHOW lock_timeout"))).scalar_one() == "0"


async def test_a_purge_stops_at_its_budget(env, monkeypatch) -> None:
    """Fix round 1: under contention each app may cost the purge its wait, so
    `PURGE_BATCH` apps could hold one registration for ~50 s. The purge
    starts no app once `PURGE_BUDGET_S` has passed: here three idle apps are
    all held, each costs the wait (500 ms), and a 0.7 s budget lets two be
    tried and leaves the third."""
    monkeypatch.setattr(clients, "purge_lock_wait_ms", lambda _deadlock_ms: 500)
    monkeypatch.setattr(clients, "PURGE_BUDGET_S", 0.7)
    org_id, now = await _org(env), datetime.now(UTC)
    apps = [
        await seed_client(env, org_id, last_used_at=now - timedelta(days=d)) for d in (33, 32, 31)
    ]
    async with oauth_session(org_id, env.app_engine) as session:
        for app in apps:
            # FOR KEY SHARE, re-checked against a clock 30 days back: held.
            assert await clients.hold(
                session, org_id=org_id, app_id=app.id, now=now - timedelta(days=30)
            )
        purged = await clients.purge_idle(env.app_engine, org_id=org_id, now=now)
    assert purged == Purged(removed=0, gave_way=2, not_tried=1)


async def test_a_registration_goes_on_when_the_purge_is_out_of_time(env, monkeypatch) -> None:
    monkeypatch.setattr(clients, "PURGE_BUDGET_S", 0.0)
    org_id, now = await _org(env), datetime.now(UTC)
    idle = await seed_client(env, org_id, last_used_at=now - timedelta(days=31))
    answer = await register(env)
    async with oauth_session(org_id, env.app_engine) as session:
        ids = set((await session.execute(select(OAuthClient.id))).scalars())
    assert idle.id in ids
    assert len(ids) == 2 and answer["client_id"].startswith("itms_")


async def test_the_purge_takes_only_what_is_idle_by_its_own_now(env) -> None:
    """`<=`, as `find_client` reads idle: thirty days to the second goes, a
    document's row as well as a registration's."""
    org_id, now = await _org(env), datetime.now(UTC)
    gone = await seed_client(env, org_id, last_used_at=now - CLIENT_IDLE)
    kept = await seed_client(env, org_id, last_used_at=now - CLIENT_IDLE + timedelta(seconds=1))
    document = await seed_client(
        env,
        org_id,
        client_id=DOCUMENT_URL,
        kind="metadata_document",
        last_used_at=now - CLIENT_IDLE,
    )
    assert await clients.purge_idle(env.app_engine, org_id=org_id, now=now) == Purged(2, 0, 0)
    async with oauth_session(org_id, env.app_engine) as session:
        ids = set((await session.execute(select(OAuthClient.id))).scalars())
    assert ids == {kept.id}
    assert gone.id not in ids and document.id not in ids


async def test_a_purge_removes_at_most_its_batch(env, monkeypatch) -> None:
    monkeypatch.setattr(clients, "PURGE_BATCH", 2)
    org_id, now = await _org(env), datetime.now(UTC)
    for days in (31, 32, 33):
        await seed_client(env, org_id, last_used_at=now - timedelta(days=days))
    assert await clients.purge_idle(env.app_engine, org_id=org_id, now=now) == Purged(2, 0, 0)
    assert await clients.purge_idle(env.app_engine, org_id=org_id, now=now) == Purged(1, 0, 0)


async def test_a_use_that_commits_first_spares_its_app_from_the_purge(env, monkeypatch) -> None:
    """The coordinator's race, with the purge as built: a token request of app
    X (`clients.hold`, its grant rows, `clients.touch`) and `purge_idle` at
    the 30-day boundary -- live by the request's clock, idle by the purge's.
    The purge's DELETE waits on the hold; the request writes its family and
    its use WITHOUT waiting on the queued DELETE (NO_WAITING would turn a
    wait into an error) and commits; the DELETE's own idle condition then
    fails on the row as it now is. No 40P01: the app and its grant survive.
    The purge's wait is lengthened here (`purge_lock_wait_ms`), and its
    result says the app was spared by that re-check: not removed, and the
    purge never gave way."""
    monkeypatch.setattr(clients, "purge_lock_wait_ms", lambda _deadlock_ms: 30_000)
    org_id = await _org(env)
    user_id = await user_id_of(env, await env.create_user(org_id, "org-admin"))
    then = datetime.now(UTC) - CLIENT_IDLE
    app = await seed_client(env, org_id, last_used_at=then)
    family_id = await _grant(env, org_id, user_id, app)
    request_now = then + CLIENT_IDLE - timedelta(seconds=1)
    purge_now = then + CLIENT_IDLE

    async with oauth_session(org_id, env.app_engine) as session:
        await lock_user_row(session, user_id)
        held = await clients.hold(session, org_id=org_id, app_id=app.id, now=request_now)
        assert held is not None
        purging = asyncio.create_task(
            clients.purge_idle(env.app_engine, org_id=org_id, now=purge_now)
        )
        blocked = await until_blocked_by(env.app_engine, await backend_pid(session))
        assert [normalised(q).split(" ")[0] for q in blocked] == ["DELETE"]
        await session.execute(text(NO_WAITING))
        await session.execute(
            update(OAuthRefreshFamily)
            .where(OAuthRefreshFamily.id == family_id)
            .values(last_used_at=request_now)
        )
        clients.touch(held, request_now)
        await session.flush()
    assert await purging == Purged(removed=0, gave_way=0, not_tried=0)
    async with oauth_session(org_id, env.app_engine) as session:
        stored = (
            await session.execute(select(OAuthClient.last_used_at).where(OAuthClient.id == app.id))
        ).scalar_one()
    assert stored == request_now
    assert await _families(env, org_id) == [family_id]


async def test_a_purge_that_commits_first_leaves_the_request_nothing_to_hold(env) -> None:
    """The other order: the purge commits before the request's hold, which
    then finds no row -- None, the request's refusal, not a stale-row 500
    and not a deadlock."""
    org_id, now = await _org(env), datetime.now(UTC)
    app = await seed_client(env, org_id, last_used_at=now - CLIENT_IDLE)
    async with oauth_session(org_id, env.app_engine) as session:
        assert await clients.purge_idle(env.app_engine, org_id=org_id, now=now) == Purged(1, 0, 0)
        held = await clients.hold(
            session, org_id=org_id, app_id=app.id, now=now - timedelta(seconds=1)
        )
        assert held is None


async def test_a_purge_of_two_apps_meets_a_revocation_across_them_without_a_deadlock(
    env, monkeypatch
) -> None:
    """WHY ONE APP PER TRANSACTION. A revocation of a person's grants across
    their apps (`grants.revoke_all_for_user`, Task 7: one UPDATE of their
    families, then the chain) locks families in no app's order -- here the
    second app's first, as one UPDATE's scan may. A purge that kept the first
    app's cascade while it waited in the second's would make that UPDATE wait
    on it in turn when it reaches the first app's family: a cycle, 40P01.
    Each app in a transaction of its own, the purge holds nothing while it
    waits: the first app is already gone, the revocation finds nothing there
    to wait for, commits, and the purge then takes the second. The purge's
    wait is lengthened here (`purge_lock_wait_ms`), and its result says it
    never gave way: that is the shape at work."""
    monkeypatch.setattr(clients, "purge_lock_wait_ms", lambda _deadlock_ms: 30_000)
    org_id = await _org(env)
    user_id = await user_id_of(env, await env.create_user(org_id, "org-admin"))
    now = datetime.now(UTC)
    first = await seed_client(env, org_id, last_used_at=now - timedelta(days=32))
    second = await seed_client(env, org_id, last_used_at=now - timedelta(days=31))
    first_family = await _grant(env, org_id, user_id, first)
    second_family = await _grant(env, org_id, user_id, second)
    revoke = update(OAuthRefreshFamily).values(revoked_at=now, revoked_reason="disabled")

    async with oauth_session(org_id, env.app_engine) as session:
        await lock_user_row(session, user_id)
        await session.execute(revoke.where(OAuthRefreshFamily.id == second_family))
        purging = asyncio.create_task(clients.purge_idle(env.app_engine, org_id=org_id, now=now))
        blocked = await until_blocked_by(env.app_engine, await backend_pid(session))
        assert [normalised(q).split(" ")[0] for q in blocked] == ["DELETE"]
        await session.execute(text(NO_WAITING))
        reached = await session.execute(revoke.where(OAuthRefreshFamily.id == first_family))
        assert reached.rowcount == 0  # the first app went in a transaction of its own
    assert await purging == Purged(removed=2, gave_way=0, not_tried=0)
    assert await _families(env, org_id) == []


async def test_a_purge_yields_to_a_revocation_of_two_grants_on_its_app(env) -> None:
    """The cycle one app per transaction does not close by itself: one
    person's two families on the one idle app, the revocation's UPDATE
    reaching the second first. The purge's cascade takes the first and waits
    on the second; the revocation then reaches the first and waits on the
    purge. So a purge waits at most half of Postgres' `deadlock_timeout`
    (`purge_lock_wait_ms`): its wait began first, so it gives up before
    either side's deadlock check runs, and leaves the app to the next purge.
    The revocation -- a person's act -- completes; nobody gets 40P01. (Were
    the cascade to scan the second family first, there would be no cycle and
    the first purge would take the app.) Either way the purge's result says
    which path ran: a first try that removed nothing gave way (fix round 1),
    and took at least its wait to do so."""
    org_id = await _org(env)
    user_id = await user_id_of(env, await env.create_user(org_id, "org-admin"))
    now = datetime.now(UTC)
    app = await seed_client(env, org_id, last_used_at=now - timedelta(days=31))
    first = await _grant(env, org_id, user_id, app)
    second = await _grant(env, org_id, user_id, app)
    revoke = update(OAuthRefreshFamily).values(revoked_at=now, revoked_reason="disabled")

    wait_s = clients.purge_lock_wait_ms(await _deadlock_ms(env)) / 1000
    loop = asyncio.get_running_loop()

    async with oauth_session(org_id, env.app_engine) as session:
        await lock_user_row(session, user_id)
        await session.execute(revoke.where(OAuthRefreshFamily.id == second))
        started = loop.time()
        purging = asyncio.create_task(clients.purge_idle(env.app_engine, org_id=org_id, now=now))
        blocked = await until_blocked_by(env.app_engine, await backend_pid(session))
        assert [normalised(q).split(" ")[0] for q in blocked] == ["DELETE"]
        reached = await session.execute(revoke.where(OAuthRefreshFamily.id == first))
        assert reached.rowcount in (0, 1)
    first_try = await purging
    took = loop.time() - started
    later = await clients.purge_idle(env.app_engine, org_id=org_id, now=now)
    if first_try.removed == 0:
        # The give-way path ran, and waited its wait out before giving way.
        assert (first_try, later) == (Purged(0, 1, 0), Purged(1, 0, 0))
        assert took >= wait_s
    else:
        assert (first_try, later) == (Purged(1, 0, 0), Purged(0, 0, 0))
    assert await _families(env, org_id) == []


async def test_two_registrations_racing_for_the_last_place_take_one(env, monkeypatch) -> None:
    """The ceiling limits under concurrency too: a registration counts and
    writes holding the organization's registration lock, so a second one
    counts after the first has written."""
    monkeypatch.setattr(clients, "MAX_REGISTRATIONS", 1)
    org_id, now = await _org(env), datetime.now(UTC)
    wanted = clients.Registration(client_name=None, redirect_uris=(CLAUDE_CALLBACK,))
    async with oauth_session(org_id, env.app_engine) as session:
        await clients.lock_registrations(session, org_id)
        racing = asyncio.create_task(
            clients.register_new(env.app, org_id=org_id, registration=wanted, now=now)
        )
        blocked = await until_blocked_by(env.app_engine, await backend_pid(session))
        assert ["PG_ADVISORY_XACT_LOCK" in normalised(q) for q in blocked] == [True]
        await clients.register(session, org_id=org_id, registration=wanted, now=now)
    with pytest.raises(OAuthError) as full:
        await racing
    assert full.value.problem == REGISTRATIONS_FULL
    async with oauth_session(org_id, env.app_engine) as session:
        assert len((await session.execute(select(OAuthClient))).scalars().all()) == 1


# --------------------------------------------------------------------------- #
# A document's row: written after a hold, never revived idle, never by a
# person who may not connect (`record_use=False`)
# --------------------------------------------------------------------------- #


async def test_without_record_use_nothing_is_written_and_no_document_is_read(env) -> None:
    """Re-review f3-2, M-3: a person who may not connect AI apps here keeps no
    app alive and has no document downloaded -- a stored copy answers, even
    an expired one, and without one the document is "unavailable"."""
    org_id, now = await _org(env), datetime.now(UTC)
    long_ago = now - timedelta(days=20)
    registered = await seed_client(env, org_id, last_used_at=long_ago)
    await seed_client(
        env,
        org_id,
        client_id=DOCUMENT_URL,
        kind="metadata_document",
        last_used_at=long_ago,
        metadata_expires_at=now - timedelta(minutes=1),
    )
    fetch = FakeFetch()
    for client_id in (registered.client_id, DOCUMENT_URL):
        found = await clients.resolve_client(
            env.app, org_id=org_id, client_id=client_id, now=now, fetch=fetch, record_use=False
        )
        assert found is not None and found.client_id == client_id
    with pytest.raises(cimd.DocumentRefused) as unread:
        await clients.resolve_client(
            env.app,
            org_id=org_id,
            client_id="https://other.example/client.json",
            now=now,
            fetch=fetch,
            record_use=False,
        )
    assert unread.value.reason == "unavailable"
    assert fetch.calls == 0
    async with oauth_session(org_id, env.app_engine) as session:
        used = (await session.execute(select(OAuthClient.last_used_at))).scalars().all()
    assert used == [long_ago, long_ago]


async def test_with_mcp_off_no_document_is_read_and_nothing_is_written(env) -> None:
    """Fix round 1, defence in depth for 1c(d) ("con MCP apagado no se
    registra nada ni se descarga ningún documento"): `resolve_client` reads
    the switch itself, in the transaction it already opens, whatever its
    caller checked. With MCP off it answers as `record_use=False` does: what
    is stored still answers, nothing is written, no document is fetched, and
    one never stored is "unavailable"."""
    org_id, now = await _org(env), datetime.now(UTC)
    long_ago = now - timedelta(days=20)
    registered = await seed_client(env, org_id, last_used_at=long_ago)
    await seed_client(
        env,
        org_id,
        client_id=DOCUMENT_URL,
        kind="metadata_document",
        last_used_at=long_ago,
        metadata_expires_at=now - timedelta(minutes=1),
    )
    await _switch(env, org_id, False)
    fetch = FakeFetch()
    for client_id in (registered.client_id, DOCUMENT_URL):
        found = await _resolve(env, fetch, now, client_id=client_id)
        assert found is not None and found.client_id == client_id
    with pytest.raises(cimd.DocumentRefused) as unread:
        await _resolve(env, fetch, now, client_id="https://other.example/client.json")
    assert unread.value.reason == "unavailable"
    assert fetch.calls == 0
    async with oauth_session(org_id, env.app_engine) as session:
        used = (await session.execute(select(OAuthClient.last_used_at))).scalars().all()
    assert used == [long_ago, long_ago]


async def test_a_use_is_recorded_on_a_held_row(env) -> None:
    """With `record_use`, a found app's use is written at most once an hour
    (`touch`), to the row as `clients.hold` re-read it."""
    org_id, now = await _org(env), datetime.now(UTC)
    app = await seed_client(env, org_id, last_used_at=now - timedelta(hours=2))
    assert await _resolve(env, FakeFetch(), now, client_id=app.client_id) is not None
    async with oauth_session(org_id, env.app_engine) as session:
        stored = (
            await session.execute(select(OAuthClient.last_used_at).where(OAuthClient.id == app.id))
        ).scalar_one()
    assert stored == now


async def test_an_expired_copy_is_refreshed_in_place(env) -> None:
    now = datetime.now(UTC)
    first = await _resolve(env, FakeFetch(), now)
    renamed = cimd.ClientDocument(
        client_id=DOCUMENT_URL,
        client_name="Claude Code 2",
        redirect_uris=("http://127.0.0.1/callback",),
        cache_for=timedelta(hours=1),
    )
    later = now + timedelta(hours=2)
    second = await _resolve(env, FakeFetch(renamed), later)
    assert second.id == first.id
    assert (second.client_name, second.redirect_uris) == (
        "Claude Code 2",
        ("http://127.0.0.1/callback",),
    )
    async with oauth_session(await _org(env), env.app_engine) as session:
        row = (await session.execute(select(OAuthClient))).scalar_one()
    assert (row.last_used_at, row.metadata_expires_at) == (later, later + timedelta(hours=1))


async def test_an_idle_document_client_comes_back_as_a_new_app_without_its_grants(env) -> None:
    """Plan D10: an app unused for 30 days is gone, with its grants, whoever
    asks for it next. A document read for a client whose row went idle
    removes that row the way the purge removes one -- the idle condition
    inside the DELETE, its grants by cascade -- and stores a new one, rather
    than reviving the row and the grants under it."""
    org_id, now = await _org(env), datetime.now(UTC)
    user_id = await user_id_of(env, await env.create_user(org_id, "org-admin"))
    idle = await seed_client(
        env,
        org_id,
        client_id=DOCUMENT_URL,
        kind="metadata_document",
        last_used_at=now - timedelta(days=31),
        metadata_expires_at=now - timedelta(days=30),
    )
    await _grant(env, org_id, user_id, idle)
    fetch = FakeFetch()
    found = await _resolve(env, fetch, now)
    assert fetch.calls == 1
    assert found is not None and found.id != idle.id
    assert await _families(env, org_id) == []
    async with oauth_session(org_id, env.app_engine) as session:
        ids = list((await session.execute(select(OAuthClient.id))).scalars())
    assert ids == [found.id]
