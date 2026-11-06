# Autenticación propia — Fase 2: el portal de contactos y «Entrar con Microsoft». Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Los contactos de cada cliente entran al portal con la autenticación propia (clave de acceso, o contraseña de 15+ caracteres con segundo factor opcional), técnicos y contactos pueden entrar con su cuenta de Microsoft (Entra ID) ya vinculada, y se cierran los pendientes A-M2 (el respaldo `local` de claves rechazado en producción) y A-M6 (retención de sesiones, equipos recordados y enlaces de un solo uso).

**Architecture:** Se construye sobre el núcleo de la fase 1 (`backend/app/core/auth/`), sin forkearlo: la misma ruta `/auth/login`, los mismos factores, sesiones y avisos, ahora conscientes del tipo de persona (`users.kind`). Una migración, 0246, añade la vinculación con Microsoft (`tid` + `oid`), los ajustes de la app de Entra y los tenants permitidos por cliente. «Entrar con Microsoft» es un código de autorización con PKCE, `state` y `nonce`, cuyo verificador PKCE es una cookie `__Host-` que ata la transacción al navegador; el ID token se valida aquí contra las claves públicas de Microsoft y nunca se enlaza ni se crea una cuenta por el correo.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, PostgreSQL 16 (RLS), OpenBao (transit, por la bóveda), Redis, httpx, PyJWT 2.15 (`RS256` para el ID token de Microsoft), py_webauthn 3.0.1, Celery (worker), pytest. React + TypeScript estricto, Vite, TanStack Query, @simplewebauthn/browser 14.0.0, vitest. **Ninguna dependencia nueva.**

**Diseño:** `docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` (aprobado el 2026-11-04): §3, §4 y §8 fase 2; §2.6, §6.1, §6.2, §6.4, §7 y §10 donde la fase 2 los toca. Fuentes: `docs/superpowers/specs/2026-11-04-autenticacion-propia-fuentes.md` ([S1]–[S65]) y las páginas leídas para este plan el 2026-11-06, citadas con su URL en la tarea que las usa (Apéndice C). Alcance fijado por el operador el 2026-11-06 («Fases 2 y 3 + pendientes pequeños»): §3, §4, A-M2 y A-M6; **no** el buzón de salida de correo. La fase 3 (§5, servidor OAuth para MCP) se planifica en paralelo: ver «Puntos de encuentro con la fase 3».

## Global Constraints

- **Seguridad.** Invariantes 1, 1b, 1c, 2, 2a, 3, 4, 5, 5a y 6 de `CLAUDE.md` intactas.
  - Ninguna tarea cambia trabajos firmados, el sobre ni el protocolo del agente.
  - La consola no protege nada: toda comprobación vive en el servidor.
  - Ningún secreto (contraseña, código, token de un solo uso, identificador de sesión o de equipo, código de autorización de Microsoft, verificador PKCE, ID token, secreto de la app de Entra) aparece en logs, respuestas (salvo el valor recién creado que la persona debe ver una vez), auditoría ni en la base de datos en claro. El secreto de la app de Entra vive en la bóveda y se descifra solo en el canje del código (invariante 3), auditado (invariante 4).
  - Un campo que lleva un secreto en un cuerpo de petición es un `str` sin límites de Pydantic (decisión 16 de la fase 1): un 422 repetiría su entrada. Lo acota la ruta o el flujo.
  - **5a para los contactos:** toda fila de credenciales de un contacto lleva el `client_id` de su cliente (trigger `its_client_from_parent`, 0231); un técnico limitado a otros clientes no la ve ni la toca. Las escrituras que hace un técnico sobre un contacto (invitar, cancelar, restablecer el acceso, desactivar) corren con el alcance de la petición (`ctx.clients`), nunca con `ClientScope.all()`.
- **Migración.** Una sola, **0246** (`backend/migrations/versions/0246_auth_contacts_microsoft.py`, revision `"0246"`, down_revision `"0245"`), hacia adelante; `downgrade()` funcional (la CI corre `upgrade head → downgrade base → upgrade head`). 0245 no se toca. **La fase 3 encadena 0247 con down_revision `"0246"`.**
- **Alcance por cliente.** Toda tabla nueva tiene `org_id` + RLS (ENABLE + FORCE + `tenant_isolation`) y entra en `client_scope_catalog.CATALOG`: `auth_external_identities` como `O("users","user_id")` (el patrón `_person_scope` de 0245), `auth_entra_settings` como `G`. Las lecturas antes de saber la organización van SOLO por funciones `SECURITY DEFINER`: las dos de 0245 y `its_auth_org` (Task 1). Toda función nueva con `ClientScope.all()` entra en `ALL_SCOPE_ALLOWLIST` con su razón; ningún archivo de rutas la usa.
- **Parámetros (diseño §3, §4; decisiones de este plan):**
  - contraseña de contacto: 15–128 puntos de código, la misma lista de bloqueo y palabras de contexto que los técnicos (`passwords.CONTACT_MINIMUM = 15`); técnicos, 12 (sin cambio);
  - invitación de contacto: 72 h (`action_tokens.INVITE_TTL`); restablecimiento: 30 min (`RESET_TTL`); sesiones de contacto: `contact_idle_minutes` (60) y `contact_max_days` (7, hasta 30) de `auth_org_settings` (ya existen);
  - transacción de Microsoft: 10 min (`ENTRA_TTL_S = 600`), de un solo uso;
  - retención: 30 días tras terminar o caducar (`retention.RETENTION`); desafíos WebAuthn, 1 día; una invitación sin usar nunca se barre (Pregunta P4).
- **Cookies.** Las dos de la fase 1 sin cambios. Nueva: **`__Host-itms_entra`** — `HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=600`; su valor (43 caracteres base64url, `secrets.token_urlsafe(32)`) **es el `code_verifier` de PKCE**; el servidor guarda solo su `S256`. Se borra al completar.
- **CSRF.** Sin cambios: toda ruta nueva bajo `/api/v1/auth/` exige `X-ITMS-Request: 1` y el `Origin` de la consola aunque no lleve cookie; toda escritura con la cookie de sesión, también.
- **Microsoft (Entra ID)** (fuentes del Apéndice C):
  - host fijo `https://login.microsoftonline.com`; autoridad `organizations` o el GUID de un tenant, nunca otra cosa;
  - `response_type=code`, `response_mode=fragment`, `scope=openid profile`, `code_challenge_method=S256`, `state` y `nonce` aleatorios de 256 bits, `prompt=select_account`; `redirect_uri` = `ITS_PUBLIC_CONSOLE_URL` + `/sign-in/microsoft` (nunca de la cabecera `Host`);
  - canje en `/{autoridad}/oauth2/v2.0/token` con `client_secret_post` y `code_verifier`;
  - ID token: `alg` fijado a `RS256`, `kid` en las claves de `https://login.microsoftonline.com/organizations/discovery/v2.0/keys`, `aud` = el id de la app, `iss` = `https://login.microsoftonline.com/{tid}/v2.0`, `ver` = `2.0`, `nonce` igual al emitido, `exp`/`nbf`/`iat` con 60 s de margen, `tid` distinto del tenant de cuentas personales `9188040d-6c67-4c5b-b112-36a304b66dad`;
  - la persona es `(tid, oid)` de una fila de `auth_external_identities`; **nunca se crea una cuenta y nunca se enlaza por correo** (falsificador 8); un técnico solo desde el tenant de Novieri (`auth_entra_settings.staff_tenant_id`); un contacto solo desde un tenant de `clients.microsoft_tenants` de su cliente.
- **Negativas de `/auth/*` y `/me/*`:** el cuerpo `AuthRefusal` de la fase 1 (`{"code", "detail", …}`). Códigos nuevos: `portal_only` (403, la puerta de la consola ante un contacto), `last_method` (409), `password_exists` (409), `passkey_rejected` (401 al entrar), `microsoft_unavailable` (409/503), `microsoft_failed` (400), `microsoft_not_linked` (401), `microsoft_tenant_refused` (403), `microsoft_already_linked` (409).
- **Textos.** Backend por `tr()` con `backend/app/core/i18n/es.py` (cada literal nuevo con su entrada; `tests/test_i18n_catalog.py`); consola por `t()` con claves en `frontend/src/i18n/{en,es}/`. *Passkey* = «clave de acceso» (D18 de la fase 1). La marca se escribe «Microsoft»; la pantalla de Ajustes dice «Microsoft Entra ID» una vez, para quien registra la app.
- **Auditoría.** Acciones nuevas: `contact.invited`, `contact.invite_cancelled`, `contact.access_reset`, `contact.invite_accepted`, `auth.entra_settings_updated`, `auth.entra_secret_used`. Se reutilizan `auth.identity_linked` y `auth.identity_unlinked` (ya en el vocabulario) con `metadata.provider = "entra"`. Cada una va a `frontend/src/i18n/{en,es}/auditVocabulary.ts` (el inglés = el código humanizado); un literal pasado a un envoltorio (`flows._audit`, `account._record`) se lista en `DYNAMIC` de `auditVocabulary.coverage.test.ts`. Si una tarea toca `backend/app`, corre también `cd frontend && npx vitest run src/modules/audit/auditVocabulary.coverage.test.ts src/i18n`.
- **MCP.** Sigue en Keycloak en esta fase (la fase 3 lo sustituye). Toda ruta HTTP nueva queda clasificada en el mismo commit que la crea: descripción MCP (`app/mcp/descriptions/part_*.py`), `NOT_FOR_MCP` o `NEVER_EXPOSED_REASONS` (`app/mcp/exclusions.py`), y `tests/test_mcp_channel_api.py` lleva el conjunto y el recuento.
- **Pruebas.** Ninguna se borra, se salta ni se debilita. Las que cambian de expectativa por una decisión de este plan se re-apuntan y el informe de la tarea lo dice, con la decisión que lo manda.
- **Recuentos que mueven varias tareas** (cada tarea cambia el número **respecto de la base que ve** y dice su delta; el coordinador suma al fusionar y corre la prueba en el árbol fusionado):
  - llamadas de log en `tests/test_logging_discipline.py`: **108** hoy; +1 Task 5 (`entra.exchange`) → **109**;
  - `NEVER_EXPOSED` en `tests/test_mcp_channel_api.py`: **36** hoy; +1 Task 4, +2 Task 6, +1 Task 7, +4 Task 8, +2 Task 9 → **46**;
  - `EXPECTED_GET_ROUTES` en `tests/test_ai_tool_surface.py`: **255** hoy; +1 Task 4 (`/portal/me`), +1 Task 5 (`/settings/microsoft-sign-in`) → **257**;
  - `GLOBAL_WRITES` en `tests/test_global_writes.py`: +1 Task 5;
  - `UNGATED_BY_DESIGN` en `tests/test_route_gating.py`: una entrada por ruta nueva de `/auth/*` o `/me/*` (Tasks 4, 6, 7, 8, 9);
  - `REVIEWED_NON_LITERAL_ACTIONS` y `DYNAMIC` de `auditVocabulary.coverage.test.ts` (Tasks 4, 7, 8, 9).
- **Archivos generados.** `frontend/src/lib/globalRoutes.json` no se edita a mano: se regenera en el árbol fusionado con `cd backend && .venv/bin/python -m tests.export_console_global_routes` (`tests/test_console_global_routes.py` lo compara byte a byte). Lo regeneran la Task 5 (un `GLOBAL_WRITES` nuevo) y la Task 13.
- **Dependencias.** Ninguna nueva (Python ni npm): `httpx`, `pyjwt[crypto]` 2.15.1 (`RS256`, `jwt.algorithms.RSAAlgorithm`), `webauthn` 3.0.1 y `@simplewebauthn/browser` 14.0.0 ya están.
- **Verificación, desde `backend/`:** `.venv/bin/pytest -q <archivos de la tarea>`; `.venv/bin/mypy app`; `.venv/bin/ruff check app tests`; la suite completa la corre el coordinador en el árbol fusionado. **Desde `frontend/`:** `npx vitest run` completo, `npx tsc -b`, `npm run build`.
- **Pistas en paralelo (worktrees)**, cuando el coordinador lo indique:
  - La worktree nace de `origin/main`: lo primero, `git merge --ff-only <rama de integración de la fase 2>` (el coordinador la nombra; parte de `claude/compassionate-bardeen-vgtwus` en `0182206` o posterior) y `git log --oneline -1`.
  - `.venv` y `node_modules` se enlazan desde el árbol principal: `ln -s /home/user/Novieri-RMM/backend/.venv backend/.venv` y `ln -s /home/user/Novieri-RMM/frontend/node_modules frontend/node_modules`.
  - El entorno es editable y apunta al árbol principal: `cd backend && export PYTHONPATH=$PWD`, y comprobar que `.venv/bin/python -c "import app; print(app.__file__)"` imprime una ruta de la worktree. `PYTHONPATH` va en cada orden que corre Python.
  - `export ITS_TEST_REDIS_URL=redis://localhost:6379/<n>` con el número de base que asigne el coordinador (0–15).
  - `pg_ctlcluster 16 main start 2>/dev/null; redis-cli ping >/dev/null 2>&1 || redis-server --daemonize yes`.
  - Se hace commit en la rama de la worktree; no se hace push ni se toca el árbol principal.

## Oleadas y paralelismo

| Oleada | Tareas | En paralelo | Depende de |
|---|---|---|---|
| 1 | 1, 2, 3 | sí | — (la 2 y la 3 no tocan 0246) |
| 2 | 4, 5 | sí | 1 (la 4 usa `org_of_slug` y `AuthExternalIdentity`; la 5, `AuthEntraSettings` y `Client.microsoft_tenants`) |
| 3 | 6, 7 | sí | 4 (`signed_user` por tipo, `flows.started`, `methods.py`, `/auth/step-up/password`) |
| 4 | 8, 10 | sí | 5, 6, 7 (la 8 registra claves de acceso de contactos y lee los ajustes de Microsoft); la 10 solo los contratos de las 4 y 6 |
| 5 | 9, 11 | sí | 5, 8 (la 9 reutiliza `flows.invitee`, `portal_access.grant_portal`, `action_tokens.consume_digest`); la 11, de la 7, 8 y 10 |
| 6 | 12 | — | 9, 11 (toca `InvitePage`, `SecurityPage` y `lib/api.ts` después de la 11) |
| 7 | 13 | — | todas |

## Tareas

| Task | Qué | Diseño |
|---|---|---|
| 1 | Migración 0246: identidades externas, ajustes de Entra, tenants por cliente, `its_auth_org`; modelos y catálogo | §4, §6.1, §6.2 |
| 2 | A-M2: el respaldo `local` de claves, rechazado en producción con o sin archivos | §6.3; revisión final A-M2 |
| 3 | A-M6: retención de sesiones, equipos recordados, enlaces y desafíos (worker) | revisión final A-M6 |
| 4 | Contactos con contraseña: `/auth/login` para los dos tipos, factores y restablecimiento por tipo, confirmación con contraseña, `portal_only`, `GET /portal/me` | §3, §2.3–§2.8 |
| 5 | Microsoft, el protocolo: `entra.py` (URL, canje, claves, ID token), Ajustes → Seguridad → Microsoft (secreto en la bóveda), tenants por cliente | §4 |
| 6 | Entrar con una clave de acceso sin dirección (contactos); claves de contacto descubribles | §3, §2.2 |
| 7 | Mi seguridad para contactos: `/me/*` por la puerta del portal, reglas de métodos, añadir la primera contraseña | §3, §2.4–§2.6 |
| 8 | Ciclo de vida del contacto: invitar, reenviar, cancelar, restablecer el acceso; aceptar con contraseña o clave; el rol de portal; desactivar corta todo | §3, §7 |
| 9 | Microsoft, los flujos: entrar, vincular desde el perfil, aceptar una invitación; desvincular; falsificador 8 | §4, §10.8 |
| 10 | Consola: el portal para contactos (`portal_only`, `PortalShell`, `GET /portal/me`), `/portal/sign-in` con contraseña y clave de acceso | §3, §1.1 |
| 11 | Consola: Contactos (invitar, cancelar, restablecer), aceptar la invitación como contacto, Mi seguridad del contacto, confirmación con contraseña | §3, §7 |
| 12 | Consola: «Entrar con Microsoft» (consola, portal, invitación), la vuelta `/sign-in/microsoft`, vincular y desvincular, Ajustes → Microsoft, tenants del cliente | §4 |
| 13 | Cierre: falsificadores 1 y 8 de punta a punta, documentos, `globalRoutes.json`, recuentos, despliegue | §9, §10 |

## Notas de integración del coordinador

Las tareas se redactaron contra el código de `0182206`. Donde una tarea y estas notas difieran, mandan estas notas.

- **Oleada 1.** Las Tasks 1, 2 y 3 no comparten archivos salvo `backend/app/core/client_scope.py` (Task 3 añade una entrada a `ALL_SCOPE_ALLOWLIST`; Task 1 no lo toca) y `deploy/README.md` (solo la 2). Orden de fusión: 1, 2, 3.
- **Oleada 2, choques esperados** (orden de fusión: 4, 5): `backend/app/core/i18n/es.py`, `backend/app/mcp/exclusions.py`, `backend/app/mcp/descriptions/part_d.py`, `backend/tests/test_mcp_channel_api.py`, `backend/tests/test_ai_tool_surface.py` (unión; recuentos sumados: `EXPECTED_GET_ROUTES` 255 → 257, Global Constraints).
- **Oleada 3:** las Tasks 6 y 7 tocan las dos `backend/app/api/v1/auth.py` (rutas distintas: la 6 añade `/auth/passkey*`; la 7 cambia la dependencia de las rutas `/me/*` y añade `POST /me/password`). Solo la 6 toca `backend/app/core/auth/flows.py` y `passkeys.py`; solo la 7, `account.py` y `deps.py`. Las listas de las pruebas de MCP, de rutas y de `es.py`: unión; `NEVER_EXPOSED` suma las dos (+2 y +1).
- **Oleada 4:** la Task 8 y la 10 no comparten archivos (backend / consola).
- **Oleada 5:** la Task 9 y la 11 no comparten archivos (backend / consola).
- **Firmas que una tarea fija y otras consumen** (el detalle está en la tarea que las crea; resumen en el Apéndice A):
  - `lookup.org_of_slug` (Task 1); `lookup.find_person`, `PreIdentity.kind` (Task 4).
  - `flows.started` (antes `_started`), `flows.after_first_factor` (antes `_after_password`, con `method`), `methods.has_sign_in_method`, `methods.has_password` (Task 4).
  - `staff_password.minimum_for(kind)` y `require_acceptable(..., minimum=...)` (Task 4).
  - `passkeys.registration_options(..., discoverable=...)` (Task 6).
  - `deps.bound_account_context` (Task 7).
  - `action_tokens.digest`, `action_tokens.peek_digest`, `action_tokens.consume_digest`, `flows.invitee`, `flows.Invitation`, `portal_access.grant_portal` (Task 8).
  - `entra.*`, `entra_settings.*` (Task 5); `entra_flows.*` (Task 9).
- **`/me/security`** gana `kind`, `has_password` y `microsoft` en la Task 7; la Task 9 no cambia su forma, solo añade la vinculación y la desvinculación.
- **Para el informe de despliegue** (cuando acaben los cuatro proyectos): migración 0246 (la corre `migrate`); **ninguna clave nueva en `.env`**; ninguna ruta nueva de OpenBao (el secreto de Entra va a la bóveda, D33); registrar la app de Entra (Task 13, `deploy/README.md`), con la dirección de vuelta `https://<consola>/sign-in/microsoft`, tipo **Web**; el api ya sale a `login.microsoftonline.com` (Email Intake). La fase 3 puede ampliar esta lista.

## Apéndice A — Interfaces compartidas

Es el contrato con el que se redactaron las tareas. **Donde el bloque «Interfaces» de una tarea dice otra firma, manda la tarea.**

### Task 1 — 0246, modelos, `its_auth_org`

- `auth_external_identities(id UUID PK, org_id, client_id (trigger), user_id UUID NOT NULL FK users CASCADE, provider TEXT CHECK IN ('entra'), tenant_id UUID, subject UUID, created_at, last_used_at NULL)`, `UNIQUE(org_id, provider, tenant_id, subject)`, `UNIQUE(user_id, provider)` → `AuthExternalIdentity` (O `users`/`user_id`).
- `auth_entra_settings(org_id PK, enabled BOOL DEFAULT false, app_client_id UUID NULL, credential_id UUID NULL FK credentials SET NULL, staff_tenant_id UUID NULL, updated_at, updated_by NULL)`, `CHECK (NOT enabled OR (app_client_id IS NOT NULL AND credential_id IS NOT NULL))` → `AuthEntraSettings` (G).
- `clients.microsoft_tenants UUID[] NOT NULL DEFAULT '{}'`, `CHECK (cardinality(microsoft_tenants) <= 20)` → `Client.microsoft_tenants: list[UUID]`.
- `its_auth_org(p_org_slug text) RETURNS uuid` (`SECURITY DEFINER`, `search_path = public, pg_temp`, solo `app_user`).
- `app/core/auth/lookup.py`: `async def org_of_slug(engine: AsyncEngine, slug: str) -> UUID | None`.
- `app/models/auth.py`: `IDENTITY_PROVIDERS = ("entra",)`, `ENTRA = "entra"`.

### Task 2 — A-M2

- `app/core/auth/keys.py`: `class LocalAuthKeysInProduction(RuntimeError)`; `build(settings)` la lanza con `auth_keys_backend == "local"` y `environment == "production"`, antes de tocar un archivo.

### Task 3 — retención

- `app/core/auth/retention.py`: `RETENTION = timedelta(days=30)`, `CHALLENGE_RETENTION = timedelta(days=1)`, `SWEEP_INTERVAL_S = 86400.0`; `@dataclass(frozen=True) class Swept(sessions, devices, links, challenges)` con `.total`; `async def sweep(session, *, now) -> Swept`; `async def sweep_org(org_id, engine, *, now=None) -> int`.
- `app/worker.py`: `async def auth_retention_all_orgs(now: datetime | None = None) -> int`; tarea `app.worker.auth_retention`; entrada de beat `auth-retention`.

### Task 4 — contactos con contraseña

- `lookup.PreIdentity(user_id, org_id, status, kind="staff")`; `async def find_person(engine, *, org_slug, email) -> PreIdentity | None` (técnico primero, después contacto).
- `guards.signed_user(session, principal)`: la persona de la sesión, activa y **del tipo de la sesión** (técnico o contacto).
- `passwords.CONTACT_MINIMUM = 15`; `staff_password.minimum_for(kind: str) -> int`; `staff_password.require_acceptable(plain, *, email, display_name, org_name, minimum: int = STAFF_MINIMUM)`.
- `app/core/auth/methods.py`: `async def has_password(session, user_id) -> bool`; `async def has_sign_in_method(session, user_id) -> bool` (contraseña, clave de acceso o identidad de Microsoft).
- `flows.started(session, request, user, *, state, factor_done=False, device=None, resume=None, methods=(), remember_device_days=None) -> SignIn` (renombra `_started`; la sesión es del tipo de la persona); `flows.after_first_factor(session, request, user, *, method: str = "password") -> SignIn` (renombra `_after_password`; un contacto sin segundo factor queda `signed_in`).
- `flows.step_up_password(request, principal, *, password) -> None`; ruta `POST /auth/step-up/password {password}` → 204 (contactos; un técnico, 409 `method_unavailable`).
- `notices.reset_mail(link, *, second_factor: bool) -> tuple[str, str]`.
- `refusals.PORTAL_ONLY = "portal_only"`; `deps._admit_session` responde a un contacto en la consola con `AuthRefusal(403, code="portal_only")`.
- `GET /api/v1/portal/me` → `PortalMeOut{user_id, kind, email, display_name, timezone: str | None, permissions: list[str]}`; `principal.portal_permissions(request, ctx) -> list[str]`; `SessionPrincipal.timezone: str | None = None` (leído en `sessions.resolve`).

### Task 5 — Microsoft, el protocolo

- `app/core/auth/entra.py`: `MICROSOFT_HOST`, `ORGANIZATIONS`, `CONSUMER_TENANT`, `SCOPE = "openid profile"`, `CALLBACK_PATH = "/sign-in/microsoft"`, `JWKS_URL`; `class EntraNotConfigured(Exception)`; `class EntraRefused(Exception)` con `.reason: Literal["exchange","token","signature","claims","nonce","tenant"]`; `@dataclass(frozen=True) class EntraIdentity(tenant_id: UUID, object_id: UUID, name: str)`; `def authority_for(tenant_id: UUID | None) -> str`; `def redirect_uri(settings) -> str`; `def challenge_of(verifier: str) -> str`; `def authorize_url(*, authority, client_id, redirect_uri, state, nonce, code_challenge) -> str`; `def http_client() -> httpx.AsyncClient`; `async def exchange(*, client, authority, client_id, client_secret, code, redirect_uri, code_verifier) -> str`; `class EntraKeys` con `async def key(kid) -> Any`; `async def validate_id_token(id_token, *, client_id, nonce, keys) -> EntraIdentity`; `def keys_of(app) -> EntraKeys` (una por proceso, en `app.state`).
- `app/core/auth/entra_settings.py`: `ENTRA_CREDENTIAL_TYPE = "entra_sign_in"`; `@dataclass(frozen=True) class EntraConfig(enabled, app_client_id, staff_tenant_id, configured, credential_id, updated_at, updated_by)` con `.usable`; `async def read(session, org_id) -> EntraConfig`; `class EntraSettingsInvalid(Exception)`; `async def save(session, vault, *, org_id, actor_id, enabled, app_client_id, staff_tenant_id, client_secret) -> EntraConfig`; `async def checkout_secret(session, vault, *, org_id, config, actor_id) -> str`.
- Rutas `GET`/`PUT /api/v1/settings/microsoft-sign-in` (`app/api/v1/entra_settings.py`).
- `ClientOut.microsoft_tenants: list[UUID]`, `ClientUpdate.microsoft_tenants: list[UUID] | None`.

### Task 6 — clave de acceso sin dirección

- `passkeys.registration_options(session, user, *, rp_id, rp_name, discoverable: bool = False)` (`residentKey: "required"` con `discoverable`).
- `flows.passkey_sign_in_options(request) -> dict`; `flows.passkey_sign_in(request, *, credential) -> SignIn`.
- Rutas `POST /auth/passkey/options` → opciones W3C; `POST /auth/passkey {credential}` → `{"state": "signed_in"}`.

### Task 7 — Mi seguridad del contacto

- `deps.bound_account_context(request) -> AsyncIterator[AuthContext]` (`admit(contacts=True)`, exige sesión).
- `MySecurityOut` gana `kind: Literal["staff","contact"]`, `has_password: bool`, `microsoft: MicrosoftLinkOut | None` (`tenant_id`, `created_at`, `last_used_at`).
- `POST /me/password {new}` → 204 (añadir la primera contraseña; 409 `password_exists`).
- `account.remove_passkey` / `remove_totp`: un contacto conserva al menos un método de entrada (409 `last_method`); su TOTP es opcional.

### Task 8 — ciclo de vida del contacto

- `action_tokens.digest(token) -> bytes`; `peek_digest(session, *, digest, purposes)`; `consume_digest(session, *, digest, purposes)`.
- `flows.Invitation(email, display_name, kind, password_minimum, passkey, microsoft)`; `flows.invite_details(request, *, token) -> Invitation`; `flows.invitee(session, *, digest, consume) -> User`; `flows.invite_passkey_options(request, *, token) -> dict`; `flows.invite_passkey(request, *, token, credential, name) -> SignIn`.
- `app/core/auth/portal_access.py`: `REQUESTER_ROLE = "requester"`; `async def grant_portal(session, user) -> bool`.
- `app/services/contact_access.py`: `ContactAccess(state: Literal["none","invited","active"], invite_expires_at)`; `access_of(session, *, org_id, user_ids) -> dict[UUID, ContactAccess]`; `invite(...)`, `cancel_invite(...)`, `reset_access(...)`.
- Rutas `POST /clients/{client_id}/contacts/{contact_id}/invite` → `ContactInviteOut{contact, invite_link}`; `DELETE /clients/{client_id}/contacts/{contact_id}/invite` → 204; `POST /clients/{client_id}/contacts/{contact_id}/access/reset` → `ContactInviteOut` (confirmación reciente); `POST /auth/invite/passkey/options {token}`; `POST /auth/invite/passkey {token, credential, name}`.
- `ContactOut` gana `access` y `invite_expires_at`; `InviteOut` gana `kind`, `password_minimum`, `passkey`, `microsoft`.

### Task 9 — Microsoft, los flujos

- `app/core/auth/entra_flows.py`: `ENTRA_COOKIE = "__Host-itms_entra"`, `ENTRA_TTL_S = 600`; `async def start(request, *, intent: Literal["sign_in","link","invite"], audience: Literal["staff","contact"], token: str | None) -> tuple[str, str]` (URL de Microsoft, valor de la cookie); `async def complete(request, *, code, state) -> SignIn | None` (`None` = vinculada); `async def unlink(request, principal) -> None`.
- Rutas `POST /auth/microsoft/start {intent, audience, token?}` → `{"authorize_url"}` + cookie; `POST /auth/microsoft/complete {code, state}` → `{"state": ...}`; `DELETE /me/microsoft` → 204 (confirmación reciente).

### Tasks 10–12 — consola

- `lib/api.ts`: `PortalMe`, `portalMe`, `passkeySignInOptions`, `passkeySignIn`, `stepUpPassword` (10); `ContactInviteOut`, `inviteContact`, `cancelContactInvite`, `resetContactAccess`, `acceptInvite` → `InviteAccepted`, `invitePasskeyOptions`, `invitePasskey`, `addPassword` (11); `MicrosoftStartIn`, `MicrosoftStep`, `MicrosoftSettingsOut`/`Update`, `microsoftStart(body, {signedIn})`, `microsoftComplete`, `unlinkMicrosoft`, `getMicrosoftSettings`, `putMicrosoftSettings` (12).
- `lib/session.ts`: `PORTAL_SIGN_IN_PATH = "/portal/sign-in"`, `PUBLIC_PATHS` con `/portal/sign-in`; `signInPath(next)` lleva al del portal cuando `next` es del portal; `signOut(api, to?)` (10). `lib/sessionCheck.ts`: un 403 `portal_only` de `/me` se confirma con `/portal/me` (10).
- `modules/portal/usePortalMe.ts` (`usePortalMe`, `PORTAL_ME_KEY`), `modules/portal/PortalShell.tsx` (10); `modules/profile/SecurityPage.tsx::PortalSecurityPage` (11); `modules/auth/microsoft.ts`, `MicrosoftButton.tsx`, `MicrosoftCallbackPage.tsx`, `modules/settings/MicrosoftTab.tsx` (12).
- Páginas: `/portal/sign-in` (10), `/portal/security` (11), `/sign-in/microsoft` (12); pestaña `?tab=microsoft` de Ajustes (12).

## Apéndice B — Decisiones del coordinador (D25–D44)

Continúan la numeración de la fase 1 (D1–D24). Se tomaron leyendo el código en `0182206`; las tareas las citan como «D*n*». Las que el diseño deja abiertas también están en «Preguntas para el operador», con la recomendación que el plan sigue mientras el operador no diga otra cosa.

- **D25** (§3; P7) **Una sola ruta de inicio de sesión.** `POST /auth/login` busca la dirección con `its_auth_principal`, que devuelve primero al técnico y después al contacto (0245), y aplica las reglas del tipo encontrado. Antes de probar la contraseña la respuesta es la misma (falsificador 2, sin cambio); después, el contacto puede quedar `signed_in` sin segundo factor. La consola tiene dos páginas (`/sign-in` y `/portal/sign-in`) sobre la misma ruta. Un contacto nunca pasa la puerta de la consola: su sesión es de tipo `contact` y `admit(contacts=False)` la rechaza (D42).
- **D26** (§3; P8) **Los contactos usan las rutas de su propia cuenta** (`/me/security`, `/me/password`, `/me/passkeys/*`, `/me/totp`, `/me/recovery-codes`, `/me/trusted-devices/*`, `/me/sessions/*`, `/auth/enroll/*` con la sesión completa, `/auth/step-up/*`) por la puerta del portal, `admit(contacts=True)`. `GET /me` y todo lo demás de la consola siguen cerrados a un contacto.
- **D27** (§3, Q2; P1) **El segundo factor de un contacto.** Opcional, y con la regla de los técnicos: si la cuenta tiene una clave de acceso o una app TOTP confirmada, la contraseña y Microsoft lo piden (salvo equipo recordado); una clave de acceso usada para entrar ya son dos factores y no pide más.
- **D28** (§3) **Clave de acceso de contacto descubrible.** Se registra con `residentKey: "required"` (W3C WebAuthn Nivel 3, *«The Relying Party requires a client-side discoverable credential. The client MUST return an error if a client-side discoverable credential cannot be created.»*, https://www.w3.org/TR/webauthn-3/ §5.4.6, leída el 2026-11-06) y se usa sin dirección (`allowCredentials` vacío, el camino que la fase 1 dejó listo en `passkeys.authentication_options(user_id=None)`). Así entrar con una clave no revela si una dirección tiene cuenta.
- **D29** (§2.5, §3; P13) **Confirmación reciente de un contacto, también con la contraseña** (`/auth/step-up/password`). Un técnico sigue confirmando solo con un segundo factor (§2.5).
- **D30** (§4; P9, P10) **La transacción de Microsoft.** `response_mode=fragment` a la página `/sign-in/microsoft` (el código no llega a ningún log de servidor ni a un `Referer`, como los enlaces de la fase 1), que lo envía por `POST` con `state`. El `code_verifier` de PKCE es el valor de la cookie `__Host-itms_entra`: así la transacción queda *«securely bound to the client and the user agent in which the transaction was started»* (RFC 9700 §2.1.1) sin guardar nada secreto en el servidor. `state` (clave en Redis) y `nonce` son de un solo uso, 10 minutos.
- **D31** (§4, [S61]; falsificador 8) **El ID token se valida aquí** y la persona es `(tid, oid)`. Nunca por `email` (*«Never use it for authorization»*, [S61]); nunca se crea una cuenta; un `tid`/`oid` sin fila es 401 `microsoft_not_linked`.
- **D32** (§4; P11) **Autoridad.** El botón de la consola usa el tenant de Novieri (`staff_tenant_id`) cuando está configurado; el del portal, `organizations`. Al volver, manda el tipo de la persona encontrada: un técnico solo con `tid = staff_tenant_id`; un contacto solo con un `tid` de `clients.microsoft_tenants` de su cliente.
- **D33** (§4, §9; P3) **El secreto de la app de Entra va a la bóveda** (un `Credential` de tipo `entra_sign_in`, cifrado por el sobre de la organización que envuelve OpenBao), el patrón D9 de la contraseña SMTP. No hay ruta KV nueva en OpenBao ni cambio de `its-app.hcl`.
- **D34** (§4) **Ajustes → Seguridad → Microsoft**: tabla G `auth_entra_settings`; leer con `orgs.manage`, guardar con `orgs.manage` en todos los clientes, como Ajustes → Correo (P14).
- **D35** (§4; P16) **Tenants por cliente** en `clients.microsoft_tenants` (como `email_domains`), hasta 20, editables con `clients.manage` en ese cliente (`PATCH /clients/{id}`), auditados en `client.updated`.
- **D36** (§6.2) **`its_auth_org(slug)`**, la tercera lectura antes de conocer la organización: una clave de acceso sin dirección y Microsoft no tienen dirección que dar a `its_auth_principal`. Devuelve solo el id de la organización.
- **D37** (A-M6; P4) **Retención**: 30 días tras terminar, caducar, revocarse o usarse; un desafío WebAuthn, 1 día; una invitación sin usar nunca se barre (es la definición de invitación pendiente de `user_admin.service._open_invites`); un equipo que aún nombra una sesión, tampoco.
- **D38** (A-M2) **`ITS_AUTH_KEYS_BACKEND=local` se rechaza con `ITS_ENVIRONMENT=production`**, existan o no los archivos, antes de abrirlos.
- **D39** (§3, §7; P6) **El acceso de un contacto lo gestiona su cliente**: invitar y reenviar (un enlace nuevo de 72 h anula el anterior), cancelar una invitación pendiente, y **restablecer el acceso** de un contacto que ya entró (quita contraseña, factores, claves, Microsoft, equipos y sesiones, y envía una invitación nueva), con `clients.manage` en ese cliente y, para restablecer, la confirmación reciente del técnico (§2.5).
- **D40** (§3) **Aceptar una invitación de contacto**: contraseña de 15+, clave de acceso o (Task 9) Microsoft. Al aceptar, el contacto recibe el rol `requester` (solo `portal.ticket.read` y `portal.ticket.write`, 0028) **en su cliente y en ningún otro**, y queda con la sesión completa: no tiene segundo factor que registrar.
- **D41** (§4, §7; P2) **Un técnico invitado que acepta con Microsoft** queda vinculado y sin contraseña; el segundo factor propio se registra a continuación, como con la contraseña.
- **D42** (§3) **La consola dice `portal_only`** (403) cuando la persona es un contacto, y el portal tiene su propio `GET /portal/me`, que responde a técnicos y contactos con códigos de portal.
- **D43** (§2.4) **Desactivar o archivar un contacto** cierra sus sesiones, olvida sus equipos y anula sus invitaciones y restablecimientos abiertos, en la misma transacción.
- **D44** Ninguna dependencia nueva; Microsoft se habla con `httpx` y PyJWT, como Email Intake (`modules/email_intake/microsoft.py`), y **no** con `app.core.security.JwksCache`, que la fase 3 o la 4 pueden retirar con Keycloak.

## Apéndice C — Fuentes leídas para este plan (2026-11-06)

Además del dossier del diseño. Cada cita es literal; la tarea que la usa la repite.

- **[M1]** Microsoft, *Microsoft identity platform and OAuth 2.0 authorization code flow* — https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow (ms.date 2026-01-09, updated_at 2026-06-15):
  - `response_mode`: *«query: Default when requesting an access token. … fragment: Default when requesting an ID token by using the implicit flow. Also supported if requesting only a code. form_post: Executes a POST containing the code to your redirect URI. Supported when requesting a code.»*
  - `code_challenge`: *«This parameter is now recommended for all application types, both public and confidential clients»*.
  - `code`: *«Authorization codes are short lived. Typically, they expire after about 1 minute.»*
  - `client_secret`: *«required for confidential web apps … It's required for web apps and web APIs, which can store the client_secret securely on the server side.»*
  - `id_token`: *«confidential clients can use this token for authorization … Note: Only provided if openid scope was requested.»*
- **[M2]** Microsoft, *OpenID Connect on the Microsoft identity platform* — https://learn.microsoft.com/en-us/entra/identity-platform/v2-protocols-oidc (ms.date 2026-06-30, updated_at 2026-10-06; es [S62]):
  - `organizations`: *«Only users with work or school accounts from Microsoft Entra ID can sign in to the application.»*
  - `nonce`: *«To mitigate token replay attacks, your app should verify the nonce value in the ID token is the same value it sent when requesting the token.»*
  - *«Web apps and web APIs that use ID tokens for authorization must validate them because such applications get access to data.»*
  - `response_mode` (en la petición de un ID token): *«Can be form_post or fragment. For web applications, we recommend using response_mode=form_post»* (Pregunta P9).
- **[M3]** Microsoft, documento de descubrimiento de `organizations` — https://login.microsoftonline.com/organizations/v2.0/.well-known/openid-configuration (JSON leído el 2026-11-06): `"issuer": "https://login.microsoftonline.com/{tenantid}/v2.0"`, `"jwks_uri": "https://login.microsoftonline.com/organizations/discovery/v2.0/keys"`, `"id_token_signing_alg_values_supported": ["RS256"]`, `"response_modes_supported": ["query", "fragment", "form_post"]`, `"token_endpoint_auth_methods_supported": ["client_secret_post", "private_key_jwt", "client_secret_basic", "self_signed_tls_client_auth"]`, y `claims_supported` sin `amr` (como [S61]).
- **[M4]** Microsoft, *ID token claims reference* — https://learn.microsoft.com/en-us/entra/identity-platform/id-token-claims-reference (updated_at 2026-06-15; es [S61]):
  - `aud`: *«This value should be validated. The token should be rejected if it fails to match your app's Application ID.»*
  - `iss`: *«If the token was issued by the v2.0 endpoint, the URI ends in /v2.0. The GUID that indicates that the user is a consumer user from a Microsoft account is 9188040d-6c67-4c5b-b112-36a304b66dad.»*
  - `nonce`: *«If it doesn't match, your application should reject the token.»*
  - `oid`: *«the profile scope is required to receive this claim»*.
  - `tid`: *«For work and school accounts, the GUID is the immutable tenant ID of the organization that the user is signing in to.»*
- **[M5]** Microsoft, *Secure applications and APIs by validating claims* — https://learn.microsoft.com/en-us/entra/identity-platform/claims-validation (updated_at 2026-06-15): *«Never use claims like email, preferred_username or unique_name to store or determine whether the user in an access token should have access to data.»*; *«use the immutable claim values tid and oid as a combined key for application data»*.
- **[R1]** RFC 9700 — https://www.rfc-editor.org/rfc/rfc9700.txt (es [S16]):
  - §2.1: *«Clients MUST prevent Cross-Site Request Forgery (CSRF). … one-time use CSRF tokens carried in the state parameter that are securely bound to the user agent MUST be used for CSRF protection»*.
  - §2.1.1: *«the PKCE challenge or OpenID Connect nonce MUST be transaction-specific and securely bound to the client and the user agent in which the transaction was started.»*
  - §4.5.3.2: *«The OP puts the received nonce value into the ID Token that is issued as part of the code exchange at the token endpoint.»* y *«MUST validate the nonce in the ID Token obtained from the token endpoint»*.
- **[W1]** W3C, *Web Authentication Level 3*, §5.4.6 `ResidentKeyRequirement` — https://www.w3.org/TR/webauthn-3/ (es [S14]): *«required: The Relying Party requires a client-side discoverable credential. The client MUST return an error if a client-side discoverable credential cannot be created.»*

---

## Las tareas

Cada tarea es de un revisor, en su propia worktree cuando la oleada es paralela (Global Constraints, «Pistas en paralelo»). Prueba primero, luego el código, luego la suite de la tarea y el commit.

### Task 1: Migración 0246, modelos, catálogo y `its_auth_org`

Diseño §4 («enlazado por `tid` + `oid`», «cada cliente lista en su ficha sus tenants permitidos», el secreto en OpenBao por la bóveda, D33), §6.1 («identidades de Microsoft (`tid` + `oid`, únicas)», todas con `org_id` y RLS, las de un contacto dentro de su cliente) y §6.2 (la lectura antes de conocer la organización, D36). Oleada 1; no depende de nada.

**Files:**
- Create: `backend/migrations/versions/0246_auth_contacts_microsoft.py`
- Modify: `backend/app/models/auth.py` (dos modelos y dos constantes, al final)
- Modify: `backend/app/models/__init__.py` (exportar `AuthExternalIdentity`, `AuthEntraSettings`)
- Modify: `backend/app/models/clients.py` (`Client.microsoft_tenants` y su CHECK)
- Modify: `backend/app/core/client_scope_catalog.py` (dos entradas)
- Modify: `backend/app/core/auth/lookup.py` (`org_of_slug`)
- Create: `backend/tests/test_auth_phase2_migration.py`
- Test: el nuevo, `backend/tests/test_client_catalog.py`, `backend/tests/test_schema_drift.py`, `backend/tests/test_rls_invariant_sql.py`, `backend/tests/test_auth_core_migration.py`

**Interfaces:**
- Consumes: el patrón `_person_scope` / `_tenant` de `0245_auth_core.py:135-177` (copiado: una migración no importa código); `client_org_fk`, `uuid7_pk` (`app/models/base.py`); `_client_id()` (`app/models/auth.py:61-69`).
- Produces:
  - tabla `auth_external_identities` y modelo `AuthExternalIdentity(id, org_id, client_id, user_id, provider, tenant_id: UUID, subject: UUID, created_at, last_used_at)`; `IDENTITY_PROVIDERS = ("entra",)`, `ENTRA = "entra"`;
  - tabla `auth_entra_settings` y modelo `AuthEntraSettings(org_id, enabled, app_client_id: UUID | None, credential_id: UUID | None, staff_tenant_id: UUID | None, updated_at, updated_by)`;
  - columna `clients.microsoft_tenants` y `Client.microsoft_tenants: list[UUID]`;
  - función `its_auth_org(text) RETURNS uuid` y `lookup.org_of_slug(engine, slug) -> UUID | None`.

**Decisión del plan (1): una vinculación por persona y proveedor, y una persona por cuenta de Microsoft en cada organización.** `UNIQUE(user_id, provider)` y `UNIQUE(org_id, provider, tenant_id, subject)`. La segunda lleva `org_id` a propósito: una restricción entre organizaciones contestaría a la segunda que la cuenta ya existe en la primera (el mismo razonamiento que `passkeys.verify_registration` documenta para `uq_auth_passkeys_credential_id`, que sí es global y por eso se atrapa en un savepoint). Microsoft recomienda exactamente esa clave: *«use the immutable claim values tid and oid as a combined key for application data»* [M5].

**Decisión del plan (2): `subject` es el `oid`, no el `sub`.** [M4]: el `sub` es *«a pairwise identifier and is unique to an application ID»*; el `oid` *«uniquely identifies the user across applications»* y el diseño §4 nombra `tid` + `oid`. Se guarda como `UUID` (el claim es un GUID, [M4]).

**Decisión del plan (3): los ajustes no se pueden encender a medias**, por un CHECK: `NOT enabled OR (app_client_id IS NOT NULL AND credential_id IS NOT NULL)`. El servicio de la Task 5 lo dice antes con un 422; el CHECK lo mantiene ante un arreglo por SQL.

- [ ] **Step 1: Comprobar la base**

Run (desde `backend/`): `.venv/bin/alembic heads`
Expected: `0245 (head)`. Si dice otra cosa, parar: la fase 3 o otra rama ya ocupó la cabeza y el coordinador debe decidir el encadenamiento.

- [ ] **Step 2: Escribir las pruebas de la migración**

Crear `backend/tests/test_auth_phase2_migration.py`:

```python
"""Migration 0246 (own authentication, phase 2; plan 2026-11-06, Task 1).

- upgrade 0245 -> 0246 -> downgrade 0245 -> upgrade 0246 leaves nothing
  behind: the two tables, the column, the function and the
  `its_client_to_auth_external_identities` trigger that lives on `users`.
- `its_auth_org` answers the id a slug names and nothing else, to `app_user`
  only (design §6.2, plan decision D36).
- A Microsoft identity follows its person (invariant 5a): a contact's link is
  inside the contact's client; a technician limited to another client does
  not see it.
- One Microsoft account is one person per organization, and a person has one
  link per provider (plan decision 1); the settings cannot be switched on half
  configured (decision 3); a client lists at most twenty tenants.
"""

import secrets
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.core.auth.lookup import org_of_slug
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import Client, Organization, User
from app.models.auth import ENTRA, AuthEntraSettings, AuthExternalIdentity
from tests.conftest import PgCluster, _psql
from tests.test_remote_supervise_migration import _alembic

TABLES = ("auth_external_identities", "auth_entra_settings")
FUNCTION = "its_auth_org(text)"


@pytest.fixture()
def db_at_0245(pg_cluster: PgCluster) -> Iterator[str]:
    dbname = f"its_auth_p2_{secrets.token_hex(4)}"
    _psql(f'CREATE DATABASE "{dbname}" OWNER its_admin')
    url = pg_cluster.admin_url.rsplit("/", 1)[0] + f"/{dbname}"
    try:
        _alembic(url, "upgrade", "0245")
        yield url
    finally:
        _psql(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


async def _schema(engine: AsyncEngine) -> dict[str, object]:
    async with engine.connect() as conn:
        tables = {
            t
            for t in TABLES
            if (await conn.execute(text("SELECT to_regclass(:t)"), {"t": t})).scalar_one()
        }
        function = (
            await conn.execute(text("SELECT to_regprocedure(:f)"), {"f": FUNCTION})
        ).scalar_one()
        column = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'clients' AND column_name = 'microsoft_tenants'"
                )
            )
        ).scalar_one()
        triggers = set(
            (
                await conn.execute(
                    text(
                        "SELECT tgname FROM pg_trigger WHERE tgrelid = 'users'::regclass "
                        "AND tgname = 'its_client_to_auth_external_identities'"
                    )
                )
            ).scalars()
        )
    return {
        "tables": tables,
        "function": None if function is None else str(function),
        "column": column,
        "triggers": triggers,
    }


async def test_0246_round_trips(db_at_0245: str) -> None:
    engine = create_async_engine(db_at_0245)
    present = {
        "tables": set(TABLES),
        "function": "its_auth_org(text)",
        "column": 1,
        "triggers": {"its_client_to_auth_external_identities"},
    }
    absent = {"tables": set(), "function": None, "column": 0, "triggers": set()}
    try:
        _alembic(db_at_0245, "upgrade", "0246")
        assert await _schema(engine) == present
        _alembic(db_at_0245, "downgrade", "0245")
        assert await _schema(engine) == absent
        _alembic(db_at_0245, "upgrade", "0246")
        assert await _schema(engine) == present
    finally:
        await engine.dispose()


async def _org(admin_engine: AsyncEngine) -> tuple[UUID, str]:
    slug = f"p2-{uuid4().hex[:8]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.commit()
        return org.id, slug


async def _contact(admin_engine: AsyncEngine, org_id: UUID, name: str) -> tuple[UUID, UUID]:
    """(client id, contact user id)."""
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        client = Client(org_id=org_id, name=f"{name}-{uuid4().hex[:6]}", kind="customer")
        session.add(client)
        await session.flush()
        user = User(
            org_id=org_id,
            kind="contact",
            client_id=client.id,
            email=f"{uuid4().hex[:8]}@{name}.test",
            external_subject=f"contact-pending:{uuid4()}",
        )
        session.add(user)
        await session.commit()
        return client.id, user.id


async def test_its_auth_org_names_an_organization_and_nothing_else(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, slug = await _org(admin_engine)
    assert await org_of_slug(app_engine, slug) == org_id
    assert await org_of_slug(app_engine, f"{slug}-nope") is None
    async with admin_engine.connect() as conn:
        allowed = {
            role: (
                await conn.execute(
                    text("SELECT has_function_privilege(:role, :fn, 'EXECUTE')"),
                    {"role": role, "fn": FUNCTION},
                )
            ).scalar_one()
            for role in ("app_user", "portal_user")
        }
        definer = (
            await conn.execute(
                text("SELECT prosecdef FROM pg_proc WHERE oid = to_regprocedure(:fn)"),
                {"fn": FUNCTION},
            )
        ).scalar_one()
    assert allowed == {"app_user": True, "portal_user": False}
    assert definer is True


async def test_a_contacts_microsoft_link_is_inside_the_contacts_client(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, _slug = await _org(admin_engine)
    acme, ana = await _contact(admin_engine, org_id, "acme")
    other, _bob = await _contact(admin_engine, org_id, "other")
    async with org_session(org_id, engine=app_engine, clients=ClientScope.only([acme])) as s:
        link = AuthExternalIdentity(
            org_id=org_id, user_id=ana, provider=ENTRA, tenant_id=uuid4(), subject=uuid4()
        )
        s.add(link)
        await s.flush()
        await s.refresh(link)
        assert link.client_id == acme
        link_id = link.id
    async with org_session(org_id, engine=app_engine, clients=ClientScope.only([other])) as s:
        seen = await s.scalar(
            select(AuthExternalIdentity.id).where(AuthExternalIdentity.id == link_id)
        )
    assert seen is None


async def test_one_microsoft_account_is_one_person_and_one_link_per_person(
    admin_engine: AsyncEngine,
) -> None:
    org_id, _slug = await _org(admin_engine)
    _acme, ana = await _contact(admin_engine, org_id, "acme")
    _beta, bob = await _contact(admin_engine, org_id, "beta")
    tenant, subject = uuid4(), uuid4()
    async with AsyncSession(admin_engine) as session:
        session.add(
            AuthExternalIdentity(
                org_id=org_id, user_id=ana, provider=ENTRA, tenant_id=tenant, subject=subject
            )
        )
        await session.commit()
    for user_id, tid, oid in ((bob, tenant, subject), (ana, uuid4(), uuid4())):
        async with AsyncSession(admin_engine) as session:
            session.add(
                AuthExternalIdentity(
                    org_id=org_id, user_id=user_id, provider=ENTRA, tenant_id=tid, subject=oid
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()


async def test_the_settings_cannot_be_switched_on_half_configured(
    admin_engine: AsyncEngine,
) -> None:
    org_id, _slug = await _org(admin_engine)
    async with AsyncSession(admin_engine) as session:
        session.add(AuthEntraSettings(org_id=org_id, enabled=True, app_client_id=uuid4()))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_a_client_lists_at_most_twenty_tenants(admin_engine: AsyncEngine) -> None:
    org_id, _slug = await _org(admin_engine)
    acme, _ana = await _contact(admin_engine, org_id, "acme")
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            update(Client).where(Client.id == acme).values(microsoft_tenants=[uuid4()] * 20)
        )
        await session.commit()
    async with AsyncSession(admin_engine) as session:
        await session.execute(
            update(Client).where(Client.id == acme).values(microsoft_tenants=[uuid4()] * 21)
        )
        with pytest.raises(IntegrityError):
            await session.commit()
```

- [ ] **Step 3: Correrlas para verlas fallar**

Run: `.venv/bin/pytest -q tests/test_auth_phase2_migration.py`
Expected: FAIL en la colección, `ImportError: cannot import name 'org_of_slug' from 'app.core.auth.lookup'` (y, tras él, `ENTRA`).

- [ ] **Step 4: Escribir la migración**

Crear `backend/migrations/versions/0246_auth_contacts_microsoft.py`:

```python
"""Own authentication, phase 2: sign in with Microsoft (Entra ID), and the one
read before the organization is known that a sign-in without an address needs
(`docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §4, §6.1,
§6.2; plan 2026-11-06, Task 1).

THE TABLES.

- `auth_external_identities`: a person's link to a Microsoft account, by the
  two claims Microsoft says do not change -- `tid`, the tenant, and `oid`, the
  account in it ("use the immutable claim values tid and oid as a combined
  key") -- and never by address ("Never use it for authorization", the email
  claim). One link per person and provider; one person per (organization,
  provider, tenant, account): the key carries `org_id`, so it does not tell
  one organization that another already holds the account. A person's row:
  O under `users` through `user_id`, 0245's pattern verbatim -- NULL client for
  a technician, the contact's own client for a contact (invariant 5a).
- `auth_entra_settings`: Settings -> Security -> Microsoft, one optional row
  per organization (G). The application's client secret is not a column: it
  is the vault secret of `credential_id` (invariant 3), the way Settings ->
  Email keeps the relay's. A CHECK stops it being switched on without an
  application and a secret.
- `clients.microsoft_tenants`: the tenants whose accounts a client's contacts
  may sign in with, at most twenty.

`its_auth_org(slug)` IS THE THIRD READ BEFORE THE ORGANIZATION IS KNOWN (§6.2).
A passkey sign-in without an address and a Microsoft sign-in bring no address
for `its_auth_principal`, so this answers the id of the organization a slug
names, and nothing else. SECURITY DEFINER, owned by the migrating role
`its_admin`, `search_path` pinned, EXECUTE for `app_user` alone, as 0245's two.

Names are written here, not imported. Forward-only; `downgrade()` has never
run in production (rollback is restore-from-backup). CI runs
upgrade->downgrade->upgrade.

Revision ID: 0246
Revises: 0245
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0246"
down_revision: str | None = "0245"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_GUC = "current_setting('app.current_org', true)::uuid"

# 0231's client predicates, copied as 0245 copies them: a migration imports nothing.
VISIBLE = "its_client_visible(client_id)"
SCOPE = "current_setting('app.client_scope', true)"
SCOPE_PINNED = (
    f"({SCOPE} = 'all' OR ({SCOPE} = 'list' AND "
    "nullif(current_setting('app.current_clients', true), '') IS NOT NULL))"
)
NULL_PINNED = f"(client_id IS NULL AND {SCOPE_PINNED})"
NULL_ALL = f"(client_id IS NULL AND {SCOPE} = 'all')"

IDENTITIES = "auth_external_identities"
SETTINGS = "auth_entra_settings"

ORG_FUNCTION = """
CREATE FUNCTION its_auth_org(p_org_slug text)
RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    -- The organization a slug names: its id and nothing else (design §6.2).
    SELECT o.id FROM organizations o WHERE o.slug = p_org_slug
$$
"""
ORG_SIGNATURE = "its_auth_org(text)"


def _uuid(name: str, *args: object, nullable: bool = False, **kw: object) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), *args, nullable=nullable, **kw)


def _when(name: str, *, nullable: bool = False, now: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.func.now() if now else None,
    )


def _tenant(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING (org_id = {_ORG_GUC}) WITH CHECK (org_id = {_ORG_GUC})"
    )


def _person_scope(table: str) -> None:
    """0245's O-under-`users` treatment, verbatim: index, org-consistency key,
    both triggers, and the three restrictive client policies."""
    op.execute(f"CREATE INDEX ix_{table}_client_id ON {table} (client_id)")
    op.execute(
        f"ALTER TABLE {table} ADD CONSTRAINT fk_{table}_client_org "
        "FOREIGN KEY (client_id, org_id) REFERENCES clients (id, org_id)"
    )
    op.execute(
        f"CREATE TRIGGER its_client_from_parent BEFORE INSERT OR UPDATE OF user_id, client_id "
        f"ON {table} FOR EACH ROW EXECUTE FUNCTION its_client_from_parent('users', 'user_id')"
    )
    op.execute(
        f"CREATE TRIGGER its_client_to_{table} AFTER UPDATE ON users FOR EACH ROW "
        "WHEN (OLD.client_id IS DISTINCT FROM NEW.client_id) "
        f"EXECUTE FUNCTION its_client_to_children('{table}', 'user_id')"
    )
    _tenant(table)
    read = f"{NULL_PINNED} OR {VISIBLE}"
    modify = f"{NULL_ALL} OR {VISIBLE}"
    op.execute(
        f"CREATE POLICY client_isolation ON {table} AS RESTRICTIVE FOR ALL "
        f"USING ({read}) WITH CHECK ({modify})"
    )
    op.execute(
        f"CREATE POLICY client_isolation_update ON {table} AS RESTRICTIVE FOR UPDATE "
        f"USING ({modify})"
    )
    op.execute(
        f"CREATE POLICY client_isolation_delete ON {table} AS RESTRICTIVE FOR DELETE "
        f"USING ({modify})"
    )


def upgrade() -> None:
    op.create_table(
        IDENTITIES,
        _uuid("id", primary_key=True),
        _uuid("org_id", sa.ForeignKey("organizations.id")),
        _uuid("client_id", sa.ForeignKey("clients.id"), nullable=True),
        _uuid("user_id", sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("provider", sa.Text(), nullable=False),
        _uuid("tenant_id"),
        _uuid("subject"),
        _when("created_at", now=True),
        _when("last_used_at", nullable=True),
        sa.UniqueConstraint(
            "org_id", "provider", "tenant_id", "subject", name="uq_auth_external_identities_subject"
        ),
        sa.UniqueConstraint("user_id", "provider", name="uq_auth_external_identities_user"),
        sa.CheckConstraint("provider IN ('entra')", name="ck_auth_external_identities_provider"),
    )
    _person_scope(IDENTITIES)

    op.create_table(
        SETTINGS,
        _uuid("org_id", sa.ForeignKey("organizations.id"), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        _uuid("app_client_id", nullable=True),
        _uuid("credential_id", sa.ForeignKey("credentials.id", ondelete="SET NULL"), nullable=True),
        _uuid("staff_tenant_id", nullable=True),
        _when("updated_at", now=True),
        _uuid("updated_by", sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.CheckConstraint(
            "NOT enabled OR (app_client_id IS NOT NULL AND credential_id IS NOT NULL)",
            name="ck_auth_entra_settings_ready",
        ),
    )
    _tenant(SETTINGS)

    op.add_column(
        "clients",
        sa.Column(
            "microsoft_tenants",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.create_check_constraint(
        "ck_clients_microsoft_tenants", "clients", "cardinality(microsoft_tenants) <= 20"
    )

    op.execute(ORG_FUNCTION)
    op.execute(f"REVOKE ALL ON FUNCTION {ORG_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {ORG_SIGNATURE} TO app_user")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {ORG_SIGNATURE}")
    op.drop_constraint("ck_clients_microsoft_tenants", "clients", type_="check")
    op.drop_column("clients", "microsoft_tenants")
    op.drop_table(SETTINGS)
    # The trigger lives on `users` (0245's downgrade says why it is dropped by hand).
    op.execute(f"DROP TRIGGER IF EXISTS its_client_to_{IDENTITIES} ON users")
    op.drop_table(IDENTITIES)
```

- [ ] **Step 5: Escribir los modelos**

En `backend/app/models/auth.py`, añadir `false` al `from sqlalchemy import (...)` y, al final del archivo:

```python
#: `auth_external_identities.provider`: Microsoft Entra ID, the one outside
#: identity provider (design §4, §11).
ENTRA = "entra"
IDENTITY_PROVIDERS = (ENTRA,)


class AuthExternalIdentity(Base):
    """A person's link to a Microsoft account (design §4, migration 0246): the
    tenant (`tid`) and the account in it (`oid`), the two claims Microsoft says
    do not change. Never an address. A person's row: NULL client for a
    technician, the contact's client for a contact (module docstring)."""

    __tablename__ = "auth_external_identities"
    __table_args__ = (
        client_org_fk("auth_external_identities"),
        UniqueConstraint(
            "org_id", "provider", "tenant_id", "subject", name="uq_auth_external_identities_subject"
        ),
        UniqueConstraint("user_id", "provider", name="uq_auth_external_identities_user"),
        CheckConstraint("provider IN ('entra')", name="ck_auth_external_identities_provider"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    client_id: Mapped[UUID | None] = _client_id()
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(Text)
    #: The `tid` claim: the tenant the account signed in to.
    tenant_id: Mapped[UUID] = mapped_column()
    #: The `oid` claim: the account, immutable within its tenant.
    subject: Mapped[UUID] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(default=None)


class AuthEntraSettings(Base):
    """Settings → Security → Microsoft (design §4; plan decision D34): the
    organization's one application registration. The client secret is the
    vault secret of `credential_id` (invariant 3), never a column."""

    __tablename__ = "auth_entra_settings"
    __table_args__ = (
        CheckConstraint(
            "NOT enabled OR (app_client_id IS NOT NULL AND credential_id IS NOT NULL)",
            name="ck_auth_entra_settings_ready",
        ),
    )

    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(default=False, server_default=false())
    #: The application (client) ID Microsoft assigned to the registration.
    app_client_id: Mapped[UUID | None] = mapped_column(default=None)
    credential_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("credentials.id", ondelete="SET NULL"), default=None
    )
    #: Novieri's own tenant: the only one technicians sign in from (design §4).
    staff_tenant_id: Mapped[UUID | None] = mapped_column(default=None)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
```

Y en el docstring del módulo, después del párrafo «NOTHING HERE HOLDS A SECRET IN CLEAR», añadir:

```
`AuthExternalIdentity` (migration 0246) links a person to a Microsoft account
by `tid` + `oid`; `AuthEntraSettings` is the organization's application
registration, whose client secret is the vault secret of its `credential_id`.
```

En `backend/app/models/__init__.py`, dentro de `from app.models.auth import (...)` y en `__all__`, añadir `AuthEntraSettings` y `AuthExternalIdentity` en su orden alfabético.

En `backend/app/models/clients.py`, en `Client.__table_args__` añadir:

```python
        CheckConstraint(
            "cardinality(microsoft_tenants) <= 20", name="ck_clients_microsoft_tenants"
        ),
```

y, debajo de `email_domains`:

```python
    # Migration 0246 (own authentication phase 2, design §4): the Microsoft
    # tenants whose accounts this client's contacts may sign in with.
    microsoft_tenants: Mapped[list[UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), default=list, server_default=text("'{}'")
    )
```

con `from sqlalchemy.dialects.postgresql import UUID as PG_UUID` junto al `ARRAY` que el archivo ya importa (y `from uuid import UUID` si no está).

- [ ] **Step 6: El catálogo y `org_of_slug`**

En `backend/app/core/client_scope_catalog.py`, en el bloque «own authentication (migration 0245)», después de `"account_mail_settings": G,`:

```python
    # own authentication phase 2 (migration 0246): a person's Microsoft link
    # follows their `users` row like every credential above; the application
    # registration is the org's.
    "auth_external_identities": O("users", "user_id"), "auth_entra_settings": G,
```

En `backend/app/core/auth/lookup.py`, añadir al final (y `from uuid import UUID` ya está):

```python
async def org_of_slug(engine: AsyncEngine, slug: str) -> UUID | None:
    """The organization `slug` names, or None (`its_auth_org`, migration 0246;
    plan decision D36): what a sign-in that brings no address -- a passkey
    without one, Microsoft -- pins before it reads anything under RLS."""
    async with engine.connect() as connection:
        value: UUID | None = (
            await connection.execute(text("SELECT its_auth_org(:slug)"), {"slug": slug})
        ).scalar_one()
    return value
```

y en su docstring, después de «A RAW CONNECTION WITH NO GUC», añadir: *«`org_of_slug` is the third such read (0246): the organization of a slug, for a sign-in that has no address to look up.»*

- [ ] **Step 7: Correr las pruebas**

Run: `.venv/bin/pytest -q tests/test_auth_phase2_migration.py tests/test_client_catalog.py tests/test_schema_drift.py tests/test_rls_invariant_sql.py tests/test_auth_core_migration.py`
Expected: PASS. Si `test_client_catalog.py` falla en las políticas de `auth_external_identities`, comparar con `expected_client_policies` (`tests/client_scope_support.py`): `_person_scope` debe ser el de 0245 letra por letra.

- [ ] **Step 8: Tipos y estilo**

Run: `.venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: `Success: no issues found` y `All checks passed!`

- [ ] **Step 9: Commit**

```bash
git add backend/migrations/versions/0246_auth_contacts_microsoft.py backend/app/models/auth.py \
  backend/app/models/__init__.py backend/app/models/clients.py \
  backend/app/core/client_scope_catalog.py backend/app/core/auth/lookup.py \
  backend/tests/test_auth_phase2_migration.py
git commit -m "feat(auth): 0246 -- Microsoft identities, Entra settings, client tenants, its_auth_org"
```

---

### Task 2: A-M2 — el respaldo `local` de claves, rechazado en producción

Revisión final de la fase 1, A-M2 (`final-review-A.md`, Minor 2): *«the `local` backend is the default, and in production it only refuses missing files. If `auth-pepper.key` and `auth-signing.key` already exist in `ITS_CA_DIR`, for example left by one start with `ITS_ENVIRONMENT` unset on the same volume, a production api silently peppers with a file on its own disk. … The pepper's whole value is being off the app host, though, so consider refusing `local` outright when `environment == "production"`.»* Diseño §6.3 («El pepper, en OpenBao y versionado»). Oleada 1; no depende de nada.

**La regla exacta (D38):** `keys.build(settings)` lanza `LocalAuthKeysInProduction` cuando `settings.auth_keys_backend == "local"` **y** `settings.environment == "production"`, **antes de mirar el disco**: no lee, no crea ni comprueba ningún archivo. Con `dev` o `test`, `local` sigue igual (y crea los archivos si faltan). `openbao` no cambia en ningún entorno. Como `keys.configure` corre en `app.main.create_app`, un api de producción con `local` no arranca, con un mensaje que nombra el remedio.

**Files:**
- Modify: `backend/app/core/auth/keys.py` (`build`, una excepción nueva, el docstring del módulo `:33-38`)
- Modify: `backend/app/core/config.py:57-64` (el comentario de `auth_keys_backend`)
- Modify: `backend/tests/test_auth_keys.py` (re-apunta `test_production_refuses_to_invent_a_pepper`, `:361-364`; cuatro pruebas nuevas)
- Modify: `deploy/.env.example:247-251` y `deploy/README.md:497-503`
- Test: `backend/tests/test_auth_keys.py`

**Interfaces:**
- Consumes: `Settings.environment`, `Settings.auth_keys_backend`, `LocalAuthKeys.from_dir`, `OpenBaoAuthKeys` (`keys.py:150-352`).
- Produces: `class LocalAuthKeysInProduction(RuntimeError)` en `app/core/auth/keys.py`.

**Decisión del plan (1): una excepción propia, no `MissingAuthKeyError`.** No falta ninguna clave: sobra un respaldo. El nombre dice qué arreglar. `test_production_refuses_to_invent_a_pepper` esperaba `MissingAuthKeyError`; se re-apunta a la excepción nueva y conserva su segunda aserción (no se crea el pepper). El informe de la tarea lo dice.

- [ ] **Step 1: Escribir las pruebas**

En `backend/tests/test_auth_keys.py`, añadir `LocalAuthKeysInProduction` al `from app.core.auth.keys import (...)` y reemplazar `test_production_refuses_to_invent_a_pepper` (`:361-364`) por:

```python
def test_production_refuses_the_local_backend_before_touching_a_file(tmp_path: Path) -> None:
    """A-M2, the half phase 1 had: nothing is created. Re-pointed from
    `MissingAuthKeyError` (plan 2026-11-06, Task 2, decision 1)."""
    with pytest.raises(LocalAuthKeysInProduction):
        keys.build(_settings(tmp_path, environment="production"))
    assert not (tmp_path / keys.PEPPER_FILE).exists()
    assert not (tmp_path / keys.SIGNING_FILE).exists()


def test_production_refuses_the_local_backend_even_with_its_files(tmp_path: Path) -> None:
    """A-M2, the half it lacked: files a development start left on the same
    volume do not make `local` acceptable in production."""
    LocalAuthKeys.from_dir(tmp_path, allow_generate=True)
    assert (tmp_path / keys.PEPPER_FILE).exists()
    with pytest.raises(LocalAuthKeysInProduction) as refused:
        keys.build(_settings(tmp_path, environment="production"))
    assert "ITS_AUTH_KEYS_BACKEND=openbao" in str(refused.value)


def test_production_refuses_a_separate_local_keys_directory_too(tmp_path: Path) -> None:
    keys_dir = tmp_path / "auth-keys"
    LocalAuthKeys.from_dir(keys_dir, allow_generate=True)
    with pytest.raises(LocalAuthKeysInProduction):
        keys.build(_settings(tmp_path, environment="production", auth_keys_dir=str(keys_dir)))


def test_production_takes_openbao(tmp_path: Path) -> None:
    bao = _settings(
        tmp_path,
        environment="production",
        auth_keys_backend="openbao",
        openbao_addr="https://bao.test",
        openbao_token="t",
    )
    assert isinstance(keys.build(bao), OpenBaoAuthKeys)


@pytest.mark.parametrize("environment", ["dev", "test"])
def test_development_and_tests_keep_the_local_backend(tmp_path: Path, environment: str) -> None:
    built = keys.build(_settings(tmp_path, environment=environment))
    assert isinstance(built, LocalAuthKeys)
    assert (tmp_path / keys.PEPPER_FILE).exists()
```

- [ ] **Step 2: Verlas fallar**

Run: `.venv/bin/pytest -q tests/test_auth_keys.py`
Expected: FAIL en la colección, `ImportError: cannot import name 'LocalAuthKeysInProduction'`.

- [ ] **Step 3: La regla**

En `backend/app/core/auth/keys.py`, después de `class UnknownKeyVersion(LookupError)`:

```python
class LocalAuthKeysInProduction(RuntimeError):
    """`ITS_AUTH_KEYS_BACKEND=local` with `ITS_ENVIRONMENT=production`
    (final review A-M2): refused whether or not the two files exist, before
    any of them is opened. The pepper's whole value is being off the app
    host; two files on the api's own disk are not."""
```

y reemplazar `build` por:

```python
def build(settings: Settings) -> AuthKeys:
    """The backend `settings` asks for (the `get_vault` / `get_signer` shape).

    LOCAL IS NEVER PRODUCTION (final review A-M2, plan 2026-11-06 decision
    D38). Before, production refused only MISSING files, so two files left on
    the volume by one start without `ITS_ENVIRONMENT` made a production api
    pepper with a key on its own disk. Now the rule is the setting itself,
    checked before the disk is looked at."""
    if settings.auth_keys_backend == "local":
        if settings.environment == "production":
            raise LocalAuthKeysInProduction(  # i18n-exempt: startup config error, logged
                "ITS_AUTH_KEYS_BACKEND=local is refused with ITS_ENVIRONMENT=production: the "
                "password pepper and the token signing key must stay in OpenBao; set "
                "ITS_AUTH_KEYS_BACKEND=openbao"
            )
        return LocalAuthKeys.from_dir(
            settings.auth_keys_dir or settings.ca_dir, allow_generate=True
        )
    if not settings.openbao_addr or not settings.openbao_token:
        raise ValueError(  # i18n-exempt: startup config error, logged
            "openbao auth keys backend requires ITS_OPENBAO_ADDR and ITS_OPENBAO_TOKEN"
        )
    return OpenBaoAuthKeys(settings.openbao_addr, settings.openbao_token)
```

En el docstring del módulo, reemplazar la frase «In production it refuses to create them -- a fresh pepper would make every stored password unverifiable -- and `ITS_AUTH_KEYS_BACKEND=openbao` is the deployment's setting (`deploy/.env.example`).» por:

```
In production it is refused outright, whether or not the files exist
(`LocalAuthKeysInProduction`, final review A-M2): a pepper on the api's own
disk is not one "stored separately", and `ITS_AUTH_KEYS_BACKEND=openbao` is
the deployment's setting (`deploy/.env.example`).
```

- [ ] **Step 4: Correr las pruebas**

Run: `.venv/bin/pytest -q tests/test_auth_keys.py`
Expected: PASS (las de la fase 1 y las cinco nuevas).

- [ ] **Step 5: Los comentarios y el despliegue**

En `backend/app/core/config.py`, el comentario de `auth_keys_backend` termina hoy «and in production refuses to start rather than invent a pepper no stored password was made with.»; cambiar esa última frase por: «and is refused in production whether or not the files exist (`LocalAuthKeysInProduction`, final review A-M2).»

En `deploy/.env.example`, el comentario encima de `ITS_AUTH_KEYS_BACKEND=openbao` (`:247-250`) queda:

```
# The sign-in pepper and the token signing key (backend/app/core/auth/keys.py):
# transit keys `auth-pepper` and `auth-signing`, which deploy/openbao/init.sh
# creates. "local" keeps both in files on the api's disk and is for
# development only: with ITS_ENVIRONMENT=production the api refuses to start
# on it, whether or not the files exist (LocalAuthKeysInProduction).
```

En `deploy/README.md` (`:499-503`), la frase «without it the api uses the `local` backend, which with `ITS_ENVIRONMENT=production` refuses to start (`MissingAuthKeyError`), so the api restart-loops:» queda «without it the api uses the `local` backend, which with `ITS_ENVIRONMENT=production` is refused at start (`LocalAuthKeysInProduction`), whether or not its files exist, so the api restart-loops:».

- [ ] **Step 6: Comprobaciones**

Run: `.venv/bin/mypy app && .venv/bin/ruff check app tests && grep -n "MissingAuthKeyError" ../deploy/README.md`
Expected: `Success: no issues found`, `All checks passed!` y el `grep` sin salida (código de salida 1).

- [ ] **Step 7: Commit**

```bash
git add backend/app/core/auth/keys.py backend/app/core/config.py backend/tests/test_auth_keys.py \
  deploy/.env.example deploy/README.md
git commit -m "fix(auth): refuse the local key backend in production, files or not (A-M2)"
```

---

### Task 3: A-M6 — retención de sesiones, equipos recordados, enlaces de un solo uso y desafíos

Revisión final de la fase 1, A-M6 (`final-review-A.md`, Minor 6): *«No retention for the new tables. `auth_sessions` (with `ip` and `user_agent`), `auth_trusted_devices`, `auth_action_tokens` and ended or expired rows grow for ever, and no worker sweep removes them. Follow-up: a sweep, plus its `ALL_SCOPE_ALLOWLIST` entry.»* El diseño no fija plazos: este plan propone 30 días (Pregunta P4, D37). Oleada 1; no depende de 0246.

**Files:**
- Create: `backend/app/core/auth/retention.py`
- Modify: `backend/app/worker.py` (import, `auth_retention_all_orgs`, entrada de beat, tarea)
- Modify: `backend/app/core/client_scope.py` (`ALL_SCOPE_ALLOWLIST`, una entrada)
- Create: `backend/tests/test_auth_retention.py`
- Modify: `backend/tests/test_worker_sweep_containment.py` (`_DELEGATING_SHELLS`, una entrada)
- Test: los dos, `backend/tests/test_client_catalog.py`

**Interfaces:**
- Consumes: los modelos `AuthSession`, `AuthTrustedDevice`, `AuthActionToken`, `AuthWebauthnChallenge` (`app/models/auth.py`), `action_tokens.INVITE`, `org_session`, `ClientScope.all()`, `_OrgCycleFailures` (`app/worker.py:150-249`).
- Produces: `app/core/auth/retention.py` (`RETENTION`, `CHALLENGE_RETENTION`, `SWEEP_INTERVAL_S`, `Swept`, `sweep`, `sweep_org`); `app.worker.auth_retention_all_orgs`; tarea Celery `app.worker.auth_retention`; entrada de beat `auth-retention`.

**Decisión del plan (1): qué se barre** — exactamente lo que ya no puede volver a servir a nadie:
- una sesión terminada (`ended_at`) o pasada su duración máxima (`expires_at`) hace más de `RETENTION`;
- un equipo recordado revocado o caducado hace más de `RETENTION` **y que ninguna sesión restante nombra** (`fk_auth_sessions_device_user` no tiene `ON DELETE`, y una sesión iniciada en un equipo puede durar más que él);
- un enlace de un solo uso usado hace más de `RETENTION`, o uno de restablecimiento o recuperación caducado sin usar hace más de `RETENTION`. **Una invitación sin usar no se barre nunca**: es la única definición de invitación pendiente (`user_admin.service._open_invites`: «Expiry is NOT part of it: an expired link is still an invitation to resend or cancel»); barrida, la persona no estaría invitada ni se podría cancelar;
- un desafío WebAuthn usado o caducado hace más de `CHALLENGE_RETENTION` (1 día): `passkeys._issue` ya barre los de la persona que vuelve; este barrido es el de quien no vuelve.

**Decisión del plan (2): sin auditoría.** Como las otras retenciones del worker (`security_logs.retention`, `ai_admin.retention`): se borra contabilidad; los actos están en la cadena.

- [ ] **Step 1: Escribir las pruebas**

Crear `backend/tests/test_auth_retention.py`:

```python
"""Own authentication's retention sweep (final review A-M6; plan 2026-11-06,
Task 3, decision 1): what goes after `RETENTION`, what never goes, and one
organization's sweep never touching another's rows."""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.auth import retention
from app.core.config import Settings, override_settings
from app.models import Client, Organization, User
from app.models.auth import AuthActionToken, AuthSession, AuthTrustedDevice, AuthWebauthnChallenge

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
OLD = NOW - retention.RETENTION - timedelta(days=1)
RECENT = NOW - timedelta(days=1)


@dataclass(frozen=True)
class _World:
    org_id: UUID
    staff_id: UUID
    contact_id: UUID


async def _world(admin_engine: AsyncEngine) -> _World:
    slug = f"ret-{uuid4().hex[:8]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name=slug, slug=slug)
        s.add(org)
        await s.flush()
        client = Client(org_id=org.id, name=f"Acme-{slug}", kind="customer")
        s.add(client)
        await s.flush()
        staff = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"t@{slug}.test")
        contact = User(
            org_id=org.id,
            kind="contact",
            client_id=client.id,
            email=f"c@{slug}.test",
            external_subject=f"contact-pending:{uuid4()}",
        )
        s.add_all([staff, contact])
        await s.commit()
        return _World(org.id, staff.id, contact.id)


def _hash() -> bytes:
    return secrets.token_bytes(32)


async def _seed(admin_engine: AsyncEngine, w: _World) -> dict[str, UUID]:
    """Every case of decision 1, named; returns name -> row id. Ids are set
    here, not by the model's default, so they exist before the flush."""
    ids: dict[str, UUID] = {}
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:

        def device(name: str, user: UUID, *, expires: datetime, revoked: datetime | None) -> None:
            row = AuthTrustedDevice(
                id=uuid4(),
                org_id=w.org_id,
                user_id=user,
                token_hash=_hash(),
                created_at=expires - timedelta(days=15),
                expires_at=expires,
                revoked_at=revoked,
            )
            s.add(row)
            ids[name] = row.id

        device("device_old_free", w.staff_id, expires=OLD, revoked=OLD)
        device("device_old_named", w.staff_id, expires=OLD, revoked=None)
        device("device_live", w.contact_id, expires=NOW + timedelta(days=10), revoked=None)
        await s.flush()

        def session(
            name: str,
            user: UUID,
            kind: str,
            *,
            expires: datetime,
            ended: datetime | None = None,
            on_device: str | None = None,
        ) -> None:
            row = AuthSession(
                id=uuid4(),
                org_id=w.org_id,
                user_id=user,
                token_hash=_hash(),
                kind=kind,
                created_at=expires - timedelta(hours=12),
                last_seen_at=expires - timedelta(hours=12),
                expires_at=expires,
                ended_at=ended,
                ended_reason="signed_out" if ended is not None else None,
                trusted_device_id=None if on_device is None else ids[on_device],
            )
            s.add(row)
            ids[name] = row.id

        session(
            "session_ended_old", w.staff_id, "staff", expires=OLD + timedelta(hours=1), ended=OLD
        )
        session("session_expired_old", w.contact_id, "contact", expires=OLD)
        session("session_ended_recent", w.staff_id, "staff", expires=NOW, ended=RECENT)
        session("session_live", w.contact_id, "contact", expires=NOW + timedelta(hours=1))
        session(
            "session_live_on_device",
            w.staff_id,
            "staff",
            expires=NOW + timedelta(days=1),
            on_device="device_old_named",
        )

        def link(
            name: str, user: UUID, purpose: str, *, expires: datetime, used: datetime | None = None
        ) -> None:
            row = AuthActionToken(
                id=uuid4(),
                org_id=w.org_id,
                user_id=user,
                purpose=purpose,
                token_hash=_hash(),
                created_at=expires - timedelta(hours=1),
                expires_at=expires,
                used_at=used,
            )
            s.add(row)
            ids[name] = row.id

        link("invite_pending_old", w.contact_id, "invite", expires=OLD - timedelta(days=60))
        link("reset_expired_old", w.staff_id, "reset", expires=OLD)
        link("recover_used_old", w.staff_id, "recover", expires=OLD, used=OLD)
        link("invite_used_old", w.contact_id, "invite", expires=OLD, used=OLD)
        link("reset_expired_recent", w.staff_id, "reset", expires=RECENT)

        def challenge(
            name: str, user: UUID | None, *, expires: datetime, used: datetime | None = None
        ) -> None:
            row = AuthWebauthnChallenge(
                id=uuid4(),
                org_id=w.org_id,
                user_id=user,
                purpose="authenticate",
                challenge=secrets.token_bytes(32),
                expires_at=expires,
                used_at=used,
            )
            s.add(row)
            ids[name] = row.id

        two_days = NOW - timedelta(days=2)
        challenge("challenge_used_old", w.staff_id, expires=two_days, used=two_days)
        challenge("challenge_expired_hours", None, expires=NOW - timedelta(hours=2))
        challenge("challenge_live", w.staff_id, expires=NOW + timedelta(minutes=5))
        await s.commit()
    return ids


async def _left(admin_engine: AsyncEngine, org_id: UUID) -> set[UUID]:
    async with AsyncSession(admin_engine) as s:
        found: set[UUID] = set()
        for model in (AuthSession, AuthTrustedDevice, AuthActionToken, AuthWebauthnChallenge):
            rows = await s.execute(select(model.id).where(model.org_id == org_id))
            found |= set(rows.scalars())
        return found


GONE = {
    "device_old_free",
    "session_ended_old",
    "session_expired_old",
    "reset_expired_old",
    "recover_used_old",
    "invite_used_old",
    "challenge_used_old",
}


async def test_the_sweep_removes_what_can_never_serve_again_and_nothing_else(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    w = await _world(admin_engine)
    ids = await _seed(admin_engine, w)
    swept = await retention.sweep_org(w.org_id, app_engine, now=NOW)
    left = await _left(admin_engine, w.org_id)
    assert swept == len(GONE)
    assert {name for name, row in ids.items() if row not in left} == GONE


async def test_a_pending_invitation_survives_however_old(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Decision 1: `_open_invites` reads an unused invitation, expired or
    not, as pending; sweeping it would strand the person."""
    w = await _world(admin_engine)
    ids = await _seed(admin_engine, w)
    await retention.sweep_org(w.org_id, app_engine, now=NOW + timedelta(days=3650))
    assert ids["invite_pending_old"] in await _left(admin_engine, w.org_id)


async def test_a_device_a_session_still_names_is_kept(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    w = await _world(admin_engine)
    ids = await _seed(admin_engine, w)
    await retention.sweep_org(w.org_id, app_engine, now=NOW)
    assert ids["device_old_named"] in await _left(admin_engine, w.org_id)


async def test_one_organizations_sweep_leaves_another_alone(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    mine, theirs = await _world(admin_engine), await _world(admin_engine)
    await _seed(admin_engine, mine)
    their_ids = await _seed(admin_engine, theirs)
    await retention.sweep_org(mine.org_id, app_engine, now=NOW)
    assert set(their_ids.values()) <= await _left(admin_engine, theirs.org_id)


def test_the_sweep_runs_daily(tmp_path: Path) -> None:
    from app.worker import create_celery

    settings = Settings(
        database_url="postgresql+asyncpg://x:x@l/x",
        database_admin_url="postgresql+asyncpg://x:x@l/x",
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
        signing_key_path=tmp_path / "job-signing.key",
    )
    with override_settings(settings):
        entry = create_celery().conf.beat_schedule["auth-retention"]
    assert entry["task"] == "app.worker.auth_retention"
    assert entry["schedule"] == retention.SWEEP_INTERVAL_S == 86400.0
```

En `backend/tests/test_worker_sweep_containment.py`, añadir al final de `_DELEGATING_SHELLS` (antes del `]`):

```python
    _Delegating(
        # Looked up on the module at call time (`auth_retention_module.sweep_org`).
        shell="auth_retention_all_orgs",
        target_module="app.core.auth.retention",
        target_attr="sweep_org",
        healthy=1,
        number=int,
        boom=TimeoutError("canceling statement due to statement timeout"),
    ),
```

- [ ] **Step 2: Verlas fallar**

Run: `.venv/bin/pytest -q tests/test_auth_retention.py "tests/test_worker_sweep_containment.py::test_delegating_shell_one_orgs_failure_does_not_abort_the_cycle"`
Expected: FAIL, `ImportError: cannot import name 'retention' from 'app.core.auth'` (y la de contención, `AttributeError: module 'app.worker' has no attribute 'auth_retention_all_orgs'`).

- [ ] **Step 3: El barrido**

Crear `backend/app/core/auth/retention.py`:

```python
"""Retention of own authentication's bookkeeping (final review A-M6; plan
2026-11-06, Task 3, decision 1).

WHAT GOES, AND AFTER HOW LONG:

- a session ended, or past its maximum, more than `RETENTION` ago -- its row
  keeps the address and the browser it was used from;
- a remembered device revoked or expired more than `RETENTION` ago, once no
  remaining session names it (`fk_auth_sessions_device_user` has no ON
  DELETE, and a session started on a device may outlive it);
- a one-time link used more than `RETENTION` ago, or a reset or recovery link
  expired unused more than `RETENTION` ago. AN UNUSED INVITATION IS NEVER
  SWEPT, expired or not: it is the one definition of a pending invitation
  (`user_admin.service._open_invites`), which an administrator resends or
  cancels; swept, the person would be neither invited nor cancellable;
- a WebAuthn challenge used or expired more than `CHALLENGE_RETENTION` ago
  (`passkeys._issue` sweeps those of a person who comes back; this is for
  whoever does not).

WHAT STAYS: everything live, the failure counters (one row per person and
factor), passwords, factors, Microsoft links -- and the audit chain, which is
the record of every act these rows took part in. Nothing is audited here.

ONE ORGANIZATION, EVERY CLIENT. A technician's rows have no client and only
an every-client scope deletes them (migration 0231), so `sweep_org` holds
`ClientScope.all()` (`ALL_SCOPE_ALLOWLIST`), over these four tables only. The
worker runs it daily (`app/worker.py`, `auth-retention`); no person is behind
it.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.auth.action_tokens import INVITE
from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models.auth import AuthActionToken, AuthSession, AuthTrustedDevice, AuthWebauthnChallenge

#: How long an ended session, a forgotten device or a spent link is kept.
RETENTION = timedelta(days=30)
#: How long a used or expired WebAuthn challenge is kept.
CHALLENGE_RETENTION = timedelta(days=1)
#: The beat interval (`app/worker.py`).
SWEEP_INTERVAL_S = 86400.0


@dataclass(frozen=True)
class Swept:
    sessions: int = 0
    devices: int = 0
    links: int = 0
    challenges: int = 0

    @property
    def total(self) -> int:
        return self.sessions + self.devices + self.links + self.challenges


async def _deleted(session: AsyncSession, statement: Any) -> int:
    return len((await session.execute(statement)).all())


async def sweep(session: AsyncSession, *, now: datetime) -> Swept:
    """Delete what decision 1 names, in the caller's session (one
    organization, every client). Sessions first: a device is kept while a
    remaining session names it."""
    cutoff = now - RETENTION
    sessions = await _deleted(
        session,
        delete(AuthSession)
        .where(
            or_(
                AuthSession.ended_at < cutoff,
                and_(AuthSession.ended_at.is_(None), AuthSession.expires_at < cutoff),
            )
        )
        .returning(AuthSession.id),
    )
    named = (
        select(AuthSession.id)
        .where(AuthSession.trusted_device_id == AuthTrustedDevice.id)
        .correlate(AuthTrustedDevice)
        .exists()
    )
    devices = await _deleted(
        session,
        delete(AuthTrustedDevice)
        .where(
            or_(AuthTrustedDevice.revoked_at < cutoff, AuthTrustedDevice.expires_at < cutoff),
            ~named,
        )
        .returning(AuthTrustedDevice.id),
    )
    links = await _deleted(
        session,
        delete(AuthActionToken)
        .where(
            or_(
                AuthActionToken.used_at < cutoff,
                and_(
                    AuthActionToken.used_at.is_(None),
                    AuthActionToken.purpose != INVITE,
                    AuthActionToken.expires_at < cutoff,
                ),
            )
        )
        .returning(AuthActionToken.id),
    )
    stale = now - CHALLENGE_RETENTION
    challenges = await _deleted(
        session,
        delete(AuthWebauthnChallenge)
        .where(
            or_(AuthWebauthnChallenge.used_at < stale, AuthWebauthnChallenge.expires_at < stale)
        )
        .returning(AuthWebauthnChallenge.id),
    )
    return Swept(sessions=sessions, devices=devices, links=links, challenges=challenges)


async def sweep_org(org_id: UUID, engine: AsyncEngine, *, now: datetime | None = None) -> int:
    """One organization's sweep, in one transaction; how many rows went."""
    async with org_session(org_id, engine=engine, clients=ClientScope.all()) as session:
        swept = await sweep(session, now=now or datetime.now(UTC))
    return swept.total
```

- [ ] **Step 4: El worker**

En `backend/app/worker.py`, junto a los demás imports de módulos de barrido (después de `from app.core.ai import sweep as ai_action_sweep_module`):

```python
# Imported as a module, like the sweeps below: the shell looks `sweep_org` up
# on it at call time (`tests/test_worker_sweep_containment.py` patches it there).
from app.core.auth import retention as auth_retention_module
```

Después de `ai_action_expiry_all_orgs` (antes de `remote_recording_retention_all_orgs`):

```python
async def auth_retention_all_orgs(now: datetime | None = None) -> int:
    """Own authentication's retention in every org (`app/core/auth/
    retention.py`, final review A-M6): ended sessions, forgotten devices, spent
    links and old challenges. One org's failure is contained; see
    `_OrgCycleFailures`."""
    settings = get_settings()
    app_engine = create_async_engine(settings.database_url)
    admin_engine = create_async_engine(settings.database_admin_url)
    failures = _OrgCycleFailures("auth.retention")
    total = 0
    try:
        async with AsyncSession(admin_engine) as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())
        for org_id in org_ids:
            try:
                total += await auth_retention_module.sweep_org(org_id, app_engine, now=now)
            except Exception as exc:  # noqa: BLE001 - contained, see `_OrgCycleFailures`
                failures.contain(org_id, exc)
        failures.raise_if_total(len(org_ids))
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()
    return total
```

En `beat_schedule`, después de la entrada `"ai-transcript-retention"`:

```python
        # Own authentication's bookkeeping (final review A-M6): ended sessions,
        # forgotten devices, spent links and old challenges, after 30 days.
        "auth-retention": {
            "task": "app.worker.auth_retention",
            "schedule": auth_retention_module.SWEEP_INTERVAL_S,
        },
```

Y entre las tareas, después de `ai_transcript_retention`:

```python
    @celery_app.task(name="app.worker.auth_retention")  # type: ignore[misc, untyped-decorator, unused-ignore]
    def auth_retention() -> dict[str, int]:
        return {"deleted": asyncio.run(auth_retention_all_orgs())}
```

- [ ] **Step 5: La lista revisada**

En `backend/app/core/client_scope.py`, en `ALL_SCOPE_ALLOWLIST`, después de `"app/core/ai/retention.py::sweep_all_orgs": ...`:

```python
    "app/core/auth/retention.py::sweep_org": (
        "worker: own authentication's retention (final review A-M6) -- one organization's "
        "ended sessions, forgotten devices, spent links and old challenges, whose technician "
        "rows only an every-client scope may delete"
    ),
```

- [ ] **Step 6: Correr las pruebas**

Run: `.venv/bin/pytest -q tests/test_auth_retention.py tests/test_worker_sweep_containment.py tests/test_client_catalog.py tests/test_worker_beat_expiry.py`
Expected: PASS.

- [ ] **Step 7: Tipos y estilo**

Run: `.venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: `Success: no issues found` y `All checks passed!`

- [ ] **Step 8: Commit**

```bash
git add backend/app/core/auth/retention.py backend/app/worker.py backend/app/core/client_scope.py \
  backend/tests/test_auth_retention.py backend/tests/test_worker_sweep_containment.py
git commit -m "feat(auth): daily retention of ended sessions, devices, spent links and challenges (A-M6)"
```

---

### Task 4: Contactos con contraseña — una ruta de inicio de sesión para los dos tipos

Diseño §3: entrada con contraseña de **15+** caracteres y la misma lista de bloqueo, segundo factor opcional; «Olvidé mi contraseña» con enlace de 30 min y la misma respuesta exista o no la cuenta, cerrando las otras sesiones al cambiarla; sesión con sus propios valores (`contact_idle_minutes`, `contact_max_days`, ya en `session_policy.py`); «Un contacto nunca pasa la puerta de la consola; solo la del portal»; mismos límites de intentos y mensajes genéricos. D25, D27, D29, D42. Oleada 2; depende de la Task 1 (`AuthExternalIdentity`, para `methods.py`).

Esta tarea no tiene consola (Task 10) ni claves de acceso sin dirección (Task 6). Al terminarla, un contacto con contraseña entra por `POST /auth/login`, recibe una sesión de tipo `contact` que el portal admite y la consola rechaza con `portal_only`, y puede restablecer su contraseña por correo.

**Files:**
- Modify: `backend/app/core/auth/lookup.py` (`PreIdentity.kind`, `find_person`, docstring)
- Modify: `backend/app/core/auth/guards.py` (`signed_user`)
- Modify: `backend/app/core/auth/passwords.py` (`CONTACT_MINIMUM`)
- Modify: `backend/app/core/auth/staff_password.py` (`minimum_for`, `require_acceptable(minimum=)`)
- Create: `backend/app/core/auth/methods.py`
- Modify: `backend/app/core/auth/flows.py` (`started`, `after_first_factor`, `login`, `send_reset_link`, `reset_password`, `step_up_password`)
- Modify: `backend/app/core/auth/notices.py` (`reset_mail`)
- Modify: `backend/app/core/auth/refusals.py` (`PORTAL_ONLY`)
- Modify: `backend/app/api/deps.py` (`_admit_session`)
- Modify: `backend/app/core/auth/sessions.py` (`SessionPrincipal.timezone`, leído en `resolve`)
- Modify: `backend/app/api/v1/auth.py` (`StepUpPasswordIn`, `POST /auth/step-up/password`)
- Modify: `backend/app/modules/portal/principal.py` (`portal_permissions`), `backend/app/modules/portal/schema.py` (`PortalMeOut`), `backend/app/modules/portal/api.py` (`GET /portal/me`)
- Modify: `backend/app/mcp/exclusions.py` (`NEVER_EXPOSED_REASONS` +1, `NOT_FOR_MCP` +1)
- Modify: `backend/app/core/client_scope.py` (la razón de `flows.py::_identity_session`)
- Modify: `backend/app/core/i18n/es.py`
- Modify: `backend/tests/auth_support.py` (`CONTACT_PASSWORD`, `contact`, `Staff.client_id`)
- Modify: `backend/tests/test_mcp_channel_api.py`, `backend/tests/test_route_gating.py`, `backend/tests/test_ai_tool_surface.py`, `backend/tests/test_auth_session_door.py`
- Create: `backend/tests/test_auth_contacts_signin.py`, `backend/tests/test_portal_me.py`
- Test: los nuevos, `backend/tests/test_auth_signin.py`, `backend/tests/test_auth_password_reset.py`, `backend/tests/test_auth_falsifiers.py`, `backend/tests/test_i18n_catalog.py`, `backend/tests/test_portal_api.py`

**Interfaces:**
- Consumes: `its_auth_principal` (0245, técnico primero); `AuthExternalIdentity` (Task 1); `failures.check_allowed`/`record_success`, `factors.gate`/`count_failure`/`summary`/`lock_person`, `devices.presented`/`recognised`, `sessions.start_session`/`resumable`/`mark_step_up`, `action_tokens.issue`/`consume`, `notices.deliver`/`tell` (fase 1); `principal._holds_within` (`app/modules/portal/principal.py:59-66`).
- Produces (Apéndice A, Task 4): `find_person`, `PreIdentity.kind`, `minimum_for`, `require_acceptable(..., minimum=)`, `CONTACT_MINIMUM`, `methods.has_password`, `methods.has_sign_in_method`, `flows.started`, `flows.after_first_factor(..., method=)`, `flows.step_up_password`, `notices.reset_mail(link, *, second_factor)`, `PORTAL_ONLY`, `GET /portal/me` → `PortalMeOut` (con `timezone`), `principal.portal_permissions`, `SessionPrincipal.timezone`.

**Decisión del plan (1): la regla por tipo vive en el flujo, no en una ruta nueva** (D25). `login`, `send_reset_link` y `reset_password` buscan a la persona con `find_person` y aplican la regla de su tipo: la longitud mínima (`minimum_for`), si hace falta un segundo factor para enviar un restablecimiento (solo técnicos, §2.8) y qué pasa cuando no hay segundo factor (técnico: `enroll_required`; contacto: `signed_in`, D27). Todo lo demás —contadores por cuenta y por equipo, espera, bloqueo a los 100, el mismo trabajo de argon2 para quien no existe, el 401 genérico— es el de la fase 1, sin cambio.

**Decisión del plan (2): `_started` y `_after_password` pasan a ser públicas** (`started`, `after_first_factor`), porque la Task 9 (Microsoft) entra por el mismo camino después de su primer factor. `after_first_factor` recibe `method` (`"password"`, y `"microsoft"` en la Task 9), que va a la auditoría `auth.signed_in`.

**Decisión del plan (3): a quién se envía un restablecimiento.** Un técnico: como en la fase 1 (con contraseña y con segundo factor, §2.8, C-I1). Un contacto: si ya aceptó su invitación, es decir, si tiene alguna forma de entrar (`methods.has_sign_in_method`: contraseña, clave de acceso o Microsoft), con o sin contraseña (Pregunta P5): para un contacto el buzón ya es la raíz de la cuenta (lo invitó), y el segundo factor, si lo activó, se sigue pidiendo. A un contacto que nunca aceptó no se le envía nada: su camino es la invitación. El correo dice «necesitarás tu segundo factor» solo cuando la persona tiene uno.

**Decisión del plan (4): la consola responde `portal_only` y deja de registrar esa negativa en el log.** `_admit_session` sigue rechazando a un contacto con 403, ahora `AuthRefusal(code="portal_only")` para que la consola sepa llevarlo al portal (D42, Task 10). Ya no llama a `log_admission_refusal` para esa razón: desde esta fase un contacto en la puerta de la consola es lo esperado (la consola pregunta `GET /me` en cada carga), y una línea WARNING por página sería ruido que taparía las negativas de verdad (dominio y estado), que se siguen registrando. El recuento de sitios de log no cambia: es la misma llamada.

**Decisión del plan (5): `GET /portal/me` lee a la persona de la sesión que la admitió** (`request.state.session_principal`, que `deps._session_principal` deja ahí): el módulo del portal solo abre `portal_session` (`tests/test_portal_api.py` lo exige) y `portal_user` no lee `users`. Los códigos salen de `person_scope` dentro del alcance del contexto, como la puerta del portal. Se protege con `require_portal_permission("portal.ticket.read")` (todo contacto lo tiene: rol `requester`) y no es herramienta MCP (`NOT_FOR_MCP`): en el canal MCP no hay sesión de navegador.

- [ ] **Step 1: Los ayudantes de prueba del contacto**

En `backend/tests/auth_support.py`:
- añadir `from uuid import UUID, uuid4` (hoy importa `UUID`), y `Client, Role, UserRole` al `from app.models import User`;
- añadir `"CONTACT_PASSWORD"` y `"contact"` a `__all__`;
- en `class Staff`, al final de los campos: `client_id: UUID | None = None`;
- después de `staff(...)`:

```python
#: A contact's password: 15 characters at least (design §3), not on the
#: blocklist, and free of every test address's words and the product's name.
CONTACT_PASSWORD = "Ultramarine-Kettle-7319-Harbor"


async def contact(env: Any, org_id: UUID, *, password: str | None = CONTACT_PASSWORD) -> Staff:
    """A contact of a new client, holding the portal role (`requester`) on that
    client only (design §3), and -- unless `password` is None, an invitation
    not accepted yet -- a password. The same `Staff` bag the technician
    helpers use: `sub` is the contact's `external_subject`."""
    async with AsyncSession(env.admin_engine, expire_on_commit=False) as session:
        client = Client(org_id=org_id, name=f"Acme-{secrets.token_hex(3)}", kind="customer")
        session.add(client)
        await session.flush()
        user = User(
            org_id=org_id,
            kind="contact",
            client_id=client.id,
            email=f"{secrets.token_hex(4)}@acme.test",
            external_subject=f"contact-pending:{uuid4()}",
        )
        session.add(user)
        await session.flush()
        role = (
            await session.execute(
                select(Role).where(Role.name == "requester", Role.org_id.is_(None))
            )
        ).scalar_one()
        session.add(UserRole(org_id=org_id, user_id=user.id, role_id=role.id, client_id=client.id))
        if password is not None:
            digest, version = await hash_password(org_id, password)
            session.add(
                AuthPassword(
                    user_id=user.id,
                    org_id=org_id,
                    hash=digest,
                    pepper_version=version,
                    changed_at=datetime.now(UTC),
                )
            )
        await session.commit()
    return Staff(
        org_id,
        user.id,
        user.external_subject,
        user.email,
        password or "",
        browser(),
        client_id=client.id,
    )
```

- [ ] **Step 2: Escribir las pruebas del inicio de sesión del contacto**

Crear `backend/tests/test_auth_contacts_signin.py`:

```python
"""A client's contact signs in with the platform's own sign-in (own
authentication phase 2, Task 4; design §3).

- One route for both kinds (plan decision D25): `POST /auth/login` signs a
  contact in; with no second factor that is the whole sign-in (D27), and the
  session is a contact's -- the portal's door admits it (`GET /portal/me`)
  and the console's answers 403 `portal_only` (falsifier 1, D42).
- A contact who set up an authenticator app is asked for it, as a
  technician is.
- A wrong password is the one generic 401, counted per account (§2.3).
- The reset link holds the contact's rule: 15 characters; the other sessions
  end; the mail does not promise a second factor the contact does not have;
  a contact who never accepted an invitation is sent nothing (decision 3).
- A contact may confirm with the password (D29); a technician may not.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pyotp
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.sessions import SESSION_COOKIE
from app.core.auth.totp import step_of
from app.models import AuditEvent
from app.models.auth import AuthFailure, AuthSession, AuthTotp
from tests.auth_support import (
    CONTACT_PASSWORD,
    PASSWORD,
    capture_mail,
    console_ready,
    contact,
    cookie,
    link_token,
    login,
    point_at,
    signed_in,
)


@pytest.fixture()
def auth_env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    console_ready(api_env, monkeypatch)
    api_env.mail = capture_mail(monkeypatch)
    return api_env


async def _signed_in_contact(env: Any, **kwargs: Any) -> Any:
    org = await env.create_org("contact")
    who = await contact(env, org, **kwargs)
    await point_at(env, org)
    return who


async def test_a_contact_signs_in_with_the_password_alone_and_only_the_portal_lets_it_in(
    auth_env: Any,
) -> None:
    who = await _signed_in_contact(auth_env)
    answer = await login(auth_env, who)
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"state": "signed_in"}
    assert who.session is not None

    portal = await auth_env.client.get("/api/v1/portal/me", headers=who.headers())
    assert portal.status_code == 200, portal.text
    assert portal.json() == {
        "user_id": str(who.user_id),
        "kind": "contact",
        "email": who.email,
        "display_name": "",
        "timezone": None,
        "permissions": ["portal.ticket.read", "portal.ticket.write"],
    }
    console = await auth_env.client.get("/api/v1/me", headers=who.headers())
    assert console.status_code == 403
    assert console.json()["code"] == "portal_only"

    async with AsyncSession(auth_env.admin_engine) as s:
        kinds = set(
            (
                await s.execute(
                    select(AuthSession.kind).where(
                        AuthSession.user_id == who.user_id, AuthSession.ended_at.is_(None)
                    )
                )
            ).scalars()
        )
        audited = (
            await s.execute(
                select(AuditEvent.meta).where(
                    AuditEvent.org_id == who.org_id, AuditEvent.action == "auth.signed_in"
                )
            )
        ).scalars().all()
    assert kinds == {"contact"}
    assert list(audited) == [{"method": "password", "trusted_device": False}]


async def test_a_contact_with_an_authenticator_app_is_asked_for_it(auth_env: Any) -> None:
    who = await _signed_in_contact(auth_env)
    await login(auth_env, who)
    start = await auth_env.client.post("/api/v1/auth/enroll/totp/start", headers=who.headers())
    assert start.status_code == 200, start.text
    secret = parse_qs(urlsplit(start.json()["otpauth_uri"]).query)["secret"][0]
    confirm = await auth_env.client.post(
        "/api/v1/auth/enroll/totp/confirm",
        headers=who.headers(),
        json={"code": pyotp.TOTP(secret).now()},
    )
    assert confirm.status_code == 204, confirm.text
    await auth_env.client.post("/api/v1/auth/logout", headers=who.headers())
    who.session = None

    again = await login(auth_env, who)
    assert again.json() == {
        "state": "factor_required",
        "methods": ["totp"],
        "remember_device_days": 15,
    }
    # The code above was accepted for this step: the next one is a step on.
    async with AsyncSession(auth_env.admin_engine) as s:
        await s.execute(
            update(AuthTotp)
            .where(AuthTotp.user_id == who.user_id)
            .values(last_step=step_of(datetime.now(UTC)) - 2)
        )
        await s.commit()
    factor = await auth_env.client.post(
        "/api/v1/auth/factor/totp",
        headers=who.headers(),
        json={"code": pyotp.TOTP(secret).now(), "remember_device": False},
    )
    assert factor.status_code == 200, factor.text
    assert factor.json() == {"state": "signed_in"}


async def test_a_contacts_wrong_password_is_the_generic_answer_and_is_counted(
    auth_env: Any,
) -> None:
    who = await _signed_in_contact(auth_env)
    answer = await login(auth_env, who, password="Not-The-Password-41-Harbor")
    assert answer.status_code == 401
    assert answer.json()["code"] == "invalid_credentials"
    assert cookie(answer, SESSION_COOKIE) is None
    async with AsyncSession(auth_env.admin_engine) as s:
        consecutive = await s.scalar(
            select(AuthFailure.consecutive).where(
                AuthFailure.user_id == who.user_id, AuthFailure.factor == "password"
            )
        )
    assert consecutive == 1


async def test_a_contacts_reset_link_holds_the_contact_rule_and_ends_the_other_sessions(
    auth_env: Any,
) -> None:
    who = await _signed_in_contact(auth_env)
    await login(auth_env, who)
    old_session = who.session

    sent = await auth_env.client.post(
        "/api/v1/auth/password/forgot", headers=who.headers(), json={"email": who.email}
    )
    assert sent.status_code == 202
    [mail] = auth_env.mail.sent
    assert mail.to == who.email
    assert "second factor" not in mail.text
    token = link_token(mail.text)

    short = await auth_env.client.post(
        "/api/v1/auth/password/reset",
        headers=who.network,
        json={"token": token, "password": "Twelve-chars-x"},
    )
    assert short.status_code == 422
    assert short.json()["code"] == "password_rejected"

    done = await auth_env.client.post(
        "/api/v1/auth/password/reset",
        headers=who.network,
        json={"token": token, "password": "Another-Harbor-Kettle-8842"},
    )
    assert done.status_code == 204, done.text
    gone = await auth_env.client.get(
        "/api/v1/portal/me", headers={**who.network, "Cookie": f"{SESSION_COOKIE}={old_session}"}
    )
    assert gone.status_code == 401
    who.session = None
    assert (await login(auth_env, who, password="Another-Harbor-Kettle-8842")).json() == {
        "state": "signed_in"
    }


async def test_a_contact_who_never_accepted_is_sent_nothing(auth_env: Any) -> None:
    who = await _signed_in_contact(auth_env, password=None)
    sent = await auth_env.client.post(
        "/api/v1/auth/password/forgot", headers=who.network, json={"email": who.email}
    )
    assert sent.status_code == 202
    assert auth_env.mail.sent == []


async def test_a_contact_confirms_with_the_password_and_a_technician_cannot(
    auth_env: Any,
) -> None:
    who = await _signed_in_contact(auth_env)
    await login(auth_env, who)
    stale = datetime.now(UTC) - timedelta(minutes=30)
    async with AsyncSession(auth_env.admin_engine) as s:
        await s.execute(
            update(AuthSession).where(AuthSession.user_id == who.user_id).values(step_up_at=stale)
        )
        await s.commit()

    wrong = await auth_env.client.post(
        "/api/v1/auth/step-up/password",
        headers=who.headers(),
        json={"password": "Not-The-Password-41-Harbor"},
    )
    assert wrong.status_code == 400
    assert wrong.json()["code"] == "factor_failed"
    right = await auth_env.client.post(
        "/api/v1/auth/step-up/password",
        headers=who.headers(),
        json={"password": CONTACT_PASSWORD},
    )
    assert right.status_code == 204, right.text
    async with AsyncSession(auth_env.admin_engine) as s:
        fresh = await s.scalar(
            select(AuthSession.step_up_at).where(
                AuthSession.user_id == who.user_id, AuthSession.ended_at.is_(None)
            )
        )
    assert fresh is not None and fresh > stale

    tech = await signed_in(auth_env, who.org_id, "technician")
    refused = await auth_env.client.post(
        "/api/v1/auth/step-up/password", headers=tech.headers(), json={"password": PASSWORD}
    )
    assert refused.status_code == 409
    assert refused.json()["code"] == "method_unavailable"
```

Crear `backend/tests/test_portal_me.py`:

```python
"""`GET /portal/me` (own authentication phase 2, Task 4, decision 5): the
person using the portal, for the portal's own pages -- a technician with a
portal code as much as a contact -- and nothing for somebody without one."""

from typing import Any

API = "/api/v1/portal/me"


async def test_a_technician_holding_the_portal_role_is_named(api_env: Any) -> None:
    org_id = await api_env.create_org("portal-me")
    sub = await api_env.create_user(org_id, "requester")
    got = await api_env.client.get(API, headers=await api_env.headers(org_id, sub))
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["kind"] == "staff"
    assert body["email"] == f"{sub}@test.local"
    assert body["permissions"] == ["portal.ticket.read", "portal.ticket.write"]


async def test_somebody_without_a_portal_code_gets_nothing(api_env: Any) -> None:
    org_id = await api_env.create_org("portal-me-no")
    sub = await api_env.create_user(org_id, "technician")
    got = await api_env.client.get(API, headers=await api_env.headers(org_id, sub))
    assert got.status_code == 403


async def test_without_a_session_it_is_401(api_env: Any) -> None:
    assert (await api_env.client.get(API)).status_code == 401
```

- [ ] **Step 3: Verlas fallar**

Run: `.venv/bin/pytest -q tests/test_auth_contacts_signin.py tests/test_portal_me.py`
Expected: FAIL. `test_a_contact_signs_in_...` con `401` en lugar de `200` (el login busca solo técnicos); `test_portal_me.py` con `404` (no hay ruta).

- [ ] **Step 4: Buscar a la persona, de cualquier tipo**

En `backend/app/core/auth/lookup.py`, reemplazar el párrafo «ONLY THE TECHNICIAN» del docstring por:

```
TECHNICIAN FIRST. The function may answer two rows for one address (a
technician and a contact of a client share it; staff first, 0245).
`find_staff` keeps the staff row -- the recovery command is a technician's.
`find_person` takes the first row of either kind (own authentication phase 2,
plan decision D25): the one sign-in route for technicians and contacts.
```

y `PreIdentity` y la función nueva:

```python
@dataclass(frozen=True)
class PreIdentity:
    user_id: UUID
    org_id: UUID
    status: str
    #: `users.kind`: `staff` or `contact` (phase 2).
    kind: str = STAFF_KIND


async def find_person(engine: AsyncEngine, *, org_slug: str, email: str) -> PreIdentity | None:
    """The person an address names in one organization: the technician when
    there is one, else the contact (plan decision D25). The order is said
    here, not left to the function's own ORDER BY."""
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT user_id, org_id, kind, status FROM its_auth_principal(:slug, :email) "
                    "ORDER BY (kind = :staff) DESC, user_id LIMIT 1"
                ),
                {"slug": org_slug, "email": email.strip().lower(), "staff": STAFF_KIND},
            )
        ).first()
    if row is None:
        return None
    return PreIdentity(user_id=row.user_id, org_id=row.org_id, status=row.status, kind=row.kind)
```

- [ ] **Step 5: La persona de la sesión, de su tipo**

En `backend/app/core/auth/guards.py`, cambiar `from app.core.identity import ACTIVE_STATUS, STAFF_KIND` por `from app.core.identity import ACTIVE_STATUS, CONTACT_KIND, STAFF_KIND` y reemplazar `signed_user`:

```python
async def signed_user(session: AsyncSession, principal: SessionPrincipal) -> User:
    """The session's person, still active and of the session's own kind: a
    technician or, since own authentication phase 2, a client's contact (plan
    decision D25). `its_auth_session` already refuses a session whose kind is
    not its person's; this reads the same rule again in the caller's
    transaction, where a disabled person or a kind changed since is caught."""
    user = (
        await session.execute(
            select(User).where(User.id == principal.user_id, User.org_id == principal.org_id)
        )
    ).scalar_one_or_none()
    if (
        user is None
        or user.kind != principal.kind
        or user.kind not in (STAFF_KIND, CONTACT_KIND)
        or user.status != ACTIVE_STATUS
    ):
        raise sign_in_again()
    return user
```

- [ ] **Step 6: La longitud mínima por tipo y las formas de entrar**

En `backend/app/core/auth/passwords.py`, debajo de `STAFF_MINIMUM = 12`:

```python
#: A contact's minimum (design §3): a contact's second factor is optional, and
#: NIST SP 800-63B-4 §3.1.1.2 asks 15 of a password used alone [S1].
CONTACT_MINIMUM = 15
```

En `backend/app/core/auth/staff_password.py`: importar `CONTACT_MINIMUM` junto a `STAFF_MINIMUM` y `from app.core.identity import CONTACT_KIND`; en el docstring del módulo, cambiar la primera línea por «A person's password: the rule of design §2.1 (§3 for a contact) and where the hash goes.»; añadir después de `SUBMITTED_MAX`:

```python
def minimum_for(kind: str) -> int:
    """The shortest password a person of `kind` may choose: 12 for a
    technician, who always has a second factor (design §2.1); 15 for a
    contact, whose second factor is optional (design §3)."""
    return CONTACT_MINIMUM if kind == CONTACT_KIND else STAFF_MINIMUM
```

y cambiar la firma y la llamada de `require_acceptable`:

```python
async def require_acceptable(
    plain: str, *, email: str, display_name: str, org_name: str, minimum: int = STAFF_MINIMUM
) -> None:
    """The rule of design §2.1 (§3 for a contact: `minimum_for`), or a 422
    `password_rejected` whose `detail` says why. The context is the address,
    the organization's name and the person's name."""
    try:
        await asyncio.to_thread(
            check_new_password,
            plain,
            minimum=minimum,
            context=context_words(email, org_name, display_name),
        )
    except PasswordRejected as exc:
        raise AuthRefusal(
            status.HTTP_422_UNPROCESSABLE_CONTENT, code="password_rejected", detail=exc.message
        ) from exc
```

Crear `backend/app/core/auth/methods.py`:

```python
"""The ways into an account (own authentication phase 2, plan decisions D25,
D27, D40).

A WAY IN is what can start a sign-in on its own: a password, a passkey used
without an address (a contact's, D28), or a linked Microsoft account (Task 9).
For a technician a passkey is a second factor, but a technician holds one only
after having signed in, so "has this person a way in" -- "have they accepted
their invitation" -- has the same answer for both kinds.

Read in the caller's session (one organization, every client: a technician's
rows are global, a contact's are their client's, migration 0231).
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import AuthExternalIdentity, AuthPasskey, AuthPassword


async def has_password(session: AsyncSession, user_id: UUID) -> bool:
    found = await session.scalar(
        select(AuthPassword.user_id).where(AuthPassword.user_id == user_id)
    )
    return found is not None


async def has_sign_in_method(session: AsyncSession, user_id: UUID) -> bool:
    """A password, a passkey or a Microsoft link."""
    if await has_password(session, user_id):
        return True
    for column in (AuthPasskey.user_id, AuthExternalIdentity.user_id):
        if await session.scalar(select(column).where(column == user_id).limit(1)) is not None:
            return True
    return False
```

- [ ] **Step 7: Los flujos**

En `backend/app/core/auth/flows.py`:

1. Imports: `from app.core.auth.lookup import find_person` (en lugar de `find_staff`, que este archivo ya no usa); `from app.core.auth.methods import has_sign_in_method`; `from app.core.identity import ACTIVE_STATUS, CONTACT_KIND, STAFF_KIND`.
2. Renombrar en todo el archivo: `sed -i 's/\b_started(/started(/g; s/\b_after_password(/after_first_factor(/g' app/core/auth/flows.py`, y comprobar con `grep -n "_started\|_after_password" app/core/auth/flows.py` (sin salida).
3. Reemplazar las definiciones de `started` y `after_first_factor`:

```python
async def started(
    session: AsyncSession,
    request: Request,
    user: User,
    *,
    state: Literal["factor_required", "enroll_required", "signed_in"],
    factor_done: bool = False,
    device: AuthTrustedDevice | None = None,
    resume: AuthSession | None = None,
    methods: tuple[str, ...] = (),
    remember_device_days: int | None = None,
) -> SignIn:
    """A new session for `user`, of the person's own kind: a contact's is a
    contact session, which only the portal's door admits (design §3). Public
    since phase 2 (plan decision 2): Microsoft's sign-in starts one too."""
    record, token = await start_session(
        session,
        user,
        kind=user.kind,
        request=request,
        factor_done=factor_done,
        trusted_device_id=device.id if device is not None else None,
        resume=resume,
    )
    return SignIn(
        state=state,
        methods=methods,
        remember_device_days=remember_device_days,
        session_token=token,
        expires_at=record.expires_at,
    )
```

```python
async def after_first_factor(
    session: AsyncSession, request: Request, user: User, *, method: str = "password"
) -> SignIn:
    """The first factor is right -- the password, or (phase 2, Task 9) a
    linked Microsoft account, named by `method` in the audit. In order: an
    idle session of this browser is resumed with it alone (design §2.4, Task
    6's `resumable`); a person with no second factor enrols one -- or, a
    contact, whose second factor is optional, is signed in (design §3, plan
    decision D27); a remembered device stands in for the factor (§2.6);
    everybody else is asked for it."""
    idle = await resumable(session, request, user.id)
    if idle is not None:
        await _audit(session, user, "auth.signed_in", {"method": "resume", "trusted_device": False})
        return await started(session, request, user, state="signed_in", resume=idle)
    has_passkey, has_totp, codes = await summary(session, user.id)
    if not (has_passkey or has_totp):
        if user.kind == CONTACT_KIND:
            await _audit(
                session, user, "auth.signed_in", {"method": method, "trusted_device": False}
            )
            return await started(session, request, user, state="signed_in", factor_done=True)
        return await started(session, request, user, state="enroll_required")
    device = await recognised(session, user.id, request)
    if device is not None:
        await _audit(session, user, "auth.signed_in", {"method": method, "trusted_device": True})
        return await started(
            session, request, user, state="signed_in", factor_done=True, device=device
        )
    offered = (("passkey", has_passkey), ("totp", has_totp), ("recovery", codes > 0))
    policy = await read_policy(session, user.org_id)
    return await started(
        session,
        request,
        user,
        state="factor_required",
        methods=tuple(name for name, available in offered if available),
        remember_device_days=(
            policy.remember_device_days if policy.remember_device_enabled else None
        ),
    )
```

4. En `login`, cambiar `found = await find_staff(_engine(request), org_slug=slug, email=email)` por `found = await find_person(_engine(request), org_slug=slug, email=email)`. Nada más cambia en `login`: un contacto sin contraseña cae en `untried` y recibe el mismo trabajo de argon2 y el mismo 401.

5. Reemplazar `_has_second_factor` y `send_reset_link` por:

```python
async def _has_second_factor(session: AsyncSession, user_id: UUID) -> bool:
    """A passkey or a CONFIRMED authenticator app. Recovery codes alone do
    not count: they are a fallback of a factor, not one (design §2.2)."""
    has_passkey, has_totp, _codes = await summary(session, user_id)
    return has_passkey or has_totp


async def _may_be_sent_a_reset(session: AsyncSession, user: User) -> bool:
    """Plan decision 3. A technician: a password and a second factor (final
    review C-I1, design §2.8). A contact: any way in, with or without a
    password -- the mailbox invited them, and a second factor they turned on
    is still asked for (design §3; operator question P5)."""
    if user.kind == STAFF_KIND:
        return await session.get(AuthPassword, user.id) is not None and await _has_second_factor(
            session, user.id
        )
    return user.kind == CONTACT_KIND and await has_sign_in_method(session, user.id)


async def send_reset_link(app: FastAPI, *, email: str, locale: str) -> None:
    """Run AFTER the 202 (plan decision 8): nothing below changes the answer
    or its time, so nothing tells whether the address has an account -- or
    which kind.

    WHO IS SENT ONE is `_may_be_sent_a_reset`: never a technician without a
    second factor (final review C-I1; Q5, "nunca solo por correo"), never a
    contact who has not accepted an invitation. The mail promises the second
    factor only to somebody who has one."""
    with use_locale(locale):
        settings = app.state.settings
        engine: AsyncEngine = app.state.app_engine
        if not settings.default_org_slug or not settings.public_console_url:
            return
        address = email.strip().lower()
        per_address = sha256(f"{settings.default_org_slug}|{address}".encode()).hexdigest()
        if not await hit(
            app.state.redis,
            f"auth-forgot:address:{per_address}",
            limit=RESET_MAILS_PER_HOUR,
            window_s=3600,
            # The only cap on the mails one address is sent (final review
            # A-M1): during a Redis outage none goes. After the 202, so the
            # answer does not change.
            fail_closed=True,
        ):
            return
        found = await find_person(engine, org_slug=settings.default_org_slug, email=address)
        if found is None or found.status != ACTIVE_STATUS:
            return
        async with _identity_session(found.org_id, engine) as session:
            user = await session.get(User, found.user_id)
            if user is None or not await _may_be_sent_a_reset(session, user):
                return
            second_factor = await _has_second_factor(session, user.id)
            token = await action_tokens.issue(
                session,
                org_id=user.org_id,
                user_id=user.id,
                purpose=action_tokens.RESET,
                ttl=action_tokens.RESET_TTL,
                created_by=None,
            )
            with use_locale(user.locale or locale):
                subject, text = notices.reset_mail(
                    action_tokens.link(settings.public_console_url, "reset-password", token),
                    second_factor=second_factor,
                )
            await notices.deliver(
                session,
                org_id=user.org_id,
                to=user.email,
                subject=subject,
                text=text,
                vault=app.state.vault,
                actor_id=None,
            )
```

6. En `reset_password`, cambiar la condición del 410 y la del enlace muerto, y la longitud:

```python
        row = await action_tokens.consume(
            session, token=token, purposes=(action_tokens.RESET,)
        )
        user = await session.get(User, row.user_id) if row is not None else None
        if (
            row is None
            or user is None
            or user.kind not in (STAFF_KIND, CONTACT_KIND)
            or user.status != ACTIVE_STATUS
        ):
            raise _invalid_link()
        # One lock with an administrator's factor reset (`factors.
        # lock_person`, item 8): the factors read below, and the mark the store
        # writes, cannot interleave with a reset that removes the factors.
        await lock_person(session, user.id)
        if not await _may_still_reset(session, user):
            # A link sent while the account could be reset, used after that
            # stopped being true (an administrator's factor reset; a contact's
            # access reset by a technician, Task 8). SPENT, then refused like
            # every dead link (re-review Minor 5): the `consume` commits with
            # the block and the 410 is raised after it.
            spent_dead = True
        else:
            # Raises a 422 that rolls the `consume` back: the link stays usable.
            await staff_password.require_acceptable(
                password,
                email=user.email,
                display_name=user.display_name,
                org_name=await staff_password.organization_name(session, org_id),
                minimum=staff_password.minimum_for(user.kind),
            )
```

(el resto del bloque —`store(..., by_mail=True)`, `reset_factor`, `end_all_for_user`, `revoke_all_for_user`, la auditoría y el aviso— queda igual), y añadir encima de `reset_password`:

```python
async def _may_still_reset(session: AsyncSession, user: User) -> bool:
    """At the link's use, under `lock_person`: a technician still has a second
    factor (§2.8); a contact still has a way in (plan decision 3)."""
    if user.kind == STAFF_KIND:
        return await _has_second_factor(session, user.id)
    return await has_sign_in_method(session, user.id)
```

7. Añadir, después de `step_up_passkey`:

```python
async def step_up_password(request: Request, principal: SessionPrincipal, *, password: str) -> None:
    """A contact confirms with the password (plan decision D29; operator
    question P13): a contact's second factor is optional, and the password
    is what one without it has. A technician confirms with a second factor
    only (design §2.5): 409 `method_unavailable`, as for a factor the account
    does not have. The password's own counters and wait (design §2.3), and
    the rule of `login` for an over-long one (plan decision 16)."""
    require_full(principal)
    accepted = False
    async with _identity_session(principal.org_id, _engine(request)) as session:
        user = await signed_user(session, principal)
        stored = await session.get(AuthPassword, user.id)
        if user.kind != CONTACT_KIND or stored is None:
            raise _unavailable()
        device = await presented(session, user.id, request)
        counter = device.id if device is not None else None
        # Raised inside the block: nothing is written before it.
        await gate(session, user.id, "password", device_id=counter)
        matches: bool | None = False
        if password and len(password) <= staff_password.SUBMITTED_MAX:
            matches = await _password_matches(user.org_id, password, stored)
        if matches:
            await record_success(session, user.id, "password")
            await _stepped_up(session, principal)
            accepted = True
        elif matches is False:
            await count_failure(
                session, user, "password", vault=_vault(request), device_id=counter
            )
    if not accepted:
        raise _factor_failed(status.HTTP_400_BAD_REQUEST)
```

En `backend/app/core/auth/notices.py`, reemplazar `reset_mail`:

```python
def reset_mail(link: str, *, second_factor: bool) -> tuple[str, str]:
    """The reset link (design §2.8, §3). "You will still need your second
    factor" only for somebody who has one (plan decision 3)."""
    subject = tr("Reset your {product} password", product=PRODUCT)
    if second_factor:
        return subject, tr(
            "Someone asked to reset the password of your {product} account.\n\n"
            "Open this link within 30 minutes to choose a new one. You will still need your "
            "second factor to sign in.\n{link}\n\n"
            "If it was not you, ignore this message: your password has not changed.",
            product=PRODUCT,
            link=link,
        )
    return subject, tr(
        "Someone asked to reset the password of your {product} account.\n\n"
        "Open this link within 30 minutes to choose a new one.\n{link}\n\n"
        "If it was not you, ignore this message: your password has not changed.",
        product=PRODUCT,
        link=link,
    )
```

- [ ] **Step 8: La ruta de confirmación con la contraseña**

En `backend/app/api/v1/auth.py`, añadir la línea `POST   /auth/step-up/password            a contact's password (design §3)` al índice del docstring, debajo de `/auth/step-up/passkey`; el modelo, junto a `StepUpRecoveryIn`:

```python
class StepUpPasswordIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    #: A secret: no Pydantic bound (decision 16); `flows.step_up_password`
    #: holds it to `SUBMITTED_MAX`.
    password: str
```

y la ruta, después de `step_up_passkey`:

```python
@router.post(
    "/step-up/password", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(_FACTOR)]
)
async def step_up_password(body: StepUpPasswordIn, request: Request) -> Response:
    """A contact confirms with the password, for ten minutes (plan decision
    D29). A technician confirms with a second factor (409 `method_unavailable`)."""
    await flows.step_up_password(request, await session_of(request), password=body.password)
    return _no_content()
```

- [ ] **Step 9: `portal_only` en la puerta de la consola**

En `backend/app/core/auth/refusals.py`, junto a `CSRF = "csrf"`:

```python
#: 403: the person is a client's contact, whom only the portal admits (design
#: §3; plan decision D42). The console answers it by going to the portal.
PORTAL_ONLY = "portal_only"
```

En `backend/app/api/deps.py`, importar `PORTAL_ONLY` junto a `AUTH_REQUIRED`, y en `_admit_session` reemplazar el bloque desde `who = f"user:{user.id}"` hasta el final del bucle `for reason in reasons:` por:

```python
        who = f"user:{user.id}"
        domain_reason = (
            None
            if user.kind == CONTACT_KIND
            else domain_refusal(
                user.email, await resolve_console_domain(session, principal.org_id, settings)
            )
        )
        for reason in (domain_reason, status_refusal(user.status)):
            if reason is not None:
                log_admission_refusal("session", sub=who, email=user.email, reason=reason)
                raise HTTPException(status.HTTP_403_FORBIDDEN, tr_marked(reason))
        if not contacts:
            reason = contact_refusal(user.kind)
            if reason is not None:
                # Not logged (plan 2026-11-06, Task 4, decision 4): a contact at
                # the console's door is expected since phase 2 -- the console
                # asks `/me` on every page -- and the console answers it by
                # going to the portal (D42).
                raise AuthRefusal(
                    status.HTTP_403_FORBIDDEN, code=PORTAL_ONLY, detail=tr_marked(reason)
                )
```

En el docstring de `_admit_session`, cambiar «a contact refused unless `contacts`» por «a contact refused unless `contacts` -- with 403 `portal_only`, not logged (phase 2, Task 4)».

En `backend/tests/test_auth_session_door.py::test_a_contact_session_never_passes_the_console_door`, después de la aserción del `detail`, añadir `assert response.json()["code"] == "portal_only"`.

- [ ] **Step 10: `GET /portal/me`**

En `backend/app/modules/portal/schema.py`, al final:

```python
class PortalMeOut(BaseModel):
    """Who is using the portal, for the portal's own pages (own authentication
    phase 2, Task 4): a client's contact, or a technician holding a portal
    code. Read from the session that admitted the request."""

    user_id: UUID
    kind: Literal["staff", "contact"]
    email: str
    display_name: str
    timezone: str | None = Field(description="The person's saved time zone, or null.")
    permissions: list[str] = Field(
        description="The portal codes held: portal.ticket.read and/or portal.ticket.write."
    )
```

(con `from typing import Literal` y `from pydantic import Field` si faltan).

En `backend/app/modules/portal/principal.py`, después de `resolve_portal_principal`:

```python
async def portal_permissions(request: Request, ctx: PortalContext) -> list[str]:
    """The portal codes the person holds inside the request's scope, in
    `PORTAL_CODES` order: what the portal's pages offer (invariant 6: the
    server still checks every route)."""
    held: list[str] = []
    for code in PORTAL_CODES:
        within = await _holds_within(request, ctx.org_id, ctx.requester_user_id, code, ctx.clients)
        if not within.is_empty:
            held.append(code)
    return held
```

En `backend/app/modules/portal/api.py`, importar `PortalMeOut` y `portal_permissions`, y añadir antes de `GET /portal/categories`:

```python
# --------------------------------------------------------------------------- #
# GET /portal/me -- the person using the portal (own authentication phase 2,
# Task 4, decision 5). No database read: the session that admitted the
# request names the person, and `person_scope` gives the codes.
# --------------------------------------------------------------------------- #


@router.get("/portal/me", response_model=PortalMeOut)
async def portal_me_route(
    request: Request,
    ctx: PortalContext = Depends(require_portal_permission("portal.ticket.read")),
) -> PortalMeOut:
    """The person using the portal -- a contact, or a technician with a
    portal code: their name, address, kind and the portal codes they hold."""
    principal = getattr(request.state, "session_principal", None)
    if principal is None:
        # Only a browser session or the assistant's internal token reaches a
        # portal route with a principal; the MCP channel has neither, and this
        # route is not an MCP tool (`NOT_FOR_MCP`).
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, tr("sign in to continue"))
    return PortalMeOut(
        user_id=principal.user_id,
        kind=principal.kind,
        email=principal.email,
        display_name=principal.display_name,
        timezone=principal.timezone,
        permissions=await portal_permissions(request, ctx),
    )
```

En `backend/app/core/auth/sessions.py`, `SessionPrincipal` gana, como último campo, `timezone: str | None = None` (con `#: The person's saved time zone, for the portal's own pages (phase 2, `GET /portal/me`).`), y en `resolve` la lectura de la persona pasa a `select(User.email, User.display_name, User.timezone)` y el `SessionPrincipal(...)` final, `timezone=person.timezone`. El valor por defecto deja intactos los demás sitios que construyen uno (`loopback._live_session`).

- [ ] **Step 11: MCP, rutas, recuentos y textos**

En `backend/app/mcp/exclusions.py`, dentro del diccionario por comprensión de `/api/v1/auth/{path}`, después de `("step-up/passkey", "takes a passkey assertion"),`:

```python
            ("step-up/password", "takes the account's password"),
```

y en `NOT_FOR_MCP`, junto al bloque «My security»:

```python
    # The portal page's own account summary (own authentication phase 2): read
    # from the browser's session, which the MCP channel never has. An AI app
    # asks `GET /me`.
    ("GET", "/api/v1/portal/me"): (
        "the portal page's own summary of the signed-in person, read from the browser's session"
    ),
```

En `backend/tests/test_mcp_channel_api.py`: añadir `"step-up/password",` a la tupla de `OWN_SIGN_IN_NEVER_EXPOSED` (después de `"step-up/passkey",`) y cambiar `assert len(NEVER_EXPOSED) == 36` por `== 37`.

En `backend/tests/test_route_gating.py`, en `UNGATED_BY_DESIGN`, después de la entrada de `step-up/passkey`:

```python
        # A contact confirms with the password (P2 phase 2, D29): the full
        # session `guards.session_of` + `require_full`, the password's counters.
        ("POST", "/api/v1/auth/step-up/password"),
```

En `backend/tests/test_ai_tool_surface.py`, encima de `EXPECTED_GET_ROUTES`, añadir:

```python
# 255 -> 256 (2026-11-06, own authentication phase 2, Task 4): `GET /portal/me`,
# gated (`require_portal_permission`), typed, not a list and closed; not an
# MCP tool (`NOT_FOR_MCP`: it reads the browser's session), so it carries no
# route description: "P5 self-describing" +1, like the other portal routes.
```

y cambiar `EXPECTED_GET_ROUTES = 255` por `256` y `"P5 self-describing": 176` por `177` (con `# +1 (2026-11-06): GET /portal/me (P2 phase 2 Task 4).` encima). **Si la prueba mide otra cosa, registrar lo que mide**: su mensaje nombra la propiedad y la ruta, y el comentario de arriba se corrige con ese dato.

En `backend/app/core/client_scope.py`, la razón de `"app/core/auth/flows.py::_identity_session"` queda:

```python
        "sign-in before the door admits anybody: the password, the second factor, the "
        "enrolment, the recent confirmation, the reset link -- a technician's or a contact's "
        "own auth rows and users row only (P2 design §6.2; phase 2, D25)"
```

En `backend/app/core/i18n/es.py`, junto a la entrada del correo de restablecimiento que ya existe:

```python
    "Someone asked to reset the password of your {product} account.\n\n"
    "Open this link within 30 minutes to choose a new one.\n{link}\n\n"
    "If it was not you, ignore this message: your password has not changed.": (
        "Alguien pidió restablecer la contraseña de tu cuenta de {product}.\n\n"
        "Abre este enlace en los próximos 30 minutos para elegir una nueva.\n{link}\n\n"
        "Si no fuiste tú, ignora este mensaje: tu contraseña no ha cambiado."
    ),
```

En `frontend/src/i18n/es/auditVocabulary.ts`, `auth_signed_in: "Sesión iniciada en la consola"` pasa a `auth_signed_in: "Sesión iniciada"`: desde esta fase la registra también un contacto, que entra al portal (el inglés, que es la derivación del código, no cambia).

- [ ] **Step 12: Correr las pruebas**

Run: `.venv/bin/pytest -q tests/test_auth_contacts_signin.py tests/test_portal_me.py tests/test_auth_signin.py tests/test_auth_password_reset.py tests/test_auth_falsifiers.py tests/test_auth_session_door.py tests/test_portal_api.py tests/test_mcp_channel_api.py tests/test_route_gating.py tests/test_ai_tool_surface.py tests/test_mcp_catalog.py tests/test_i18n_catalog.py tests/test_client_catalog.py tests/test_logging_discipline.py`
Expected: PASS. `test_logging_discipline.py` sigue en 108: la Task 4 no añade ni quita sitios.

- [ ] **Step 13: Tipos, estilo y vocabulario**

Run: `.venv/bin/mypy app && .venv/bin/ruff check app tests && cd ../frontend && npx vitest run src/modules/audit/auditVocabulary.coverage.test.ts src/i18n`
Expected: `Success: no issues found`, `All checks passed!` y la prueba de vocabulario en verde (ninguna acción nueva; `auth.signed_in` ya está).

- [ ] **Step 14: Commit**

```bash
git add backend/app/core/auth/ backend/app/api/deps.py backend/app/api/v1/auth.py \
  backend/app/modules/portal/ backend/app/mcp/exclusions.py backend/app/core/client_scope.py \
  backend/app/core/i18n/es.py backend/tests/ frontend/src/i18n/es/auditVocabulary.ts
git commit -m "feat(auth): contacts sign in with a password through the one sign-in route"
```

---

### Task 5: Microsoft, el protocolo — `entra.py`, Ajustes → Seguridad → Microsoft y los tenants de cada cliente

Diseño §4: una app registrada por Novieri, multi-tenant (autoridad `organizations`), su secreto en la bóveda (invariante 3, D33), código de autorización con PKCE, `state` y `nonce`; técnicos solo desde el tenant de Novieri «configurado en Ajustes»; contactos desde los tenants que su cliente lista. D30–D35, D44. Oleada 2; depende de la Task 1 (`AuthEntraSettings`, `Client.microsoft_tenants`).

Esta tarea no inicia la sesión de nadie: produce el protocolo (Task 9 lo compone) y la configuración. Todo hecho externo viene del Apéndice C ([M1]–[M5], [R1]) o del dossier.

**Files:**
- Create: `backend/app/core/auth/entra.py`
- Create: `backend/app/core/auth/entra_settings.py`
- Create: `backend/app/api/v1/entra_settings.py`
- Modify: `backend/app/main.py` (registrar el router, junto a `session_settings_router`, `:344` y `:381`)
- Modify: `backend/app/api/v1/credentials.py` (rechazar la credencial `entra_sign_in`, `:256-271` y `:292-303`)
- Modify: `backend/app/api/v1/clients.py` (`ClientOut`, `ClientUpdate`, `_client_out`) y `backend/app/services/clients.py` (`CLIENT_FIELDS`, `update_client`)
- Modify: `backend/app/mcp/descriptions/part_d.py` (dos descripciones; la de `PATCH /clients/{client_id}`)
- Modify: `backend/app/core/i18n/es.py`
- Modify: `frontend/src/i18n/en/auditVocabulary.ts`, `frontend/src/i18n/es/auditVocabulary.ts`
- Modify: `backend/tests/test_global_writes.py`, `backend/tests/test_ai_tool_surface.py`
- Regenerate: `frontend/src/lib/globalRoutes.json`
- Create: `backend/tests/entra_support.py`, `backend/tests/test_auth_entra.py`, `backend/tests/test_entra_settings_api.py`, `backend/tests/test_clients_microsoft_tenants.py`
- Test: los nuevos, `backend/tests/test_mcp_catalog.py`, `backend/tests/test_mcp_descriptions.py`, `backend/tests/test_console_global_routes.py`, `backend/tests/test_credentials_api.py`, `backend/tests/test_clients_api.py`, `backend/tests/test_logging_discipline.py`, `backend/tests/test_i18n_catalog.py`

**Interfaces:**
- Consumes: `AuthEntraSettings`, `Client.microsoft_tenants` (Task 1); `VaultService.store`/`checkout` (`app/core/vault.py:70-80`); `Credential` (`app/models/core.py:600-630`); `audit_service.record`; `require_permission`, `require_permission_on_every_client` (`app/api/deps.py`); PyJWT 2.15 (`jwt.decode`, `jwt.get_unverified_header`, `jwt.algorithms.RSAAlgorithm.from_jwk`); `httpx`.
- Produces: Apéndice A, Task 5 (`entra.*`, `entra_settings.*`, las dos rutas, `microsoft_tenants` en `ClientOut`/`ClientUpdate`).

**Decisión del plan (1): el host es una constante y la autoridad, `organizations` o un GUID.** El patrón de `modules/email_intake/microsoft.py` («this pin is the whole control against an `email_intake.admin` holder pointing it at their own host»): el canje lleva el secreto de la app. `_authority` rechaza cualquier otra cosa antes de construir una URL.

**Decisión del plan (2): qué se valida del ID token, y por qué cada cosa** ([M2]: *«Web apps and web APIs that use ID tokens for authorization must validate them»*):
- `alg` = `RS256` y nada más ([M3] `id_token_signing_alg_values_supported: ["RS256"]`; RFC 8725 §3.1 [S38], «MUST NOT use any other algorithms»); un `kid` que esté en el JWKS de `organizations` ([M3] `jwks_uri`);
- `aud` = el id de la app ([M4]: *«The token should be rejected if it fails to match your app's Application ID»*);
- `iss` = `https://login.microsoftonline.com/{tid}/v2.0` con el `tid` del propio token ([M3] `"issuer": "https://login.microsoftonline.com/{tenantid}/v2.0"`; [M4] *«If the token was issued by the v2.0 endpoint, the URI ends in /v2.0»*), y `ver` = `2.0`;
- `tid` ≠ `9188040d-6c67-4c5b-b112-36a304b66dad`, el de las cuentas personales ([M4]); `organizations` ya las excluye ([M2]), esto es la defensa por si no;
- `nonce` igual al emitido, en tiempo constante ([M4] *«If it doesn't match, your application should reject the token»*; [R1] §4.5.3.2);
- `exp`, `nbf`, `iat` presentes y vigentes, con `LEEWAY_S = 60` de margen de reloj (valor de este plan: los relojes del api y de Microsoft no son el mismo);
- `oid` y `tid`, GUID ([M4]). `email`, `preferred_username` y `name` no deciden nada ([M5]); `name` se devuelve solo para mostrarlo.

**Decisión del plan (3): las claves de Microsoft, en una caché propia** (`EntraKeys`), no en `app.core.security.JwksCache`, que es del camino de Keycloak que la fase 3 o la 4 retiran (D44). Mismas tres defensas que esa clase: una sola lectura a la vez, como mucho una lectura nueva por minuto ante un `kid` desconocido, y una lectura fallida nunca vacía las claves que había. Además caducan a la hora.

**Decisión del plan (4): un solo sitio de log, en `exchange`** (+1, 108 → 109): Microsoft rechazó el código o no contestó. `extra={"error": <código>}` y nada más: el código de error de la máquina (`refusal_code`, el patrón de `email_intake/oauth.py`) o `transport`/`http_<estado>`/`no_id_token`. Nunca el cuerpo: `error_description` puede citar lo que rechazó, que aquí es un secreto.

**Decisión del plan (5): el secreto de la app es un `Credential` de tipo `entra_sign_in`** (D33), guardado con `vault.store`, sacado solo en el canje (`checkout_secret`, auditado `auth.entra_secret_used`) y que `credentials.py` se niega a revelar, rotar o borrar, como el del correo de cuentas. Ajustes → Microsoft lo reemplaza; nunca lo devuelve (la respuesta dice `configured`, no `has_secret`: `secret` es una palabra que el filtro de MCP bloquea, `app/core/secrets.py`).

- [ ] **Step 1: El Microsoft de las pruebas**

Crear `backend/tests/entra_support.py`:

```python
"""A fake Microsoft for the tests of sign-in with Microsoft (plan 2026-11-06,
Tasks 5 and 9): one RSA signing key, its JWK set, the token endpoint, and ID
tokens shaped as the claims reference describes them ([M4]).

`install` replaces `app.core.auth.entra.http_client` -- the one seam every
call to Microsoft goes through -- and forgets the process's cached keys, so a
test signs with its own key."""

import json
import secrets
import time
from typing import Any
from urllib.parse import parse_qsl
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

#: Novieri's tenant and the application, in these tests.
TENANT = UUID("11111111-2222-4333-8444-555555555555")
APP = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
CONSUMERS = UUID("9188040d-6c67-4c5b-b112-36a304b66dad")
#: What a token endpoint's refusal may quote, which nothing may repeat.
QUOTED_SECRET = "AADSTS7000215: Invalid client secret provided: s3cr3t-quoted"


class FakeMicrosoft:
    def __init__(self) -> None:
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = f"kid-{secrets.token_hex(4)}"
        self.codes: dict[str, dict[str, Any]] = {}
        self.token_requests: list[dict[str, str]] = []
        self.jwks_reads = 0
        self.refuse_with: tuple[int, dict[str, Any]] | None = None

    def jwks(self) -> dict[str, Any]:
        jwk = json.loads(RSAAlgorithm.to_jwk(self.private.public_key()))
        jwk.update(kid=self.kid, use="sig", alg="RS256")
        return {"keys": [jwk]}

    def id_token(
        self,
        *,
        nonce: str | None,
        tid: UUID = TENANT,
        oid: UUID | None = None,
        aud: UUID = APP,
        headers: dict[str, Any] | None = None,
        key: Any = None,
        **overrides: Any,
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "aud": str(aud),
            "iss": f"https://login.microsoftonline.com/{tid}/v2.0",
            "tid": str(tid),
            "oid": str(oid or uuid4()),
            "sub": secrets.token_urlsafe(16),
            "ver": "2.0",
            "iat": now,
            "nbf": now,
            "exp": now + 3600,
            "name": "Dana Okafor",
            "email": "dana@example.test",
        }
        if nonce is not None:
            claims["nonce"] = nonce
        for name, value in overrides.items():
            if value is None:
                claims.pop(name, None)
            else:
                claims[name] = value
        return jwt.encode(
            claims,
            key or self.private,
            algorithm="RS256",
            headers={"kid": self.kid, **(headers or {})},
        )

    def issue_code(self, **claims: Any) -> str:
        """A code the token endpoint redeems for an ID token with `claims`
        (`nonce` included: the test reads it from the authorize URL)."""
        code = secrets.token_urlsafe(24)
        self.codes[code] = claims
        return code

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/discovery/v2.0/keys"):
            self.jwks_reads += 1
            return httpx.Response(200, json=self.jwks())
        if request.url.path.endswith("/oauth2/v2.0/token"):
            form = dict(parse_qsl(request.content.decode()))
            self.token_requests.append(form)
            if self.refuse_with is not None:
                status, body = self.refuse_with
                return httpx.Response(status, json=body)
            claims = self.codes.pop(form.get("code", ""), None)
            if claims is None:
                return httpx.Response(
                    400, json={"error": "invalid_grant", "error_description": QUOTED_SECRET}
                )
            body = {
                "token_type": "Bearer",
                "access_token": "opaque",
                "id_token": self.id_token(**claims),
            }
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def install(self, monkeypatch: pytest.MonkeyPatch, app: Any | None = None) -> None:
        monkeypatch.setattr("app.core.auth.entra.http_client", self.client)
        if app is not None and hasattr(app.state, "entra_keys"):
            del app.state.entra_keys
```

- [ ] **Step 2: Las pruebas del protocolo**

Crear `backend/tests/test_auth_entra.py`:

```python
"""Sign in with Microsoft, the protocol half (`app/core/auth/entra.py`; plan
2026-11-06, Task 5, decisions 1-4). Every rule decision 2 lists fails a token
that breaks it; the authorize URL carries exactly the parameters [M1] names;
the exchange sends the verifier and the secret and logs the error code only."""

import logging
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.auth import entra
from app.core.auth.entra import EntraKeys, EntraNotConfigured, EntraRefused
from app.core.config import Settings
from tests.entra_support import APP, CONSUMERS, QUOTED_SECRET, TENANT, FakeMicrosoft

NONCE = "n-0S6_WzA2Mj"


@pytest.fixture()
def microsoft(monkeypatch: pytest.MonkeyPatch) -> FakeMicrosoft:
    fake = FakeMicrosoft()
    fake.install(monkeypatch)
    return fake


def _keys(fake: FakeMicrosoft, clock: list[float] | None = None) -> EntraKeys:
    tick = clock if clock is not None else [0.0]
    return EntraKeys(client_factory=fake.client, clock=lambda: tick[0])


def test_the_authorize_url_says_exactly_what_the_flow_needs() -> None:
    url = entra.authorize_url(
        authority=entra.ORGANIZATIONS,
        client_id=APP,
        redirect_uri="https://itms.test/sign-in/microsoft",
        state="st",
        nonce=NONCE,
        code_challenge="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
    )
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == (
        "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize"
    )
    assert {k: v[0] for k, v in parse_qs(parts.query).items()} == {
        "client_id": str(APP),
        "response_type": "code",
        "redirect_uri": "https://itms.test/sign-in/microsoft",
        "response_mode": "fragment",
        "scope": "openid profile",
        "state": "st",
        "nonce": NONCE,
        "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    tenant_url = entra.authorize_url(
        authority=str(TENANT),
        client_id=APP,
        redirect_uri="https://itms.test/sign-in/microsoft",
        state="st",
        nonce=NONCE,
        code_challenge="c",
    )
    assert tenant_url.startswith(f"{entra.MICROSOFT_HOST}/{TENANT}/oauth2/v2.0/authorize?")


@pytest.mark.parametrize("authority", ["common", "consumers", "evil.example/x", "", "../token"])
def test_no_other_authority_is_spoken_to(authority: str) -> None:
    with pytest.raises(ValueError):
        entra.authorize_url(
            authority=authority,
            client_id=APP,
            redirect_uri="https://itms.test/sign-in/microsoft",
            state="s",
            nonce="n",
            code_challenge="c",
        )


def test_the_challenge_is_rfc7636s() -> None:
    """RFC 7636 Appendix B's own example."""
    assert entra.challenge_of("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == (
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )


def _settings(url: str | None) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://x:x@l/x",
        database_admin_url="postgresql+asyncpg://x:x@l/x",
        oidc_issuer="https://i",
        oidc_jwks_url="https://i/certs",
        ca_dir="/tmp/ca",
        public_console_url=url,
    )


def test_the_redirect_is_the_consoles_own_https_address() -> None:
    redirect = entra.redirect_uri(_settings("https://itms.test/"))
    assert redirect == "https://itms.test/sign-in/microsoft"
    for unusable in (None, "", "http://itms.test", "https://itms.test/?x=1"):
        with pytest.raises(EntraNotConfigured):
            entra.redirect_uri(_settings(unusable))


async def test_a_well_formed_token_names_its_tenant_and_account(microsoft: FakeMicrosoft) -> None:
    oid = uuid4()
    token = microsoft.id_token(nonce=NONCE, oid=oid)
    found = await entra.validate_id_token(token, client_id=APP, nonce=NONCE, keys=_keys(microsoft))
    assert found == entra.EntraIdentity(tenant_id=TENANT, object_id=oid, name="Dana Okafor")


def _forged_hs256(fake: FakeMicrosoft) -> str:
    return jwt.encode(
        {"aud": str(APP), "tid": str(TENANT), "oid": str(uuid4()), "nonce": NONCE},
        "secret",
        algorithm="HS256",
        headers={"kid": fake.kid},
    )


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (lambda f: f.id_token(nonce=NONCE, aud=uuid4()), "claims"),
        (lambda f: f.id_token(nonce=NONCE, iss=f"{entra.MICROSOFT_HOST}/{uuid4()}/v2.0"), "claims"),
        (lambda f: f.id_token(nonce=NONCE, iss=f"{entra.MICROSOFT_HOST}/{TENANT}"), "claims"),
        (lambda f: f.id_token(nonce=NONCE, ver="1.0"), "claims"),
        (lambda f: f.id_token(nonce=NONCE, exp=1), "claims"),
        (lambda f: f.id_token(nonce=NONCE, nbf=None), "claims"),
        (lambda f: f.id_token(nonce=NONCE, oid="not-a-guid"), "claims"),
        (lambda f: f.id_token(nonce=NONCE, tid=CONSUMERS), "tenant"),
        (lambda f: f.id_token(nonce="another-nonce"), "nonce"),
        (lambda f: f.id_token(nonce=None), "nonce"),
        (lambda f: f.id_token(nonce=NONCE, headers={"kid": "unknown"}), "signature"),
        (
            lambda f: f.id_token(
                nonce=NONCE, key=rsa.generate_private_key(public_exponent=65537, key_size=2048)
            ),
            "signature",
        ),
        (_forged_hs256, "token"),
        (lambda f: "not.a.token", "token"),
    ],
)
async def test_every_rule_of_decision_2_refuses_a_token_that_breaks_it(
    microsoft: FakeMicrosoft, make: Any, reason: str
) -> None:
    with pytest.raises(EntraRefused) as refused:
        await entra.validate_id_token(
            make(microsoft), client_id=APP, nonce=NONCE, keys=_keys(microsoft)
        )
    assert refused.value.reason == reason


async def test_unknown_kids_read_the_keys_at_most_once_a_minute(microsoft: FakeMicrosoft) -> None:
    clock = [0.0]
    keys = _keys(microsoft, clock)
    for _ in range(5):
        with pytest.raises(EntraRefused):
            await keys.key("nobody-has-this-kid")
    assert microsoft.jwks_reads == 1
    clock[0] += entra.JWKS_MIN_REFETCH_S
    with pytest.raises(EntraRefused):
        await keys.key("nobody-has-this-kid")
    assert microsoft.jwks_reads == 2
    assert await keys.key(microsoft.kid) is not None
    assert microsoft.jwks_reads == 2


async def test_the_exchange_sends_the_verifier_and_the_secret(microsoft: FakeMicrosoft) -> None:
    code = microsoft.issue_code(nonce=NONCE)
    async with microsoft.client() as client:
        id_token = await entra.exchange(
            client=client,
            authority=str(TENANT),
            client_id=APP,
            client_secret="the-app-secret",
            code=code,
            redirect_uri="https://itms.test/sign-in/microsoft",
            code_verifier="v" * 43,
        )
    assert id_token.count(".") == 2
    assert microsoft.token_requests == [
        {
            "client_id": str(APP),
            "scope": "openid profile",
            "code": code,
            "redirect_uri": "https://itms.test/sign-in/microsoft",
            "grant_type": "authorization_code",
            "code_verifier": "v" * 43,
            "client_secret": "the-app-secret",
        }
    ]


async def test_a_refused_exchange_logs_the_error_code_and_nothing_it_quoted(
    microsoft: FakeMicrosoft, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="its.auth.entra")
    async with microsoft.client() as client:
        with pytest.raises(EntraRefused) as refused:
            await entra.exchange(
                client=client,
                authority=entra.ORGANIZATIONS,
                client_id=APP,
                client_secret="the-app-secret",
                code="never-issued",
                redirect_uri="https://itms.test/sign-in/microsoft",
                code_verifier="v" * 43,
            )
    assert refused.value.reason == "exchange"
    [record] = caplog.records
    assert record.getMessage() == "microsoft sign-in: the token endpoint refused the code"
    assert record.__dict__["error"] == "invalid_grant"
    assert QUOTED_SECRET not in caplog.text
    assert "the-app-secret" not in caplog.text


def test_keys_of_keeps_one_cache_per_process() -> None:
    class _App:
        class state:  # noqa: N801 - a stand-in for Starlette's app.state
            pass

    app: Any = _App()
    assert entra.keys_of(app) is entra.keys_of(app)
    assert isinstance(entra.keys_of(app), EntraKeys)
    _ = UUID  # imported for the parametrised cases' readers
```

- [ ] **Step 3: Verlas fallar**

Run: `.venv/bin/pytest -q tests/test_auth_entra.py`
Expected: FAIL en la colección, `ImportError: cannot import name 'entra' from 'app.core.auth'`.

- [ ] **Step 4: El protocolo**

Crear `backend/app/core/auth/entra.py`:

```python
"""Sign in with Microsoft (Entra ID), the protocol half (design §4; plan
2026-11-06, Task 5, decisions 1-4).

ONE APPLICATION, REGISTERED BY NOVIERI, MULTI-TENANT: the authority is
`organizations` -- "Only users with work or school accounts from Microsoft
Entra ID can sign in" [M2] -- or one tenant's GUID (the console's button uses
Novieri's, plan decision D32). Nothing else is ever put in a URL: the host is
a constant and the authority is checked first (decision 1), because the token
request carries the application's secret.

THE FLOW (plan decision D30; [M1], [R1]): the authorization code with PKCE
(`S256`), `state` and `nonce`; `response_mode=fragment`, "Also supported if
requesting only a code" [M1], to the console page `/sign-in/microsoft`, which
posts the code and the state back (Task 9); the code is redeemed at the token
endpoint with the client secret, "required for confidential web apps" [M1],
and the code verifier. The ID token comes from that response -- "Only provided
if openid scope was requested" [M1] -- and `profile` is asked for because "the
profile scope is required to receive" `oid` [M4].

THE ID TOKEN IS VALIDATED HERE (decision 2): "Web apps and web APIs that use
ID tokens for authorization must validate them" [M2]. The person is then
(`tid`, `oid`) -- never `email`, which Microsoft says never to use for
authorization ([S61], [M5]).

This module holds no session and opens no transaction: `entra_flows.py` (Task
9) composes it, and `entra_settings.py` supplies the application and its
secret.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit
from uuid import UUID

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

from app.core.config import Settings

logger = logging.getLogger("its.auth.entra")

MICROSOFT_HOST = "https://login.microsoftonline.com"
ORGANIZATIONS = "organizations"
#: The personal Microsoft account tenant: never a work or school account [M4].
CONSUMER_TENANT = UUID("9188040d-6c67-4c5b-b112-36a304b66dad")
SCOPE = "openid profile"
#: The console page Microsoft returns the browser to (Task 12), and the
#: redirect URI the application is registered with (`deploy/README.md`).
CALLBACK_PATH = "/sign-in/microsoft"
#: The `jwks_uri` of `organizations`' discovery document [M3].
JWKS_URL = f"{MICROSOFT_HOST}/{ORGANIZATIONS}/discovery/v2.0/keys"
#: Seconds, per request: a wedged Microsoft must not hold a request.
HTTP_TIMEOUT_S = 15.0
#: How long the keys are reused, and how often an unknown `kid` may read
#: them again (decision 3).
JWKS_TTL_S = 3600.0
JWKS_MIN_REFETCH_S = 60.0
#: Clock skew allowed on `exp`, `nbf` and `iat` (decision 2).
LEEWAY_S = 60
_ALGORITHM = "RS256"
_ERROR_CODE = re.compile(r"[a-z_]{1,64}")

Reason = Literal["exchange", "token", "signature", "claims", "nonce", "tenant"]


class EntraNotConfigured(Exception):
    """`ITS_PUBLIC_CONSOLE_URL` is unset or is not an https address."""


class EntraRefused(Exception):
    """The sign-in cannot go on. `reason` is for the tests and the flows'
    choice of answer; the person is told one sentence whatever it is."""

    def __init__(self, reason: Reason) -> None:
        super().__init__(reason)
        self.reason: Reason = reason


@dataclass(frozen=True)
class EntraIdentity:
    """Who Microsoft says signed in: `tid` and `oid`, and a name to show."""

    tenant_id: UUID
    object_id: UUID
    name: str


def authority_for(tenant_id: UUID | None) -> str:
    """A tenant's own authority, or `organizations` with none (D32)."""
    return ORGANIZATIONS if tenant_id is None else str(tenant_id)


def _authority(value: str) -> str:
    """Decision 1: `organizations` or a GUID, or a `ValueError`."""
    if value == ORGANIZATIONS:
        return value
    return str(UUID(value))


def redirect_uri(settings: Settings) -> str:
    """From configuration, never from `Host` (host-header injection, the
    reasoning of `email_intake/microsoft.py::redirect_uri`)."""
    base = (settings.public_console_url or "").strip().rstrip("/")
    parts = urlsplit(base)
    if parts.scheme != "https" or not parts.netloc or parts.query or parts.fragment:
        raise EntraNotConfigured()
    return f"{base}{CALLBACK_PATH}"


def challenge_of(verifier: str) -> str:
    """PKCE `S256`: BASE64URL(SHA256(verifier)), no padding (RFC 7636 §4.2)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorize_url(
    *,
    authority: str,
    client_id: UUID,
    redirect_uri: str,
    state: str,
    nonce: str,
    code_challenge: str,
) -> str:
    query = urlencode(
        {
            "client_id": str(client_id),
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "response_mode": "fragment",
            "scope": SCOPE,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            # Otherwise whoever is already signed in to Microsoft in this
            # browser is used without asking [M1, `prompt`].
            "prompt": "select_account",
        }
    )
    return f"{MICROSOFT_HOST}/{_authority(authority)}/oauth2/v2.0/authorize?{query}"


def http_client() -> httpx.AsyncClient:
    """The client every call to Microsoft uses; the tests replace this seam."""
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT_S)


def refusal_code(response: httpx.Response) -> str:
    """Microsoft's machine `error` code, and nothing else from the body: its
    `error_description` can quote what it refused (decision 4)."""
    try:
        body = response.json()
    except ValueError:
        return ""
    code = body.get("error") if isinstance(body, dict) else None
    return code if isinstance(code, str) and _ERROR_CODE.fullmatch(code) else ""


async def exchange(
    *,
    client: httpx.AsyncClient,
    authority: str,
    client_id: UUID,
    client_secret: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> str:
    """Redeem the code at the token endpoint, `client_secret_post` [M3]: the
    ID token. Any refusal is `EntraRefused("exchange")`, logged once with the
    error code alone (decision 4)."""
    error: str | None = None
    id_token: object = None
    try:
        response = await client.post(
            f"{MICROSOFT_HOST}/{_authority(authority)}/oauth2/v2.0/token",
            data={
                "client_id": str(client_id),
                "scope": SCOPE,
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
                "client_secret": client_secret,
            },
        )
        if response.status_code != 200:
            error = refusal_code(response) or f"http_{response.status_code}"
        else:
            body = response.json()
            id_token = body.get("id_token") if isinstance(body, dict) else None
    except (httpx.HTTPError, ValueError):
        error = "transport"
    if error is None and not isinstance(id_token, str):
        error = "no_id_token"
    if error is not None:
        logger.warning(
            "microsoft sign-in: the token endpoint refused the code", extra={"error": error}
        )
        raise EntraRefused("exchange")
    assert isinstance(id_token, str)
    return id_token


class EntraKeys:
    """Microsoft's signing keys (decision 3): read from `JWKS_URL`, kept for
    `JWKS_TTL_S`, and read again for an unknown `kid` at most every
    `JWKS_MIN_REFETCH_S`, one read at a time. A failed read keeps the keys it
    had."""

    def __init__(
        self,
        *,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # Looked up at call time, so the tests' replacement of `http_client`
        # reaches a cache made before it.
        self._client_factory = client_factory
        self._clock = clock
        self._keys: dict[str, Any] = {}
        self._read_at: float | None = None
        self._last_try = float("-inf")
        self._lock = asyncio.Lock()

    async def _read(self) -> None:
        factory = self._client_factory or http_client
        try:
            async with factory() as client:
                response = await client.get(JWKS_URL)
                response.raise_for_status()
                entries = response.json()["keys"]
            keys = {
                entry["kid"]: RSAAlgorithm.from_jwk(json.dumps(entry))
                for entry in entries
                if isinstance(entry, dict)
                and entry.get("kty") == "RSA"
                and isinstance(entry.get("kid"), str)
            }
        except (httpx.HTTPError, ValueError, KeyError, TypeError, jwt.PyJWTError):
            return
        if keys:
            self._keys, self._read_at = keys, self._clock()

    async def key(self, kid: str) -> Any:
        async with self._lock:
            now = self._clock()
            stale = self._read_at is None or now - self._read_at >= JWKS_TTL_S
            if (kid not in self._keys or stale) and now - self._last_try >= JWKS_MIN_REFETCH_S:
                self._last_try = now
                await self._read()
        found = self._keys.get(kid)
        if found is None:
            raise EntraRefused("signature")
        return found


def keys_of(app: Any) -> EntraKeys:
    """The process's one key cache, on `app.state` (made on first use)."""
    keys: EntraKeys | None = getattr(app.state, "entra_keys", None)
    if keys is None:
        keys = EntraKeys()
        app.state.entra_keys = keys
    return keys


async def validate_id_token(
    id_token: str, *, client_id: UUID, nonce: str, keys: EntraKeys
) -> EntraIdentity:
    """Decision 2, every rule, in this order; the first that fails refuses."""
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise EntraRefused("token") from exc
    if header.get("alg") != _ALGORITHM or not isinstance(header.get("kid"), str):
        raise EntraRefused("token")
    key = await keys.key(header["kid"])
    try:
        claims: dict[str, Any] = jwt.decode(
            id_token,
            key,
            algorithms=[_ALGORITHM],
            audience=str(client_id),
            leeway=LEEWAY_S,
            options={"require": ["exp", "iat", "nbf", "aud", "iss"]},
        )
    except jwt.InvalidSignatureError as exc:
        raise EntraRefused("signature") from exc
    except jwt.PyJWTError as exc:
        raise EntraRefused("claims") from exc
    try:
        tenant = UUID(str(claims["tid"]))
        account = UUID(str(claims["oid"]))
    except (KeyError, ValueError) as exc:
        raise EntraRefused("claims") from exc
    issuer = str(claims.get("iss", "")).lower()
    if claims.get("ver") != "2.0" or issuer != f"{MICROSOFT_HOST}/{tenant}/v2.0":
        raise EntraRefused("claims")
    if tenant == CONSUMER_TENANT:
        raise EntraRefused("tenant")
    sent = claims.get("nonce")
    if not isinstance(sent, str) or not hmac.compare_digest(
        sent.encode("utf-8"), nonce.encode("utf-8")
    ):
        raise EntraRefused("nonce")
    name = claims.get("name")
    return EntraIdentity(
        tenant_id=tenant, object_id=account, name=name if isinstance(name, str) else ""
    )
```

- [ ] **Step 5: Correr las pruebas del protocolo**

Run: `.venv/bin/pytest -q tests/test_auth_entra.py`
Expected: PASS.

- [ ] **Step 6: Las pruebas de los ajustes y de los tenants**

Crear `backend/tests/test_entra_settings_api.py`:

```python
"""Settings → Security → Microsoft (plan 2026-11-06, Task 5, decision 5;
D33, D34): the application and Novieri's tenant, the secret into the vault
and never back out, every save audited, nothing switched on half configured."""

from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.entra_settings import ENTRA_CREDENTIAL_TYPE
from app.models import AuditEvent, Credential
from tests.entra_support import APP, CONSUMERS, TENANT

API = "/api/v1/settings/microsoft-sign-in"
SECRET = "Fq8~app-secret-value-from-entra"


async def _admin(env: Any) -> tuple[Any, dict[str, str]]:
    org = await env.create_org("entra-settings")
    sub = await env.create_user(org, "org-admin")
    return org, await env.headers(org, sub)


async def test_nothing_is_set_until_somebody_saves(api_env: Any) -> None:
    _org, headers = await _admin(api_env)
    got = await api_env.client.get(API, headers=headers)
    assert got.status_code == 200, got.text
    assert got.json() == {
        "enabled": False,
        "app_client_id": None,
        "staff_tenant_id": None,
        "configured": False,
        "redirect_uri": "https://itms.test/sign-in/microsoft",
        "updated_at": None,
        "updated_by": None,
    }


async def test_a_save_stores_the_secret_in_the_vault_and_never_returns_it(api_env: Any) -> None:
    org, headers = await _admin(api_env)
    body = {"enabled": True, "app_client_id": str(APP), "staff_tenant_id": str(TENANT)}
    first = await api_env.client.put(API, headers=headers, json=body)
    assert first.status_code == 422, "no secret yet: decision 3's 422"
    saved = await api_env.client.put(API, headers=headers, json={**body, "client_secret": SECRET})
    assert saved.status_code == 200, saved.text
    assert saved.json()["configured"] is True and saved.json()["enabled"] is True
    assert SECRET not in saved.text
    async with AsyncSession(api_env.admin_engine) as s:
        credential = (
            await s.execute(
                select(Credential).where(
                    Credential.org_id == org, Credential.type == ENTRA_CREDENTIAL_TYPE
                )
            )
        ).scalar_one()
        events = (
            await s.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org, AuditEvent.action == "auth.entra_settings_updated"
                )
            )
        ).scalars().all()
    assert credential.ciphertext and SECRET not in credential.ciphertext
    [event] = events
    assert event.meta["secret_changed"] is True
    assert SECRET not in str(event.meta)

    kept = await api_env.client.put(
        API, headers=headers, json={**body, "staff_tenant_id": None}
    )
    assert kept.status_code == 200 and kept.json()["configured"] is True


async def test_the_secret_cannot_be_checked_out_rotated_or_deleted_elsewhere(
    api_env: Any,
) -> None:
    org, headers = await _admin(api_env)
    await api_env.client.put(
        API,
        headers=headers,
        json={
            "enabled": True,
            "app_client_id": str(APP),
            "staff_tenant_id": None,
            "client_secret": SECRET,
        },
    )
    async with AsyncSession(api_env.admin_engine) as s:
        credential_id = await s.scalar(
            select(Credential.id).where(
                Credential.org_id == org, Credential.type == ENTRA_CREDENTIAL_TYPE
            )
        )
    checkout = await api_env.client.post(
        f"/api/v1/credentials/{credential_id}/checkout", headers=headers, json={"reason": "look"}
    )
    assert checkout.status_code == 409
    assert SECRET not in checkout.text
    deleted = await api_env.client.delete(f"/api/v1/credentials/{credential_id}", headers=headers)
    assert deleted.status_code == 409


async def test_a_personal_account_tenant_is_refused(api_env: Any) -> None:
    _org, headers = await _admin(api_env)
    refused = await api_env.client.put(
        API,
        headers=headers,
        json={
            "enabled": True,
            "app_client_id": str(APP),
            "staff_tenant_id": str(CONSUMERS),
            "client_secret": SECRET,
        },
    )
    assert refused.status_code == 422
    assert SECRET not in refused.text


async def test_only_a_holder_of_orgs_manage_everywhere_saves(api_env: Any) -> None:
    org = await api_env.create_org("entra-settings-tech")
    sub = await api_env.create_user(org, "technician")
    headers = await api_env.headers(org, sub)
    put = await api_env.client.put(
        API,
        headers=headers,
        json={"enabled": False, "app_client_id": str(uuid4()), "staff_tenant_id": None},
    )
    assert put.status_code == 403
```

Crear `backend/tests/test_clients_microsoft_tenants.py`:

```python
"""A client's Microsoft tenants (plan decision D35): edited with
`clients.manage` on the client, kept sorted and unique, audited as text, and
never a personal-account tenant or the unassigned client's."""

from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent, Client
from tests.entra_support import CONSUMERS


async def _client(env: Any) -> tuple[Any, dict[str, str], str]:
    org = await env.create_org("ms-tenants")
    sub = await env.create_user(org, "org-admin")
    headers = await env.headers(org, sub)
    made = await env.client.post("/api/v1/clients", headers=headers, json={"name": "Acme"})
    assert made.status_code == 201, made.text
    return org, headers, made.json()["id"]


async def test_tenants_are_kept_sorted_unique_and_audited_as_text(api_env: Any) -> None:
    org, headers, client_id = await _client(api_env)
    one, two = sorted([uuid4(), uuid4()], key=str)
    patched = await api_env.client.patch(
        f"/api/v1/clients/{client_id}",
        headers=headers,
        json={"microsoft_tenants": [str(two), str(one), str(two)]},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["microsoft_tenants"] == [str(one), str(two)]
    async with AsyncSession(api_env.admin_engine) as s:
        event = (
            await s.execute(
                select(AuditEvent)
                .where(AuditEvent.org_id == org, AuditEvent.action == "client.updated")
                .order_by(AuditEvent.ts.desc())
            )
        ).scalars().first()
        stored = await s.scalar(select(Client.microsoft_tenants).where(Client.id == client_id))
    assert event is not None
    assert event.meta["changed"]["microsoft_tenants"] == {
        "before": [],
        "after": [str(one), str(two)],
    }
    assert stored == [one, two]


async def test_a_personal_account_tenant_is_refused(api_env: Any) -> None:
    _org, headers, client_id = await _client(api_env)
    refused = await api_env.client.patch(
        f"/api/v1/clients/{client_id}",
        headers=headers,
        json={"microsoft_tenants": [str(CONSUMERS)]},
    )
    assert refused.status_code == 422


async def test_at_most_twenty(api_env: Any) -> None:
    _org, headers, client_id = await _client(api_env)
    refused = await api_env.client.patch(
        f"/api/v1/clients/{client_id}",
        headers=headers,
        json={"microsoft_tenants": [str(uuid4()) for _ in range(21)]},
    )
    assert refused.status_code == 422
```

- [ ] **Step 7: Verlas fallar**

Run: `.venv/bin/pytest -q tests/test_entra_settings_api.py tests/test_clients_microsoft_tenants.py`
Expected: FAIL (`ImportError: cannot import name 'entra_settings'`; y, en la segunda, el `PATCH` responde 422 por un campo que `ClientUpdate` no conoce — `extra="forbid"`).

- [ ] **Step 8: Los ajustes**

Crear `backend/app/core/auth/entra_settings.py`:

```python
"""Settings → Security → Microsoft (design §4; plan decisions D33, D34; Task 5,
decision 5): the organization's one Entra application and Novieri's tenant.

THE CLIENT SECRET IS A VAULT SECRET (invariant 3): a `credentials` row of type
`entra_sign_in`, envelope-encrypted under the organization's key, which
OpenBao wraps -- D9's pattern for the account-mail relay. It is written by
`save`, read only by `checkout_secret` at the moment of a code exchange
(audited `auth.entra_secret_used`), and refused by every route of
`app/api/v1/credentials.py`. Nothing returns it.

THE SESSION IS THE CALLER'S, and must be able to write a global row: the
settings route holds `orgs.manage` on every client, and the sign-in flows
(Task 9) an every-client identity session.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.auth.entra import CONSUMER_TENANT
from app.core.i18n import tr
from app.core.vault import VaultService
from app.models import Credential
from app.models.auth import AuthEntraSettings

ENTRA_CREDENTIAL_TYPE = "entra_sign_in"
#: The longest client secret accepted: Entra's are far shorter.
SECRET_MAX = 512


class EntraSettingsInvalid(Exception):
    """A save refused, with a sentence already translated (`tr`)."""


@dataclass(frozen=True)
class EntraConfig:
    enabled: bool
    app_client_id: UUID | None
    staff_tenant_id: UUID | None
    #: An application id and a secret are stored.
    configured: bool
    credential_id: UUID | None
    updated_at: datetime | None
    updated_by: UUID | None

    @property
    def usable(self) -> bool:
        """Switched on and complete: sign-in with Microsoft may start."""
        return self.enabled and self.configured


def _config(row: AuthEntraSettings | None) -> EntraConfig:
    if row is None:
        return EntraConfig(False, None, None, False, None, None, None)
    return EntraConfig(
        enabled=row.enabled,
        app_client_id=row.app_client_id,
        staff_tenant_id=row.staff_tenant_id,
        configured=row.app_client_id is not None and row.credential_id is not None,
        credential_id=row.credential_id,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


async def _row(session: AsyncSession, org_id: UUID) -> AuthEntraSettings | None:
    return (
        await session.execute(select(AuthEntraSettings).where(AuthEntraSettings.org_id == org_id))
    ).scalar_one_or_none()


async def read(session: AsyncSession, org_id: UUID) -> EntraConfig:
    return _config(await _row(session, org_id))


async def _store_secret(
    session: AsyncSession,
    vault: VaultService,
    *,
    org_id: UUID,
    actor_id: UUID,
    secret: str,
    existing: UUID | None,
) -> UUID:
    """Into the vault, rotating `existing` in place (`app/core/mail.py::_store_secret`)."""
    credential: Credential | None = None
    if existing is not None:
        credential = (
            await session.execute(
                select(Credential).where(Credential.org_id == org_id, Credential.id == existing)
            )
        ).scalar_one_or_none()
    if credential is None:
        credential = Credential(
            org_id=org_id,
            name="entra-sign-in",
            type=ENTRA_CREDENTIAL_TYPE,
            vault_ref="",
            created_by=actor_id,
        )
        session.add(credential)
        await session.flush()
    stored = await vault.store(
        session, org_id=org_id, credential_id=credential.id, plaintext=secret
    )
    credential.ciphertext = stored.ciphertext_b64
    credential.wrapped_dek = stored.wrapped_dek
    credential.key_version = stored.key_version
    credential.vault_ref = f"envelope:{credential.id}"
    session.add(credential)
    await session.flush()
    return credential.id


def _plain(value: object) -> object:
    return str(value) if isinstance(value, UUID) else value


async def save(
    session: AsyncSession,
    vault: VaultService,
    *,
    org_id: UUID,
    actor_id: UUID,
    enabled: bool,
    app_client_id: UUID | None,
    staff_tenant_id: UUID | None,
    client_secret: str | None,
) -> EntraConfig:
    """Every value, every time; `client_secret` None keeps the stored one.
    Refused (`EntraSettingsInvalid`) before anything is written: a secret
    empty or over `SECRET_MAX`; a personal-account tenant as Novieri's; on,
    without an application id or with no secret ever saved. A save that
    changes nothing writes nothing and audits nothing."""
    secret = None if client_secret is None else client_secret.strip()
    if secret is not None and (not secret or len(secret) > SECRET_MAX):
        raise EntraSettingsInvalid(
            tr("the client secret is empty or longer than {maximum} characters", maximum=SECRET_MAX)
        )
    if staff_tenant_id == CONSUMER_TENANT:
        raise EntraSettingsInvalid(
            tr("a personal Microsoft account tenant cannot sign anyone in here")
        )
    row = await _row(session, org_id)
    before = _config(row)
    if enabled and app_client_id is None:
        raise EntraSettingsInvalid(
            tr(
                "the Microsoft application (client) ID is needed to switch sign-in with "
                "Microsoft on"
            )
        )
    if enabled and secret is None and before.credential_id is None:
        raise EntraSettingsInvalid(
            tr("a client secret is needed the first time sign-in with Microsoft is switched on")
        )
    if row is None:
        row = AuthEntraSettings(org_id=org_id)
        session.add(row)
    changed = {
        name: [_plain(getattr(before, name)), _plain(value)]
        for name, value in (
            ("enabled", enabled),
            ("app_client_id", app_client_id),
            ("staff_tenant_id", staff_tenant_id),
        )
        if getattr(before, name) != value
    }
    if secret is not None:
        row.credential_id = await _store_secret(
            session,
            vault,
            org_id=org_id,
            actor_id=actor_id,
            secret=secret,
            existing=row.credential_id,
        )
    if changed or secret is not None:
        row.enabled = enabled
        row.app_client_id = app_client_id
        row.staff_tenant_id = staff_tenant_id
        row.updated_at, row.updated_by = datetime.now(UTC), actor_id
        await session.flush()
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="user",
            actor_id=actor_id,
            action="auth.entra_settings_updated",
            target_type="organization",
            target_id=str(org_id),
            metadata={"changed": changed, "secret_changed": secret is not None},
        )
    return _config(row)


async def checkout_secret(
    session: AsyncSession,
    vault: VaultService,
    *,
    org_id: UUID,
    config: EntraConfig,
    actor_id: UUID | None,
) -> str:
    """The client secret, for one code exchange, audited in the same breath
    (invariant 4). `actor_id` is the person when the flow already knows them
    (a link, an invitation), None at a sign-in (`system`)."""
    if config.credential_id is None:
        raise LookupError("no Entra client secret is stored")  # i18n-exempt: the caller answers 503
    secret = await vault.checkout(session, org_id=org_id, credential_id=config.credential_id)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user" if actor_id is not None else "system",
        actor_id=actor_id,
        action="auth.entra_secret_used",
        target_type="credential",
        target_id=str(config.credential_id),
        metadata={"purpose": "sign_in"},
    )
    return secret
```

Crear `backend/app/api/v1/entra_settings.py`:

```python
"""Settings → Security → Microsoft (design §4; plan 2026-11-06, Task 5, D34).

`GET` needs `orgs.manage`; `PUT` needs it on EVERY client
(`require_permission_on_every_client`, `tests/test_global_writes.py`), as
Settings → Email and → Sessions. The client secret goes in and never out
(`app/core/auth/entra_settings.py`). `redirect_uri` is what to register in the
Microsoft application, from `ITS_PUBLIC_CONSOLE_URL`.
"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import AuthContext, require_permission, require_permission_on_every_client
from app.core.auth import entra, entra_settings
from app.core.tenancy import org_session

router = APIRouter(prefix="/settings", tags=["settings"])


class EntraSettingsOut(BaseModel):
    """Sign-in with Microsoft for this organization. The client secret is never
    returned; `configured` says whether an application and its secret are stored."""

    enabled: bool = Field(description="Whether people may sign in with Microsoft.")
    app_client_id: UUID | None = Field(description="The Microsoft application (client) ID.")
    staff_tenant_id: UUID | None = Field(
        description="Novieri's tenant: the only one technicians sign in from. Null: none."
    )
    configured: bool = Field(description="An application ID and its client secret are stored.")
    redirect_uri: str | None = Field(
        description="The redirect URI to register in the Microsoft application; null while "
        "the console's address is not configured."
    )
    updated_at: datetime | None = Field(description="When it was last saved.")
    updated_by: UUID | None = Field(description="Who last saved it.")


class EntraSettingsIn(BaseModel):
    """Every value, every time (a PUT)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    app_client_id: UUID | None
    staff_tenant_id: UUID | None
    #: A secret: a plain `str` with no Pydantic bound (phase 1, decision 16);
    #: `entra_settings.save` bounds it. Absent or null keeps the stored one.
    client_secret: str | None = None


def _out(request: Request, config: entra_settings.EntraConfig) -> EntraSettingsOut:
    try:
        redirect = entra.redirect_uri(request.app.state.settings)
    except entra.EntraNotConfigured:
        redirect = None
    return EntraSettingsOut(
        enabled=config.enabled,
        app_client_id=config.app_client_id,
        staff_tenant_id=config.staff_tenant_id,
        configured=config.configured,
        redirect_uri=redirect,
        updated_at=config.updated_at,
        updated_by=config.updated_by,
    )


@router.get("/microsoft-sign-in", response_model=EntraSettingsOut)
async def read_entra_settings(
    request: Request, ctx: AuthContext = Depends(require_permission("orgs.manage"))
) -> EntraSettingsOut:
    """How people sign in with Microsoft here: the application, Novieri's
    tenant, and whether it is switched on. Never the client secret."""
    async with org_session(
        ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
    ) as session:
        return _out(request, await entra_settings.read(session, ctx.org_id))


@router.put("/microsoft-sign-in", response_model=EntraSettingsOut)
async def write_entra_settings(
    body: EntraSettingsIn,
    request: Request,
    ctx: AuthContext = Depends(require_permission_on_every_client("orgs.manage")),
) -> EntraSettingsOut:
    """Save the application, Novieri's tenant and the switch; a client secret
    sent goes to the vault and replaces the stored one. Audited, the secret
    never."""
    try:
        async with org_session(
            ctx.org_id, engine=request.app.state.app_engine, clients=ctx.clients
        ) as session:
            config = await entra_settings.save(
                session,
                request.app.state.vault,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                enabled=body.enabled,
                app_client_id=body.app_client_id,
                staff_tenant_id=body.staff_tenant_id,
                client_secret=body.client_secret,
            )
    except entra_settings.EntraSettingsInvalid as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    return _out(request, config)
```

En `backend/app/main.py`, junto a `from app.api.v1.session_settings import router as session_settings_router` (`:344`): `from app.api.v1.entra_settings import router as entra_settings_router`; y después de `app.include_router(session_settings_router, prefix="/api/v1")` (`:381`): `app.include_router(entra_settings_router, prefix="/api/v1")`.

- [ ] **Step 9: La credencial que nadie saca**

En `backend/app/api/v1/credentials.py`, importar `from app.core.auth.entra_settings import ENTRA_CREDENTIAL_TYPE`; en `checkout_credential`, después del bloque de `ACCOUNT_MAIL_CREDENTIAL_TYPE`:

```python
        # Sign-in with Microsoft's client secret (own authentication phase 2):
        # opened by the code exchange alone, audited there. Settings ->
        # Security -> Microsoft replaces it; nothing reads it back.
        if credential.type == ENTRA_CREDENTIAL_TYPE:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                tr(
                    "This credential is used only to sign people in with Microsoft and cannot be "
                    "revealed."
                ),
            )
```

y en `_refuse_account_mail`, después de su `if`:

```python
    if credential.type == ENTRA_CREDENTIAL_TYPE:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            tr(
                "This credential belongs to sign-in with Microsoft; change it in Settings, "
                "Security, Microsoft."
            ),
        )
```

(su docstring gana: «The same holds for sign-in with Microsoft's client secret (Task 5).»).

- [ ] **Step 10: Los tenants de cada cliente**

En `backend/app/api/v1/clients.py`: en `ClientOut`, debajo de `email_domains`:

```python
    microsoft_tenants: list[UUID] = Field(
        description="Microsoft tenants whose accounts this client's contacts may sign in with"
    )
```

en `ClientUpdate`, debajo de `email_domains`:

```python
    microsoft_tenants: list[UUID] | None = Field(default=None, max_length=20)
```

y en `_client_out`, `microsoft_tenants=list(c.microsoft_tenants),` debajo de `email_domains=...`.

En `backend/app/services/clients.py`: añadir `"microsoft_tenants"` a `CLIENT_FIELDS`; `from app.core.auth.entra import CONSUMER_TENANT`; las dos funciones:

```python
def _tenants(values: Sequence[UUID]) -> list[UUID]:
    """Sorted, without repeats; never the personal-account tenant (plan D35)."""
    tenants = sorted(set(values), key=str)
    if CONSUMER_TENANT in tenants:
        raise ClientError(422, tr("a personal Microsoft account tenant cannot sign anyone in here"))
    return tenants


def _plain(value: Any) -> Any:
    """A value as the audit row keeps it: JSON has no UUID."""
    if isinstance(value, list):
        return [str(v) if isinstance(v, UUID) else v for v in value]
    return value
```

y en `update_client`: en el bucle de `wanted`, antes del `else`:

```python
        elif key == "microsoft_tenants":
            wanted[key] = _tenants(value)
```

en el bloque del cliente sin asignar, `if wanted.get("microsoft_tenants"): raise ClientError(422, tr("the unassigned client takes no Microsoft tenants"))`; y reemplazar el cálculo de `changed` y su aplicación por:

```python
    changed = {
        key: {"before": _plain(getattr(client, key)), "after": _plain(value)}
        for key, value in wanted.items()
        if getattr(client, key) != value
    }
    if "email_domains" in changed and await _domain_taken(
        session, org_id, wanted["email_domains"], client_id
    ):
        raise _domain_refusal()
    if not changed:
        return client
    for key in changed:
        setattr(client, key, wanted[key])
```

En `backend/app/mcp/descriptions/part_d.py`, en «settings», después de `"PUT /api/v1/settings/email"`:

```python
    "GET /api/v1/settings/microsoft-sign-in": (
        "Returns how people sign in with Microsoft here: whether it is on, the Microsoft "
        "application ID, Novieri's tenant for technicians, and the redirect URI to register. "
        "Never returns the client secret, only whether one is configured."
    ),
    "PUT /api/v1/settings/microsoft-sign-in": (
        "Saves sign-in with Microsoft: the switch, the application ID and Novieri's tenant; a "
        "client secret sent goes to the vault and is never returned. Needs every client. "
        "Audited without the secret."
    ),
```

y en la descripción de `PATCH /api/v1/clients/{client_id}` añadir al final la frase «Also sets the Microsoft tenants whose accounts its contacts may sign in with.».

- [ ] **Step 11: Recuentos, listas, textos y vocabulario**

En `backend/tests/test_global_writes.py`, en `GLOBAL_WRITES`, debajo de `("PUT", "/api/v1/settings/sessions"): "orgs.manage",`:

```python
    # sign in with Microsoft (`auth_entra_settings`, P2 phase 2 design §4)
    ("PUT", "/api/v1/settings/microsoft-sign-in"): "orgs.manage",
```

En `backend/tests/test_ai_tool_surface.py`: encima de `EXPECTED_GET_ROUTES`, `# 256 -> 257 (2026-11-06, P2 phase 2 Task 5): GET /settings/microsoft-sign-in, gated (orgs.manage), typed, described and closed: passing five +1. It passes all six and waits in PENDING_TOOL_REVIEW beside /settings/email and /settings/sessions: who may sign in with which Microsoft tenant is reconnaissance about the organisation's sign-in posture.`; `EXPECTED_GET_ROUTES` +1 respecto de lo que veas (255 → 256 si la Task 4 no está fusionada; el coordinador suma); `EXPECTED_PASSING_FIVE` +1; y `"/api/v1/settings/microsoft-sign-in",` en las dos listas donde está `"/api/v1/settings/sessions",` (la de `EXPECTED_PASSING_SIX` y la de `PENDING_TOOL_REVIEW`), con el comentario `# Added 2026-11-06 (P2 phase 2 Task 5); reasoning above EXPECTED_GET_ROUTES.`

En `backend/app/core/i18n/es.py`:

```python
    "the client secret is empty or longer than {maximum} characters": (
        "el secreto de cliente está vacío o tiene más de {maximum} caracteres"
    ),
    "a personal Microsoft account tenant cannot sign anyone in here": (
        "el tenant de las cuentas personales de Microsoft no puede iniciar sesión aquí"
    ),
    "the Microsoft application (client) ID is needed to switch sign-in with Microsoft on": (
        "para activar el inicio de sesión con Microsoft hace falta el ID de la aplicación (cliente)"
    ),
    "a client secret is needed the first time sign-in with Microsoft is switched on": (
        "la primera vez que se activa el inicio de sesión con Microsoft hace falta un secreto de "
        "cliente"
    ),
    "This credential is used only to sign people in with Microsoft and cannot be revealed.": (
        "Esta credencial solo sirve para iniciar sesión con Microsoft y no se puede revelar."
    ),
    "This credential belongs to sign-in with Microsoft; change it in Settings, Security, "
    "Microsoft.": (
        "Esta credencial es del inicio de sesión con Microsoft; cámbiala en Ajustes, Seguridad, "
        "Microsoft."
    ),
    "the unassigned client takes no Microsoft tenants": (
        "el cliente sin asignar no lleva tenants de Microsoft"
    ),
```

En `frontend/src/i18n/en/auditVocabulary.ts`, en su orden alfabético: `auth_entra_secret_used: "Auth entra secret used",` y `auth_entra_settings_updated: "Auth entra settings updated",`. En `frontend/src/i18n/es/auditVocabulary.ts`: `auth_entra_secret_used: "Secreto de Microsoft usado al iniciar sesión",` y `auth_entra_settings_updated: "Ajustes de Microsoft modificados",`.

En `backend/tests/test_logging_discipline.py`, encima del párrafo «NOW 108», añadir:

```
    NOW 109, own authentication phase 2 Task 5: one static WARNING in
    `app/core/auth/entra.py::exchange`, "microsoft sign-in: the token endpoint
    refused the code". `extra` = Microsoft's machine error code, or
    `transport` / `http_<status>` / `no_id_token`; never the body, whose
    `error_description` can quote the client secret.
```

y cambiar `assert total == 108` por `109` (y, en `docs/superpowers/specs/2026-08-14-logging-kernel-design.md` §2.2, el mismo párrafo encima de «NOW 108»).

Regenerar el archivo de rutas globales: `.venv/bin/python -m tests.export_console_global_routes`.

- [ ] **Step 12: Correr las pruebas**

Run: `.venv/bin/pytest -q tests/test_auth_entra.py tests/test_entra_settings_api.py tests/test_clients_microsoft_tenants.py tests/test_clients_api.py tests/test_credentials_api.py tests/test_global_writes.py tests/test_console_global_routes.py tests/test_ai_tool_surface.py tests/test_mcp_catalog.py tests/test_mcp_descriptions.py tests/test_logging_discipline.py tests/test_i18n_catalog.py tests/test_route_gating.py`
Expected: PASS.

- [ ] **Step 13: Tipos, estilo y vocabulario**

Run: `.venv/bin/mypy app && .venv/bin/ruff check app tests && cd ../frontend && npx vitest run src/modules/audit/auditVocabulary.coverage.test.ts src/i18n`
Expected: `Success: no issues found`, `All checks passed!`, verde.

- [ ] **Step 14: Commit**

```bash
git add backend/app/core/auth/entra.py backend/app/core/auth/entra_settings.py \
  backend/app/api/v1/entra_settings.py backend/app/main.py backend/app/api/v1/credentials.py \
  backend/app/api/v1/clients.py backend/app/services/clients.py backend/app/mcp/descriptions/part_d.py \
  backend/app/core/i18n/es.py backend/tests/ docs/superpowers/specs/2026-08-14-logging-kernel-design.md \
  frontend/src/i18n/en/auditVocabulary.ts frontend/src/i18n/es/auditVocabulary.ts \
  frontend/src/lib/globalRoutes.json
git commit -m "feat(auth): Microsoft sign-in protocol, Settings -> Microsoft and client tenants"
```

---

### Task 6: Entrar con una clave de acceso sin dirección (contactos)

Diseño §3, «Entrada: llave de acceso o contraseña de 15+ caracteres»; §2.2 (verificación del usuario requerida y comprobada en el servidor, desafíos de un solo uso); falsificador §10.4. D27, D28. Oleada 3; depende de la Task 4 (`flows.started`, `signed_user` por tipo) y de la Task 1 (`org_of_slug`).

La fase 1 dejó el camino preparado: `passkeys.authentication_options(user_id=None)` emite un desafío «de nadie» (`ck_auth_webauthn_challenges_person` lo permite solo para `authenticate`) y `verify_authentication` acepta una aserción descubrible que se identifica por `userHandle`. Esta tarea pone la ruta y la regla: solo un contacto entra así.

**Files:**
- Modify: `backend/app/core/auth/passkeys.py` (`registration_options(..., discoverable=)`)
- Modify: `backend/app/core/auth/flows.py` (`enroll_passkey_options`, `_default_org`, `passkey_sign_in_options`, `passkey_sign_in`)
- Modify: `backend/app/api/v1/auth.py` (`PasskeySignInIn`, `POST /auth/passkey/options`, `POST /auth/passkey`, índice del docstring)
- Modify: `backend/app/mcp/exclusions.py` (+2 en `NEVER_EXPOSED_REASONS`)
- Modify: `backend/tests/test_mcp_channel_api.py`, `backend/tests/test_route_gating.py`
- Create: `backend/tests/test_auth_passkey_signin.py`
- Test: el nuevo, `backend/tests/test_auth_passkeys.py`, `backend/tests/test_auth_passkey_vectors.py`, `backend/tests/test_auth_signin.py`, `backend/tests/test_auth_falsifiers.py`

**Interfaces:**
- Consumes: `passkeys.authentication_options`, `verify_authentication`, `PasskeyRejected` (fase 1); `lookup.org_of_slug` (Task 1); `flows.started`, `CONTACT_KIND` en `flows.py` (Task 4); `failures.check_allowed`, `record_success`; `guards.relying_party`.
- Produces: `registration_options(session, user, *, rp_id, rp_name, discoverable: bool = False)`; `flows.passkey_sign_in_options(request) -> dict[str, Any]`; `flows.passkey_sign_in(request, *, credential: dict[str, Any]) -> SignIn`; rutas `POST /auth/passkey/options` y `POST /auth/passkey` (→ `{"state": "signed_in"}`).

**Decisión del plan (1): solo un contacto entra con la clave de acceso sola.** Un técnico entra con su contraseña y después el segundo factor (§2.2, Q1): su clave de acceso en esta ruta no abre nada. La negativa es la misma que la de una aserción falsa (401 `passkey_rejected`), como W3C pide para una aserción que no verifica: *«make it indistinguishable whether verification failed because the signature is invalid or because no such user or credential is registered»* ([S14]).

**Decisión del plan (2): no se cuenta un fallo, y un factor bloqueado o en espera no entra.** Antes de que la aserción verifique no se sabe de quién es, y contar contra la persona que el `userHandle` dice sería dejar que cualquiera bloquee a otro con aserciones inventadas. El desafío es de un solo uso y caduca a los 5 min, y el límite por dirección `_FACTOR` (30 cada 10 min) acota los intentos. Pero si la clave de acceso de esa persona está bloqueada (100 fallos como segundo factor) o en espera, tampoco entra por aquí: `check_allowed` después de verificar.

**Decisión del plan (3): la clave de un contacto se registra descubrible** (`residentKey: "required"`, D28, [W1]): sin eso un navegador puede crear una credencial que no se ofrece sin dirección, y el contacto no podría entrar con ella. Las de un técnico siguen en `"preferred"`.

- [ ] **Step 1: Escribir las pruebas**

Crear `backend/tests/test_auth_passkey_signin.py`:

```python
"""A contact signs in with a passkey alone, without an address (plan
2026-11-06, Task 6; design §3, decisions D27, D28).

The cryptography is phase 1's (`tests/test_auth_passkeys.py`,
`tests/test_auth_passkey_vectors.py`); here `flows.verify_authentication` is
replaced, as `tests/auth_support.py::add_passkey` says, and the rule around
it is what is held: who may enter this way, what a refusal says, and that
user verification is still asked for (falsifier 4)."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import flows
from app.core.auth.passkeys import PasskeyRejected
from app.core.auth.sessions import SESSION_COOKIE
from app.models.auth import AuthFailure, AuthPasskey, AuthWebauthnChallenge
from tests.auth_support import (
    add_passkey,
    capture_mail,
    console_ready,
    contact,
    cookie,
    login,
    point_at,
    signed_in,
)


@pytest.fixture()
def auth_env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    console_ready(api_env, monkeypatch)
    api_env.mail = capture_mail(monkeypatch)
    return api_env


def _verifying(monkeypatch: pytest.MonkeyPatch, passkey_id: UUID | None) -> None:
    """`flows.verify_authentication` answers that passkey, or refuses."""

    async def fake(session: Any, credential: Any, **_kwargs: Any) -> AuthPasskey:
        if passkey_id is None:
            raise PasskeyRejected(reason="verification")
        found = await session.get(AuthPasskey, passkey_id)
        assert found is not None
        return found

    monkeypatch.setattr(flows, "verify_authentication", fake)


async def _post(env: Any, who: Any) -> Any:
    return await env.client.post(
        "/api/v1/auth/passkey", headers=who.network, json={"credential": {"id": "x"}}
    )


async def test_the_options_name_no_credential_and_require_user_verification(
    auth_env: Any,
) -> None:
    org = await auth_env.create_org("pk-options")
    who = await contact(auth_env, org, password=None)
    await point_at(auth_env, org)
    got = await auth_env.client.post("/api/v1/auth/passkey/options", headers=who.network)
    assert got.status_code == 200, got.text
    options = got.json()
    assert options["allowCredentials"] == []
    assert options["userVerification"] == "required"
    async with AsyncSession(auth_env.admin_engine) as s:
        rows = (
            await s.execute(
                select(AuthWebauthnChallenge.user_id, AuthWebauthnChallenge.purpose).where(
                    AuthWebauthnChallenge.org_id == org
                )
            )
        ).all()
    assert [(r.user_id, r.purpose) for r in rows] == [(None, "authenticate")]


async def test_a_contacts_passkey_alone_signs_the_contact_in(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = await auth_env.create_org("pk-contact")
    who = await contact(auth_env, org, password=None)
    await point_at(auth_env, org)
    _verifying(monkeypatch, await add_passkey(auth_env, who))
    answer = await _post(auth_env, who)
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"state": "signed_in"}
    who.session = cookie(answer, SESSION_COOKIE)
    me = await auth_env.client.get("/api/v1/portal/me", headers=who.headers())
    assert me.status_code == 200 and me.json()["kind"] == "contact"


async def test_a_technicians_passkey_signs_nobody_in_here(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = await auth_env.create_org("pk-tech")
    tech = await signed_in(auth_env, org, "technician")
    _verifying(monkeypatch, await add_passkey(auth_env, tech))
    answer = await _post(auth_env, tech)
    assert answer.status_code == 401
    assert answer.json()["code"] == "passkey_rejected"
    assert cookie(answer, SESSION_COOKIE) is None


async def test_a_refused_assertion_is_the_same_answer(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = await auth_env.create_org("pk-refused")
    who = await contact(auth_env, org, password=None)
    await point_at(auth_env, org)
    _verifying(monkeypatch, None)
    answer = await _post(auth_env, who)
    assert answer.status_code == 401
    assert answer.json()["code"] == "passkey_rejected"


async def test_a_locked_passkey_signs_nobody_in(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = await auth_env.create_org("pk-locked")
    who = await contact(auth_env, org, password=None)
    await point_at(auth_env, org)
    _verifying(monkeypatch, await add_passkey(auth_env, who))
    async with AsyncSession(auth_env.admin_engine) as s:
        s.add(
            AuthFailure(
                org_id=org,
                user_id=who.user_id,
                factor="passkey",
                consecutive=100,
                last_failed_at=datetime.now(UTC),
                disabled_at=datetime.now(UTC),
            )
        )
        await s.commit()
    answer = await _post(auth_env, who)
    assert answer.status_code == 401
    assert answer.json()["code"] == "passkey_rejected"


async def test_a_contacts_new_passkey_is_discoverable_and_a_technicians_is_preferred(
    auth_env: Any,
) -> None:
    org = await auth_env.create_org("pk-register")
    who = await contact(auth_env, org)
    await point_at(auth_env, org)
    await login(auth_env, who)
    mine = await auth_env.client.post("/api/v1/auth/enroll/passkey/options", headers=who.headers())
    assert mine.status_code == 200, mine.text
    assert mine.json()["authenticatorSelection"]["residentKey"] == "required"
    assert mine.json()["authenticatorSelection"]["userVerification"] == "required"

    tech = await signed_in(auth_env, org, "technician")
    theirs = await auth_env.client.post(
        "/api/v1/auth/enroll/passkey/options", headers=tech.headers()
    )
    assert theirs.status_code == 200, theirs.text
    assert theirs.json()["authenticatorSelection"]["residentKey"] == "preferred"
```

- [ ] **Step 2: Verlas fallar**

Run: `.venv/bin/pytest -q tests/test_auth_passkey_signin.py`
Expected: FAIL: las rutas `/auth/passkey*` responden 404 y la última ve `"preferred"` para el contacto.

- [ ] **Step 3: El registro descubrible**

En `backend/app/core/auth/passkeys.py`, cambiar la firma y la selección de `registration_options`:

```python
async def registration_options(
    session: AsyncSession, user: User, *, rp_id: str, rp_name: str, discoverable: bool = False
) -> dict[str, Any]:
    """`PublicKeyCredentialCreationOptionsJSON` for `user`, as
    `@simplewebauthn/browser`'s `startRegistration({ optionsJSON })` takes it:
    user verification required, no attestation, the person's existing passkeys
    excluded, and a discoverable credential preferred -- or REQUIRED with
    `discoverable` (a contact's, which signs in without an address: W3C
    WebAuthn Level 3 §5.4.6, "The client MUST return an error if a
    client-side discoverable credential cannot be created"; plan 2026-11-06,
    Task 6, decision 3)."""
```

y, en la llamada a `generate_registration_options`:

```python
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=(
                ResidentKeyRequirement.REQUIRED
                if discoverable
                else ResidentKeyRequirement.PREFERRED
            ),
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
```

- [ ] **Step 4: Los flujos**

En `backend/app/core/auth/flows.py`, importar `org_of_slug` junto a `find_person` (`from app.core.auth.lookup import find_person, org_of_slug`); en `enroll_passkey_options`, la llamada queda:

```python
        return await registration_options(
            session,
            user,
            rp_id=rp_id,
            rp_name=notices.PRODUCT,
            discoverable=user.kind == CONTACT_KIND,
        )
```

y, después de `step_up_password`, una sección nueva:

```python
# --------------------------------------------------------------------------- #
# a passkey alone, without an address (phase 2, Task 6; design §3, D28)
# --------------------------------------------------------------------------- #


def _no_organization() -> AuthRefusal:
    return AuthRefusal(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        code="sign_in_unavailable",
        detail=tr(
            "signing in is not set up on this installation: it names no organization to "
            "sign in to"
        ),
    )


async def _default_org(request: Request) -> UUID:
    """The organization a sign-in without an address signs in to: the
    deployment's (`default_org_slug`), through `its_auth_org` (D36)."""
    slug = request.app.state.settings.default_org_slug
    org_id = await org_of_slug(_engine(request), slug) if slug else None
    if org_id is None:
        raise _no_organization()
    return org_id


def _passkey_rejected() -> AuthRefusal:
    return AuthRefusal(
        status.HTTP_401_UNAUTHORIZED,
        code="passkey_rejected",
        detail=tr("the passkey was not accepted; try again"),
    )


async def passkey_sign_in_options(request: Request) -> dict[str, Any]:
    """A challenge that belongs to nobody yet, and no list of credentials:
    the browser offers the person's own discoverable passkeys (D28). Nothing
    here says whether any address has an account."""
    rp_id, _origin = relying_party(request.app.state.settings)
    org_id = await _default_org(request)
    async with _identity_session(org_id, _engine(request)) as session:
        return await authentication_options(
            session, None, org_id=org_id, rp_id=rp_id, purpose="authenticate"
        )


async def passkey_sign_in(request: Request, *, credential: dict[str, Any]) -> SignIn:
    """A contact's passkey alone: user verification required and checked
    (Task 5 of phase 1), so it is two factors and the session is complete
    (D27). Refused alike -- 401 `passkey_rejected` -- for an unknown, spent
    or forged assertion, a technician's passkey (decision 1), a person not
    active, and a passkey factor locked or waiting (decision 2). Nothing is
    counted (decision 2). The challenge is spent in this transaction, which
    commits before the refusal (`passkeys.py`, "THE CALLER'S TRANSACTION")."""
    rp_id, origin = relying_party(request.app.state.settings)
    org_id = await _default_org(request)
    result: SignIn | None = None
    async with _identity_session(org_id, _engine(request)) as session:
        try:
            key = await verify_authentication(
                session, credential, expected_origin=origin, rp_id=rp_id, purpose="authenticate"
            )
        except PasskeyRejected:
            key = None
        user = await session.get(User, key.user_id) if key is not None else None
        if user is not None and user.kind == CONTACT_KIND and user.status == ACTIVE_STATUS:
            state = await check_allowed(session, user.id, "passkey")
            if state.allowed:
                await record_success(session, user.id, "passkey")
                await _audit(
                    session, user, "auth.signed_in", {"method": "passkey", "trusted_device": False}
                )
                result = await started(session, request, user, state="signed_in", factor_done=True)
    if result is None:
        raise _passkey_rejected()
    return result
```

- [ ] **Step 5: Las rutas**

En `backend/app/api/v1/auth.py`, en el índice del docstring, debajo de `/auth/login`:

```
    POST   /auth/passkey/options            a passkey alone: the request options (contacts)
    POST   /auth/passkey                    the assertion      -> signed in (contacts)
```

el modelo, junto a `PasskeyIn`:

```python
class PasskeySignInIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credential: dict[str, Any]
```

y las rutas, después de `login`:

```python
@router.post("/passkey/options", dependencies=[Depends(_FACTOR)])
async def passkey_sign_in_options(request: Request) -> dict[str, Any]:
    """The WebAuthn request options of a sign-in with a passkey alone: no
    address and no list of credentials (a contact's discoverable passkey)."""
    return await flows.passkey_sign_in_options(request)


@router.post(
    "/passkey",
    response_model=SignInOut,
    response_model_exclude_unset=True,
    dependencies=[Depends(_FACTOR)],
)
async def passkey_sign_in(
    body: PasskeySignInIn, request: Request, response: Response
) -> SignInOut:
    """A contact's passkey, alone: signed in. Any other answer is the one 401."""
    step = await flows.passkey_sign_in(request, credential=body.credential)
    return _answer(response, step)
```

- [ ] **Step 6: MCP y rutas**

En `backend/app/mcp/exclusions.py`, en la comprensión de `/api/v1/auth/{path}`, después de `("login", ...)`:

```python
            ("passkey/options", "issues a passkey challenge for a sign-in without an address"),
            ("passkey", "takes a passkey assertion and mints a session cookie"),
```

En `backend/tests/test_mcp_channel_api.py`: `"passkey/options",` y `"passkey",` en `OWN_SIGN_IN_NEVER_EXPOSED` (después de `"login",`) y el recuento +2 respecto del que veas (37 → 39 con la Task 4 fusionada).

En `backend/tests/test_route_gating.py`, en `UNGATED_BY_DESIGN`, después de `("POST", "/api/v1/auth/login"),`:

```python
        # A contact's passkey alone (P2 phase 2, D28): before there is a
        # person; the CSRF rule, `_FACTOR` per address and the single-use
        # challenge stand in for the gate.
        ("POST", "/api/v1/auth/passkey/options"),
        ("POST", "/api/v1/auth/passkey"),
```

- [ ] **Step 7: Correr las pruebas**

Run: `.venv/bin/pytest -q tests/test_auth_passkey_signin.py tests/test_auth_passkeys.py tests/test_auth_passkey_vectors.py tests/test_auth_signin.py tests/test_auth_falsifiers.py tests/test_mcp_channel_api.py tests/test_route_gating.py tests/test_mcp_catalog.py`
Expected: PASS. `test_falsifier_4_every_passkey_check_requires_user_verification` sigue verde: la verificación de los dos ceremoniales no cambió.

- [ ] **Step 8: Tipos y estilo**

Run: `.venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: `Success: no issues found` y `All checks passed!`

- [ ] **Step 9: Commit**

```bash
git add backend/app/core/auth/passkeys.py backend/app/core/auth/flows.py backend/app/api/v1/auth.py \
  backend/app/mcp/exclusions.py backend/tests/
git commit -m "feat(auth): a contact signs in with a discoverable passkey alone"
```

---

### Task 7: Mi seguridad para contactos — las rutas de la propia cuenta por la puerta del portal

Diseño §3 («segundo factor opcional»; «al cambiarla se cierran sus otras sesiones»), §2.4–§2.6 (sesiones, confirmación reciente, equipos recordados, que «aplica a cualquier cuenta con segundo factor: técnicos y contactos que lo activaron»). D26, D27, D29. Oleada 3; depende de la Task 4 (`signed_user` por tipo, `minimum_for`, `methods.py`) y de la Task 1 (`AuthExternalIdentity`).

**Files:**
- Modify: `backend/app/api/deps.py` (`bound_account_context`)
- Modify: `backend/app/api/v1/auth.py` (`_console_ctx` → `_account_ctx`; `MicrosoftLinkOut`; `MySecurityOut`; `PasswordAddIn`; `POST /me/password`)
- Modify: `backend/app/core/auth/account.py` (`MySecurity`, `my_security`, `change_password`, `add_password`, `remove_passkey`, `remove_totp`, `_last_method`)
- Modify: `backend/app/mcp/exclusions.py` (+1 `NEVER_EXPOSED_REASONS`)
- Modify: `backend/app/core/i18n/es.py`
- Modify: `backend/tests/test_mcp_channel_api.py`, `backend/tests/test_route_gating.py`
- Create: `backend/tests/test_auth_contact_security.py`
- Test: el nuevo, `backend/tests/test_auth_my_security.py`, `backend/tests/test_auth_session_door.py`, `backend/tests/test_logging_discipline.py`, `backend/tests/test_i18n_catalog.py`

**Interfaces:**
- Consumes: `admit(request, contacts=True)` (`deps.py:205`); `methods.has_password`, `methods.has_sign_in_method`, `staff_password.minimum_for` (Task 4); `AuthExternalIdentity`, `ENTRA` (Task 1); las funciones de `account.py` de la fase 1.
- Produces: `deps.bound_account_context(request) -> AsyncIterator[AuthContext]`; `MySecurityOut.kind`, `.has_password`, `.microsoft: MicrosoftLinkOut | None` (`tenant_id`, `created_at`, `last_used_at`); `account.add_password(request, principal, *, new) -> None`; ruta `POST /me/password {new}` → 204; códigos `last_method` y `password_exists` (409).

**Decisión del plan (1): la puerta de la propia cuenta es la del portal** (D26). `bound_account_context` llama a `admit(contacts=True)` y exige una sesión de navegador; `GET /me` y el resto de la consola siguen con `bound_auth_context` (`admit(contacts=False)`). Estas rutas solo leen y escriben las filas de autenticación de la propia persona (`account._own_session`), nunca datos de un cliente: que un contacto las use no le abre nada de la consola.

**Decisión del plan (2): las reglas de los métodos, por tipo.** Un técnico, como en la fase 1: nunca se queda sin segundo factor (`last_factor`). Un contacto (D27): su segundo factor es opcional (puede quitar su app TOTP siempre), pero **nunca se queda sin una forma de entrar** (`last_method`): no puede quitar su última clave de acceso si no tiene contraseña ni Microsoft. Quitar a Microsoft con la misma regla es de la Task 9.

**Decisión del plan (3): añadir la primera contraseña** (`POST /me/password`): quien no tiene una (un contacto que entró con una clave de acceso o con Microsoft; un técnico que aceptó con Microsoft, D41) la elige con la confirmación reciente (§2.5) y la regla de su tipo (15 o 12). Quien ya tiene una la cambia con `PUT /me/password` (409 `password_exists`). Las demás sesiones no se cierran: añadir una forma de entrar no es la reacción de quien teme por su cuenta, y el aviso por correo llega igual (§2.7).

- [ ] **Step 1: Escribir las pruebas**

Crear `backend/tests/test_auth_contact_security.py`:

```python
"""My security for a client's contact (plan 2026-11-06, Task 7; design §3,
D26, D27): the account's own routes through the portal's door, the contact's
rules for its ways in, and the first password."""

from typing import Any
from urllib.parse import parse_qs, urlsplit

import pyotp
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import AuthPassword, AuthTotp
from tests.auth_support import (
    CONTACT_PASSWORD,
    add_passkey,
    capture_mail,
    console_ready,
    contact,
    login,
    point_at,
)
from tests.session_support import sign_in


@pytest.fixture()
def auth_env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    console_ready(api_env, monkeypatch)
    api_env.mail = capture_mail(monkeypatch)
    return api_env


async def _signed(env: Any, **kwargs: Any) -> tuple[Any, dict[str, str]]:
    """A contact and the headers of a fresh, complete session of theirs."""
    org = await env.create_org("contact-sec")
    who = await contact(env, org, **kwargs)
    await point_at(env, org)
    signed = await sign_in(env, org, who.sub, step_up=True)
    return who, {**who.network, **signed.headers}


async def test_a_contact_reads_their_own_security_and_still_not_the_console(
    auth_env: Any,
) -> None:
    who, headers = await _signed(auth_env)
    mine = await auth_env.client.get("/api/v1/me/security", headers=headers)
    assert mine.status_code == 200, mine.text
    body = mine.json()
    assert (body["kind"], body["has_password"], body["has_totp"], body["microsoft"]) == (
        "contact",
        True,
        False,
        None,
    )
    console = await auth_env.client.get("/api/v1/me", headers=headers)
    assert console.json()["code"] == "portal_only"


async def test_a_contact_keeps_one_way_in(auth_env: Any) -> None:
    who, headers = await _signed(auth_env, password=None)
    only = await add_passkey(auth_env, who)
    refused = await auth_env.client.delete(f"/api/v1/me/passkeys/{only}", headers=headers)
    assert refused.status_code == 409
    assert refused.json()["code"] == "last_method"

    with_password, headers2 = await _signed(auth_env)
    key = await add_passkey(auth_env, with_password)
    allowed = await auth_env.client.delete(f"/api/v1/me/passkeys/{key}", headers=headers2)
    assert allowed.status_code == 204, allowed.text


async def test_a_contacts_authenticator_app_is_optional(auth_env: Any) -> None:
    who, headers = await _signed(auth_env)
    start = await auth_env.client.post("/api/v1/auth/enroll/totp/start", headers=headers)
    assert start.status_code == 200, start.text
    secret = parse_qs(urlsplit(start.json()["otpauth_uri"]).query)["secret"][0]
    confirmed = await auth_env.client.post(
        "/api/v1/auth/enroll/totp/confirm", headers=headers, json={"code": pyotp.TOTP(secret).now()}
    )
    assert confirmed.status_code == 204, confirmed.text
    removed = await auth_env.client.delete("/api/v1/me/totp", headers=headers)
    assert removed.status_code == 204, removed.text
    async with AsyncSession(auth_env.admin_engine) as s:
        left = await s.scalar(select(AuthTotp.user_id).where(AuthTotp.user_id == who.user_id))
    assert left is None


async def test_a_contact_without_a_password_adds_one_with_the_contact_rule(
    auth_env: Any,
) -> None:
    who, headers = await _signed(auth_env, password=None)
    await add_passkey(auth_env, who)
    short = await auth_env.client.post(
        "/api/v1/me/password", headers=headers, json={"new": "Fourteen-chars"}
    )
    assert short.status_code == 422
    assert short.json()["code"] == "password_rejected"
    added = await auth_env.client.post(
        "/api/v1/me/password", headers=headers, json={"new": CONTACT_PASSWORD}
    )
    assert added.status_code == 204, added.text
    again = await auth_env.client.post(
        "/api/v1/me/password", headers=headers, json={"new": CONTACT_PASSWORD}
    )
    assert again.status_code == 409
    assert again.json()["code"] == "password_exists"
    async with AsyncSession(auth_env.admin_engine) as s:
        assert await s.get(AuthPassword, who.user_id) is not None
    assert [m.to for m in auth_env.mail.sent] == [who.email]
    who.password = CONTACT_PASSWORD
    assert (await login(auth_env, who)).status_code == 200


async def test_a_contacts_password_change_holds_the_contact_rule(auth_env: Any) -> None:
    who, headers = await _signed(auth_env)
    short = await auth_env.client.put(
        "/api/v1/me/password",
        headers=headers,
        json={"current": CONTACT_PASSWORD, "new": "Twelve-chars"},
    )
    assert short.status_code == 422
    assert short.json()["code"] == "password_rejected"
```

- [ ] **Step 2: Verlas fallar**

Run: `.venv/bin/pytest -q tests/test_auth_contact_security.py`
Expected: FAIL: `GET /api/v1/me/security` responde 403 `portal_only` (la puerta aún es la de la consola).

- [ ] **Step 3: La puerta de la propia cuenta**

En `backend/app/api/deps.py`, después de `bound_auth_context`:

```python
async def bound_account_context(request: Request) -> AsyncIterator[AuthContext]:
    """The routes of a person's OWN account -- My security: `/me/security`,
    `/me/password`, `/me/passkeys/*`, `/me/totp`, `/me/recovery-codes`,
    `/me/trusted-devices/*`, `/me/sessions/*` -- for a technician or a client's
    contact (own authentication phase 2, plan decision D26). The portal's
    door (`admit(contacts=True)`), and only for a browser session: a token is
    refused. These routes touch only the caller's own auth rows
    (`app.core.auth.account`), never a client's data; `GET /me` and the rest
    of the console keep `bound_auth_context`, which refuses a contact."""
    person = await admit(request, contacts=True)
    if person.principal is None:
        raise AuthRefusal(
            status.HTTP_401_UNAUTHORIZED, code=AUTH_REQUIRED, detail=tr("sign in to continue")
        )
    ctx = AuthContext(
        org_id=person.org_id,
        user_id=person.user_id,
        claims=person.claims,
        clients=person.clients.narrow(person.requested_client),
        principal=person.principal,
    )
    with bind(org_id=str(ctx.org_id), user_id=str(ctx.user_id)):
        yield ctx
```

- [ ] **Step 4: Las reglas de la cuenta**

En `backend/app/core/auth/account.py`:

1. Imports: `from app.core.auth.methods import has_password, has_sign_in_method`; `from app.core.identity import CONTACT_KIND`; `AuthExternalIdentity` y `ENTRA` en el `from app.models.auth import ...`.
2. Las vistas:

```python
@dataclass(frozen=True)
class MicrosoftLinkView:
    tenant_id: UUID
    created_at: datetime
    last_used_at: datetime | None


@dataclass(frozen=True)
class MySecurity:
    kind: str
    has_password: bool
    has_totp: bool
    passkeys: list[PasskeyView]
    recovery_codes_left: int
    trusted_devices: list[DeviceView]
    sessions: list[SessionView]
    microsoft: MicrosoftLinkView | None
```

3. En `my_security`, dentro del bloque, después de `_has_passkey, has_totp, codes = await summary(...)`:

```python
        password = await has_password(session, user.id)
        link = (
            await session.execute(
                select(AuthExternalIdentity).where(
                    AuthExternalIdentity.user_id == user.id, AuthExternalIdentity.provider == ENTRA
                )
            )
        ).scalar_one_or_none()
```

y en el `return MySecurity(...)`: `kind=user.kind, has_password=password,` al principio y, al final, `microsoft=None if link is None else MicrosoftLinkView(tenant_id=link.tenant_id, created_at=link.created_at, last_used_at=link.last_used_at),`. (El `user` se lee dentro del bloque: guardar `kind = user.kind` antes de salir si mypy lo pide.)

4. En `change_password`, la llamada a `require_acceptable` gana `minimum=staff_password.minimum_for(user.kind),`.

5. Añadir después de `change_password`:

```python
def _password_exists() -> AuthRefusal:
    return AuthRefusal(
        status.HTTP_409_CONFLICT,
        code="password_exists",
        detail=tr("you already have a password; change it instead"),
    )


async def add_password(request: Request, principal: SessionPrincipal, *, new: str) -> None:
    """The first password of an account that has none (plan 2026-11-06, Task
    7, decision 3): a contact who entered with a passkey or Microsoft, a
    technician who accepted with Microsoft. With a recent confirmation (the
    route's `require_step_up`, and here) and the rule of the person's kind; a
    person who has one changes it (`change_password`, 409 `password_exists`
    here). The other sessions stay: adding a way in is not what somebody does
    who fears for the account, and the notice goes all the same (§2.7)."""
    require_fresh(principal)
    async with _own_session(principal, request) as session:
        user = await signed_user(session, principal)
        if await has_password(session, user.id):
            raise _password_exists()
        # A refused password raises its 422 here, before anything is written.
        await staff_password.require_acceptable(
            new,
            email=user.email,
            display_name=user.display_name,
            org_name=await staff_password.organization_name(session, user.org_id),
            minimum=staff_password.minimum_for(user.kind),
        )
        await staff_password.store(session, org_id=user.org_id, user_id=user.id, plain=new)
        await _record(session, user, "auth.password_changed", {"via": "self_added"})
        await notices.tell(
            session, user, "password_changed", vault=_vault(request), actor_id=user.id
        )
```

6. Reemplazar `_last_factor`, `remove_passkey` y `remove_totp` por:

```python
def _last_factor() -> AuthRefusal:
    return AuthRefusal(
        status.HTTP_409_CONFLICT,
        code="last_factor",
        detail=tr("this is your only second factor; add another one before removing it"),
    )


def _last_method() -> AuthRefusal:
    return AuthRefusal(
        status.HTTP_409_CONFLICT,
        code="last_method",
        detail=tr("this is your only way to sign in; add another one before removing it"),
    )


async def remove_passkey(request: Request, principal: SessionPrincipal, passkey_id: UUID) -> None:
    """Remove one of the caller's passkeys. A technician never loses the last
    second factor (`last_factor`); a contact never loses the last way in
    (`last_method`, decision 2)."""
    async with _own_session(principal, request) as session:
        user = await signed_user(session, principal)
        key = (
            await session.execute(
                select(AuthPasskey).where(
                    AuthPasskey.id == passkey_id, AuthPasskey.user_id == user.id
                )
            )
        ).scalar_one_or_none()
        if key is None:
            raise _not_found(tr("no such passkey"))
        keys = (
            await session.execute(
                select(func.count()).select_from(AuthPasskey).where(AuthPasskey.user_id == user.id)
            )
        ).scalar_one()
        if user.kind == CONTACT_KIND:
            await session.delete(key)
            await session.flush()
            if not await has_sign_in_method(session, user.id):
                # Raised after the delete, inside the block: it rolls back.
                raise _last_method()
        else:
            _has_passkey, has_totp, _codes = await summary(session, user.id)
            if keys == 1 and not has_totp:
                raise _last_factor()
            await session.delete(key)
        await _record(session, user, "auth.factor_removed", {"factor": "passkey", "name": key.name})
        await notices.tell(
            session,
            user,
            "factor_removed",
            vault=_vault(request),
            actor_id=user.id,
            factor="passkey",
        )


async def remove_totp(request: Request, principal: SessionPrincipal) -> None:
    """Remove the caller's authenticator app. A technician keeps a second
    factor (`last_factor`); a contact's is optional (D27)."""
    async with _own_session(principal, request) as session:
        user = await signed_user(session, principal)
        has_passkey, has_totp, _codes = await summary(session, user.id)
        if not has_totp:
            raise _not_found(tr("that sign-in method is not set up on this account"))
        if user.kind != CONTACT_KIND and not has_passkey:
            raise _last_factor()
        await drop_totp(session, user.id)
        await _record(session, user, "auth.factor_removed", {"factor": "totp"})
        await notices.tell(
            session, user, "factor_removed", vault=_vault(request), actor_id=user.id, factor="totp"
        )
```

- [ ] **Step 5: Las rutas**

En `backend/app/api/v1/auth.py`:

1. `from app.api.deps import AuthContext, bound_account_context, require_step_up` (sin `bound_auth_context`, que este archivo ya no usa).
2. Reemplazar `_console_ctx` por:

```python
async def _account_ctx(ctx: AuthContext = Depends(bound_account_context)) -> AuthContext:
    """My security's door (phase 2, D26): a technician's or a contact's own
    browser session; `bound_account_context` already refused anything else."""
    return ctx
```

y cambiar cada `Depends(_console_ctx)` del archivo por `Depends(_account_ctx)` (comprobar con `grep -n "_console_ctx" app/api/v1/auth.py`: sin salida). En el docstring de `_me`, «`_console_ctx` refused it otherwise» pasa a «`_account_ctx` refused it otherwise».
3. Modelos, junto a `MySecurityOut`:

```python
class MicrosoftLinkOut(BaseModel):
    """The Microsoft account linked to the caller (design §4): its tenant and
    when it was linked and last used. Never the account's own id."""

    tenant_id: UUID
    created_at: datetime
    last_used_at: datetime | None


class MySecurityOut(BaseModel):
    """The caller's own ways in, second factors, remembered browsers and sessions."""

    kind: Literal["staff", "contact"] = Field(description="Technician or a client's contact.")
    has_password: bool
    has_totp: bool
    passkeys: list[PasskeyOut]
    recovery_codes_left: int
    trusted_devices: list[TrustedDeviceOut]
    sessions: list[SessionOut]
    microsoft: MicrosoftLinkOut | None = Field(
        description="The linked Microsoft account, or null."
    )


class PasswordAddIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    #: A secret: no Pydantic bound (decision 16); the rule bounds it.
    new: str
```

4. En `my_security`, el `return` queda:

```python
    return MySecurityOut(
        kind=mine.kind,
        has_password=mine.has_password,
        has_totp=mine.has_totp,
        passkeys=[PasskeyOut(**vars(p)) for p in mine.passkeys],
        recovery_codes_left=mine.recovery_codes_left,
        trusted_devices=[TrustedDeviceOut(**vars(d)) for d in mine.trusted_devices],
        sessions=[SessionOut(**vars(s)) for s in mine.sessions],
        microsoft=None if mine.microsoft is None else MicrosoftLinkOut(**vars(mine.microsoft)),
    )
```

5. La ruta nueva, después de `change_password` (y su línea en el índice del docstring: `POST   /me/password                     the first password, for an account with none`):

```python
@me_router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
async def add_password(
    body: PasswordAddIn, request: Request, ctx: AuthContext = Depends(_account_ctx)
) -> Response:
    """The first password of an account that has none, after a recent
    confirmation (409 `password_exists` when there is one)."""
    require_step_up(ctx)
    await account.add_password(request, _me(ctx), new=body.new)
    return _no_content()
```

- [ ] **Step 6: MCP, rutas y textos**

En `backend/app/mcp/exclusions.py`, debajo de `("PUT", "/api/v1/me/password"): ...`:

```python
    ("POST", "/api/v1/me/password"): "takes the first password of an account that has none",
```

En `backend/tests/test_mcp_channel_api.py`: `("POST", "/api/v1/me/password"),` en `OWN_SIGN_IN_NEVER_EXPOSED` (junto a `("PUT", "/api/v1/me/password")`) y el recuento +1 respecto del que veas.

En `backend/tests/test_route_gating.py`, junto a la entrada de `PUT /api/v1/me/password`:

```python
        # The first password of an account with none (P2 phase 2, D26): the
        # caller's own account, `bound_account_context` + `require_step_up`.
        ("POST", "/api/v1/me/password"),
```

En `backend/app/core/i18n/es.py`:

```python
    "you already have a password; change it instead": (
        "ya tienes una contraseña; cámbiala en su lugar"
    ),
    "this is your only way to sign in; add another one before removing it": (
        "es tu única forma de iniciar sesión; añade otra antes de quitarla"
    ),
```

- [ ] **Step 7: Correr las pruebas**

Run: `.venv/bin/pytest -q tests/test_auth_contact_security.py tests/test_auth_my_security.py tests/test_auth_session_door.py tests/test_auth_falsifiers.py tests/test_mcp_channel_api.py tests/test_route_gating.py tests/test_mcp_catalog.py tests/test_logging_discipline.py tests/test_i18n_catalog.py tests/test_ai_tool_surface.py`
Expected: PASS. Si `test_auth_my_security.py` compara el cuerpo entero de `GET /me/security`, añadir a su esperado `"kind": "staff"`, `"has_password": True` y `"microsoft": None` (re-apuntado por la decisión 1; el informe lo dice).

- [ ] **Step 8: Tipos, estilo y vocabulario**

Run: `.venv/bin/mypy app && .venv/bin/ruff check app tests && cd ../frontend && npx vitest run src/modules/audit/auditVocabulary.coverage.test.ts`
Expected: verde (`auth.password_changed` ya está en el `DYNAMIC` de `core/auth/account.py`).

- [ ] **Step 9: Commit**

```bash
git add backend/app/api/deps.py backend/app/api/v1/auth.py backend/app/core/auth/account.py \
  backend/app/mcp/exclusions.py backend/app/core/i18n/es.py backend/tests/
git commit -m "feat(auth): contacts manage their own sign-in methods through the portal's door"
```

---

### Task 8: El ciclo de vida del contacto — invitar, aceptar, cancelar, restablecer el acceso, desactivar

Diseño §3: «Invitación desde la ficha del cliente (pestaña Contactos), por quien tenga `clients.manage` en ese cliente: enlace aleatorio, de un solo uso, que caduca a las 72 h [S8]. Al aceptarla, el contacto recibe en **su** cliente el rol de portal (`portal.ticket.read` y `portal.ticket.write`), en ningún otro»; §2.4 («Desactivar a una persona corta sus sesiones… al instante»); §2.5 (la confirmación reciente para restablecer los factores de otro); §7 (sin correo configurado, el enlace se ve una sola vez). D39, D40, D43. Oleada 4; depende de las Tasks 4, 5 (los ajustes de Microsoft, para `invite_details`) y 6 (`registration_options(discoverable=)`).

**Files:**
- Modify: `backend/app/core/auth/action_tokens.py` (`digest`, `peek_digest`, `consume_digest`)
- Create: `backend/app/core/auth/portal_access.py`
- Modify: `backend/app/core/auth/flows.py` (`Invitation`, `invitee`, `microsoft_offered`, `contact_admitted`, `invite_details`, `accept_invite`, `invite_passkey_options`, `invite_passkey`)
- Modify: `backend/app/core/auth/notices.py` (`contact_invitation_mail`)
- Create: `backend/app/services/contact_access.py`
- Modify: `backend/app/services/clients.py` (`update_contact`, `_disable_contacts`, `_cut_access`)
- Modify: `backend/app/api/v1/clients.py` (`ContactOut`, `ContactInviteOut`, tres rutas, `_contact_out`)
- Modify: `backend/app/api/v1/auth.py` (`InviteOut`, `InvitePasskeyIn`, `/auth/invite/passkey*`)
- Modify: `backend/app/mcp/exclusions.py` (+4), `backend/app/mcp/descriptions/part_d.py`
- Modify: `backend/app/core/i18n/es.py`; `frontend/src/i18n/{en,es}/auditVocabulary.ts`; `frontend/src/modules/audit/auditVocabulary.coverage.test.ts` (`DYNAMIC` de `core/auth/flows.py`)
- Modify: `backend/tests/test_mcp_channel_api.py`, `backend/tests/test_route_gating.py`
- Create: `backend/tests/test_contact_access_api.py`
- Test: el nuevo, `backend/tests/test_clients_api.py`, `backend/tests/test_auth_invite*.py` (los de la fase 1: `grep -l "invite" tests/test_auth_*.py`), `backend/tests/test_user_admin_api.py`, `backend/tests/test_mcp_catalog.py`, `backend/tests/test_i18n_catalog.py`

**Interfaces:**
- Consumes: `action_tokens.issue`/`revoke_open`/`link`/`org_of`; `methods.has_sign_in_method`, `flows.started`, `staff_password.minimum_for` (Task 4); `registration_options(discoverable=)`, `verify_registration`, `PasskeyRejected` (fase 1, Task 6); `entra_settings.read` (Task 5); `factors.lock_person`, `clear_second_factors`; `sessions.end_all_for_user`; `devices.revoke_all_for_user`; `notices.deliver`; `services.clients.ClientError`.
- Produces: Apéndice A, Task 8. `flows.invitee`, `flows.contact_admitted` y `flows.microsoft_offered` los consume la Task 9.

**Decisión del plan (1): un contacto acepta con una contraseña o con una clave de acceso**, y en la Task 9 con Microsoft (D40). Quien acepta queda con la sesión completa: no tiene segundo factor que registrar (D27). El rol de portal (`requester`) se concede **en ese momento** y no al invitar: hasta que la persona acepta no hay nadie que lo use.

**Decisión del plan (2): una clave de acceso rechazada deja el enlace vivo.** El desafío se gasta (fase 1: «only a commit keeps it spent»), pero el enlace no: se consume con su bloqueo de fila antes de verificar y, si la clave se rechaza, `used_at` vuelve a NULL en la misma transacción, que confirma. Ninguna otra petición pudo tomarlo entretanto: la fila está bloqueada.

**Decisión del plan (3): restablecer el acceso es una invitación nueva** (D39; Pregunta P6). Para un contacto el buzón ya es la raíz de la cuenta (recibió la primera invitación), así que no hay nada más seguro que ofrecer; lo que este acto añade es que **todo** lo anterior deja de valer (contraseña, claves, app, códigos, contadores, Microsoft, equipos, sesiones, restablecimientos abiertos) y que lo hace un técnico con `clients.manage` en ese cliente y su propia confirmación reciente (§2.5).

**Decisión del plan (4): todo con el alcance de la petición.** Invitar, cancelar, restablecer y desactivar corren en `org_session(..., clients=ctx.clients)`: las filas del contacto son de su cliente (0231), que ese alcance puede escribir. Ningún `ClientScope.all()` nuevo. Aceptar la invitación sí corre en la sesión de identidad de `flows` (antes de que exista nadie), como la de un técnico.

- [ ] **Step 1: Escribir las pruebas**

Crear `backend/tests/test_contact_access_api.py`:

```python
"""A contact's way into the portal (plan 2026-11-06, Task 8; design §3, §7;
decisions D39, D40, D43)."""

import secrets
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import flows
from app.core.auth.passkeys import PasskeyRejected
from app.core.auth.sessions import SESSION_COOKIE
from app.models import AuditEvent, Role, User, UserRole
from app.models.auth import AuthPasskey, AuthPassword
from tests.auth_support import (
    CONTACT_PASSWORD,
    browser,
    capture_mail,
    console_ready,
    contact,
    cookie,
    link_token,
    login,
    point_at,
)


@pytest.fixture()
def auth_env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    console_ready(api_env, monkeypatch)
    api_env.mail = capture_mail(monkeypatch)
    return api_env


async def _world(env: Any, *, password: str | None = None) -> tuple[Any, dict[str, str]]:
    org = await env.create_org("contact-life")
    who = await contact(env, org, password=password)
    await point_at(env, org)
    admin = await env.create_user(org, "org-admin")
    return who, await env.headers(org, admin)


def _invite_url(who: Any) -> str:
    return f"/api/v1/clients/{who.client_id}/contacts/{who.user_id}/invite"


async def _invite(env: Any, who: Any, headers: dict[str, str]) -> str:
    sent = await env.client.post(_invite_url(who), headers=headers)
    assert sent.status_code == 200, sent.text
    assert sent.json()["invite_link"] is None
    assert sent.json()["contact"]["access"] == "invited"
    return link_token(env.mail.sent[-1].text)


async def test_an_invited_contact_accepts_with_a_password_and_gets_the_portal_in_their_client(
    auth_env: Any,
) -> None:
    who, headers = await _world(auth_env)
    token = await _invite(auth_env, who, headers)
    details = await auth_env.client.post(
        "/api/v1/auth/invite/details", headers=browser(), json={"token": token}
    )
    assert details.status_code == 200, details.text
    assert details.json() == {
        "email": who.email,
        "display_name": "",
        "kind": "contact",
        "password_minimum": 15,
        "passkey": True,
        "microsoft": False,
    }
    short = await auth_env.client.post(
        "/api/v1/auth/invite/accept",
        headers=browser(),
        json={"token": token, "password": "Fourteen-chars"},
    )
    assert short.status_code == 422
    accepted = await auth_env.client.post(
        "/api/v1/auth/invite/accept",
        headers=browser(),
        json={"token": token, "password": CONTACT_PASSWORD},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {"state": "signed_in"}
    session = cookie(accepted, SESSION_COOKIE)
    me = await auth_env.client.get(
        "/api/v1/portal/me", headers={**browser(), "Cookie": f"{SESSION_COOKIE}={session}"}
    )
    assert me.status_code == 200 and me.json()["kind"] == "contact"
    async with AsyncSession(auth_env.admin_engine) as s:
        grants = (
            await s.execute(
                select(UserRole.client_id)
                .join(Role, Role.id == UserRole.role_id)
                .where(UserRole.user_id == who.user_id, Role.name == "requester")
            )
        ).scalars().all()
        accepted_events = (
            await s.execute(
                select(AuditEvent.meta).where(
                    AuditEvent.org_id == who.org_id, AuditEvent.action == "contact.invite_accepted"
                )
            )
        ).scalars().all()
    assert list(grants) == [who.client_id]
    assert list(accepted_events) == [{"client_id": str(who.client_id), "method": "password"}]
    again = await auth_env.client.post(
        "/api/v1/auth/invite/accept",
        headers=browser(),
        json={"token": token, "password": CONTACT_PASSWORD},
    )
    assert again.status_code == 410


async def test_without_mail_the_link_is_shown_once(auth_env: Any) -> None:
    who, headers = await _world(auth_env)
    auth_env.mail.configured = False
    sent = await auth_env.client.post(_invite_url(who), headers=headers)
    assert sent.status_code == 200, sent.text
    assert sent.json()["invite_link"].startswith("https://itms.test/invite#")


async def test_a_contact_accepts_with_a_passkey_and_a_refused_one_leaves_the_link_alive(
    auth_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    who, headers = await _world(auth_env)
    token = await _invite(auth_env, who, headers)
    options = await auth_env.client.post(
        "/api/v1/auth/invite/passkey/options", headers=browser(), json={"token": token}
    )
    assert options.status_code == 200, options.text
    assert options.json()["authenticatorSelection"]["residentKey"] == "required"

    async def refusing(*_args: Any, **_kwargs: Any) -> AuthPasskey:
        raise PasskeyRejected(reason="verification")

    monkeypatch.setattr(flows, "verify_registration", refusing)
    refused = await auth_env.client.post(
        "/api/v1/auth/invite/passkey",
        headers=browser(),
        json={"token": token, "credential": {"id": "x"}, "name": "Phone"},
    )
    assert refused.status_code == 400
    assert refused.json()["code"] == "passkey_rejected"
    still = await auth_env.client.post(
        "/api/v1/auth/invite/details", headers=browser(), json={"token": token}
    )
    assert still.status_code == 200

    async def accepting(session: Any, user: Any, credential: Any, **_kwargs: Any) -> AuthPasskey:
        key = AuthPasskey(
            org_id=user.org_id,
            user_id=user.id,
            credential_id=secrets.token_bytes(32),
            public_key=secrets.token_bytes(77),
            sign_count=0,
            transports=["internal"],
            backup_eligible=True,
            backup_state=True,
            name="Phone",
        )
        session.add(key)
        await session.flush()
        return key

    monkeypatch.setattr(flows, "verify_registration", accepting)
    accepted = await auth_env.client.post(
        "/api/v1/auth/invite/passkey",
        headers=browser(),
        json={"token": token, "credential": {"id": "x"}, "name": "Phone"},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {"state": "signed_in"}


async def test_an_active_contact_is_not_invited_again_and_a_reset_cuts_everything(
    auth_env: Any,
) -> None:
    who, headers = await _world(auth_env, password=CONTACT_PASSWORD)
    await login(auth_env, who)
    old = who.headers()
    again = await auth_env.client.post(_invite_url(who), headers=headers)
    assert again.status_code == 409

    reset = await auth_env.client.post(
        f"/api/v1/clients/{who.client_id}/contacts/{who.user_id}/access/reset", headers=headers
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()["contact"]["access"] == "invited"
    assert (await auth_env.client.get("/api/v1/portal/me", headers=old)).status_code == 401
    async with AsyncSession(auth_env.admin_engine) as s:
        assert await s.get(AuthPassword, who.user_id) is None
    assert "reset" in auth_env.mail.sent[-1].subject.lower()


async def test_cancelling_kills_the_link_and_keeps_the_contact(auth_env: Any) -> None:
    who, headers = await _world(auth_env)
    token = await _invite(auth_env, who, headers)
    cancelled = await auth_env.client.delete(_invite_url(who), headers=headers)
    assert cancelled.status_code == 204, cancelled.text
    dead = await auth_env.client.post(
        "/api/v1/auth/invite/details", headers=browser(), json={"token": token}
    )
    assert dead.status_code == 410
    listed = await auth_env.client.get(f"/api/v1/clients/{who.client_id}/contacts", headers=headers)
    [row] = listed.json()
    assert (row["id"], row["access"]) == (str(who.user_id), "none")


async def test_switching_a_contact_off_ends_their_sessions_and_links(auth_env: Any) -> None:
    who, headers = await _world(auth_env, password=CONTACT_PASSWORD)
    await login(auth_env, who)
    signed = who.headers()
    off = await auth_env.client.patch(
        f"/api/v1/contacts/{who.user_id}", headers=headers, json={"status": "disabled"}
    )
    assert off.status_code == 200, off.text
    assert (await auth_env.client.get("/api/v1/portal/me", headers=signed)).status_code == 401
    refused = await auth_env.client.post(_invite_url(who), headers=headers)
    assert refused.status_code == 409


async def test_a_technician_of_another_client_cannot_reach_this_contact(auth_env: Any) -> None:
    """Decision 4: an org-admin grant on another client only; RLS hides this
    client's contact, and the answer is the 404 of an unknown one."""
    who, _headers = await _world(auth_env)
    other = await contact(auth_env, who.org_id)
    sub = await auth_env.create_user(who.org_id)
    async with AsyncSession(auth_env.admin_engine) as s:
        role_id = await s.scalar(
            select(Role.id).where(Role.name == "org-admin", Role.org_id.is_(None))
        )
        user_id = await s.scalar(select(User.id).where(User.external_subject == sub))
        s.add(
            UserRole(org_id=who.org_id, user_id=user_id, role_id=role_id, client_id=other.client_id)
        )
        await s.commit()
    limited = await auth_env.headers(who.org_id, sub)
    refused = await auth_env.client.post(_invite_url(who), headers=limited)
    assert refused.status_code == 404
```

- [ ] **Step 2: Verlas fallar**

Run: `.venv/bin/pytest -q tests/test_contact_access_api.py`
Expected: FAIL: `POST .../invite` responde 404 (no hay ruta).

- [ ] **Step 3: Los enlaces por su resumen**

En `backend/app/core/auth/action_tokens.py`, reemplazar `_live`, `peek` y `consume` por:

```python
def digest(token: str) -> bytes:
    """What the database keeps of a link: its SHA-256. Public since phase 2:
    a Microsoft invitation keeps only this while the browser is away (Task 9)."""
    return _digest(token)


def _live(value: bytes, purposes: Sequence[str]) -> Select[tuple[AuthActionToken]]:
    return select(AuthActionToken).where(
        AuthActionToken.token_hash == value,
        AuthActionToken.purpose.in_(list(purposes)),
        AuthActionToken.used_at.is_(None),
        AuthActionToken.expires_at > datetime.now(UTC),
    )


async def peek_digest(
    session: AsyncSession, *, digest: bytes, purposes: Sequence[str]
) -> AuthActionToken | None:
    """The live row, left unused."""
    return (await session.execute(_live(digest, purposes))).scalar_one_or_none()


async def consume_digest(
    session: AsyncSession, *, digest: bytes, purposes: Sequence[str]
) -> AuthActionToken | None:
    """The live row, marked used under a row lock: two requests with one link
    cannot both get it. The caller's transaction decides: an exception after
    this rolls the use back, so a refused new password leaves the link usable."""
    row = (await session.execute(_live(digest, purposes).with_for_update())).scalar_one_or_none()
    if row is not None:
        row.used_at = datetime.now(UTC)
        await session.flush()
    return row


async def peek(
    session: AsyncSession, *, token: str, purposes: Sequence[str]
) -> AuthActionToken | None:
    return await peek_digest(session, digest=_digest(token), purposes=purposes)


async def consume(
    session: AsyncSession, *, token: str, purposes: Sequence[str]
) -> AuthActionToken | None:
    return await consume_digest(session, digest=_digest(token), purposes=purposes)
```

- [ ] **Step 4: El rol de portal**

Crear `backend/app/core/auth/portal_access.py`:

```python
"""The portal role a contact receives on accepting an invitation (design §3;
plan decision D40): `requester` -- which holds `portal.ticket.read` and
`portal.ticket.write` and nothing else (migration 0028) -- on the contact's
own client, and on no other. Granted once; a second acceptance (Microsoft
after a reset, say) finds it there."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identity import CONTACT_KIND
from app.models import Role, User, UserRole

REQUESTER_ROLE = "requester"


async def grant_portal(session: AsyncSession, user: User) -> bool:
    """True when the grant was made now, False when it was there already."""
    if user.kind != CONTACT_KIND or user.client_id is None:
        raise ValueError(  # i18n-exempt: a caller's defect
            "only a contact of a client is granted the portal"
        )
    role_id = await session.scalar(
        select(Role.id).where(Role.name == REQUESTER_ROLE, Role.org_id.is_(None))
    )
    if role_id is None:
        raise LookupError(  # i18n-exempt: a deployment defect
            "the requester role is missing (migration 0028)"
        )
    held = await session.scalar(
        select(UserRole.id).where(
            UserRole.user_id == user.id,
            UserRole.role_id == role_id,
            UserRole.client_id == user.client_id,
        )
    )
    if held is not None:
        return False
    session.add(
        UserRole(org_id=user.org_id, user_id=user.id, role_id=role_id, client_id=user.client_id)
    )
    await session.flush()
    return True
```

- [ ] **Step 5: Aceptar la invitación, por tipo**

En `backend/app/core/auth/flows.py`:

1. Imports: `from app.core.auth import action_tokens, entra_settings, notices, staff_password`; `from app.core.auth.portal_access import grant_portal`; `from app.models import Client, Role, User, UserRole`.
2. Reemplazar `_invited` por `invitee` y añadir `Invitation`, `microsoft_offered` y `contact_admitted`:

```python
@dataclass(frozen=True)
class Invitation:
    """What an invitation's page needs (design §3, §7): who it is for, and how
    they may accept it."""

    email: str
    display_name: str
    kind: str
    password_minimum: int
    #: A contact may accept with a passkey instead of a password (D40).
    passkey: bool
    #: Sign-in with Microsoft is set up for this person (Task 9 offers it).
    microsoft: bool


async def invitee(session: AsyncSession, *, digest: bytes, consume: bool) -> User:
    """The person of a live, unaccepted invitation -- a technician or a
    client's contact -- or `invalid_link`.

    NEVER A SUPER-ADMIN (Task 8 review, phase 1); NEVER SOMEBODY WHO ALREADY
    HAS A WAY IN (`methods.has_sign_in_method`: a password, a passkey or a
    Microsoft link). Nothing is written before a refusal here but the
    `consume`, and the refusal rolls it back with the block."""
    purposes = (action_tokens.INVITE,)
    if consume:
        row = await action_tokens.consume_digest(session, digest=digest, purposes=purposes)
    else:
        row = await action_tokens.peek_digest(session, digest=digest, purposes=purposes)
    user = await session.get(User, row.user_id) if row is not None else None
    if (
        row is None
        or user is None
        or user.kind not in (STAFF_KIND, CONTACT_KIND)
        or user.status != ACTIVE_STATUS
        or await has_sign_in_method(session, user.id)
        or (user.kind == STAFF_KIND and await _holds_super_admin(session, user.id))
    ):
        raise _invalid_link()
    return user


async def microsoft_offered(session: AsyncSession, user: User) -> bool:
    """Whether this person could sign in with Microsoft here: it is on and,
    for a technician, Novieri's tenant is set; for a contact, their client
    lists a tenant (design §4, D32)."""
    config = await entra_settings.read(session, user.org_id)
    if not config.usable:
        return False
    if user.kind == STAFF_KIND:
        return config.staff_tenant_id is not None
    tenants = await session.scalar(
        select(Client.microsoft_tenants).where(Client.id == user.client_id)
    )
    return bool(tenants)


async def contact_admitted(
    session: AsyncSession, request: Request, user: User, *, method: str
) -> SignIn:
    """A contact accepted their invitation (D40): the portal role on their own
    client (`grant_portal`), audited `contact.invite_accepted`, and a complete
    session -- a contact has no second factor to enrol. Public: Task 9's
    Microsoft acceptance ends here too."""
    await grant_portal(session, user)
    await _audit(
        session,
        user,
        "contact.invite_accepted",
        {"client_id": str(user.client_id), "method": method},
    )
    await _audit(session, user, "auth.signed_in", {"method": "invite", "trusted_device": False})
    return await started(session, request, user, state="signed_in", factor_done=True)
```

3. Reemplazar `invite_details` y `accept_invite`, y añadir las dos de la clave de acceso:

```python
async def invite_details(request: Request, *, token: str) -> Invitation:
    """Who an invitation is for and how they may accept it. Leaves the link
    unused."""
    org_id = action_tokens.org_of(token)
    if org_id is None:
        raise _invalid_link()
    async with _identity_session(org_id, _engine(request)) as session:
        user = await invitee(session, digest=action_tokens.digest(token), consume=False)
        return Invitation(
            email=user.email,
            display_name=user.display_name,
            kind=user.kind,
            password_minimum=staff_password.minimum_for(user.kind),
            passkey=user.kind == CONTACT_KIND,
            microsoft=await microsoft_offered(session, user),
        )


async def accept_invite(request: Request, *, token: str, password: str) -> SignIn:
    """The invitee's own password. A technician: a session waiting for the
    first factor (design §2.2). A contact: signed in, the portal granted
    (`contact_admitted`). A refused password rolls the `consume` back: the
    link stays usable."""
    org_id = action_tokens.org_of(token)
    if org_id is None:
        raise _invalid_link()
    async with _identity_session(org_id, _engine(request)) as session:
        user = await invitee(session, digest=action_tokens.digest(token), consume=True)
        # Raises a 422 that rolls the `consume` back: the link stays usable.
        await staff_password.require_acceptable(
            password,
            email=user.email,
            display_name=user.display_name,
            org_name=await staff_password.organization_name(session, org_id),
            minimum=staff_password.minimum_for(user.kind),
        )
        await staff_password.store(session, org_id=org_id, user_id=user.id, plain=password)
        await _audit(session, user, "auth.password_changed", {"via": "invite"})
        if user.kind == CONTACT_KIND:
            return await contact_admitted(session, request, user, method="password")
        return await started(session, request, user, state="enroll_required")


async def invite_passkey_options(request: Request, *, token: str) -> dict[str, Any]:
    """The creation options of a contact's first passkey, from the invitation
    (D40): discoverable (D28), user verification required. Leaves the link
    unused. A technician's invitation chooses a password (409)."""
    org_id = action_tokens.org_of(token)
    if org_id is None:
        raise _invalid_link()
    rp_id, _origin = relying_party(request.app.state.settings)
    async with _identity_session(org_id, _engine(request)) as session:
        user = await invitee(session, digest=action_tokens.digest(token), consume=False)
        if user.kind != CONTACT_KIND:
            raise _unavailable()
        return await registration_options(
            session, user, rp_id=rp_id, rp_name=notices.PRODUCT, discoverable=True
        )


async def invite_passkey(
    request: Request, *, token: str, credential: dict[str, Any], name: str
) -> SignIn:
    """A contact accepts with a passkey. Task 8's decision 2: the link is
    consumed under its row lock BEFORE the passkey is verified; a refused
    passkey puts it back (`used_at` NULL), the challenge stays spent, and the
    block commits both before the 400."""
    org_id = action_tokens.org_of(token)
    if org_id is None:
        raise _invalid_link()
    rp_id, origin = relying_party(request.app.state.settings)
    digest = action_tokens.digest(token)
    rejected = False
    result: SignIn | None = None
    async with _identity_session(org_id, _engine(request)) as session:
        user = await invitee(session, digest=digest, consume=False)
        if user.kind != CONTACT_KIND:
            raise _unavailable()
        row = await action_tokens.consume_digest(
            session, digest=digest, purposes=(action_tokens.INVITE,)
        )
        if row is None:
            raise _invalid_link()
        try:
            await verify_registration(
                session, user, credential, name=name, expected_origin=origin, rp_id=rp_id
            )
        except PasskeyRejected:
            row.used_at = None
            await session.flush()
            rejected = True
        else:
            result = await contact_admitted(session, request, user, method="passkey")
    if rejected or result is None:
        raise AuthRefusal(
            status.HTTP_400_BAD_REQUEST,
            code="passkey_rejected",
            detail=tr("the passkey was not accepted; try again"),
        )
    return result
```

4. Ninguna otra función llamaba a `_invited`; comprobar con `grep -n "_invited" app/core/auth/flows.py` (sin salida).

En `backend/app/core/auth/notices.py`, después de `invitation_mail`:

```python
def contact_invitation_mail(link: str, *, inviter: str, reset: bool) -> tuple[str, str]:
    """A contact's invitation to the portal (design §3; D39, D40). `reset`: a
    technician reset the contact's access, so the old ways in are gone."""
    if reset:
        return (
            tr("Your access to the {product} portal was reset", product=PRODUCT),
            tr(
                "{inviter} reset your access to the {product} portal: your previous password, "
                "passkeys and linked accounts no longer work.\n\n"
                "Open this link within 72 hours to choose how you will sign in:\n{link}\n\n"
                "If you did not expect this, tell {inviter}.",
                inviter=inviter,
                product=PRODUCT,
                link=link,
            ),
        )
    return (
        tr("You are invited to the {product} portal", product=PRODUCT),
        tr(
            "{inviter} invited you to the {product} portal, where you can follow your "
            "requests.\n\n"
            "Open this link within 72 hours to choose how you will sign in:\n{link}\n\n"
            "If you did not expect this, ignore this message.",
            inviter=inviter,
            product=PRODUCT,
            link=link,
        ),
    )
```

- [ ] **Step 6: El servicio del acceso del contacto**

Crear `backend/app/services/contact_access.py`:

```python
"""A contact's way into the portal (design §3; plan decisions D39, D40, D43;
Task 8, decisions 3 and 4).

WHO: a technician with `clients.manage` on the contact's client. EVERY
FUNCTION TAKES THE CALLER'S SESSION, scoped by the request (`ctx.clients`): a
contact of a client outside it does not exist here (404), and every row
written -- the link, the wiped credentials -- is the contact's client's, which
that scope may write (migration 0231). Nothing here holds `ClientScope.all()`.

THE STATES (`access_of`): `active`, the contact has a way in
(`methods.has_sign_in_method`); `invited`, an unused invitation, expired or
not (the definition `user_admin.service._open_invites` gives technicians'),
with its expiry; `none`, neither.

THE ACTS, each audited:
- `invite` (and resend): a new 72-hour link, replacing the open one. Refused
  for an `active` contact (reset its access instead), a disabled one, or one
  of an archived client.
- `cancel_invite`: the open link dies; the contact stays (a client's
  directory entry, not an account to burn).
- `reset_access`: everything the contact signs in with goes and a new
  invitation is issued (decision 3); the route asks the technician's recent
  confirmation (design §2.5).

The link is in the result so the route can show it ONCE when no mail carried
it (design §7); it is never stored in clear and never audited.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.auth import action_tokens
from app.core.auth.devices import revoke_all_for_user
from app.core.auth.factors import clear_second_factors, lock_person
from app.core.auth.methods import has_sign_in_method
from app.core.auth.sessions import end_all_for_user
from app.core.i18n import tr
from app.core.identity import CONTACT_KIND
from app.models import Client, User
from app.models.auth import AuthActionToken, AuthExternalIdentity, AuthPassword
from app.models.clients import CLIENT_ARCHIVED
from app.services.clients import ClientError

ContactAccessState = Literal["none", "invited", "active"]
#: Hands a contact their link: (contact, link, reset) -> True when a mail took it.
Deliver = Callable[[User, str, bool], Awaitable[bool]]


@dataclass(frozen=True)
class ContactAccess:
    state: ContactAccessState
    invite_expires_at: datetime | None


@dataclass(frozen=True)
class ContactInvited:
    contact: User
    access: ContactAccess
    link: str
    delivered: bool


async def access_of(
    session: AsyncSession, *, org_id: UUID, user_ids: Sequence[UUID]
) -> dict[UUID, ContactAccess]:
    found: dict[UUID, ContactAccess] = {}
    for user_id in user_ids:
        if await has_sign_in_method(session, user_id):
            found[user_id] = ContactAccess("active", None)
            continue
        expires = await session.scalar(
            select(func.max(AuthActionToken.expires_at)).where(
                AuthActionToken.org_id == org_id,
                AuthActionToken.user_id == user_id,
                AuthActionToken.purpose == action_tokens.INVITE,
                AuthActionToken.used_at.is_(None),
                # An invitation that ran out is no invitation (the link answers 410).
                AuthActionToken.expires_at > func.now(),
            )
        )
        found[user_id] = (
            ContactAccess("none", None) if expires is None else ContactAccess("invited", expires)
        )
    return found


async def _one(session: AsyncSession, org_id: UUID, user_id: UUID) -> ContactAccess:
    return (await access_of(session, org_id=org_id, user_ids=[user_id]))[user_id]


async def _contact(
    session: AsyncSession, *, org_id: UUID, client_id: UUID, contact_id: UUID
) -> User:
    """The contact of a visible, active client -- the client held FOR SHARE
    first, as `services.clients.update_contact` does, so an archive in flight
    is waited for and seen."""
    status = (
        await session.execute(
            select(Client.status)
            .where(Client.id == client_id, Client.org_id == org_id)
            .with_for_update(read=True)
        )
    ).scalar_one_or_none()
    user = (
        await session.execute(
            select(User).where(
                User.id == contact_id,
                User.org_id == org_id,
                User.kind == CONTACT_KIND,
                User.client_id == client_id,
            )
        )
    ).scalar_one_or_none()
    if status is None or user is None:
        raise ClientError(404, tr("no such contact"))
    if status == CLIENT_ARCHIVED:
        raise ClientError(409, tr("that client is archived"))
    if user.status != "active":
        raise ClientError(409, tr("switch the contact on first"))
    return user


async def _issue(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    user: User,
    console_url: str,
    deliver: Deliver,
    reset: bool,
) -> tuple[str, bool]:
    token = await action_tokens.issue(
        session,
        org_id=org_id,
        user_id=user.id,
        purpose=action_tokens.INVITE,
        ttl=action_tokens.INVITE_TTL,
        created_by=actor_id,
    )
    link = action_tokens.link(console_url, "invite", token)
    return link, await deliver(user, link, reset)


async def invite(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    client_id: UUID,
    contact_id: UUID,
    console_url: str,
    deliver: Deliver,
) -> ContactInvited:
    user = await _contact(session, org_id=org_id, client_id=client_id, contact_id=contact_id)
    before = await _one(session, org_id, user.id)
    if before.state == "active":
        raise ClientError(409, tr("that contact already signs in; reset their access instead"))
    link, delivered = await _issue(
        session,
        org_id=org_id,
        actor_id=actor_id,
        user=user,
        console_url=console_url,
        deliver=deliver,
        reset=False,
    )
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="contact.invited",
        target_type="user",
        target_id=str(user.id),
        client_id=user.client_id,
        metadata={
            "client_id": str(client_id),
            "email": user.email,
            "resent": before.state == "invited",
            "link_shown": not delivered,
        },
    )
    return ContactInvited(user, await _one(session, org_id, user.id), link, delivered)


async def cancel_invite(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, client_id: UUID, contact_id: UUID
) -> User:
    user = await _contact(session, org_id=org_id, client_id=client_id, contact_id=contact_id)
    if (await _one(session, org_id, user.id)).state != "invited":
        raise ClientError(409, tr("that contact has no pending invitation"))
    await action_tokens.revoke_open(session, user_id=user.id, purpose=action_tokens.INVITE)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="contact.invite_cancelled",
        target_type="user",
        target_id=str(user.id),
        client_id=user.client_id,
        metadata={"client_id": str(client_id), "email": user.email},
    )
    return user


async def reset_access(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    client_id: UUID,
    contact_id: UUID,
    console_url: str,
    deliver: Deliver,
) -> ContactInvited:
    """Decision 3: every way in and everything they opened go, under the
    person's lock (`factors.lock_person`), and a new invitation is issued."""
    user = await _contact(session, org_id=org_id, client_id=client_id, contact_id=contact_id)
    if (await _one(session, org_id, user.id)).state != "active":
        raise ClientError(409, tr("that contact has not signed in yet; send an invitation instead"))
    await lock_person(session, user.id)
    await session.execute(delete(AuthPassword).where(AuthPassword.user_id == user.id))
    await clear_second_factors(session, user.id)
    await session.execute(
        delete(AuthExternalIdentity).where(AuthExternalIdentity.user_id == user.id)
    )
    ended = await end_all_for_user(session, user.id, "factors_reset")
    await revoke_all_for_user(session, user.id)
    await action_tokens.revoke_open(session, user_id=user.id, purpose=action_tokens.RESET)
    link, delivered = await _issue(
        session,
        org_id=org_id,
        actor_id=actor_id,
        user=user,
        console_url=console_url,
        deliver=deliver,
        reset=True,
    )
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="contact.access_reset",
        target_type="user",
        target_id=str(user.id),
        client_id=user.client_id,
        metadata={
            "client_id": str(client_id),
            "email": user.email,
            "sessions_ended": ended,
            "link_shown": not delivered,
        },
    )
    return ContactInvited(user, await _one(session, org_id, user.id), link, delivered)
```

- [ ] **Step 7: Desactivar o archivar corta el acceso**

En `backend/app/services/clients.py`, importar `from app.core.auth import action_tokens`, `from app.core.auth.devices import revoke_all_for_user` y `from app.core.auth.sessions import end_all_for_user`; añadir antes de `_disable_contacts`:

```python
async def _cut_access(session: AsyncSession, user: User) -> None:
    """A contact switched off -- by hand or with its client's archive -- signs
    in no more, at once (design §2.4; plan D43): their sessions end, their
    remembered devices are forgotten, and their open invitation and reset
    links die."""
    await end_all_for_user(session, user.id, "disabled")
    await revoke_all_for_user(session, user.id)
    for purpose in (action_tokens.INVITE, action_tokens.RESET):
        await action_tokens.revoke_open(session, user_id=user.id, purpose=purpose)
```

en `_disable_contacts`, después de `await session.flush()`: `await _cut_access(session, user)`; y en `update_contact`, después de `await _flush_contact(session)` (dentro del `if changed:`):

```python
        if changed.get("status", {}).get("after") == "disabled":
            await _cut_access(session, user)
```

- [ ] **Step 8: Las rutas**

En `backend/app/api/v1/clients.py`: imports `from app.api.deps import AuthContext, require_permission, require_permission_on_every_client, require_permission_with_step_up`; `from fastapi import Response`; `from app.core.auth import notices`; `from app.core.i18n import locale_var, tr, use_locale`; `from app.services import contact_access`; `from sqlalchemy.ext.asyncio import AsyncSession`. En la tabla del docstring, tres filas nuevas:

```
| `POST /clients/{id}/contacts/{cid}/invite`, `DELETE` the same | `clients.manage` on that client |
| `POST /clients/{id}/contacts/{cid}/access/reset` | `clients.manage` on that client, and a recent second factor |
```

`ContactOut` gana:

```python
    access: Literal["none", "invited", "active"] = Field(
        description="Portal access: none, invited (a link is open) or active (can sign in)"
    )
    invite_expires_at: datetime | None = Field(description="When the open invitation expires")
```

`ContactCreate` cambia su docstring a «A new contact of a client. They sign in to the portal once invited (`POST .../invite`).»; `_contact_out` pasa a:

```python
def _contact_out(user: User, access: contact_access.ContactAccess) -> ContactOut:
    assert user.client_id is not None  # a contact always has one (ck_users_kind_client)
    return ContactOut(
        id=user.id,
        client_id=user.client_id,
        kind=user.kind,
        email=user.email,
        display_name=user.display_name,
        phone=user.phone,
        status=user.status,
        access=access.state,
        invite_expires_at=access.invite_expires_at,
    )
```

y, en `list_contacts`, `create_contact` y `update_contact`, la vista se hace dentro del bloque con `access = await contact_access.access_of(session, org_id=ctx.org_id, user_ids=[u.id for u in users])` (o `[user.id]`) y `_contact_out(user, access[user.id])`. Las rutas nuevas, al final:

```python
class ContactInviteOut(BaseModel):
    """A contact's invitation; its link only when no mail carried it, once."""

    contact: ContactOut
    invite_link: str | None = Field(description="The one-time link, when no mail carried it")


def _console_url(request: Request) -> str:
    """`ITS_PUBLIC_CONSOLE_URL`, never the Host header (`user_admin/api.py::_console_url`)."""
    url = request.app.state.settings.public_console_url
    if not url:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            tr(
                "this installation's console address is not configured, so an invitation link "
                "cannot be made. That is a server setting, not something this screen can fix."
            ),
        )
    return str(url)


def _mailer(session: AsyncSession, request: Request, ctx: AuthContext) -> contact_access.Deliver:
    """The invitation, in the contact's language, from the technician who sent
    it, in the request's session (the relay checkout is audited there)."""
    inviter = (ctx.principal.display_name if ctx.principal else "") or ctx.email

    async def deliver(user: User, link: str, reset: bool) -> bool:
        with use_locale(user.locale or locale_var.get()):
            subject, text = notices.contact_invitation_mail(link, inviter=inviter, reset=reset)
        return await notices.deliver(
            session,
            org_id=ctx.org_id,
            to=user.email,
            subject=subject,
            text=text,
            vault=request.app.state.vault,
            actor_id=ctx.user_id,
        )

    return deliver


def _invited_out(invited: contact_access.ContactInvited) -> ContactInviteOut:
    return ContactInviteOut(
        contact=_contact_out(invited.contact, invited.access),
        invite_link=None if invited.delivered else invited.link,
    )


@router.post("/clients/{client_id}/contacts/{contact_id}/invite", response_model=ContactInviteOut)
async def invite_contact(
    client_id: UUID,
    contact_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("clients.manage")),
) -> ContactInviteOut:
    """Send a contact a one-time link to the portal, valid 72 hours, replacing
    any open one. The link comes back only when no mail carried it."""
    console = _console_url(request)
    try:
        async with org_session(
            ctx.org_id, clients=ctx.clients, engine=request.app.state.app_engine
        ) as session:
            invited = await contact_access.invite(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                client_id=client_id,
                contact_id=contact_id,
                console_url=console,
                deliver=_mailer(session, request, ctx),
            )
            out = _invited_out(invited)
    except svc.ClientError as exc:
        raise _http(exc) from None
    return out


@router.delete(
    "/clients/{client_id}/contacts/{contact_id}/invite", status_code=status.HTTP_204_NO_CONTENT
)
async def cancel_contact_invite(
    client_id: UUID,
    contact_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission("clients.manage")),
) -> Response:
    """Cancel a contact's pending invitation: its link stops working; the
    contact stays."""
    try:
        async with org_session(
            ctx.org_id, clients=ctx.clients, engine=request.app.state.app_engine
        ) as session:
            await contact_access.cancel_invite(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                client_id=client_id,
                contact_id=contact_id,
            )
    except svc.ClientError as exc:
        raise _http(exc) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/clients/{client_id}/contacts/{contact_id}/access/reset", response_model=ContactInviteOut
)
async def reset_contact_access(
    client_id: UUID,
    contact_id: UUID,
    request: Request,
    ctx: AuthContext = Depends(require_permission_with_step_up("clients.manage")),
) -> ContactInviteOut:
    """Reset a contact's access: their password, passkeys, authenticator app,
    Microsoft link, remembered devices and sessions go, and a new invitation
    is sent. Needs a recent second-factor confirmation."""
    console = _console_url(request)
    try:
        async with org_session(
            ctx.org_id, clients=ctx.clients, engine=request.app.state.app_engine
        ) as session:
            invited = await contact_access.reset_access(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                client_id=client_id,
                contact_id=contact_id,
                console_url=console,
                deliver=_mailer(session, request, ctx),
            )
            out = _invited_out(invited)
    except svc.ClientError as exc:
        raise _http(exc) from None
    return out
```

En `backend/app/api/v1/auth.py`: `InviteOut` pasa a:

```python
class InviteOut(BaseModel):
    """Who an invitation is for, and how they may accept it, for its page."""

    email: str
    display_name: str
    kind: Literal["staff", "contact"] = Field(description="A technician or a client's contact.")
    password_minimum: int = Field(description="The shortest password this person may choose.")
    passkey: bool = Field(description="The invitation may be accepted with a passkey (contacts).")
    microsoft: bool = Field(description="The invitation may be accepted with Microsoft.")
```

`invite_details` devuelve `InviteOut(**vars(await flows.invite_details(request, token=body.token)))`; `accept_invite` no cambia (su respuesta ya es `SignInOut`); y, después de `accept_invite`:

```python
class InvitePasskeyOptionsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    #: A one-time link's token: a secret, no Pydantic bound (`LinkTokenIn`).
    token: str


class InvitePasskeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str
    credential: dict[str, Any]
    name: str = Field(min_length=1, max_length=100)


@router.post("/invite/passkey/options", dependencies=[Depends(_LINK)])
async def invite_passkey_options(body: InvitePasskeyOptionsIn, request: Request) -> dict[str, Any]:
    """A contact's first passkey, from the invitation: the creation options.
    Leaves the link unused."""
    return await flows.invite_passkey_options(request, token=body.token)


@router.post(
    "/invite/passkey",
    response_model=SignInOut,
    response_model_exclude_unset=True,
    dependencies=[Depends(_LINK)],
)
async def invite_passkey(body: InvitePasskeyIn, request: Request, response: Response) -> SignInOut:
    """Accept a contact's invitation with a passkey: signed in. A refused
    passkey leaves the link usable."""
    step = await flows.invite_passkey(
        request, token=body.token, credential=body.credential, name=body.name
    )
    return _answer(response, step)
```

(y sus dos líneas en el índice del docstring, debajo de `/auth/invite/accept`).

- [ ] **Step 9: MCP, rutas, vocabulario y textos**

En `backend/app/mcp/exclusions.py`: en la comprensión de `/api/v1/auth/{path}`:

```python
            (
                "invite/passkey/options",
                "issues a passkey registration challenge from an invitation",
            ),
            (
                "invite/passkey",
                "consumes an invitation link, binds a passkey and mints a session cookie",
            ),
```

y, junto a las invitaciones de técnicos:

```python
    ("POST", "/api/v1/clients/{client_id}/contacts/{contact_id}/invite"): (
        "returns a one-time portal invitation link when no mail carried it, which lets a person in"
    ),
    ("POST", "/api/v1/clients/{client_id}/contacts/{contact_id}/access/reset"): (
        "removes a contact's ways in and returns a new invitation link when no mail carried it; "
        "needs the technician's own recent second-factor confirmation in the console"
    ),
```

En `backend/app/mcp/descriptions/part_d.py`, en «clients»:

```python
    "DELETE /api/v1/clients/{client_id}/contacts/{contact_id}/invite": (
        "Cancels a contact's pending portal invitation: the link stops working and the contact "
        "stays. A 409 when none is pending."
    ),
```

y la de `GET /api/v1/clients/{client_id}/contacts` acaba «…with their status and whether each can sign in to the portal (none, invited or active).».

En `backend/tests/test_mcp_channel_api.py`: `"invite/passkey/options"`, `"invite/passkey"` en la tupla de `OWN_SIGN_IN_NEVER_EXPOSED`, y en el conjunto `("POST", "/api/v1/clients/{client_id}/contacts/{contact_id}/invite")` y `("POST", "/api/v1/clients/{client_id}/contacts/{contact_id}/access/reset")`; el recuento +4 respecto del que veas.

En `backend/tests/test_route_gating.py`, en `UNGATED_BY_DESIGN`, junto a `invite/accept`:

```python
        # A contact's first passkey, from the invitation (P2 phase 2, D40):
        # the 256-bit link, `_LINK` per address and the single-use challenge.
        ("POST", "/api/v1/auth/invite/passkey/options"),
        ("POST", "/api/v1/auth/invite/passkey"),
```

En `frontend/src/i18n/en/auditVocabulary.ts`, en orden: `contact_access_reset: "Contact access reset",`, `contact_invite_accepted: "Contact invite accepted",`, `contact_invite_cancelled: "Contact invite cancelled",`, `contact_invited: "Contact invited",`. En `es`: `"Acceso del contacto restablecido"`, `"Invitación del contacto aceptada"`, `"Invitación del contacto cancelada"`, `"Contacto invitado"`. En `frontend/src/modules/audit/auditVocabulary.coverage.test.ts`, en la entrada `DYNAMIC` de `core/auth/flows.py`, añadir `"contact.invite_accepted"` a `codes`.

En `backend/app/core/i18n/es.py`:

```python
    "switch the contact on first": "activa el contacto primero",
    "that contact already signs in; reset their access instead": (
        "ese contacto ya inicia sesión; restablece su acceso en su lugar"
    ),
    "that contact has no pending invitation": "ese contacto no tiene una invitación pendiente",
    "that contact has not signed in yet; send an invitation instead": (
        "ese contacto aún no ha iniciado sesión; envíale una invitación en su lugar"
    ),
    "Your access to the {product} portal was reset": (
        "Se restableció tu acceso al portal de {product}"
    ),
    "{inviter} reset your access to the {product} portal: your previous password, passkeys and "
    "linked accounts no longer work.\n\n"
    "Open this link within 72 hours to choose how you will sign in:\n{link}\n\n"
    "If you did not expect this, tell {inviter}.": (
        "{inviter} restableció tu acceso al portal de {product}: tu contraseña, tus claves de "
        "acceso y tus cuentas vinculadas anteriores ya no funcionan.\n\n"
        "Abre este enlace en las próximas 72 horas para elegir cómo iniciarás sesión:\n{link}\n\n"
        "Si no lo esperabas, díselo a {inviter}."
    ),
    "You are invited to the {product} portal": "Te han invitado al portal de {product}",
    "{inviter} invited you to the {product} portal, where you can follow your requests.\n\n"
    "Open this link within 72 hours to choose how you will sign in:\n{link}\n\n"
    "If you did not expect this, ignore this message.": (
        "{inviter} te invitó al portal de {product}, donde puedes seguir tus solicitudes.\n\n"
        "Abre este enlace en las próximas 72 horas para elegir cómo iniciarás sesión:\n{link}\n\n"
        "Si no lo esperabas, ignora este mensaje."
    ),
```

- [ ] **Step 10: Correr las pruebas**

Run: `.venv/bin/pytest -q tests/test_contact_access_api.py tests/test_clients_api.py tests/test_user_admin_api.py tests/test_auth_signin.py tests/test_auth_contacts_signin.py tests/test_mcp_channel_api.py tests/test_route_gating.py tests/test_mcp_catalog.py tests/test_mcp_descriptions.py tests/test_i18n_catalog.py tests/test_client_catalog.py $(grep -l "invite" tests/test_auth_*.py)`
Expected: PASS. Si una prueba de la fase 1 compara el cuerpo entero de `POST /auth/invite/details` de un técnico, su esperado gana `"kind": "staff", "password_minimum": 12, "passkey": False, "microsoft": False` (re-apuntado por D40; el informe lo dice). Si una de `tests/test_clients_api.py` compara un `ContactOut` entero, gana `"access": "none", "invite_expires_at": None`.

- [ ] **Step 11: Tipos, estilo y vocabulario**

Run: `.venv/bin/mypy app && .venv/bin/ruff check app tests && cd ../frontend && npx vitest run src/modules/audit/auditVocabulary.coverage.test.ts src/i18n`
Expected: verde.

- [ ] **Step 12: Commit**

```bash
git add backend/app/core/auth/ backend/app/services/contact_access.py backend/app/services/clients.py \
  backend/app/api/v1/clients.py backend/app/api/v1/auth.py backend/app/mcp/ backend/app/core/i18n/es.py \
  backend/tests/ frontend/src/i18n/en/auditVocabulary.ts frontend/src/i18n/es/auditVocabulary.ts \
  frontend/src/modules/audit/auditVocabulary.coverage.test.ts
git commit -m "feat(clients): invite, cancel and reset a contact's portal access; accept with a password or passkey"
```

---

### Task 9: Microsoft, los flujos — entrar, vincular, aceptar una invitación, desvincular

Diseño §4: técnicos solo del tenant de Novieri y después el segundo factor propio («salvo equipo recordado»); contactos desde los tenants de su cliente; «Nunca se crea una cuenta por entrar con Microsoft. La persona tiene que estar invitada; el enlace se hace la primera vez desde la invitación o desde «Mi perfil» y guarda `tid` + `oid` … **nunca se enlaza por correo**»; §10.8: «Microsoft con `tid`/`oid` desconocido se rechaza; nunca se crea cuenta ni se enlaza por correo». D30–D32, D41. Oleada 5; depende de las Tasks 4, 5, 7 y 8.

**Files:**
- Create: `backend/app/core/auth/entra_flows.py`
- Modify: `backend/app/core/auth/sessions.py` (`ENTRA_COOKIE`, `set_entra_cookie`, `clear_entra_cookie`)
- Modify: `backend/app/core/auth/notices.py` (`_factor_name("microsoft")`)
- Modify: `backend/app/api/v1/auth.py` (`_MICROSOFT`, tres modelos, `POST /auth/microsoft/start`, `POST /auth/microsoft/complete`, `DELETE /me/microsoft`)
- Modify: `backend/app/modules/user_admin/service.py` (`_open_invites`, `_accepted`)
- Modify: `backend/app/core/client_scope.py` (`ALL_SCOPE_ALLOWLIST`, una entrada)
- Modify: `backend/app/mcp/exclusions.py` (+2 `NEVER_EXPOSED_REASONS`, +1 `NOT_FOR_MCP`)
- Modify: `backend/app/core/i18n/es.py`
- Modify: `backend/tests/test_mcp_channel_api.py`, `backend/tests/test_route_gating.py`
- Create: `backend/tests/test_auth_entra_flows.py`
- Test: el nuevo, `backend/tests/test_auth_entra.py`, `backend/tests/test_user_admin_api.py`, `backend/tests/test_client_catalog.py`, `backend/tests/test_auth_csrf.py`

**Interfaces:**
- Consumes: `entra.*`, `entra_settings.read`/`checkout_secret` (Task 5); `flows.after_first_factor`, `flows.started` (Task 4); `flows.invitee`, `flows.contact_admitted`, `action_tokens.digest`/`org_of` (Task 8); `methods.has_password`, `has_sign_in_method` (Task 4); `guards.session_of`, `require_fresh`, `signed_user`, `sign_in_again`; `sessions.resolve`, `cookie_token`; `lookup.org_of_slug` (Task 1).
- Produces: Apéndice A, Task 9.

**Decisión del plan (1): la transacción, entera** (D30). `start` crea tres valores aleatorios de 256 bits: el `state` (su SHA-256 es la clave en Redis), el `nonce` y el verificador PKCE, que va **solo** en la cookie `__Host-itms_entra`; Redis guarda su `S256`, la organización, la intención, la autoridad y, según la intención, la sesión que vincula o el resumen del enlace de la invitación. Nada de eso es secreto. `complete` toma la entrada con `GETDEL` (un solo uso), exige que el `S256` de la cookie coincida, canjea, valida el ID token con el `nonce` y solo entonces mira quién es. Así, un código robado o inyectado en otro navegador no sirve: falta la cookie ([R1] §2.1, §2.1.1).

**Decisión del plan (2): la persona la dice la fila, el tenant lo dice su tipo** (D31, D32). `sign_in` busca `(tid, oid)` en `auth_external_identities`; sin fila, o con una persona que no está activa, 401 `microsoft_not_linked`, sin crear nada ni mirar el `email`. Encontrada, un técnico solo vale con `tid = staff_tenant_id`; un contacto, con un `tid` de los `microsoft_tenants` de su cliente activo; si no, 403 `microsoft_tenant_refused`. Después, el camino de la contraseña (`after_first_factor(method="microsoft")`): reanudar una sesión inactiva, el segundo factor propio (salvo equipo recordado) o, un contacto sin él, la sesión completa.

**Decisión del plan (3): vincular exige la sesión que empezó, y una confirmación reciente al empezar** (§2.5: cambiar los propios factores). Al volver, la cookie de sesión del navegador debe nombrar la misma sesión, todavía completa. Una cuenta de Microsoft ya vinculada a otra persona es 409 `microsoft_already_linked`; quien ya tiene una vinculada la quita antes (409, el mismo código, otra frase). Se audita `auth.identity_linked` (`metadata.provider = "entra"`) y se avisa por correo como un factor añadido (§2.7).

**Decisión del plan (4): aceptar una invitación con Microsoft** consume el enlace en la misma transacción que crea la vinculación (un tenant rechazado lo deja usable). Un contacto queda dentro (`contact_admitted`, D40); un técnico, sin contraseña y con su primer factor por registrar (D41; Pregunta P2).

**Decisión del plan (5): desvincular nunca deja a nadie fuera.** Un técnico conserva su contraseña (su primer factor: una clave de acceso de técnico no lo es); un contacto, alguna forma de entrar. Si no, 409 `last_method`. Con confirmación reciente.

**Decisión del plan (6): `user_admin` cuenta como aceptada una invitación con Microsoft.** `_open_invites` y `_accepted` miraban solo la contraseña; un técnico que aceptó con Microsoft no tiene, y el reenvío le respondía «no tiene invitación pendiente» en vez de «ya la aceptó». Ahora miran cualquier forma de entrar.

- [ ] **Step 1: Escribir las pruebas**

Crear `backend/tests/test_auth_entra_flows.py`:

```python
"""Sign in with Microsoft, the flows (plan 2026-11-06, Task 9; design §4,
falsifier 8; decisions D30-D32, D41)."""

from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.sessions import ENTRA_COOKIE, SESSION_COOKIE
from app.models import AuditEvent, Client, User
from app.models.auth import AuthExternalIdentity
from tests.auth_support import (
    browser,
    capture_mail,
    console_ready,
    contact,
    cookie,
    link_token,
    point_at,
    signed_in,
)
from tests.entra_support import APP, TENANT, FakeMicrosoft

OTHER_TENANT = UUID("22222222-3333-4444-8555-666666666666")


@pytest.fixture()
def auth_env(api_env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    console_ready(api_env, monkeypatch)
    api_env.mail = capture_mail(monkeypatch)
    api_env.microsoft = FakeMicrosoft()
    api_env.microsoft.install(monkeypatch, api_env.app)
    return api_env


async def _org(env: Any) -> tuple[UUID, dict[str, str]]:
    """An organization with sign-in with Microsoft switched on, and an admin."""
    org = await env.create_org("entra-flow")
    await point_at(env, org)
    admin = await env.create_user(org, "org-admin")
    headers = await env.headers(org, admin)
    saved = await env.client.put(
        "/api/v1/settings/microsoft-sign-in",
        headers=headers,
        json={
            "enabled": True,
            "app_client_id": str(APP),
            "staff_tenant_id": str(TENANT),
            "client_secret": "the-app-secret",
        },
    )
    assert saved.status_code == 200, saved.text
    return org, headers


def _with_cookie(headers: dict[str, str], name: str, value: str) -> dict[str, str]:
    jar = [part for part in headers.get("Cookie", "").split("; ") if part]
    return {**headers, "Cookie": "; ".join([*jar, f"{name}={value}"])}


async def _round_trip(
    env: Any,
    headers: dict[str, str],
    body: dict[str, Any],
    *,
    oid: UUID,
    tid: UUID = TENANT,
    nonce: str | None = None,
) -> httpx.Response:
    start = await env.client.post("/api/v1/auth/microsoft/start", headers=headers, json=body)
    assert start.status_code == 200, start.text
    verifier = cookie(start, ENTRA_COOKIE)
    assert verifier is not None
    query = parse_qs(urlsplit(start.json()["authorize_url"]).query)
    code = env.microsoft.issue_code(nonce=nonce or query["nonce"][0], tid=tid, oid=oid)
    return await env.client.post(
        "/api/v1/auth/microsoft/complete",
        headers=_with_cookie(headers, ENTRA_COOKIE, verifier),
        json={"code": code, "state": query["state"][0]},
    )


async def _lists_other_tenant(env: Any, client_id: UUID) -> None:
    """The contact's client lists `OTHER_TENANT` (D35), written past the API."""
    async with AsyncSession(env.admin_engine) as s:
        await s.execute(
            update(Client).where(Client.id == client_id).values(microsoft_tenants=[OTHER_TENANT])
        )
        await s.commit()


async def _counts(env: Any, org: UUID) -> tuple[int, int]:
    async with AsyncSession(env.admin_engine) as s:
        users = await s.scalar(select(func.count()).select_from(User).where(User.org_id == org))
        links = await s.scalar(
            select(func.count())
            .select_from(AuthExternalIdentity)
            .where(AuthExternalIdentity.org_id == org)
        )
    return int(users or 0), int(links or 0)


async def test_falsifier_8_an_unknown_account_is_refused_and_nothing_is_made(
    auth_env: Any,
) -> None:
    org, _admin = await _org(auth_env)
    # A technician whose address is the very one the token's `email` claim says.
    async with AsyncSession(auth_env.admin_engine) as s:
        s.add(
            User(org_id=org, external_subject=f"sub-{uuid4().hex[:8]}", email="dana@example.test")
        )
        await s.commit()
    before = await _counts(auth_env, org)
    answer = await _round_trip(
        auth_env, browser(), {"intent": "sign_in", "audience": "staff"}, oid=uuid4()
    )
    assert answer.status_code == 401
    assert answer.json()["code"] == "microsoft_not_linked"
    assert cookie(answer, SESSION_COOKIE) is None
    assert await _counts(auth_env, org) == before


async def test_a_technician_links_from_my_security_then_signs_in_and_is_asked_the_factor(
    auth_env: Any,
) -> None:
    org, _admin = await _org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    oid = uuid4()
    linked = await _round_trip(auth_env, tech.headers(), {"intent": "link"}, oid=oid)
    assert linked.status_code == 200, linked.text
    assert linked.json() == {"state": "linked"}
    async with AsyncSession(auth_env.admin_engine) as s:
        row = (
            await s.execute(
                select(AuthExternalIdentity).where(AuthExternalIdentity.user_id == tech.user_id)
            )
        ).scalar_one()
        events = (
            await s.execute(
                select(AuditEvent.meta).where(
                    AuditEvent.org_id == org, AuditEvent.action == "auth.identity_linked"
                )
            )
        ).scalars().all()
    assert (row.tenant_id, row.subject) == (TENANT, oid)
    assert list(events) == [{"provider": "entra", "tenant_id": str(TENANT), "via": "profile"}]

    signed = await _round_trip(
        auth_env, tech.network, {"intent": "sign_in", "audience": "staff"}, oid=oid
    )
    assert signed.status_code == 200, signed.text
    assert signed.json()["state"] == "factor_required"
    assert cookie(signed, SESSION_COOKIE) is not None


async def test_a_technician_from_another_tenant_is_refused(auth_env: Any) -> None:
    org, _admin = await _org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    refused = await _round_trip(
        auth_env, tech.headers(), {"intent": "link"}, oid=uuid4(), tid=OTHER_TENANT
    )
    assert refused.status_code == 403
    assert refused.json()["code"] == "microsoft_tenant_refused"


async def test_a_contact_accepts_the_invitation_with_microsoft_in_a_tenant_of_their_client(
    auth_env: Any,
) -> None:
    org, admin = await _org(auth_env)
    who = await contact(auth_env, org, password=None)
    await _lists_other_tenant(auth_env, who.client_id)
    sent = await auth_env.client.post(
        f"/api/v1/clients/{who.client_id}/contacts/{who.user_id}/invite", headers=admin
    )
    assert sent.status_code == 200, sent.text
    token = link_token(auth_env.mail.sent[-1].text)
    details = await auth_env.client.post(
        "/api/v1/auth/invite/details", headers=browser(), json={"token": token}
    )
    assert details.json()["microsoft"] is True
    oid = uuid4()
    accepted = await _round_trip(
        auth_env, browser(), {"intent": "invite", "token": token}, oid=oid, tid=OTHER_TENANT
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {"state": "signed_in"}
    again = await _round_trip(
        auth_env, browser(), {"intent": "sign_in", "audience": "contact"}, oid=oid, tid=OTHER_TENANT
    )
    assert again.json() == {"state": "signed_in"}


async def test_the_transaction_is_single_use_and_bound_to_its_browser(auth_env: Any) -> None:
    await _org(auth_env)
    start = await auth_env.client.post(
        "/api/v1/auth/microsoft/start",
        headers=browser(),
        json={"intent": "sign_in", "audience": "staff"},
    )
    query = parse_qs(urlsplit(start.json()["authorize_url"]).query)
    code = auth_env.microsoft.issue_code(nonce=query["nonce"][0], oid=uuid4())
    other_browser = _with_cookie(browser(), ENTRA_COOKIE, "A" * 43)
    stolen = await auth_env.client.post(
        "/api/v1/auth/microsoft/complete",
        headers=other_browser,
        json={"code": code, "state": query["state"][0]},
    )
    assert stolen.status_code == 400
    assert stolen.json()["code"] == "microsoft_failed"
    assert auth_env.microsoft.token_requests == []
    replay = await auth_env.client.post(
        "/api/v1/auth/microsoft/complete",
        headers=_with_cookie(browser(), ENTRA_COOKIE, cookie(start, ENTRA_COOKIE) or ""),
        json={"code": code, "state": query["state"][0]},
    )
    assert replay.status_code == 400, "the state was spent by the first attempt"


async def test_a_token_with_another_nonce_is_refused(auth_env: Any) -> None:
    await _org(auth_env)
    answer = await _round_trip(
        auth_env,
        browser(),
        {"intent": "sign_in", "audience": "staff"},
        oid=uuid4(),
        nonce="not-the-nonce-that-was-sent",
    )
    assert answer.status_code == 400
    assert answer.json()["code"] == "microsoft_failed"


async def test_unlinking_never_leaves_somebody_without_a_way_in(auth_env: Any) -> None:
    org, admin = await _org(auth_env)
    tech = await signed_in(auth_env, org, "technician")
    await _round_trip(auth_env, tech.headers(), {"intent": "link"}, oid=uuid4())
    unlinked = await auth_env.client.delete("/api/v1/me/microsoft", headers=tech.headers())
    assert unlinked.status_code == 204, unlinked.text

    who = await contact(auth_env, org, password=None)
    await _lists_other_tenant(auth_env, who.client_id)
    await auth_env.client.post(
        f"/api/v1/clients/{who.client_id}/contacts/{who.user_id}/invite", headers=admin
    )
    token = link_token(auth_env.mail.sent[-1].text)
    accepted = await _round_trip(
        auth_env, browser(), {"intent": "invite", "token": token}, oid=uuid4(), tid=OTHER_TENANT
    )
    session = cookie(accepted, SESSION_COOKIE)
    refused = await auth_env.client.delete(
        "/api/v1/me/microsoft",
        headers={**browser(), "Cookie": f"{SESSION_COOKIE}={session}"},
    )
    assert refused.status_code == 409
    assert refused.json()["code"] == "last_method"
```

- [ ] **Step 2: Verlas fallar**

Run: `.venv/bin/pytest -q tests/test_auth_entra_flows.py`
Expected: FAIL en la colección, `ImportError: cannot import name 'ENTRA_COOKIE' from 'app.core.auth.sessions'`.

- [ ] **Step 3: La cookie de la transacción**

En `backend/app/core/auth/sessions.py`, debajo de `DEVICE_COOKIE`:

```python
#: Sign in with Microsoft's transaction (own authentication phase 2, plan
#: decision D30): its value is the PKCE code verifier, so the transaction is
#: bound to the browser that started it. Ten minutes, then gone.
ENTRA_COOKIE = "__Host-itms_entra"
```

y, después de `clear_device_cookie`:

```python
def set_entra_cookie(response: Response, value: str, *, max_age: int) -> None:
    """Sign in with Microsoft's cookie (D30), the same attributes as the others."""
    _set(response, ENTRA_COOKIE, value, max_age)


def clear_entra_cookie(response: Response) -> None:
    _set(response, ENTRA_COOKIE, "", 0)
```

- [ ] **Step 4: Los flujos**

Crear `backend/app/core/auth/entra_flows.py`:

```python
"""Sign in with Microsoft, the flows (design §4; plan 2026-11-06, Task 9;
decisions D30-D32, D41, and this task's 1-5).

THREE INTENTS, ONE RETURN PAGE (`/sign-in/microsoft`, Task 12):

- `sign_in`: nobody is signed in. The account must already be linked -- by
  (`tid`, `oid`), never by address -- to an active person (falsifier 8):
  nothing is ever created here. Then the password's path: an idle session
  resumed, the platform's own second factor (save a remembered device), or a
  contact without one, signed in (`flows.after_first_factor`).
- `link`: a signed-in person, with a recent confirmation (design §2.5),
  links their own account from My security.
- `invite`: somebody accepting an invitation links their account instead of
  choosing a password (D40, D41).

THE TRANSACTION (decision 1). `start` makes the `state` (the Redis key is its
SHA-256), the `nonce` and the PKCE verifier, which travels ONLY in the
`__Host-itms_entra` cookie; Redis keeps its S256 and the rest, none of it
secret, for `ENTRA_TTL_S`. `complete` takes the entry once (GETDEL), checks
the cookie against it, redeems the code, validates the ID token with the
nonce -- and only then looks at who it is. RFC 9700 §2.1.1: "the PKCE
challenge or OpenID Connect nonce MUST be transaction-specific and securely
bound to the client and the user agent in which the transaction was started".

THE TENANT IS THE KIND'S (decision 2, D32): a technician only from Novieri's
tenant (`staff_tenant_id`), a contact only from one of their active client's
`microsoft_tenants`.

ONE ORGANIZATION, EVERY CLIENT (`_entra_session`, `ALL_SCOPE_ALLOWLIST`): a
technician's link has no client, a contact's is their client's, and the
lookup by (`tid`, `oid`) happens before anybody's scope exists -- the
organization comes from the deployment's slug, the session or the
invitation, never a request field naming it.
"""

import hashlib
import hmac
import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

import httpx
from cryptography.exceptions import InvalidTag
from fastapi import Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import audit_service
from app.core.auth import action_tokens, entra, entra_settings, flows, notices
from app.core.auth.entra_settings import EntraConfig
from app.core.auth.guards import require_fresh, session_of, sign_in_again, signed_user
from app.core.auth.lookup import org_of_slug
from app.core.auth.methods import has_password, has_sign_in_method
from app.core.auth.refusals import AuthRefusal
from app.core.auth.sessions import ENTRA_COOKIE, SessionPrincipal, cookie_token, resolve
from app.core.client_scope import ClientScope
from app.core.i18n import tr
from app.core.identity import ACTIVE_STATUS, CONTACT_KIND, STAFF_KIND
from app.core.tenancy import org_session
from app.core.vault import VaultService
from app.models import Client, User
from app.models.auth import ENTRA, AuthExternalIdentity

ENTRA_TTL_S = 600
_KEY = "auth:entra:"
#: The longest code accepted before it is sent anywhere.
_CODE_MAX = 4096

Intent = Literal["sign_in", "link", "invite"]
Audience = Literal["staff", "contact"]


@asynccontextmanager
async def _entra_session(org_id: UUID, engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """See the module docstring: one organization, every client."""
    async with org_session(org_id, engine=engine, clients=ClientScope.all()) as session:
        yield session


def _engine(request: Request) -> AsyncEngine:
    engine: AsyncEngine = request.app.state.app_engine
    return engine


def _vault(request: Request) -> VaultService:
    vault: VaultService = request.app.state.vault
    return vault


def _key(state: str) -> str:
    return _KEY + hashlib.sha256(state.encode("utf-8")).hexdigest()


def _refusal(code: str, status_code: int, detail: str) -> AuthRefusal:
    return AuthRefusal(status_code, code=code, detail=detail)


def _unavailable() -> AuthRefusal:
    return _refusal(
        "microsoft_unavailable",
        status.HTTP_409_CONFLICT,
        tr("sign-in with Microsoft is not set up here"),
    )


def _not_configured() -> AuthRefusal:
    return _refusal(
        "microsoft_unavailable",
        status.HTTP_503_SERVICE_UNAVAILABLE,
        tr(
            "signing in is not set up on this installation: its console address is not "
            "configured"
        ),
    )


def _failed() -> AuthRefusal:
    return _refusal(
        "microsoft_failed",
        status.HTTP_400_BAD_REQUEST,
        tr("the sign-in with Microsoft did not finish; start it again"),
    )


def _not_linked() -> AuthRefusal:
    return _refusal(
        "microsoft_not_linked",
        status.HTTP_401_UNAUTHORIZED,
        tr(
            "this Microsoft account is not linked to anybody here; sign in another way and link "
            "it from your profile, or use your invitation"
        ),
    )


def _tenant_refused() -> AuthRefusal:
    return _refusal(
        "microsoft_tenant_refused",
        status.HTTP_403_FORBIDDEN,
        tr("this Microsoft account's organization may not sign in here"),
    )


def _already_linked(detail: str) -> AuthRefusal:
    return _refusal("microsoft_already_linked", status.HTTP_409_CONFLICT, detail)


def _keys_down() -> AuthRefusal:
    return _refusal(
        "sign_in_unavailable",
        status.HTTP_503_SERVICE_UNAVAILABLE,
        tr("signing in is not available right now; try again in a moment"),
    )


def _invalid_link() -> AuthRefusal:
    return _refusal(
        "invalid_link",
        status.HTTP_410_GONE,
        tr(
            "this link is not valid: it has expired, was already used or was replaced by a "
            "newer one"
        ),
    )


async def _tenant_allowed(
    session: AsyncSession, user: User, config: EntraConfig, tenant: UUID
) -> bool:
    """Decision 2 (D32)."""
    if user.kind == STAFF_KIND:
        return config.staff_tenant_id is not None and tenant == config.staff_tenant_id
    if user.kind != CONTACT_KIND or user.client_id is None:
        return False
    row = (
        await session.execute(
            select(Client.status, Client.microsoft_tenants).where(Client.id == user.client_id)
        )
    ).one_or_none()
    return row is not None and row.status == "active" and tenant in row.microsoft_tenants


async def start(
    request: Request, *, intent: Intent, audience: Audience, token: str | None
) -> tuple[str, str]:
    """Microsoft's sign-in URL and the cookie's value (decision 1)."""
    try:
        redirect = entra.redirect_uri(request.app.state.settings)
    except entra.EntraNotConfigured as exc:
        raise _not_configured() from exc
    session_id: UUID | None = None
    invite_digest: bytes | None = None
    if intent == "sign_in":
        slug = request.app.state.settings.default_org_slug
        org_id = await org_of_slug(_engine(request), slug) if slug else None
        if org_id is None:
            raise _unavailable()
        kind: str = audience
    elif intent == "link":
        principal = await session_of(request)
        require_fresh(principal)
        org_id, session_id, kind = principal.org_id, principal.session_id, principal.kind
    else:
        found = action_tokens.org_of(token or "")
        if found is None:
            raise _invalid_link()
        org_id = found
        invite_digest = action_tokens.digest(token or "")
        async with _entra_session(org_id, _engine(request)) as session:
            kind = (await flows.invitee(session, digest=invite_digest, consume=False)).kind
    async with _entra_session(org_id, _engine(request)) as session:
        config = await entra_settings.read(session, org_id)
    if not config.usable or config.app_client_id is None:
        raise _unavailable()
    if kind == STAFF_KIND and intent != "sign_in" and config.staff_tenant_id is None:
        raise _unavailable()
    authority = entra.authority_for(config.staff_tenant_id if kind == STAFF_KIND else None)
    verifier = secrets.token_urlsafe(32)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    pending = {
        "org_id": str(org_id),
        "intent": intent,
        "authority": authority,
        "challenge": entra.challenge_of(verifier),
        "nonce": nonce,
        "session_id": None if session_id is None else str(session_id),
        "invite": None if invite_digest is None else invite_digest.hex(),
    }
    await request.app.state.redis.set(_key(state), json.dumps(pending), ex=ENTRA_TTL_S)
    url = entra.authorize_url(
        authority=authority,
        client_id=config.app_client_id,
        redirect_uri=redirect,
        state=state,
        nonce=nonce,
        code_challenge=entra.challenge_of(verifier),
    )
    return url, verifier


async def complete(request: Request, *, code: str, state: str) -> flows.SignIn | None:
    """Decision 1, then the intent: a sign-in (`SignIn`), a link (None) or an
    invitation accepted (`SignIn`)."""
    verifier = cookie_token(request, ENTRA_COOKIE)
    raw = await request.app.state.redis.getdel(_key(state)) if state else None
    if verifier is None or raw is None or not code or len(code) > _CODE_MAX:
        raise _failed()
    try:
        pending = json.loads(raw)
        org_id = UUID(pending["org_id"])
        intent: Intent = pending["intent"]
        authority = str(pending["authority"])
        nonce = str(pending["nonce"])
        challenge = str(pending["challenge"])
    except (ValueError, KeyError, TypeError) as exc:
        raise _failed() from exc
    if not hmac.compare_digest(entra.challenge_of(verifier), challenge):
        raise _failed()
    async with _entra_session(org_id, _engine(request)) as session:
        config = await entra_settings.read(session, org_id)
        if not config.usable or config.app_client_id is None:
            raise _unavailable()
        try:
            secret = await entra_settings.checkout_secret(
                session, _vault(request), org_id=org_id, config=config, actor_id=None
            )
        except (LookupError, ValueError, InvalidTag, httpx.HTTPError) as exc:
            raise _keys_down() from exc
    try:
        async with entra.http_client() as client:
            id_token = await entra.exchange(
                client=client,
                authority=authority,
                client_id=config.app_client_id,
                client_secret=secret,
                code=code,
                redirect_uri=entra.redirect_uri(request.app.state.settings),
                code_verifier=verifier,
            )
        identity = await entra.validate_id_token(
            id_token, client_id=config.app_client_id, nonce=nonce, keys=entra.keys_of(request.app)
        )
    except entra.EntraRefused as exc:
        raise _failed() from exc
    finally:
        del secret
    if intent == "sign_in":
        return await _sign_in(request, org_id, identity, config)
    if intent == "link":
        await _link(request, org_id, identity, config, UUID(str(pending["session_id"])))
        return None
    return await _accept_invite(
        request, org_id, identity, config, bytes.fromhex(str(pending["invite"]))
    )


async def _identity_of(
    session: AsyncSession, org_id: UUID, identity: entra.EntraIdentity
) -> AuthExternalIdentity | None:
    return (
        await session.execute(
            select(AuthExternalIdentity).where(
                AuthExternalIdentity.org_id == org_id,
                AuthExternalIdentity.provider == ENTRA,
                AuthExternalIdentity.tenant_id == identity.tenant_id,
                AuthExternalIdentity.subject == identity.object_id,
            )
        )
    ).scalar_one_or_none()


async def _sign_in(
    request: Request, org_id: UUID, identity: entra.EntraIdentity, config: EntraConfig
) -> flows.SignIn:
    """Decision 2: the row says who; nothing is created; never the address."""
    async with _entra_session(org_id, _engine(request)) as session:
        link = await _identity_of(session, org_id, identity)
        user = await session.get(User, link.user_id) if link is not None else None
        if link is None or user is None or user.status != ACTIVE_STATUS:
            raise _not_linked()
        if not await _tenant_allowed(session, user, config, identity.tenant_id):
            raise _tenant_refused()
        link.last_used_at = datetime.now(UTC)
        return await flows.after_first_factor(session, request, user, method="microsoft")


async def _add_link(
    session: AsyncSession, org_id: UUID, user: User, identity: entra.EntraIdentity, *, via: str
) -> None:
    """The link, or `microsoft_already_linked`: the unique key is
    organization-wide, so a savepoint catches another person's (Task 1,
    decision 1). Audited `auth.identity_linked`."""
    try:
        async with session.begin_nested():
            session.add(
                AuthExternalIdentity(
                    org_id=org_id,
                    user_id=user.id,
                    provider=ENTRA,
                    tenant_id=identity.tenant_id,
                    subject=identity.object_id,
                )
            )
            await session.flush()
    except IntegrityError as exc:
        detail = tr("this Microsoft account is already linked to somebody here")
        raise _already_linked(detail) from exc
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=user.id,
        action="auth.identity_linked",
        target_type="user",
        target_id=str(user.id),
        metadata={"provider": ENTRA, "tenant_id": str(identity.tenant_id), "via": via},
    )


async def _link(
    request: Request,
    org_id: UUID,
    identity: entra.EntraIdentity,
    config: EntraConfig,
    session_id: UUID,
) -> None:
    """Decision 3: the session that started, still complete."""
    principal = await resolve(request)
    if principal is None or principal.session_id != session_id or not principal.factor_done:
        raise sign_in_again()
    async with _entra_session(org_id, _engine(request)) as session:
        user = await signed_user(session, principal)
        if not await _tenant_allowed(session, user, config, identity.tenant_id):
            raise _tenant_refused()
        mine = await session.scalar(
            select(AuthExternalIdentity.id).where(
                AuthExternalIdentity.user_id == user.id, AuthExternalIdentity.provider == ENTRA
            )
        )
        if mine is not None:
            raise _already_linked(
                tr("you already have a Microsoft account linked; remove it first")
            )
        await _add_link(session, org_id, user, identity, via="profile")
        await notices.tell(
            session,
            user,
            "factor_added",
            vault=_vault(request),
            actor_id=user.id,
            factor="microsoft",
        )


async def _accept_invite(
    request: Request,
    org_id: UUID,
    identity: entra.EntraIdentity,
    config: EntraConfig,
    digest: bytes,
) -> flows.SignIn:
    """Decision 4: the link consumed and the account linked in one
    transaction; a refusal rolls both back and the link stays usable."""
    async with _entra_session(org_id, _engine(request)) as session:
        user = await flows.invitee(session, digest=digest, consume=True)
        if not await _tenant_allowed(session, user, config, identity.tenant_id):
            raise _tenant_refused()
        await _add_link(session, org_id, user, identity, via="invite")
        if user.kind == CONTACT_KIND:
            return await flows.contact_admitted(session, request, user, method="microsoft")
        return await flows.started(session, request, user, state="enroll_required")


async def unlink(request: Request, principal: SessionPrincipal) -> None:
    """Decision 5: never the last way in. With a recent confirmation (the
    route's `require_step_up`, and here)."""
    require_fresh(principal)
    async with _entra_session(principal.org_id, _engine(request)) as session:
        user = await signed_user(session, principal)
        link = (
            await session.execute(
                select(AuthExternalIdentity).where(
                    AuthExternalIdentity.user_id == user.id, AuthExternalIdentity.provider == ENTRA
                )
            )
        ).scalar_one_or_none()
        if link is None:
            raise AuthRefusal(
                status.HTTP_404_NOT_FOUND,
                code="not_found",
                detail=tr("no Microsoft account is linked"),
            )
        tenant = link.tenant_id
        await session.delete(link)
        await session.flush()
        keeps_a_way_in = (
            await has_password(session, user.id)
            if user.kind == STAFF_KIND
            else await has_sign_in_method(session, user.id)
        )
        if not keeps_a_way_in:
            # Raised inside the block, after the delete: it rolls back.
            raise AuthRefusal(
                status.HTTP_409_CONFLICT,
                code="last_method",
                detail=tr("this is your only way to sign in; add another one before removing it"),
            )
        await audit_service.record(
            session,
            org_id=user.org_id,
            actor_type="user",
            actor_id=user.id,
            action="auth.identity_unlinked",
            target_type="user",
            target_id=str(user.id),
            metadata={"provider": ENTRA, "tenant_id": str(tenant)},
        )
        await notices.tell(
            session,
            user,
            "factor_removed",
            vault=_vault(request),
            actor_id=user.id,
            factor="microsoft",
        )
```

En `backend/app/core/auth/notices.py`, en `_factor_name`, antes del `return` final:

```python
    if factor == "microsoft":
        return tr("a Microsoft account")
```

- [ ] **Step 5: Las rutas**

En `backend/app/api/v1/auth.py`: `from app.core.auth import account, entra_flows, flows`; `from app.core.auth.sessions import ENTRA_COOKIE, SessionPrincipal, clear_entra_cookie, clear_session_cookie, set_device_cookie, set_entra_cookie, set_session_cookie` (lo que falte); en el índice del docstring, debajo de `/auth/recover/accept`:

```
    POST   /auth/microsoft/start            Microsoft's sign-in URL (+ the transaction cookie)
    POST   /auth/microsoft/complete         its code and state -> signed in | linked
    DELETE /me/microsoft
```

el límite, junto a los otros: `_MICROSOFT = _limit("auth-microsoft", 30, 600)`; los modelos:

```python
class MicrosoftStartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: Literal["sign_in", "link", "invite"]
    #: Which page asked: the console (`staff`, Novieri's tenant) or the portal.
    audience: Literal["staff", "contact"] = "staff"
    #: An invitation's token, for `invite`: a secret, no Pydantic bound.
    token: str | None = None


class MicrosoftStartOut(BaseModel):
    """Where to send the browser. The transaction's key travels in the
    `__Host-itms_entra` cookie, never here."""

    authorize_url: str


class MicrosoftCompleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    #: Microsoft's one-time code and the state: secrets, no Pydantic bound.
    code: str
    state: str


class MicrosoftCompleteOut(BaseModel):
    """Where the sign-in with Microsoft stands; `linked` after linking from My security."""

    state: Literal["factor_required", "enroll_required", "signed_in", "linked"]
    methods: list[str] | None = None
    remember_device_days: int | None = None
```

y las rutas, después de `accept_recovery`:

```python
@router.post(
    "/microsoft/start", response_model=MicrosoftStartOut, dependencies=[Depends(_MICROSOFT)]
)
async def microsoft_start(
    body: MicrosoftStartIn, request: Request, response: Response
) -> MicrosoftStartOut:
    """Start a sign-in with Microsoft: to sign in, to link the caller's account
    (a recent confirmation), or to accept an invitation."""
    url, verifier = await entra_flows.start(
        request, intent=body.intent, audience=body.audience, token=body.token
    )
    set_entra_cookie(response, verifier, max_age=entra_flows.ENTRA_TTL_S)
    return MicrosoftStartOut(authorize_url=url)


@router.post(
    "/microsoft/complete",
    response_model=MicrosoftCompleteOut,
    response_model_exclude_unset=True,
    dependencies=[Depends(_MICROSOFT)],
)
async def microsoft_complete(
    body: MicrosoftCompleteIn, request: Request, response: Response
) -> MicrosoftCompleteOut:
    """Finish it with the code and the state Microsoft handed the browser. An
    unknown account is refused: nothing is created, nothing is linked by address."""
    step = await entra_flows.complete(request, code=body.code, state=body.state)
    clear_entra_cookie(response)
    if step is None:
        return MicrosoftCompleteOut(state="linked")
    signed = _answer(response, step)
    return MicrosoftCompleteOut(**signed.model_dump(exclude_unset=True))
```

y en `me_router`, después de `end_my_session`:

```python
@me_router.delete("/microsoft", status_code=status.HTTP_204_NO_CONTENT)
async def unlink_microsoft(request: Request, ctx: AuthContext = Depends(_account_ctx)) -> Response:
    """Unlink the caller's Microsoft account, after a recent confirmation;
    never the last way in (409 `last_method`)."""
    require_step_up(ctx)
    await entra_flows.unlink(request, _me(ctx))
    return _no_content()
```

(`ENTRA_COOKIE` se importa en las pruebas desde `sessions`; si la ruta no lo usa, no lo importes aquí.)

- [ ] **Step 6: `user_admin` cuenta Microsoft como aceptada**

En `backend/app/modules/user_admin/service.py`: importar `AuthExternalIdentity` junto a `AuthPassword` y `from app.core.auth.methods import has_sign_in_method`; en `_open_invites`, debajo de `~exists().where(AuthPassword.user_id == AuthActionToken.user_id),`:

```python
            # Accepted with Microsoft (own authentication phase 2, Task 9, decision 6).
            ~exists().where(AuthExternalIdentity.user_id == AuthActionToken.user_id),
```

y `_accepted` pasa a:

```python
async def _accepted(session: AsyncSession, user_id: UUID) -> bool:
    """Has this person accepted their invitation -- a password, or (phase 2)
    a Microsoft account or a passkey (`methods.has_sign_in_method`)?"""
    return await has_sign_in_method(session, user_id)
```

- [ ] **Step 7: Lista revisada, MCP, rutas y textos**

En `backend/app/core/client_scope.py`, en el bloque (e), después de `"app/core/auth/recover.py::issue_recovery_link"`:

```python
    "app/core/auth/entra_flows.py::_entra_session": (
        "sign-in with Microsoft before the door admits anybody, a link from My security and an "
        "invitation accepted with Microsoft: one person's link (tid, oid) and users row, found "
        "before their scope exists -- the organization from the deployment's slug, the session "
        "or the invitation (P2 phase 2 design §4)"
    ),
```

En `backend/app/mcp/exclusions.py`, en la comprensión de `/api/v1/auth/{path}`:

```python
            (
                "microsoft/start",
                "starts a sign-in with Microsoft only the person's browser can finish",
            ),
            (
                "microsoft/complete",
                "redeems the code Microsoft's redirect gave the person's browser and mints a "
                "session cookie",
            ),
```

y, en `NOT_FOR_MCP`, junto a las de «My security»: `("DELETE", "/api/v1/me/microsoft"): _MY_SECURITY,`.

En `backend/tests/test_mcp_channel_api.py`: `"microsoft/start"`, `"microsoft/complete"` en la tupla; recuento +2 respecto del que veas (→ 46 con todo fusionado).

En `backend/tests/test_route_gating.py`, en `UNGATED_BY_DESIGN`:

```python
        # Sign in with Microsoft (P2 phase 2, D30): before there is a person, or
        # the caller's own session (`link`); the CSRF rule, `_MICROSOFT` per
        # address and the transaction bound to the browser stand in for the gate.
        ("POST", "/api/v1/auth/microsoft/start"),
        ("POST", "/api/v1/auth/microsoft/complete"),
        # The caller's own Microsoft link (My security, D26).
        ("DELETE", "/api/v1/me/microsoft"),
```

En `backend/app/core/i18n/es.py`:

```python
    "sign-in with Microsoft is not set up here": (
        "el inicio de sesión con Microsoft no está configurado aquí"
    ),
    "the sign-in with Microsoft did not finish; start it again": (
        "el inicio de sesión con Microsoft no terminó; empieza de nuevo"
    ),
    "this Microsoft account is not linked to anybody here; sign in another way and link it from "
    "your profile, or use your invitation": (
        "esta cuenta de Microsoft no está vinculada a nadie aquí; inicia sesión de otra forma y "
        "vincúlala desde tu perfil, o usa tu invitación"
    ),
    "this Microsoft account's organization may not sign in here": (
        "la organización de esta cuenta de Microsoft no puede iniciar sesión aquí"
    ),
    "this Microsoft account is already linked to somebody here": (
        "esta cuenta de Microsoft ya está vinculada a otra persona aquí"
    ),
    "you already have a Microsoft account linked; remove it first": (
        "ya tienes una cuenta de Microsoft vinculada; quítala primero"
    ),
    "no Microsoft account is linked": "no hay ninguna cuenta de Microsoft vinculada",
    "a Microsoft account": "una cuenta de Microsoft",
```

- [ ] **Step 8: Correr las pruebas**

Run: `.venv/bin/pytest -q tests/test_auth_entra_flows.py tests/test_auth_entra.py tests/test_user_admin_api.py tests/test_client_catalog.py tests/test_auth_csrf.py tests/test_mcp_channel_api.py tests/test_route_gating.py tests/test_mcp_catalog.py tests/test_i18n_catalog.py tests/test_logging_discipline.py`
Expected: PASS (el recuento de log sigue en 109: esta tarea no añade sitios).

- [ ] **Step 9: Tipos, estilo y vocabulario**

Run: `.venv/bin/mypy app && .venv/bin/ruff check app tests && cd ../frontend && npx vitest run src/modules/audit/auditVocabulary.coverage.test.ts`
Expected: verde (`auth.identity_linked` y `auth.identity_unlinked` ya están en el vocabulario; las llamadas de `entra_flows.py` son literales).

- [ ] **Step 10: Commit**

```bash
git add backend/app/core/auth/ backend/app/api/v1/auth.py backend/app/modules/user_admin/service.py \
  backend/app/core/client_scope.py backend/app/mcp/exclusions.py backend/app/core/i18n/es.py backend/tests/
git commit -m "feat(auth): sign in with a linked Microsoft account; link, accept an invitation, unlink"
```

---

### Task 10: Consola — el portal para contactos y `/portal/sign-in`

Diseño §3 («Un contacto nunca pasa la puerta de la consola; solo la del portal»), §1.1 (pantallas propias, sesión por cookie), §2.4 (la sesión que termina se oculta y se vacía, decisión 3 del operador de la fase 1). D25, D28, D42. Oleada 4; consume los contratos de las Tasks 4 y 6 (no necesita su código: vitest simula `ApiClient`).

**Files:**
- Modify: `frontend/src/lib/api.ts` (`PortalMe`; `portalMe`, `passkeySignInOptions`, `passkeySignIn`, `stepUpPassword`)
- Modify: `frontend/src/lib/session.ts` (`PORTAL_SIGN_IN_PATH`, `PUBLIC_PATHS`, `signInPath`, `signOut`)
- Modify: `frontend/src/lib/sessionCheck.ts` (decisión 4)
- Create: `frontend/src/lib/sessionCheck.portal.test.ts`
- Create: `frontend/src/modules/portal/usePortalMe.ts`, `frontend/src/modules/portal/PortalShell.tsx`
- Modify: `frontend/src/modules/portal/MyTicketsPage.tsx`, `NewTicketPage.tsx`, `PortalTicketDetailPage.tsx` (la persona por `usePortalMe`)
- Create: `frontend/src/modules/auth/PortalSignInPage.tsx`
- Modify: `frontend/src/App.tsx` (`RequireAuth`: `portal_only`; la ruta `/portal/sign-in`)
- Modify: `frontend/src/i18n/en/auth.ts`, `frontend/src/i18n/es/auth.ts`, `frontend/src/i18n/en/portal.ts`, `frontend/src/i18n/es/portal.ts`
- Modify: `frontend/src/modules/auth/fixtures.ts`, `frontend/src/modules/portal/fixtures.ts`, `frontend/scripts/routes.mjs`
- Modify: `frontend/src/modules/auth/__tests__/harness.tsx`, `frontend/src/modules/auth/__tests__/routes.test.ts`, las pruebas del portal (`frontend/src/modules/portal/__tests__/*.test.tsx`, `frontend/src/modules/portal/portal.es.test.tsx`)
- Create: `frontend/src/App.portal.test.tsx`, `frontend/src/lib/session.portal.test.ts`, `frontend/src/modules/auth/__tests__/portalSignIn.test.tsx`
- Test: los nuevos y `npx vitest run` completo

**Interfaces:**
- Consumes: `POST /auth/login` (Task 4, sin cambio de forma); `GET /portal/me` → `{user_id, kind, email, display_name, timezone, permissions}` (Task 4); `POST /auth/passkey/options`, `POST /auth/passkey {credential}` → `{state: "signed_in"}` (Task 6); `POST /auth/step-up/password {password}` → 204 (Task 4); el 403 `{"code": "portal_only"}` de la consola (Task 4).
- Produces: `PortalMe`; `ApiClient.portalMe()`, `.passkeySignInOptions()`, `.passkeySignIn(credential)`, `.stepUpPassword(password)`; `PORTAL_SIGN_IN_PATH`; `usePortalMe()`, `PORTAL_ME_KEY`; `PortalShell`; `PortalSignInPage` en `/portal/sign-in`; `signOut(api, to?)`.

**Decisión del plan (1): la consola reconoce al contacto por la respuesta de la consola.** `RequireAuth` sigue preguntando `GET /me`. Un 403 `portal_only` en una ruta del portal dibuja `PortalShell` (que pregunta `GET /portal/me`) en lugar del armazón de la consola; en cualquier otra ruta, lleva a `/portal`. Un técnico no cambia: su `/me` responde y ve la consola de siempre, también en `/portal`. Nada de esto protege nada (invariante 6): el servidor ya rechazó a la persona en la puerta de la consola.

**Decisión del plan (2): la sesión del contacto termina como la de cualquiera.** `PortalShell` llama a `sessionPerson(user_id)` cuando `/portal/me` responde, así que el final de la sesión se oculta y se vacía con el aviso de siempre; «Entrar de nuevo» lleva a `/portal/sign-in` porque la página era del portal (`signInPath`). Salir lleva también a la página del portal.

**Decisión del plan (3): las páginas del portal leen a la persona de `GET /portal/me`**, no de `GET /me`: un contacto no tiene `/me`. Los permisos y la zona horaria salen de ahí; para un técnico es lo mismo que daba `/me` (sus códigos de portal en el alcance de la petición).

**Decisión del plan (4): un 401 de un contacto se confirma con `/portal/me`.** `lib/sessionCheck.ts` confirma un 401 preguntando `/me` (revisión final de la fase 1, I1) y, si nombra a otra persona, termina la sesión (C3). A un contacto `/me` le responde 403 `portal_only`, que no decide nada: su pestaña nunca sabría que la sesión terminó ni que otra persona entró en el mismo navegador, y sus páginas mostrarían los datos de esa otra persona. Así que, ante ese 403 y solo ante él, la comprobación pregunta `/portal/me` y aplica las mismas dos reglas.

**Decisión del plan (5): el idioma de un contacto no se guarda en su cuenta.** `LanguageBridge` lo guarda con `PATCH /me` solo cuando hay sesión de consola (`hasSession`, que mira `["me"]`); a un contacto esa ruta le está cerrada, así que su elección queda en el navegador (`itms.lang`), como en las páginas de entrada. Su correo sale en el idioma de `users.locale`, que fija quien lo invita.

- [ ] **Step 1: Escribir las pruebas**

Crear `frontend/src/lib/session.portal.test.ts`:

```ts
/** The portal's sign-in page among the public ones (own-auth phase 2, Task 10). */
import { describe, expect, it } from "vitest";

import { PORTAL_SIGN_IN_PATH, isPublicPath, safeNext, signInPath } from "./session";

describe("the portal's sign-in page", () => {
  it("is public, and the portal itself is not", () => {
    expect(PORTAL_SIGN_IN_PATH).toBe("/portal/sign-in");
    expect(isPublicPath("/portal/sign-in")).toBe(true);
    expect(isPublicPath("/portal")).toBe(false);
    expect(isPublicPath("/portal/tickets/t1")).toBe(false);
  });

  it("is where a portal page sends a person to sign in, carrying the page", () => {
    expect(signInPath("/portal")).toBe("/portal/sign-in?next=%2Fportal");
    expect(signInPath("/portal/tickets/t1")).toBe("/portal/sign-in?next=%2Fportal%2Ftickets%2Ft1");
    expect(signInPath("/devices")).toBe("/sign-in?next=%2Fdevices");
    expect(signInPath()).toBe("/sign-in");
  });

  it("is never a page to come back to", () => {
    expect(safeNext("/portal/sign-in")).toBe("/");
  });
});
```

Crear `frontend/src/lib/sessionCheck.portal.test.ts`:

```ts
/**
 * A contact's 401 is confirmed with `/portal/me` (own-auth phase 2, Task 10,
 * decision 4): the console's `/me` answers them 403 `portal_only`, which
 * decides nothing, so the portal's own `/me` is asked with the same two rules.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { confirmSessionEnded } from "./sessionCheck";
import { isSessionEnded, sessionEndReason, sessionPerson } from "./sessionEnd";

const PORTAL_ONLY = JSON.stringify({ detail: "this account can only use the portal", code: "portal_only" });

function answers(byPath: Record<string, Response>): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input).replace("/api/v1", "");
    const found = byPath[path];
    if (found === undefined) throw new Error(`unexpected request to ${path}`);
    return found.clone();
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("a contact's 401", () => {
  it("ends the session when the portal's /me says 401 too", async () => {
    answers({ "/me": new Response(PORTAL_ONLY, { status: 403 }), "/portal/me": new Response("{}", { status: 401 }) });
    await confirmSessionEnded();
    expect(sessionEndReason()).toBe("ended");
  });

  it("ends it as replaced when the portal's /me names somebody else", async () => {
    sessionPerson("contact-1");
    answers({
      "/me": new Response(PORTAL_ONLY, { status: 403 }),
      "/portal/me": new Response(JSON.stringify({ user_id: "contact-2" }), { status: 200 }),
    });
    await confirmSessionEnded();
    expect(sessionEndReason()).toBe("replaced");
  });

  it("does not end it when the portal's /me names the same person", async () => {
    sessionPerson("contact-1");
    answers({
      "/me": new Response(PORTAL_ONLY, { status: 403 }),
      "/portal/me": new Response(JSON.stringify({ user_id: "contact-1" }), { status: 200 }),
    });
    await confirmSessionEnded();
    expect(isSessionEnded()).toBe(false);
  });

  it("does not ask the portal for any other 403", async () => {
    const fetchMock = answers({ "/me": new Response(JSON.stringify({ detail: "no" }), { status: 403 }) });
    await confirmSessionEnded();
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(isSessionEnded()).toBe(false);
  });
});
```

Crear `frontend/src/App.portal.test.tsx`:

```tsx
/**
 * A client's contact in the console (own-auth phase 2, Task 10, decisions
 * 1-2): the console's door answers 403 `portal_only`, and the guard draws the
 * portal's own frame on a portal page, sends them to the portal from any
 * other, and ends their session like anybody's.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { navigation } from "./lib/session";
import { sessionEnded } from "./lib/sessionEnd";

const { ApiProvider } = await import("./lib/ApiContext");
const { RequireAuth } = await import("./App");

const PORTAL_ONLY = { status: 403, body: { detail: "this account can only use the portal", code: "portal_only" } };
const PORTAL_ME = {
  user_id: "contact-1",
  kind: "contact",
  email: "ana@acme.test",
  display_name: "Ana Ruiz",
  timezone: null,
  permissions: ["portal.ticket.read", "portal.ticket.write"],
};

function stubServer(me: { status: number; body: unknown }): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const answer = url.endsWith("/api/v1/me")
        ? me
        : url.endsWith("/api/v1/portal/me")
          ? { status: 200, body: PORTAL_ME }
          : url.endsWith("/api/v1/auth/logout")
            ? { status: 204, body: null }
            : { status: 404, body: {} };
      return new Response(answer.body === null ? null : JSON.stringify(answer.body), { status: answer.status });
    }),
  );
}

function app(path: string): QueryClient {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <ApiProvider>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/portal" element={<RequireAuth><p>my tickets</p></RequireAuth>} />
            <Route path="/devices" element={<RequireAuth><p>the fleet</p></RequireAuth>} />
          </Routes>
        </MemoryRouter>
      </ApiProvider>
    </QueryClientProvider>,
  );
  return queryClient;
}

let assign: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  assign = vi.spyOn(navigation, "assign").mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("a client's contact", () => {
  it("gets the portal's own frame on a portal page, with their name", async () => {
    stubServer(PORTAL_ONLY);
    app("/portal");
    expect(await screen.findByText("my tickets")).toBeInTheDocument();
    expect(await screen.findByText("Ana Ruiz")).toBeInTheDocument();
    expect(screen.getByTestId("portal-shell")).toBeInTheDocument();
  });

  it("is taken to the portal from a console page, which never draws", async () => {
    stubServer(PORTAL_ONLY);
    app("/devices");
    expect(await screen.findByText("my tickets")).toBeInTheDocument();
    expect(screen.queryByText("the fleet")).not.toBeInTheDocument();
  });

  it("whose session ends is sent to sign in again on the portal's page", async () => {
    stubServer(PORTAL_ONLY);
    const user = userEvent.setup();
    app("/portal");
    await screen.findByText("Ana Ruiz");
    act(() => sessionEnded());
    expect(await screen.findByRole("heading", { name: "Your session has ended" })).toBeInTheDocument();
    expect(screen.queryByText("my tickets")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /sign in again/i }));
    expect(assign).toHaveBeenCalledExactlyOnceWith("/portal/sign-in?next=%2Fportal");
  });
});

describe("a technician on a portal page", () => {
  it("keeps the console's frame", async () => {
    stubServer({
      status: 200,
      body: { user_id: "u1", org_id: "o1", email: "t@x.test", display_name: "Tess", roles: [], permissions: ["portal.ticket.read"] },
    });
    app("/portal");
    expect(await screen.findByText("my tickets")).toBeInTheDocument();
    expect(screen.queryByTestId("portal-shell")).not.toBeInTheDocument();
  });
});
```

Crear `frontend/src/modules/auth/__tests__/portalSignIn.test.tsx`:

```tsx
/**
 * `/portal/sign-in` (own-auth phase 2, Task 10; design §3): the same
 * `POST /auth/login` as the console's page, and a passkey alone (D28). Lands
 * on the portal unless `next` says a portal page; every refusal is one
 * sentence.
 */
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../../lib/api";
import { navigation } from "../../../lib/session";
import { authApi, calls, installDefaults, renderAuth, where } from "./harness";

vi.mock("../../../lib/ApiContext", () => ({ useApi: () => authApi() }));

const webauthn = vi.hoisted(() => ({ supported: true, startAuthentication: vi.fn() }));
vi.mock("@simplewebauthn/browser", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@simplewebauthn/browser")>()),
  browserSupportsWebAuthn: () => webauthn.supported,
  startAuthentication: (options: unknown) => webauthn.startAuthentication(options),
}));

const ASSERTION = { id: "Y3JlZA", rawId: "Y3JlZA", type: "public-key", response: {}, clientExtensionResults: {} };
let replace: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  installDefaults();
  webauthn.supported = true;
  webauthn.startAuthentication.mockReset().mockResolvedValue(ASSERTION);
  replace = vi.spyOn(navigation, "replace").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("signing in to the portal", () => {
  it("with a password and no second factor, lands on the portal", async () => {
    calls.login.mockResolvedValue({ state: "signed_in" });
    const user = userEvent.setup();
    renderAuth("/portal/sign-in");
    await user.type(screen.getByLabelText("Email address"), "ana@acme.test");
    await user.type(screen.getByLabelText("Password"), "Ultramarine-Kettle-7319-Harbor");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(calls.login).toHaveBeenCalledWith({ email: "ana@acme.test", password: "Ultramarine-Kettle-7319-Harbor" });
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/portal"));
  });

  it("with a password and a second factor, goes to the factor step carrying the portal", async () => {
    calls.login.mockResolvedValue({ state: "factor_required", methods: ["totp"], remember_device_days: 15 });
    const user = userEvent.setup();
    renderAuth("/portal/sign-in");
    await user.type(screen.getByLabelText("Email address"), "ana@acme.test");
    await user.type(screen.getByLabelText("Password"), "Ultramarine-Kettle-7319-Harbor");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(where.location?.pathname).toBe("/sign-in/factor"));
    expect(where.location?.search).toBe("?next=%2Fportal");
  });

  it("with a passkey alone, sends the browser's assertion and lands on the portal", async () => {
    const user = userEvent.setup();
    renderAuth("/portal/sign-in");
    await user.click(screen.getByRole("button", { name: "Sign in with a passkey" }));
    expect(calls.passkeySignInOptions).toHaveBeenCalledOnce();
    await waitFor(() => expect(calls.passkeySignIn).toHaveBeenCalledWith(ASSERTION));
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/portal"));
  });

  it("says one sentence when the passkey is refused", async () => {
    calls.passkeySignIn.mockRejectedValue(new ApiError(401, '{"code":"passkey_rejected"}'));
    const user = userEvent.setup();
    renderAuth("/portal/sign-in");
    await user.click(screen.getByRole("button", { name: "Sign in with a passkey" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That passkey was not accepted. Try again, or use your email address and password.",
    );
  });

  it("does not offer a passkey on a browser that cannot use one", () => {
    webauthn.supported = false;
    renderAuth("/portal/sign-in");
    expect(screen.queryByRole("button", { name: "Sign in with a passkey" })).not.toBeInTheDocument();
  });
});
```

En `frontend/src/modules/auth/__tests__/harness.tsx`: `import { PortalSignInPage } from "../PortalSignInPage";`; en `calls`, `passkeySignInOptions: vi.fn(), passkeySignIn: vi.fn(),`; en `installDefaults`, `calls.passkeySignInOptions.mockResolvedValue({ challenge: "cG9ydGFs", userVerification: "required", allowCredentials: [] });` y `calls.passkeySignIn.mockResolvedValue({ state: "signed_in" });`; y en `renderAuth`, `<Route path="/portal/sign-in" element={<PortalSignInPage />} />`.

En `frontend/src/modules/auth/__tests__/routes.test.ts`, `"/portal/sign-in",` al final de `PAGES`, y la última línea de «are exactly the public paths…», que tomaba el primer segmento de cada página como su prefijo (con `/portal/sign-in` daría `/portal`, que no es público), pasa a:

```ts
    // Each page is under one public prefix, and each prefix is some page's.
    const under = (page: string, prefix: string) => page === prefix || page.startsWith(`${prefix}/`);
    for (const page of PAGES) expect(PUBLIC_PATHS.some((prefix) => under(page, prefix)), page).toBe(true);
    for (const prefix of PUBLIC_PATHS) expect(PAGES.some((page) => under(page, prefix)), prefix).toBe(true);
```

(re-apuntada: la propiedad es la misma, dicha sin suponer un prefijo de un solo segmento; el informe lo dice).

- [ ] **Step 2: Verlas fallar**

Run (desde `frontend/`): `npx vitest run src/lib/session.portal.test.ts src/lib/sessionCheck.portal.test.ts src/App.portal.test.tsx src/modules/auth/__tests__/portalSignIn.test.tsx src/modules/auth/__tests__/routes.test.ts`
Expected: FAIL (`PORTAL_SIGN_IN_PATH` no existe; `../PortalSignInPage` no se resuelve; la comprobación no pregunta `/portal/me`).

- [ ] **Step 3: El cliente y la sesión**

En `frontend/src/lib/api.ts`, junto a `export interface Me`:

```ts
/**
 * `GET /portal/me` (own-auth phase 2): the person using the portal -- a
 * client's contact, or a technician holding a portal code -- for the
 * portal's own pages. A contact has no `GET /me`: the console's door answers
 * them 403 `portal_only`.
 */
export interface PortalMe {
  user_id: string;
  kind: "staff" | "contact";
  email: string;
  display_name: string;
  timezone: string | null;
  /** Of `portal.ticket.read` and `portal.ticket.write`. */
  permissions: string[];
}
```

y en `ApiClient`, junto a `factorPasskey`:

```ts
  /** `POST /auth/passkey/options`: a challenge for a passkey alone, with no address (a contact's, D28). */
  passkeySignInOptions(): Promise<PublicKeyCredentialRequestOptionsJSON> {
    return this.request<PublicKeyCredentialRequestOptionsJSON>("/auth/passkey/options", { method: "POST" }, { anonymous: true });
  }

  /** `POST /auth/passkey`: the browser's assertion; signed in, or the one 401. */
  passkeySignIn(credential: AuthenticationResponseJSON): Promise<SignedIn> {
    return this.request<SignedIn>(
      "/auth/passkey",
      { method: "POST", body: JSON.stringify({ credential }) },
      { anonymous: true },
    );
  }
```

junto a `stepUpRecovery`:

```ts
  /** A contact confirms with the password (D29); a technician's answer is 409 `method_unavailable`. */
  stepUpPassword(password: string): Promise<void> {
    return this.request<void>("/auth/step-up/password", { method: "POST", body: JSON.stringify({ password }) }, { global: true });
  }
```

y junto a `listPortalCategories`:

```ts
  /** `GET /portal/me`: the person using the portal -- the person, not a client's data, so no `X-Client-Id`. */
  portalMe(): Promise<PortalMe> {
    return this.request<PortalMe>("/portal/me", {}, { global: true });
  }
```

En `frontend/src/lib/session.ts`: debajo de `SIGN_IN_PATH`:

```ts
/** The portal's sign-in page (own-auth phase 2): a client's contact signs in here. */
export const PORTAL_SIGN_IN_PATH = "/portal/sign-in";

function isPortalPage(path: string): boolean {
  return path === "/portal" || path.startsWith("/portal/") || path.startsWith("/portal?");
}
```

`PUBLIC_PATHS` gana `"/portal/sign-in"` (al final); `signInPath` pasa a:

```ts
/**
 * The sign-in page, with `?next=` when there is somewhere better than the
 * root to come back to: the portal's when that somewhere is a portal page
 * (own-auth phase 2, Task 10, decision 2), else the console's.
 */
export function signInPath(next?: string): string {
  const back = safeNext(next);
  const page = isPortalPage(back) ? PORTAL_SIGN_IN_PATH : SIGN_IN_PATH;
  return back === DEFAULT_AFTER_SIGN_IN ? page : `${page}?next=${encodeURIComponent(back)}`;
}
```

y `signOut` gana un destino:

```ts
export async function signOut(api: Pick<ApiClient, "logout">, to: string = SIGN_IN_PATH): Promise<void> {
  try {
    await api.logout();
  } catch (error: unknown) {
    if (!(error instanceof ApiError && error.status === 401)) throw error;
  }
  forgetStoredClient();
  sessionSignedOut();
  navigation.assign(to);
}
```

(su docstring: «`to`: the console's sign-in page, or the portal's for a contact (`PortalShell`).»).

En `frontend/src/lib/sessionCheck.ts` (decisión 4), `check` pasa a:

```ts
async function ask(base: string, path: string): Promise<Response | null> {
  try {
    return await fetch(`${base}${path}`, {
      // Global: the person, whichever client the top bar names.
      headers: requestHeaders(undefined, { method: "GET", path, client: null, global: true }),
      credentials: "same-origin",
    });
  } catch {
    return null;
  }
}

/** The refusal's `code`, or `null` when the body says none. */
async function codeOf(response: Response): Promise<string | null> {
  try {
    const body = (await response.clone().json()) as { code?: unknown };
    return typeof body.code === "string" ? body.code : null;
  } catch {
    return null;
  }
}

async function check(base: string): Promise<void> {
  let response = await ask(base, "/me");
  if (response === null) return;
  // A CLIENT'S CONTACT (own-auth phase 2, Task 10, decision 4): the console's
  // door answers them 403 `portal_only`, which says nothing about their
  // session; the portal's own `/me` does, under the same two rules.
  if (response.status === 403 && (await codeOf(response)) === "portal_only") {
    response = await ask(base, "/portal/me");
    if (response === null) return;
  }
  if (response.status === 401) {
    sessionEnded();
    return;
  }
  if (!response.ok) return;
  try {
    const me = (await response.json()) as { user_id?: unknown };
    if (typeof me.user_id === "string") sessionPerson(me.user_id);
  } catch {
    // A body that does not read is no evidence either way.
  }
}
```

y su cabecera gana, después de la lista de cuatro casos: «A client's contact is answered 403 `portal_only` by `/me`; for them, and only then, `/portal/me` is asked instead (own-auth phase 2, Task 10).»

- [ ] **Step 4: La persona del portal y su marco**

Crear `frontend/src/modules/portal/usePortalMe.ts`:

```ts
// The person using the portal (own-auth phase 2, Task 10, decision 3): a
// client's contact or a technician with a portal code. The portal's pages
// read their permissions and time zone from here, never from `GET /me`,
// which a contact does not have.
import { useQuery } from "@tanstack/react-query";

import type { ApiError, PortalMe } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";

export const PORTAL_ME_KEY = "portal-me";

export function usePortalMe() {
  const api = useApi();
  return useQuery<PortalMe, ApiError>({
    queryKey: [PORTAL_ME_KEY],
    queryFn: () => api.portalMe(),
    staleTime: 60_000,
    retry: false,
  });
}
```

Crear `frontend/src/modules/portal/PortalShell.tsx`:

```tsx
/**
 * THE PORTAL'S OWN FRAME, for a client's contact (own-auth design §3; plan
 * D42, Task 10). The console's frame -- the rail, the client selector, the
 * organisation, search, health -- reads the console's API, which a contact is
 * refused; this one reads `GET /portal/me` alone and offers the portal's two
 * pages and signing out.
 *
 * `RequireAuth` draws it when the console's door answers 403 `portal_only`
 * on a portal page. It remembers the person (`sessionPerson`), so a session
 * that ends here is hidden and emptied like any other (decision 2), and
 * signing out lands on the portal's sign-in page.
 *
 * NOTHING HERE GATES ANYTHING (invariant 6).
 */
import { useLayoutEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { useT } from "../../i18n/context";
import { useApi } from "../../lib/ApiContext";
import { PORTAL_SIGN_IN_PATH, signOut } from "../../lib/session";
import { sessionPerson } from "../../lib/sessionEnd";
import { Btn, KIND_ICON } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { usePortalMe } from "./usePortalMe";

export function PortalShell({ children }: { children: ReactNode }) {
  const t = useT();
  const api = useApi();
  const me = usePortalMe();
  const [signOutFailed, setSignOutFailed] = useState(false);
  const userId = me.data?.user_id;
  useLayoutEffect(() => {
    if (userId !== undefined) sessionPerson(userId);
  }, [userId]);

  return (
    <div data-testid="portal-shell" className="min-h-screen">
      <header className="flex flex-wrap items-center gap-4 border-b border-[var(--line)] px-6 py-3">
        <Link to="/portal" className="font-semibold">
          {t("portal.shell.brand")}
        </Link>
        <nav aria-label={t("portal.nav.aria")} className="flex gap-4">
          <Link to="/portal">{t("portal.nav.myTickets")}</Link>
          <Link to="/portal/new">{t("portal.nav.newTicket")}</Link>
        </nav>
        <span className="ml-auto text-sm text-[var(--muted)]">
          {me.data ? me.data.display_name || me.data.email : "…"}
        </span>
        <Btn
          small
          label={t("shell.accountMenu.signOut")}
          onClick={() => {
            setSignOutFailed(false);
            signOut(api, PORTAL_SIGN_IN_PATH).catch(() => setSignOutFailed(true));
          }}
        />
      </header>
      {signOutFailed ? (
        <p role="alert" className="notice red">
          <Icon name={KIND_ICON.crit} size={16} />
          <span>{t("shell.app.signOutFailed")}</span>
        </p>
      ) : null}
      <main className="px-6 py-6">{children}</main>
    </div>
  );
}
```

En `MyTicketsPage.tsx`, `NewTicketPage.tsx` y `PortalTicketDetailPage.tsx`: la línea `const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });` pasa a `const me = usePortalMe();` (con `import { usePortalMe } from "./usePortalMe";`), y salen los imports que quedan sin uso — `useQuery` de `@tanstack/react-query` y `type Me` en los tres; `useApi` y `const api = useApi();` en `MyTicketsPage.tsx` y `NewTicketPage.tsx`, donde `api` no tenía otro uso; en `PortalTicketDetailPage.tsx` sale el `const api = useApi();` de la página (línea 52) y se queda el import, que usa el componente de los adjuntos (línea 271). Las lecturas `me.data?.permissions` y `me.data?.timezone` no cambian: `PortalMe` tiene los dos, y `formatDateTime` y las props `zone` ya aceptan `string | null`.

En las pruebas del portal (`frontend/src/modules/portal/__tests__/*.test.tsx`, `frontend/src/modules/portal/portal.es.test.tsx`): junto a cada `me: vi.fn().mockResolvedValue(ME)` del `ApiClient` simulado, `portalMe: vi.fn().mockResolvedValue(portalMeOf(ME))`, con, en cada archivo:

```ts
const portalMeOf = (me: { user_id: string; email: string; display_name: string; permissions: string[]; timezone?: string | null }) => ({
  user_id: me.user_id,
  kind: "staff" as const,
  email: me.email,
  display_name: me.display_name,
  timezone: me.timezone ?? null,
  permissions: me.permissions.filter((code) => code.startsWith("portal.")),
});
```

(re-apuntadas por la decisión 3; el informe lo dice).

- [ ] **Step 5: `/portal/sign-in`**

Crear `frontend/src/modules/auth/PortalSignInPage.tsx`:

```tsx
/**
 * `/portal/sign-in` — a client's contact signs in to the portal (own-auth
 * design §3; plan D25, D28, Task 10).
 *
 * THE SAME `POST /auth/login` AS THE CONSOLE'S PAGE (D25): the server finds
 * the person and applies their kind's rules. A contact without a second
 * factor is signed in at once; with one, the factor step follows, carrying
 * the portal as `next`. A technician who lands here signs in too, and the
 * console's guard puts them where they belong.
 *
 * A PASSKEY ALONE (D28): no address is asked for and none is sent; the
 * browser offers the person's own passkeys. Not offered where the browser
 * cannot use one. A refusal -- any refusal -- is one sentence, as on the
 * console's page (§2.3).
 */
import { useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { Form } from "../../components/form";
import { Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { useApi } from "../../lib/ApiContext";
import { enterConsole } from "../../lib/session";
import { Btn } from "../../shell/components";
import { AuthAlert, AuthCard } from "./AuthCard";
import { describeAuthError } from "./errors";
import { carryNext, useNext } from "./flow";
import { canUsePasskeys, describePasskeyError, signWithPasskey } from "./passkey";

const PORTAL = "/portal";

export function PortalSignInPage() {
  const t = useT();
  const api = useApi();
  const navigate = useNavigate();
  const next = useNext() ?? PORTAL;
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);

  async function guarded(step: () => Promise<void>) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      await step();
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  function submit() {
    if (!email.trim() || !password) return;
    void guarded(async () => {
      try {
        const result = await api.login({ email: email.trim(), password });
        if (result.state === "signed_in") {
          enterConsole(next);
          return;
        }
        if (result.state === "enroll_required") {
          navigate(carryNext("/sign-in/enroll", next));
          return;
        }
        navigate(carryNext("/sign-in/factor", next), {
          state: { methods: result.methods, rememberDays: result.remember_device_days ?? null },
        });
      } catch (cause: unknown) {
        setPassword("");
        setError(describeAuthError(cause, { rejected: "auth.signIn.rejected", canTimeOut: false }));
      }
    });
  }

  function usePasskey() {
    void guarded(async () => {
      try {
        const credential = await signWithPasskey(await api.passkeySignInOptions());
        await api.passkeySignIn(credential);
        enterConsole(next);
      } catch (cause: unknown) {
        setError(
          describePasskeyError(cause) ??
            describeAuthError(cause, { rejected: "auth.portalSignIn.passkeyRejected", canTimeOut: false }),
        );
      }
    });
  }

  return (
    <AuthCard title={t("auth.portalSignIn.title")} intro={t("auth.portalSignIn.intro")}>
      {canUsePasskeys() ? (
        <div className="form-actions">
          <Btn
            variant="primary"
            icon="key"
            label={t("auth.portalSignIn.usePasskey")}
            disabled={busy}
            onClick={usePasskey}
          />
        </div>
      ) : null}
      <Form onSubmit={submit}>
        <Field label={t("auth.common.email")}>
          <input
            className={inputClass}
            type="email"
            autoComplete="username webauthn"
            spellCheck={false}
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
        </Field>
        <Field label={t("auth.common.password")}>
          <input
            className={inputClass}
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </Field>
        <AuthAlert text={error} />
        <div className="form-actions">
          <Btn
            type="submit"
            variant="secondary"
            icon="lock"
            label={busy ? t("auth.common.working") : t("auth.signIn.submit")}
            disabled={busy || !email.trim() || !password}
          />
        </div>
      </Form>
      <p className="note" style={{ margin: 0 }}>
        <Link to={carryNext("/forgot-password", next)}>{t("auth.signIn.forgot")}</Link>
      </p>
    </AuthCard>
  );
}
```

En `frontend/src/i18n/en/auth.ts`, después de `signIn`:

```ts
  portalSignIn: {
    title: "Sign in to the portal",
    intro: "Use a passkey, or your email address and password.",
    usePasskey: "Sign in with a passkey",
    passkeyRejected: "That passkey was not accepted. Try again, or use your email address and password.",
  },
```

y en `frontend/src/i18n/es/auth.ts`:

```ts
  portalSignIn: {
    title: "Entrar al portal",
    intro: "Usa una clave de acceso, o tu correo y tu contraseña.",
    usePasskey: "Entrar con una clave de acceso",
    passkeyRejected: "No se aceptó esa clave de acceso. Inténtalo de nuevo, o usa tu correo y tu contraseña.",
  },
```

En `frontend/src/i18n/en/portal.ts` y `es/portal.ts`, una sección `shell` (la navegación reutiliza `portal.nav.*`, que ya existe; el enlace a «Acceso y seguridad» lo añade la Task 11, con su página):

```ts
  shell: {
    brand: "Support portal",
  },
```

```ts
  shell: {
    brand: "Portal de soporte",
  },
```

- [ ] **Step 6: La guardia y la ruta**

En `frontend/src/App.tsx`: `import { PortalSignInPage } from "./modules/auth/PortalSignInPage";`, `import { PortalShell } from "./modules/portal/PortalShell";` y `apiErrorCode` junto a `ApiError` desde `./lib/api`; en las rutas sin sesión, después de `<Route path="/recover" element={<RecoverPage />} />`:

```tsx
            <Route path="/portal/sign-in" element={<PortalSignInPage />} />
```

y en `RequireAuth`, antes del bloque `if (session.failure instanceof ApiError && session.failure.status === 403)`:

```tsx
  // A CLIENT'S CONTACT (own-auth phase 2, Task 10, decision 1): the
  // console's door said `portal_only`. On a portal page, the portal's own
  // frame, which asks `/portal/me`; anywhere else, the portal. Not a control:
  // the server refused them already.
  if (
    session.failure instanceof ApiError &&
    session.failure.status === 403 &&
    apiErrorCode(session.failure) === "portal_only"
  ) {
    if (location.pathname !== "/portal" && !location.pathname.startsWith("/portal/")) {
      return <Navigate to="/portal" replace />;
    }
    return <PortalShell>{children}</PortalShell>;
  }
```

y en el docstring de `RequireAuth`, después del párrafo del 403: «A 403 `portal_only` is a client's contact (own-auth phase 2): the portal's frame on a portal page, the portal from anywhere else.»

- [ ] **Step 7: Fixtures y la marcha de harmony**

En `frontend/scripts/routes.mjs`, `"/portal/sign-in",` después de `"/recover",`. En `frontend/src/modules/auth/fixtures.ts`, junto a las demás: `passkeySignInOptions: () => ({ challenge: "cG9ydGFs", userVerification: "required", allowCredentials: [] }),` y `passkeySignIn: () => ({ state: "signed_in" as const }),`. En `frontend/src/modules/portal/fixtures.ts`: `portalMe: () => ({ user_id: "u-1", kind: "staff" as const, email: "ada@example.test", display_name: "Ada Lovelace", timezone: null, permissions: ["portal.ticket.read", "portal.ticket.write"] }),`.

- [ ] **Step 8: Correr las pruebas**

Run (desde `frontend/`): `npx vitest run`
Expected: PASS, incluidas `routes.test.ts` (las tres listas: `App.tsx`, `PUBLIC_PATHS`, `routes.mjs`) y `api.clientScope.test.ts` / `api.session.test.ts` (los métodos nuevos dicen adónde van).

- [ ] **Step 9: Tipos y construcción**

Run: `npx tsc -b && npm run build`
Expected: sin errores.

- [ ] **Step 10: Commit**

```bash
git add frontend/src frontend/scripts/routes.mjs
git commit -m "feat(console): the portal's own frame for contacts, and /portal/sign-in"
```

---

### Task 11: Consola — el acceso de los contactos, su invitación y su seguridad

Diseño §3 (invitación desde la pestaña Contactos, contraseña de 15+ o clave de acceso, segundo factor opcional), §7 (ciclo de vida), §2.5 (la confirmación reciente). D26, D27, D29, D39, D40, D43. Oleada 5; consume los contratos de las Tasks 7, 8 y 10 (vitest simula `ApiClient`).

**Files:**
- Modify: `frontend/src/lib/api.ts` (`ContactOut`, `ContactInviteOut`; `inviteContact`, `cancelContactInvite`, `resetContactAccess`, `acceptInvite`, `invitePasskeyOptions`, `invitePasskey`, `addPassword`)
- Modify: `frontend/src/modules/auth/types.ts` (`InviteDetails`, `InviteAccepted`)
- Modify: `frontend/src/modules/profile/types.ts` (`MySecurity.kind`, `.has_password`, `.microsoft`; `MyMicrosoftLink`)
- Modify: `frontend/src/modules/clients/api.ts`, `frontend/src/modules/clients/ContactsTab.tsx`
- Modify: `frontend/src/modules/auth/errors.ts` (`checkNewPassword(..., minimum)`), `frontend/src/modules/auth/NewPasswordFields.tsx` (`minimum`), `frontend/src/modules/auth/InvitePage.tsx`
- Modify: `frontend/src/modules/profile/api.ts` (`useAddPasswordMutation`), `frontend/src/modules/profile/SecurityPage.tsx` (`PortalSecurityPage`, las reglas del contacto)
- Modify: `frontend/src/components/stepUp.tsx` (la contraseña de un contacto)
- Modify: `frontend/src/modules/portal/PortalShell.tsx` (el enlace a «Acceso y seguridad»)
- Modify: `frontend/src/App.tsx` (la ruta `/portal/security`), `frontend/scripts/routes.mjs`
- Modify: `frontend/src/i18n/en/{auth,clients,profile,components,portal}.ts` y los mismos de `es/`
- Modify: `frontend/src/modules/clients/fixtures.ts`, `frontend/src/modules/auth/fixtures.ts`, `frontend/src/modules/profile/fixtures.ts`
- Modify: `frontend/src/modules/clients/__tests__/clients.test.tsx`, `frontend/src/modules/auth/__tests__/harness.tsx`, `frontend/src/modules/settings/__tests__/harness.tsx`
- Create: `frontend/src/modules/auth/__tests__/inviteContact.test.tsx`, `frontend/src/modules/profile/SecurityPage.contact.test.tsx`

**Interfaces:**
- Consumes: `ContactOut.access`/`invite_expires_at`, `POST/DELETE /clients/{id}/contacts/{cid}/invite`, `POST .../access/reset` (403 `step_up_required` sin confirmación reciente), `InviteOut.kind/password_minimum/passkey/microsoft`, `POST /auth/invite/accept` → `{state: "signed_in" | "enroll_required"}`, `POST /auth/invite/passkey/options {token}`, `POST /auth/invite/passkey {token, credential, name}` (Task 8); `MySecurityOut.kind/has_password/microsoft`, `POST /me/password {new}`, 409 `last_method`/`password_exists` (Task 7); `POST /auth/step-up/password` (Task 4, vía `ApiClient.stepUpPassword` de la Task 10); `usePortalMe`, `PortalShell` (Task 10).
- Produces: `ContactInviteOut`; `ApiClient.inviteContact(clientId, contactId)`, `.cancelContactInvite(clientId, contactId)`, `.resetContactAccess(clientId, contactId)`, `.invitePasskeyOptions(token)`, `.invitePasskey({token, credential, name})`, `.addPassword(password)`; `InviteAccepted`; `MyMicrosoftLink`; `checkNewPassword(password, again, minimum = 12)`; `NewPasswordFields.minimum`; `PortalSecurityPage` en `/portal/security`.

**Decisión del plan (1): la pestaña Contactos dice el acceso de cada contacto y ofrece el acto que corresponde.** «Sin acceso» → Invitar; «Invitado, hasta …» → Enviar de nuevo y Cancelar la invitación; «Puede entrar» → Restablecer el acceso (con confirmación y la confirmación reciente del técnico, que pide el servidor). Un contacto desactivado no ofrece ninguno: activarlo es el primer paso. Sin correo configurado, el enlace se muestra **una vez**, como en Ajustes → Usuarios, y la mutación que lo trajo se descarta (`gcTime: 0`).

**Decisión del plan (2): la invitación de un contacto lleva al portal.** La página `/invite` lee `kind` y `password_minimum` de la invitación: un contacto elige una contraseña de 15+ o una clave de acceso, y entra directamente (`signed_in` → `/portal`); un técnico sigue como en la fase 1 (`enroll_required` → `/sign-in/enroll`).

**Decisión del plan (3): una sola página de seguridad, dos marcos.** `SecurityPage` (consola, `/profile/security`) y `PortalSecurityPage` (portal, `/portal/security`) dibujan el mismo cuerpo; cambia de dónde sale la zona horaria (la organización y `/me` en la consola; `/portal/me` en el portal, porque un contacto no puede leer ninguna de las dos). Lo que cambia por tipo lo dice `GET /me/security` (`kind`, `has_password`, `microsoft`): la regla del mínimo (15 o 12), quién no tiene contraseña todavía (añadirla, `POST /me/password`), la app TOTP opcional del contacto, la última forma de entrar (`last_method`) y los códigos de recuperación (Pregunta P12: a un contacto se le muestran solo cuando tiene un segundo factor). El servidor decide todo eso de nuevo (invariante 6).

**Decisión del plan (4): la confirmación reciente de un contacto acepta su contraseña** (D29). El diálogo la ofrece cuando `kind` es `contact` y tiene contraseña, además de su clave de acceso o su app; a un contacto que solo tiene Microsoft le dice que vuelva a entrar (entrar deja la confirmación reciente durante 10 minutos, Task 4) — Pregunta P13.

- [ ] **Step 1: Escribir las pruebas**

En `frontend/src/modules/clients/__tests__/clients.test.tsx`: `ANA` gana `access: "active", invite_expires_at: null`; en `api()`, `getMySecurity: vi.fn().mockResolvedValue({ kind: "staff", has_password: true, microsoft: null, has_totp: true, passkeys: [], recovery_codes_left: 8, trusted_devices: [], sessions: [] }), stepUpTotp: vi.fn().mockResolvedValue(undefined), getCurrentOrg: vi.fn().mockResolvedValue({ id: "org-1", name: "Toeshee", settings: {} }),` (la zona de la fecha de caducidad, `useOrgZone`); y al final del archivo:

```tsx
const LUIS: ContactOut = {
  ...ANA,
  id: "ct-luis",
  email: "luis@andes.example",
  display_name: "Luis Pardo",
  access: "none",
};
const MARTA: ContactOut = {
  ...ANA,
  id: "ct-marta",
  email: "marta@andes.example",
  display_name: "Marta Gil",
  access: "invited",
  invite_expires_at: "2026-11-09T12:00:00Z",
};
const STEP_UP = new ApiError(403, JSON.stringify({ detail: { code: "step_up_required" } }));

describe("a client's contacts' portal access", () => {
  it("says each contact's access and offers the act that fits it", async () => {
    mockUseApi.mockReturnValue(api({ listContacts: vi.fn().mockResolvedValue([ANA, LUIS, MARTA]) }));
    renderAt("/clients/c-andes?tab=contacts");
    const ana = (await screen.findByText("Ana Rojas")).closest("tr") as HTMLElement;
    const luis = screen.getByText("Luis Pardo").closest("tr") as HTMLElement;
    const marta = screen.getByText("Marta Gil").closest("tr") as HTMLElement;
    expect(within(ana).getByText("Can sign in")).toBeInTheDocument();
    expect(within(ana).getByRole("button", { name: "Reset portal access for Ana Rojas" })).toBeInTheDocument();
    expect(within(luis).getByText("No access")).toBeInTheDocument();
    expect(within(luis).getByRole("button", { name: "Invite Luis Pardo to the portal" })).toBeInTheDocument();
    expect(within(marta).getByText(/^Invited/)).toBeInTheDocument();
    expect(within(marta).getByRole("button", { name: "Send Marta Gil a new invitation" })).toBeInTheDocument();
    expect(within(marta).getByRole("button", { name: "Cancel Marta Gil's invitation" })).toBeInTheDocument();
  });

  it("invites by email and says so", async () => {
    const inviteContact = vi.fn().mockResolvedValue({ contact: { ...LUIS, access: "invited" }, invite_link: null });
    mockUseApi.mockReturnValue(api({ listContacts: vi.fn().mockResolvedValue([LUIS]), inviteContact }));
    const user = userEvent.setup();
    renderAt("/clients/c-andes?tab=contacts");
    await user.click(await screen.findByRole("button", { name: "Invite Luis Pardo to the portal" }));
    expect(inviteContact).toHaveBeenCalledExactlyOnceWith("c-andes", "ct-luis");
    expect(await screen.findByText("Invitation sent to luis@andes.example.")).toBeInTheDocument();
  });

  it("without email, shows the link once and forgets it when done", async () => {
    const link = "https://itms.example/invite#tok-contact-1";
    const inviteContact = vi.fn().mockResolvedValue({ contact: { ...LUIS, access: "invited" }, invite_link: link });
    mockUseApi.mockReturnValue(api({ listContacts: vi.fn().mockResolvedValue([LUIS]), inviteContact }));
    const user = userEvent.setup();
    renderAt("/clients/c-andes?tab=contacts");
    await user.click(await screen.findByRole("button", { name: "Invite Luis Pardo to the portal" }));
    const shown = await screen.findByRole("region", { name: "Portal invitation for luis@andes.example" });
    expect(within(shown).getByLabelText("Invitation link")).toHaveValue(link);
    await user.click(within(shown).getByRole("button", { name: "Done, I have passed it on" }));
    expect(screen.queryByDisplayValue(link)).not.toBeInTheDocument();
  });

  it("cancels a pending invitation", async () => {
    const cancelContactInvite = vi.fn().mockResolvedValue(undefined);
    mockUseApi.mockReturnValue(api({ listContacts: vi.fn().mockResolvedValue([MARTA]), cancelContactInvite }));
    const user = userEvent.setup();
    renderAt("/clients/c-andes?tab=contacts");
    await user.click(await screen.findByRole("button", { name: "Cancel Marta Gil's invitation" }));
    expect(cancelContactInvite).toHaveBeenCalledExactlyOnceWith("c-andes", "ct-marta");
  });

  it("resets access after a confirmation, asking for the technician's second factor when the server does", async () => {
    const resetContactAccess = vi
      .fn()
      .mockRejectedValueOnce(STEP_UP)
      .mockResolvedValue({ contact: { ...ANA, access: "invited" }, invite_link: null });
    const stepUpTotp = vi.fn().mockResolvedValue(undefined);
    mockUseApi.mockReturnValue(api({ resetContactAccess, stepUpTotp }));
    const user = userEvent.setup();
    renderAt("/clients/c-andes?tab=contacts");
    await user.click(await screen.findByRole("button", { name: "Reset portal access for Ana Rojas" }));
    const confirm = await screen.findByRole("dialog", { name: "Reset this contact's portal access" });
    await user.click(within(confirm).getByRole("button", { name: "Reset access" }));
    const stepUp = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.type(within(stepUp).getByLabelText("Code from your authenticator app"), "123456");
    await user.click(within(stepUp).getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(resetContactAccess).toHaveBeenCalledTimes(2));
    expect(stepUpTotp).toHaveBeenCalledWith("123456");
    expect(resetContactAccess).toHaveBeenLastCalledWith("c-andes", "ct-ana");
  });

  it("offers nothing to a contact that is turned off", async () => {
    mockUseApi.mockReturnValue(api({ listContacts: vi.fn().mockResolvedValue([{ ...LUIS, status: "disabled" }]) }));
    renderAt("/clients/c-andes?tab=contacts");
    const luis = (await screen.findByText("Luis Pardo")).closest("tr") as HTMLElement;
    expect(within(luis).queryByRole("button", { name: /Invite/ })).not.toBeInTheDocument();
  });
});
```

Crear `frontend/src/modules/auth/__tests__/inviteContact.test.tsx`:

```tsx
/**
 * A contact's invitation (own-auth phase 2, Task 11, decision 2; design §3):
 * a password of 15 or more, or a passkey, and straight to the portal. A
 * technician's invitation is unchanged.
 */
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { navigation } from "../../../lib/session";
import { checkNewPassword } from "../errors";
import { authApi, calls, installDefaults, renderAuth, where } from "./harness";

vi.mock("../../../lib/ApiContext", () => ({ useApi: () => authApi() }));

const webauthn = vi.hoisted(() => ({ supported: true, startRegistration: vi.fn() }));
vi.mock("@simplewebauthn/browser", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@simplewebauthn/browser")>()),
  browserSupportsWebAuthn: () => webauthn.supported,
  startRegistration: (options: unknown) => webauthn.startRegistration(options),
}));

const CONTACT_INVITE = {
  email: "ana@acme.test",
  display_name: "Ana Ruiz",
  kind: "contact",
  password_minimum: 15,
  passkey: true,
  microsoft: false,
};
const ATTESTATION = { id: "bmV3", rawId: "bmV3", type: "public-key", response: {}, clientExtensionResults: {} };
const GOOD = "Ultramarine-Kettle-7319-Harbor";
let replace: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  installDefaults();
  calls.getInvite.mockResolvedValue(CONTACT_INVITE);
  calls.acceptInvite.mockResolvedValue({ state: "signed_in" });
  webauthn.supported = true;
  webauthn.startRegistration.mockReset().mockResolvedValue(ATTESTATION);
  replace = vi.spyOn(navigation, "replace").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("the new-password check", () => {
  it("takes the minimum of the person's kind", () => {
    expect(checkNewPassword("fourteen chars", "fourteen chars", 15)).toBe("Use at least 15 characters.");
    expect(checkNewPassword("fourteen chars", "fourteen chars")).toBeNull();
  });
});

describe("a contact's invitation", () => {
  it("asks for 15 characters, then lands on the portal", async () => {
    const user = userEvent.setup();
    renderAuth("/invite#tok-contact-1");
    expect(await screen.findByText("At least 15 characters, any you like, spaces included. A phrase you can remember works well.")).toBeInTheDocument();
    await user.type(screen.getByLabelText("New password"), "fourteen chars");
    await user.type(screen.getByLabelText("The same password again"), "fourteen chars");
    await user.click(screen.getByRole("button", { name: "Set password and continue" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Use at least 15 characters.");
    expect(calls.acceptInvite).not.toHaveBeenCalled();
    await user.clear(screen.getByLabelText("New password"));
    await user.clear(screen.getByLabelText("The same password again"));
    await user.type(screen.getByLabelText("New password"), GOOD);
    await user.type(screen.getByLabelText("The same password again"), GOOD);
    await user.click(screen.getByRole("button", { name: "Set password and continue" }));
    expect(calls.acceptInvite).toHaveBeenCalledWith({ token: "tok-contact-1", password: GOOD });
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/portal"));
  });

  it("accepts with a passkey instead, named by the person", async () => {
    calls.invitePasskeyOptions.mockResolvedValue({ challenge: "cmVnaXN0ZXI", rp: { name: "ITMS" } });
    calls.invitePasskey.mockResolvedValue({ state: "signed_in" });
    const user = userEvent.setup();
    renderAuth("/invite#tok-contact-1");
    await user.click(await screen.findByRole("button", { name: "Use a passkey instead" }));
    expect(calls.invitePasskeyOptions).toHaveBeenCalledWith("tok-contact-1");
    await waitFor(() =>
      expect(calls.invitePasskey).toHaveBeenCalledWith({
        token: "tok-contact-1",
        credential: ATTESTATION,
        name: "My passkey",
      }),
    );
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/portal"));
  });

  it("does not offer a passkey on a browser that cannot make one", async () => {
    webauthn.supported = false;
    renderAuth("/invite#tok-contact-1");
    await screen.findByLabelText("New password");
    expect(screen.queryByRole("button", { name: "Use a passkey instead" })).not.toBeInTheDocument();
  });
});

describe("a technician's invitation", () => {
  it("still goes on to the second factor", async () => {
    calls.getInvite.mockResolvedValue({ ...CONTACT_INVITE, kind: "staff", password_minimum: 12, passkey: false });
    calls.acceptInvite.mockResolvedValue({ state: "enroll_required" });
    const user = userEvent.setup();
    renderAuth("/invite#tok-staff-1");
    await user.type(await screen.findByLabelText("New password"), GOOD);
    await user.type(screen.getByLabelText("The same password again"), GOOD);
    await user.click(screen.getByRole("button", { name: "Set password and continue" }));
    await waitFor(() => expect(where.location?.pathname).toBe("/sign-in/enroll"));
    expect(screen.queryByRole("button", { name: "Use a passkey instead" })).not.toBeInTheDocument();
  });
});
```

(«My passkey» es `auth.enroll.passkeyDefaultName`, el nombre que la página de alta de la fase 1 propone.)

En `frontend/src/modules/auth/__tests__/harness.tsx`: en `calls`, `invitePasskeyOptions: vi.fn(), invitePasskey: vi.fn(),`; en `installDefaults`, `calls.getInvite.mockResolvedValue({ email: "dana@example.test", display_name: "Dana Okafor", kind: "staff", password_minimum: 12, passkey: false, microsoft: false });` y `calls.acceptInvite.mockResolvedValue({ state: "enroll_required" });` (re-apuntadas: la forma nueva de las dos respuestas, Task 8).

Crear `frontend/src/modules/profile/SecurityPage.contact.test.tsx`:

```tsx
/**
 * A client's contact's own security, in the portal (own-auth phase 2, Task
 * 11, decisions 3-4): the minimum of 15, the first password added, the app
 * optional, never the last way in, and the confirmation with the password.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../lib/api";
import { MY_SECURITY_FIXTURE, apiClient, calls, installDefaults } from "../settings/__tests__/harness";
import { PortalSecurityPage } from "./SecurityPage";

vi.mock("../../lib/ApiContext", () => ({ useApi: () => apiClient() }));

const CONTACT = {
  ...MY_SECURITY_FIXTURE,
  kind: "contact",
  has_password: true,
  microsoft: null,
  has_totp: false,
  passkeys: [],
  recovery_codes_left: 0,
};
const STEP_UP = new ApiError(403, JSON.stringify({ detail: { code: "step_up_required" } }));
const GOOD = "Ultramarine-Kettle-7319-Harbor";

function renderPortalSecurity() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/portal/security"]}>
        <PortalSecurityPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function panel(title: string): Promise<HTMLElement> {
  const heading = await screen.findByRole("heading", { name: title });
  const section = heading.closest("section");
  if (!section) throw new Error(`no panel titled ${title}`);
  return section;
}

beforeEach(() => {
  installDefaults();
  calls.getMySecurity.mockResolvedValue(CONTACT);
});

describe("a contact's security", () => {
  it("reads the time zone from the portal, never from the console", async () => {
    renderPortalSecurity();
    await panel("Password");
    expect(calls.portalMe).toHaveBeenCalled();
    expect(calls.getCurrentOrg).not.toHaveBeenCalled();
    expect(calls.me).not.toHaveBeenCalled();
  });

  it("holds a new password to 15 characters", async () => {
    const user = userEvent.setup();
    renderPortalSecurity();
    const box = await panel("Password");
    await user.type(within(box).getByLabelText("Current password"), GOOD);
    await user.type(within(box).getByLabelText("New password"), "fourteen chars");
    await user.type(within(box).getByLabelText("New password again"), "fourteen chars");
    await user.click(within(box).getByRole("button", { name: "Change password" }));
    expect(await within(box).findByRole("alert")).toHaveTextContent("Use at least 15 characters.");
    expect(calls.changePassword).not.toHaveBeenCalled();
  });

  it("adds a first password for a contact who signed in with a passkey", async () => {
    calls.getMySecurity.mockResolvedValue({
      ...CONTACT,
      has_password: false,
      passkeys: [MY_SECURITY_FIXTURE.passkeys[0]],
    });
    const user = userEvent.setup();
    renderPortalSecurity();
    const box = await panel("Password");
    expect(within(box).queryByLabelText("Current password")).not.toBeInTheDocument();
    await user.type(within(box).getByLabelText("New password"), GOOD);
    await user.type(within(box).getByLabelText("New password again"), GOOD);
    await user.click(within(box).getByRole("button", { name: "Add password" }));
    await waitFor(() => expect(calls.addPassword).toHaveBeenCalledExactlyOnceWith(GOOD));
  });

  it("never offers to remove the last way in", async () => {
    calls.getMySecurity.mockResolvedValue({
      ...CONTACT,
      has_password: false,
      passkeys: [MY_SECURITY_FIXTURE.passkeys[0]],
    });
    renderPortalSecurity();
    const box = await panel("Passkeys");
    expect(within(box).getByRole("button", { name: /Remove/ })).toBeDisabled();
    expect(within(box).getByText("This passkey is your only way to sign in. Add a password before you remove it.")).toBeInTheDocument();
  });

  it("lets the authenticator app go, and shows recovery codes only with a second factor", async () => {
    calls.getMySecurity.mockResolvedValue({ ...CONTACT, has_totp: true });
    renderPortalSecurity();
    const app = await panel("Authenticator app");
    expect(within(app).getByRole("button", { name: /Remove/ })).toBeEnabled();
    expect(screen.getByRole("heading", { name: "Recovery codes" })).toBeInTheDocument();
  });

  it("hides recovery codes from a contact without a second factor", async () => {
    renderPortalSecurity();
    await panel("Password");
    expect(screen.queryByRole("heading", { name: "Recovery codes" })).not.toBeInTheDocument();
  });

  it("confirms with the password when the server asks", async () => {
    calls.changePassword.mockRejectedValueOnce(STEP_UP).mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPortalSecurity();
    const box = await panel("Password");
    await user.type(within(box).getByLabelText("Current password"), GOOD);
    await user.type(within(box).getByLabelText("New password"), `${GOOD}-2`);
    await user.type(within(box).getByLabelText("New password again"), `${GOOD}-2`);
    await user.click(within(box).getByRole("button", { name: "Change password" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.type(within(dialog).getByLabelText("Your password"), GOOD);
    await user.click(within(dialog).getByRole("button", { name: "Confirm with your password" }));
    await waitFor(() => expect(calls.changePassword).toHaveBeenCalledTimes(2));
    expect(calls.stepUpPassword).toHaveBeenCalledExactlyOnceWith(GOOD);
  });
});
```

(«Authenticator app» y «Recovery codes» son `profile.securityPage.totp.title` y `.recovery.title`; el botón de quitar de cada panel es su `remove`, «Remove».)

En `frontend/src/modules/settings/__tests__/harness.tsx`: `MY_SECURITY_FIXTURE` gana `kind: "staff", has_password: true, microsoft: null,` al principio; en `calls`, `portalMe: vi.fn(), addPassword: vi.fn(), stepUpPassword: vi.fn(),`; en `installDefaults`, `calls.portalMe.mockResolvedValue({ user_id: "contact-1", kind: "contact", email: "ana@acme.test", display_name: "Ana Ruiz", timezone: "America/Bogota", permissions: ["portal.ticket.read", "portal.ticket.write"] });`, `calls.addPassword.mockResolvedValue(undefined);` y `calls.stepUpPassword.mockResolvedValue(undefined);`.

- [ ] **Step 2: Verlas fallar**

Run (desde `frontend/`): `npx vitest run src/modules/clients/__tests__/clients.test.tsx src/modules/auth/__tests__/inviteContact.test.tsx src/modules/profile/SecurityPage.contact.test.tsx`
Expected: FAIL (no hay columna de acceso; `checkNewPassword` no toma mínimo; `PortalSecurityPage` no existe).

- [ ] **Step 3: Los tipos y el cliente**

En `frontend/src/lib/api.ts`, `ContactOut` gana:

```ts
  /** Portal access (own-auth phase 2): none yet, an open invitation, or able to sign in. */
  access: "none" | "invited" | "active";
  /** When the open invitation expires; `null` without one. */
  invite_expires_at: string | null;
```

y después de `ContactUpdate`:

```ts
/**
 * `POST .../contacts/{id}/invite` and `.../access/reset`: the contact as it
 * stands now, and the one-time link ONLY when no mail carried it -- shown
 * once, kept nowhere (invariant 3's spirit: the server keeps its hash only).
 */
export interface ContactInviteOut {
  contact: ContactOut;
  invite_link: string | null;
}
```

En `ApiClient`, después de `updateContact`:

```ts
  /** Send a contact a one-time portal link, valid 72 hours, replacing any open one (`clients.manage`). */
  inviteContact(clientId: string, contactId: string): Promise<ContactInviteOut> {
    return this.request<ContactInviteOut>(
      `/clients/${encodeURIComponent(clientId)}/contacts/${encodeURIComponent(contactId)}/invite`,
      { method: "POST" },
    );
  }

  /** Cancel a contact's open invitation; the contact stays. */
  cancelContactInvite(clientId: string, contactId: string): Promise<void> {
    return this.request<void>(
      `/clients/${encodeURIComponent(clientId)}/contacts/${encodeURIComponent(contactId)}/invite`,
      { method: "DELETE" },
    );
  }

  /**
   * Everything the contact signs in with goes, and a new invitation is sent.
   * Needs the technician's recent confirmation: 403 `step_up_required`, sent
   * through `useStepUp().run`.
   */
  resetContactAccess(clientId: string, contactId: string): Promise<ContactInviteOut> {
    return this.request<ContactInviteOut>(
      `/clients/${encodeURIComponent(clientId)}/contacts/${encodeURIComponent(contactId)}/access/reset`,
      { method: "POST" },
    );
  }
```

`acceptInvite` pasa a:

```ts
  /**
   * `POST /auth/invite/accept`: the password. A technician's answer is
   * `enroll_required` and a session that still needs a factor; a contact's is
   * `signed_in` (own-auth phase 2).
   */
  acceptInvite(body: { token: string; password: string }): Promise<InviteAccepted> {
    return this.request<InviteAccepted>(
      "/auth/invite/accept",
      { method: "POST", body: JSON.stringify(body) },
      { anonymous: true },
    );
  }

  /** A contact's first passkey from the invitation: the creation options. The link stays unused. */
  invitePasskeyOptions(token: string): Promise<PublicKeyCredentialCreationOptionsJSON> {
    return this.request<PublicKeyCredentialCreationOptionsJSON>(
      "/auth/invite/passkey/options",
      { method: "POST", body: JSON.stringify({ token }) },
      { anonymous: true },
    );
  }

  /** Accept a contact's invitation with that passkey: signed in. A refused one leaves the link usable. */
  invitePasskey(body: { token: string; credential: RegistrationResponseJSON; name: string }): Promise<SignedIn> {
    return this.request<SignedIn>(
      "/auth/invite/passkey",
      { method: "POST", body: JSON.stringify(body) },
      { anonymous: true },
    );
  }
```

(`InviteAccepted` se añade al `import type { … } from "../modules/auth/types"` de `lib/api.ts`, que ya trae `SignedIn`, `RegistrationResponseJSON` y `PublicKeyCredentialCreationOptionsJSON`); y después de `changePassword`:

```ts
  /**
   * `POST /me/password`: the FIRST password, for someone who has none (a
   * contact who signed in with a passkey or Microsoft; a technician who
   * accepted with Microsoft). 409 `password_exists` otherwise.
   */
  addPassword(password: string): Promise<void> {
    return this.request<void>("/me/password", { method: "POST", body: JSON.stringify({ new: password }) }, { global: true });
  }
```

En `frontend/src/modules/auth/types.ts`, `InviteDetails` pasa a:

```ts
/** `POST /auth/invite/details {token}`: who the invitation is for, and how it may be accepted. */
export interface InviteDetails {
  email: string;
  display_name: string;
  /** A technician or a client's contact (own-auth phase 2). */
  kind: "staff" | "contact";
  /** The shortest password this person may choose: 12 or 15. */
  password_minimum: number;
  /** The invitation may be accepted with a passkey (contacts). */
  passkey: boolean;
  /** The invitation may be accepted with Microsoft (Task 12). */
  microsoft: boolean;
}

/** `POST /auth/invite/accept`: a technician still needs a factor; a contact is in. */
export type InviteAccepted = { state: "enroll_required" } | SignedIn;
```

En `frontend/src/modules/profile/types.ts`, antes de `MySecurity`:

```ts
/** The Microsoft account linked to this person (own-auth phase 2): which organisation, and when. */
export interface MyMicrosoftLink {
  /** The Microsoft organisation (tenant) the account belongs to. */
  tenant_id: string;
  created_at: string;
  last_used_at: string | null;
}
```

y `MySecurity` gana, al principio:

```ts
  /** A technician or a client's contact: whose rules apply (own-auth phase 2). */
  kind: "staff" | "contact";
  /** False for someone who signed up with a passkey or Microsoft and never set one. */
  has_password: boolean;
  microsoft: MyMicrosoftLink | null;
```

En `frontend/src/modules/clients/api.ts`: importar `ContactInviteOut` junto a `ContactOut`, `type StepUpRun` desde `../../components/stepUp`; y al final:

```ts
/**
 * Invite (or invite again) a contact. THE ANSWER CAN CARRY A ONE-TIME LINK,
 * so the mutation is dropped as soon as the page resets it (`gcTime: 0`,
 * `profile/api.ts`'s rule for secrets).
 */
export function useInviteContactMutation(clientId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<ContactInviteOut, ApiError, string>({
    mutationFn: (contactId) => api.inviteContact(clientId, contactId),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: [CONTACTS_KEY, clientId] }),
    gcTime: 0,
  });
}

export function useCancelContactInviteMutation(clientId: string) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (contactId) => api.cancelContactInvite(clientId, contactId),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: [CONTACTS_KEY, clientId] }),
  });
}

/** Through the tab's `useStepUp().run`: the server asks for the technician's recent confirmation. */
export function useResetContactAccessMutation(clientId: string, run: StepUpRun) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<ContactInviteOut, Error, string>({
    mutationFn: (contactId) => run(() => api.resetContactAccess(clientId, contactId)),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: [CONTACTS_KEY, clientId] }),
    gcTime: 0,
  });
}
```

- [ ] **Step 4: La pestaña Contactos**

En `frontend/src/modules/clients/ContactsTab.tsx`: el docstring gana, después de su primera frase, «Portal access too (own-auth phase 2): invite, invite again, cancel an open invitation, and reset the access of a contact who signed in -- the last one after a confirmation and the technician's recent second factor, which the server asks for.»; los imports ganan `useStepUp` (`../../components/stepUp`), `formatDateTime` (`../../lib/datetime`), `useOrgZone` (`../../lib/zone`), `KIND_ICON` (de `../../shell/components`, junto a `Btn`), `FOLDING_FORM_GRID` (`../settings/formGrid`) y los tres hooks nuevos. Dentro de `ContactsTab`, junto a los demás estados:

```tsx
  const stepUp = useStepUp();
  const zone = useOrgZone();
  const invite = useInviteContactMutation(client.id);
  const cancelInvite = useCancelContactInviteMutation(client.id);
  const reset = useResetContactAccessMutation(client.id, stepUp.run);
  const [resetting, setResetting] = useState<ContactOut | null>(null);
  // THE ONE COPY OF A LINK NO MAIL CARRIED: here until the technician says
  // they passed it on, and in no mutation (each is reset once it settles).
  const [shownLink, setShownLink] = useState<{ email: string; link: string } | null>(null);
  const [sentTo, setSentTo] = useState<string | null>(null);
  // What came of the last access act, held here: the two mutations whose
  // answer can carry a link are reset as soon as they settle.
  const [accessError, setAccessError] = useState<unknown>(null);

  function sent(out: ContactInviteOut) {
    if (out.invite_link) setShownLink({ email: out.contact.email, link: out.invite_link });
    else setSentTo(out.contact.email);
  }

  function inviteOne(contact: ContactOut) {
    setSentTo(null);
    setAccessError(null);
    invite.mutate(contact.id, {
      onSuccess: sent,
      onSettled: (_data, error) => {
        setAccessError(error ?? null);
        invite.reset();
      },
    });
  }
```

(`ContactInviteOut` se importa como tipo desde `../../lib/api`). La tabla gana una columna después de «Status»: `<Th>{t("clients.contacts.colAccess")}</Th>` y, en cada fila, después de la celda del estado:

```tsx
                    <Td>
                      {contact.access === "active" ? (
                        <StatusPill value="active" kind="ok" label={t("clients.contacts.access.active")} />
                      ) : contact.access === "invited" ? (
                        <StatusPill
                          value="invited"
                          kind="warn"
                          label={t("clients.contacts.access.invited", {
                            when: formatDateTime(contact.invite_expires_at, zone),
                          })}
                        />
                      ) : (
                        <StatusPill value="none" kind="mut" label={t("clients.contacts.access.none")} />
                      )}
                    </Td>
```

y, en la celda de acciones, dentro de `{canManage ? (<>…</>) : null}`, después del botón Editar y antes del de apagar/encender:

```tsx
                          {active && contact.access === "none" ? (
                            <>
                              <Btn
                                small
                                icon="mail"
                                label={t("clients.contacts.invite")}
                                ariaLabel={t("clients.contacts.inviteAria", { name })}
                                disabled={invite.isPending}
                                onClick={() => inviteOne(contact)}
                              />{" "}
                            </>
                          ) : null}
                          {active && contact.access === "invited" ? (
                            <>
                              <Btn
                                small
                                icon="mail"
                                label={t("clients.contacts.inviteAgain")}
                                ariaLabel={t("clients.contacts.inviteAgainAria", { name })}
                                disabled={invite.isPending}
                                onClick={() => inviteOne(contact)}
                              />{" "}
                              <Btn
                                small
                                label={t("clients.contacts.cancelInvite")}
                                ariaLabel={t("clients.contacts.cancelInviteAria", { name })}
                                disabled={cancelInvite.isPending}
                                onClick={() => {
                                  setSentTo(null);
                                  setAccessError(null);
                                  cancelInvite.mutate(contact.id, {
                                    onSettled: (_data, error) => setAccessError(error ?? null),
                                  });
                                }}
                              />{" "}
                            </>
                          ) : null}
                          {active && contact.access === "active" ? (
                            <>
                              <Btn
                                small
                                variant="danger"
                                label={t("clients.contacts.resetAccess")}
                                ariaLabel={t("clients.contacts.resetAccessAria", { name })}
                                disabled={reset.isPending}
                                onClick={() => {
                                  reset.reset();
                                  setResetting(contact);
                                }}
                              />{" "}
                            </>
                          ) : null}
```

Debajo del `<p role="alert">` de `change.isError`, dentro del `Panel`:

```tsx
        {sentTo ? (
          <p role="status" className="form-saved">
            <Icon name={KIND_ICON.ok} size={13} />
            {t("clients.contacts.inviteSent", { email: sentTo })}
          </p>
        ) : null}
        {accessError ? (
          <p role="alert" className="form-error">
            {t("clients.contacts.accessFailed")} {describeSubmitError(accessError)}
          </p>
        ) : null}
```

(Cerrar la confirmación reciente rechaza con `StepUpCancelled`, cuya frase dice que no cambió nada.) Después del `Panel` y antes del `ContactFormDialog`:

```tsx
      {shownLink ? (
        // SHOWN ONCE: the server keeps only the link's hash.
        <section aria-label={t("clients.contacts.link.title", { email: shownLink.email })}>
          <Panel title={t("clients.contacts.link.title", { email: shownLink.email })}>
            <div className="notice gold" role="status">
              <Icon name={KIND_ICON.warn} size={16} />
              <span>{t("clients.contacts.link.once", { email: shownLink.email })}</span>
            </div>
            <div className="form-grid" style={{ ...FOLDING_FORM_GRID, marginTop: 12 }}>
              <Field label={t("clients.contacts.link.label")}>
                <input
                  className={inputClass}
                  data-secret
                  readOnly
                  value={shownLink.link}
                  onFocus={(event) => event.currentTarget.select()}
                />
              </Field>
            </div>
            <div className="form-actions">
              <Btn
                icon="copy"
                label={t("clients.contacts.link.copy")}
                onClick={() => void navigator.clipboard?.writeText(shownLink.link).catch(() => undefined)}
              />
              <Btn
                variant="primary"
                icon="check"
                label={t("clients.contacts.link.done")}
                onClick={() => setShownLink(null)}
              />
            </div>
          </Panel>
        </section>
      ) : null}
```

y, junto al `ConfirmDialog` de apagar:

```tsx
      <ConfirmDialog
        open={resetting !== null}
        title={t("clients.contacts.resetTitle")}
        subject={resetting ? resetting.display_name || resetting.email : ""}
        body={t("clients.contacts.resetBody")}
        confirmLabel={t("clients.contacts.resetConfirm")}
        destructive
        onCancel={() => setResetting(null)}
        onConfirm={() => {
          const contact = resetting;
          setResetting(null);
          if (!contact) return;
          setSentTo(null);
          setAccessError(null);
          reset.mutate(contact.id, {
            onSuccess: sent,
            onSettled: (_data, error) => {
              setAccessError(error ?? null);
              reset.reset();
            },
          });
        }}
      />
      {stepUp.dialog}
```

En `frontend/src/i18n/en/clients.ts`, `contacts` gana (y `turnOffBody` cambia, D43):

```ts
    colAccess: "Portal access",
    access: {
      none: "No access",
      invited: "Invited, until {when}",
      active: "Can sign in",
    },
    invite: "Invite",
    inviteAria: "Invite {name} to the portal",
    inviteAgain: "Send again",
    inviteAgainAria: "Send {name} a new invitation",
    cancelInvite: "Cancel invitation",
    cancelInviteAria: "Cancel {name}'s invitation",
    resetAccess: "Reset access",
    resetAccessAria: "Reset portal access for {name}",
    resetTitle: "Reset this contact's portal access",
    resetBody:
      "Their password, passkeys, authenticator app, Microsoft link, remembered devices and sessions stop working at once, and a new invitation is sent. Use it when an account may be in the wrong hands, or the person cannot get in.",
    resetConfirm: "Reset access",
    inviteSent: "Invitation sent to {email}.",
    accessFailed: "Portal access was not changed.",
    link: {
      title: "Portal invitation for {email}",
      once: "Email is not set up, so this link was not sent. It is shown only now: copy it and give it to {email} another way. It works once and expires after 72 hours.",
      label: "Invitation link",
      copy: "Copy link",
      done: "Done, I have passed it on",
    },
    turnOffBody:
      "They can no longer use the portal: their sessions end at once and an open invitation stops working. You can turn them back on here.",
```

y en `frontend/src/i18n/es/clients.ts`:

```ts
    colAccess: "Acceso al portal",
    access: {
      none: "Sin acceso",
      invited: "Invitado, hasta el {when}",
      active: "Puede entrar",
    },
    invite: "Invitar",
    inviteAria: "Invitar a {name} al portal",
    inviteAgain: "Enviar de nuevo",
    inviteAgainAria: "Enviar a {name} una invitación nueva",
    cancelInvite: "Cancelar la invitación",
    cancelInviteAria: "Cancelar la invitación de {name}",
    resetAccess: "Restablecer el acceso",
    resetAccessAria: "Restablecer el acceso al portal de {name}",
    resetTitle: "Restablecer el acceso al portal de este contacto",
    resetBody:
      "Su contraseña, sus claves de acceso, su app de autenticación, su cuenta de Microsoft, sus equipos recordados y sus sesiones dejan de valer en el acto, y se envía una invitación nueva. Úsalo cuando una cuenta pueda estar en malas manos o la persona no consiga entrar.",
    resetConfirm: "Restablecer el acceso",
    inviteSent: "Se envió la invitación a {email}.",
    accessFailed: "No se cambió el acceso al portal.",
    link: {
      title: "Invitación al portal para {email}",
      once: "El correo no está configurado, así que este enlace no se envió. Solo se muestra ahora: cópialo y dáselo a {email} por otra vía. Funciona una vez y caduca a las 72 horas.",
      label: "Enlace de invitación",
      copy: "Copiar el enlace",
      done: "Hecho, ya lo entregué",
    },
    turnOffBody:
      "Ya no podrá usar el portal: sus sesiones terminan en el acto y una invitación abierta deja de funcionar. Puedes volver a activarlo aquí.",
```

En `frontend/src/modules/clients/fixtures.ts`, cada contacto gana `access` e `invite_expires_at` (uno `"active"`, uno `"invited"` con una fecha dentro de 72 h del reloj de los fixtures, uno `"none"`), y las respuestas `inviteContact: () => ({ contact: …, invite_link: null })`, `cancelContactInvite: () => undefined`, `resetContactAccess: () => ({ contact: …, invite_link: null })`.

- [ ] **Step 5: La invitación del contacto**

En `frontend/src/modules/auth/errors.ts`, `checkNewPassword` pasa a:

```ts
/**
 * The new-password checks the console can make before asking (length in code
 * points, design §2.1): at least `minimum` -- 12 for a technician, 15 for a
 * client's contact (design §3) -- and at most 128.
 */
export function checkNewPassword(password: string, again: string, minimum: number = 12): string | null {
  const length = Array.from(password).length;
  if (length < minimum) return translate("auth.password.too_short", { min: minimum });
  if (length > 128) return translate("auth.password.too_long");
  if (password !== again) return translate("auth.password.mismatch");
  return null;
}
```

En `frontend/src/i18n/en/auth.ts`, `password.rules` y `password.too_short` pasan a `"At least {min} characters, any you like, spaces included. A phrase you can remember works well."` y `"Use at least {min} characters."`; en `es/auth.ts`, a `"Al menos {min} caracteres, los que quieras, espacios incluidos. Una frase que recuerdes funciona bien."` y `"Usa al menos {min} caracteres."` (con `min` 12 el texto es el de antes: las pruebas de la fase 1 que lo citan no cambian). `invite` gana:

```ts
    introContact: "You were invited to the support portal as <b>{email}</b>. Choose a password, or use a passkey.",
    usePasskey: "Use a passkey instead",
    passkeyName: "Name for this passkey",
    or: "or",
```

```ts
    introContact: "Te invitaron al portal de soporte como <b>{email}</b>. Elige una contraseña o usa una clave de acceso.",
    usePasskey: "Usar una clave de acceso",
    passkeyName: "Nombre de esta clave de acceso",
    or: "o",
```

En `frontend/src/modules/auth/NewPasswordFields.tsx`: la cabecera dice «at least 12 characters (15 for a client's contact, `minimum`)»; la firma gana `minimum = 12` (`minimum?: number`) y el `help` pasa a `t("auth.password.rules", { min: minimum })`. Busca con `grep -rn "auth.password.rules\|checkNewPassword(" frontend/src` las demás llamadas: las que no pasan mínimo siguen en 12, que es lo correcto para un técnico.

En `frontend/src/modules/auth/InvitePage.tsx`: importar `createPasskey`, `canUsePasskeys`, `describePasskeyError` (`./passkey`), `enterConsole` (`../../lib/session`) y `translate` (`../../i18n/core`); el docstring gana el párrafo «A CLIENT'S CONTACT (own-auth phase 2, Task 11, decision 2): the invitation says `kind` and the shortest password allowed (15). A contact chooses a password or a passkey and is signed in at once: there is no second factor to add, and the next page is the portal.»; y el cuerpo, desde `async function submit()` hasta el final, pasa a:

```tsx
  const [passkeyName, setPasskeyName] = useState(() => translate("auth.enroll.passkeyDefaultName"));

  function landed(kind: "staff" | "contact", state: "enroll_required" | "signed_in") {
    if (state === "signed_in") enterConsole(kind === "contact" ? "/portal" : null);
    else navigate("/sign-in/enroll", { replace: true });
  }

  async function guarded(step: () => Promise<void>) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      await step();
    } catch (cause: unknown) {
      if (isDeadLink(cause)) setDead(true);
      else setError(describePasskeyError(cause) ?? describeAuthError(cause, { canTimeOut: false }));
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  function submit(kind: "staff" | "contact", minimum: number) {
    const problem = checkNewPassword(password, again, minimum);
    if (problem) {
      setError(problem);
      return;
    }
    void guarded(async () => {
      const result = await api.acceptInvite({ token, password });
      landed(kind, result.state);
    });
  }

  function withPasskey() {
    void guarded(async () => {
      // The options are fetched by this click and nothing else stands before
      // the browser's prompt (a passkey is made only from a click).
      const credential = await createPasskey(await api.invitePasskeyOptions(token));
      await api.invitePasskey({ token, credential, name: passkeyName.trim() });
      landed("contact", "signed_in");
    });
  }

  if (invite.isPending) {
    return (
      <AuthCard title={t("auth.invite.loading")}>
        <p className="note" style={{ margin: 0 }}>
          {t("auth.common.working")}
        </p>
      </AuthCard>
    );
  }
  if (dead || (invite.isError && isDeadLink(invite.error))) {
    return (
      <AuthCard title={t("auth.invite.heading")}>
        <AuthAlert text={t("auth.invite.invalid")} />
      </AuthCard>
    );
  }
  if (invite.isError) {
    return (
      <AuthCard title={t("auth.invite.heading")}>
        <AuthAlert text={describeAuthError(invite.error, { canTimeOut: false })} />
      </AuthCard>
    );
  }
  const { email, display_name, kind, password_minimum, passkey } = invite.data;
  const offerPasskey = kind === "contact" && passkey && canUsePasskeys();
  return (
    <AuthCard
      title={t("auth.invite.title", { name: display_name || email })}
      intro={
        <Trans
          k={kind === "contact" ? "auth.invite.introContact" : "auth.invite.intro"}
          vars={{ email }}
          components={{ b: <b style={{ color: "var(--ink)" }} /> }}
        />
      }
    >
      {offerPasskey ? (
        <>
          <Field label={t("auth.invite.passkeyName")}>
            <input
              className={inputClass}
              value={passkeyName}
              maxLength={64}
              onChange={(event) => setPasskeyName(event.target.value)}
            />
          </Field>
          <div className="form-actions">
            <Btn
              variant="primary"
              icon="key"
              label={t("auth.invite.usePasskey")}
              disabled={busy || !passkeyName.trim()}
              onClick={withPasskey}
            />
          </div>
          <p className="note" style={{ margin: 0 }}>
            {t("auth.invite.or")}
          </p>
        </>
      ) : null}
      <Form onSubmit={() => submit(kind, password_minimum)}>
        {/* For the password manager: the account the new password belongs to. */}
        <Field label={t("auth.common.email")}>
          <input className={inputClass} type="email" autoComplete="username" value={email} readOnly />
        </Field>
        <NewPasswordFields
          password={password}
          again={again}
          minimum={password_minimum}
          onPassword={setPassword}
          onAgain={setAgain}
        />
        <AuthAlert text={error} />
        <div className="form-actions">
          <Btn
            type="submit"
            variant={offerPasskey ? "secondary" : "primary"}
            icon="arrow-right"
            label={busy ? t("auth.common.working") : t("auth.invite.submit")}
            disabled={busy || !password || !again}
          />
        </div>
      </Form>
    </AuthCard>
  );
}
```

(el `useState` de `passkeyName` va con los demás, antes de cualquier `return`). En `frontend/src/modules/auth/fixtures.ts`: `getInvite` gana `kind: "staff", password_minimum: 12, passkey: false, microsoft: false`; `acceptInvite: () => ({ state: "enroll_required" as const })`; `invitePasskeyOptions` y `invitePasskey` como las de alta de la fase 1 (`enrollPasskeyOptions`) y `{ state: "signed_in" as const }`.

- [ ] **Step 6: Mi seguridad del contacto**

En `frontend/src/modules/profile/api.ts`, después de `useChangePasswordMutation`:

```ts
/**
 * The FIRST password, for someone who has none (own-auth phase 2): a recent
 * confirmation, like a change. Its body is the password: `gcTime: 0`.
 */
export function useAddPasswordMutation(run: StepUpRun) {
  const api = useApi();
  const refetch = useRefetchSecurity();
  return useMutation<void, Error, string>({
    mutationFn: (password) => run(() => api.addPassword(password)),
    onSuccess: refetch,
    gcTime: 0,
  });
}
```

En `frontend/src/modules/profile/SecurityPage.tsx`:

1. La cabecera gana, debajo de la lista de rutas, `//   POST   /me/password                                        the first password (own-auth phase 2)` y el párrafo:

```tsx
// A CLIENT'S CONTACT HAS THE SAME PAGE IN THE PORTAL (`PortalSecurityPage`,
// `/portal/security`; own-auth phase 2, Task 11). `GET /me/security` says
// whose rules apply (`kind`): a contact's password is 15 characters or more,
// their authenticator app is optional, their last way in -- a password, a
// passkey or Microsoft -- is never offered for removal, and recovery codes
// are shown only to one who has a second factor (operator question P12). The
// time zone comes from `/portal/me`: a contact cannot read the
// organisation's, nor `/me`. The server refuses all of it anyway.
```

2. Importar `usePortalMe` (`../portal/usePortalMe`) y `useAddPasswordMutation`; `export function SecurityPage()` pasa a:

```tsx
/** `/profile/security`, in the console's frame. */
export function SecurityPage() {
  const zone = useOrgZone();
  return <SecurityBody zone={zone} />;
}

/** `/portal/security`, in the portal's: the zone of `/portal/me` (decision 3). */
export function PortalSecurityPage() {
  const me = usePortalMe();
  return <SecurityBody zone={me.data?.timezone ?? null} />;
}

function SecurityBody({ zone }: { zone: string | null }) {
  const t = useT();
  const security = useMySecurity();
  const stepUp = useStepUp();
  const data = security.data;
  const contact = data?.kind === "contact";
  const secondFactor = data !== undefined && (data.has_totp || data.passkeys.length > 0);
```

con el mismo JSX de antes, salvo los paneles:

```tsx
          {data.has_password ? (
            <PasswordPanel run={stepUp.run} minimum={contact ? 15 : 12} contact={contact} />
          ) : (
            <AddPasswordPanel run={stepUp.run} minimum={contact ? 15 : 12} />
          )}
          <PasskeysPanel security={data} run={stepUp.run} zone={zone} />
          <TotpPanel security={data} run={stepUp.run} />
          {!contact || secondFactor ? <RecoveryPanel security={data} run={stepUp.run} /> : null}
          <DevicesPanel security={data} run={stepUp.run} zone={zone} />
          <SessionsPanel security={data} run={stepUp.run} zone={zone} />
```

3. `PasswordPanel` gana `minimum: number` y `contact: boolean`: cada `checkNewPassword(next, repeat)` pasa a `checkNewPassword(next, repeat, minimum)`, y el subtítulo a `t(contact ? "profile.securityPage.password.subtitleContact" : "profile.securityPage.password.subtitle")`.

4. Nuevo, después de `PasswordPanel`:

```tsx
/**
 * The FIRST password (own-auth phase 2): for someone who signed up with a
 * passkey or Microsoft. No current password to give; the recent confirmation
 * is the server's to ask (§2.5), and the boxes empty whatever it answers.
 */
function AddPasswordPanel({ run, minimum }: { run: StepUpRun; minimum: number }) {
  const t = useT();
  const add = useAddPasswordMutation(run);
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [checked, setChecked] = useState(false);
  const [outcome, setOutcome] = useState<{ kind: "added" } | { kind: "failed"; error: unknown } | null>(null);
  const refusal = checked ? checkNewPassword(next, repeat, minimum) : null;

  function submit() {
    setOutcome(null);
    setChecked(true);
    if (checkNewPassword(next, repeat, minimum) !== null) return;
    add.mutate(next, {
      onSettled: (_data, error) => {
        setNext("");
        setRepeat("");
        setChecked(false);
        setOutcome(error ? { kind: "failed", error } : { kind: "added" });
        add.reset();
      },
    });
  }

  return (
    <Panel title={t("profile.securityPage.password.title")} subtitle={t("profile.securityPage.password.addSubtitle", { min: minimum })}>
      <Form onSubmit={submit}>
        <div className="form-grid" style={FOLDING_FORM_GRID}>
          <Field label={t("profile.securityPage.password.next")}>
            <input
              className={inputClass}
              type="password"
              autoComplete="new-password"
              value={next}
              onChange={(event) => setNext(event.target.value)}
            />
          </Field>
          <Field label={t("profile.securityPage.password.repeat")}>
            <input
              className={inputClass}
              type="password"
              autoComplete="new-password"
              value={repeat}
              onChange={(event) => setRepeat(event.target.value)}
            />
          </Field>
        </div>
        <div className="form-actions">
          <Btn
            type="submit"
            variant="primary"
            icon="lock"
            label={add.isPending ? t("profile.securityPage.password.adding") : t("profile.securityPage.password.add")}
            disabled={add.isPending}
          />
          {refusal ? (
            <span role="alert" className="form-error">
              <Icon name={KIND_ICON.crit} size={13} />
              {refusal}
            </span>
          ) : null}
          {outcome?.kind === "failed" ? (
            <span role="alert" className="form-error">
              <Icon name={KIND_ICON.crit} size={13} />
              {describeSubmitError(outcome.error)}
            </span>
          ) : null}
          {outcome?.kind === "added" ? (
            <span role="status" className="form-saved">
              <Icon name={KIND_ICON.ok} size={13} />
              {t("profile.securityPage.password.added")}
            </span>
          ) : null}
        </div>
      </Form>
    </Panel>
  );
}
```

5. En `PasskeysPanel`: `onlyFactor` pasa a

```tsx
  // A technician never goes without a second factor; a contact, without a
  // way in (own-auth phase 2: a password, a passkey or Microsoft). The
  // server refuses both (`last_factor`, `last_method`); this only says so first.
  const contact = security.kind === "contact";
  const onlyFactor = contact
    ? security.passkeys.length <= 1 && !security.has_password && security.microsoft === null
    : security.passkeys.length + (security.has_totp ? 1 : 0) <= 1;
  const onlyFactorText = t(
    contact ? "profile.securityPage.passkeys.lastMethod" : "profile.securityPage.passkeys.onlyFactor",
  );
```

y los dos usos de `t("profile.securityPage.passkeys.onlyFactor")` pasan a `onlyFactorText`. En `TotpPanel`: `const onlyFactor = security.kind !== "contact" && security.has_totp && security.passkeys.length === 0;` y su subtítulo, `t(security.kind === "contact" ? "profile.securityPage.totp.subtitleContact" : "profile.securityPage.totp.subtitle")`.

En `frontend/src/i18n/en/profile.ts`, `securityPage.password` gana:

```ts
      subtitleContact: "At least 15 characters. A long phrase you can remember beats a short complicated one",
      addSubtitle: "You sign in without one. Add one of at least {min} characters to have another way in",
      add: "Add password",
      adding: "Adding…",
      added: "Your password was added.",
```

`securityPage.passkeys`, `lastMethod: "This passkey is your only way to sign in. Add a password before you remove it."`; `securityPage.totp`, `subtitleContact: "Optional. When it is on, signing in with your password also asks for a code"`. En `es/profile.ts`:

```ts
      subtitleContact: "Al menos 15 caracteres. Una frase larga que puedas recordar es mejor que una corta y complicada",
      addSubtitle: "Entras sin contraseña. Añade una de al menos {min} caracteres para tener otra forma de entrar",
      add: "Añadir contraseña",
      adding: "Añadiendo…",
      added: "Se añadió tu contraseña.",
```

`lastMethod: "Esta clave de acceso es tu única forma de entrar. Añade una contraseña antes de quitarla."`; `subtitleContact: "Opcional. Si está activa, entrar con tu contraseña también pide un código"`.

- [ ] **Step 7: La confirmación con la contraseña**

En `frontend/src/components/stepUp.tsx`: la cabecera, después de «passkey first, the design's order --», gana «or, for a client's contact, the password (own-auth phase 2, D29) --»; en `StepUpDialog`, junto a `recovery`, `const [password, setPassword] = useState("");` (vaciado en el efecto de `open` y tras aceptar, como los otros dos); debajo de `const codesLeft = …`:

```tsx
  // A CLIENT'S CONTACT ALSO CONFIRMS WITH THE PASSWORD (D29): their second
  // factor is optional. One who has only Microsoft is told to sign in again,
  // which leaves the confirmation fresh (operator question P13).
  const contactPassword = data?.kind === "contact" && data.has_password;
```

después del bloque de `hasTotp`:

```tsx
      {contactPassword ? (
        <Form onSubmit={() => void attempt(() => api.stepUpPassword(password))}>
          <div className="form-grid">
            <Field label={t("components.stepUp.password")}>
              <input
                className={inputClass}
                type="password"
                autoComplete="current-password"
                value={password}
                disabled={busy}
                onChange={(event) => setPassword(event.target.value)}
              />
            </Field>
          </div>
          <div className="form-actions">
            <Btn
              type="submit"
              icon="check"
              variant={hasPasskey || hasTotp ? "secondary" : "primary"}
              label={t("components.stepUp.confirmPassword")}
              disabled={busy || password === ""}
            />
          </div>
        </Form>
      ) : null}
```

y el aviso de «sin método» pasa a:

```tsx
      {data && !ownsPasskey && !hasTotp && codesLeft === 0 && !contactPassword ? (
        <p className="note">
          {t(data.kind === "contact" ? "components.stepUp.noMethodContact" : "components.stepUp.noMethod")}
        </p>
      ) : null}
```

En `frontend/src/i18n/en/components.ts`, `stepUp` gana `password: "Your password"`, `confirmPassword: "Confirm with your password"`, `noMethodContact: "Sign out and sign in again, then make this change within 10 minutes."`; en `es/components.ts`, `password: "Tu contraseña"`, `confirmPassword: "Confirmar con tu contraseña"`, `noMethodContact: "Sal y vuelve a entrar, y haz este cambio en los 10 minutos siguientes."`.

- [ ] **Step 8: La ruta del portal**

En `frontend/src/App.tsx`: importar `PortalSecurityPage` junto a `YourSecurityPage` (`import { PortalSecurityPage, SecurityPage as YourSecurityPage } from "./modules/profile/SecurityPage";`) y, después de la ruta `/portal/tickets/:ticketId`:

```tsx
            {/* A client's contact's own sign-in security (own-auth phase 2,
                Task 11): the same page as /profile/security, in the portal. */}
            <Route
              path="/portal/security"
              element={
                <RequireAuth>
                  <PortalSecurityPage />
                </RequireAuth>
              }
            />
```

En `frontend/scripts/routes.mjs`, `"/portal/security",` después de `"/portal/tickets/tkt-1",`. En `frontend/src/modules/portal/PortalShell.tsx`, en el `nav`, `<Link to="/portal/security">{t("portal.shell.security")}</Link>`; en `i18n/en/portal.ts`, `shell.security: "Sign-in and security"`; en `es/portal.ts`, `shell.security: "Acceso y seguridad"`. En `frontend/src/modules/profile/fixtures.ts`, la respuesta de `getMySecurity` gana `kind: "staff", has_password: true, microsoft: null`.

- [ ] **Step 9: Correr las pruebas, los tipos y la construcción**

Run (desde `frontend/`): `npx vitest run && npx tsc -b && npm run build`
Expected: PASS y sin errores. `routes-covered.test.ts` ve `/portal/security` en `App.tsx` y en `routes.mjs`; los catálogos `en`/`es` tienen las mismas claves y los mismos `{min}`, `{name}`, `{email}`, `{when}`.

- [ ] **Step 10: Commit**

```bash
git add frontend/src frontend/scripts/routes.mjs
git commit -m "feat(console): contacts' portal access, their invitation and their own security"
```

---

### Task 12: Consola — «Entrar con Microsoft»

Diseño §4 (técnicos desde el tenant de Novieri, contactos desde los tenants de su cliente, nunca una cuenta por entrar, el enlace la primera vez desde la invitación o desde «Mi perfil»). D30–D35, D41. Oleada 6; consume los contratos de las Tasks 5 y 9 y el código de las Tasks 10 y 11.

**Files:**
- Modify: `frontend/src/lib/api.ts` (`ClientOut.microsoft_tenants`, `ClientUpdate.microsoft_tenants`; `MicrosoftStartIn`, `MicrosoftStep`; `microsoftStart`, `microsoftComplete`, `unlinkMicrosoft`, `getMicrosoftSettings`, `putMicrosoftSettings`)
- Create: `frontend/src/modules/auth/microsoft.ts`, `frontend/src/modules/auth/MicrosoftButton.tsx`, `frontend/src/modules/auth/MicrosoftCallbackPage.tsx`
- Modify: `frontend/src/modules/auth/SignInPage.tsx`, `frontend/src/modules/auth/PortalSignInPage.tsx`, `frontend/src/modules/auth/InvitePage.tsx`
- Modify: `frontend/src/modules/profile/SecurityPage.tsx` (`MicrosoftPanel`), `frontend/src/modules/profile/api.ts` (`useUnlinkMicrosoftMutation`)
- Create: `frontend/src/modules/settings/MicrosoftTab.tsx`
- Modify: `frontend/src/modules/settings/tabs.ts`, `SettingsPage.tsx`, `api.ts`, `types.ts`, `fixtures.ts`
- Modify: `frontend/src/modules/clients/ClientForm.tsx`, `frontend/src/modules/clients/labels.ts` (`parseTenants`)
- Modify: `frontend/src/App.tsx` (la ruta `/sign-in/microsoft`), `frontend/scripts/routes.mjs`, `frontend/src/modules/auth/__tests__/routes.test.ts`
- Modify: `frontend/src/i18n/en/{auth,profile,settings,clients}.ts` y los mismos de `es/`
- Modify: `frontend/src/modules/auth/fixtures.ts`, `frontend/src/modules/auth/__tests__/harness.tsx`, `frontend/src/modules/settings/__tests__/harness.tsx`, `frontend/src/modules/settings/__tests__/settingsPage.test.tsx`, `frontend/src/modules/clients/__tests__/clients.test.tsx`
- Create: `frontend/src/modules/auth/__tests__/microsoft.test.tsx`, `frontend/src/modules/settings/__tests__/microsoft.test.tsx`, `frontend/src/modules/profile/SecurityPage.microsoft.test.tsx`

**Interfaces:**
- Consumes: `POST /auth/microsoft/start {intent, audience, token?}` → `{authorize_url}` (+ cookie), `POST /auth/microsoft/complete {code, state}` → `{state: "signed_in" | "factor_required" | "enroll_required" | "linked", methods?, remember_device_days?}`, `DELETE /me/microsoft`, códigos `microsoft_unavailable` (409/503), `microsoft_failed` (400), `microsoft_not_linked` (401), `microsoft_tenant_refused` (403), `microsoft_already_linked` (409), `last_method` (409), `step_up_required` (403) (Task 9); `GET`/`PUT /settings/microsoft-sign-in` (`EntraSettingsOut`/`EntraSettingsIn`), `ClientOut.microsoft_tenants` (Task 5); `InviteDetails.microsoft`, `MySecurity.microsoft` (Task 11).
- Produces: `MICROSOFT_ORIGIN`, `rememberMicrosoftReturn`, `readMicrosoftReturn`, `forgetMicrosoftReturn`, `takeMicrosoftReturn`, `microsoftReturnOf(hash)`, `checkedAuthorizeUrl(url)`, `describeMicrosoftError(error)` (`modules/auth/microsoft.ts`); `MicrosoftButton`; `MicrosoftCallbackPage` en `/sign-in/microsoft`; `MicrosoftTab` (`?tab=microsoft`); `parseTenants`.

**Decisión del plan (1): el botón está siempre en las dos páginas de entrada.** Ninguna ruta anónima dice si una instalación tiene Microsoft configurado, y añadirla sería una lectura más antes de la identidad (Pregunta P15). Un clic donde no está configurado recibe una frase («Entrar con Microsoft no está configurado aquí», 409 `microsoft_unavailable`). La invitación sí lo sabe (`InviteDetails.microsoft`) y solo lo ofrece entonces; «Mi seguridad» lo ofrece siempre, con la misma frase si no está.

**Decisión del plan (2): la vuelta es una página, y el código viaja en el cuerpo** (D30). Microsoft devuelve `#code=…&state=…` a `/sign-in/microsoft` (`response_mode=fragment`); la página lo lee una vez, lo quita de la dirección (como `useLinkToken`), y lo envía con `POST /auth/microsoft/complete`, que lleva la cookie `__Host-itms_entra` porque es una petición del mismo origen. Un `error` de Microsoft en el fragmento, o ningún código (una recarga), se dice con una frase y un enlace de vuelta; nunca se reenvía nada. Una sola petición aunque React monte la página dos veces (un `ref`).

**Decisión del plan (3): adónde volver lo guarda la pestaña, no el servidor.** Antes de salir hacia Microsoft, la consola guarda en `sessionStorage` (`itms.microsoft.return`) la intención y la página a la que volver — una ruta, no un secreto, filtrada por `safeNext` al leerla y borrada al leerla. La transacción del servidor no lleva `next`: lo que se firma allí es la identidad, no la navegación.

**Decisión del plan (4): la consola solo navega a Microsoft.** `authorize_url` viene del servidor, pero la consola comprueba que su origen es `https://login.microsoftonline.com` (el host fijo de la Task 5, [M3]) antes de `navigation.assign`; otro origen es un fallo con frase, no una navegación. No protege nada que el servidor no proteja ya (invariante 6), pero una respuesta del API nunca decide adónde va el documento entero.

- [ ] **Step 1: Escribir las pruebas**

Crear `frontend/src/modules/auth/__tests__/microsoft.test.tsx`:

```tsx
/**
 * Sign in with Microsoft (own-auth phase 2, Task 12; design §4): the button
 * leaves only for Microsoft, the return page sends the code once in a body,
 * and every refusal is one sentence.
 */
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../../lib/api";
import { navigation } from "../../../lib/session";
import { microsoftReturnOf, rememberMicrosoftReturn, takeMicrosoftReturn } from "../microsoft";
import { authApi, calls, installDefaults, renderAuth, where } from "./harness";

vi.mock("../../../lib/ApiContext", () => ({ useApi: () => authApi() }));

const AUTHORIZE = "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize?client_id=x";
let assign: ReturnType<typeof vi.spyOn>;
let replace: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  installDefaults();
  window.sessionStorage.clear();
  assign = vi.spyOn(navigation, "assign").mockImplementation(() => {});
  replace = vi.spyOn(navigation, "replace").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("the return address", () => {
  it("is read once from the fragment", () => {
    expect(microsoftReturnOf("#code=abc&state=xyz")).toEqual({ code: "abc", state: "xyz" });
    expect(microsoftReturnOf("#error=access_denied&error_description=no")).toEqual({ error: "access_denied" });
    expect(microsoftReturnOf("")).toBeNull();
    expect(microsoftReturnOf("#code=abc")).toBeNull();
  });

  it("remembers where to go back, once, and never a page without a session", () => {
    rememberMicrosoftReturn({ intent: "sign_in", back: "/portal" });
    expect(takeMicrosoftReturn()).toEqual({ intent: "sign_in", back: "/portal" });
    expect(takeMicrosoftReturn()).toEqual({ intent: "sign_in", back: "/" });
    rememberMicrosoftReturn({ intent: "sign_in", back: "//evil.example" });
    expect(takeMicrosoftReturn().back).toBe("/");
  });
});

describe("the button", () => {
  it("on the console's page asks for a technician's sign-in and leaves for Microsoft", async () => {
    calls.microsoftStart.mockResolvedValue({ authorize_url: AUTHORIZE });
    const user = userEvent.setup();
    renderAuth("/sign-in?next=%2Fdevices");
    await user.click(screen.getByRole("button", { name: "Sign in with Microsoft" }));
    expect(calls.microsoftStart).toHaveBeenCalledWith({ intent: "sign_in", audience: "staff" }, {});
    await waitFor(() => expect(assign).toHaveBeenCalledWith(AUTHORIZE));
    expect(takeMicrosoftReturn()).toEqual({ intent: "sign_in", back: "/devices" });
  });

  it("on the portal's page asks for a contact's sign-in", async () => {
    calls.microsoftStart.mockResolvedValue({ authorize_url: AUTHORIZE });
    const user = userEvent.setup();
    renderAuth("/portal/sign-in");
    await user.click(screen.getByRole("button", { name: "Sign in with Microsoft" }));
    expect(calls.microsoftStart).toHaveBeenCalledWith({ intent: "sign_in", audience: "contact" }, {});
    await waitFor(() => expect(assign).toHaveBeenCalledWith(AUTHORIZE));
  });

  it("never leaves for anywhere but Microsoft", async () => {
    calls.microsoftStart.mockResolvedValue({ authorize_url: "https://evil.example/authorize" });
    const user = userEvent.setup();
    renderAuth("/sign-in");
    await user.click(screen.getByRole("button", { name: "Sign in with Microsoft" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The sign-in with Microsoft did not finish. Start it again.");
    expect(assign).not.toHaveBeenCalled();
  });

  it("says so where Microsoft is not set up", async () => {
    calls.microsoftStart.mockRejectedValue(new ApiError(409, '{"code":"microsoft_unavailable"}'));
    const user = userEvent.setup();
    renderAuth("/sign-in");
    await user.click(screen.getByRole("button", { name: "Sign in with Microsoft" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Signing in with Microsoft is not set up here.");
  });

  it("is offered on an invitation only when the invitation says so", async () => {
    calls.getInvite.mockResolvedValue({
      email: "ana@acme.test",
      display_name: "Ana Ruiz",
      kind: "contact",
      password_minimum: 15,
      passkey: true,
      microsoft: true,
    });
    calls.microsoftStart.mockResolvedValue({ authorize_url: AUTHORIZE });
    const user = userEvent.setup();
    renderAuth("/invite#tok-contact-1");
    await user.click(await screen.findByRole("button", { name: "Accept with Microsoft" }));
    expect(calls.microsoftStart).toHaveBeenCalledWith(
      { intent: "invite", audience: "contact", token: "tok-contact-1" },
      {},
    );
    expect(takeMicrosoftReturn()).toEqual({ intent: "invite", back: "/portal" });
  });
});

describe("the return page", () => {
  it("sends the code once and lands where the person was going", async () => {
    rememberMicrosoftReturn({ intent: "sign_in", back: "/portal" });
    calls.microsoftComplete.mockResolvedValue({ state: "signed_in" });
    renderAuth("/sign-in/microsoft#code=abc&state=xyz");
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/portal"));
    expect(calls.microsoftComplete).toHaveBeenCalledExactlyOnceWith({ code: "abc", state: "xyz" });
    expect(where.location?.hash).toBe("");
  });

  it("goes on to the second factor, carrying the page", async () => {
    rememberMicrosoftReturn({ intent: "sign_in", back: "/devices" });
    calls.microsoftComplete.mockResolvedValue({ state: "factor_required", methods: ["totp"], remember_device_days: 15 });
    renderAuth("/sign-in/microsoft#code=abc&state=xyz");
    await waitFor(() => expect(where.location?.pathname).toBe("/sign-in/factor"));
    expect(where.location?.search).toBe("?next=%2Fdevices");
  });

  it("after linking, goes back to the security page", async () => {
    rememberMicrosoftReturn({ intent: "link", back: "/profile/security" });
    calls.microsoftComplete.mockResolvedValue({ state: "linked" });
    renderAuth("/sign-in/microsoft#code=abc&state=xyz");
    await waitFor(() => expect(where.location?.pathname).toBe("/profile/security"));
  });

  it("says one sentence for an account nobody here has linked, and offers to sign in another way", async () => {
    rememberMicrosoftReturn({ intent: "sign_in", back: "/portal" });
    calls.microsoftComplete.mockRejectedValue(new ApiError(401, '{"code":"microsoft_not_linked"}'));
    renderAuth("/sign-in/microsoft#code=abc&state=xyz");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This Microsoft account is not linked to anybody here. Sign in another way and link it from your profile, or use your invitation.",
    );
    expect(screen.getByRole("link", { name: "Back to sign in" })).toHaveAttribute(
      "href",
      "/portal/sign-in?next=%2Fportal",
    );
  });

  it("says Microsoft's own refusal without asking the server", async () => {
    renderAuth("/sign-in/microsoft#error=access_denied&error_description=The%20user%20cancelled");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Microsoft did not sign you in. Try again, or sign in another way.",
    );
    expect(calls.microsoftComplete).not.toHaveBeenCalled();
    expect(screen.queryByText(/The user cancelled/)).not.toBeInTheDocument();
  });

  it("asks nothing after a reload, which has no code", async () => {
    renderAuth("/sign-in/microsoft");
    expect(await screen.findByRole("alert")).toHaveTextContent("The sign-in with Microsoft did not finish. Start it again.");
    expect(calls.microsoftComplete).not.toHaveBeenCalled();
  });
});
```

En `frontend/src/modules/auth/__tests__/harness.tsx`: `import { MicrosoftCallbackPage } from "../MicrosoftCallbackPage";`; en `calls`, `microsoftStart: vi.fn(), microsoftComplete: vi.fn(),`; en `installDefaults`, `calls.microsoftStart.mockRejectedValue(new ApiError(409, '{"code":"microsoft_unavailable"}'));` (importar `ApiError`) y `calls.microsoftComplete.mockResolvedValue({ state: "signed_in" });`; en `renderAuth`, `<Route path="/sign-in/microsoft" element={<MicrosoftCallbackPage />} />`, y las rutas `/profile/security` y `/devices` como `<Route path=… element={<p>…</p>} />` si el arnés no las tiene (la prueba de «linked» mira solo `pathname`).

Crear `frontend/src/modules/settings/__tests__/microsoft.test.tsx`:

```tsx
/**
 * Settings -> Microsoft sign-in (own-auth phase 2, Task 12; plan D33-D34):
 * the application, Novieri's tenant and the secret, which is sent once and
 * never shown; the redirect address to register; `orgs.manage` on every
 * client to save.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { MICROSOFT_SETTINGS_FIXTURE, apiClient, calls, installDefaults, renderSettings } from "./harness";

vi.mock("../../../lib/ApiContext", () => ({ useApi: () => apiClient() }));

const APP = "3f2504e0-4f89-41d3-9a0c-0305e82c3301";
const TENANT = "9a1b2c3d-4e5f-4a6b-8c7d-0e1f2a3b4c5d";

beforeEach(() => {
  installDefaults();
});

describe("Microsoft sign-in", () => {
  it("shows the address to register, and no secret", async () => {
    renderSettings("/settings?tab=microsoft");
    const tab = await screen.findByRole("tabpanel");
    expect(await within(tab).findByDisplayValue(MICROSOFT_SETTINGS_FIXTURE.redirect_uri)).toBeInTheDocument();
    expect(within(tab).getByLabelText("Client secret")).toHaveValue("");
  });

  it("saves every value, the secret once, and empties its box", async () => {
    const user = userEvent.setup();
    renderSettings("/settings?tab=microsoft");
    const tab = await screen.findByRole("tabpanel");
    await user.click(await within(tab).findByRole("switch", { name: "People may sign in with Microsoft" }));
    await user.type(within(tab).getByLabelText("Application (client) ID"), APP);
    await user.type(within(tab).getByLabelText("Novieri's Microsoft organization (tenant ID)"), TENANT);
    await user.type(within(tab).getByLabelText("Client secret"), "s3cr3t~value");
    await user.click(within(tab).getByRole("button", { name: "Save changes" }));
    await waitFor(() =>
      expect(calls.putMicrosoftSettings).toHaveBeenCalledExactlyOnceWith({
        enabled: true,
        app_client_id: APP,
        staff_tenant_id: TENANT,
        client_secret: "s3cr3t~value",
      }),
    );
    await waitFor(() => expect(within(tab).getByLabelText("Client secret")).toHaveValue(""));
  });

  it("refuses an identifier that is not one before asking", async () => {
    const user = userEvent.setup();
    renderSettings("/settings?tab=microsoft");
    const tab = await screen.findByRole("tabpanel");
    await user.type(await within(tab).findByLabelText("Application (client) ID"), "not-a-guid");
    expect(within(tab).getByText("Each identifier is a GUID, as Microsoft shows it: 8-4-4-4-12 hexadecimal digits.")).toBeInTheDocument();
    expect(calls.putMicrosoftSettings).not.toHaveBeenCalled();
  });

  it("is not offered without orgs.manage", async () => {
    installDefaults(["users.read"]);
    renderSettings();
    await screen.findByRole("tab", { name: /Organization/ });
    expect(screen.queryByRole("tab", { name: /Microsoft sign-in/ })).not.toBeInTheDocument();
    expect(calls.getMicrosoftSettings).not.toHaveBeenCalled();
  });
});
```

En `frontend/src/modules/settings/__tests__/harness.tsx`: `export const MICROSOFT_SETTINGS_FIXTURE = { enabled: false, app_client_id: null, staff_tenant_id: null, configured: false, redirect_uri: "https://itms.example.test/sign-in/microsoft", updated_at: null, updated_by: null };`; en `calls`, `getMicrosoftSettings: vi.fn(), putMicrosoftSettings: vi.fn(),`; en `installDefaults`, `calls.getMicrosoftSettings.mockResolvedValue(MICROSOFT_SETTINGS_FIXTURE);` y `calls.putMicrosoftSettings.mockImplementation(async (body: { enabled: boolean; app_client_id: string | null; staff_tenant_id: string | null }) => ({ ...MICROSOFT_SETTINGS_FIXTURE, ...body, configured: true }));`. En `settingsPage.test.tsx`, `"Microsoft sign-in"` al final de `ALL_TABS` y en la lista de pestañas ocultas sin permiso (`["Assistant", …, "Sessions", "Microsoft sign-in"]`), y `expect(calls.getMicrosoftSettings).not.toHaveBeenCalled();` junto a `getSessionSettings`.

Crear `frontend/src/modules/profile/SecurityPage.microsoft.test.tsx`:

```tsx
/**
 * The Microsoft account in Sign-in and security (own-auth phase 2, Task 12;
 * design §4: linked the first time from the invitation or from the profile):
 * link with a recent confirmation, unlink never the last way in.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../lib/api";
import { navigation } from "../../lib/session";
import { takeMicrosoftReturn } from "../auth/microsoft";
import { MY_SECURITY_FIXTURE, apiClient, calls, installDefaults, renderSecurityPage } from "../settings/__tests__/harness";

vi.mock("../../lib/ApiContext", () => ({ useApi: () => apiClient() }));

const AUTHORIZE = "https://login.microsoftonline.com/9a1b2c3d-4e5f-4a6b-8c7d-0e1f2a3b4c5d/oauth2/v2.0/authorize?x=1";
const LINK = { tenant_id: "9a1b2c3d-4e5f-4a6b-8c7d-0e1f2a3b4c5d", created_at: "2026-11-01T09:00:00Z", last_used_at: null };
let assign: ReturnType<typeof vi.spyOn>;

async function panel(): Promise<HTMLElement> {
  const heading = await screen.findByRole("heading", { name: "Microsoft account" });
  return heading.closest("section") as HTMLElement;
}

beforeEach(() => {
  installDefaults();
  window.sessionStorage.clear();
  assign = vi.spyOn(navigation, "assign").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("the Microsoft account", () => {
  it("links after the server's recent confirmation, and comes back here", async () => {
    calls.microsoftStart
      .mockRejectedValueOnce(new ApiError(403, '{"code":"step_up_required"}'))
      .mockResolvedValue({ authorize_url: AUTHORIZE });
    const user = userEvent.setup();
    renderSecurityPage();
    await user.click(within(await panel()).getByRole("button", { name: "Link a Microsoft account" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.type(within(dialog).getByLabelText("Code from your authenticator app"), "123456");
    await user.click(within(dialog).getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith(AUTHORIZE));
    expect(calls.microsoftStart).toHaveBeenLastCalledWith({ intent: "link", audience: "staff" }, { signedIn: true });
    expect(takeMicrosoftReturn()).toEqual({ intent: "link", back: "/profile/security" });
  });

  it("unlinks after a confirmation", async () => {
    calls.getMySecurity.mockResolvedValue({ ...MY_SECURITY_FIXTURE, microsoft: LINK });
    const user = userEvent.setup();
    renderSecurityPage();
    await user.click(within(await panel()).getByRole("button", { name: "Unlink" }));
    await user.click(
      within(await screen.findByRole("dialog", { name: "Unlink your Microsoft account" })).getByRole("button", {
        name: "Unlink",
      }),
    );
    await waitFor(() => expect(calls.unlinkMicrosoft).toHaveBeenCalledOnce());
  });

  it("does not offer to unlink the last way in", async () => {
    calls.getMySecurity.mockResolvedValue({ ...MY_SECURITY_FIXTURE, has_password: false, microsoft: LINK });
    renderSecurityPage();
    expect(within(await panel()).getByRole("button", { name: "Unlink" })).toBeDisabled();
  });
});
```

En `frontend/src/modules/settings/__tests__/harness.tsx`, `calls` gana también `microsoftStart: vi.fn(), unlinkMicrosoft: vi.fn(),`, con `calls.microsoftStart.mockResolvedValue({ authorize_url: "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize" })` y `calls.unlinkMicrosoft.mockResolvedValue(undefined)` en `installDefaults`.

En `frontend/src/modules/clients/__tests__/clients.test.tsx`: `ANDES` gana `microsoft_tenants: []` (y con él `INTERNAL` y `UNASSIGNED`, que lo esparcen), y al final:

```tsx
describe("a client's Microsoft organizations", () => {
  it("are edited one per line and sent as a list", async () => {
    const tenant = "9a1b2c3d-4e5f-4a6b-8c7d-0e1f2a3b4c5d";
    const updateClient = vi.fn().mockResolvedValue({ ...ANDES, microsoft_tenants: [tenant] });
    mockUseApi.mockReturnValue(api({ updateClient }));
    const user = userEvent.setup();
    renderAt("/clients/c-andes");
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit client" });
    await user.type(within(dialog).getByLabelText("Microsoft organizations (tenant IDs)"), ` ${tenant} \n\n`);
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(updateClient).toHaveBeenCalledWith("c-andes", expect.objectContaining({ microsoft_tenants: [tenant] })),
    );
  });
});
```

(«Edit» es el botón de la pestaña Resumen, `clients.summary.edit` en `SummaryTab.tsx`; «Edit client», `clients.form.editTitle`.)

- [ ] **Step 2: Verlas fallar**

Run (desde `frontend/`): `npx vitest run src/modules/auth/__tests__/microsoft.test.tsx src/modules/settings/__tests__/microsoft.test.tsx src/modules/profile/SecurityPage.microsoft.test.tsx src/modules/clients/__tests__/clients.test.tsx`
Expected: FAIL (`../microsoft` no se resuelve; no hay botón ni pestaña).

- [ ] **Step 3: El cliente**

En `frontend/src/lib/api.ts`: `ClientOut` gana `/** Microsoft organisations (tenant IDs) whose accounts this client's contacts may sign in with (own-auth phase 2, at most 20). */ microsoft_tenants: string[];` y `ClientUpdate`, `microsoft_tenants?: string[];`. Junto a `PortalMe`:

```ts
/** `POST /auth/microsoft/start` (own-auth phase 2, design §4). */
export interface MicrosoftStartIn {
  intent: "sign_in" | "link" | "invite";
  /** Which page asked: the console (technicians, Novieri's tenant) or the portal (contacts). */
  audience: "staff" | "contact";
  /** The invitation's token, for `invite`: it travels in this body only. */
  token?: string;
}

/** `POST /auth/microsoft/complete`: where the sign-in stands, or `linked`. */
export type MicrosoftStep = LoginResult | { state: "linked" };

/** `GET /settings/microsoft-sign-in`: never the client secret; `configured` says whether one is stored. */
export interface MicrosoftSettingsOut {
  enabled: boolean;
  app_client_id: string | null;
  staff_tenant_id: string | null;
  configured: boolean;
  /** To register in the Microsoft application; `null` while the console's address is not configured. */
  redirect_uri: string | null;
  updated_at: string | null;
  updated_by: string | null;
}

/** `PUT /settings/microsoft-sign-in`: every value; `client_secret` null or absent keeps the stored one. */
export interface MicrosoftSettingsUpdate {
  enabled: boolean;
  app_client_id: string | null;
  staff_tenant_id: string | null;
  client_secret: string | null;
}
```

(`LoginResult` y `EnrollFrom` ya vienen en el `import type` de `../modules/auth/types`), y en `ApiClient`, después de `passkeySignIn`:

```ts
  /**
   * Start a sign-in with Microsoft. `signedIn` for `link`, from Sign-in and
   * security: there a 401 is the session ending, and a 403
   * `step_up_required` goes through `useStepUp().run`.
   */
  microsoftStart(body: MicrosoftStartIn, { signedIn = false }: EnrollFrom = {}): Promise<{ authorize_url: string }> {
    return this.request<{ authorize_url: string }>(
      "/auth/microsoft/start",
      { method: "POST", body: JSON.stringify(body) },
      signedIn ? { global: true } : { anonymous: true },
    );
  }

  /** The code and the state Microsoft handed the browser, in the body; the transaction's cookie travels with it. */
  microsoftComplete(body: { code: string; state: string }): Promise<MicrosoftStep> {
    return this.request<MicrosoftStep>(
      "/auth/microsoft/complete",
      { method: "POST", body: JSON.stringify(body) },
      { anonymous: true },
    );
  }
```

después de `addPassword`:

```ts
  /** Unlink your Microsoft account: a recent confirmation; never the last way in (409 `last_method`). */
  unlinkMicrosoft(): Promise<void> {
    return this.request<void>("/me/microsoft", { method: "DELETE" }, { global: true });
  }
```

y después de `putSessionSettings`:

```ts
  /** `orgs.manage`. The secret is never in the answer. */
  getMicrosoftSettings(): Promise<MicrosoftSettingsOut> {
    return this.request<MicrosoftSettingsOut>("/settings/microsoft-sign-in");
  }

  /** `orgs.manage` on every client. The secret travels here once and comes back in nothing. */
  putMicrosoftSettings(body: MicrosoftSettingsUpdate): Promise<MicrosoftSettingsOut> {
    return this.request<MicrosoftSettingsOut>("/settings/microsoft-sign-in", {
      method: "PUT",
      body: JSON.stringify(body),
    });
  }
```
- [ ] **Step 4: El viaje de ida y vuelta**

Crear `frontend/src/modules/auth/microsoft.ts`:

```ts
/**
 * SIGN IN WITH MICROSOFT, THE CONSOLE'S HALF (own-auth phase 2, Task 12;
 * design §4; plan D30).
 *
 * THE SERVER HOLDS THE TRANSACTION. `POST /auth/microsoft/start` stores the
 * state and the nonce, sets the `__Host-itms_entra` cookie (the PKCE
 * verifier) and answers Microsoft's address. Microsoft sends the browser back
 * to `/sign-in/microsoft#code=...&state=...` (`response_mode=fragment`): a
 * fragment reaches no server log and no `Referer`, and the page posts it
 * same-origin, so the Strict cookie travels with it.
 *
 * WHAT THE TAB KEEPS (decision 3) is where to come back to: an intent and a
 * path, never a secret, in `sessionStorage`, read once and filtered by
 * `safeNext`. Storage that throws (a private window, blocked site data) only
 * means coming back to the root.
 *
 * THE CONSOLE LEAVES ONLY FOR MICROSOFT (decision 4): `authorize_url` is
 * checked against the one host the server speaks to (`entra.MICROSOFT_HOST`,
 * [M3]) before the whole document goes there.
 */
import { translate } from "../../i18n/core";
import { ApiError, apiErrorCode } from "../../lib/api";
import { DEFAULT_AFTER_SIGN_IN, safeNext } from "../../lib/session";

export const MICROSOFT_ORIGIN = "https://login.microsoftonline.com";

const RETURN_KEY = "itms.microsoft.return";

export type MicrosoftIntent = "sign_in" | "link" | "invite";

export interface MicrosoftReturn {
  intent: MicrosoftIntent;
  /** A page of the console, already through `safeNext`. */
  back: string;
}

/** Before leaving for Microsoft: where to come back to. */
export function rememberMicrosoftReturn(value: MicrosoftReturn): void {
  try {
    window.sessionStorage.setItem(RETURN_KEY, JSON.stringify(value));
  } catch {
    // No storage: the return page lands on the root.
  }
}

/**
 * On the way back: where to go. A missing or unreadable value is the root.
 * PURE, so a state initializer may call it (React calls those twice in
 * strict mode); the page forgets it once it has sent the code.
 */
export function readMicrosoftReturn(): MicrosoftReturn {
  let raw: string | null = null;
  try {
    raw = window.sessionStorage.getItem(RETURN_KEY);
  } catch {
    raw = null;
  }
  try {
    const parsed = raw ? (JSON.parse(raw) as Partial<MicrosoftReturn>) : {};
    const intent: MicrosoftIntent =
      parsed.intent === "link" || parsed.intent === "invite" ? parsed.intent : "sign_in";
    return { intent, back: safeNext(parsed.back) };
  } catch {
    return { intent: "sign_in", back: DEFAULT_AFTER_SIGN_IN };
  }
}

/** Once the return has been used: nothing of it stays in the tab. */
export function forgetMicrosoftReturn(): void {
  try {
    window.sessionStorage.removeItem(RETURN_KEY);
  } catch {
    // Nothing was kept.
  }
}

/** Read it and forget it. */
export function takeMicrosoftReturn(): MicrosoftReturn {
  const value = readMicrosoftReturn();
  forgetMicrosoftReturn();
  return value;
}

/** What Microsoft put in the fragment: a code and a state, its refusal, or nothing usable. */
export function microsoftReturnOf(hash: string): { code: string; state: string } | { error: string } | null {
  const params = new URLSearchParams(hash.startsWith("#") ? hash.slice(1) : hash);
  const error = params.get("error");
  if (error) return { error };
  const code = params.get("code");
  const state = params.get("state");
  return code && state ? { code, state } : null;
}

/** Microsoft's address, only when it is Microsoft's (decision 4). */
export function checkedAuthorizeUrl(raw: string): string | null {
  try {
    return new URL(raw).origin === MICROSOFT_ORIGIN ? raw : null;
  } catch {
    return null;
  }
}

/** The sentence for a refused step of the Microsoft sign-in, or `null` when it is not one of its codes. */
export function describeMicrosoftError(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;
  switch (apiErrorCode(error)) {
    case "microsoft_unavailable":
      return translate("auth.microsoft.unavailable");
    case "microsoft_failed":
      return translate("auth.microsoft.failed");
    case "microsoft_not_linked":
      return translate("auth.microsoft.notLinked");
    case "microsoft_tenant_refused":
      return translate("auth.microsoft.tenantRefused");
    case "microsoft_already_linked":
      return translate("auth.microsoft.alreadyLinked");
    case "last_method":
      return translate("auth.microsoft.lastMethod");
    default:
      return null;
  }
}
```

Crear `frontend/src/modules/auth/MicrosoftButton.tsx`:

```tsx
/**
 * "Sign in with Microsoft" (own-auth phase 2, Task 12): asks the server to
 * start, remembers where to come back to, and leaves -- for Microsoft only.
 * A refusal is one sentence under the button. `run` is `useStepUp().run`
 * where the server may ask for a recent confirmation (linking).
 */
import { useRef, useState } from "react";

import type { StepUpRun } from "../../components/stepUp";
import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";
import { translate } from "../../i18n/core";
import type { MicrosoftStartIn } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { navigation } from "../../lib/session";
import { Btn } from "../../shell/components";
import { AuthAlert } from "./AuthCard";
import { describeAuthError } from "./errors";
import { checkedAuthorizeUrl, describeMicrosoftError, rememberMicrosoftReturn } from "./microsoft";

export function MicrosoftButton({
  start,
  back,
  label,
  run,
  variant = "secondary",
}: {
  start: MicrosoftStartIn;
  back: string;
  label: MessageKey;
  run?: StepUpRun;
  variant?: "primary" | "secondary";
}) {
  const t = useT();
  const api = useApi();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);

  async function go() {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      const ask = () => api.microsoftStart(start, run ? { signedIn: true } : {});
      const { authorize_url } = await (run ? run(ask) : ask());
      const url = checkedAuthorizeUrl(authorize_url);
      if (url === null) {
        setError(translate("auth.microsoft.failed"));
        return;
      }
      rememberMicrosoftReturn({ intent: start.intent, back });
      navigation.assign(url);
    } catch (cause: unknown) {
      setError(describeMicrosoftError(cause) ?? describeAuthError(cause, { canTimeOut: false }));
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  return (
    <>
      <div className="form-actions">
        {/* `key`, not a vendor's mark: the console's icon set has none, and
            the label says whose sign-in it is. */}
        <Btn variant={variant} icon="key" label={busy ? t("auth.common.working") : t(label)} disabled={busy} onClick={() => void go()} />
      </div>
      <AuthAlert text={error} />
    </>
  );
}
```

Crear `frontend/src/modules/auth/MicrosoftCallbackPage.tsx`:

```tsx
/**
 * `/sign-in/microsoft` — Microsoft's return (own-auth phase 2, Task 12,
 * decision 2; design §4).
 *
 * THE FRAGMENT IS READ ONCE AND LEAVES THE ADDRESS, like an emailed link's
 * token (`./flow.ts::useLinkToken`). The code and the state go to the server
 * in a body, ONCE: a `ref` holds the request across React's double mount, and
 * a second one would only be refused (the state is single-use).
 *
 * WHAT THE SERVER ANSWERS DECIDES THE NEXT PAGE, as on `/sign-in`:
 * `signed_in` -> where the person was going; `factor_required` and
 * `enroll_required` -> the steps of the first sign-in, carrying it; `linked`
 * -> back to Sign-in and security. Microsoft's own refusal (`#error=`) is
 * said in this console's words -- its `error_description` is never shown --
 * and nothing is sent.
 */
import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { useApi } from "../../lib/ApiContext";
import { enterConsole, signInPath } from "../../lib/session";
import { AuthAlert, AuthCard } from "./AuthCard";
import { describeAuthError } from "./errors";
import { carryNext } from "./flow";
import { describeMicrosoftError, forgetMicrosoftReturn, microsoftReturnOf, readMicrosoftReturn } from "./microsoft";

export function MicrosoftCallbackPage() {
  const t = useT();
  const api = useApi();
  const location = useLocation();
  const navigate = useNavigate();
  const [answer] = useState(() => microsoftReturnOf(location.hash));
  const [target] = useState(() => readMicrosoftReturn());
  const [error, setError] = useState<string | null>(() =>
    answer === null
      ? translate("auth.microsoft.failed")
      : "error" in answer
        ? translate("auth.microsoft.refusedByMicrosoft")
        : null,
  );
  const sent = useRef(false);

  useEffect(() => {
    if (location.hash) navigate({ pathname: location.pathname, search: location.search }, { replace: true });
  }, [location.hash, location.pathname, location.search, navigate]);

  useEffect(() => {
    if (sent.current) return;
    sent.current = true;
    forgetMicrosoftReturn();
    if (answer === null || "error" in answer) return;
    api
      .microsoftComplete({ code: answer.code, state: answer.state })
      .then((step) => {
        if (step.state === "signed_in") enterConsole(target.back);
        else if (step.state === "linked") navigate(target.back, { replace: true });
        else if (step.state === "enroll_required") navigate(carryNext("/sign-in/enroll", target.back), { replace: true });
        else
          navigate(carryNext("/sign-in/factor", target.back), {
            replace: true,
            state: { methods: step.methods, rememberDays: step.remember_device_days ?? null },
          });
      })
      .catch((cause: unknown) => {
        setError(describeMicrosoftError(cause) ?? describeAuthError(cause, { canTimeOut: false }));
      });
  }, [answer, api, navigate, target]);

  const signedIn = target.intent === "link";
  return (
    <AuthCard title={t("auth.microsoft.returnTitle")}>
      {error === null ? (
        <p className="note" style={{ margin: 0 }}>
          {t("auth.common.working")}
        </p>
      ) : (
        <>
          <AuthAlert text={error} />
          <p className="note" style={{ margin: 0 }}>
            <Link to={signedIn ? target.back : signInPath(target.back)}>
              {t(signedIn ? "auth.microsoft.backToSecurity" : "auth.microsoft.backToSignIn")}
            </Link>
          </p>
        </>
      )}
    </AuthCard>
  );
}
```

(Para un vínculo, `target.back` es la página de seguridad, de la consola o del portal; para entrar, `signInPath` elige la página de entrada del portal cuando `back` es del portal.)

- [ ] **Step 5: Los botones**

En `frontend/src/modules/auth/SignInPage.tsx`: importar `MicrosoftButton` y `safeNext` (`../../lib/session`); el docstring gana «SIGN IN WITH MICROSOFT (own-auth phase 2) is offered below the form, always: no anonymous route says whether it is set up here, and a click where it is not gets one sentence (Task 12, decision 1).»; y después del `</Form>`:

```tsx
      <MicrosoftButton start={{ intent: "sign_in", audience: "staff" }} back={safeNext(next)} label="auth.microsoft.signIn" />
```

En `PortalSignInPage.tsx`, después del `</Form>`: `<MicrosoftButton start={{ intent: "sign_in", audience: "contact" }} back={safeNext(next)} label="auth.microsoft.signIn" />` (su `next` ya vale `/portal` por omisión). En `InvitePage.tsx`, dentro del `AuthCard`, después del bloque de la clave de acceso (`offerPasskey`, Task 11) y antes del `<Form>`:

```tsx
      {invite.data.microsoft ? (
        <MicrosoftButton
          start={{ intent: "invite", audience: kind, token }}
          back={kind === "contact" ? "/portal" : "/"}
          label="auth.microsoft.acceptInvite"
          variant={offerPasskey ? "secondary" : "primary"}
        />
      ) : null}
```

- [ ] **Step 6: La cuenta de Microsoft en Mi seguridad**

En `frontend/src/modules/profile/api.ts`, al final:

```ts
/** Unlink the Microsoft account (own-auth phase 2): a recent confirmation, never the last way in. */
export function useUnlinkMicrosoftMutation(run: StepUpRun) {
  const api = useApi();
  const refetch = useRefetchSecurity();
  return useMutation<void, Error, void>({
    mutationFn: () => run(() => api.unlinkMicrosoft()),
    onSuccess: refetch,
  });
}
```

En `frontend/src/modules/profile/SecurityPage.tsx`: `SecurityBody` recibe `back: string` (`"/profile/security"` desde `SecurityPage`, `"/portal/security"` desde `PortalSecurityPage`) y dibuja `<MicrosoftPanel security={data} run={stepUp.run} zone={zone} back={back} />` después de `PasskeysPanel`; y el panel, después de `TotpPanel`:

```tsx
/**
 * The Microsoft account (design §4): linked the first time from here or from
 * the invitation, by its `tid` + `oid`, never by address. Linking leaves for
 * Microsoft after the server's recent confirmation; unlinking is refused when
 * it is the last way in (`last_method`), and this only says so first.
 */
function MicrosoftPanel({
  security,
  run,
  zone,
  back,
}: {
  security: MySecurity;
  run: StepUpRun;
  zone: string | null;
  back: string;
}) {
  const t = useT();
  const unlink = useUnlinkMicrosoftMutation(run);
  const [confirming, setConfirming] = useState(false);
  const link = security.microsoft;
  const lastWayIn =
    security.kind === "contact" ? !security.has_password && security.passkeys.length === 0 : !security.has_password;

  return (
    <Panel title={t("profile.securityPage.microsoft.title")} subtitle={t("profile.securityPage.microsoft.subtitle")}>
      {link ? (
        <>
          <div style={ROW}>
            <div style={ROW_TEXT}>
              <div className="cell-title" style={WRAP_ANYWHERE}>
                {t("profile.securityPage.microsoft.linked", { tenant: link.tenant_id })}
              </div>
              <div className="cell-sub">
                {`${t("profile.securityPage.microsoft.added", { when: describeWhen(link.created_at, undefined, zone) })} · ${
                  link.last_used_at
                    ? t("profile.securityPage.microsoft.lastUsed", { when: describeWhen(link.last_used_at, undefined, zone) })
                    : t("profile.securityPage.microsoft.neverUsed")
                }`}
              </div>
            </div>
            <Btn
              small
              icon="trash"
              label={t("profile.securityPage.microsoft.unlink")}
              disabled={lastWayIn || unlink.isPending}
              title={lastWayIn ? t("profile.securityPage.microsoft.lastWayIn") : undefined}
              onClick={() => setConfirming(true)}
            />
          </div>
          {lastWayIn ? (
            <p className="note" style={{ marginTop: 10 }}>
              {t("profile.securityPage.microsoft.lastWayIn")}
            </p>
          ) : null}
          {unlink.error ? <Failure message={describeMicrosoftError(unlink.error) ?? describeSubmitError(unlink.error)} /> : null}
        </>
      ) : (
        <MicrosoftButton
          start={{ intent: "link", audience: security.kind }}
          back={back}
          label="profile.securityPage.microsoft.link"
          run={run}
        />
      )}
      <ConfirmDialog
        open={confirming}
        title={t("profile.securityPage.microsoft.confirmTitle")}
        body={t("profile.securityPage.microsoft.confirmBody")}
        subject={link?.tenant_id ?? ""}
        confirmLabel={t("profile.securityPage.microsoft.unlink")}
        destructive
        onConfirm={() => {
          setConfirming(false);
          unlink.mutate();
        }}
        onCancel={() => setConfirming(false)}
      />
    </Panel>
  );
}
```

(importar `MicrosoftButton` desde `../auth/MicrosoftButton`, `describeMicrosoftError` desde `../auth/microsoft` y `useUnlinkMicrosoftMutation`). La cabecera gana `//   POST   /auth/microsoft/start, DELETE /me/microsoft             link or unlink a Microsoft account (own-auth phase 2)` en la lista de rutas.

- [ ] **Step 7: Ajustes → Microsoft**

En `frontend/src/modules/settings/tabs.ts`, `"microsoft",` después de `"sessions",`. En `frontend/src/modules/settings/types.ts`, re-exportar `MicrosoftSettingsOut` y `MicrosoftSettingsUpdate` (`export type { MicrosoftSettingsOut, MicrosoftSettingsUpdate } from "../../lib/api";`). En `frontend/src/modules/settings/api.ts`, junto a `SESSION_SETTINGS_KEY`: `export const MICROSOFT_SETTINGS_KEY = "microsoft-settings";` y, después de `useUpdateSessionSettingsMutation`:

```ts
export function useMicrosoftSettings(enabled: boolean) {
  const api = useApi();
  return useQuery<MicrosoftSettingsOut, ApiError>({
    queryKey: [MICROSOFT_SETTINGS_KEY],
    queryFn: () => api.getMicrosoftSettings(),
    enabled,
    ...NO_RETRY,
  });
}

/** Its body can carry the client secret: `gcTime: 0`, and the tab empties the box once it settles. */
export function usePutMicrosoftSettingsMutation() {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation<MicrosoftSettingsOut, ApiError, MicrosoftSettingsUpdate>({
    mutationFn: (body) => api.putMicrosoftSettings(body),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: [MICROSOFT_SETTINGS_KEY] }),
    gcTime: 0,
  });
}
```

Crear `frontend/src/modules/settings/MicrosoftTab.tsx`:

```tsx
// Microsoft sign-in -- `GET /settings/microsoft-sign-in` (`orgs.manage`), `PUT`
// (`orgs.manage` on EVERY client): the one application Novieri registered in
// Microsoft Entra ID, and Novieri's own tenant (own-auth design §4; plan
// D33-D34). One setting for the whole organisation, like Email and Sessions.
//
// THE CLIENT SECRET IS WRITE-ONLY (invariant 3). No answer carries it; the box
// is `type="password"`, always empty, and an empty box keeps the stored one.
// It goes to the vault as a credential, never to the database in the clear.
//
// THE REDIRECT ADDRESS is the server's (`ITS_PUBLIC_CONSOLE_URL` +
// `/sign-in/microsoft`), shown here so it can be copied into the
// application's registration, as a Web platform.
//
// A CLIENT'S CONTACTS sign in from the tenants their client lists (Clients ->
// Edit); the tenant here is for technicians only. Each tenant's administrator
// may have to approve the application once (design §4).

import { useEffect, useState } from "react";

import { Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { FieldGroup, KIND_ICON, Panel, Toggle } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { useGlobalWriteReason, useMicrosoftSettings, usePutMicrosoftSettingsMutation } from "./api";
import { FOLDING_FORM_GRID } from "./formGrid";
import { LoadFailure } from "./LoadFailure";
import { SaveBar } from "./SaveBar";
import { isDirty, isForbidden } from "./settingsState";
import type { MicrosoftSettingsOut } from "./types";

/** Read and write are the same code, as on the Email and Sessions tabs. */
export const MICROSOFT_MANAGE = "orgs.manage";

/** A GUID as Microsoft shows one: 8-4-4-4-12 hexadecimal digits. */
const GUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

interface MicrosoftForm {
  enabled: boolean;
  app_client_id: string;
  staff_tenant_id: string;
}

function seedFrom(data: MicrosoftSettingsOut | undefined): MicrosoftForm {
  return {
    enabled: data?.enabled ?? false,
    app_client_id: data?.app_client_id ?? "",
    staff_tenant_id: data?.staff_tenant_id ?? "",
  };
}

export function MicrosoftTab({ canRead, canEdit }: { canRead: boolean; canEdit: boolean }) {
  const t = useT();
  const reason = useGlobalWriteReason(MICROSOFT_MANAGE);
  const settings = useMicrosoftSettings(canRead);
  const update = usePutMicrosoftSettingsMutation();
  const seed = seedFrom(settings.data);
  const [form, setForm] = useState<MicrosoftForm>(seed);
  // THE SECRET IS NOT PART OF THE FORM'S SEED: it is never read back.
  const [secret, setSecret] = useState("");
  const seedSignature = JSON.stringify(seed);
  useEffect(() => {
    setForm(JSON.parse(seedSignature) as MicrosoftForm);
  }, [seedSignature]);

  const forbidden = isForbidden(settings.error);
  const failed = settings.error != null && !forbidden;
  const editable = canEdit && settings.data != null;
  const badId = [form.app_client_id, form.staff_tenant_id].some((value) => value.trim() !== "" && !GUID.test(value.trim()));
  const dirty = isDirty(form, seed) || secret !== "";

  function submit() {
    if (badId) return;
    update.mutate(
      {
        enabled: form.enabled,
        app_client_id: form.app_client_id.trim() || null,
        staff_tenant_id: form.staff_tenant_id.trim() || null,
        client_secret: secret === "" ? null : secret,
      },
      { onSettled: () => setSecret("") },
    );
  }

  return (
    <Panel title={t("settings.microsoft.title")} subtitle={t("settings.microsoft.subtitle")}>
      {settings.isLoading ? (
        <p className="note">{t("settings.microsoft.loading")}</p>
      ) : forbidden ? (
        <p className="note">{t("settings.microsoft.forbidden", { code: MICROSOFT_MANAGE })}</p>
      ) : failed ? (
        <LoadFailure error={settings.error} />
      ) : (
        <>
          <Toggle
            checked={form.enabled}
            disabled={!editable}
            label={t("settings.microsoft.enabled")}
            onChange={(next) => setForm((current) => ({ ...current, enabled: next }))}
          />
          <FieldGroup title={t("settings.microsoft.groups.application")} icon="key">
            <div className="form-grid" style={FOLDING_FORM_GRID}>
              <Field label={t("settings.microsoft.appId")}>
                <input
                  className={inputClass}
                  spellCheck={false}
                  value={form.app_client_id}
                  disabled={!editable}
                  onChange={(event) => setForm((current) => ({ ...current, app_client_id: event.target.value }))}
                />
              </Field>
              <Field
                label={t("settings.microsoft.secret")}
                help={t(settings.data?.configured ? "settings.microsoft.secretKept" : "settings.microsoft.secretMissing")}
              >
                <input
                  className={inputClass}
                  type="password"
                  autoComplete="off"
                  data-secret
                  value={secret}
                  disabled={!editable}
                  onChange={(event) => setSecret(event.target.value)}
                />
              </Field>
              <Field label={t("settings.microsoft.redirect")} help={t("settings.microsoft.redirectHelp")} full>
                <input
                  className={inputClass}
                  readOnly
                  value={settings.data?.redirect_uri ?? t("settings.microsoft.noConsoleUrl")}
                  onFocus={(event) => event.currentTarget.select()}
                />
              </Field>
            </div>
          </FieldGroup>
          <FieldGroup title={t("settings.microsoft.groups.staff")} icon="users">
            <div className="form-grid" style={FOLDING_FORM_GRID}>
              <Field label={t("settings.microsoft.staffTenant")} help={t("settings.microsoft.staffTenantHelp")}>
                <input
                  className={inputClass}
                  spellCheck={false}
                  value={form.staff_tenant_id}
                  disabled={!editable}
                  onChange={(event) => setForm((current) => ({ ...current, staff_tenant_id: event.target.value }))}
                />
              </Field>
            </div>
            <p className="note" style={{ marginTop: 8 }}>
              {t("settings.microsoft.contactsNote")}
            </p>
          </FieldGroup>
          {badId ? (
            <p className="form-error" style={{ marginTop: 10 }}>
              <Icon name={KIND_ICON.crit} size={13} /> {t("settings.microsoft.badId")}
            </p>
          ) : null}
          <SaveBar
            canEdit={editable && !badId}
            disabledReason={canEdit ? t("settings.microsoft.correct") : reason}
            dirty={dirty}
            pending={update.isPending}
            error={update.error}
            saved={update.isSuccess && !dirty}
            onSave={submit}
          />
        </>
      )}
    </Panel>
  );
}
```

En `SettingsPage.tsx`: la tabla de la cabecera gana `| Microsoft sign-in | \`GET /settings/microsoft-sign-in\` | \`PUT /settings/microsoft-sign-in\` | \`orgs.manage\` / \`orgs.manage\` |` después de la fila de Sessions; importar `MicrosoftTab`; en `tabs`, después de la entrada de Sessions, `...(canManageOrg ? [{ key: "microsoft", label: t("settings.page.tabs.microsoft") }] : []),` (con el mismo comentario de Sessions: «its read is the same code»); y junto al de Sessions, `{current === "microsoft" ? <MicrosoftTab canRead={canManageOrg} canEdit={everywhere.has(ORGS_MANAGE)} /> : null}`. En `frontend/src/modules/settings/fixtures.ts`, `getMicrosoftSettings` y `putMicrosoftSettings` con el `MICROSOFT_SETTINGS_FIXTURE` de las pruebas.

- [ ] **Step 8: Los tenants de un cliente**

En `frontend/src/modules/clients/labels.ts`, junto a `parseDomains`:

```ts
/** One Microsoft tenant ID per line or comma; blanks dropped. The server checks each is a GUID (422 otherwise). */
export function parseTenants(text: string): string[] {
  return text
    .split(/[\s,]+/)
    .map((value) => value.trim().toLowerCase())
    .filter((value) => value !== "");
}
```

En `ClientForm.tsx`: importar `parseTenants`; `const [tenants, setTenants] = useState((client?.microsoft_tenants ?? []).join("\n"));`; en `submit`, la edición manda `{ ...fields, email_domains: emailDomains, microsoft_tenants: parseTenants(tenants) }` cuando `takesDomains`; y, en edición (`client !== null && takesDomains`), un campo después de los dominios:

```tsx
          {client !== null && takesDomains ? (
            <Field label={t("clients.form.microsoftTenants")} help={t("clients.form.microsoftTenantsHelp")} full>
              <textarea
                className={inputClass}
                rows={2}
                spellCheck={false}
                value={tenants}
                onChange={(event) => setTenants(event.target.value)}
              />
            </Field>
          ) : null}
```

(La creación no lo ofrece: `ClientCreate` no lo lleva, Task 5.) En `frontend/src/modules/clients/fixtures.ts`, cada cliente gana `microsoft_tenants: []`.

- [ ] **Step 9: La ruta, los textos y la marcha de harmony**

En `frontend/src/App.tsx`: importar `MicrosoftCallbackPage` y, después de `<Route path="/sign-in/recovery-codes" … />`, `<Route path="/sign-in/microsoft" element={<MicrosoftCallbackPage />} />`. En `frontend/scripts/routes.mjs`, `"/sign-in/microsoft",` después de `"/sign-in/recovery-codes",`. En `frontend/src/modules/auth/__tests__/routes.test.ts`, `"/sign-in/microsoft",` en `PAGES`. (Bajo `/sign-in`, ya pública: `PUBLIC_PATHS` no cambia.) En `frontend/src/modules/auth/fixtures.ts`: `microsoftStart: () => ({ authorize_url: "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize" })`, `microsoftComplete: () => ({ state: "signed_in" as const })`.

En `frontend/src/i18n/en/auth.ts`:

```ts
  microsoft: {
    signIn: "Sign in with Microsoft",
    acceptInvite: "Accept with Microsoft",
    returnTitle: "Signing in with Microsoft",
    unavailable: "Signing in with Microsoft is not set up here.",
    failed: "The sign-in with Microsoft did not finish. Start it again.",
    refusedByMicrosoft: "Microsoft did not sign you in. Try again, or sign in another way.",
    notLinked:
      "This Microsoft account is not linked to anybody here. Sign in another way and link it from your profile, or use your invitation.",
    tenantRefused: "Accounts from this Microsoft organization may not sign in here.",
    alreadyLinked: "That Microsoft account is already linked: to somebody else, or you already have one. Unlink yours first.",
    lastMethod: "That is your last way to sign in. Add a password or a passkey first.",
    backToSignIn: "Back to sign in",
    backToSecurity: "Back to Sign-in and security",
  },
```

y en `es/auth.ts`:

```ts
  microsoft: {
    signIn: "Entrar con Microsoft",
    acceptInvite: "Aceptar con Microsoft",
    returnTitle: "Entrando con Microsoft",
    unavailable: "Entrar con Microsoft no está configurado aquí.",
    failed: "No terminó la entrada con Microsoft. Empiézala de nuevo.",
    refusedByMicrosoft: "Microsoft no te dejó entrar. Inténtalo de nuevo, o entra de otra forma.",
    notLinked:
      "Esta cuenta de Microsoft no está vinculada a nadie aquí. Entra de otra forma y vincúlala desde tu perfil, o usa tu invitación.",
    tenantRefused: "Las cuentas de esta organización de Microsoft no pueden entrar aquí.",
    alreadyLinked: "Esa cuenta de Microsoft ya está vinculada: a otra persona, o ya tienes una. Desvincula la tuya primero.",
    lastMethod: "Es tu última forma de entrar. Añade antes una contraseña o una clave de acceso.",
    backToSignIn: "Volver a entrar",
    backToSecurity: "Volver a Acceso y seguridad",
  },
```

En `en/profile.ts`, `securityPage.microsoft`:

```ts
    microsoft: {
      title: "Microsoft account",
      subtitle: "Sign in with your work or school Microsoft account",
      link: "Link a Microsoft account",
      linked: "Linked to an account of the Microsoft organization {tenant}",
      added: "Linked {when}",
      lastUsed: "last used {when}",
      neverUsed: "not used yet",
      unlink: "Unlink",
      lastWayIn: "This is your only way to sign in. Add a password before you unlink it.",
      confirmTitle: "Unlink your Microsoft account",
      confirmBody: "You will no longer sign in with it. You can link it again later.",
    },
```

y en `es/profile.ts`:

```ts
    microsoft: {
      title: "Cuenta de Microsoft",
      subtitle: "Entra con tu cuenta de Microsoft del trabajo o de la escuela",
      link: "Vincular una cuenta de Microsoft",
      linked: "Vinculada a una cuenta de la organización de Microsoft {tenant}",
      added: "Vinculada {when}",
      lastUsed: "usada por última vez {when}",
      neverUsed: "sin usar todavía",
      unlink: "Desvincular",
      lastWayIn: "Es tu única forma de entrar. Añade una contraseña antes de desvincularla.",
      confirmTitle: "Desvincular tu cuenta de Microsoft",
      confirmBody: "Ya no entrarás con ella. Puedes volver a vincularla más adelante.",
    },
```

En `en/settings.ts`, `page.tabs.microsoft: "Microsoft sign-in"` y la sección:

```ts
  microsoft: {
    title: "Sign in with Microsoft",
    subtitle: "One application registered in Microsoft Entra ID, for technicians and for clients' contacts",
    loading: "Loading…",
    forbidden: "Reading this needs {code}.",
    enabled: "People may sign in with Microsoft",
    groups: { application: "The application", staff: "Technicians" },
    appId: "Application (client) ID",
    secret: "Client secret",
    secretKept: "A secret is stored. Leave this empty to keep it.",
    secretMissing: "No secret is stored yet.",
    redirect: "Redirect URI",
    redirectHelp: "Register this address in the application, as a Web platform.",
    noConsoleUrl: "The console's address is not configured on the server (ITS_PUBLIC_CONSOLE_URL).",
    staffTenant: "Novieri's Microsoft organization (tenant ID)",
    staffTenantHelp: "Technicians may sign in only with accounts from it. Empty: technicians do not sign in with Microsoft.",
    contactsNote:
      "A client's contacts sign in from the organizations their client lists (Clients, Edit). Each organization's administrator may have to approve the application once.",
    badId: "Each identifier is a GUID, as Microsoft shows it: 8-4-4-4-12 hexadecimal digits.",
    correct: "Correct the identifiers to save.",
  },
```

y en `es/settings.ts`, `page.tabs.microsoft: "Entrar con Microsoft"` y:

```ts
  microsoft: {
    title: "Entrar con Microsoft",
    subtitle: "Una aplicación registrada en Microsoft Entra ID, para técnicos y para los contactos de los clientes",
    loading: "Cargando…",
    forbidden: "Para leer esto hace falta {code}.",
    enabled: "Se puede entrar con Microsoft",
    groups: { application: "La aplicación", staff: "Técnicos" },
    appId: "Id. de la aplicación (cliente)",
    secret: "Secreto de cliente",
    secretKept: "Hay un secreto guardado. Déjalo vacío para conservarlo.",
    secretMissing: "Todavía no hay un secreto guardado.",
    redirect: "URI de redirección",
    redirectHelp: "Registra esta dirección en la aplicación, como plataforma Web.",
    noConsoleUrl: "La dirección de la consola no está configurada en el servidor (ITS_PUBLIC_CONSOLE_URL).",
    staffTenant: "Organización de Microsoft de Novieri (id. de inquilino)",
    staffTenantHelp: "Los técnicos solo pueden entrar con cuentas de ella. Vacío: los técnicos no entran con Microsoft.",
    contactsNote:
      "Los contactos de un cliente entran desde las organizaciones que lista su cliente (Clientes, Editar). El administrador de cada organización puede tener que aprobar la aplicación una vez.",
    badId: "Cada identificador es un GUID, como lo muestra Microsoft: 8-4-4-4-12 dígitos hexadecimales.",
    correct: "Corrige los identificadores para guardar.",
  },
```

En `en/clients.ts`, `form.microsoftTenants: "Microsoft organizations (tenant IDs)"`, `form.microsoftTenantsHelp: "One per line. Contacts of this client may sign in with Microsoft accounts from these organizations only. At most 20."`; en `es/clients.ts`, `"Organizaciones de Microsoft (id. de inquilino)"` y `"Una por línea. Los contactos de este cliente solo pueden entrar con cuentas de Microsoft de estas organizaciones. Hasta 20."`.

- [ ] **Step 10: Correr las pruebas, los tipos y la construcción**

Run (desde `frontend/`): `npx vitest run && npx tsc -b && npm run build`
Expected: PASS y sin errores.

- [ ] **Step 11: Commit**

```bash
git add frontend/src frontend/scripts/routes.mjs
git commit -m "feat(console): sign in with Microsoft, link and unlink, and its settings"
```

---

### Task 13: Cierre — falsificadores, documentos, archivos generados, recuentos y el informe

Diseño §9 (documentos que cambian a propósito), §10 (falsificadores 1 y 8). Oleada 7, en el árbol fusionado de las Tasks 1–12.

**Files:**
- Create: `backend/tests/test_auth_phase2_falsifiers.py`
- Regenerate: `frontend/src/lib/globalRoutes.json`
- Modify: `docs/03-security-model.md`, `docs/modules/console-auth/spec.md`, `deploy/README.md`
- Test: la suite completa (backend y consola)

**Interfaces:**
- Consumes: todo lo anterior; `tests.auth_support.contact` (Task 4), `tests.session_support.sign_in`/`refusal_code` (fase 1), `app.core.router_inspect.flatten`, `app.core.rbac.route_permission`, la fixture `a_ticket_for` (`tests/conftest.py`).
- Produces: nada que otra tarea consuma.

**Decisión del plan (1): el falsificador 1 se sostiene en toda la consola, no en una ruta.** La fase 1 lo prueba en `/me` (`test_auth_falsifiers.py::test_falsifier_1_a_contact_session_does_not_pass_the_console_door`), cuando ningún contacto podía entrar todavía. Desde esta fase entran de verdad, así que la prueba recorre el enrutador (el mismo recorrido que `test_route_gating.py`) y exige a **cada** lectura de la consola con permiso que no sea de portal la respuesta 403 `portal_only`. Una ruta que se añada después entra en el recorrido sin que nadie la liste. El falsificador 8 ya lo sostiene la Task 9 (`test_auth_entra_flows.py::test_falsifier_8_an_unknown_account_is_refused_and_nothing_is_made`); esta tarea lo nombra en los documentos.

**Decisión del plan (2): sin secuencia de despliegue en esta fase.** Como en la fase 1, el trabajo vive en la rama de integración y no llega a `main` hasta que acaben los cuatro proyectos (diseño §9: «El despliegue lo absorbe el proyecto 4»). El informe de esta tarea lista lo que el despliegue tendrá que hacer por la fase 2 y dice el camino hasta un `main` desplegado; la secuencia completa, con OpenBao y `migrate`, la da el proyecto 4 (`CLAUDE.md`, «Reporting to the operator»).

- [ ] **Step 1: Escribir el falsificador**

Crear `backend/tests/test_auth_phase2_falsifiers.py`:

```python
"""Own authentication phase 2: falsifier 1 at every console read, and a
contact's portal (plan 2026-11-06, Task 13; design §10).

Phase 1 held falsifier 1 at one route, `/me`, before any contact could sign
in. Contacts sign in now, so the claim is held at EVERY gated console read:
the router is walked (`router_inspect.flatten`, the walk
`test_route_gating.py` makes), and every GET that carries a permission that
is not a portal code must answer a contact's session 403 `portal_only` --
the door's refusal, before the route's own validation or queries. A route
added later is in the walk without anybody listing it.

Falsifier 8 -- an unknown Microsoft account is refused, and no account and
no link is made -- is
`test_auth_entra_flows.py::test_falsifier_8_an_unknown_account_is_refused_and_nothing_is_made`
(Task 9).
"""

import re
from typing import Any
from uuid import uuid4

from fastapi.routing import APIRoute

from app.core.rbac import route_permission
from app.core.router_inspect import flatten
from tests.auth_support import contact
from tests.session_support import refusal_code, sign_in

_PARAM = re.compile(r"\{[^}]+\}")


def _console_reads(app: Any) -> list[str]:
    """Every gated console GET under /api/v1, its parameters filled in."""
    paths: list[str] = []
    for path, route in flatten(app.router):
        if not isinstance(route, APIRoute) or "GET" not in (route.methods or set()):
            continue
        if not path.startswith("/api/v1/"):
            continue
        code = route_permission(route)
        if code is None or code.startswith("portal."):
            continue
        paths.append(_PARAM.sub(lambda _match: str(uuid4()), path))
    return paths


async def test_falsifier_1_no_console_read_lets_a_contact_in(api_env: Any) -> None:
    """Holds `_admit_session`'s contact refusal (Task 4, D42) at every
    console read. Without it a contact would reach RBAC, which refuses with
    another code -- or, for a code a contact's role were ever given, answers."""
    org = await api_env.create_org("f1-every-read")
    who = await contact(api_env, org)
    signed = await sign_in(api_env, org, who.sub)
    reads = _console_reads(api_env.app)
    assert len(reads) > 150, "the walk found too few console reads to mean anything"

    wrong: list[tuple[str, int, str | None]] = []
    for path in reads:
        answer = await api_env.client.get(path, headers=signed.headers)
        code = refusal_code(answer) if answer.status_code >= 400 else None
        if answer.status_code != 403 or code != "portal_only":
            wrong.append((path, answer.status_code, code))
    assert wrong == []

    portal = await api_env.client.get("/api/v1/portal/me", headers=signed.headers)
    assert portal.status_code == 200, portal.text


async def test_falsifier_1_a_contacts_portal_reads_nobody_elses_ticket(
    api_env: Any, a_ticket_for: Any
) -> None:
    """One organization's contact reads no ticket of another person of
    theirs, nor anything of another organization."""
    org = await api_env.create_org("f1-portal")
    other_org = await api_env.create_org("f1-portal-other")
    who = await contact(api_env, org)
    signed = await sign_in(api_env, org, who.sub)
    neighbour = await a_ticket_for(org, "neighbour")
    stranger = await a_ticket_for(other_org, "stranger")

    for ticket in (neighbour, stranger):
        path = f"/api/v1/portal/tickets/{ticket.id}"
        one = await api_env.client.get(path, headers=signed.headers)
        assert one.status_code == 404, one.text
    listed = await api_env.client.get("/api/v1/portal/tickets", headers=signed.headers)
    assert listed.status_code == 200, listed.text
    assert listed.json() == []
```

- [ ] **Step 2: Verla pasar, y verla fallar sin la protección**

Run (desde `backend/`, con `PYTHONPATH=$PWD`): `.venv/bin/pytest -q tests/test_auth_phase2_falsifiers.py`
Expected: PASS (2 tests).

Después, **sin hacer commit**, en `backend/app/api/deps.py::_admit_session` cambia el `code=PORTAL_ONLY` de la negativa del contacto por `code="falsifier-check"` y corre la misma orden.
Expected: FAIL en `test_falsifier_1_no_console_read_lets_a_contact_in`, con todas las lecturas en `wrong`. Deshaz el cambio (`git checkout backend/app/api/deps.py`) y vuelve a correrla: PASS. (Quitar la negativa entera da el mismo FAIL: el contacto llega a RBAC y la respuesta es otra.)

- [ ] **Step 3: El archivo generado y los recuentos**

Run (desde `backend/`): `.venv/bin/python -m tests.export_console_global_routes`
y después `git diff --stat ../frontend/src/lib/globalRoutes.json`.
Expected: el archivo cambia solo si el árbol fusionado difiere de lo que la Task 5 regeneró (la ruta `PUT /api/v1/settings/microsoft-sign-in`); no se edita a mano.

Run (desde `backend/`): `.venv/bin/pytest -q tests/test_console_global_routes.py tests/test_logging_discipline.py tests/test_mcp_channel_api.py tests/test_ai_tool_surface.py tests/test_global_writes.py tests/test_route_gating.py tests/test_mcp_catalog.py tests/test_mcp_descriptions.py tests/test_i18n_catalog.py tests/test_client_catalog.py`
Expected: PASS con los recuentos de las Global Constraints: llamadas de log **109**, `NEVER_EXPOSED` **46**, `EXPECTED_GET_ROUTES` **257**; `GLOBAL_WRITES` con `("PUT", "/api/v1/settings/microsoft-sign-in")`. Si una de estas falla por un recuento, la causa es una fusión (dos tareas sumaron el mismo delta, o ninguna): se corrige el número al de las Global Constraints, nunca la prueba.

Run (desde `frontend/`): `npx vitest run src/modules/audit/auditVocabulary.coverage.test.ts src/i18n src/lib/globalRoutes.test.ts src/lib/api.clientScope.test.ts src/test/routes-covered.test.ts`
Expected: PASS.

- [ ] **Step 4: Los documentos**

En `docs/03-security-model.md`, en el párrafo de la autenticación (el que termina «Not built: SAML and on-premises Active Directory federation (out of scope); sign-in with Microsoft and the contacts' portal are phase 2. MCP clients still authenticate through Keycloak until phase 3.»), esa frase final pasa a:

```markdown
Not built: SAML and on-premises Active Directory federation (out of scope).
MCP clients still authenticate through Keycloak until phase 3.
- **Clients' contacts (own authentication phase 2):** a contact is invited
  from their client's Contacts tab by someone with `clients.manage` on that
  client, with a single-use link that lives 72 hours, and on accepting holds
  the portal role on **their** client and no other. They sign in to the
  portal -- never the console, whose door answers them 403 `portal_only` --
  with a passkey alone, or a password of at least 15 characters checked
  against the same blocklist, with an optional second factor (design §3,
  NIST SP 800-63B-4 §3.1.1.2). Their sessions are their own (60 minutes idle,
  7 days at most by default). Their client's technician can reset their
  access, which ends every way they had in and sends a new invitation.
- **Sign in with Microsoft (own authentication phase 2):** one multi-tenant
  application in Microsoft Entra ID, its client secret in the vault. The
  authorization-code flow with PKCE, `state` and `nonce`; the ID token is
  validated here (signature, audience, issuer, nonce, times) and the person
  is identified by the immutable `tid` + `oid`, **never by email**, as
  Microsoft says to. Nobody gets an account by signing in with Microsoft: a
  person links it from their invitation or their own security page. A
  technician signs in only from Novieri's tenant and is still asked for the
  platform's own second factor (the token does not say whether Microsoft
  asked for one); a contact only from a tenant their client lists.
```

En `docs/modules/console-auth/spec.md`, debajo del párrafo «**Superseded in part on 2026-11-04** …», añadir:

```markdown
**Superseded again on 2026-11-06** (own authentication phase 2,
`docs/superpowers/plans/2026-11-06-autenticacion-propia-fase2.md`): "Sign in
with Microsoft" is the platform's own -- an application in Microsoft Entra
ID configured in Settings -> Microsoft sign-in, the identity kept as `tid` +
`oid` -- and §3.3's Keycloak identity provider no longer signs anybody in to
the console or the portal. It concerns MCP clients only, until phase 3
retires Keycloak.
```

En `deploy/README.md`, inmediatamente antes de `### 3. Sign in with Microsoft — by hand, for now`, añadir:

````markdown
### Sign in with Microsoft — own authentication (phase 2)

**This replaces the Keycloak section below for the console and the
portal.** That section now concerns MCP clients only, until phase 3.

In Microsoft Entra ID, register **one** application for this installation:

1. **Multi-tenant**, for work and school accounts of any organization: the
   platform signs in through the `organizations` authority ("Only users with
   work or school accounts from Microsoft Entra ID can sign in to the
   application",
   https://learn.microsoft.com/en-us/entra/identity-platform/v2-protocols-oidc).
   Technicians come from Novieri's tenant and contacts from their clients'
   tenants; the platform checks the tenant itself.
2. **A web application** (a confidential client: it holds a client secret,
   https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow),
   whose redirect URI is the console's address followed by
   `/sign-in/microsoft`. Settings -> Microsoft sign-in shows the exact value
   to paste, built from `ITS_PUBLIC_CONSOLE_URL`.
3. **A client secret.**
4. The platform asks for `openid profile` and nothing else.

Then, in the console, **Settings -> Microsoft sign-in** (`orgs.manage` on
every client): the application (client) ID, the client secret (it goes to
the vault and is never shown again), and Novieri's tenant ID -- the only
tenant technicians may sign in from. Each client's tenants are on that
client's **Edit** form.

Nothing is added to `.env` and nothing to OpenBao: the secret is a vault
credential sealed by the organization's envelope key, like the SMTP
password. The API already reaches `login.microsoftonline.com` (Email
intake). Depending on each tenant's policy, its administrator may have to
approve the application once.

Nobody gets an account by signing in with Microsoft. A person links their
Microsoft account from their invitation or from Sign-in and security; an
account nobody linked is refused.
````

- [ ] **Step 5: La suite completa**

Run (desde `backend/`, con `PYTHONPATH=$PWD`): `.venv/bin/pytest -q`
Expected: PASS, sin pruebas saltadas nuevas.

Run (desde `backend/`): `.venv/bin/mypy app && .venv/bin/ruff check app tests`
Expected: sin errores. (La ida y vuelta de 0246 la prueba `tests/test_auth_phase2_migration.py` de la Task 1, dentro de la suite; la CI, además, corre `upgrade head → downgrade base → upgrade head` en su job `migrations`.)

Run (desde `frontend/`): `npx vitest run && npx tsc -b && npm run build`
Expected: PASS y sin errores.

- [ ] **Step 6: Commit**

```bash
git add backend/tests/test_auth_phase2_falsifiers.py frontend/src/lib/globalRoutes.json docs/03-security-model.md docs/modules/console-auth/spec.md deploy/README.md
git commit -m "test(auth): falsifier 1 at every console read; docs for contacts and Microsoft (own auth phase 2 Task 13)"
```

(Si `globalRoutes.json` no cambió, `git add` no lo incluye y el commit sigue igual.)

- [ ] **Step 7: El informe**

El informe de la tarea termina con estas líneas, tal cual (decisión 2):

- **Dónde está el trabajo:** en la rama de integración de la fase 2, no en `main`. El operador despliega solo desde `main` (2026-09-11), y la fase 2 no se fusiona a `main` hasta que acaben los cuatro proyectos (diseño §9).
- **Camino hasta un `main` desplegado:** fase 3 (MCP y el asistente, migración 0247) → proyecto 4 (despliegue) → fusión a `main` → la secuencia completa del proyecto 4, que empieza por `docker compose exec openbao bao status`.
- **Lo que la fase 2 añade a esa secuencia:** la migración **0246** (la corre `migrate` como dependencia de `api` en cada `up -d`); **ninguna clave nueva en `.env`**; **ninguna ruta ni política nueva en OpenBao**; ningún servicio nuevo; registrar la aplicación de Entra y rellenar Ajustes → Microsoft sign-in (`deploy/README.md`, «Sign in with Microsoft — own authentication»). Y, por A-M2 (Task 2): un `.env` de producción con `ITS_AUTH_KEYS_BACKEND=local` hace que el api se niegue a arrancar; debe decir `ITS_AUTH_KEYS_BACKEND=openbao`, como ya pide la fase 1.
- **Qué comprobar después:** `/sign-in` y `/portal/sign-in` cargan; un contacto invitado entra al portal y `/devices` lo lleva a `/portal`; «Entrar con Microsoft» con una cuenta no vinculada responde «no está vinculada a nadie aquí».

---

## Puntos de encuentro con la fase 3

La fase 3 (servidor OAuth para MCP y el asistente interno, diseño §5) se planifica en paralelo y encadena su migración **0247** con `down_revision = "0246"`. Estos son los archivos que las dos fases probablemente tocan, con lo que hace aquí la fase 2; al fusionar, **la unión** de los dos cambios salvo donde se dice otra cosa.

| Archivo | Lo que hace la fase 2 | Al fusionar |
|---|---|---|
| `backend/migrations/versions/0246_auth_contacts_microsoft.py` | La única migración de la fase (Task 1): `auth_external_identities`, `auth_entra_settings`, `clients.microsoft_tenants`, `its_auth_org`. | La 0247 de la fase 3 declara `down_revision = "0246"`; nunca dos cabezas (`alembic heads` = una). |
| `backend/app/api/v1/auth.py` | Rutas nuevas: `POST /auth/step-up/password` (T4); `POST /auth/passkey/options`, `POST /auth/passkey` (T6); `POST /me/password` y la dependencia `_account_ctx` en todas las `/me/*` (T7); `POST /auth/invite/passkey/options`, `POST /auth/invite/passkey` y los campos nuevos de `InviteOut` (T8); `POST /auth/microsoft/start`, `POST /auth/microsoft/complete`, `DELETE /me/microsoft` y el límite `_MICROSOFT` (T9). El índice del docstring gana una línea por ruta. | Unión; si la fase 3 pone sus rutas OAuth aquí, el índice del docstring lleva las dos listas. |
| `backend/app/api/deps.py` | `_admit_session` responde `portal_only` a un contacto y deja de registrar esa negativa (T4); `bound_account_context` (T7). | La fase 3 sustituye la puerta de MCP de Keycloak (D5 de la fase 1) en `admit()`: el bloque de `_admit_session` de la fase 2 no se toca; si los dos cambian `admit()`, se fusiona a mano y se corre `tests/test_auth_phase2_falsifiers.py`. |
| `backend/app/core/auth/sessions.py` | `SessionPrincipal.timezone` (último campo, por defecto `None`, T4); `ENTRA_COOKIE`, `set_entra_cookie`, `clear_entra_cookie` (T9). | Unión; un campo nuevo de la fase 3 en `SessionPrincipal` va también con valor por defecto, después de `timezone`. |
| `backend/app/core/auth/flows.py` | Renombra `_started` → `started` y `_after_password` → `after_first_factor(…, method=)` (T4); añade los flujos de contacto (T4, T6, T8). | La fase 3 usa los nombres nuevos. |
| `backend/app/core/auth/keys.py`, `backend/app/core/config.py` | `LocalAuthKeysInProduction` y la negativa en `build()` (T2); en `config.py`, solo el comentario de `auth_keys_backend`. Ningún campo nuevo en `Settings`. | Si la fase 3 añade usos de la clave de firma, `build()` conserva la negativa antes de abrir un archivo. |
| `backend/app/core/auth/retention.py`, `backend/app/worker.py` | El barrido diario (T3) y su entrada de beat `auth-retention`. | La fase 3 puede añadir sus filas caducadas (códigos de autorización, tokens de refresco) como un campo más de `Swept` y una sentencia más en `sweep`, sin otra tarea de beat. |
| `backend/app/core/client_scope_catalog.py` | +2 entradas: `auth_external_identities` (`O("users","user_id")`) y `auth_entra_settings` (`G`) (T1). | Unión. |
| `backend/app/core/client_scope.py` (`ALL_SCOPE_ALLOWLIST`) | +2: `app/core/auth/retention.py::sweep_org` (T3), `app/core/auth/entra_flows.py::_entra_session` (T9). | Unión, cada una con su razón. |
| `backend/app/core/i18n/es.py` | Una entrada por literal nuevo de T4–T9. | Unión; `tests/test_i18n_catalog.py` en el árbol fusionado. |
| `backend/app/mcp/exclusions.py`, `backend/tests/test_mcp_channel_api.py` | `NEVER_EXPOSED` 36 → **46** (+1 T4, +2 T6, +1 T7, +4 T8, +2 T9); `NOT_FOR_MCP` gana `DELETE /me/microsoft` y las de contactos. | Se suman los deltas de las dos fases al número que vea la fusión; nunca se resta una entrada. |
| `backend/app/mcp/descriptions/part_d.py` | Las descripciones de `GET`/`PUT /settings/microsoft-sign-in` y la de `PATCH /clients/{client_id}` con `microsoft_tenants` (T5). `GET /portal/me` no lleva descripción (`NOT_FOR_MCP`, T4). | Unión. |
| `backend/tests/test_logging_discipline.py` | 108 → **109** (T5: el aviso de `entra.exchange`). | Se suma el delta de la fase 3. |
| `backend/tests/test_ai_tool_surface.py` | `EXPECTED_GET_ROUTES` 255 → **257** (T4 `/portal/me`, T5 `/settings/microsoft-sign-in`); «P5 self-describing» 176 → 177 (T4). | Se suman los deltas. |
| `backend/tests/test_route_gating.py`, `backend/tests/test_global_writes.py` | `UNGATED_BY_DESIGN` gana las rutas nuevas de `/auth/*` y `/me/*`; `GLOBAL_WRITES` gana `PUT /api/v1/settings/microsoft-sign-in` (T5). | Unión. |
| `frontend/src/lib/globalRoutes.json` | Regenerado por T5 y T13. | **Nunca se fusiona a mano**: ante un conflicto, se toma cualquiera de los dos lados y se corre `cd backend && .venv/bin/python -m tests.export_console_global_routes`. |
| `frontend/src/i18n/{en,es}/auditVocabulary.ts`, `frontend/src/modules/audit/auditVocabulary.coverage.test.ts` | Seis acciones nuevas (`contact.invited`, `contact.invite_cancelled`, `contact.access_reset`, `contact.invite_accepted`, `auth.entra_settings_updated`, `auth.entra_secret_used`) y sus entradas en `DYNAMIC`/`REVIEWED_NON_LITERAL_ACTIONS`. | Unión. |
| `frontend/src/lib/api.ts` | Los tipos y métodos de T10–T12 (Apéndice A). | Unión. |
| `frontend/src/App.tsx`, `frontend/scripts/routes.mjs`, `frontend/src/modules/auth/__tests__/routes.test.ts` | Rutas `/portal/sign-in`, `/sign-in/microsoft` (públicas) y `/portal/security`; `RequireAuth` reconoce `portal_only` (T10–T12). | Unión; las tres listas de páginas públicas deben seguir de acuerdo (`routes.test.ts`). |
| `frontend/src/lib/session.ts`, `frontend/src/lib/sessionCheck.ts` | `PUBLIC_PATHS` gana `/portal/sign-in`; `signInPath` y `signOut(api, to?)`; la comprobación de un 401 de contacto por `/portal/me` (T10). | Si la fase 3 añade una página pública (p. ej. el consentimiento OAuth), va a `PUBLIC_PATHS` con la regla de prefijo que reescribió T10 en `routes.test.ts`. |
| `frontend/src/modules/settings/tabs.ts`, `SettingsPage.tsx` | La pestaña `microsoft` (T12). | Unión. |
| `deploy/.env.example` | Solo el comentario de `ITS_AUTH_KEYS_BACKEND` (T2): ninguna clave nueva. | Unión. |
| `deploy/README.md` | La frase de A-M2 (T2) y la sección «Sign in with Microsoft — own authentication» antes de la sección de Keycloak (T13). | Si la fase 3 retira las secciones de Keycloak, conserva la de T13 y quita solo la frase «This replaces the Keycloak section below». |
| `.github/workflows/ci.yml` | **Ningún cambio.** | Lo que añada la fase 3 entra sin conflicto. |
| `backend/app/core/security.py` (`JwksCache`) | **No lo usa** (D44): Microsoft tiene su propia caché de claves en `entra.EntraKeys`. | La fase 3 (o la 4) puede retirarlo con Keycloak sin tocar nada de la fase 2. |
| `backend/app/core/auth/csrf.py` | **Sin cambios.** | — |
| `backend/app/main.py` | Incluye el router de `app/api/v1/entra_settings.py` (T5). | Unión. |

---

## Preguntas para el operador

Lo que el diseño deja abierto, o donde una fuente dice otra cosa que el diseño. Cada una con sus opciones, la recomendación y el texto del diseño del que sale. **El plan sigue la recomendación** mientras el operador no diga otra cosa; la decisión del coordinador (D*n*) y la tarea donde vive están al final de cada una, para que cambiar de opción sea cambiar esa tarea y no buscarla.

**P1. ¿Cuándo pide su segundo factor un contacto que lo tiene?**
Diseño §3: «**Entrada:** llave de acceso o contraseña de **15+** caracteres (NIST, sin segundo factor [S1 §3.1.1.2]) con la misma lista de bloqueo; segundo factor opcional.»
- (a) Con la regla de los técnicos: si la cuenta tiene una clave de acceso o una app TOTP confirmada, la contraseña y Microsoft la piden (salvo equipo recordado); entrar con la clave de acceso sola ya son dos factores.
- (b) Solo si el contacto lo activa expresamente (una opción «pedir siempre un código»), aunque tenga una clave de acceso.
- (c) Nunca al entrar; el segundo factor solo sirve para la confirmación reciente.
Recomendación: **(a)**. Es lo que la persona espera al añadir una app («si está activa, entrar con tu contraseña también pide un código») y no hay un estado más que explicar. D27; Tasks 4 y 11.

**P2. Un técnico invitado que acepta con Microsoft, ¿queda sin contraseña?**
Diseño §4: «La persona tiene que estar invitada; el enlace se hace la primera vez desde la invitación o desde «Mi perfil» y guarda `tid` + `oid`».
- (a) Sí: queda vinculado, sin contraseña, y registra a continuación su propio segundo factor (la plataforma lo pide igual, §4). Puede añadir una contraseña después (`POST /me/password`).
- (b) Debe elegir además una contraseña al aceptar: Microsoft como segunda forma de entrar, nunca la única.
- (c) Un técnico no acepta con Microsoft; lo vincula después desde su perfil.
Recomendación: **(a)**. Si su cuenta de Microsoft deja de funcionar, la recuperación es la de siempre (otro administrador o el comando del servidor, Q5). D41; Tasks 8 y 9.

**P3. ¿Dónde se guarda el secreto de la aplicación de Entra? (contradicción con el diseño)**
Diseño §4: «Su secreto va a OpenBao (invariante 3).» Diseño §9: «OpenBao: rutas nuevas para el pepper, la clave de firma, la contraseña SMTP y el secreto de Entra; `openbao-init` las crea.»
- (a) En la bóveda de la plataforma, como una credencial de tipo `entra_sign_in` sellada por la clave de sobre de la organización (que envuelve OpenBao), escrita desde Ajustes → Microsoft sign-in: el patrón de la contraseña SMTP de la fase 1 (D9). Ni `.env`, ni ruta nueva de OpenBao, ni cambio de política.
- (b) Una ruta KV de OpenBao que `openbao-init` crea desde una clave nueva de `.env` (`ENTRA_CLIENT_SECRET`), como dice §9 al pie de la letra: una clave más en `.env`, cambios en `init.sh` y en `its-app.hcl`, y el secreto no se puede cambiar desde la consola.
Recomendación: **(a)**, y corregir esa línea de §9. El secreto nunca está en claro en la base de datos (invariante 3), lo descifra OpenBao en el momento de usarlo, y «todo lo de Ajustes se edita en Ajustes» (`SettingsPage.tsx`, requisito del operador). D33; Task 5.

**P4. ¿Cuánto se guardan las sesiones, equipos, enlaces y desafíos que ya no valen?**
No está en el diseño; viene de la revisión final de la fase 1 (A-M6: «No retention for the new tables … grow for ever»).
- (a) 30 días después de terminar, caducar, revocarse o usarse; un desafío WebAuthn, 1 día; una invitación sin usar no se borra nunca (es la invitación pendiente que ve Usuarios); un equipo que una sesión aún nombra, tampoco.
- (b) 90 días (más margen para mirar una sesión concreta después de un incidente).
- (c) 7 días.
Recomendación: **(a)**. Quién entró, cuándo y desde dónde queda en el registro de auditoría, que es inmutable y no se barre; estas filas son operativas. D37; Task 3.

**P5. ¿Se envía el enlace de «olvidé mi contraseña» a un contacto que entra sin contraseña?**
Diseño §3: «**Olvidé mi contraseña:** enlace de un solo uso de 30 min; misma respuesta exista o no la cuenta; al cambiarla se cierran sus otras sesiones [S8].»
- (a) Sí, a todo contacto que ya aceptó su invitación (tiene contraseña, clave de acceso o Microsoft): el enlace le deja elegir una contraseña; si tiene segundo factor, lo sigue pidiendo al entrar.
- (b) Solo a quien tiene contraseña.
- (c) Como a los técnicos: solo a quien tiene contraseña y segundo factor (§2.8).
Recomendación: **(a)**. Para un contacto el buzón ya es la raíz de su cuenta (lo invitaron por correo), y la respuesta es la misma en los tres casos. Task 4 (decisión 3).

**P6. ¿Qué es «restablecer el acceso» de un contacto, y quién puede?**
Diseño §3 solo describe la invitación («por quien tenga `clients.manage` en ese cliente»).
- (a) Quitar todo lo que le deja entrar (contraseña, claves, app, códigos, Microsoft, equipos, sesiones) y enviarle una invitación nueva, con `clients.manage` en su cliente y la confirmación reciente del técnico (§2.5).
- (b) Solo cancelar y volver a invitar, sin quitar nada: lo anterior sigue valiendo.
- (c) Como (a), pero reservado a `users.manage` (administración de la organización).
Recomendación: **(a)**. Es la respuesta a «esta cuenta puede estar en malas manos», y quien lleva el cliente es quien se entera. D39; Tasks 8 y 11.

**P7. ¿Una sola ruta de inicio de sesión para técnicos y contactos?**
Diseño §3: «Un contacto nunca pasa la puerta de la consola; solo la del portal» y «Mismos límites de intentos y mensajes genéricos que los técnicos.»
- (a) Una ruta (`POST /auth/login`) y dos páginas (`/sign-in`, `/portal/sign-in`); la persona se busca técnico primero y después contacto (como ya hace `its_auth_principal`, 0245). Consecuencia: una dirección que es a la vez de un técnico y de un contacto entra siempre como técnico, y ese contacto no puede entrar con contraseña (sí con su clave de acceso o con Microsoft, que no buscan por dirección).
- (b) Una ruta aparte para el portal, que solo busca contactos.
- (c) Como (a), y además impedir el caso que queda abierto: `its_contact_email_unique` (0232) ya rechaza un **contacto** con la dirección de cualquier otra persona, pero es de un solo sentido a propósito («A staff row written later with a contact's address is NOT refused»), así que invitar a un técnico con la dirección de un contacto crea la dirección doble. Cerrarlo es una regla más en 0246 y un 409 al invitar.
Recomendación: **(a)**. Las negativas y los contadores son los mismos por construcción, y 0232 explica por qué su regla es de un solo sentido. Si el operador prefiere cerrar el caso, (c) es la regla en la Task 1 y el 409 en la invitación de técnicos de `user_admin`, sin cambiar nada más. D25; Task 4.

**P8. ¿Los contactos usan las rutas `/me/*` de Mi seguridad?**
Diseño §3: «Un contacto nunca pasa la puerta de la consola; solo la del portal (`admit(contacts=True)`).»
- (a) Sí, por la puerta del portal: `/me/security`, `/me/password`, `/me/passkeys/*`, `/me/totp`, `/me/recovery-codes`, `/me/trusted-devices/*`, `/me/sessions/*`, el alta de factores y la confirmación reciente. Solo tocan las filas de autenticación de la propia persona; `GET /me` y el resto de la consola siguen cerrados.
- (b) Rutas propias bajo `/portal/me/*`, duplicando las reglas.
Recomendación: **(a)**: una sola implementación de las reglas. D26; Task 7.

**P9. ¿`response_mode=fragment` o `form_post` para la vuelta de Microsoft? (contradicción con la fuente)**
Diseño §4: «Código de autorización con PKCE, `state` y `nonce` [S62].» Microsoft, en la misma página que el diseño cita ([M2], https://learn.microsoft.com/en-us/entra/identity-platform/v2-protocols-oidc): «Can be form_post or fragment. For web applications, we recommend using response_mode=form_post». Para pedir solo un código, [M1] admite `fragment` («Also supported if requesting only a code»).
- (a) `fragment` a la página `/sign-in/microsoft`, que envía el código por `POST` del mismo origen: el código no llega a ningún log ni `Referer`, y la petición lleva las cookies `SameSite=Strict` (la de la transacción y, al vincular, la de la sesión).
- (b) `form_post` a una ruta del servidor: es un `POST` entre sitios, que no lleva ninguna cookie `Strict`; la transacción necesitaría una cookie `Lax`, y vincular, otra forma de reconocer la sesión.
Recomendación: **(a)**, que se aparta de la recomendación de Microsoft por esa razón y la deja escrita en `entra.py`. D30; Tasks 5 y 9.

**P10. ¿Dónde vive el verificador PKCE y cuánto dura la transacción?**
Diseño §4: «Código de autorización con PKCE, `state` y `nonce`». RFC 9700 §2.1.1 ([R1]): «the PKCE challenge or OpenID Connect nonce MUST be transaction-specific and securely bound to the client and the user agent in which the transaction was started.»
- (a) El verificador es el valor de la cookie `__Host-itms_entra` (`HttpOnly`, `Secure`, `Strict`, 10 min); el servidor guarda en Redis solo su S256, el `nonce` y la intención, bajo el SHA-256 del `state`, de un solo uso, 10 minutos.
- (b) El servidor guarda el verificador; el navegador queda atado solo por el `state`.
Recomendación: **(a)**: un `state` robado no basta sin la cookie de ese navegador. D30; Task 9.

**P11. ¿Con qué autoridad se pide a Microsoft que inicie sesión?**
Diseño §4: «Una app registrada por Novieri, multi-tenant (autoridad `organizations` [S62])» y «**Técnicos:** solo el tenant de Novieri (configurado en Ajustes).»
- (a) La página de la consola pide con el tenant de Novieri (Microsoft solo ofrece sus cuentas); la del portal, con `organizations`. Al volver, manda el tipo de la persona encontrada: técnico solo con `tid` = tenant de Novieri; contacto solo con un `tid` de su cliente.
- (b) Siempre `organizations`, y la comprobación del `tid` al volver.
Recomendación: **(a)**: el mismo resultado, y un técnico ni siquiera ve la elección de cuenta de otra organización. D32; Tasks 5 y 9.

**P12. ¿Códigos de recuperación para los contactos?**
Diseño Q2: «segundo factor opcional»; §2.2 habla de los códigos de los técnicos. Nada para contactos.
- (a) La página los muestra a un contacto solo cuando tiene un segundo factor (sirven entonces para el paso del segundo factor); el servidor no cambia.
- (b) Nunca para contactos: el servidor rechaza `POST /me/recovery-codes` a un contacto; su camino de vuelta es «olvidé mi contraseña» o que su cliente restablezca el acceso (P6).
- (c) Siempre, como a los técnicos.
Recomendación: **(a)**. Task 11 (decisión 3).

**P13. ¿Con qué confirma un contacto un cambio delicado?**
Diseño §2.5: «Exigida en los últimos 10 minutos para: cambiar la propia contraseña o los factores; que un administrador restablezca los factores de otro; sacar una credencial de la bóveda.» Para un contacto sin segundo factor no hay con qué.
- (a) También con su contraseña (`/auth/step-up/password`); un técnico sigue solo con un segundo factor. Un contacto que solo tiene Microsoft confirma volviendo a entrar (entrar deja la confirmación fresca 10 minutos).
- (b) Volviendo a entrar siempre, para todo contacto.
- (c) Volviendo a pasar por Microsoft (`prompt=login`) como confirmación: no construido en esta fase.
Recomendación: **(a)**. D29; Tasks 4 y 11.

**P14. ¿Qué permiso abre Ajustes → Microsoft sign-in?**
Diseño §4: «solo el tenant de Novieri (configurado en Ajustes)».
- (a) Leer con `orgs.manage`; guardar con `orgs.manage` en **todos** los clientes, como Correo y Sesiones (es un ajuste de toda la organización).
- (b) Un código nuevo (`auth.manage`).
Recomendación: **(a)**: sin código nuevo que sembrar en los roles. D34; Task 5.

**P15. ¿Se muestra siempre «Entrar con Microsoft» en las páginas de entrada?**
Diseño §4 no dice cuándo se ofrece.
- (a) Siempre; donde no está configurado, un clic responde «Entrar con Microsoft no está configurado aquí». La invitación sí sabe si está y solo lo ofrece entonces.
- (b) Una lectura anónima nueva (`GET /auth/microsoft` → `{available}`) para ocultarlo: una ruta más antes de la identidad y sus recuentos.
- (c) Un interruptor de construcción de la consola.
Recomendación: **(a)** en esta fase: la instalación de Toeshee lo configura, y no añade superficie antes de la identidad. Task 12 (decisión 1).

**P16. ¿Cuántas organizaciones de Microsoft puede listar un cliente?**
Diseño §4: «**Contactos:** cada cliente lista en su ficha sus tenants permitidos.» Sin número.
- (a) Hasta 20 (`CHECK` en 0246 y el mismo límite en la API); el cliente «sin asignar» ninguno; nunca el tenant de las cuentas personales de Microsoft (`9188040d-6c67-4c5b-b112-36a304b66dad`, [M4]).
- (b) Sin límite.
- (c) Uno solo.
Recomendación: **(a)**: cubre grupos con varias filiales y mantiene la lista legible. D35; Tasks 1 y 5.
