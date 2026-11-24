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

The key is served through `tests/outbound_support.route_outbound`, the same
routing `install_outbound` is built on -- but by this file's own handler
(`offer_the_key`): the recorder answers every request with a 599, and so
would hand a would-be validator nothing to accept.

THE DEPLOYMENT HALF READS WHAT SHIPS. No setting (the twelve phase 4
removed, named one by one, and none like them), no compose service, image
or router, no `deploy/keycloak/`, no beat entry or worker task of it (the
removed `keycloak-event-sweep` by name), no realm's address in any string
literal of `app/`, no console code that asks an identity provider to sign it
out, no deleted `lib/auth.ts` come back, and no token verification in `app/`
but the platform's own three (its access tokens, its loopback tokens,
Microsoft's ID tokens). The `deploy` CI job asserts the
same of the running stack (`deploy/ci/mcp_flow.py`, and its two
`grep -qi keycloak` checks).

Falsifier 10 -- no screen says "Keycloak" -- is the two guards that already
hold it: `frontend/src/lib/noVendorNames.test.ts` and
`backend/tests/test_no_vendor_names_backend.py`. Its documents' half is
here: `test_docs_without_keycloak.py` (Task 10) checks a list of nine, and
this file walks every living Markdown document in the repository, the
kernel's spec included, with the runbooks' rule for a dated line
(`test_runbook_without_keycloak._dated`: own authentication's phase 4 by
name, or a date of 2026-11 or later, dates it; a bare "phase 4", "until ...
phase 4" and "phase 4 removes" do not -- final review of phase 4, C4).
"""

import ast
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.config import Settings, override_settings
from tests.auth_support import console_ready
from tests.foreign_tokens import ISSUER, ForeignIssuer
from tests.outbound_support import route_outbound
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
    `issuer`'s JWKS for any URL (`route_outbound`, with every way out that
    `install_outbound` covers: new httpx clients and their mounts, httpx's own
    network transports -- so a client built BEFORE this, as a JWKS cache built
    at startup was, is handed the key too -- and urllib, PyJWT's way)."""
    offered = OfferedKey(issuer)
    route_outbound(monkeypatch, offered.handler)
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
            # PyJWT (unpinned) may refetch on a kid it does not find: at least
            # once per way in, and only ever the realm's keys.
            assert set(stranger.seen) == {CERTS} and len(stranger.seen) >= 3

            offered = offer_the_key(monkeypatch, api_env.foreign)
            found = await _differs(client, walked, bearer)
            assert found == [(method, path, 401, 200) for method, path in walked]
            assert set(offered.seen) == {CERTS} and len(offered.seen) >= 3
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
    assert {name for _p, name, _r in routers()} == {"api", "api-unavailable", "minio", "web"}


#: The worker's half of the old provider until phase 4 of own authentication
#: (Task 5, 2026-11-15): the beat entry and the task that forwarded its
#: credential events into the audit chain (`app.modules.keycloak_events`).
REMOVED_WORKER_NAMES = frozenset(
    {"keycloak-event-sweep", "keycloak_event_sweep", "app.worker.keycloak_event_sweep"}
)
KEYCLOAK_NAMED = re.compile(r"keycloak", re.IGNORECASE)
WORKER = APP / "worker.py"


def _registered_task_names(source: str) -> set[str]:
    """Every `name=` of a `@<app>.task(...)` decorator. The worker registers
    its tasks on the `celery_app` it builds at import, and only when settings
    load -- in a test process they do not, and `celery_app` is None -- so the
    registrations are read where they are written."""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "task"
            ):
                for keyword in decorator.keywords:
                    if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                        names.add(str(keyword.value.value))
    return names


def test_falsifier_11_the_worker_schedules_and_registers_nothing_of_it(tmp_path: Path) -> None:
    """The beat schedule `create_celery()` builds, its entries' tasks, and
    every task the worker registers: none is the removed sweep, and none
    names the old provider, whatever the case. Non-vacuous: the schedule is
    not empty, the registrations are read, and every scheduled task is one
    of them -- so the two lists are the worker's, not an empty reading."""
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://user:pass@localhost:59999/nonexistent",
        database_admin_url="postgresql+asyncpg://user:pass@localhost:59999/nonexistent",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
        redis_url=os.environ.get("ITS_TEST_REDIS_URL", "redis://localhost:6379/9"),
    )
    from app import worker

    with override_settings(settings):
        schedule = worker.create_celery().conf.beat_schedule
    registered = _registered_task_names(WORKER.read_text())
    scheduled = {str(entry["task"]) for entry in schedule.values()}
    assert len(schedule) > 40, len(schedule)
    assert len(registered) > 40, len(registered)
    assert scheduled <= registered, sorted(scheduled - registered)

    names = set(schedule) | scheduled | registered
    assert names & REMOVED_WORKER_NAMES == set()
    assert sorted(name for name in names if KEYCLOAK_NAMED.search(name)) == []


#: A realm's address: where the console, the gateway and the worker reached
#: the old provider (its token and admin endpoints, the compose host).
REALM_ADDRESS = re.compile(r"/realms/|openid-connect|keycloak:8080", re.IGNORECASE)


def _realm_literals(source: str) -> list[int]:
    """Lines of every string literal -- an f-string's pieces and a docstring
    included -- that holds a realm's address."""
    return sorted(
        {
            node.lineno
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and REALM_ADDRESS.search(node.value)
        }
    )


