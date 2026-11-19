"""Own authentication phase 4: design §10's falsifier 11 (added 2026-11-15 by
the phase 4 plan, Task 11) -- no Keycloak token, image or service is reachable
or accepted anywhere -- and the documents' half of falsifier 10.

THE TOKEN HALF IS WALKED, NOT SAMPLED, AND THE KEY IS ON OFFER. Every route
under /api/v1, every method, is asked twice: with no credential, and with a
bearer shaped exactly like the console's Keycloak access token (`iss` the
realm, `aud` `it-suite`, `azp` the console's SPA, `typ` Bearer, `org`, a
`sub` that IS a user of that organization), signed by `ForeignIssuer`'s key.
And while the walk runs, every request the platform sends out -- over httpx
or over urllib, which PyJWT's JWKS client uses -- is answered with that
key's JWKS (and an OpenID discovery document that points at it). So a door
that still validated such a token -- the validator phase 4 removed, a JWKS
cache, a fallback -- would be handed the right key, accept the token and
answer the second request differently: a validator that refuses an UNKNOWN
key does not pass this test by refusing, because the key is not unknown to
it. The positive control below is such a door, and the same comparison must
flag it; offered another key, the same door is invisible to the comparison,
which is why the key is offered. `/mcp` must refuse the token as an invalid
token. The console's socket refuses it too, in `tests/test_token_origin.py`.

The key is served by a transport of this file's own (`offer_the_key`), not
by `test_user_admin_api.install_outbound`, which answers every request with
a 599 and so would hand a would-be validator nothing to accept.

THE DEPLOYMENT HALF READS WHAT SHIPS. No setting (the twelve phase 4
removed, named one by one, and none like them), no compose service, image
or router, no `deploy/keycloak/`, no console code that asks an identity
provider to sign it out or loads the deleted `lib/auth.ts`, and no token
verification in `app/` but the platform's own three (its access tokens, its
loopback tokens, Microsoft's ID tokens). The `deploy` CI job asserts the
same of the running stack (`deploy/ci/mcp_flow.py`, and its two
`grep -qi keycloak` checks).

Falsifier 10 -- no screen says "Keycloak" -- is the two guards that already
hold it: `frontend/src/lib/noVendorNames.test.ts` and
`backend/tests/test_no_vendor_names_backend.py`. Its documents' half is
here: `test_docs_without_keycloak.py` (Task 10) checks a list of nine, and
this file walks every living Markdown document in the repository, with the
runbooks' rule for a dated line (`test_runbook_without_keycloak.DATED`:
"phase 4" dates it, "until phase 4" does not).
"""

import ast
import io
import json
import os
import re
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.config import Settings
from tests.auth_support import console_ready
from tests.foreign_tokens import ISSUER, ForeignIssuer
from tests.test_auth_phase2_falsifiers import _filled, _routes
from tests.test_config import KEYCLOAK_ERA_ENV
from tests.test_docs_without_keycloak import LIVING as TASK_10_LIVING
from tests.test_runbook_without_keycloak import BACKUP_DOC, GO_LIVE, README, _dated
from tests.traefik_routes import COMPOSE, routers

REPO = Path(__file__).resolve().parents[2]
APP = REPO / "backend" / "app"
CONSOLE_SRC = REPO / "frontend" / "src"
DRILL = REPO / "deploy" / "drill" / "docker-compose.drill.yml"
#: Where the realm published its keys; what a JWKS client of it fetched.
CERTS = f"{ISSUER}/protocol/openid-connect/certs"

#: The sign-in's and the setup's own routes: a bearer is no part of them.
EXCEPT_PREFIXES = ("/api/v1/auth/", "/api/v1/setup/")
_REAL_ASYNC_CLIENT = httpx.AsyncClient
_REAL_CLIENT = httpx.Client
_REAL_OPEN = urllib.request.OpenerDirector.open


class OfferedKey:
    """Answers every outbound request with `issuer`'s key, and keeps a list."""

    def __init__(self, issuer: ForeignIssuer) -> None:
        self.jwks = {"keys": [issuer.public_jwk()]}
        self.seen: list[str] = []

    def document(self, url: str) -> dict[str, Any]:
        self.seen.append(url)
        if url.rstrip("/").endswith("/.well-known/openid-configuration"):
            return {"issuer": ISSUER, "jwks_uri": CERTS}
        return self.jwks

    def handler(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=self.document(str(request.url)))


