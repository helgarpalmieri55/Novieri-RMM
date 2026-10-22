# MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Any MCP client can connect to `https://<console>/mcp` over OAuth (Keycloak, PKCE, open but hardened DCR). It then acts as the signed-in person through one MCP tool per allowed `/api/v1` route. RBAC, tenancy and audit are exactly the console's. Plaintext secrets, files and streams are never reachable. Every call is audited with `via: "mcp"`.

**Architecture:** A raw ASGI app mounted at `/mcp` inside the existing FastAPI app does three things:
- It authenticates the bearer with the MCP audience (reusing `deps.auth_context`). It enforces the org switch, `mcp.use`, revocation and the rate limit.
- It hands the request to the official `mcp` SDK's stateless Streamable-HTTP session manager.
- Its `tools/call` handler calls the real route **in-process** (`httpx.ASGITransport`) through a wrapper that marks the ASGI scope (`scope["extensions"]["itms.mcp"]`). A network request cannot set that marker.

The API reads the marker to:
- validate the MCP audience instead of `it-suite`;
- refuse the never-exposed routes and permission codes;
- stamp `via: "mcp"` on every audit event.

Tools are generated at start-up from the live routes. A test partitions every route into tool or excluded-with-reason. Keycloak gets an `mcp` client scope, anonymous-registration policies and client policies, all converged by `keycloak-init`.

**Tech Stack:**
- Python 3.12, FastAPI 0.143 / Starlette 1.7, SQLAlchemy 2 async, Alembic, PyJWT, Redis
- `mcp==2.3.0`, the official SDK: `mcp.server.lowlevel.Server`, `mcp.server.streamable_http_manager.StreamableHTTPSessionManager`
- Keycloak 26.2
- React + TS + TanStack Query
- vitest, pytest

**Spec:** `docs/superpowers/specs/2026-10-09-mcp-server-design.md`. It was approved on 2026-10-09, including invariant 1c in §9, verbatim.

## Global Constraints

- **OAuth only.** There is no API key, no static token, no basic auth and no client-credentials grant. A request without a valid OAuth access token gets only a 401 carrying `WWW-Authenticate: Bearer resource_metadata="<console>/.well-known/oauth-protected-resource/mcp"`.
- **The MCP resource** is `ITS_PUBLIC_CONSOLE_URL` (trailing `/` stripped) + `/mcp`. With that setting unset, `/mcp` and the metadata answer **503** with a `tr()` sentence naming `ITS_PUBLIC_CONSOLE_URL`. **No new `.env` keys.**
- **Two separate audiences.** `/mcp` accepts only tokens whose `aud` includes the MCP resource. `/api` keeps accepting only `settings.oidc_audience` (`it-suite`). A console token on `/mcp` gets 401, and an MCP token on `/api` gets 401.
- **Checked on every MCP request:**
  - the org switch `mcp_enabled` (`Organization.settings`, off by default);
  - permission `mcp.use`;
  - `mcp_revocation`;
  - the rate limit of **120 calls per minute per person** (Redis `INCR`/`EXPIRE`, key `mcp:rate:user:{user_id}`, window 60 s; fails open on `RedisError`, as `ai_admin/api.py::_bump` does).