def test_the_realm_literal_sweep_is_not_vacuous() -> None:
    url = 'u = f"{base}/realms/{realm}/protocol/openid-connect/token"\n'
    assert _realm_literals(url) == [1]
    assert _realm_literals('x = 1\nhost = "http://keycloak:8080"\n') == [2]
    assert _realm_literals('"""Talks to /admin/realms/x."""\n') == [1]
    assert _realm_literals("# /realms/ in a comment\nx = 1\n") == []
    assert _realm_literals('path = "/.well-known/oauth-authorization-server"\n') == []


def test_falsifier_11_no_code_holds_a_realm_address() -> None:
    """No string literal in `backend/app` is a realm's address. The sweep
    reads every module, and would see what the old clients built:
    `kc_client.py`, `realm_config.py` and `keycloak_events/client.py` held
    eight such lines at `dd062f8`."""
    modules = sorted(APP.rglob("*.py"))
    assert len(modules) > 300, len(modules)
    assert WORKER in modules and APP / "core" / "auth" / "oauth" / "grants.py" in modules
    found = {
        f"{path.relative_to(APP)}:{line}"
        for path in modules
        for line in _realm_literals(path.read_text())
    }
    assert sorted(found) == []


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


def test_falsifier_11_the_console_asks_no_identity_provider_anything() -> None:
    """The console half of the deleted `test_console_auth_realm.py`
    (`test_the_console_no_longer_asks_the_realm_to_sign_it_out` and the
    `lib/auth.ts` check of `test_the_action_sets_are_read_from_both_sides_
    and_are_not_empty`), widened to any OpenID route: no console code that
    is not a test builds an OpenID sign-out, reads an end-session endpoint
    or names a `/realms/` URL, and the deleted `lib/auth.ts` is not back.
    Nothing can load it while it is gone: every source is in `tsc -b`'s
    `include` (`src`), which refuses an import of a missing module (TS2307),
    and `npm run build` cannot resolve one either -- both run in CI's
    `frontend` job. (`lib/oidcLeftovers.ts` only deletes what the Keycloak-era
    console left in the browser's storage, P4-16.)

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
#: Named like a record and walked all the same (final review of phase 4, C4):
#: the platform kernel's spec, which every module registers into and which
#: `CLAUDE.md` holds authoritative with the rest of `docs/`. `RECORD_NAME`
#: skipped it by name, as it skips every module's `spec.md`.
AUTHORITATIVE = (REPO / "docs" / "kernel" / "spec.md",)


def _is_record(path: Path) -> bool:
    if path in AUTHORITATIVE:
        return False
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
    it is dated on it, on one of the three lines before or on the next --
    with the runbooks' reading of a date (`_dated`): own authentication's
    phase 4 by name, or a date of 2026-11 or later, and not in the future
    tense."""
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
    # The future tense however it is wrapped or capitalised (fix round 1):
    # the rule reads the window as one text, not line by line.
    assert _undated("Keycloak signs people in until own\nauthentication phase 4.") != []
    assert _undated("Keycloak signs people in until\nphase 4.") != []
    assert _undated("Until phase 4, Keycloak signs people in.") != []
    assert _undated("# Keycloak answers /realms until own authentication\n# phase 4.") != []
    # And history, capitalised or wrapped, is still history.
    assert _undated("Keycloak left the stack in\nPhase 4 (2026-11-15).") == []
    assert _undated("Keycloak was removed by own authentication\n*phase 4*.") == []
    # The final review's probes (C4): the present tense of a plan, the
    # roadmap's own Phase 4, and an "until" however far before.
    assert _undated("The console signs people in through Keycloak; phase 4 removes it.") != []
    assert _undated("## Phase 4 — Patch, App-deploy\n\nAuth: Keycloak as identity broker.") != []
    assert _undated("Keycloak signs people in until it is removed in phase 4.") != []


def test_falsifier_10_the_walk_reads_the_living_documents_and_skips_only_records() -> None:
    walked = set(_living_documents())
    assert len(walked) > 40, len(walked)
    named = {
        REPO / "README.md",
        REPO / "agent" / "README.md",
        REPO / "docs" / "03-security-model.md",
        REPO / "docs" / "05-agent-spec.md",
        REPO / "deploy" / "host-network.md",
        *AUTHORITATIVE,
    }
    assert named | set(TASK_10_LIVING) <= walked, sorted(named | set(TASK_10_LIVING) - walked)
    # Every exception is a real file, and each kind of record skips one.
    for path in RUNBOOKS + OPERATOR_FILES + HISTORY + AUTHORITATIVE:
        assert path.is_file(), path
    skipped = (
        REPO / "docs" / "modules" / "mdm" / "spec.md",
        REPO / "docs" / "modules" / "ai-admin" / "plan.md",
        REPO / "docs" / "kernel" / "security-review-2026-07-18.md",
    )
    for record in skipped:
        assert record.is_file(), record
        assert record not in walked, record


def test_falsifier_10_no_living_document_names_keycloak_but_as_dated_history() -> None:
    offenders = [
        line
        for path in _living_documents()
        for line in _undated(path.read_text(), str(path.relative_to(REPO)))
    ]
    assert offenders == [], offenders