def offer_the_key(monkeypatch: pytest.MonkeyPatch, issuer: ForeignIssuer) -> OfferedKey:
    """From now until the test ends, every request out of the process gets
    `issuer`'s JWKS for any URL: through every httpx client but an
    in-process one, and through every urllib opener.

    The same ways out as `test_user_admin_api.install_outbound`, which this
    cannot reuse (it answers 599): a client built now has its transport and
    its mounts replaced, and httpx's own network transports answer for as
    long as this is installed -- so a client built BEFORE it, as a JWKS
    cache built at startup was, is handed the key too."""
    offered = OfferedKey(issuer)
    in_process = (httpx.ASGITransport, httpx.MockTransport)

    def to_offer() -> httpx.MockTransport:
        return httpx.MockTransport(offered.handler)

    def routed(kw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(kw.get("transport"), in_process):
            kw["transport"] = to_offer()
        if kw.get("mounts"):
            # `None` is httpx's "this pattern uses `transport`", kept as it is.
            kw["mounts"] = {
                pattern: mounted
                if mounted is None or isinstance(mounted, in_process)
                else to_offer()
                for pattern, mounted in kw["mounts"].items()
            }
        return kw

    def async_client(*args: Any, **kw: Any) -> httpx.AsyncClient:
        return _REAL_ASYNC_CLIENT(*args, **routed(kw))

    def client(*args: Any, **kw: Any) -> httpx.Client:
        return _REAL_CLIENT(*args, **routed(kw))

    async def network_async(
        self: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return offered.handler(request)

    def network_sync(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        return offered.handler(request)

    def opener(self: urllib.request.OpenerDirector, fullurl: Any, *a: Any, **kw: Any) -> Any:
        url = fullurl if isinstance(fullurl, str) else fullurl.full_url
        if url.startswith(("http://", "https://")):
            return io.BytesIO(json.dumps(offered.document(url)).encode())
        return _REAL_OPEN(self, fullurl, *a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", network_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", network_sync)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", opener)
    return offered


async def _differs(
    client: httpx.AsyncClient, walked: Sequence[tuple[str, str]], bearer: dict[str, str]
) -> list[tuple[str, str, int, int]]:
    """Each route asked bare and with BEARER; the ones that answer differently."""
    found: list[tuple[str, str, int, int]] = []
    for method, path in walked:
        url = _filled(path)
        bare = await client.request(method, url, json={})
        shaped = await client.request(method, url, headers=bearer, json={})
        if bare.status_code != shaped.status_code:
            found.append((method, path, bare.status_code, shaped.status_code))
    return found


async def _keycloak_shaped(env: Any) -> tuple[str, dict[str, str]]:
    """The token, and the console session headers of the same person."""
    org_id = await env.create_org("f11-keycloak")
    sub = await env.create_user(org_id, "org-admin")
    token = str(env.foreign.token(sub, await env.org_slug(org_id)))
    return token, await env.headers(org_id, sub)


def _a_door_that_still_trusts_the_realm() -> tuple[FastAPI, httpx.AsyncClient]:
    """The positive control: what `app/core/security.py` was, three ways --
    a JWKS fetched by a client built at startup (its `JwksCache`), by one
    built per request over httpx, and by PyJWT's own client over urllib --
    each behind one route. Like that validator, a token it cannot verify, a
    key it does not have included, is a 401: no credential and an unknown
    key look the same from outside. Returns the startup client to close."""
    door = FastAPI()
    at_startup = httpx.AsyncClient()

    def _bearer(request: Request) -> str | None:
        header = request.headers.get("authorization", "")
        return header.removeprefix("Bearer ") if header.startswith("Bearer ") else None

    def _verified(token: str, keys: list[dict[str, Any]]) -> JSONResponse:
        try:
            claims = jwt.decode(
                token, jwt.PyJWK(keys[0]).key, algorithms=["RS256"], audience="it-suite"
            )
        except jwt.PyJWTError:
            return JSONResponse({"detail": "bad token"}, status_code=401)
        return JSONResponse({"sub": claims["sub"]})

    @door.get("/api/v1/control/startup")
    async def over_a_startup_client(request: Request) -> JSONResponse:
        token = _bearer(request)
        if token is None:
            return JSONResponse({"detail": "no token"}, status_code=401)
        return _verified(token, (await at_startup.get(CERTS)).json()["keys"])

    @door.get("/api/v1/control/httpx")
    async def over_httpx(request: Request) -> JSONResponse:
        token = _bearer(request)
        if token is None:
            return JSONResponse({"detail": "no token"}, status_code=401)
        async with httpx.AsyncClient() as fetch:
            return _verified(token, (await fetch.get(CERTS)).json()["keys"])

    @door.get("/api/v1/control/urllib")
    async def over_urllib(request: Request) -> JSONResponse:
        token = _bearer(request)
        if token is None:
            return JSONResponse({"detail": "no token"}, status_code=401)
        try:
            key = jwt.PyJWKClient(CERTS).get_signing_key_from_jwt(token)
            claims = jwt.decode(token, key.key, algorithms=["RS256"], audience="it-suite")
        except jwt.PyJWTError:
            return JSONResponse({"detail": "bad token"}, status_code=401)
        return JSONResponse({"sub": claims["sub"]})

    return door, at_startup


async def test_the_walk_sees_a_door_that_trusts_the_realm(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-vacuity of the walk below, and why it offers the key. The door
    refuses an unknown key with a 401: offered a stranger's, it answers the
    token as it answers no credential, and the comparison cannot see it.
    Offered the key that signed the token, the same door accepts it on
    every way in, and the comparison flags each one -- with the JWKS served
    from this file's transport, and the token's own subject answered back."""
    token = str(api_env.foreign.token("someone"))
    bearer = {"Authorization": f"Bearer {token}"}
    walked = [("GET", f"/api/v1/control/{way}") for way in ("startup", "httpx", "urllib")]
    door, at_startup = _a_door_that_still_trusts_the_realm()
    transport = httpx.ASGITransport(app=door)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://door") as client:
            stranger = offer_the_key(monkeypatch, ForeignIssuer(kid="a-stranger"))
            assert await _differs(client, walked, bearer) == []
            assert stranger.seen == [CERTS] * 3

            offered = offer_the_key(monkeypatch, api_env.foreign)
            found = await _differs(client, walked, bearer)
            assert found == [(method, path, 401, 200) for method, path in walked]
            assert offered.seen == [CERTS] * 3
            for _method, path in walked:
                answer = await client.get(path, headers=bearer)
                assert answer.json() == {"sub": "someone"}, path
    finally:
        await at_startup.aclose()


async def test_falsifier_11_a_keycloak_token_changes_nothing_at_any_route(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    console_ready(api_env, monkeypatch)
    token, session = await _keycloak_shaped(api_env)
    offered = offer_the_key(monkeypatch, api_env.foreign)
    # Non-vacuity: the comparison sees a credential that works.
    me_bare = await api_env.client.get("/api/v1/me")
    me_session = await api_env.client.get("/api/v1/me", headers=session)
    assert (me_bare.status_code, me_session.status_code) == (401, 200)
    walked = [(m, p) for m, p, _r in _routes(api_env.app) if not p.startswith(EXCEPT_PREFIXES)]
    assert len(walked) > 300, "the walk found too few routes to mean anything"
    differs = await _differs(api_env.client, walked, {"Authorization": f"Bearer {token}"})
    assert differs == []
    # And nothing even asked for the key: no request left the process at all.
    assert offered.seen == []


async def test_falsifier_11_mcp_refuses_a_keycloak_token(
    api_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.mcp_support import mcp_client, running_mcp

    api_env.settings.public_console_url = "https://itms.example.com"
    token, _session = await _keycloak_shaped(api_env)
    offer_the_key(monkeypatch, api_env.foreign)
    async with running_mcp(api_env), mcp_client(api_env) as client:
        answer = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            },
        )
    assert answer.status_code == 401
    assert 'error="invalid_token"' in answer.headers["www-authenticate"]


#: Every setting phase 4 removed, as `Settings` declared it at `dd062f8`:
#: Task 4's (`app/core/config.py:32-56`) and Task 5's (`:102-113`). Named one
#: by one: `tests/test_config.py` pins the `oidc_*` ones and `multi_tenant`,
#: and Task 5's six `keycloak_*` are pinned here and nowhere else (P4-33).
REMOVED_SETTINGS = frozenset(
    {
        "oidc_issuer",
        "oidc_audience",
        "oidc_jwks_url",
        "oidc_org_claim",
        "oidc_console_client_id",
        "multi_tenant",
        "keycloak_admin_url",
        "keycloak_realm",
        "keycloak_events_client_id",
        "keycloak_events_secret_path",
        "keycloak_admin_client_id",
        "keycloak_admin_secret_path",
    }
)


def test_falsifier_11_no_setting_of_the_old_provider_is_left(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """None of the twelve is a field, none like them is, and a deployment's
    environment that still names every one of them (an upgraded `.env`,
    `tests/test_config.py::KEYCLOAK_ERA_ENV`) reaches none: no attribute,
    nothing in the dump."""
    assert len({name for name in REMOVED_SETTINGS if name.startswith("keycloak_")}) == 6
    fields = set(Settings.model_fields)
    assert REMOVED_SETTINGS & fields == set()
    like_them = re.compile(r"^(oidc_|keycloak_|kc_)|keycloak|jwks|^multi_tenant$")
    assert {name for name in fields if like_them.search(name)} == set()

    names = {f"ITS_{name.upper()}" for name in REMOVED_SETTINGS}
    assert names <= set(KEYCLOAK_ERA_ENV), sorted(names - set(KEYCLOAK_ERA_ENV))
    for key, value in KEYCLOAK_ERA_ENV.items():
        monkeypatch.setenv(key, value)
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://app_user:app@localhost/x",
        database_admin_url="postgresql+asyncpg://admin:a@localhost/x",
        ca_dir=tmp_path / "ca",
    )
    assert [name for name in sorted(REMOVED_SETTINGS) if hasattr(settings, name)] == []
    assert REMOVED_SETTINGS.isdisjoint(settings.model_dump())


def test_falsifier_11_nothing_of_it_is_deployed() -> None:
    for path in (COMPOSE, DRILL):
        services = yaml.safe_load(path.read_text())["services"]
        for name, service in services.items():
            assert "keycloak" not in name.lower(), (path.name, name)
            assert "keycloak" not in str(service.get("image", "")).lower(), (path.name, name)
    # No file under it (git keeps no empty directory, but a checkout can; and
    # the bytecode the old test of `ci_mcp_flow.py` left there is not shipped).
    left = (REPO / "deploy").glob("keycloak/**/*")
    assert sorted(p for p in left if "__pycache__" not in p.parts) == []
    assert {name for _p, name, _r in routers()} == {"api", "minio", "web"}


def _console_sources() -> list[Path]:
    """Every source file of the console that is not a test, the set the
    deleted `test_console_auth_realm.py` read."""
    return [
        path
        for path in sorted(CONSOLE_SRC.rglob("*"))
        if path.suffix in {".ts", ".tsx"}
        and ".test." not in path.name
        and "__tests__" not in path.parts
    ]


#: The console's own sign-in module until own authentication Task 10, which
#: deleted it with `oidc-client-ts`: it started every step at Keycloak.
DELETED_AUTH_MODULE = CONSOLE_SRC / "lib" / "auth"
_SPECIFIER = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*\(\s*|\bimport\s+|\brequire\s*\(\s*)["'](\.{1,2}/[^"']*)["']"""
)


def _imported(source: str, path: Path) -> set[Path]:
    """Every module `source` (the file at `path`) imports by a relative
    specifier, resolved as a path without its extension. The console has no
    import alias (`tsconfig*.json`, `vite.config.ts`): a relative specifier
    is the only way to reach `lib/auth`."""
    found = set()
    for specifier in _SPECIFIER.findall(source):
        target = Path(os.path.normpath(path.parent / specifier))
        if target.suffix in {".ts", ".tsx", ".js"}:
            target = target.with_suffix("")
        found.add(target.parent if target.name == "index" else target)
    return found


def test_the_import_resolver_is_not_vacuous() -> None:
    lib = CONSOLE_SRC / "lib"
    assert _imported('import { signIn } from "./auth";\n', lib / "ws.ts") == {DELETED_AUTH_MODULE}
    assert _imported(
        'const m = await import("../lib/auth.ts");', CONSOLE_SRC / "modules" / "y.ts"
    ) == {DELETED_AUTH_MODULE}
    assert _imported('export { a } from "../auth/index";\n', lib / "z" / "w.ts") == {
        DELETED_AUTH_MODULE
    }
    # A comment that names the file, as `lib/session.ts` does, is no import.
    assert _imported("// It replaces `lib/auth.ts` and `oidc-client-ts`.\n", lib / "s.ts") == set()
    assert _imported('import { x } from "./authSurface";\n', lib / "s.ts") == {lib / "authSurface"}


def test_falsifier_11_the_console_asks_no_identity_provider_anything() -> None:
    """The console half of the deleted `test_console_auth_realm.py`
    (`test_the_console_no_longer_asks_the_realm_to_sign_it_out` and the
    `lib/auth.ts` check of `test_the_action_sets_are_read_from_both_sides_
    and_are_not_empty`), widened to any OpenID route: no console code that
    is not a test builds an OpenID sign-out, reads an end-session endpoint
    or names a `/realms/` URL, and none loads the deleted `lib/auth.ts`,
    which is not back either. (`lib/oidcLeftovers.ts` only deletes what the
    Keycloak-era console left in the browser's storage, P4-16.)

    `kc_action` and `signoutRedirect` are not repeated here: the console's
    own `lib/session.navigation.test.ts:58` refuses both (with
    `signinRedirect`) in every source that is not a test, over more than
    300 files."""
    sources = _console_sources()
    assert len(sources) > 300, len(sources)
    pattern = re.compile(r"post_logout_redirect_uri|end_session_endpoint|/realms/")
    texts = {path: path.read_text() for path in sources}
    offenders = [
        str(path.relative_to(REPO)) for path, text in texts.items() if pattern.search(text)
    ]
    assert offenders == [], offenders

    assert not DELETED_AUTH_MODULE.with_suffix(".ts").exists()
    assert not DELETED_AUTH_MODULE.with_suffix(".tsx").exists()
    assert not DELETED_AUTH_MODULE.exists()
    imports = {path: _imported(text, path) for path, text in texts.items()}
    # Non-vacuity: the resolver reads the real console, into `lib/` too.
    assert sum(len(found) for found in imports.values()) > 2000
    assert any(CONSOLE_SRC / "lib" / "session" in found for found in imports.values())
    loading = [
        str(p.relative_to(REPO)) for p, found in imports.items() if DELETED_AUTH_MODULE in found
    ]
    assert loading == [], loading


#: What PyJWT verifies or fetches keys with, under any of its spellings.
JWT_VERIFIERS = frozenset({"decode", "decode_complete", "PyJWKClient", "PyJWT", "PyJWS"})


def _decodes(source: str) -> list[int]:
    """Lines that verify a JWT or fetch keys for one: `jwt.decode(...)` and
    its kin, under any name the module is imported as (`import jwt as j`), a
    name imported from `jwt` (`from jwt import decode`), and any use of
    PyJWT's JWKS client or of its classes."""
    tree = ast.parse(source)
    modules = {"jwt"} | {
        alias.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "jwt" and alias.asname
    }
    imported = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "jwt"
        for alias in node.names
        if alias.name in JWT_VERIFIERS
    }
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in JWT_VERIFIERS:
            base = node.value
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name) and base.id in modules:
                found.add(node.lineno)
        elif isinstance(node, ast.Name) and node.id in imported:
            if not isinstance(node.ctx, ast.Store):
                found.add(node.lineno)
    return sorted(found)


def test_the_decode_sweep_is_not_vacuous() -> None:
    assert _decodes("import jwt\nclaims = jwt.decode(t, key, algorithms=['ES256'])\n") == [2]
    assert _decodes("# jwt.decode(t) in a comment\nx = 1\n") == []
    assert _decodes("from jwt import decode\nx = 1\nclaims = decode(t, k)\n") == [3]
    assert _decodes("from jwt import PyJWKClient as C\n\nC(u).get_signing_key_from_jwt(t)\n") == [3]
    assert _decodes("import jwt\njwt.api_jwt.decode_complete(t, k)\n") == [2]
    assert _decodes("import jwt\njwt.PyJWKClient(u)\n") == [2]
    assert _decodes("import jwt as _jwt\nx = 1\n_jwt.decode(t, k)\n") == [3]
    assert _decodes("import jwt as _jwt\n_jwt.PyJWK(j).key\n") == []
    assert _decodes("import jwt\ntoken = jwt.encode(c, k)\n") == []


def test_falsifier_11_only_the_platforms_own_tokens_are_decoded() -> None:
    """Its access tokens (`oauth/access.py`), its loopback tokens
    (`loopback.py`) and Microsoft's ID tokens (`entra.py`, against
    Microsoft's own keys). A fourth file verifying a JWT, or fetching keys
    for one, is a fourth door to argue for here."""
    found = {
        str(path.relative_to(APP))
        for path in sorted(APP.rglob("*.py"))
        if _decodes(path.read_text())
    }
    assert found == {"core/auth/oauth/access.py", "core/auth/loopback.py", "core/auth/entra.py"}


# --------------------------------------------------------------------------- #
# Falsifier 10, the documents' half
# --------------------------------------------------------------------------- #

NAMES = re.compile(r"keycloak", re.IGNORECASE)
#: Never walked: tools and dependencies, not the product's documents.
_PRUNED = frozenset({".git", ".claude", ".venv", "node_modules", "__pycache__"})
#: Dated records by where they live: plans, specs, audits, reviews.
RECORD_DIRS = (REPO / "docs" / "superpowers", REPO / "docs" / "reviews")
#: Dated records by their name: a date in it, or a module's or the kernel's
#: spec or plan -- "Specs and plans are dated records and may describe what
#: was decided then" (`test_docs_without_keycloak.py`). Not `docs/0N-*.md`,
#: the core documents, which `CLAUDE.md` calls authoritative.
RECORD_NAME = re.compile(r"\d{4}-\d{2}-\d{2}|^spec(-[\w-]+)?\.md$|(^|-)plan\.md$")
#: Walked by their own guard, `test_runbook_without_keycloak.py`, which also
#: allows the section that retires the old provider's data.
RUNBOOKS = (README, GO_LIVE, BACKUP_DOC)
#: The operator's, never edited by a build session; its one mention is the
#: dated example of 2026-08-20 (phase 4 plan, P4-14).
OPERATOR_FILES = (REPO / "CLAUDE.md",)
#: A design direction dated on its first line ("Direction for the settings
#: rework, 2026-09-11"), history in the phase 4 plan's Appendix D.
HISTORY = (REPO / "docs" / "design" / "settings-language.md",)


def _is_record(path: Path) -> bool:
    return (
        any(path.is_relative_to(directory) for directory in RECORD_DIRS)
        or (path.parent != REPO / "docs" and RECORD_NAME.search(path.name) is not None)
        or path in RUNBOOKS + OPERATOR_FILES + HISTORY
    )


def _living_documents() -> list[Path]:
    found = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = sorted(d for d in dirs if d not in _PRUNED)
        for name in sorted(files):
            path = Path(root) / name
            if name.endswith(".md") and not _is_record(path):
                found.append(path)
    return found


def _undated(text: str, name: str = "doc") -> list[str]:
    """Task 10's rule -- a line may name the old identity provider only if
    "phase 4" is on it, on one of the three lines before or on the next --
    with the runbooks' reading of "phase 4" (`_dated`): "until phase 4" is
    the same words in the future tense, and dates nothing."""
    lines = text.splitlines()
    return [
        f"{name}:{index + 1}: {line.strip()[:100]}"
        for index, line in enumerate(lines)
        if NAMES.search(line) and not _dated(lines, index)
    ]


def test_falsifier_10_the_document_rule_sees_an_undated_name() -> None:
    assert _undated("Auth -- Keycloak as identity broker.") != []
    assert _undated("Keycloak stays until phase 4.") != []
    assert _undated("Keycloak signs people in\nuntil own authentication phase 4.") != []
    assert _undated("Keycloak was removed in own\nauthentication phase 4.") == []
    assert _undated("2026-11-15 (phase 4):\n\n\nKeycloak's realm is gone.") == []
    assert _undated("phase 4 retired it.\n\n\n\nKeycloak") != []


def test_falsifier_10_the_walk_reads_the_living_documents_and_skips_only_records() -> None:
    walked = set(_living_documents())
    assert len(walked) > 40, len(walked)
    named = {
        REPO / "README.md",
        REPO / "agent" / "README.md",
        REPO / "docs" / "03-security-model.md",
        REPO / "docs" / "05-agent-spec.md",
        REPO / "deploy" / "host-network.md",
    }
    assert named | set(TASK_10_LIVING) <= walked, sorted(named | set(TASK_10_LIVING) - walked)
    # Every exception is a real file, and each kind of record skips one.
    for path in RUNBOOKS + OPERATOR_FILES + HISTORY:
        assert path.is_file(), path
    assert REPO / "docs" / "kernel" / "spec.md" not in walked
    assert REPO / "docs" / "modules" / "ai-admin" / "plan.md" not in walked
    assert REPO / "docs" / "kernel" / "security-review-2026-07-18.md" not in walked


def test_falsifier_10_no_living_document_names_keycloak_but_as_dated_history() -> None:
    offenders = [
        line
        for path in _living_documents()
        for line in _undated(path.read_text(), str(path.relative_to(REPO)))
    ]
    assert offenders == [], offenders