- **Tool call limits.** The timeout is **30 s per tool call**. The in-process call carries the caller's own bearer token. Path values are `quote(str(v), safe="")`.
- **Never exposed, refused twice** (no tool generated, AND the API refuses the call on the MCP channel). Routes:
  - `POST /api/v1/credentials/{credential_id}/checkout`
  - `GET /api/v1/licenses/{license_id}/key`
  - `POST /api/v1/enrollment-tokens`
  - `PUT /api/v1/install-link`
  - `POST /api/v1/install-link/regenerate`
  - `POST /api/v1/agent/releases/{release_id}/install-command`
  - `POST /api/v1/agent/releases/{release_id}/deployment-token`
  - `POST /api/v1/backup/recovery-keys/{key_id}/challenge`
  - `POST /api/v1/backup/recovery-keys/{key_id}/prove`
  - `POST /api/v1/setup/claim`

  Permission codes: `credentials.read`, `credentials.checkout`, `credentials.manage`, `licenses.keys.read` (the in-app AI's `_FORBIDDEN_GATES`).
- **Files and streams are never tools.** That covers:
  - `POST /install-link/msi`;
  - attachments, recordings and document content;
  - every CSV/XLSX export;
  - the in-app AI chat, turn and SSE routes;
  - WebSocket routes.
- **The scrub** uses `app.core.secrets.looks_like_secret_key` at every depth. A hit **blocks the whole result**; it is never redacted. A static test walks every exposed route's response model, and every matching field is on a reviewed allowlist `(METHOD path, field_path) → reason`, or the route is excluded.
- **Audit.** Every tool call writes `mcp.tool_called` (`actor_type="user"`) with metadata `{"tool", "app" (azp), "status", "args_sha256"}`. The arguments themselves are never stored. Every audit event written during an MCP call gains `{"via": "mcp", "mcp_app": azp}`. Also audit `mcp.app_revoked` and `mcp.settings_changed`.
- **New permission `mcp.use`**, seeded to `super-admin` and `org-admin` by migration.
- **New table `mcp_revocation`** `(id, org_id, user_id NULL, client_id, revoked_at, revoked_by)`, with FORCE RLS `tenant_isolation`.
- **Names and annotations.** Tool names are `[a-z0-9_]{1,64}` and unique. Annotations: `readOnlyHint` on GET, `destructiveHint` on DELETE plus the named destructive set, `idempotentHint` on PUT.
- **`?areas=a,b`** lists only tools whose first path segment after `/api/v1/` is in the list.
- **Language.** Every new person-visible server sentence goes through `tr()` with a Spanish entry in `app/core/i18n/es.py`; `tests/test_i18n_catalog.py` enforces this. Logs stay English, static templates. Every new console string is in `frontend/src/i18n/en/*.ts` **and** `es/*.ts` (typed `Shape<typeof en>`). New audit actions get `AuditAction` entries in `frontend/src/i18n/{en,es}/auditVocabulary.ts`, because `auditVocabulary.coverage.test.ts` reads the backend.
- **Engineering rules.**
  - Never edit existing English test assertions.
  - Do not run prettier; don't run `ruff format` on whole files.
  - Tests first.
  - Migrations are forward-only.
  - Commit trailers per session instructions.

---

## File structure

**Backend, new:**

| File | Responsibility |
|---|---|
| `app/core/mcp_channel.py` | The scope-extension key, the `McpCall` dataclass, `mcp_call_var`, `use_mcp_call()`, `mcp_call_from_scope()`, `mcp_resource(settings)`, `MCP_FORBIDDEN_CODES` |
| `app/mcp/__init__.py` | `build_mcp(app) -> McpMount` (wiring), used by `create_app` |
| `app/mcp/catalog.py` | Route enumeration, partition (`EXCLUDED`), naming, input schema, annotations, `Tool` dataclass, `build_catalog(app)` |
| `app/mcp/exclusions.py` | `NEVER_EXPOSED` (secrets), `FILE_OR_STREAM`, `NOT_FOR_MCP` (with reasons), `DESTRUCTIVE`, `NAME_OVERRIDES`, `OPEN_TO_ANY_SIGNED_IN` |
| `app/mcp/descriptions.py` | `DESCRIPTIONS: dict["METHOD /path", str]`, one or two sentences per exposed tool |
| `app/mcp/scrub_allowlist.py` | `REVIEWED_FIELDS: dict[tuple[str, str], str]` |
| `app/mcp/invoke.py` | `invoke(...)`: the in-process call, status mapping and scrub |
| `app/mcp/server.py` | The ASGI wrapper (`McpAsgi`), protected-resource metadata, the SDK `Server` handlers, rate limit, audit |
| `app/mcp/api.py` | Console admin routes `/api/v1/mcp/connections` (list) and `.../revoke` |
| `app/models/mcp.py` | `McpRevocation` model |
| `migrations/versions/0210_mcp.py` | Permission `mcp.use` and table `mcp_revocation` |
| `tests/test_mcp_*.py` | See each task |

**Backend, modified:**
- `app/core/security.py`: `TokenClaims` gains `azp`, `iat` and `aud`; `validate_token(..., audience=None)`.
- `app/api/deps.py`: MCP audience on marked requests; bind `mcp_call_var`; never-exposed refusal; refusal of forbidden codes in `require_permission`.
- `app/core/audit.py`: merge `via`/`mcp_app` from `mcp_call_var`.
- `app/api/v1/orgs.py`: `mcp_enabled` in `SETTINGS_ALLOWLIST`, plus the `mcp.settings_changed` audit.
- `app/core/rbac.py`: `"mcp.use"` in `PERMISSIONS`.
- `app/modules/user_admin/kc_client.py`: `revoke_consent`, a sixth operation.
- `app/main.py`: mount `/mcp`, the metadata routes, and the lifespan session manager.
- `pyproject.toml`: `mcp==2.3.0`.
- `deploy/keycloak/init-clients.py` and `realm-it-suite.json`: the `mcp` client scope, registration policies and client policies.
- `deploy/docker-compose.yml`: the Traefik api rule, and `ITS_PUBLIC_CONSOLE_URL` for keycloak-init.
- `.github/workflows/ci.yml`: the live Keycloak MCP step.
- `deploy/README.md`, `CLAUDE.md` (1c), and the five spec pointers.

**Frontend:**
- `src/modules/settings/AiConnectionsTab.tsx`
- `tabs.ts` and `SettingsPage.tsx`
- `settings/api.ts` hooks
- `lib/api.ts` methods and types
- `settings/fixtures.ts`
- `i18n/{en,es}/settings.ts`
- `i18n/{en,es}/auditVocabulary.ts`
- tests

---

### Task 1: The SDK, token claims, and the MCP channel core

**Files:**
- Modify: `backend/pyproject.toml` (dependencies), `backend/app/core/security.py`, `backend/app/core/config.py` (docstring only, no new key)
- Create: `backend/app/core/mcp_channel.py`
- Test: `backend/tests/test_mcp_channel.py`, `backend/tests/test_security.py` (add tests, edit nothing)
- Modify: `backend/tests/fake_idp.py` (add an `azp` and `extra` kwarg, defaulting so existing tokens are byte-identical)

**Interfaces:**
- Produces:
  - `TokenClaims(sub, email, name, org_slug, azp: str = "", iat: int = 0, aud: tuple[str, ...] = ())`
  - `validate_token(token, *, settings, jwks, audience: str | None = None) -> TokenClaims`
  - `mcp_channel.EXTENSION_KEY = "itms.mcp"`
  - `@dataclass(frozen=True) McpCall(azp: str, tool: str)`
  - `mcp_call_var: ContextVar[McpCall | None]`
  - `use_mcp_call(call) -> ContextManager`
  - `mcp_call_from_scope(scope) -> McpCall | None`
  - `mcp_resource(settings) -> str | None`
  - `MCP_FORBIDDEN_CODES: frozenset[str]`

- [ ] **Step 1: Add the dependency.** In `backend/pyproject.toml` `[project] dependencies`, add `"mcp==2.3.0",`. Run `cd backend && .venv/bin/pip install "mcp==2.3.0"`. The dry run showed no conflicts with FastAPI 0.143 / Starlette 1.7.

- [ ] **Step 2: Write the failing tests** in `backend/tests/test_mcp_channel.py`:

```python
from types import SimpleNamespace

import pytest

from app.core import mcp_channel
from app.core.mcp_channel import McpCall, mcp_call_from_scope, mcp_call_var, mcp_resource, use_mcp_call


def test_the_resource_is_the_console_url_plus_mcp() -> None:
    assert mcp_resource(SimpleNamespace(public_console_url="https://itms.example.com/")) == "https://itms.example.com/mcp"
    assert mcp_resource(SimpleNamespace(public_console_url=None)) is None
    assert mcp_resource(SimpleNamespace(public_console_url="  ")) is None


def test_a_scope_without_the_extension_is_not_mcp() -> None:
    assert mcp_call_from_scope({"type": "http", "extensions": {}}) is None
    assert mcp_call_from_scope({"type": "http"}) is None


def test_a_marked_scope_names_the_app_and_tool() -> None:
    scope = {"type": "http", "extensions": {mcp_channel.EXTENSION_KEY: {"azp": "claude", "tool": "devices_list"}}}
    assert mcp_call_from_scope(scope) == McpCall(azp="claude", tool="devices_list")


def test_a_malformed_marker_is_refused_rather_than_trusted() -> None:
    with pytest.raises(ValueError):
        mcp_call_from_scope({"extensions": {mcp_channel.EXTENSION_KEY: {"azp": 3}}})


def test_use_mcp_call_sets_and_resets() -> None:
    assert mcp_call_var.get() is None
    with use_mcp_call(McpCall("a", "t")):
        assert mcp_call_var.get() == McpCall("a", "t")
    assert mcp_call_var.get() is None


def test_the_forbidden_codes_are_the_in_app_ais() -> None:
    from app.core.ai.tools.registry import _FORBIDDEN_GATES

    assert mcp_channel.MCP_FORBIDDEN_CODES == _FORBIDDEN_GATES
```

Add the following to `backend/tests/test_security.py`. First read its fixtures and reuse the existing pattern for building a `JwksCache` over `FakeIdp`. These tests must assert that:
- (a) a token with `aud="https://itms.example.com/mcp"` fails `validate_token(..., settings=s)` (default audience `it-suite`) with `TokenValidationError`;
- (b) the same token passes with `audience="https://itms.example.com/mcp"`;
- (c) `claims.azp` equals the token's `azp` and `claims.iat` is an int;
- (d) an `it-suite` token fails with `audience=<mcp>`.

- [ ] **Step 3: Run them and see them fail.** Run `cd backend && .venv/bin/pytest tests/test_mcp_channel.py tests/test_security.py -q`. Expected: ImportError, or failures on the new tests.

- [ ] **Step 4: Implement.**

Create `backend/app/core/mcp_channel.py`:

```python
"""The MCP channel: how the API knows a request came from the platform's own MCP server.

AN ASGI SCOPE EXTENSION, NEVER A HEADER. `app/mcp/server.py` calls routes
in-process through a wrapper that adds `scope["extensions"][EXTENSION_KEY]`; a
network request reaches the app through uvicorn and cannot set a scope key. So
a client cannot claim to be "not MCP" (its token has only the MCP audience) and
cannot claim to be MCP without coming through `/mcp`.
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

EXTENSION_KEY = "itms.mcp"

#: Never reachable over MCP, whatever the person holds (spec §5.1; the in-app AI's
#: `_FORBIDDEN_GATES`). Invariant 3 is not amended.
MCP_FORBIDDEN_CODES: frozenset[str] = frozenset(
    {"credentials.read", "credentials.checkout", "credentials.manage", "licenses.keys.read"}
)


@dataclass(frozen=True)
class McpCall:
    azp: str
    tool: str


mcp_call_var: ContextVar[McpCall | None] = ContextVar("mcp_call", default=None)


@contextmanager
def use_mcp_call(call: McpCall) -> Iterator[None]:
    token = mcp_call_var.set(call)
    try:
        yield
    finally:
        mcp_call_var.reset(token)


def mcp_call_from_scope(scope: Mapping[str, Any]) -> McpCall | None:
    marker = (scope.get("extensions") or {}).get(EXTENSION_KEY)
    if marker is None:
        return None
    azp, tool = marker.get("azp"), marker.get("tool", "")
    if not isinstance(azp, str) or not isinstance(tool, str):
        raise ValueError("malformed MCP scope marker")
    return McpCall(azp=azp, tool=tool)


def mcp_resource(settings: Any) -> str | None:
    base = (getattr(settings, "public_console_url", None) or "").strip().rstrip("/")
    return f"{base}/mcp" if base else None
```

In `app/core/security.py`:
- add `azp: str = ""`, `iat: int = 0` and `aud: tuple[str, ...] = ()` to `TokenClaims`;
- give `validate_token` a keyword `audience: str | None = None`;
- decode with `audience=audience or settings.oidc_audience`;
- build the claims with:

```python
raw_aud = claims.get("aud")
aud = (raw_aud,) if isinstance(raw_aud, str) else tuple(str(a) for a in (raw_aud or ()))
return TokenClaims(sub=..., email=..., name=..., org_slug=org_slug,
                   azp=str(claims.get("azp") or ""), iat=int(claims["iat"]), aud=aud)
```

`iat` is already required. Every other line is unchanged.

In `tests/fake_idp.py` `FakeIdp.token`:
- add `azp: str | None = None` and `extra: dict | None = None`;
- add `"azp"` only when `azp` is not None, then merge `extra`;
- keep defaults identical, so existing tokens don't change.

- [ ] **Step 5: Run the tests and see them pass.** Run `.venv/bin/pytest tests/test_mcp_channel.py tests/test_security.py tests/test_deps*.py -q` and `.venv/bin/mypy app`.
- [ ] **Step 6: Commit** with the message `feat(mcp): SDK dependency, token azp/aud, and the MCP channel marker`.

---

### Task 2: Permission `mcp.use`, table `mcp_revocation`, org switch `mcp_enabled`

**Files:**
- Create: `backend/migrations/versions/0210_mcp.py`, `backend/app/models/mcp.py`
- Modify: `backend/app/models/__init__.py` (export), `backend/app/core/rbac.py` (`PERMISSIONS`), `backend/app/api/v1/orgs.py` (`SETTINGS_ALLOWLIST` and its docstring), `backend/tests/test_rbac.py` (add `"mcp.use"` to the hard-coded set in `test_permission_registry_complete`, which is that test's documented update path)
- Test: `backend/tests/test_mcp_schema.py`

**Interfaces:**
- Produces:
  - `McpRevocation(id: UUID, org_id: UUID, user_id: UUID | None, client_id: str, revoked_at: datetime, revoked_by: UUID | None)`
  - permission `"mcp.use"`
  - the org setting `"mcp_enabled": SettingSpec(kind="bool")`

- [ ] **Step 1: Write the failing tests** in `backend/tests/test_mcp_schema.py`, using `api_env`:

```python
import pytest
from sqlalchemy import select

from app.core.rbac import PERMISSIONS
from app.models import McpRevocation, Permission


async def test_mcp_use_is_catalogued_and_seeded(api_env) -> None:
    assert "mcp.use" in PERMISSIONS
    async with api_env.admin_session() as s:  # use whatever admin-session helper conftest offers; else AsyncSession(api_env.admin_engine)
        assert (await s.execute(select(Permission).where(Permission.code == "mcp.use"))).scalar_one()


async def test_org_admin_holds_mcp_use_and_a_technician_does_not(api_env) -> None:
    org = await api_env.create_org("mcp")
    admin = await api_env.create_user(org, "org-admin")
    tech = await api_env.create_user(org, "technician")
    me_admin = (await api_env.client.get("/api/v1/me", headers=await api_env.headers(org, admin))).json()
    me_tech = (await api_env.client.get("/api/v1/me", headers=await api_env.headers(org, tech))).json()
    assert "mcp.use" in me_admin["permissions"] and "mcp.use" not in me_tech["permissions"]


async def test_mcp_enabled_is_an_org_setting_that_defaults_off(api_env) -> None:
    org = await api_env.create_org("mcp")
    admin = await api_env.create_user(org, "org-admin")
    h = await api_env.headers(org, admin)
    assert (await api_env.client.get("/api/v1/orgs/current", headers=h)).json()["settings"].get("mcp_enabled") in (None, False)
    r = await api_env.client.patch("/api/v1/orgs/current", headers=h, json={"settings": {"mcp_enabled": True}})
    assert r.status_code == 200 and r.json()["settings"]["mcp_enabled"] is True
    bad = await api_env.client.patch("/api/v1/orgs/current", headers=h, json={"settings": {"mcp_enabled": "yes"}})
    assert bad.status_code == 422
```

Adjust these helper calls (`headers` sync or async, the admin session) to what `tests/conftest.py` actually exposes; read it first. `tests/test_rls.py` already fails for a tenant table without FORCE RLS, so it covers `mcp_revocation` once the migration exists.

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.**

Add `"mcp.use": "Connect an AI app to the platform over MCP and act through it as yourself",` to `PERMISSIONS` in `app/core/rbac.py`.

`app/models/mcp.py`. Follow the model style of the newest tenant model (read `app/models/` for push runs: `Mapped[...]`, `uuid7` default, `TimestampMixin` if they use it):

```python
class McpRevocation(Base):
    __tablename__ = "mcp_revocation"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), nullable=False, index=True)
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    client_id: Mapped[str] = mapped_column(String(255), nullable=False)
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    revoked_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    __table_args__ = (Index("ix_mcp_revocation_lookup", "org_id", "client_id"),)
```

`migrations/versions/0210_mcp.py` (revision `"0210"`, down_revision `"0200"`) creates the table with the same columns. It then applies the 0200 RLS pattern verbatim:

```python
_ORG_GUC = "current_setting('app.current_org', true)::uuid"
op.execute("ALTER TABLE mcp_revocation ENABLE ROW LEVEL SECURITY")
op.execute("ALTER TABLE mcp_revocation FORCE ROW LEVEL SECURITY")
op.execute(f"CREATE POLICY tenant_isolation ON mcp_revocation USING (org_id = {_ORG_GUC}) WITH CHECK (org_id = {_ORG_GUC})")
```

Finally it applies the 0170 permission pattern:
- `PERMISSIONS = [("mcp.use", "mcp", "use")]`;
- `ROLE_GRANTS = {"super-admin": ["mcp.use"], "org-admin": ["mcp.use"]}`;
- `downgrade()` deletes `role_permissions` and `permissions` by code, drops the policy, then drops the table.

In `app/api/v1/orgs.py`, add `"mcp_enabled": SettingSpec(kind="bool")` to `SETTINGS_ALLOWLIST`. Add a paragraph to its docstring under the security-control rule saying that:
- `mcp_enabled` is the second admitted security control;
- its enforcement is wired and tested in `app/mcp/server.py` and `tests/test_mcp_server.py`, on every request.

- [ ] **Step 4: Run** `.venv/bin/pytest tests/test_mcp_schema.py tests/test_rbac.py tests/test_rbac_admin_api.py tests/test_rls.py tests/test_org_settings.py tests/test_route_gating.py -q`. Then run the migration round trip: `.venv/bin/alembic upgrade head && .venv/bin/alembic downgrade base && .venv/bin/alembic upgrade head`, against a scratch DB if CI's migrations job does so; read `ci.yml` `migrations`.
- [ ] **Step 5: Commit** with the message `feat(mcp): mcp.use permission, mcp_revocation table, mcp_enabled switch`.

---

### Task 3: The API recognises the MCP channel

**Files:**
- Modify: `backend/app/api/deps.py`, `backend/app/core/audit.py`, `backend/app/main.py` (record `app.state.mcp_never_route_ids` after modules register)
- Create: `backend/app/mcp/__init__.py` (empty for now), `backend/app/mcp/exclusions.py` (the `NEVER_EXPOSED` set only, in this task)
- Test: `backend/tests/test_mcp_channel_api.py`

**Interfaces:**
- Consumes: Task 1's `mcp_channel`.
- Produces:
  - `exclusions.NEVER_EXPOSED: frozenset[tuple[str, str]]`, a set of `(method, full_path_template)` pairs;
  - `app.state.mcp_never_route_ids: frozenset[int]`;
  - the test helper `tests/mcp_support.py::marked_client(api_env, azp="claude", tool="t") -> httpx.AsyncClient` (an ASGITransport over a wrapper that adds the scope marker).

- [ ] **Step 1: Write the failing tests** in `backend/tests/test_mcp_channel_api.py`. Create `tests/mcp_support.py`:

```python
import httpx
from app.core.mcp_channel import EXTENSION_KEY

MCP_AUD = "https://itms.example.com/mcp"

def marked(app, azp: str = "claude", tool: str = "t"):
    async def wrapper(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "extensions": {**(scope.get("extensions") or {}), EXTENSION_KEY: {"azp": azp, "tool": tool}}}
        await app(scope, receive, send)
    return wrapper

def marked_client(api_env, azp: str = "claude", tool: str = "t") -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=marked(api_env.app, azp, tool)), base_url="http://mcp.test")

def mcp_token(api_env, org_slug: str, sub: str, azp: str = "claude") -> str:
    return api_env.idp.token(sub, org_slug, aud=MCP_AUD, azp=azp, email=f"{sub}@test.local")
```

The tests use an `api_env` whose settings set `public_console_url="https://itms.example.com"` (override via `api_env.settings.public_console_url = ...` if `Settings` is mutable, or a fixture variant; read conftest). They must show that:
1. An MCP token on a normal (unmarked) request to `GET /api/v1/me` gets 401.
2. An MCP token on a marked request to `GET /api/v1/me` gets 200.
3. A console token (`aud="it-suite"`) on a marked request gets 401.
4. A marked request to `POST /api/v1/enrollment-tokens` as org-admin with an MCP token gets 403, with the Spanish catalogue sentence under `Accept-Language: es`. Do this for every route in `NEVER_EXPOSED`, parametrised over the set, which must contain exactly the 10 routes in the Global Constraints.
5. A marked request to a route gated on `credentials.read` (e.g. `GET /api/v1/credentials`) as org-admin gets 403.
6. A marked write (e.g. `PATCH /api/v1/me` with `{"timezone":"UTC"}`) writes its audit event with `metadata["via"] == "mcp"` and `metadata["mcp_app"] == "claude"`. The same call unmarked, with a console token, has neither key.
7. `NEVER_EXPOSED` names only real routes: every pair is found in `flatten(app.router)`.

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.**

`app/mcp/exclusions.py` holds the `NEVER_EXPOSED` frozenset of the 10 `(method, path)` pairs from Global Constraints, with a reason comment per group.

In `app/main.py`, after `modules.register_all(app)`:

```python
from app.core.router_inspect import flatten
from app.mcp.exclusions import NEVER_EXPOSED
app.state.mcp_never_route_ids = frozenset(
    id(route) for path, route in flatten(app.router)
    if any((m, path) in NEVER_EXPOSED for m in getattr(route, "methods", ()) or ())
)
```

In `app/api/deps.py` `auth_context`:
- Before calling `validate_token`, read `call = mcp_call_from_scope(request.scope)`. Treat a `ValueError` as 401.
- If `call` is not None:
  - `audience = mcp_resource(request.app.state.settings)`. If that is None, return 503 `tr("MCP is not available: ITS_PUBLIC_CONSOLE_URL is not set.")`.
  - If `id(request.scope.get("route")) in request.app.state.mcp_never_route_ids`, return 403 `tr("this action is never available to an AI app")`.
- Pass `audience=audience` to `validate_token` (None for unmarked requests).

In `bound_auth_context`, wrap the `yield` so that when `call` is set it also runs `with use_mcp_call(call):`. Use `contextlib.ExitStack` so the unmarked path is unchanged.

In `require_permission.<locals>.dependency`, keep the name `dependency` and the closure variable `code`, because `route_permission` reads them. Before `has_permission`, add:

```python
if code in MCP_FORBIDDEN_CODES and mcp_call_from_scope(request.scope) is not None:
    raise HTTPException(status.HTTP_403_FORBIDDEN, tr("this action is never available to an AI app"))
```

In `app/core/audit.py`, at the two lines where `metadata = metadata or {}` (record) and where each entry's metadata is taken (record_many), merge:

```python
call = mcp_call_var.get()
if call is not None:
    metadata = {**metadata, "via": "mcp", "mcp_app": call.azp}
```

Add Spanish entries for the two new `tr()` sentences to `app/core/i18n/es.py`:
- "this action is never available to an AI app" → "esta acción nunca está disponible para una aplicación de IA"
- "MCP is not available: ITS_PUBLIC_CONSOLE_URL is not set." → "MCP no está disponible: ITS_PUBLIC_CONSOLE_URL no está configurado."

- [ ] **Step 4: Run** `.venv/bin/pytest tests/test_mcp_channel_api.py tests/test_i18n_catalog.py tests/test_route_gating.py tests/test_logging_discipline.py tests/test_audit*.py -q`, then `mypy app`. Run `tests/test_deps*.py` and `tests/test_me*.py` unchanged.
- [ ] **Step 5: Commit** with the message `feat(mcp): the API recognises the MCP channel (audience, never-exposed, forbidden codes, audit via)`.

---

### Task 4: The tool catalogue (partition, names, schemas, annotations)

**Files:**
- Create: `backend/app/mcp/catalog.py`, `backend/app/mcp/descriptions.py` (empty dict for now)
- Modify: `backend/app/mcp/exclusions.py` (add `FILE_OR_STREAM`, `NOT_FOR_MCP`, `DESTRUCTIVE`, `NAME_OVERRIDES`, `OPEN_TO_ANY_SIGNED_IN`)
- Test: `backend/tests/test_mcp_catalog.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class Tool:
    name: str                      # [a-z0-9_]{1,64}
    method: str                    # GET/POST/PUT/PATCH/DELETE
    path: str                      # full template, e.g. /api/v1/devices/{device_id}
    area: str                      # first segment after /api/v1/
    permission: str | None         # rbac.route_permission(route); None only if OPEN_TO_ANY_SIGNED_IN
    path_params: tuple[str, ...]
    query_params: tuple[str, ...]
    has_body: bool
    input_schema: dict[str, Any]   # JSON Schema, type=object, $defs inlined
    annotations: dict[str, bool]   # readOnlyHint / destructiveHint / idempotentHint
    description: str

def build_catalog(app: FastAPI) -> dict[str, Tool]   # name -> Tool
def route_decisions(app: FastAPI) -> dict[tuple[str, str], str]   # (method, path) -> "tool" | "excluded: <reason>"
```

- [ ] **Step 1: Write the failing tests** in `backend/tests/test_mcp_catalog.py`. Use `tests/tool_surface.py::build_router_only_app()` (already used by AI tests) to get a full app without a DB:

```python
import re
import pytest
from app.core.router_inspect import flatten
from app.mcp import exclusions
from app.mcp.catalog import build_catalog, route_decisions
from tests.tool_surface import build_router_only_app

@pytest.fixture(scope="module")
def app():
    return build_router_only_app()

def _api_routes(app):
    out = set()
    for path, route in flatten(app.router):
        if path.startswith("/api/v1/") and getattr(route, "methods", None):
            out |= {(m, path) for m in route.methods if m != "HEAD"}
    return out

def test_every_route_is_decided(app):
    decided = route_decisions(app)
    missing = _api_routes(app) - set(decided)
    assert not missing, f"undecided routes (add to the catalogue or an exclusion with a reason): {sorted(missing)}"

def test_every_exclusion_names_a_real_route_and_has_a_reason(app):
    real = _api_routes(app)
    for table in (exclusions.NEVER_EXPOSED_REASONS, exclusions.FILE_OR_STREAM, exclusions.NOT_FOR_MCP):
        for key, reason in table.items():
            assert key in real, f"stale exclusion {key}"
            assert reason.strip(), key

def test_never_exposed_routes_are_not_tools(app):
    tools = {(t.method, t.path) for t in build_catalog(app).values()}
    assert not (tools & exclusions.NEVER_EXPOSED)
    assert not (tools & set(exclusions.FILE_OR_STREAM))

def test_no_tool_is_gated_on_a_forbidden_code(app):
    from app.core.mcp_channel import MCP_FORBIDDEN_CODES
    assert not [t.name for t in build_catalog(app).values() if t.permission in MCP_FORBIDDEN_CODES]

def test_names_are_unique_valid_and_short(app):
    names = list(build_catalog(app))
    assert len(names) == len(set(names))
    assert all(re.fullmatch(r"[a-z0-9_]{1,64}", n) for n in names)

def test_examples_from_the_spec(app):
    cat = build_catalog(app)
    by_route = {(t.method, t.path): n for n, t in cat.items()}
    assert by_route[("GET", "/api/v1/devices")] == "devices_list"
    assert by_route[("GET", "/api/v1/devices/{device_id}")] == "devices_get"
    assert by_route[("POST", "/api/v1/devices/{device_id}/review/approve")] == "devices_review_approve"

def test_annotations(app):
    for t in build_catalog(app).values():
        assert t.annotations.get("readOnlyHint", False) == (t.method == "GET")
        if t.method == "DELETE" or (t.method, t.path) in exclusions.DESTRUCTIVE:
            assert t.annotations["destructiveHint"] is True
        if t.method == "PUT":
            assert t.annotations["idempotentHint"] is True

def test_ungated_routes_are_tools_only_when_listed_open(app):
    for t in build_catalog(app).values():
        assert t.permission is not None or (t.method, t.path) in exclusions.OPEN_TO_ANY_SIGNED_IN

def test_input_schema_is_an_object_with_path_params_required(app):
    t = build_catalog(app)["devices_get"]
    assert t.input_schema["type"] == "object"
    assert "device_id" in t.input_schema["properties"] and "device_id" in t.input_schema["required"]
    assert "#/components/" not in repr(t.input_schema)
```

- [ ] **Step 2: Run them and see them fail.**

- [ ] **Step 3: Implement the exclusion tables** in `exclusions.py`.

`NEVER_EXPOSED_REASONS: dict[(method, path), str]` holds the 10 routes, each with a reason such as "returns a plaintext credential (invariant 3)". Keep `NEVER_EXPOSED = frozenset(NEVER_EXPOSED_REASONS)`.

`FILE_OR_STREAM: dict[(method, path), str]` lists every file or stream route found in the codebase survey:
- `POST /api/v1/install-link/msi`
- `GET /api/v1/export/devices.csv`, `/export/devices.xlsx`, `/export/software.csv`, `/export/software.xlsx`
- `GET /api/v1/ipam/export/subnets.csv`, `/ipam/export/addresses.csv`, `/ipam/export/drift.csv`
- `GET /api/v1/reports/access.csv`
- the four `GET /api/v1/reports/{key}/export.csv` (fleet-posture, patch-compliance, backup-coverage, sla-attainment)
- `GET /api/v1/patch/report/export`, `GET /api/v1/backup/report/export`
- `GET /api/v1/budget/expenses/export`, `GET /api/v1/budget/expenses/export/odoo`
- `GET /api/v1/service-desk/attachments/{attachment_id}`
- `GET /api/v1/portal/tickets/{ticket_id}/attachments/{attachment_id}`
- `GET /api/v1/budget/expenses/{expense_id}/attachment`
- `GET /api/v1/remote/sessions/{session_id}/recording`
- `GET /api/v1/docs/documents/{document_id}/versions/{version}/content`
- the SSE `GET /api/v1/ai/sessions/{session_id}/turns/{turn_id}/stream`

Confirm each exact path with `flatten` while writing it. Also grep `backend/app` for every `response_class=` and `Response(` route, and every `UploadFile`/`File(` parameter (uploads are bytes in). Add each to `FILE_OR_STREAM` with a reason.

`NOT_FOR_MCP: dict[(method, path), str]`, with reasons:
- the in-app AI's own chat and turn routes: `POST /ai/sessions`, `POST /ai/sessions/{id}/turns`, the copilot and portal AI session/turn routes. Reason: "an AI calling the platform's own AI is circular".
- the MCP admin routes from Task 9. Reason: "connections are managed by the person in the console". Add those entries when Task 9 lands; that task's test makes them required.
- `GET /api/v1/setup/status` and any other route that `rbac.route_permission` returns None for and that is not in `OPEN_TO_ANY_SIGNED_IN`, with a reason each. Produce this list by running the partition test and reading what it reports; never add a route without a reason.
- the agent-facing routes under `/api/v1` that are not for people (e.g. agent self-service; check `flatten` for routes gated by agent mTLS, not by a person's token). Reason: "an agent's endpoint, not a person's".

`OPEN_TO_ANY_SIGNED_IN: frozenset[(method, path)]` = `{("GET", "/api/v1/me")}`, plus any other ungated person-route the partition shows. Justify each in a comment.

`DESTRUCTIVE: frozenset[(method, path)]` is the spec's named set. Find each path with `flatten`:
- push install and runs (create run, resume, dispatch);
- update rollouts (create, resume);
- `POST /devices/{device_id}/review/reject`;
- remote session create;
- patch apply/approve-and-deploy;
- script run;
- backup restore;
- app deploy (create deployment).

`NAME_OVERRIDES: dict[(method, path), str]` starts empty. Fill it only where the derived names collide or exceed 64 characters.

- [ ] **Step 4: Implement `app/mcp/catalog.py`.**

```python
"""MCP tools generated from the live API (spec §4.1). One tool per decided route."""

import re
from dataclasses import dataclass, field
from typing import Any
from fastapi import FastAPI
from app.core.rbac import route_permission
from app.core.router_inspect import flatten
from app.mcp import exclusions
from app.mcp.descriptions import DESCRIPTIONS

PREFIX = "/api/v1/"
_VERB = {"GET": "get", "POST": "create", "PUT": "set", "PATCH": "update", "DELETE": "delete"}

def derive_name(method: str, path: str) -> str:
    segments = [s for s in path.removeprefix(PREFIX).split("/") if s]
    words = [re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") for s in segments if not s.startswith("{")]
    ends_with_param = bool(segments) and segments[-1].startswith("{")
    has_param = any(s.startswith("{") for s in segments)
    if method == "GET":
        verb = "get" if ends_with_param else "list"
    elif method == "POST":
        # POST /devices/{id}/review/approve -> devices_review_approve (an action on a thing);
        # POST /devices -> devices_create (a new thing in a collection).
        verb = "" if has_param and not ends_with_param else "create"
    else:
        verb = _VERB[method]
    return "_".join(w for w in [*words, verb] if w)[:64]
```

The rules: `GET` → `get` on an item, `list` on a collection. `POST` after a `{param}` that ends in a literal is an action (no suffix); any other `POST` gets `create`. `PUT` → `set`, `PATCH` → `update`, `DELETE` → `delete`. This satisfies the three spec examples. `test_names_are_unique_valid_and_short` then drives `NAME_OVERRIDES` for the rest (collisions and truncation at 64).

The input schema comes from `app.openapi()`:

```python
def _operation(openapi: dict, method: str, path: str) -> dict:
    return openapi["paths"][path][method.lower()]

def _schema_for(op: dict, components: dict) -> tuple[dict, tuple[str, ...], tuple[str, ...], bool]:
    props: dict[str, Any] = {}
    required: list[str] = []
    path_params, query_params = [], []
    for p in op.get("parameters", []):
        if p["in"] not in ("path", "query"):
            continue                    # headers/cookies are never tool inputs
        props[p["name"]] = p.get("schema", {}) | ({"description": p["description"]} if p.get("description") else {})
        (path_params if p["in"] == "path" else query_params).append(p["name"])
        if p.get("required") or p["in"] == "path":
            required.append(p["name"])
    has_body = "requestBody" in op
    if has_body:
        body = op["requestBody"]["content"].get("application/json", {}).get("schema", {})
        props["body"] = body
        if op["requestBody"].get("required"):
            required.append("body")
    schema = {"type": "object", "properties": props, "required": required, "additionalProperties": False}
    text = json.dumps(schema).replace("#/components/schemas/", "#/$defs/")
    schema = json.loads(text)
    used = _referenced_defs(schema, components)          # transitive closure over $ref names
    if used:
        schema["$defs"] = json.loads(json.dumps(used).replace("#/components/schemas/", "#/$defs/"))
    return schema, tuple(path_params), tuple(query_params), has_body


_REF_PREFIXES = ("#/$defs/", "#/components/schemas/")

def _referenced_defs(node: Any, components: dict[str, Any]) -> dict[str, Any]:
    """Every component schema reachable from `node` by $ref, transitively."""
    found: dict[str, Any] = {}
    stack: list[Any] = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            ref = cur.get("$ref")
            if isinstance(ref, str):
                for prefix in _REF_PREFIXES:
                    if ref.startswith(prefix):
                        name = ref.removeprefix(prefix)
                        if name not in found:
                            found[name] = components[name]
                            stack.append(components[name])
            stack.extend(v for k, v in cur.items() if k != "$ref")
        elif isinstance(cur, list):
            stack.extend(cur)
    return found
```

`components` is `app.openapi()["components"]["schemas"]`. Import `json` at module top.

A route whose request body is not JSON (multipart) is in `FILE_OR_STREAM`, so the catalogue raises if it meets one.

`route_decisions(app)` iterates `flatten(app.router)`. It skips non-HTTP routes; WebSockets are excluded implicitly, so list them in a docstring. It returns `"excluded: <reason>"` for anything in the three exclusion tables or gated on a forbidden code. Everything else is `"tool"`, provided `route_permission(route)` is not None or the route is in `OPEN_TO_ANY_SIGNED_IN`. Otherwise it returns `"undecided"`, which the partition test treats as missing.

`build_catalog(app)` builds `Tool`s for the `"tool"` decisions. The description is `DESCRIPTIONS.get(f"{method} {path}", "")`; it is validated in Task 5. It applies `NAME_OVERRIDES` and raises `RuntimeError` on a duplicate name.

Annotations:
- `{"readOnlyHint": method == "GET"}`;
- `"destructiveHint": True` when `method == "DELETE"` or the route is in `DESTRUCTIVE`;
- `"idempotentHint": True` when `method == "PUT"`.

- [ ] **Step 5: Run.** `.venv/bin/pytest tests/test_mcp_catalog.py -q`. Iterate on the exclusion tables until the partition is complete. Every addition must carry a reason that is true; read the route before writing one.
- [ ] **Step 6: Commit** with the message `feat(mcp): tool catalogue generated from the live API, every route decided`.

---

### Tasks 5a–5d: Tool descriptions

**Files:**
- Modify: `backend/app/mcp/descriptions.py`
- Test: `backend/tests/test_mcp_descriptions.py`

**Procedure** (identical for each sub-task; each covers a set of areas, the first path segment):

1. For each exposed tool in the sub-task's areas, add `"METHOD /api/v1/path": "…"` to `DESCRIPTIONS`. Write one or two English sentences (≤ 300 characters), present tense, for an AI reader. Say what it does, what it returns, and any refusal or side effect a person would want an AI to know, e.g. "Rejecting a device uninstalls its agent." Read the route handler before writing.
2. Descriptions are not translated. The MCP tool surface is for the AI client, and the spec puts no language requirement on it. Do not route them through `tr()`.
3. Test, added in 5a and extended per sub-task:

```python
from app.mcp.catalog import build_catalog
from app.mcp.descriptions import DESCRIPTIONS
from tests.tool_surface import build_router_only_app

DONE_AREAS = {...}  # 5a sets it; each later sub-task adds its areas; 5d replaces it with "all"

def test_every_exposed_tool_in_done_areas_has_a_description():
    cat = build_catalog(build_router_only_app())
    missing = [n for n, t in cat.items() if t.area in DONE_AREAS and not t.description.strip()]
    assert not missing, missing

def test_no_description_names_an_unexposed_route():
    cat = build_catalog(build_router_only_app())
    exposed = {f"{t.method} {t.path}" for t in cat.values()}
    assert not set(DESCRIPTIONS) - exposed

def test_descriptions_are_short():
    assert all(0 < len(d) <= 300 for d in DESCRIPTIONS.values())
```

4. One commit per sub-task: `feat(mcp): descriptions for <areas>`.

| Sub-task | Areas |
|---|---|
| 5a | devices, device-groups, agents, jobs, audit, me, orgs, roles, users, dashboard, platform, errors, enrollment, install-link, agent, discovered |
| 5b | patch, deploy, inventory, health, remote, ipam, backup, bcp, provisioning, site-servers |
| 5c | service-desk, portal, sla, email-intake, notifications, people, docs, reports, security |
| 5d | licenses, subscriptions, budget, changes, ai, and every remaining area. Then `DONE_AREAS` becomes every area and the test asserts that no exposed tool lacks a description. |

Run `build_catalog` and list the actual areas first; adjust the table so every area is in exactly one sub-task.

---

### Task 6: The scrub, and the reviewed response-field allowlist

**Files:**
- Create: `backend/app/mcp/scrub_allowlist.py`
- Modify: `backend/app/mcp/catalog.py` (attach `response_model` to `Tool`: the route's `response_model` or `None`)
- Test: `backend/tests/test_mcp_scrub.py`

**Interfaces:**
- Produces:
  - `REVIEWED_FIELDS: dict[tuple[str, str], str]`, mapping `("METHOD /api/v1/path", "field.path")` to a reason;
  - `secret_key_path(payload: Any) -> str | None`, which returns the first secret-looking key path at any depth, or None.

- [ ] **Step 1: Write the failing tests.**

```python
from app.mcp.scrub_allowlist import REVIEWED_FIELDS, secret_key_path

def test_a_planted_secret_key_is_found_at_any_depth():
    assert secret_key_path({"items": [{"name": "x", "nested": {"api_token": "s"}}]}) == "items[].nested.api_token"
    assert secret_key_path({"name": "x", "count": 2}) is None

def test_every_secret_looking_response_field_of_an_exposed_tool_is_reviewed():
    from app.core.secrets import looks_like_secret_key
    from app.mcp.catalog import build_catalog
    from tests.tool_surface import build_router_only_app
    unreviewed = []
    for t in build_catalog(build_router_only_app()).values():
        for field_path in _field_paths(t.response_model):        # walk pydantic model_fields recursively, lists as []
            leaf = field_path.rsplit(".", 1)[-1].removesuffix("[]")
            if looks_like_secret_key(leaf) and (f"{t.method} {t.path}", field_path) not in REVIEWED_FIELDS:
                unreviewed.append((t.method, t.path, field_path))
    assert not unreviewed, unreviewed

def test_every_reviewed_field_is_real_and_reasoned():
    from app.mcp.catalog import build_catalog
    from tests.tool_surface import build_router_only_app
    fields = {f"{t.method} {t.path}": _field_paths(t.response_model) for t in build_catalog(build_router_only_app()).values()}
    for (route, field_path), reason in REVIEWED_FIELDS.items():
        assert route in fields, f"stale route: {route}"
        assert field_path in fields[route], f"stale field: {route} {field_path}"
        assert reason.strip(), (route, field_path)


def _field_paths(ann, prefix: str = "", seen: frozenset = frozenset()) -> set[str]:
    """Every JSON field path a response annotation can produce; lists as `[]`."""
    import collections.abc, types, typing
    from pydantic import BaseModel
    out: set[str] = set()
    origin, args = typing.get_origin(ann), typing.get_args(ann)
    if origin in (list, tuple, set, frozenset, collections.abc.Sequence):
        for a in args:
            if a is not Ellipsis:
                out |= _field_paths(a, prefix + "[]", seen)
    elif origin in (typing.Union, types.UnionType, typing.Annotated):
        for a in args:
            out |= _field_paths(a, prefix, seen)
    elif origin is dict and len(args) == 2:
        out |= _field_paths(args[1], prefix + ".*", seen)
    elif isinstance(ann, type) and issubclass(ann, BaseModel) and ann not in seen:
        for name, f in ann.model_fields.items():
            key = f.alias or name
            p = f"{prefix}.{key}" if prefix else key
            out.add(p)
            out |= _field_paths(f.annotation, p, seen | {ann})
    return out
```

The first test uses `_field_paths(t.response_model)` in place of `_field_paths(...)`'s `model` argument. A `None` response model yields nothing; the runtime scrub still applies. `secret_key_path` uses the same notation (`items[].nested.api_token`, a top-level list as `[].name`).

- [ ] **Step 2: Implement.** `secret_key_path` is a recursive walk using `looks_like_secret_key` on every dict key. Fill `REVIEWED_FIELDS` from the test's output, one entry per field with a true reason, e.g. `"has_key is a boolean; it says whether a key exists, never the key"`. A field that does carry a secret means the route moves to `NEVER_EXPOSED_REASONS` instead, and you report it.

  The runtime rule (Task 8):
  - A hit whose `(route, path)` is in `REVIEWED_FIELDS` with the list indices normalised to `[]` is allowed.
  - Any other hit blocks the whole result.
- [ ] **Step 3: Run, then commit** with the message `feat(mcp): reviewed response-field allowlist and the blocking scrub`.

---

### Task 7: `/mcp` transport, OAuth gate and protected-resource metadata

**Files:**
- Create: `backend/app/mcp/server.py` (the gate and metadata in this task; the SDK handlers come in Task 8)
- Modify: `backend/app/mcp/__init__.py` (`build_mcp`), `backend/app/main.py` (mount and lifespan)
- Test: `backend/tests/test_mcp_server.py`

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces:
  - `McpPrincipal(org_id: UUID, user_id: UUID, claims: TokenClaims, bearer: str, areas: frozenset[str] | None, accept_language: str)`, set at `scope["state"]["mcp_principal"]` for the SDK handlers;
  - `build_mcp(app) -> McpMount`, which has `.asgi` (mount at `/mcp`), `.lifespan()` (an async context manager running the session manager) and `.metadata_router` (an APIRouter for the two `.well-known` paths).

- [ ] **Step 1: Write the failing tests.** Use `tests/mcp_support.py`. Each case below is one test:

1. **Unset console URL.** With `public_console_url=None`, `GET /.well-known/oauth-protected-resource/mcp` returns 503, and `POST /mcp` returns 503.
2. **Metadata.** With it set to `https://itms.example.com`, `GET /.well-known/oauth-protected-resource/mcp` and `/.well-known/oauth-protected-resource` both return 200 with JSON:

   ```json
   {"resource": "https://itms.example.com/mcp",
    "authorization_servers": ["https://idp.test/realms/it-suite"],
    "scopes_supported": ["mcp"],
    "bearer_methods_supported": ["header"]}
   ```

   `authorization_servers` is `[settings.oidc_issuer]`.
3. **No token.** `POST /mcp` with no Authorization returns 401 with header `WWW-Authenticate: Bearer resource_metadata="https://itms.example.com/.well-known/oauth-protected-resource/mcp"`.
4. **Console token.** A console token (`aud="it-suite"`) on `POST /mcp` returns 401.
5. **MCP token, switch off.** An MCP token from an org-admin while the org switch is off returns 403, with a sentence saying MCP is off for the organization.
6. **MCP token, switch on, no `mcp.use`.** A technician (no `mcp.use`) gets 403 "requires mcp.use".
7. **Revocation.** Insert `McpRevocation(org_id, user_id=None, client_id="claude", revoked_at=now)`. A token with `iat` earlier than that returns 401. A token minted after it is accepted, so the person can reconnect deliberately. A per-user revocation affects only that user.
8. **No org claim.** A token without the `org` claim, in multi-tenant settings, returns 401.
9. **Language.** Under `Accept-Language: es`, the 403 bodies are the catalogue's Spanish.

For tests 5–9, send a minimal MCP `initialize` JSON-RPC POST with headers `Accept: application/json, text/event-stream` and `Content-Type: application/json`. Use the protocol version the SDK reports as its latest (`mcp.types.LATEST_PROTOCOL_VERSION` or equivalent; find it in the installed package).

- [ ] **Step 2: Run them and see them fail.**

- [ ] **Step 3: Implement `app/mcp/server.py`: the gate.**

```python
class McpAsgi:
    """The only door to the MCP server. Every request is authenticated here, then
    handed to the SDK; nothing reaches a tool handler without a principal."""

    def __init__(self, fastapi_app: FastAPI, session_manager: StreamableHTTPSessionManager) -> None:
        self._app = fastapi_app
        self._sm = session_manager

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return
        settings = self._app.state.settings
        resource = mcp_resource(settings)
        if resource is None:
            return await _json(send, 503, {"detail": tr("MCP is not available: ITS_PUBLIC_CONSOLE_URL is not set.")})
        request = Request(scope, receive)
        if not request.headers.get("authorization", "").startswith("Bearer "):
            return await _unauthorised(send, resource)
        # Reuse the console's whole admission path (org lookup, email domain, user
        # status, self-provisioning) with the MCP audience: mark the scope for auth.
        marked = {**scope, "app": self._app, "extensions": {**(scope.get("extensions") or {}), EXTENSION_KEY: {"azp": "", "tool": ""}}}
        try:
            ctx = await auth_context(Request(marked, receive))
        except HTTPException as exc:
            if exc.status_code == 401:
                return await _unauthorised(send, resource, detail=exc.detail)
            return await _json(send, exc.status_code, {"detail": exc.detail})
        refusal = await _gate(self._app, ctx)            # switch, mcp.use, revocation (None when allowed)
        if refusal is not None:
            status_code, detail = refusal
            return await (_unauthorised(send, resource, detail=detail) if status_code == 401 else _json(send, status_code, {"detail": detail}))
        areas = frozenset(a for a in request.query_params.get("areas", "").split(",") if a) or None
        principal = McpPrincipal(org_id=ctx.org_id, user_id=ctx.user_id, claims=ctx.claims,
                                 bearer=request.headers["authorization"], areas=areas,
                                 accept_language=request.headers.get("accept-language", ""))
        scope.setdefault("state", {})["mcp_principal"] = principal
        await self._sm.handle_request(scope, receive, send)
```

`_gate` opens `org_session(ctx.org_id, engine=app.state.app_engine)` and does three checks:
- `Organization.settings.get("mcp_enabled") is True`, else 403 `tr("MCP is turned off for this organization. An org-admin turns it on in Settings → AI connections.")`;
- `has_permission(session, ctx.user_id, "mcp.use")`, else 403 `tr("requires {code}", code="mcp.use")` (that sentence already exists);
- revocation: any `McpRevocation` row with `org_id`, `client_id == ctx.claims.azp`, and either `user_id is None` or `user_id == ctx.user_id`, whose `revoked_at > datetime.fromtimestamp(ctx.claims.iat, UTC)`. If one exists, return 401 `tr("this AI app's access was revoked; connect it again to continue")`.

`_unauthorised` sends a 401 with the `WWW-Authenticate` header (Global Constraints) and the JSON `{"detail": …}`.

The protected-resource metadata goes on an `APIRouter` with no prefix:

```python
metadata_router = APIRouter()

@metadata_router.get("/.well-known/oauth-protected-resource")
@metadata_router.get("/.well-known/oauth-protected-resource/mcp")
async def protected_resource(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    resource = mcp_resource(settings)
    if resource is None:
        raise HTTPException(503, tr("MCP is not available: ITS_PUBLIC_CONSOLE_URL is not set."))
    return JSONResponse({"resource": resource, "authorization_servers": [settings.oidc_issuer],
                         "scopes_supported": ["mcp"], "bearer_methods_supported": ["header"]})
```

`tests/test_route_gating.py` requires every `/api/v1` route to be gated, but these paths are outside `/api/v1`. Check `test_public_surface.py`; it may list public paths and need these added deliberately.

`build_mcp(app)`:
- creates `Server("itms", version=<app version>, on_list_tools=…, on_call_tool=…)`; the handlers come in Task 8, and here they return an empty list or a "not implemented" error;
- creates `StreamableHTTPSessionManager(app=server, stateless=True, json_response=True)`;
- creates `McpAsgi(app, sm)`;
- returns them.

Read the installed SDK's `StreamableHTTPSessionManager.__init__` and `run()` to pass the correct arguments. `security_settings` may need `TransportSecuritySettings(enable_dns_rebinding_protection=False)`, since Traefik terminates TLS and Host checks happen there; check what the default does, and document the choice.

In `create_app`:
- `mcp = build_mcp(app)`;
- `app.include_router(mcp.metadata_router)`;
- `app.mount("/mcp", mcp.asgi)`.

In `lifespan`, `async with app.state.mcp.lifespan():` around the existing body; store `app.state.mcp = mcp`. Starlette's `Mount("/mcp")` passes `scope["path"]` with the prefix stripped, so the SDK sees `/` (check `root_path` handling). Test with both `/mcp` and `/mcp/`.

The `api_env` fixture constructs the app via `create_app` and `httpx.ASGITransport`, which does not run lifespan. Add to `tests/mcp_support.py` an async context manager `running_mcp(api_env)` that enters `api_env.app.state.mcp.lifespan()` for the test.

Add Spanish for every new `tr()` sentence to `es.py`.

- [ ] **Step 4: Run** `.venv/bin/pytest tests/test_mcp_server.py tests/test_public_surface.py tests/test_i18n_catalog.py -q`, then `mypy app`.
- [ ] **Step 5: Commit** with the message `feat(mcp): /mcp over Streamable HTTP behind the OAuth gate, with protected-resource metadata`.

---

### Task 8: `tools/list` and `tools/call`: in-process invoke, scrub, rate limit, audit

**Files:**
- Create: `backend/app/mcp/invoke.py`
- Modify: `backend/app/mcp/server.py` (handlers), `backend/app/mcp/__init__.py`
- Modify: `frontend/src/i18n/en/auditVocabulary.ts`, `frontend/src/i18n/es/auditVocabulary.ts` (`mcp_tool_called`). The English must equal the derivation "Mcp tool called" unless it is one of the hand-worded originals; read `auditVocabulary.english.test.ts`. The Spanish is "Herramienta de MCP usada".
- Test: `backend/tests/test_mcp_tools.py`

**Interfaces:**
- Consumes: Task 4 `build_catalog`, Task 6 `secret_key_path`/`REVIEWED_FIELDS`, Task 7 `McpPrincipal`.
- Produces: `invoke(*, app, tool: Tool, args: dict, principal: McpPrincipal) -> InvokeOutcome(status: int, payload: Any | None, error: str | None)`.

- [ ] **Step 1: Write the failing tests.** Use the SDK's own client against the app, where possible over `httpx.ASGITransport`. Find the client API in the installed package: `mcp.client.streamable_http.streamable_http_client` and `mcp.ClientSession`. The client uses `httpx2`. If it cannot take a custom transport, drive the JSON-RPC directly with `httpx` POSTs (`initialize`, `notifications/initialized`, `tools/list`, `tools/call`), and add one SDK-client test that runs against a real uvicorn on a free port, started in-test.

Each case below is one test:
1. **Org-admin listing.** With the switch on, an org-admin's `tools/list` includes `devices_list`. Each tool has `inputSchema`, `annotations.readOnlyHint` and a non-empty `description`. No `credentials` tool appears.
2. **Technician listing.** A technician's `tools/list` omits a tool that only org-admin holds (pick one from `route_permission`, e.g. a `roles` write). Calling it anyway returns an MCP tool error naming `requires roles.manage` (whatever the code is).
3. **Areas filter.** `?areas=devices` lists only tools whose area is `devices`.
4. **Read call.** `tools/call devices_list {}` returns `structuredContent` (or text JSON) equal to the API's own JSON for the same request, and writes `mcp.tool_called` with metadata:
   - `tool="devices_list"`
   - `app=<azp>`
   - `status=200`
   - `args_sha256` equal to the SHA-256 of `json.dumps({}, sort_keys=True, separators=(",", ":"))`
   - no `args` key
5. **Write call.** `tools/call me_update {"body": {"timezone": "UTC"}}` (the actual name from the catalogue) writes the route's own audit event (`user.profile_updated`) with `via: "mcp"` and `mcp_app`.
6. **Scrub.** Monkeypatch a route's response to include `{"api_token": "x"}` on a field not in `REVIEWED_FIELDS`. The call returns an MCP tool error ("blocked: the result contained a secret-looking field at …") and no payload.
7. **Status mapping.** The `tools/call` error text for each status is the API's own `detail`:
   - 404 → "not found: <detail>"
   - 409 → "<detail>"
   - 422 → "invalid arguments: <detail json>"
   - 401 → "re-authenticate"
8. **Rate limit.** Set the limit to 3 via the module constant (monkeypatch). The fourth call within a minute returns an error saying when to retry, and no in-process request is made.
9. **Timeout.** A tool whose route sleeps longer than the timeout (monkeypatch `TOOL_TIMEOUT_S` to 0.05 and a route that awaits) returns a timeout error.
10. **Spanish.** Under `Accept-Language: es`, a 404's error text carries the API's Spanish detail. The principal's `accept_language` is forwarded.

- [ ] **Step 2: Run them and see them fail.**

- [ ] **Step 3: Implement `invoke.py`.**

```python
TOOL_TIMEOUT_S = 30.0
LOOPBACK = "http://mcp-loopback.invalid"

def _marked(app, call: McpCall):
    async def wrapper(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "extensions": {**(scope.get("extensions") or {}),
                                             EXTENSION_KEY: {"azp": call.azp, "tool": call.tool}}}
        await app(scope, receive, send)
    return wrapper

async def invoke(*, app, tool, args, principal) -> InvokeOutcome:
    path_values = {p: quote(str(args[p]), safe="") for p in tool.path_params}
    query = {q: args[q] for q in tool.query_params if args.get(q) is not None}
    url = tool.path.format(**path_values)
    headers = {"Authorization": principal.bearer}
    if principal.accept_language:
        headers["Accept-Language"] = principal.accept_language
    call = McpCall(azp=principal.claims.azp, tool=tool.name)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_marked(app, call)),
                                     base_url=LOOPBACK, timeout=TOOL_TIMEOUT_S) as client:
            response = await client.request(tool.method, url, params=query, headers=headers,
                                            json=args.get("body") if tool.has_body else None)
    except httpx.TimeoutException:
        return InvokeOutcome(504, None, tr("the action took longer than {seconds} seconds", seconds=int(TOOL_TIMEOUT_S)))
    ...
```

Argument validation:
- Validate `args` against `tool.input_schema` with `jsonschema` (a dependency of the SDK). Pin it in pyproject as a direct dependency if you import it.
- On failure, return 422-style error text without calling the route.

Response handling:
- On a 2xx, decode the JSON. A non-JSON 2xx is an error, because files are excluded.
- Run `secret_key_path` on it. If there is a hit and `(f"{tool.method} {tool.path}", normalised_path)` is not in `REVIEWED_FIELDS`, block the result.
- On an error status, use the `detail` from the body.

`server.py` handlers:
- `on_list_tools(ctx, params)`:
  - `principal = ctx.request.scope["state"]["mcp_principal"]`;
  - `held = await user_permission_codes(session, principal.user_id)` in `org_session`;
  - return `ListToolsResult(tools=[types.Tool(name=…, description=…, inputSchema=…, annotations=types.ToolAnnotations(**t.annotations)) for t in catalog.values() if (t.permission in held or t.permission is None) and (principal.areas is None or t.area in principal.areas)])`. Verify the `types.Tool` and `ToolAnnotations` field names in `mcp_types`.
- `on_call_tool(ctx, params)`:
  1. Look up the tool. An unknown tool, or one outside `areas`, is an error.
  2. Rate limit (Redis `_bump` pattern, `MCP_CALLS_PER_MINUTE = 120`).
  3. Call `invoke`.
  4. Audit `mcp.tool_called` in a fresh `org_session` as the user, with metadata `{tool, app, status, args_sha256}`.
  5. Return `CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))], structuredContent=payload if dict else {"items": payload}, isError=False)`, or `isError=True` with the error text.

The audit row is written even when the call errors or is rate-limited, with the status the person saw: 429 for the rate limit, 504 for a timeout.

Build the catalogue once in `build_mcp`, which runs after modules register and before the mount. Store it in `app.state.mcp_catalog`.

- [ ] **Step 4: Run** `.venv/bin/pytest tests/test_mcp_tools.py tests/test_mcp_server.py tests/test_i18n_catalog.py -q`. Also run `cd frontend && npx vitest run src/modules/audit` for the vocabulary guard. Then run `mypy app`.
- [ ] **Step 5: Commit** with the message `feat(mcp): tools/list filtered by the person's permissions, tools/call in-process with scrub, rate limit and audit`.

---

### Task 9: Console admin API: list connections, revoke

**Files:**
- Create: `backend/app/mcp/api.py`
- Modify:
  - `backend/app/main.py` (include the router at `/api/v1`)
  - `backend/app/modules/user_admin/kc_client.py` (`revoke_consent`)
  - `backend/tests/test_console_auth_realm.py` (the pinned operation list gains `revoke_consent`, the documented update path)
  - `docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md` §2.1 and `deploy/README.md` (the "five" → "six" operations rows), and the `kc_client.py` docstring
  - `backend/app/mcp/exclusions.py` (`NOT_FOR_MCP` for the three new routes)
  - `backend/app/api/v1/orgs.py` (`mcp.settings_changed` audit when `mcp_enabled` changes)
  - frontend `auditVocabulary.ts` en/es (`mcp_app_revoked`: "Aplicación de IA revocada"; `mcp_settings_changed`: "Ajustes de MCP cambiados")
- Test: `backend/tests/test_mcp_admin_api.py`

**Interfaces:**
- Produces:
  - `GET /api/v1/mcp/connections` (gated `mcp.use`) returns `McpConnectionsOut`:

    ```
    {"enabled": bool, "address": str | None,
     "apps": [{"client_id", "people": int, "last_used_at": datetime | None,
               "revoked_for_me": bool, "revoked_for_everyone": bool}]}
    ```

    Apps are derived from `mcp.tool_called` audit events in this org, grouped by `metadata->>'app'`.
  - `POST /api/v1/mcp/connections/{client_id}/revoke` with body `{"scope": "me" | "everyone"}`:
    - `"me"` needs `mcp.use`;
    - `"everyone"` additionally needs `orgs.manage`, checked in the handler with `has_permission`, because the route itself is gated on `mcp.use`;
    - inserts `McpRevocation`;
    - calls `kc_client.revoke_consent` for the person (`"me"`) or for every user of the org with an `external_subject` (`"everyone"`), best effort, recording `keycloak_failures` in the response;
    - audits `mcp.app_revoked` with `{client_id, scope}`.
  - `revoke_consent(config, *, user_id: str, client_id: str) -> None`, a `DELETE /admin/realms/{realm}/users/{user_id}/consents/{client_id}`, where 404 is treated as done.

- [ ] **Step 1: Write the failing tests.** Each case below is one test:
1. **Listing.** After two tool calls by two people of the same app, the list shows `people=2`, a `last_used_at` value, and both revocation flags false.
2. **Revoke for me.** It inserts a row with `user_id=me` and calls the fake Keycloak `DELETE …/consents/claude` for my subject only. My next `/mcp` call with an old token returns 401, while the colleague's still works.
3. **Revoke for everyone without `orgs.manage`.** The request returns 403 and nothing is inserted.
4. **Revoke for everyone with `orgs.manage`.** It inserts a row with `user_id NULL` and calls `DELETE` for each org user. Every old token returns 401. Another organization's user with the same `azp` is unaffected.
5. **Keycloak down.** The revocation row is still written, and the response says `keycloak_failures: 1`.
6. **Turning the switch on.** `PATCH /orgs/current {"settings": {"mcp_enabled": true}}` writes `mcp.settings_changed` with `{"mcp_enabled": true}`.
7. **Exclusions.** The partition test from Task 4 still passes, now that the three new routes are in `NOT_FOR_MCP`.

Use the `FakeKeycloak` pattern from `tests/test_me_locale.py` for kc calls.

- [ ] **Step 2: Implement.** Follow the module API style (`require_permission`, `org_session`, Pydantic models). Use `tr()` for every person-visible sentence, with Spanish entries.

**Why "everyone" also removes consent in Keycloak:** a revocation row alone only refuses tokens issued before it. The app's refresh token would mint a new one, and access would come back. Removing the consent invalidates the app's grants for that person until they connect it again. The plan therefore extends spec §3.4 ("Revoke for one person removes … consent") to org-wide revocation, applied per person. This is called out to the operator in the plan hand-off.

- [ ] **Step 3: Run** `.venv/bin/pytest tests/test_mcp_admin_api.py tests/test_console_auth_realm.py tests/test_mcp_catalog.py tests/test_org_settings.py tests/test_i18n_catalog.py -q`, then `cd frontend && npx vitest run src/modules/audit`.
- [ ] **Step 4: Commit** with the message `feat(mcp): console admin API — connections and revoke (Keycloak consent + revocation row)`.

---

### Task 10: Console: Settings → AI connections

**Files:**
- Create: `frontend/src/modules/settings/AiConnectionsTab.tsx`, `frontend/src/modules/settings/__tests__/aiConnections.test.tsx`
- Modify:
  - `frontend/src/modules/settings/tabs.ts`: add `"ai-connections"` to `SETTINGS_TABS`.
  - `frontend/src/modules/settings/SettingsPage.tsx`: the tab, the permission booleans, the render branch, and the header doc table.
  - `frontend/src/modules/settings/api.ts`: `useMcpConnections(enabled)`, `useRevokeMcpConnection()`.
  - `frontend/src/lib/api.ts`: `getMcpConnections()`, `revokeMcpConnection(clientId, scope)`, and the types. Add `mcp_enabled?: boolean` to `OrgSettings`.
  - `frontend/src/modules/settings/fixtures.ts`: `getMcpConnections` and `revokeMcpConnection` fixtures.
  - `frontend/src/modules/settings/__tests__/harness.tsx`: the mock entries.
  - `frontend/src/i18n/en/settings.ts`, `es/settings.ts`: `page.tabs["ai-connections"]` and an `aiConnections` section.
  - `frontend/src/modules/settings/settings.es.test.tsx`: extend it.

**Behaviour** (spec §8):
- **Visibility and switch.**
  - The tab is visible to anyone holding `mcp.use` or `orgs.manage`.
  - The switch "Allow AI apps to connect over MCP" (`mcp_enabled`, via `updateCurrentOrg`) is editable with `orgs.manage`. Without it the switch is disabled, and `permissionReason("orgs.manage")` names the code.
- **Address.** Show the connection address from `getMcpConnections().address`. When it is null, show a note that `ITS_PUBLIC_CONSOLE_URL` is not set.
- **"Connect Claude" guide.** Three numbered steps, in catalogue text:
  1. add a custom connector with this address;
  2. sign in and approve;
  3. set each tool to Always allow or Needs approval.
- **Apps table.** Columns: app (client id), people, last used (`formatDateTime`), and actions.
  - "Revoke for me" needs `mcp.use`.
  - "Revoke for everyone" needs `orgs.manage`, and asks for confirmation through the existing ConfirmDialog.
- **Read-only access.** A person without `mcp.use` sees the tab read-only, with the reason.
- **Conventions.**
  - Follow the existing conventions:
    - `useT()`, no cached translations;
    - errors held as data;
    - `Attempt` for refusals, if any form;
    - `lazyLabels` for tables.
  - Every string goes in both catalogues.
  - English is the source. Spanish uses the glossary at the top of `es/index.ts` (tú register; "aplicación de IA"; "conexión").
  - The JSX coverage guard (`src/i18n/coverage.test.ts`) must pass.

- [ ] **Step 1: Write the failing tests** (`aiConnections.test.tsx`, plus the Spanish test additions). Each case below is one test:
1. **Org-admin.** An org-admin sees the switch on and the address. Toggling the switch calls `updateCurrentOrg({settings: {mcp_enabled: false}})`. The table lists the fixture app, and "Revoke for everyone" opens a confirm dialog, then calls `revokeMcpConnection("claude", "everyone")`.
2. **`mcp.use` only.** A person with `mcp.use` but not `orgs.manage` sees the switch disabled with the reason, and only "Revoke for me".
3. **Neither permission.** A person with neither does not see the tab.
4. **Spanish.** Everything above renders with `expectNoEnglish`, and a live switch re-words it.
- [ ] **Step 2: Implement**, following `AssistantTab.tsx` and `OrganizationTab.tsx` patterns. Read them first.
- [ ] **Step 3: Run** `npx tsc --noEmit && npx vitest run src/modules/settings src/i18n`, then `node scripts/harmony.mjs --strict-fixtures --no-shots --lang es --routes /settings` and the same with `--lang en`.
- [ ] **Step 4: Commit** with the message `feat(mcp): Settings → AI connections (switch, address, apps, revoke)`.

---

### Task 11: Keycloak: the `mcp` client scope, open registration hardened, client policies

**Files:**
- Modify:
  - `deploy/keycloak/init-clients.py`
  - `deploy/keycloak/realm-it-suite.json`
  - `deploy/docker-compose.yml`: give `keycloak-init` the environment variable `ITS_PUBLIC_CONSOLE_URL: ${ITS_PUBLIC_CONSOLE_URL:-}`
  - `backend/tests/test_keycloak_init_job.py`: add tests. Also add the new write verbs to the idempotence test's prefix tuple, which is its documented extension point.
  - `backend/tests/test_console_auth_realm.py`, if it enumerates realm top-level keys (`clientScopes`, `defaultOptionalClientScopes`, components)

**What must converge on every deploy** (spec §3.2–§3.3). Each item is idempotent; a converged realm gets zero writes.

1. **Client scope `mcp`** (OIDC, `include.in.token.scope=true`, `display.on.consent.screen=true`) with two protocol mappers:
   - `mcp-audience`: `oidc-audience-mapper`, `included.custom.audience` = `<ITS_PUBLIC_CONSOLE_URL>/mcp`, `access.token.claim=true`, `id.token.claim=false`.
   - `org-claim`: `oidc-usermodel-attribute-mapper`, the same config as `it-suite-web`'s.

   If `ITS_PUBLIC_CONSOLE_URL` is unset, the scope is created without the audience mapper and the job prints that MCP needs it. Do not fail the job.
2. **Realm optional client scope.** `mcp` is in the realm's default **optional** client scopes, so a registered client can request it.
3. **Anonymous client-registration policies** (components, `providerType` `org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy`, `subType` `anonymous`):
   - **remove** `trusted-hosts`;
   - **ensure** `consent-required`;
   - `allowed-client-templates`: config `allowed-client-scopes: ["mcp"]`, `allow-default-scopes: ["true"]`;
   - `allowed-protocol-mappers`: keep the stock list, with **no** additions;
   - `scope` (Full Scope Disabled): present;
   - `max-clients`: `max-clients: ["200"]`.

   Verify each provider id against Keycloak 26.2. Read the `keycloak-services` sources jar the way Task 20 of the Spanish work did: download `org.keycloak:keycloak-services:26.2.x:sources` from Maven Central into the scratchpad. Record the ids you confirmed in a comment.
4. **Client policies.** A client profile `itms-mcp-apps` with executors:
   - `pkce-enforcer` (auto-configure true);
   - `reject-implicit-grant`;
   - the resource-owner-password-credentials rejection executor;
   - `secure-redirect-uris-enforcer` (or the 26.2 name for the redirect-URI executor).

   A client policy `itms-mcp-apps` applies it on condition `client-updater-context` with `update-client-source: ["ByAnonymous"]`. Verify every executor and condition id in the 26.2 sources. Any that does not exist is reported to the controller, not silently dropped.

**Implementation shape** (follow the file's style exactly):
- **Pure decision functions:**
  - `desired_mcp_scope(public_url) -> dict`
  - `registration_policy_changes(existing_components, desired) -> (to_create, to_update, to_delete)`
  - `client_policy_changes(existing_profiles, existing_policies) -> (...)`
- **New methods** on the `RealmAdmin` Protocol, `FakeRealm` and `HttpRealmAdmin`. These are the endpoints of the admin API:
  - `list_client_scopes()`, `create_client_scope(rep)`, `list_scope_mappers(scope_id)`, `create_scope_mapper(scope_id, rep)`, `update_scope_mapper(scope_id, mapper_id, rep)`
  - `get_default_optional_scopes()`, `add_default_optional_scope(scope_id)`
  - `list_components(provider_type)`, `create_component(rep)`, `update_component(id, rep)`, `delete_component(id)`
  - `get_client_profiles()`, `put_client_profiles(rep)`, `get_client_policies()`, `put_client_policies(rep)`
- **`converge_mcp(admin, public_url) -> list[str]`** returns what changed. It is called from `run_init` after `converge_realm` and before the rotation.
- **`main()`** prints `identity init: mcp converged=[…] or 'nothing'`.
- **Realm file.** The fresh-install realm file declares the same:
  - `clientScopes` (only the `mcp` scope; Keycloak adds the built-ins on import);
  - `defaultOptionalClientScopes`, including `mcp`, while keeping the built-in optional ones (check what an import without the key yields);
  - the registration components;
  - `clientProfiles` / `clientPolicies`.

  Make convergence and import agree, and add a test asserting that.

- [ ] **Step 1: Write the failing tests** (`test_keycloak_init_job.py`, `FakeRealm`):
1. **Fresh realm.** A fresh realm converges the scope, both mappers, the optional scope, the registration policies (trusted-hosts deleted) and the client profile/policy.
2. **No-op re-run.** A second run gives zero writes, with the new verbs added to the idempotence test.
3. **Missing console URL.** With an empty `ITS_PUBLIC_CONSOLE_URL`, the scope has no audience mapper and the output says so.
4. **Wrong audience.** A pre-existing `mcp` scope with the wrong audience is corrected.
5. **Ordering.** Convergence happens after the clients exist and before the rotation, following the existing ordering test's pattern.
- [ ] **Step 2: Implement.** The HTTP admin class stays `# pragma: no cover`; CI's live step (Task 12) covers it.
- [ ] **Step 3: Run** `.venv/bin/pytest tests/test_keycloak_init_job.py tests/test_console_auth_realm.py -q`.
- [ ] **Step 4: Commit** with the message `feat(mcp): keycloak-init converges the mcp scope, hardened open registration and client policies`.

---

### Task 12: Traefik, live Keycloak in CI, deploy docs

**Files:**
- Modify:
  - `deploy/docker-compose.yml`: the api router rule gains `` || PathPrefix(`/mcp`) || PathPrefix(`/.well-known/oauth-protected-resource`) ``.
  - `.github/workflows/ci.yml`: the `deploy` job's Keycloak step, which must keep its existing assertions.
  - `deploy/README.md`: a new section, "AI connections (MCP)".
- Create: `deploy/keycloak/ci_mcp_flow.py` (stdlib-only; run by CI with the runner's `python3`)
- Test: `backend/tests/test_deploy_install_link.py`-style test asserting the two new prefixes, in a new `backend/tests/test_deploy_mcp_route.py`. Also check that `frontend/scripts/routes.mjs` has no `/mcp` route, as the install-link test does for `/i/`.

**CI step** (append to the existing Keycloak step, or add a step after it in the same job, before teardown):
1. Run keycloak-init against the live Keycloak, with `ITS_PUBLIC_CONSOLE_URL=https://itms.example.test`. Use the compose `keycloak-init` service as deploy does; it needs the backend image, so read how the deploy job builds images. Alternatively, run `python3 deploy/keycloak/init-clients.py` with the env pointed at `http://localhost:8080`, if the job exposes Keycloak's port. Read the job and pick the one that works, then document why.
2. Run `python3 deploy/keycloak/ci_mcp_flow.py --base http://localhost:8080 --realm it-suite --resource https://itms.example.test/mcp`. Using only `urllib` and `http.cookiejar`, it:
   - creates a test user through the admin API, with the `org` attribute set, a password and no required actions;
   - registers a client anonymously at `/realms/it-suite/clients-registrations/openid-connect` with `redirect_uris: ["http://127.0.0.1:33418/callback"]`, `grant_types: ["authorization_code","refresh_token"]`, `token_endpoint_auth_method: "none"` and `scope: "mcp"`, and asserts 201;
   - registers a second client asking for scope `it-suite` (or any non-allowed scope) and asserts that registration is refused, or that the scope is stripped. Record which happens; the assertion is that the resulting token never carries the `it-suite` audience;
   - runs authorization-code + PKCE (S256) for scope `openid mcp`:
     - GET the auth URL;
     - parse the login form `action`;
     - POST the username and password;
     - if the consent page appears, POST its form with `accept`;
     - capture `code` from the `Location` redirect without following it;
     - POST to the token endpoint;
   - decodes the access token payload (base64; no verification needed in CI) and asserts:
     - `aud` includes the resource and not `it-suite`;
     - `org` is present;
     - `azp` is the registered client id;
   - asserts that a token request without `code_verifier` is refused.
3. Exit non-zero with a clear message on any failed assertion.

**deploy/README.md "AI connections (MCP)"** covers:
- what it is, and that it is off by default;
- the operator steps from spec §1;
- the Traefik paths;
- the fact that `ITS_PUBLIC_CONSOLE_URL` is required;
- what keycloak-init converges and how to check it (`docker compose logs keycloak-init | grep 'mcp converged'`);
- how to revoke;
- what is never reachable;
- a pointer to CLAUDE.md 1c.

- [ ] **Steps:** write the route test and see it fail; change the compose rule; see it pass. Write `ci_mcp_flow.py`, and run it locally against a throwaway Keycloak container if Docker is available; otherwise note that CI is its first run. Edit `ci.yml`, validate the YAML, and commit with the message `ci(mcp): live Keycloak registration + PKCE flow asserts the MCP audience; Traefik routes /mcp`.

---

### Task 13: Invariant 1c, spec pointers, final verification

**Files:**
- Modify:
  - `CLAUDE.md`: insert spec §9's 1c text **verbatim** after the "1b is not a precedent…" paragraph that ends 2a (currently line 251) and before the `---` (line 253).
  - `docs/modules/agent-distribution/spec-self-update.md`: after §8.5 (after line 605).
  - `docs/modules/agent-distribution/spec-push-install.md`: after §2.3 (after line 431).
  - `docs/superpowers/specs/2026-09-28-install-link-and-windows-push-design.md`: §3, after line 339.
  - `docs/modules/remote-control/spec-screen-share.md`: §3.10, after line 428.
  - `docs/modules/mdm/spec.md`: §1.6 item 6, after line 226.

  Each spec gets one line: `> **Amended by CLAUDE.md invariant 1c (2026-10-09):** an AI connected over the platform's MCP server acts as the person who connected it and may reach this act within that person's RBAC; the reservation above now governs automated and in-app-AI actors only.` For `mdm/spec.md`, adapt it to say 1c governs MDM lock/erase when MDM is built. The approved-actions refusals are unchanged; say so in the self-update and push-install pointers.

- [ ] **Step 1:** Apply the CLAUDE.md insertion and the five pointers. Diff CLAUDE.md to confirm that only the 1c block was added and that no other byte changed.
- [ ] **Step 2: Full verification.**
  - Backend: `ruff check app tests`, `mypy app`, the full `pytest -q`, run in the background for about an hour, and the migration round trip.
  - Frontend: `npx tsc --noEmit`, `npx vitest run`, `npm run build`, and `node scripts/harmony.mjs --strict-fixtures --no-shots --lang en` and `--lang es`.
- [ ] **Step 3: Commit** with the message `docs(mcp): invariant 1c in CLAUDE.md and pointers in the specs that reserved acts to a person`.

---

## Decisions this plan makes beyond the spec (to confirm with the operator at hand-off)

1. **"Revoke for everyone" also removes Keycloak consent for each person in the organization** (spec §3.4 says this only for per-person revoke). Without it, the app's refresh token mints a new access token after `revoked_at`, and access returns at the next refresh. This is per person, so it never touches another organization.
2. **The AI connections table names an app by its registered client id, not its display name.** Reading the client's registered name needs Keycloak's `view-clients` role, which the provisioning client does not hold, and granting it widens a credential that already holds `manage-users`. The consent screen still shows the name to the person. The alternative is to grant `view-clients`.
3. **Tool descriptions are English only.** They are read by the AI client, not shown to a person in the console.
4. **SDK pinned to `mcp==2.3.0`.** The 2.x API (`Server(on_list_tools=…, on_call_tool=…)`, `StreamableHTTPSessionManager(stateless=True, json_response=True)`) is what the plan targets.

## Self-review notes

- **Spec coverage, section by section:**

  | Spec | Covered by |
  |---|---|
  | §1 | T10, T12 docs |
  | §2 | T1, T7, T12 |
  | §3.1 | T7, T12 |
  | §3.2 | T1, T3, T11 |
  | §3.3 | T11, T12 |
  | §3.4 | T2, T7, T9 |
  | §4.1 | T4, T5 |
  | §4.2 | T8 (list filter); T4 (open routes, areas); T7 (areas parse) |
  | §4.3 | T8 |
  | §5 | T3 (API refusal); T4 (no tool); T6 (scrub) |
  | §6 | T2, T7 |
  | §7 | T3 (via), T8 (`mcp.tool_called`, rate limit), T9 (revoked/settings) |
  | §8 | T9, T10 |
  | §9 | T13 |
  | §10 | tests across T3, T4, T6, T7, T8, T9, T11, T12 |
  | §11 | T2 migration; T1 image; T11 keycloak-init; T12 Traefik |
  | §12 | nothing to build |

- **Interfaces:** `McpCall`, `mcp_call_from_scope`, `mcp_resource`, `Tool`, `build_catalog`, `McpPrincipal` and `invoke` are each defined once and used with the same names.
- **Values left to the implementer, each with a rule:**
  - exact SDK type and field names (verify in the installed `mcp`/`mcp_types`);
  - Keycloak 26.2 provider ids (verify in the sources jar);
  - the exclusion tables (driven by the partition test, each with a true reason).
